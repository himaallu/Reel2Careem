"""[P1] Confirm -> basket -> price (-> checkout) per ORDER_MODE. Idempotent; mutating calls never retried.

    from app import orders
    outcome = orders.place(token)                    # called by POST /c/{token} after the human taps Confirm
    outcome = orders.place(token, addon=True)        # also add the suggested add-on
    outcome = orders.place(token, pick=0)            # order alternatives[0] instead of the main dish
    page.render(**outcome.to_dict())

ORDER_MODE
  basket            cart_add_item -> cart_get_basket_price, stop. "Complete payment in the Careem app". (default)
  sandbox_checkout  same against the local sandbox, then cart_checkout (fake order, no money)
  live_checkout     production checkout = REAL MONEY. Also needs ALLOW_REAL_MONEY=1, else falls back to basket.

Safety (CLAUDE.md rules 1 + 3): only this module adds to carts or checks out, only after Confirm.
db.claim_checkout() sets checkout_attempted=1 BEFORE the first mutating call; a second tap gets the
existing status and makes no Careem call. A timeout on a mutating call = unknown outcome -> handed_to_app.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any, Optional

from app import db
from app.contracts import Match

log = logging.getLogger("genie.orders")

FINAL = ("basket", "checked_out", "failed", "handed_to_app")
MSG = {
    "basket": "Your basket is ready — complete payment in the Careem app.",
    "checked_out": "Order placed on Careem! 🎉",
    "failed": "Couldn't add this to your Careem basket. Please order it in the Careem app.",
    "handed_to_app": "We're not sure the last step went through — please check your Careem app before trying again.",
    "pending": "We're already working on this order — check your Careem app in a moment.",
    "unknown": "This link has expired or doesn't exist.",
}
_lock = threading.Lock()


@dataclass
class OrderOutcome:
    status: str                       # basket | checked_out | failed | handed_to_app | pending | unknown
    message: str
    mode: str = "basket"              # ORDER_MODE actually used
    target: str = ""                  # production | sandbox
    aed: Optional[float] = None       # Careem's own basket total (fees + promos included)
    basket_id: Optional[int] = None
    invoice_id: Optional[str] = None
    deep_link: Optional[str] = None   # card checkout: user finishes payment here
    eta_min: Optional[int] = None
    lines: list[str] = field(default_factory=list)   # what was added, for the page
    duplicate: bool = False           # True when this tap did nothing because an earlier tap already ran
    detail: str = ""                  # technical reason (dashboard), never shown as the main message

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ================================================================ config
def order_mode() -> tuple[str, str]:
    """(mode, target). live_checkout without ALLOW_REAL_MONEY=1 is downgraded to basket."""
    mode = os.getenv("ORDER_MODE", "basket").strip().lower()
    if mode == "sandbox_checkout":
        return mode, "sandbox"
    from app.careem_mcp import _target_from_env

    if mode == "live_checkout" and os.getenv("ALLOW_REAL_MONEY") != "1":
        log.warning("ORDER_MODE=live_checkout but ALLOW_REAL_MONEY!=1 — using basket mode")
        return "basket", _target_from_env()
    return (mode if mode == "live_checkout" else "basket"), _target_from_env()


def _emit(signal_id: Optional[str], detail: dict[str, Any], level: str = "info") -> None:
    try:
        from app.events import emit
        emit(signal_id, "order", detail, level)
    except Exception:  # noqa: BLE001
        pass


# ================================================================ public API
def status(token: str) -> OrderOutcome:
    """Current state for the page (no Careem calls)."""
    o = db.get_order(token)
    if not o:
        return OrderOutcome(status="none", message="")
    st = o["status"]
    return OrderOutcome(status=st, message=MSG.get(st, ""), aed=o.get("aed"), duplicate=True)


def place(token: str, *, addon: bool = False, pick: Optional[int] = None) -> OrderOutcome:
    """Run the order once per token. Safe to call on every tap."""
    match = db.get_match(token)
    if match is None:
        return OrderOutcome(status="unknown", message=MSG["unknown"])
    mode, target = order_mode()
    choice = _choice(match, pick)
    if choice is None:
        return OrderOutcome(status="failed", message=MSG["failed"], mode=mode, detail="nothing orderable in this match")
    merchant_id, item_id, item_name, price = choice
    with _lock:   # serialises the claim inside this process; the DB claim protects across processes
        claimed = db.claim_checkout(token, price, match.result.service)
    if not claimed:
        prev = status(token)
        prev.message = MSG.get(prev.status, MSG["pending"]) if prev.status in FINAL else MSG["pending"]
        prev.mode, prev.target = mode, target
        _emit(match.signal_id, {"step": "duplicate_tap", "status": prev.status})
        return prev
    try:
        out = _run(match, mode, target, merchant_id, item_id, item_name, addon)
    except Exception as e:  # noqa: BLE001 — must never leave the order in 'pending' silently
        log.exception("order %s crashed", token)
        out = OrderOutcome(status="handed_to_app", message=MSG["handed_to_app"], mode=mode, target=target,
                           detail=f"{type(e).__name__}: {str(e)[:200]}")
    db.set_order_status(token, out.status, out.aed)
    _emit(match.signal_id, {"step": "done", **{k: v for k, v in out.to_dict().items() if k != "message"}},
          "info" if out.status in ("basket", "checked_out") else "warn")
    return out


# ================================================================ internals
def _choice(match: Match, pick: Optional[int]) -> Optional[tuple[int, str, str, Optional[float]]]:
    r = match.result
    if pick is not None and 0 <= pick < len(r.alternatives):
        a = r.alternatives[pick]
        m, it, price = a.merchant, a.item, a.price_aed if a.price_aed is not None else a.item.price_aed
    else:
        m, it, price = r.merchant, r.item, r.item.price_aed if r.item else None
    if not m or not it or not m.id or not it.id:
        return None
    try:
        return int(m.id), str(it.id), it.name, price
    except ValueError:            # e.g. scripted demo ids like "demo-m1"
        return None


def _run(match: Match, mode: str, target: str, merchant_id: int, item_id: str, item_name: str,
         want_addon: bool) -> OrderOutcome:
    from app import careem_mcp

    mcp = careem_mcp.get(target)
    sid = match.signal_id
    res = OrderOutcome(status="failed", message=MSG["failed"], mode=mode, target=target)

    def step(name: str, **kw: Any) -> None:
        _emit(sid, {"step": name, "target": target, **kw})

    # 1. options for customisable items (read; safe to retry)
    options, chosen = _required_options(mcp, merchant_id, item_id, sid)
    step("options", item=item_name, chosen=chosen)

    # 2. add the dish — MUTATING, exactly once
    try:
        added = mcp.cart_call("cart_add_item", {
            "merchant_id": merchant_id, "catalog_item_id": item_id, "count": 1, "domain": "food",
            **({"options": options} if options else {}),
        }, signal_id=sid)
    except careem_mcp.CareemError as e:
        res.status, res.message, res.detail = "handed_to_app", MSG["handed_to_app"], f"cart_add_item: {e}"
        return res
    if added.get("error") or not added.get("basket"):
        res.detail = f"cart_add_item: {added.get('error') or 'no basket returned'}"
        return res
    res.lines.append(item_name + (f" ({', '.join(chosen)})" if chosen else ""))
    step("added", item=item_name)

    # 3. optional add-on — MUTATING, exactly once; failure keeps the main dish
    if want_addon and match.result.addon:
        line = _add_addon(mcp, merchant_id, match.result.addon.name, sid)
        if line:
            res.lines.append(line)
        else:
            res.detail = "add-on could not be added; main dish kept"

    # 4. price the basket (production marks this not read-only -> cart_call, no retry)
    try:
        priced = mcp.cart_call("cart_get_basket_price", {"merchant_id": merchant_id, "domain": "food"}, signal_id=sid)
    except careem_mcp.CareemError as e:
        res.status, res.message, res.detail = "handed_to_app", MSG["basket"], f"price: {e}"
        return res   # items are in the basket; the app will show the price
    total = ((priced.get("price") or {}).get("price") or {}).get("total")
    res.basket_id = (priced.get("basket") or {}).get("id")
    res.eta_min = (priced.get("merchant") or {}).get("eta_in_minutes")
    res.aed = float(total) if isinstance(total, (int, float)) else None
    if priced.get("error") or res.aed is None or res.basket_id is None:
        res.status, res.message = "handed_to_app", MSG["basket"]
        res.detail = f"price: {priced.get('error') or 'no total/basket id'}"
        return res
    step("priced", aed=res.aed, basket_id=res.basket_id)

    if mode == "basket":
        res.status, res.message = "basket", MSG["basket"]
        return res

    # 5. checkout — MUTATING, exactly once, with Careem's own total
    try:
        co = mcp.cart_call("cart_checkout", {"basket_id": int(res.basket_id), "total_price": res.aed,
                                             "domain": "food"}, signal_id=sid, timeout=15)
    except careem_mcp.CareemError as e:
        res.status, res.message, res.detail = "handed_to_app", MSG["handed_to_app"], f"checkout: {e}"
        return res
    if co.get("error") or not co.get("invoice_id"):
        res.status, res.message = "handed_to_app", "Your basket is ready, but checkout didn't go through — " \
                                                   "please finish in the Careem app."
        res.detail = f"checkout: {co.get('error') or 'no invoice'}"
        return res
    res.status, res.message = "checked_out", MSG["checked_out"]
    res.invoice_id, res.deep_link = co.get("invoice_id"), co.get("deep_link")
    if res.deep_link:
        res.message = "Order created — tap to finish paying in Careem."
    step("checked_out", invoice=res.invoice_id, payment=co.get("payment_type"))
    return res


def _required_options(mcp: Any, merchant_id: int, item_id: str, sid: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Cheapest valid choice for every required group. Optional groups are left empty."""
    try:
        o = mcp.call("food_get_item_options", {"restaurant_id": merchant_id, "item_id": int(item_id)},
                     signal_id=sid, timeout=6)
    except Exception as e:  # noqa: BLE001
        log.warning("item options failed (%s) — adding without options", e)
        return [], []
    picks: list[dict[str, Any]] = []
    names: list[str] = []
    for g in o.get("option_groups") or []:
        need = int(g.get("min") or (1 if g.get("required") else 0))
        if not g.get("required") and need == 0:
            continue
        opts = sorted((x for x in g.get("options") or [] if x.get("catalog_option_id")),
                      key=lambda x: (float(x.get("price") or 0), str(x.get("name"))))
        for x in opts[:max(need, 1)]:
            picks.append({"catalog_option_id": x["catalog_option_id"], "count": 1})
            names.append(str(x.get("name")))
    return picks, names


