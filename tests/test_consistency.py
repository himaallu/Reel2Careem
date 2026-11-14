"""Consistency + honesty fixes: same reel -> same answer, text DMs judged, RULES stays with the named
restaurant, no invented dishes, confirm page can order an alternative. Needs the sandbox running.

    python -m tests.test_consistency
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

os.environ["CAREEM_TARGET"] = "sandbox"
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")
os.environ["ORDER_MODE"] = "basket"
os.environ["IG_TOKEN"] = ""

from app import agent, careem_mcp, db, fallbacks, intent  # noqa: E402
from app.contracts import AgentResult, Alternative, Item, Match, Merchant, Signal  # noqa: E402

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        failures.append(what)


def sig(text="", caption=None, kind="dm_share", media_id=None, restaurant=None) -> Signal:
    t = f"{time.time():.6f}-{uuid.uuid4().hex[:4]}"
    return Signal(id=f"t:{t}", platform="instagram", kind=kind, source_id=t, user_ref="u", text=text,
                  caption=caption, media_id=media_id, restaurant=restaurant)


class Boom:
    calls = 0

    def converse(self, **kw):  # noqa: ANN003
        Boom.calls += 1
        raise RuntimeError("ExpiredTokenException")


def main() -> int:
    db.init_db()

    # 1. same reel -> same LIVE answer, without calling the model again
    first = sig(caption="Crispy chicken shawarma 📍 Shawarma Station", media_id="REEL-1")
    db.insert_signal(first)
    live = AgentResult(status="found", mode="live", wanted="shawarma", merchant=Merchant("Shawarma Station", "30101"),
                       item=Item("Chicken Shawarma Wrap", "700101", 18, 25), confidence=0.9)
    db.save_match(Match(token=uuid.uuid4().hex[:10], signal_id=first.id, result=live))
    agent._bedrock = Boom()
    again = sig(caption="Crispy chicken shawarma 📍 Shawarma Station", media_id="REEL-1")
    r = agent.resolve(again)
    check(r.mode == "live" and r.item.name == "Chicken Shawarma Wrap" and Boom.calls == 0
          and "reused" in r.steps[0], f"same reel twice -> same LIVE answer, 0 model calls ({r.steps[0][:60]})")
    other = sig(caption="Pepperoni pizza at Pizza Piazza", media_id="REEL-2")
    r = agent.resolve(other)
    check(r.mode == "rules" and Boom.calls == 1, f"a different reel is not reused (mode={r.mode})")
    rules_first = sig(caption="Pepperoni pizza at Pizza Piazza", media_id="REEL-2")
    db.insert_signal(other)
    db.save_match(Match(token=uuid.uuid4().hex[:10], signal_id=other.id, result=r))
    agent.resolve(rules_first)
    check(Boom.calls == 2, "a RULES answer is never reused (live is retried)")
    agent._bedrock = None

    # 2. a plain text DM is judged like a comment; a shared reel always counts
    never = type("N", (), {"converse": lambda self, **k: (_ for _ in ()).throw(AssertionError("no model"))})()
    check(intent.classify(sig(text="Thank you"), client=never).buying is False, "text DM 'Thank you' -> no dish, no reply")
    check(intent.classify(sig(text="", caption="BUNSTICK 📍 PappaRoti", media_id="M"), client=never).buying,
          "shared reel -> always handled")

    # 3. RULES: stays with the restaurant the post names, even without a dish keyword
    r = fallbacks.rule_resolve(sig(caption="Best burger of my life, no joke 📍 Sakura Sushi Lab, JLT"))
    check(r.merchant.name == "Sakura Sushi Lab" and r.status == "closest",
          f"named restaurant wins over a generic keyword: {r.item.name} @ {r.merchant.name} ({r.status})")
    r = fallbacks.rule_resolve(sig(caption="Pepperoni pizza night with @pizzapiazza.uae 🍕"))
    check(r.merchant.name == "Pizza Piazza" and r.item.name == "Pepperoni Pizza",
          f"@handle with a suffix finds the restaurant: {r.item.name} @ {r.merchant.name}")
    check(fallbacks.restaurant_candidates(sig(caption="new menu @bonbird.mena and @momobros_me")) == ["bonbird", "momobros"],
          "handles cleaned: bonbird, momobros")

    # 3b. the real reel that went wrong: 📍 'Available at Selected Cinnabon Stores including Deira City Centre!'
    cap = ("Pecanbon, Pistachio Kunafa & Cookies & Cream or Classic Cinnabons paired with Carvel Soft Serve\n"
           "@cinnabonuae\n📍Available at Selected Cinnabon Stores including Deira City Centre!\n"
           "#cinnabon #cinnabonrolls #cinnamonroll #dubaidesserts")
    s_ = sig(caption=cap)
    check(fallbacks.restaurant_candidates(s_)[0] == "Cinnabon" and fallbacks.extract_dish(s_) == "cinnamon roll",
          f"Cinnabon reel -> restaurant Cinnabon, dish cinnamon roll ({fallbacks.restaurant_candidates(s_)})")
    check(not fallbacks._same_restaurant("Cake in the City", "Selected Cinnabon Stores including Deira City Centre"),
          "'Cake in the City' no longer matches on the word 'City'")
    check(fallbacks._item({"id": 1, "name": "Kunafa", "price": {"total": 0}}) is None, "AED 0 items are never offered")

    # 3c. agent close to its deadline is told to submit what it has (instead of losing everything)
    import copy
    seen_msgs: list = []

    class Slowish:
        script = [
            {"output": {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": "a", "name": "food_dish_search",
                                                                                 "input": {"query": "shawarma"}}}]}},
             "stopReason": "tool_use"},
            {"output": {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": "b", "name": agent.SUBMIT, "input": {
                "status": "found", "wanted": "shawarma", "service": "food", "confidence": 0.8,
                "merchant": {"name": "Shawarma Station", "id": 30101},
                "item": {"name": "Chicken Shawarma Wrap", "id": 700101, "price_aed": 18, "eta_min": 25}}}}]}},
             "stopReason": "tool_use"}]

        def converse(self, **kw):  # noqa: ANN003
            seen_msgs.append(copy.deepcopy(kw["messages"]))
            return Slowish.script.pop(0)
    old_wrap = agent.WRAP_UP_S
    agent.WRAP_UP_S = 1000      # pretend we're always close to the deadline
    r = agent.run_agent(sig(caption="shawarma"), mcp=careem_mcp.get("sandbox"), client=Slowish())
    agent.WRAP_UP_S = old_wrap
    last_user = seen_msgs[-1][-1]["content"]
    check(any("Time is almost up" in (c.get("text") or "") for c in last_user) and r.item.name == "Chicken Shawarma Wrap",
          "near the deadline the model is told to submit now, and does")

    # 4. no invented dish when nothing matches
    old = fallbacks.SCRIPTED_PATH
    r = fallbacks.fallback_resolve(sig(text="hello there"), reason="all down", mode="scripted")
    check(r.status == "none" and r.item is None, f"SCRIPTED with no match -> 'not on Careem', no fake dish ({r.status})")
    fallbacks.SCRIPTED_PATH = Path(tempfile.mkdtemp()) / "missing.json"
    r = fallbacks.fallback_resolve(sig(text="hello"), reason="all down", mode="scripted")
    check(r.status == "none" and r.item is None and "last resort" in r.steps[0], "last resort is honest too")
    fallbacks.SCRIPTED_PATH = old

    # 5. confirm page: alternatives are choosable and pick= orders the alternative
    from fastapi.testclient import TestClient
    from app import orders
    from app.web import app
    token = uuid.uuid4().hex[:12]
    res = AgentResult(status="found", mode="live", wanted="syrniki", merchant=Merchant("Morning Syrniki Café", "30104"),
                      item=Item("Classic Syrniki", "700401", 29, 25), confidence=0.9,
                      alternatives=[Alternative(Merchant("Pizza Piazza", "30103", "JLT"), Item("Margherita Pizza", "700301", 39), 39, 30)])
    db.save_match(Match(token=token, signal_id="sig-confirm", result=res))
    seen: list = []
    orig = orders.place
    orders.place = lambda t, addon=False, pick=None: (seen.append((addon, pick)), orig(t, addon=addon, pick=pick))[1]
    with TestClient(app) as c:
        page = c.get(f"/c/{token}", headers={"User-Agent": "Mozilla/5.0"}).text
        check(page.count('name="pick"') == 2 and "Margherita Pizza" in page, "confirm page lists main + alternative as choices")
        j = c.post(f"/c/{token}/confirm", data={"addon": "true", "pick": "0"}).json()
    orders.place = orig
    check(seen == [(False, 0)] and j["status"] == "basket", f"pick=0 orders the alternative, add-on off ({seen}, {j['status']})")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
