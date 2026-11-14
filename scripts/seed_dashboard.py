"""[P2] Fake history so the dashboard looks alive during development. Every row is flagged demo=1.

  python scripts/seed_dashboard.py            # add ~40 demo signals
  python scripts/seed_dashboard.py --clear    # remove all demo rows (do this before the real demo!)
"""
from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app import db  # noqa: E402
from app.contracts import Addon, AgentResult, Alternative, Item, Match, Merchant, Signal  # noqa: E402

POSTS = [  # (caption, creator, merchant, dish, price)
    ("Smash burger heaven 🍔 📍 Salt", "dubaifoodie", "Salt", "Classic Smash Burger", 42),
    ("Best shawarma in the Marina 🌯 📍 Shawarma Station", "eatwithzee", "Shawarma Station", "Chicken Shawarma", 14),
    ("Paris' viral chocolate mousse 📍 XOCO BAR", "sweettooth.dxb", "XOCO BAR", "Chocolate Mousse", 35),
    ("Biryani that slaps 🍛 📍 Ravi Restaurant", "spicetrail", "Ravi Restaurant", "Chicken Biryani", 28),
    ("Matcha everything 🍵 📍 Kyo Matcha", "cafehopper", "Kyo Matcha", "Iced Matcha Latte", 26),
]
COMMENTS = ["price?", "where is this?", "yummy 😍", "بكم", "b kam", "need this", "delivery?", "nice pic", "how much"]
MISSED = ["Nonna's tiramisu", "Kunafa pancakes", "Ube cheesecake", "Lobster roll from a pop-up"]


def clear() -> None:
    with db.connect() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM signals WHERE demo=1")]
        tokens = [r["token"] for r in c.execute(
            f"SELECT token FROM matches WHERE signal_id IN ({','.join('?' * len(ids))})", ids)] if ids else []
        for t in tokens:
            c.execute("DELETE FROM orders WHERE token=?", (t,))
        for sid in ids:
            for table, col in (("events", "signal_id"), ("matches", "signal_id"), ("missed", "signal_id"), ("signals", "id")):
                c.execute(f"DELETE FROM {table} WHERE {col}=?", (sid,))
    print(f"removed {len(ids)} demo signals")


def add_event(c, signal_id: str, stage: str, detail: dict, ts: datetime, level: str = "info") -> None:
    c.execute("INSERT INTO events (signal_id, stage, detail, level, ts) VALUES (?,?,?,?,?)",
              (signal_id, stage, json.dumps(detail, ensure_ascii=False), level, ts.isoformat(timespec="seconds")))


def seed(n: int) -> None:
    now = datetime.now(timezone.utc)
    rnd = random.Random(7)
    for i in range(n):
        caption, creator, merchant, dish, price = rnd.choice(POSTS)
        kind = "comment" if rnd.random() < 0.6 else "dm_share"
        text = rnd.choice(COMMENTS) if kind == "comment" else ""
        ts = now - timedelta(minutes=rnd.randint(5, 600))
        src = f"demo-{secrets.token_hex(5)}"
        sig = Signal(id=Signal.make_id("instagram", kind, src), platform="instagram", kind=kind, source_id=src,
                     user_ref=f"user{rnd.randint(100, 999)}", text=text, media_id=f"demo-media-{POSTS.index((caption, creator, merchant, dish, price))}",
                     caption=caption, creator=creator, restaurant=merchant, received_at=ts.isoformat(timespec="seconds"))
        db.insert_signal(sig, demo=True)
        buying = kind == "dm_share" or text != "nice pic"
        with db.connect() as c:
            add_event(c, sig.id, "received", {"kind": kind, "text": text, "user": sig.user_ref}, ts)
            add_event(c, sig.id, "intent", {"buying": buying, "how": "demo"}, ts)
            if not buying:
                add_event(c, sig.id, "result", {"status": "not_buying"}, ts)
                continue
        missed = rnd.random() < 0.1
        latency = rnd.randint(4200, 11800)
        mode = rnd.choices(["live", "cached", "rules"], [0.8, 0.12, 0.08])[0]
        if missed:
            wanted = rnd.choice(MISSED)
            result = AgentResult(status="none", mode=mode, wanted=wanted, missed_demand=wanted, latency_ms=latency)
            db.add_missed(sig.id, wanted)
        else:
            result = AgentResult(
                status=rnd.choice(["found", "found", "found", "closest"]), mode=mode, wanted=dish,
                merchant=Merchant(merchant), item=Item(dish, price_aed=price, eta_min=rnd.randint(18, 40)),
                alternatives=[Alternative(Merchant(f"{merchant} rival {k}"), Item(dish), price + rnd.randint(-6, 8), rnd.randint(20, 45)) for k in range(2)],
                addon=Addon("Fries", 9), confidence=round(rnd.uniform(0.7, 0.98), 2), latency_ms=latency)
        token = secrets.token_urlsafe(8)
        db.save_match(Match(token=token, signal_id=sig.id, result=result, created_at=ts.isoformat(timespec="seconds")))
        with db.connect() as c:
            add_event(c, sig.id, "result", {"status": result.status, "mode": mode, "latency_ms": latency}, ts)
            add_event(c, sig.id, "dm_sent", {"text": "(demo DM)", "to": sig.user_ref, "total_ms": latency + rnd.randint(300, 1500)}, ts)
            if not missed and rnd.random() < 0.45:
                add_event(c, sig.id, "confirm_open", {"token": token}, ts + timedelta(minutes=1))
                if rnd.random() < 0.6:
                    c.execute("INSERT INTO orders (token, status, aed, service, checkout_attempted, created_at) VALUES (?,?,?,?,1,?)",
                              (token, "basket", price + (9 if rnd.random() < 0.4 else 0), "food", ts.isoformat(timespec="seconds")))
                    add_event(c, sig.id, "order", {"status": "basket", "aed": price}, ts + timedelta(minutes=2))
    print(f"seeded {n} demo signals (flagged demo=1)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("-n", type=int, default=40)
    a = ap.parse_args()
    db.init_db()
    clear() if a.clear else seed(a.n)
