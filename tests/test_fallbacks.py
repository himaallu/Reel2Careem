"""Fallback ladder tests against the local sandbox (python -m sandbox.ctl start first).

    python -m tests.test_fallbacks
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["CAREEM_TARGET"] = "sandbox"
TMP = Path(tempfile.mkdtemp())
os.environ.setdefault("DB_PATH", str(TMP / "t.db"))

from app import agent, db, fallbacks  # noqa: E402
from app.contracts import Signal  # noqa: E402
from sandbox import server as sb  # noqa: E402

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        failures.append(what)


def sig(text="price?", caption=None, restaurant=None, media_id=None) -> Signal:
    t = f"{time.time():.6f}"
    return Signal(id=f"t:{t}", platform="instagram", kind="comment", source_id=t, user_ref="u", text=text,
                  caption=caption, restaurant=restaurant, media_id=media_id)


def force(tool: str | None, scenario: str | None = None) -> None:
    sb.OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    sb.OVERRIDES_PATH.write_text(json.dumps({tool: scenario} if tool else {}))


def main() -> int:
    db.init_db()
    force(None)
    catalog = TMP / "catalog.json"
    catalog.write_text(json.dumps({"snapshot_at": "2026-11-14T12:00:00+00:00", "entries": {"pizza": {
        "keys": ["pizza", "pizza piazza"], "media_ids": ["M1"],
        "result": {"status": "found", "mode": "live", "wanted": "pizza", "service": "food",
                   "merchant": {"name": "Pizza Piazza", "id": "30103"},
                   "item": {"name": "Margherita Pizza", "id": "700301", "price_aed": 39, "eta_min": 30},
                   "alternatives": [], "confidence": 0.8}}}}))
    fallbacks.CATALOG_PATH = catalog

    # keyword extraction
    check(fallbacks.extract_dish(sig(caption="Best chicken shawarma in town 🔥")) == "chicken shawarma",
          "extract longest dish phrase")
    check(fallbacks.extract_dish(sig(text="بكم الشاورما؟")) == "shawarma", "Arabic comment -> shawarma")
    check(fallbacks.extract_restaurant(sig(caption="Pepperoni night at Pizza Piazza!")) == "Pizza Piazza",
          "restaurant from 'at X'")
    check(fallbacks.extract_restaurant(sig(caption="so good @shawarma_station")) == "shawarma station",
          "restaurant from @handle")

    # RULES
    r = fallbacks.rule_resolve(sig(caption="Best shawarma wrap in the Marina"))
    check(r.mode == "rules" and r.status == "found" and r.item.name == "Chicken Shawarma Wrap"
          and r.item.price_aed == 18 and r.item.eta_min == 25, f"rules: shawarma -> {r.item.name} AED {r.item.price_aed}")
    r = fallbacks.rule_resolve(sig(caption="Pepperoni pizza at Pizza Piazza"))
    check(r.merchant.name == "Pizza Piazza" and r.item.name == "Pepperoni Pizza" and r.item.price_aed == 46,
          f"rules: restaurant-first -> {r.item.name} AED {r.item.price_aed}")
    r = fallbacks.rule_resolve(sig(caption="crispy chicken"))
    names = {a.merchant.name for a in r.alternatives}
    check(len(r.alternatives) == 2 and r.merchant.name not in names, f"rules: alternatives from other restaurants {names}")
    r = fallbacks.rule_resolve(sig(caption="xyz special"))
    check(r.status == "none" and r.missed_demand, f"rules: nothing found -> none, missed={r.missed_demand!r}")

    # ladder: Careem down -> CACHED (with snapshot time), cache miss -> SCRIPTED
    force("food_dish_search", "upstream-unavailable")
    s = sig(caption="margherita pizza night")
    r = fallbacks.fallback_resolve(s, reason="bedrock down", mode="live")
    check(r.mode == "cached" and "2026-11-14" in " ".join(r.steps), f"Careem down -> CACHED ({r.steps[:2]})")
    stages = [(e["stage"], json.loads(e["detail"]).get("to")) for e in db.events_for(s.id) if e["stage"] == "fallback"]
    check(stages == [("fallback", "cached")], f"fallback event says CACHED {stages}")
    r = fallbacks.fallback_resolve(sig(caption="shawarma at the beach"), reason="bedrock down", mode="live")
    check(r.mode == "scripted" and r.merchant.name == "Operation Falafel", "Careem down + cache miss -> SCRIPTED")
    force(None)

    # mode switches
    r = fallbacks.fallback_resolve(sig(media_id="M1"), mode="cached")
    check(r.mode == "cached" and r.item.name == "Margherita Pizza", "AGENT_MODE=cached matches by media_id")
    r = fallbacks.fallback_resolve(sig(caption="sushi"), mode="scripted")
    check(r.mode == "scripted", "AGENT_MODE=scripted goes straight to SCRIPTED")

    # never raises, even with broken fixtures
    fallbacks.CATALOG_PATH = TMP / "missing.json"
    old = fallbacks.SCRIPTED_PATH
    bad = TMP / "bad.json"
    bad.write_text("{not json")
    fallbacks.SCRIPTED_PATH = bad
    r = fallbacks.fallback_resolve(sig(caption="xyz"), mode="cached")
    check(r.mode == "scripted" and "last resort" in r.steps[0], "broken fixtures -> built-in last resort, no exception")
    fallbacks.SCRIPTED_PATH = old
    fallbacks.CATALOG_PATH = catalog

    # end to end through agent.resolve(): Bedrock down -> RULES with real (sandbox) data
    class Boom:
        def converse(self, **kw):  # noqa: ANN003
            raise RuntimeError("ExpiredToken")
    agent._bedrock = Boom()
    s = sig(caption="Best shawarma wrap in the Marina")
    r = agent.resolve(s)
    check(r.mode == "rules" and r.item.name == "Chicken Shawarma Wrap", f"agent.resolve: Bedrock down -> RULES ({r.mode})")
    check(any(e["stage"] == "tool_call" for e in db.events_for(s.id)), "RULES tool calls visible on the dashboard")
    agent._bedrock = None

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
