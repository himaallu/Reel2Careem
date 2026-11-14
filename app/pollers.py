"""[P2] Comment poller (+ tunnel health check) -> pipeline.submit().

Watches IG_WATCH_MEDIA_IDS; default 'auto' = every recent post Genie owns, refreshed each minute,
so a new demo post is picked up without editing .env or restarting.

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


AUTO_REFRESH_S = 60
_auto: dict[str, Any] = {"ids": [], "at": 0.0}


def _auto_ids() -> list[str]:
    """Genie's own latest posts (refreshed every AUTO_REFRESH_S). New posts get watched without a restart."""
    if time.time() - _auto["at"] >= AUTO_REFRESH_S:
        try:
            limit = int(os.getenv("AUTO_WATCH_LIMIT", "12"))
            _auto["ids"] = [m["id"] for m in instagram.list_media(limit=limit)]
        except Exception as e:  # noqa: BLE001  keep the previous list on errors
            log.warning("auto-watch refresh failed: %s", instagram.redact(str(e))[:200])
        _auto["at"] = time.time()
    return list(_auto["ids"])


def watch_ids() -> list[str]:
    """IG_WATCH_MEDIA_IDS: comma-separated post ids, plus 'auto' (= all of Genie's own recent posts).
    Empty or unset means 'auto'. Example: 'auto,1789...' watches Genie's posts and one extra post."""
    raw = [m.strip() for m in os.getenv("IG_WATCH_MEDIA_IDS", "auto").split(",") if m.strip()]
    if not raw or "auto" in (r.lower() for r in raw):
        explicit = [r for r in raw if r.lower() != "auto"]
        return list(dict.fromkeys(_auto_ids() + explicit))
    return raw


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
    if os.getenv("IG_TOKEN"):
        threading.Thread(target=_comment_loop, name="comment-poller", daemon=True).start()
        log.info("comment poller watching %s (IG_WATCH_MEDIA_IDS=%s)", watch_ids(),
                 os.getenv("IG_WATCH_MEDIA_IDS", "auto") or "auto")
    else:
        log.warning("comment poller off (IG_TOKEN empty)")
    threading.Thread(target=_tunnel_loop, name="tunnel-health", daemon=True).start()


def stop() -> None:
    _stop.set()
