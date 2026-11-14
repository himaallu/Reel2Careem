"""[P2, shared] emit(signal_id, stage, detail, level) -> DB + SSE broadcast.

The DB is the bus: emit() writes an events row (from any thread: pollers, agent, pipeline), and every
SSE client tails events by id. No cross-thread queues, survives restarts, replays on reconnect.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional

from app import db

log = logging.getLogger("genie.events")


def emit(signal_id: Optional[str], stage: str, detail: Any = "", level: str = "info") -> dict[str, Any]:
    ev = db.add_event(signal_id, stage, detail, level)
    log.info("[%s] %s %s", stage, signal_id or "-", str(ev["detail"])[:200])
    return ev


async def stream(after_id: int = 0, interval: float = 0.4) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE dicts for every new event after `after_id`, forever."""
    last = after_id
    idle = 0.0
    while True:
        rows = await asyncio.to_thread(db.events_since, last, 200)
        for r in rows:
            last = r["id"]
            yield {"event": "ev", "id": str(r["id"]), "data": json.dumps(r, ensure_ascii=False)}
        if rows:
            idle = 0.0
        else:
            idle += interval
            if idle >= 15:  # keep proxies/tunnels from closing an idle stream
                idle = 0.0
                yield {"event": "ping", "data": "{}"}
        await asyncio.sleep(interval)


def last_event_id() -> int:
    with db.connect() as c:
        r = c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(r["m"])
