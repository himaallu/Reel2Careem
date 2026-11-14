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
    "cinnamon roll", "cinnamon rolls", "soft serve", "cookies", "brownie", "churros", "crepe", "tiramisu", "mousse",
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
        squashed = low.replace(" ", "")          # #cinnamonroll counts as "cinnamon roll"
        counts = {}
        for w in set(DISH_WORDS) | set(_catalog_keys()):
            n = len(re.findall(rf"\b{re.escape(w)}\b", low))
            if " " in w:
                n = max(n, squashed.count(w.replace(" ", "")))
            if n:
                counts[w] = n
        for w in list(counts):   # "pizza" inside a matched "pepperoni pizza" doesn't compete with it
            if any(w != o and w in o for o in counts):
                del counts[w]
        if counts:   # most mentioned first, then the longest (most specific) phrase
            return max(counts, key=lambda w: (counts[w], len(w)))
    words = [w for w in re.findall(r"[a-z][a-z'-]+", re.sub(r"[#@]\S+", " ", (signal.caption or "").lower()))
             if w not in _STOP]
    return " ".join(words[:3]) or None


_HANDLE_SUFFIX = re.compile(r"[._-]?(mena|uae|dxb|dubai|ae|me|ksa|official|eats|food|kitchen|hq)$", re.I)


def _clean_handle(h: str) -> str:
    h = h.lstrip("@").strip(".")
    for _ in range(2):
        h = _HANDLE_SUFFIX.sub("", h)
    return re.sub(r"[._]+", " ", h).strip()


def _clean_place(c: str) -> str:
    """'Available at Selected Cinnabon Stores including Deira City Centre!' -> 'Cinnabon'."""
    c = re.sub(r"(?i)\b(available|find (it|them|us)|now|only|exclusively|grab (it|one))\s+(at|in|on)\b", " ", c)
    c = re.sub(r"(?i)\b(selected|all|participating|any)\b", " ", c)
    c = re.sub(r"(?i)\b(stores?|branches?|outlets?|locations?|including)\b.*$", " ", c)
    return re.sub(r"\s+", " ", c).strip(" !.-–—:")


def restaurant_candidates(signal: Signal) -> list[str]:
    """Every restaurant hint in the post, best first: tagged restaurant, 📍 line, 'at X', @handles."""
    cands: list[str] = []
    if signal.restaurant:
        cands.append(_clean_handle(signal.restaurant) if signal.restaurant.startswith("@") else signal.restaurant.strip())
    cap = signal.caption or ""
    for m in re.finditer(r"📍\s*([^\n,]+?)(?:\s+[-–—]\s+|,|\n|$)", cap):   # "📍 Thailand XOCO BAR - Thailand Pavilion"
        c = _clean_place(m.group(1))
        if len(c) >= 3:
            cands.append(c)
    m = re.search(r"\bat\s+((?:[A-Z0-9][\w'&-]*\s?){1,4})", cap)
    if m:
        cands.append(m.group(1).strip())
    for h in re.findall(r"@([\w.]{3,})", cap):
        c = _clean_handle(h)
        if len(c) >= 3:
            cands.append(c)
    seen: set[str] = set()
    return [c for c in cands if not (c.lower() in seen or seen.add(c.lower()))][:4]


def extract_restaurant(signal: Signal) -> Optional[str]:
    c = restaurant_candidates(signal)
    return c[0] if c else None


def caption_words(signal: Signal) -> set[str]:
    text = re.sub(r"@\S+|#\S+|https?://\S+", " ", f"{signal.caption or ''} {signal.text or ''}")
    return {w for w in _words(text) if len(w) > 2}


