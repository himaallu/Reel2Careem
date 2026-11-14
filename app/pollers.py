"""[P2] Comment poller (+ tunnel health check) -> pipeline.submit().

DMs do NOT need a poller: /conversations shows shared reels as is_unsupported, so DMs arrive via the
`messages` webhook (web.py POST /webhook). Comments need Advanced Access for webhooks, so we poll.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app import db, instagram, pipeline
from app.contracts import Signal

log = logging.getLogger("genie.pollers")

state: dict[str, Any] = {"last_poll": None, "last_error": None, "started_at": None, "polls": 0}
_stop = threading.Event()
_started = False


def watch_ids() -> list[str]:
    return [m.strip() for m in os.getenv("IG_WATCH_MEDIA_IDS", "").split(",") if m.strip()]


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def comment_signal(c: dict[str, Any], media_id: str, meta: dict[str, Any]) -> Signal:
    caption = meta.get("caption")
    owner = meta.get("username")
    genie = instagram.genie_username()
    return Signal(
        id=Signal.make_id("instagram", "comment", c["id"]), platform="instagram", kind="comment",
        source_id=c["id"], user_ref=c.get("username") or (c.get("from") or {}).get("username") or "unknown",
        text=c.get("text") or "", media_id=media_id,
        media_url=meta.get("thumbnail_url") or meta.get("media_url") or meta.get("permalink"),
        caption=caption,
        creator=owner if owner and owner != genie else instagram.guess_creator(caption),
        restaurant=instagram.guess_restaurant(caption),
    )


def poll_comments_once(cutoff: datetime) -> int:
    """One pass over watched posts. Returns the number of new signals submitted."""
    genie = instagram.genie_username()
    new = 0
    for media_id in watch_ids():
        meta = instagram.media_meta(media_id)
        for c in instagram.comments(media_id):
            if c.get("username") == genie or db.seen_source(c["id"]):
                continue  # our own replies / already handled
            ts = _parse_ts(c.get("timestamp"))
            if ts and ts < cutoff:
                continue  # don't answer history from before the poller started
            pipeline.submit(comment_signal(c, media_id, meta))
            new += 1
    return new


def _comment_loop() -> None:
    lookback = int(os.getenv("COMMENT_LOOKBACK_MIN", "10"))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback)
    period = float(os.getenv("POLL_SECONDS", "4"))
    while not _stop.is_set():
        try:
            n = poll_comments_once(cutoff)
            state["last_error"] = None
            if n:
                log.info("comment poller: %d new", n)
        except Exception as e:  # noqa: BLE001
            state["last_error"] = instagram.redact(f"{type(e).__name__}: {e}")[:300]
            log.warning("comment poll failed: %s", state["last_error"])
        state["last_poll"] = time.time()
        state["polls"] += 1
        _stop.wait(period)


def _tunnel_loop() -> None:
    while not _stop.is_set():
        base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if not base:
            db.set_health("tunnel", False, "PUBLIC_BASE_URL not set")
        else:
            try:
                r = httpx.get(f"{base}/api/health", timeout=8)
                db.set_health("tunnel", r.status_code == 200, f"HTTP {r.status_code}")
            except httpx.HTTPError as e:
                db.set_health("tunnel", False, f"{type(e).__name__}")
        _stop.wait(30)


def start() -> None:
    """Start background threads once (idempotent)."""
    global _started
    if _started:
        return
    _started = True
    state["started_at"] = time.time()
    if watch_ids() and os.getenv("IG_TOKEN"):
        threading.Thread(target=_comment_loop, name="comment-poller", daemon=True).start()
        log.info("comment poller watching %s", watch_ids())
    else:
        log.warning("comment poller off (IG_WATCH_MEDIA_IDS or IG_TOKEN empty)")
    threading.Thread(target=_tunnel_loop, name="tunnel-health", daemon=True).start()


def stop() -> None:
    _stop.set()
