"""[P1] Deterministic fallback ladder. Every result carries an honest mode badge.

    live agent failed  ->  RULES  -> CACHED -> SCRIPTED
    AGENT_MODE=cached  ->            CACHED -> SCRIPTED
    AGENT_MODE=scripted ->                     SCRIPTED

RULES    keyword extraction + fixed read-only Careem calls (no LLM). Real data, mode="rules".
CACHED   fixtures/catalog.json — a REAL production snapshot (scripts/snapshot.py), mode="cached".
SCRIPTED fixtures/scripted.json, mode="scripted". Always answers.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from app.contracts import AgentResult, Alternative, Item, Merchant, Signal

log = logging.getLogger("genie.fallbacks")

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = Path(os.getenv("CATALOG_PATH", ROOT / "fixtures" / "catalog.json"))
SCRIPTED_PATH = Path(os.getenv("SCRIPTED_PATH", ROOT / "fixtures" / "scripted.json"))
RULES_CALL_TIMEOUT_S = 5.0


class FallbackMiss(RuntimeError):
    """This rung could not answer; try the next one."""


# ================================================================ ladder
def fallback_resolve(signal: Signal, *, reason: str = "", mode: str = "live") -> AgentResult:
    """Never raises."""
    t0 = time.monotonic()
    rungs = {"live": ("rules", "cached", "scripted"), "rules": ("rules", "cached", "scripted"),
             "cached": ("cached", "scripted")}.get(mode, ("scripted",))
    why = reason
    for rung in rungs:
        try:
            result = {"rules": rule_resolve, "cached": cached_resolve, "scripted": scripted_resolve}[rung](signal)
            result.steps.insert(0, f"fallback to {rung.upper()}: {why[:140]}")
            result.latency_ms = int((time.monotonic() - t0) * 1000)
            _emit(signal.id, "fallback", {"to": rung, "reason": why[:300]}, "warn")
            return result
        except Exception as e:  # noqa: BLE001 — a rung failing just moves us down the ladder
            why = f"{rung} failed: {type(e).__name__}: {e}"
            log.warning(why)
    return _last_resort(signal, why)


def _emit(signal_id: str, stage: str, detail: Any, level: str = "info") -> None:
    try:
        from app.events import emit
        emit(signal_id, stage, detail, level)
    except Exception:  # noqa: BLE001
        pass


# ================================================================ keyword extraction
DISH_WORDS: tuple[str, ...] = (
    "chicken shawarma", "beef shawarma", "shish tawook", "butter chicken", "fried chicken", "smash burger",
    "ice cream", "acai bowl", "poke bowl", "cheese cake", "cheesecake", "pepperoni pizza", "margherita pizza",
    "shawarma", "falafel", "burger", "pizza", "sushi", "biryani", "karak", "manakeesh", "manousheh", "kunafa",
    "knafeh", "hummus", "mandi", "machboos", "luqaimat", "pasta", "ramen", "tacos", "taco", "steak", "wings",
    "fries", "salad", "sandwich", "wrap", "noodles", "dumplings", "poke", "bowl", "coffee", "latte", "matcha",
    "cake", "donut", "waffle", "pancake", "croissant", "kebab", "tikka", "curry", "dosa", "nuggets", "acai",
    "syrniki", "katsu", "bento", "cappuccino", "grill", "kofta", "samosa", "paratha", "pho", "bao", "gelato",
)
ARABIC_DISHES: dict[str, str] = {
    "شاورما": "shawarma", "شاورمة": "shawarma", "برجر": "burger", "برغر": "burger", "بيتزا": "pizza",
    "فلافل": "falafel", "كنافة": "kunafa", "برياني": "biryani", "مندي": "mandi", "مجبوس": "machboos",
    "سوشي": "sushi", "قهوة": "coffee", "كرك": "karak", "حمص": "hummus", "لقيمات": "luqaimat",
}
_STOP = set("""a an the and or of in on at to for with my our your is are was this that it its so very best
the new try tried today tonight here there must love loved amazing yummy price where how much delivery wen
b kam bkam pls please omg wow just got eat eating food foodie dubai uae""".split())


def _catalog_keys() -> list[str]:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        return [k.lower() for e in (data.get("entries") or {}).values() for k in e.get("keys", [])]
    except (OSError, ValueError, AttributeError):
        return []


def extract_dish(signal: Signal) -> Optional[str]:
    """Best dish phrase: longest known phrase, caption before comment; else cleaned caption words."""
    for source in (signal.caption, signal.text):
        if not source:
            continue
        low = source.lower()
        for ar, en in ARABIC_DISHES.items():
            if ar in source:
                return en
        hits = [w for w in set(DISH_WORDS) | set(_catalog_keys()) if re.search(rf"\b{re.escape(w)}\b", low)]
        if hits:
            return max(hits, key=len)
    words = [w for w in re.findall(r"[a-z][a-z'-]+", re.sub(r"[#@]\S+", " ", (signal.caption or "").lower()))
             if w not in _STOP]
    return " ".join(words[:3]) or None


def extract_restaurant(signal: Signal) -> Optional[str]:
    if signal.restaurant:
        return signal.restaurant.lstrip("@").replace("_", " ").replace(".", " ").strip()
    cap = signal.caption or ""
    m = re.search(r"\bat\s+((?:[A-Z0-9][\w'&-]*\s?){1,4})", cap)
    if m:
        return m.group(1).strip()
    m = re.search(r"@([\w.]{3,})", cap)
    if m:
        return m.group(1).replace("_", " ").replace(".", " ").strip()
    return None


# ================================================================ RULES
def rule_resolve(signal: Signal) -> AgentResult:
    from app import careem_mcp

    mcp = careem_mcp.get()
    dish = extract_dish(signal)
    if not dish:
        raise FallbackMiss("no dish keyword in the post")
    rest_q = extract_restaurant(signal)
    steps = [f"rules: dish={dish!r} restaurant={rest_q!r} (data: {mcp.target})"]

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        out = mcp.call(name, args, signal_id=signal.id, timeout=RULES_CALL_TIMEOUT_S, retries=1)
        steps.append(f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})")
        if isinstance(out, dict) and out.get("error") and not (out.get("restaurants") or out.get("dishes")):
            raise FallbackMiss(f"{name}: {str(out['error'])[:120]}")
        return out

    main: Optional[tuple[dict, dict]] = None
    if rest_q:
        rests = _restaurants(call("food_merchant_search", {"query": rest_q}))
        rest = next((r for r in rests if _overlap(r["name"], rest_q)), None)
        if rest:
            dishes = _dishes(call("food_search_restaurant_items", {"restaurant_id": _int(rest["id"]), "query": dish}))
            best = _best_item(dishes, dish)
            if best:
                main = (rest, best)
    by_dish = _restaurants(call("food_dish_search", {"query": dish}))
    if main is None:
        for r in by_dish:
            best = _best_item(r["items"], dish) or (r["items"][0] if r["items"] else None)
            if best:          # dish_search already matched these items, even without shared words
                main = (r, best)
                break
    if main is None:
        return AgentResult(status="none", mode="rules", wanted=dish, missed_demand=dish, confidence=0.5, steps=steps)

    rest, it = main
    alts = []
    for r in by_dish:
        if r["id"] == rest["id"] or r["name"].lower() == rest["name"].lower():
            continue
        best = _best_item(r["items"], dish) or (r["items"][0] if r["items"] else None)
        if best:
            alts.append(Alternative(Merchant(r["name"], r["id"]),
                                    Item(best["name"], best["id"], best["price"], r["eta"]), best["price"], r["eta"]))
        if len(alts) == 3:
            break
    exact = all(w in it["name"].lower() for w in dish.split())
    return AgentResult(
        status="found" if exact else "closest", mode="rules", wanted=dish, service="food",
        merchant=Merchant(rest["name"], rest["id"]),
        item=Item(it["name"], it["id"], it["price"], rest["eta"], it.get("image")),
        alternatives=alts, addon=None, confidence=0.6 if exact else 0.4, steps=steps,
    )


def _name(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("value") or "")
    return str(v or "")


def _price(p: Any) -> Optional[float]:
    if isinstance(p, dict):
        p = p.get("total")
    return float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None


def _int(v: Any) -> Any:
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _item(d: dict[str, Any]) -> Optional[dict[str, Any]]:
    name = _name(d.get("name")) or _name(d.get("item"))
    price = _price(d.get("price"))
    if d.get("id") is None or not name or price is None or d.get("active") is False:
        return None
    return {"id": str(d["id"]), "name": name, "price": price, "image": d.get("image_url")}


def _restaurants(out: dict[str, Any]) -> list[dict[str, Any]]:
    res = []
    for r in out.get("restaurants") or []:
        if not isinstance(r, dict) or r.get("id") is None:
            continue
        if (r.get("availability_details") or {}).get("is_available") is False:
            continue
        eta = (r.get("delivery") or {}).get("average")
        res.append({"id": str(r["id"]), "name": _name(r.get("name")),
                    "eta": int(eta) if isinstance(eta, (int, float)) else None,
                    "items": [i for i in (_item(x) for x in r.get("items") or [] if isinstance(x, dict)) if i]})
    return res


def _dishes(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in (_item(x) for x in out.get("dishes") or [] if isinstance(x, dict)) if i]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP and len(w) > 1}


def _overlap(a: str, b: str) -> bool:
    return bool(_words(a) & _words(b))


def _best_item(items: list[dict[str, Any]], dish: str) -> Optional[dict[str, Any]]:
    want = _words(dish)
    scored = [(len(want & _words(i["name"])), -i["price"], i) for i in items]
    scored = [s for s in scored if s[0] > 0] or ([] if want else scored)
    return max(scored, key=lambda s: (s[0], s[1]))[2] if scored else None


# ================================================================ CACHED / SCRIPTED
def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _match_entry(signal: Signal, entries: list[tuple[list[str], list[str], dict]]) -> Optional[dict]:
    if signal.media_id:
        for keys, media_ids, result in entries:
            if signal.media_id in media_ids:
                return result
    hay = " ".join(x for x in (signal.caption, signal.text, signal.restaurant) if x).lower()
    best: Optional[tuple[int, dict]] = None
    for keys, _, result in entries:
        for k in keys:
            if k and (k.lower() in hay or k in hay) and (best is None or len(k) > best[0]):
                best = (len(k), result)
    return best[1] if best else None


def cached_resolve(signal: Signal) -> AgentResult:
    data = _load(CATALOG_PATH)
    entries = [(e.get("keys") or [], [str(m) for m in e.get("media_ids") or []], e.get("result") or {})
               for e in (data.get("entries") or {}).values() if isinstance(e, dict)]
    raw = _match_entry(signal, entries)
    if not raw:
        raise FallbackMiss("no cached snapshot entry matches this post")
    res = AgentResult.from_dict({**copy.deepcopy(raw), "mode": "cached"})
    res.steps = [f"cached snapshot from {data.get('snapshot_at') or 'unknown time'}"]
    return res


def scripted_resolve(signal: Signal) -> AgentResult:
    data = _load(SCRIPTED_PATH)
    entries = [([k], [], v) for k, v in (data.get("by_keyword") or {}).items()]
    entries += [([], [str(k)], v) for k, v in (data.get("by_media_id") or {}).items()]
    raw = _match_entry(signal, entries) or data.get("default")
    if not raw:
        raise FallbackMiss("scripted.json has no matching entry and no default")
    res = AgentResult.from_dict({**copy.deepcopy(raw), "mode": "scripted"})
    res.steps = ["scripted demo answer (fixtures/scripted.json)"]
    return res


def _last_resort(signal: Signal, why: str) -> AgentResult:
    from app.agent import fake_resolve

    res = fake_resolve(signal)
    res.steps = [f"last resort: built-in scripted answer ({why[:120]})"]
    _emit(signal.id, "fallback", {"to": "scripted", "reason": why[:300]}, "error")
    return res


# ================================================================ CLI
def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Run one fallback rung (or the ladder) and print the AgentResult")
    p.add_argument("rung", choices=["rules", "cached", "scripted", "ladder"])
    p.add_argument("--text", default="price?")
    p.add_argument("--caption", default=None)
    p.add_argument("--restaurant", default=None)
    p.add_argument("--media-id", default=None)
    p.add_argument("--target", choices=["production", "sandbox"], default=None)
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    if a.target:
        os.environ["CAREEM_TARGET"] = a.target
    from app import db
    db.init_db()
    s = Signal(id=f"cli:{time.time():.0f}", platform="instagram", kind="comment", source_id=f"cli-{time.time()}",
               user_ref="cli", text=a.text, caption=a.caption, restaurant=a.restaurant, media_id=a.media_id)
    t = time.monotonic()
    try:
        fn = {"rules": rule_resolve, "cached": cached_resolve, "scripted": scripted_resolve}.get(a.rung)
        res = fn(s) if fn else fallback_resolve(s, reason="manual ladder test", mode="live")
    except Exception as e:  # noqa: BLE001
        print(f"❌ {a.rung} failed after {time.monotonic() - t:.1f}s: {type(e).__name__}: {e}")
        return 1
    print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
    print(f"\n✅ {res.status} via {res.mode.upper()} in {(time.monotonic() - t) * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
