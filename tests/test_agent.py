"""Offline agent tests: a scripted fake Bedrock client driving the real loop against the local sandbox.

    python -m sandbox.ctl start
    python -m tests.test_agent
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import time

os.environ["CAREEM_TARGET"] = "sandbox"
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "t.db"))

from app import agent, careem_mcp, db  # noqa: E402
from app.contracts import Signal  # noqa: E402

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        failures.append(what)


def sig(text: str = "price?", caption: str | None = "Best shawarma wrap in the Marina 🔥") -> Signal:
    t = str(time.time())
    return Signal(id=f"t:{t}", platform="instagram", kind="comment", source_id=t, user_ref="u", text=text,
                  caption=caption)


def tool_msg(*uses: tuple[str, dict]) -> dict:
    return {"output": {"message": {"role": "assistant", "content": [
        {"text": "Searching."}, *[{"toolUse": {"toolUseId": f"tu{i}", "name": n, "input": a}}
                                  for i, (n, a) in enumerate(uses)]]}},
            "stopReason": "tool_use", "usage": {"inputTokens": 1000, "outputTokens": 50}}


class FakeBedrock:
    """Replays a list of responses; records every request so tests can inspect them."""

    def __init__(self, script: list, delay: float = 0.0) -> None:
        self.script, self.delay, self.requests = list(script), delay, []

    def converse(self, **kw):  # noqa: ANN003
        self.requests.append({**kw, "messages": copy.deepcopy(kw["messages"])})
        time.sleep(self.delay)
        step = self.script.pop(0)
        return step(kw) if callable(step) else step


GOOD_SUBMIT = tool_msg((agent.SUBMIT, {
    "status": "found", "wanted": "chicken shawarma wrap", "service": "food", "confidence": 0.9,
    "merchant": {"name": "Shawarma Station", "id": "30101"},
    "item": {"name": "Chicken Shawarma Wrap", "id": "700101", "price_aed": 18, "eta_min": 25},
    "alternatives": [
        {"merchant": {"name": "Invented Grill", "id": "1"}, "item": {"name": "Made Up Wrap"}, "price_aed": 9},
    ],
    "addon": {"name": "Teleported Fries", "price_aed": 99},
}))


def main() -> int:
    db.init_db()
    mcp = careem_mcp.get("sandbox")

    # 1. tool config: read-only only, no lat/lng in schemas, submit tool present
    cfg = agent.tool_config(mcp)
    names = [t["toolSpec"]["name"] for t in cfg["tools"]]
    props = [set((t["toolSpec"]["inputSchema"]["json"].get("properties") or {})) for t in cfg["tools"]]
    check(agent.SUBMIT in names and all(n in careem_mcp.READ_ONLY or n == agent.SUBMIT for n in names),
          f"tool config = read-only tools + submit ({len(names)} tools)")
    check(not any({"latitude", "longitude"} & p for p in props), "latitude/longitude stripped from schemas")

    # 2. happy path: parallel tools, then grounded submit; invented alt + add-on dropped
    fake = FakeBedrock([
        tool_msg(("food_dish_search", {"query": "shawarma"}), ("food_merchant_search", {"query": "shawarma"})),
        tool_msg(("food_get_restaurant_menu", {"restaurant_id": 30101, "latitude": 1, "longitude": 2})),
        GOOD_SUBMIT,
    ])
    s = sig()
    r = agent.run_agent(s, mcp=mcp, client=fake)
    check(r.status == "found" and r.item.name == "Chicken Shawarma Wrap" and r.item.price_aed == 18,
          f"found Chicken Shawarma Wrap AED 18 in {r.latency_ms} ms")
    check(r.mode == "scripted", "sandbox result is labelled scripted, never live")
    check(r.alternatives == [] and r.addon is None, "invented alternative and add-on were dropped")
    check(r.item.image_url and "700101" in r.item.image_url, "item image_url recovered from tool evidence")
    tr = fake.requests[1]["messages"][-1]["content"]
    check(len(tr) == 2 and all("toolResult" in c for c in tr), "2 parallel tool calls -> 2 tool results in one message")
    check(all(len(c["toolResult"]["content"][0]["text"]) <= agent.MAX_TOOL_CHARS + 20 for c in tr),
          "tool results compacted for the model")
    check("image_url" not in tr[0]["toolResult"]["content"][0]["text"], "noise keys removed from model input")
    evs = db.events_for(s.id)
    check(sum(e["stage"] == "agent_step" for e in evs) == 3 and sum(e["stage"] == "tool_call" for e in evs) == 3,
          f"events: {sum(e['stage'] == 'agent_step' for e in evs)} agent_step, "
          f"{sum(e['stage'] == 'tool_call' for e in evs)} tool_call")

    # 2b. real alternatives and add-on from tool results are kept
    fake = FakeBedrock([
        tool_msg(("food_dish_search", {"query": "chicken"})),
        tool_msg((agent.SUBMIT, {
            "status": "found", "wanted": "chicken shawarma", "service": "food", "confidence": 0.8,
            "merchant": {"name": "Shawarma Station", "id": 30101},
            "item": {"name": "Chicken Shawarma Wrap", "id": 700101, "price_aed": 18, "eta_min": 25},
            "alternatives": [
                {"merchant": {"name": "Sakura Sushi Lab", "id": 30102},
                 "item": {"name": "Chicken Katsu Bento", "id": 700202}, "price_aed": 48, "eta_min": 40},
                {"merchant": {"name": "Green Bowl Co.", "id": 30105},
                 "item": {"name": "Chicken Caesar Bowl", "id": 700501}, "price_aed": 38, "eta_min": 30},
                {"merchant": {"name": "Green Bowl Co.", "id": 30105},
                 "item": {"name": "Chicken Caesar Bowl", "id": 700501}, "price_aed": 12, "eta_min": 30},
            ],
            "addon": {"name": "Margherita Pizza", "price_aed": 39},
        })),
    ])
    r = agent.run_agent(sig(), mcp=mcp, client=fake)
    check([a.item.name for a in r.alternatives] == ["Chicken Katsu Bento", "Chicken Caesar Bowl"],
          "2 real alternatives kept, 1 with a wrong price dropped")
    check(r.addon is None, "add-on not in any tool result is dropped (pizza never fetched)")

    # 2c. menu-only path: dish + add-on grounded from the menu alone; add-on must be from that restaurant
    fake = FakeBedrock([
        tool_msg(("food_get_restaurant_menu", {"restaurant_id": 30101})),
        tool_msg((agent.SUBMIT, {
            "status": "found", "wanted": "falafel", "service": "food", "confidence": 0.8,
            "merchant": {"name": "Shawarma Station", "id": 30101},
            "item": {"name": "Falafel Plate", "id": 700102, "price_aed": 22, "eta_min": 25},
            "addon": {"name": "Extra Garlic Sauce", "price_aed": 2},
        })),
    ])
    r = agent.run_agent(sig(caption="falafel at shawarma station"), mcp=mcp, client=fake)
    check(r.item.name == "Falafel Plate" and r.item.eta_min == 25, "dish grounded from the menu alone (ETA from menu)")
    check(r.addon is not None and r.addon.price_aed == 2, "real add-on from the same menu kept")
    ev = agent.Evidence()
    ev.add(mcp.call("food_get_restaurant_menu", {"restaurant_id": 30101}))
    check(not agent._grounded_item(ev, {"name": "Pizza Piazza", "id": 30103}, {"name": "Falafel Plate", "id": 700102}),
          "same dish claimed at another restaurant is rejected")
    check(not agent._grounded_item(ev, {"name": "Shawarma Station", "id": 30101},
                                   {"name": "Falafel Plate", "id": 700102, "price_aed": 0}),
          "price 0 (discount field) does not ground a price")

    # 2d. same-restaurant alternative dropped
    fake = FakeBedrock([tool_msg(("food_get_restaurant_menu", {"restaurant_id": 30101})), tool_msg((agent.SUBMIT, {
        "status": "found", "wanted": "falafel", "service": "food", "confidence": 0.8,
        "merchant": {"name": "Shawarma Station", "id": 30101},
        "item": {"name": "Falafel Plate", "id": 700102, "price_aed": 22},
        "alternatives": [{"merchant": {"name": "Shawarma Station", "id": 30101},
                          "item": {"name": "Mixed Grill Platter", "id": 700103}, "price_aed": 65}]}))])
    r = agent.run_agent(sig(), mcp=mcp, client=fake)
    check(r.alternatives == [] and any("same-restaurant" in x for x in r.steps), "same-restaurant alternative dropped")

    # 2e. agent tool calls are time-boxed: a hanging tool can't eat the budget
    class SlowMCP:
        target = "sandbox"
        def __init__(self): self.calls = []
        def call(self, name, args, *, signal_id=None, timeout=10, retries=2):  # noqa: ANN001
            self.calls.append((timeout, retries)); raise careem_mcp.CareemTimeout("slow")
    slow = SlowMCP()
    agent._run_tools([{"toolUseId": "a", "name": "food_dish_search", "input": {}}], sig(), slow,
                     agent.Evidence(), time.monotonic() + 8)
    t_, r_ = slow.calls[0]
    check(t_ <= 4.1 and r_ == 0, f"8 s left -> per-attempt timeout {t_:.1f}s, retries {r_} (fits before the model's turn)")

    # 3. hallucinated main dish -> AgentError (fallback takes over)
    fake = FakeBedrock([tool_msg(("food_dish_search", {"query": "pizza"})), tool_msg((agent.SUBMIT, {
        "status": "found", "wanted": "pizza", "service": "food", "confidence": 0.9,
        "merchant": {"name": "Luigi Imaginary", "id": "42"}, "item": {"name": "Ghost Pizza", "price_aed": 12}}))])
    try:
        agent.run_agent(sig(caption="pizza"), mcp=mcp, client=fake)
        check(False, "ungrounded main dish rejected")
    except agent.AgentError as e:
        check("not grounded" in str(e), "ungrounded main dish rejected")

    # 4. model forgets submit -> one nudge -> JSON in text accepted
    fake = FakeBedrock([
        {"output": {"message": {"role": "assistant", "content": [{"text": "I think it's sushi."}]}},
         "stopReason": "end_turn"},
        {"output": {"message": {"role": "assistant", "content": [{"text": '{"status":"not_buying","wanted":"",'
                                                                          '"service":"food","confidence":0.8}'}]}},
         "stopReason": "end_turn"},
    ])
    r = agent.run_agent(sig(text="lol nice", caption="sunset"), mcp=mcp, client=fake)
    check(r.status == "not_buying" and len(fake.requests) == 2, "missing submit -> nudge -> parsed")

    # 4b. "none" without any successful search is rejected (no fake missed demand)
    fake = FakeBedrock([tool_msg(("food_dish_search", {"query": "trigger_upstream_down"})), tool_msg((agent.SUBMIT, {
        "status": "none", "wanted": "x", "service": "food", "confidence": 0.1, "missed_demand": "x"}))])
    try:
        agent.run_agent(sig(), mcp=mcp, client=fake)
        check(False, "'none' with no successful search rejected")
    except agent.AgentError as e:
        check("no Careem search succeeded" in str(e), "'none' with no successful search rejected")
    fake = FakeBedrock([tool_msg(("food_dish_search", {"query": "xyz"})), tool_msg((agent.SUBMIT, {
        "status": "none", "wanted": "xyz", "service": "food", "confidence": 0.7}))])
    r = agent.run_agent(sig(caption="xyz"), mcp=mcp, client=fake)
    check(r.status == "none" and r.missed_demand == "xyz", "'none' after a real empty search is accepted")

    # 4c. production without DEMO_LAT/LNG fails fast, before any model call
    prod = careem_mcp.CareemMCP("production")
    saved = {k: os.environ.pop(k, None) for k in ("DEMO_LAT", "DEMO_LNG")}
    fake = FakeBedrock([])
    try:
        agent.run_agent(sig(), mcp=prod, client=fake)
        check(False, "production without location fails fast")
    except agent.AgentError as e:
        check("DEMO_LAT" in str(e) and not fake.requests, "production without location fails fast (0 model calls)")
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v

    # 5. budget: slow model -> AgentError within ~budget
    os.environ["AGENT_BUDGET_S"] = "2"
    agent.BUDGET_S = 2.0
    fake = FakeBedrock([tool_msg(("food_dish_search", {"query": "x"}))] * 5, delay=1.2)
    t = time.monotonic()
    try:
        agent.run_agent(sig(), mcp=mcp, client=fake)
        check(False, "budget enforced")
    except agent.AgentError:
        check(time.monotonic() - t < 3.0, f"budget enforced ({time.monotonic() - t:.1f}s for a 2s budget)")
    agent.BUDGET_S = 20.0

    # 6. Bedrock exception -> resolve() never raises, falls back, emits fallback event
    class Boom:
        def converse(self, **kw):  # noqa: ANN003
            raise RuntimeError("AccessDeniedException")
    agent._bedrock = Boom()
    s = sig()
    r = agent.resolve(s)
    check(r.mode in ("scripted", "cached", "rules") and r.mode != "live", f"resolve() fell back (mode={r.mode})")
    check(any(e["stage"] == "fallback" for e in db.events_for(s.id)), "fallback event emitted")
    agent._bedrock = None

    # 7. disallowed tool requested by the model -> error result, not executed
    out = agent._run_tools([{"toolUseId": "x", "name": "cart_checkout", "input": {}}], sig(), mcp,
                           agent.Evidence(), time.monotonic() + 5)
    check(out[0]["toolResult"]["status"] == "error" and "not allowed" in out[0]["toolResult"]["content"][0]["text"],
          "model asking for cart_checkout gets 'not allowed'")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