def _add_addon(mcp: Any, merchant_id: int, name: str, sid: str) -> Optional[str]:
    from app import careem_mcp

    try:
        found = mcp.call("food_search_restaurant_items", {"restaurant_id": merchant_id, "query": name},
                         signal_id=sid, timeout=6)
    except Exception:  # noqa: BLE001
        return None
    item = next((d for d in found.get("dishes") or [] if str(d.get("name", "")).strip().lower() == name.lower()), None)
    if not item or item.get("id") is None:
        return None
    options, chosen = _required_options(mcp, merchant_id, str(item["id"]), sid)
    try:
        added = mcp.cart_call("cart_add_item", {
            "merchant_id": merchant_id, "catalog_item_id": str(item["id"]), "count": 1, "domain": "food",
            **({"options": options} if options else {}),
        }, signal_id=sid)
    except careem_mcp.CareemError:
        return None   # unknown outcome for the add-on only; never retried
    return None if added.get("error") else name + (f" ({', '.join(chosen)})" if chosen else "")


@lru_cache(maxsize=4)
def delivery_label(target: Optional[str] = None) -> str:
    """Human label for the delivery location shown on the confirm page (read-only reverse_geocode)."""
    from app import careem_mcp

    try:
        loc = careem_mcp.get(target).call("reverse_geocode", {}, timeout=5).get("location") or {}
        for k in ("formattedAddress", "formatted_address", "title", "name", "address"):
            if isinstance(loc.get(k), str) and loc[k].strip():
                return loc[k].strip()
    except Exception as e:  # noqa: BLE001
        log.warning("reverse_geocode failed: %s", e)
    return "Your current location"


