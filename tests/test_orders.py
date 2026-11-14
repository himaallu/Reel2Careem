"""orders.py tests against the local sandbox (python -m sandbox.ctl start first).

    python -m tests.test_orders
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

os.environ["CAREEM_TARGET"] = "sandbox"
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from app import careem_mcp, db, orders  # noqa: E402
from app.contracts import Addon, AgentResult, Alternative, Item, Match, Merchant  # noqa: E402
from sandbox import server as sb  # noqa: E402

failures: list[str] = []
calls: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        failures.append(what)


def force(tool: str | None, scenario: str | None = None) -> None:
    sb.OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    sb.OVERRIDES_PATH.write_text(json.dumps({tool: scenario} if tool else {}))


def spy(mcp: careem_mcp.CareemMCP) -> None:
    orig = mcp.cart_call

    def wrapped(name, args=None, **kw):  # noqa: ANN001
        calls.append(name)
        return orig(name, args, **kw)
    mcp.cart_call = wrapped


def match(item=("700401", "Classic Syrniki", 29.0), merchant=("30104", "Morning Syrniki Café"),
          addon: str | None = None, alts=()) -> str:
    token = uuid.uuid4().hex[:12]
    r = AgentResult(status="found", mode="scripted", wanted="syrniki", merchant=Merchant(merchant[1], merchant[0]),
                    item=Item(item[1], item[0], item[2], 25), addon=Addon(addon, 12.0) if addon else None,
                    alternatives=list(alts), confidence=0.9)
    db.save_match(Match(token=token, signal_id=f"sig-{token}", result=r))
    return token


def mutating() -> list[str]:
    return [c for c in calls if c in careem_mcp.MUTATING]


def main() -> int:
    db.init_db()
    force(None)
    mcp = careem_mcp.get("sandbox")
    spy(mcp)
    os.environ["ORDER_MODE"] = "basket"

    # 1. basket mode: options -> add -> price, stops before checkout
    t = match()
    calls.clear()
    o = orders.place(t)
    check(o.status == "basket" and o.aed == 79.5 and o.basket_id == 6000001 and o.mode == "basket",
          f"basket: status={o.status} AED {o.aed} basket {o.basket_id}")
    check(calls == ["cart_add_item", "cart_get_basket_price"], f"basket mode never checks out {calls}")
    check(db.get_order(t)["checkout_attempted"] == 1 and db.get_order(t)["status"] == "basket", "DB: attempted=1, basket")
    check(o.lines and "(" in o.lines[0], f"required option auto-chosen: {o.lines}")

    # 2. second tap: nothing runs again
    calls.clear()
    o2 = orders.place(t)
    check(o2.duplicate and o2.status == "basket" and calls == [], "second tap returns the same result, 0 Careem calls")

    # 3. five simultaneous taps: exactly one add
    t = match()
    calls.clear()
    outs: list = []
    th = [threading.Thread(target=lambda: outs.append(orders.place(t))) for _ in range(5)]
    [x.start() for x in th]
    [x.join() for x in th]
    check(mutating().count("cart_add_item") == 1 and sum(not x.duplicate for x in outs) == 1,
          f"5 simultaneous taps -> 1 cart_add_item ({mutating()})")

    # 4. add-on looked up by name on the same menu and added once
    t = match(addon="Cappuccino")
    calls.clear()
    o = orders.place(t, addon=True)
    check(mutating().count("cart_add_item") == 2 and len(o.lines) == 2, f"add-on added: {o.lines}")

    # 5. pick an alternative
    t = match(alts=[Alternative(Merchant("Pizza Piazza", "30103"), Item("Margherita Pizza", "700301", 39.0), 39.0, 30)])
    o = orders.place(t, pick=0)
    check(o.lines and o.lines[0].startswith("Margherita Pizza"), f"alternative ordered: {o.lines}")

    # 6. sandbox_checkout: checks out with Careem's own total
    os.environ["ORDER_MODE"] = "sandbox_checkout"
    t = match()
    calls.clear()
    o = orders.place(t)
    check(o.status == "checked_out" and o.invoice_id == "INV-9001" and o.deep_link, f"sandbox checkout -> {o.invoice_id}")
    check(calls[-1] == "cart_checkout" and calls.count("cart_checkout") == 1, "checkout called exactly once")

    # 7. checkout declined -> handed_to_app, not retried
    force("cart_checkout", "payment-declined")
    t = match()
    calls.clear()
    o = orders.place(t)
    force(None)
    check(o.status == "handed_to_app" and calls.count("cart_checkout") == 1 and "Careem app" in o.message,
          f"declined -> handed_to_app, 1 checkout call ({o.detail[:60]})")

    # 8. checkout times out -> handed_to_app, never retried, later taps do nothing
    orig = mcp.cart_call

    def hang(name, args=None, **kw):  # noqa: ANN001
        if name == "cart_checkout":
            calls.append(name)
            raise careem_mcp.CareemTimeout("no answer within 15s")
        return orig(name, args, **kw)
    mcp.cart_call = hang
    t = match()
    calls.clear()
    o = orders.place(t)
    o2 = orders.place(t)
    mcp.cart_call = orig
    check(o.status == "handed_to_app" and calls.count("cart_checkout") == 1 and o2.duplicate,
          "checkout timeout -> 'check your Careem app', never retried, re-tap does nothing")

    # 9. live_checkout without ALLOW_REAL_MONEY=1 -> basket only
    os.environ["ORDER_MODE"] = "live_checkout"
    os.environ.pop("ALLOW_REAL_MONEY", None)
    check(orders.order_mode()[0] == "basket", "live_checkout needs ALLOW_REAL_MONEY=1, else basket")

    # 10. unknown token / scripted demo ids
    os.environ["ORDER_MODE"] = "basket"
    check(orders.place("nope").status == "unknown", "unknown token -> 'link expired'")
    t = match(item=("demo-i1", "Chicken Shawarma Wrap", 18.0), merchant=("demo-m1", "Demo Shawarma House"))
    calls.clear()
    o = orders.place(t)
    check(o.status == "failed" and calls == [], "scripted demo ids -> failed, no Careem calls")

    # 11. cart_add_item business error -> failed (not added), no retry
    force("cart_add_item", "out-of-stock")
    t = match()
    calls.clear()
    o = orders.place(t)
    force(None)
    check(o.status == "failed" and calls == ["cart_add_item"], f"out of stock -> failed, 1 call ({o.detail[:70]})")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
