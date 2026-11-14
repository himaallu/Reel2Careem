"""[P2] Instagram Graph API calls: media, comments, public replies, private replies, DMs, webhook parsing.

Works against graph.facebook.com (unified login, EAA token, calls go to IG_USER_ID) and
graph.instagram.com (Instagram Login, IGAA token). Never logs the token.

Facts we verified (see fixtures/):
- Polling /conversations shows shared reels as is_unsupported -> DMs come in via the `messages` webhook.
- Webhook shared reel: message.attachments[].type == "ig_reel", payload {url, title (= caption), reel_video_id}.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

import httpx

from app import db
from app.contracts import Signal

log = logging.getLogger("genie.instagram")

TIMEOUT_S = 10.0
_TOKEN_RE = re.compile(r"(access_token=)[^&\s\"']+")


class IGError(RuntimeError):
    pass


def _cfg() -> tuple[str, str, str]:
    api = os.getenv("IG_API", "https://graph.facebook.com/v23.0").rstrip("/")
    return api, os.getenv("IG_TOKEN", ""), os.getenv("IG_USER_ID", "")


def redact(text: str) -> str:
    token = os.getenv("IG_TOKEN", "")
    if token:
        text = text.replace(token, "***")
    return _TOKEN_RE.sub(r"\1***", text)


def _health(ok: bool, detail: str) -> None:
    try:
        db.set_health("instagram", ok, detail)
    except Exception:  # noqa: BLE001
        log.exception("health update failed")


def _request(method: str, path: str, *, params: Optional[dict] = None, json: Optional[dict] = None,
             retries: int = 0) -> dict[str, Any]:
    """One Graph API call. Reads may retry; writes (retries=0) never do."""
    api, token, _ = _cfg()
    if not token:
        raise IGError("IG_TOKEN not set")
    params = {**(params or {}), "access_token": token}
    last: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            r = httpx.request(method, f"{api}/{path.lstrip('/')}", params=params, json=json, timeout=TIMEOUT_S)
        except httpx.HTTPError as e:
            last = redact(f"{type(e).__name__}: {e}")
        else:
            if r.status_code < 400:
                _health(True, f"{method} {path.split('?')[0]} ok")
                return r.json()
            last = redact(f"HTTP {r.status_code}: {r.text[:400]}")
            if r.status_code < 500:  # client errors won't fix themselves
                break
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    _health(False, last or "unknown error")
    raise IGError(last or "unknown error")


def _get(path: str, **params: Any) -> dict[str, Any]:
    return _request("GET", path, params=params, retries=2)


def me_id() -> str:
    _, _, uid = _cfg()
    return uid or "me"


# ---------------------------------------------------------------- reads
_profile_cache: dict[str, Any] = {}


def profile() -> dict[str, Any]:
    if not _profile_cache:
        _profile_cache.update(_get(me_id(), fields="id,username,name"))
    return _profile_cache


def genie_username() -> str:
    try:
        return profile().get("username", "")
    except IGError:
        return ""


def list_media(limit: int = 25) -> list[dict[str, Any]]:
    fields = "id,caption,media_type,media_product_type,permalink,timestamp,username,comments_count,media_url,thumbnail_url"
    return _get(f"{me_id()}/media", fields=fields, limit=limit).get("data", [])


_media_cache: dict[str, dict[str, Any]] = {}


def media_meta(media_id: str) -> dict[str, Any]:
    """caption / owner username / permalink / image for a post (cached; captions don't change mid-demo)."""
    if media_id not in _media_cache:
        try:
            _media_cache[media_id] = _get(
                media_id, fields="id,caption,username,permalink,media_type,media_url,thumbnail_url")
        except IGError as e:
            log.warning("media_meta %s failed: %s", media_id, e)
            return {"id": media_id}
    return _media_cache[media_id]


def comments(media_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return _get(f"{media_id}/comments", fields="id,text,timestamp,username,from", limit=limit).get("data", [])


_username_cache: dict[str, Optional[str]] = {}


def username_for(user_ref: Optional[str]) -> Optional[str]:
    """Readable @username for a DM sender's numeric IGSID (cached; None if unknown). Comment authors
    already arrive as usernames, so non-numeric refs are returned unchanged."""
    if not user_ref:
        return None
    if not user_ref.isdigit():
        return user_ref
    if user_ref not in _username_cache:
        try:
            _username_cache[user_ref] = _request("GET", user_ref, params={"fields": "username"}).get("username")
        except IGError:
            _username_cache[user_ref] = None
    return _username_cache[user_ref]


# ---------------------------------------------------------------- writes (never retried)
def reply_comment(comment_id: str, text: str) -> dict[str, Any]:
    """Public reply under a comment."""
    return _request("POST", f"{comment_id}/replies", params={"message": text})


def private_reply(comment_id: str, text: str) -> dict[str, Any]:
    """One private DM per comment (Meta allows one, within 7 days)."""
    return _request("POST", f"{me_id()}/messages",
                    json={"recipient": {"comment_id": comment_id}, "message": {"text": text}})


def send_dm(igsid: str, text: str) -> dict[str, Any]:
    """DM a user who messaged us in the last 24 h."""
    return _request("POST", f"{me_id()}/messages",
                    json={"recipient": {"id": igsid}, "message": {"text": text}})


# ---------------------------------------------------------------- webhook -> Signals
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_.]{2,30})")
_PIN_RE = re.compile(r"📍\s*([^\n#]+)")


def guess_restaurant(caption: Optional[str]) -> Optional[str]:
    """Cheap hint for the agent: the '📍 Place' line, else None (the agent reads the caption anyway)."""
    if not caption:
        return None
    m = _PIN_RE.search(caption)
    return m.group(1).strip()[:120] if m else None


def guess_creator(caption: Optional[str]) -> Optional[str]:
    if not caption:
        return None
    m = _HANDLE_RE.search(caption)
    return m.group(1) if m else None


def parse_webhook(payload: dict[str, Any]) -> list[Signal]:
    """Turn a `messages` webhook POST into dm_share Signals. Ignores echoes, reads, reactions."""
    out: list[Signal] = []
    if payload.get("object") != "instagram":
        return out
    genie = me_id()
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []):
            msg = ev.get("message") or {}
            sender = (ev.get("sender") or {}).get("id")
            if not msg or not sender or msg.get("is_echo") or msg.get("is_deleted") or sender == genie:
                continue
            mid = msg.get("mid")
            if not mid:
                continue
            text = msg.get("text") or ""
            media_id = media_url = caption = None
            for a in msg.get("attachments") or []:
                p = a.get("payload") or {}
                if a.get("type") in ("ig_reel", "reel", "share", "ig_post"):
                    media_id = p.get("reel_video_id") or p.get("id") or media_id
                    media_url = p.get("url") or media_url
                    caption = p.get("title") or caption
                elif a.get("type") in ("image", "video") and not media_url:
                    media_url = p.get("url")
            if not (text or media_url or caption):
                continue
            out.append(Signal(
                id=Signal.make_id("instagram", "dm_share", mid), platform="instagram", kind="dm_share",
                source_id=mid, user_ref=sender, text=text, media_id=media_id, media_url=media_url,
                caption=caption, creator=guess_creator(caption), restaurant=guess_restaurant(caption),
            ))
    return out