# ================================================================ CLI (end-to-end check on the laptop)
def main() -> int:
    import argparse
    import json
    import time
    import uuid

    p = argparse.ArgumentParser(description="Resolve a post with the agent, then place it per ORDER_MODE")
    p.add_argument("--caption", required=True)
    p.add_argument("--text", default="price?")
    p.add_argument("--addon", action="store_true", help="also add the suggested add-on")
    p.add_argument("--yes", action="store_true", help="required: this changes your REAL Careem basket")
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    mode, target = order_mode()
    if not a.yes:
        print(f"ORDER_MODE={mode} on {target}: this adds items to your Careem basket"
              f"{' and CHECKS OUT' if mode != 'basket' else ''}. Re-run with --yes to continue.")
        return 2
    from app import agent
    from app.contracts import Signal

    db.init_db()
    sig = Signal(id=f"cli:{time.time():.0f}", platform="instagram", kind="comment", source_id=f"cli-{time.time()}",
                 user_ref="cli", text=a.text, caption=a.caption)
    result = agent.resolve(sig)
    print(f"match: {result.status} via {result.mode.upper()} — "
          f"{result.item.name if result.item else '-'} @ {result.merchant.name if result.merchant else '-'}")
    token = uuid.uuid4().hex[:12]
    db.save_match(Match(token=token, signal_id=sig.id, result=result))
    out = place(token, addon=a.addon)
    print(json.dumps(out.to_dict(), indent=2, ensure_ascii=False))
    again = place(token, addon=a.addon)
    print(f"\nsecond tap: duplicate={again.duplicate}, status={again.status} (no Careem calls)")
    return 0 if out.status in ("basket", "checked_out") else 1


if __name__ == "__main__":
    raise SystemExit(main())
