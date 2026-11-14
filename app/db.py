"""SQLite schema + helpers. FROZEN after 12:00.

One short-lived connection per call (safe across pollers / web threads), WAL mode.
All timestamps are UTC ISO-8601.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.contracts import AgentResult, Match, Signal, utc_now

DB_PATH = os.getenv("DB_PATH", "genie.db")

EVENT_STAGES = (
    "received", "intent", "agent_step", "tool_call", "result", "reply_public",
    "dm_sent", "confirm_open", "order", "fallback", "error",
)
HEALTH_COMPONENTS = ("instagram", "careem_mcp", "bedrock", "tunnel")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    source_id   TEXT NOT NULL UNIQUE,
    user_ref    TEXT,
    text        TEXT,
    media_id    TEXT,
    media_url   TEXT,
    caption     TEXT,
    creator     TEXT,
    restaurant  TEXT,
    received_at TEXT NOT NULL,
    demo        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT,
    stage     TEXT NOT NULL,
    detail    TEXT,
    level     TEXT NOT NULL DEFAULT 'info',
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_signal ON events(signal_id);
CREATE TABLE IF NOT EXISTS matches (
    token       TEXT PRIMARY KEY,
    signal_id   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    confidence  REAL,
    mode        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    token              TEXT PRIMARY KEY,
    status             TEXT NOT NULL,
    aed                REAL,
    service            TEXT,
    checkout_attempted INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS missed (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT,
    wanted    TEXT,
    ts        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS health (
    component TEXT PRIMARY KEY,
    ok        INTEGER NOT NULL,
    detail    TEXT,
    ts        TEXT NOT NULL
);
"""


@contextmanager
def connect(path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- signals (dedupe)
def insert_signal(sig: Signal, demo: bool = False) -> bool:
    """Insert a signal. Returns False if source_id was already seen (dedupe)."""
    d = sig.to_dict()
    with connect() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO signals
               (id, platform, kind, source_id, user_ref, text, media_id, media_url,
                caption, creator, restaurant, received_at, demo)
               VALUES (:id,:platform,:kind,:source_id,:user_ref,:text,:media_id,:media_url,
                       :caption,:creator,:restaurant,:received_at,:demo)""",
            {**d, "demo": int(demo)},
        )
        return cur.rowcount == 1


def seen_source(source_id: str) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM signals WHERE source_id=?", (source_id,)).fetchone() is not None


def get_signal(signal_id: str) -> Optional[Signal]:
    with connect() as c:
        r = c.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    return Signal.from_dict(dict(r)) if r else None


def recent_signals(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM signals ORDER BY received_at DESC LIMIT ?", (limit,)))


# ---------------------------------------------------------------- events
def add_event(signal_id: Optional[str], stage: str, detail: Any = "", level: str = "info") -> dict[str, Any]:
    """Low-level insert. P2's events.emit() wraps this and broadcasts over SSE."""
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    ts = utc_now()
    with connect() as c:
        cur = c.execute(
            "INSERT INTO events (signal_id, stage, detail, level, ts) VALUES (?,?,?,?,?)",
            (signal_id, stage, detail, level, ts),
        )
        eid = cur.lastrowid
    return {"id": eid, "signal_id": signal_id, "stage": stage, "detail": detail, "level": level, "ts": ts}


def events_since(after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)))


def events_for(signal_id: str) -> list[dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM events WHERE signal_id=? ORDER BY id", (signal_id,)))


# ---------------------------------------------------------------- matches
def save_match(m: Match) -> None:
    with connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO matches (token, signal_id, result_json, confidence, mode, created_at)
               VALUES (?,?,?,?,?,?)""",
            (m.token, m.signal_id, json.dumps(m.result.to_dict(), ensure_ascii=False),
             m.result.confidence, m.result.mode, m.created_at),
        )


def get_match(token: str) -> Optional[Match]:
    with connect() as c:
        r = c.execute("SELECT * FROM matches WHERE token=?", (token,)).fetchone()
    if not r:
        return None
    return Match(token=r["token"], signal_id=r["signal_id"],
                 result=AgentResult.from_dict(json.loads(r["result_json"])), created_at=r["created_at"])


# ---------------------------------------------------------------- orders (idempotent, never retry)
def get_order(token: str) -> Optional[dict[str, Any]]:
    with connect() as c:
        r = c.execute("SELECT * FROM orders WHERE token=?", (token,)).fetchone()
    return dict(r) if r else None


def claim_checkout(token: str, aed: Optional[float], service: str) -> bool:
    """Atomically mark checkout_attempted=1 BEFORE any mutating call.

    Returns True only for the first caller; every later tap gets False and must not call Careem.
    """
    with connect() as c:
        c.execute(
            """INSERT OR IGNORE INTO orders (token, status, aed, service, checkout_attempted, created_at)
               VALUES (?, 'pending', ?, ?, 0, ?)""",
            (token, aed, service, utc_now()),
        )
        cur = c.execute(
            "UPDATE orders SET checkout_attempted=1 WHERE token=? AND checkout_attempted=0", (token,)
        )
        return cur.rowcount == 1


def set_order_status(token: str, status: str, aed: Optional[float] = None) -> None:
    with connect() as c:
        c.execute("UPDATE orders SET status=?, aed=COALESCE(?, aed) WHERE token=?", (status, aed, token))


# ---------------------------------------------------------------- missed demand
def add_missed(signal_id: Optional[str], wanted: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO missed (signal_id, wanted, ts) VALUES (?,?,?)", (signal_id, wanted, utc_now()))


def recent_missed(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM missed ORDER BY id DESC LIMIT ?", (limit,)))


# ---------------------------------------------------------------- health
def set_health(component: str, ok: bool, detail: str = "") -> None:
    with connect() as c:
        c.execute(
            """INSERT INTO health (component, ok, detail, ts) VALUES (?,?,?,?)
               ON CONFLICT(component) DO UPDATE SET ok=excluded.ok, detail=excluded.detail, ts=excluded.ts""",
            (component, int(ok), detail[:500], utc_now()),
        )


def get_health() -> dict[str, dict[str, Any]]:
    with connect() as c:
        return {r["component"]: dict(r) for r in c.execute("SELECT * FROM health")}