# ================================================================ RULES
def rule_resolve(signal: Signal) -> AgentResult:
    from app import careem_mcp

    mcp = careem_mcp.get()
    dish = extract_dish(signal)
    cands = restaurant_candidates(signal)
    if not dish and not cands:
        raise FallbackMiss("no dish keyword or restaurant in the post")
    steps = [f"rules: dish={dish!r} restaurants={cands} (data: {mcp.target})"]

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        out = mcp.call(name, args, signal_id=signal.id, timeout=RULES_CALL_TIMEOUT_S, retries=1)
        steps.append(f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})")
        if isinstance(out, dict) and out.get("error") and not (out.get("restaurants") or out.get("dishes")):
            raise FallbackMiss(f"{name}: {str(out['error'])[:120]}")
        return out

    # 1. The restaurant the post names wins: stay there, even if only a close dish exists.
    main: Optional[tuple[dict, dict]] = None
    named_rest: Optional[dict] = None
    for cand in cands:
        rests = _restaurants(call("food_merchant_search", {"query": cand}))
        named_rest = next((r for r in rests if _same_restaurant(r["name"], cand)), None)
        if named_rest:
            break
    if named_rest:
        rid = _int(named_rest["id"])
        if dish:
            best = _best_item(_dishes(call("food_search_restaurant_items", {"restaurant_id": rid, "query": dish})), dish)
            if best:
                main = (named_rest, best)
        if main is None:   # no keyword hit: pick the menu item sharing most words with the caption
            menu_items = _menu_items(call("food_get_restaurant_menu", {"restaurant_id": rid}))
            words = caption_words(signal)
            scored = sorted(menu_items, key=lambda i: (len(words & _words(i["name"])), -i["price"]), reverse=True)
            if scored:
                main = (named_rest, scored[0])
                steps.append(f"closest dish on {named_rest['name']}'s menu: {scored[0]['name']}")
    if not dish:
        dish = (main[1]["name"] if main else None) or (cands[0] if cands else "")
    by_dish = _restaurants(call("food_dish_search", {"query": dish})) if dish else []
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
            alts.append(Alternative(Merchant(r["name"], r["id"], r.get("area")),
                                    Item(best["name"], best["id"], best["price"], r["eta"]), best["price"], r["eta"]))
        if len(alts) == 3:
            break
    exact = bool(dish) and all(w in it["name"].lower() for w in dish.split())
    return AgentResult(
        status="found" if exact else "closest", mode="rules", wanted=dish, service="food",
        merchant=Merchant(rest["name"], rest["id"], rest.get("area")),
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
    if d.get("id") is None or not name or price is None or price <= 0 or d.get("active") is False:
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
        loc = r.get("location")
        loc = loc.get("value") if isinstance(loc, dict) else loc
        res.append({"id": str(r["id"]), "name": _name(r.get("name")), "area": loc if isinstance(loc, str) else None,
                    "eta": int(eta) if isinstance(eta, (int, float)) else None,
                    "items": [i for i in (_item(x) for x in r.get("items") or [] if isinstance(x, dict)) if i]})
    return res


def _menu_items(out: dict[str, Any]) -> list[dict[str, Any]]:
    """Every orderable item in a food_get_restaurant_menu answer (menu.groups[].items[])."""
    found: list[dict[str, Any]] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            it = _item(v) if ("item" in v or "name" in v) and "price" in v else None
            if it:
                found.append(it)
                return
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk((out.get("restaurant") or {}).get("menu") or out.get("menu") or {})
    return found


def _dishes(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in (_item(x) for x in out.get("dishes") or [] if isinstance(x, dict)) if i]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP and len(w) > 1}


_GENERIC = set("""city centre center mall dubai abu dhabi sharjah uae cafe café restaurant restaurants kitchen store stores
selected available including branch branches outlet outlets location locations deira marina jbr jlt level food
bakery house co the and""".split())


def _same_restaurant(rest_name: str, cand: str) -> bool:
    """Distinctive words must match: 'Cake in the City' is NOT 'Cinnabon Stores including Deira City Centre'."""
    sq_r = "".join(sorted(_words(rest_name) - _GENERIC, key=rest_name.lower().find))
    sq_c = "".join(_words(cand) - _GENERIC) if len(_words(cand) - _GENERIC) == 1 else ""
    if len(sq_r) >= 5 and sq_c and (sq_c == sq_r or sq_c.startswith(sq_r)):   # @pizzapiazza -> Pizza Piazza
        return True
    rw = _words(rest_name) - _GENERIC
    cw = _words(cand) - _GENERIC
    inter = rw & cw
    if inter and inter == rw:
        return True                       # every distinctive word of the name is there
    food = {w for d in DISH_WORDS for w in d.split()}
    return bool(inter - food) and len(inter) >= max(1, round(0.5 * len(rw)))   # 'shawarma' alone is not a name


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
    entries = [(e.get("keys") or [], [str(m) for m in e.get("media_ids") or []],
                {**(e.get("result") or {}), "_captured_at": e.get("captured_at")})
               for e in (data.get("entries") or {}).values() if isinstance(e, dict) and e.get("result")]
    raw = _match_entry(signal, entries)
    if not raw:
        raise FallbackMiss("no cached snapshot entry matches this post")
    res = AgentResult.from_dict({**copy.deepcopy(raw), "mode": "cached"})
    res.steps = [f"cached snapshot from {raw.get('_captured_at') or data.get('snapshot_at') or 'unknown time'}"]
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
    """Nothing could answer: say so honestly (no invented dish)."""
    res = AgentResult(status="none", mode="scripted", wanted=(signal.caption or signal.text or "")[:80],
                      steps=[f"last resort: nothing could answer ({why[:120]})"])
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
