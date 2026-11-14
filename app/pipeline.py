"""[P2] handle_signal(): dedupe -> intent -> agent.resolve -> public reply -> DM -> events.

Safety: every comment / message id is processed once (db.insert_signal is the dedupe gate), and each
reply call is made at most once (writes are never retried; Meta allows ONE private reply per comment).
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app import db, instagram, messages
from app.contracts import AgentResult, Match, Signal
from app.events import emit

log = logging.getLogger("genie.pipeline")

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pipeline")

# Only used if app/intent.py (P1-8) can't be imported.
BUYING_KEYWORDS = (
    "price", "how much", "where", "how", "yum", "want", "order", "delivery", "deliver", "need",
    "menu", "cost", "location", "address", "link", "buy", "hungry", "craving", "😍", "🤤", "🔥",
    "بكم", "كم", "وين", "فين", "سعر", "ابي", "أبي", "ابغى", "b kam", "bkam", "wen", "wain", "fein",
)
GREETINGS = {"hi", "hey", "hello", "hii", "yo", "salam", "salaam", "مرحبا", "السلام عليكم"}


def submit(signal: Signal, *, demo: bool = False, simulated: bool = False) -> None:
    """Fire-and-forget from pollers / webhook so they never block on the agent (up to ~20 s)."""
    _pool.submit(_safe_handle, signal, demo, simulated)


def _safe_handle(signal: Signal, demo: bool, simulated: bool) -> None:
    try:
        handle_signal(signal, demo=demo, simulated=simulated)
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline crashed for %s", signal.id)
        emit(signal.id, "error", {"where": "pipeline", "error": f"{type(e).__name__}: {e}"[:300]}, "error")


# ---------------------------------------------------------------- intent
def keyword_intent(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in BUYING_KEYWORDS)


def check_intent(signal: Signal) -> bool:
    """P1's classifier (app/intent.py, P1-8) when present: it emits its own `intent` event and never raises.
    Falls back to the keyword list here (and emits the event itself) if that module is missing or broken."""
    try:
        from app import intent
        return bool(intent.classify(signal).buying)
    except Exception as e:  # noqa: BLE001
        log.warning("intent classifier unavailable, using keywords: %s", e)
    buying = signal.kind == "dm_share" or keyword_intent(signal.text)
    emit(signal.id, "intent", {"buying": buying, "how": "dm" if signal.kind == "dm_share" else "keywords"})
    return buying


# ---------------------------------------------------------------- main flow
def public_link(token: str) -> Optional[str]:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/c/{token}" if base else f"http://localhost:{os.getenv('WEB_PORT', '8000')}/c/{token}"


def handle_signal(signal: Signal, *, demo: bool = False, simulated: bool = False) -> Optional[AgentResult]:
    t0 = time.monotonic()
    if not db.insert_signal(signal, demo=demo):
        log.info("dedupe: %s already processed", signal.source_id)
        return None
    emit(signal.id, "received", {
        "kind": signal.kind, "text": signal.text, "caption": (signal.caption or "")[:300],
        "media_url": signal.media_url, "user": signal.user_ref, "simulated": simulated,
    })

    # text-only greeting in DMs -> help message, no agent
    if signal.kind == "dm_share" and not (signal.caption or signal.media_url) \
            and (signal.text or "").strip().lower() in GREETINGS:
        _send(signal, "dm", messages.no_caption_dm().replace("Got your reel! I couldn't tell which dish it is — ",
                                                               "Hi! Send me any food reel, or "), simulated)
        return None

    buying = check_intent(signal)
    if not buying:
        emit(signal.id, "result", {"status": "not_buying"})
        return None

    from app import agent  # P1; resolve() never raises and runs the fallback ladder itself
    result = agent.resolve(signal)
    token = secrets.token_urlsafe(8)
    db.save_match(Match(token=token, signal_id=signal.id, result=result))
    link = public_link(token) if result.item else None
    emit(signal.id, "result", {
        "status": result.status, "mode": result.mode, "token": token, "confidence": result.confidence,
        "merchant": result.merchant.name if result.merchant else None,
        "item": result.item.name if result.item else None,
        "price_aed": result.item.price_aed if result.item else None,
        "latency_ms": result.latency_ms,
    })

    dm = messages.dm_text(result, link)
    if signal.kind == "comment":
        _send(signal, "public", messages.comment_reply(result), simulated)
    _send(signal, "dm", dm, simulated, token=token, total_ms=int((time.monotonic() - t0) * 1000))
    return result


def _send(signal: Signal, channel: str, text: str, simulated: bool, **extra: Any) -> bool:
    """One attempt, never retried. Emits reply_public / dm_sent, or error."""
    stage = "reply_public" if channel == "public" else "dm_sent"
    detail: dict[str, Any] = {"text": text, "to": signal.user_ref, "simulated": simulated, **extra}
    if simulated:
        emit(signal.id, stage, detail)
        return True
    try:
        if channel == "public":
            instagram.reply_comment(signal.source_id, text)
        elif signal.kind == "comment":
            instagram.private_reply(signal.source_id, text)
        else:
            instagram.send_dm(signal.user_ref, text)
    except instagram.IGError as e:
        emit(signal.id, "error", {"where": stage, "error": str(e)[:300], "text": text}, "error")
        return False
    emit(signal.id, stage, detail)
    return True


def new_simulated_signal(kind: str, text: str, caption: Optional[str] = None,
                         media_id: Optional[str] = None, user: str = "simulator") -> Signal:
    sid = f"sim-{secrets.token_hex(6)}"
    return Signal(
        id=Signal.make_id("instagram", kind, sid), platform="instagram", kind=kind,  # type: ignore[arg-type]
        source_id=sid, user_ref=user, text=text, media_id=media_id, caption=caption,
        creator=instagram.guess_creator(caption), restaurant=instagram.guess_restaurant(caption),
    )
