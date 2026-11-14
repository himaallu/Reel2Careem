"""[P2] FastAPI routes: /webhook, /c/{token}, /api/*, /dashboard, /api/stream (SSE), /simulate."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app import db, events, instagram, pipeline, pollers, stats
from app.events import emit

log = logging.getLogger("genie.web")

app = FastAPI(title="Careem Genie")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    if os.getenv("GENIE_NO_POLLERS") != "1":
        pollers.start()


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "components": db.get_health()}


# ---------------------------------------------------------------- Instagram webhook (DMs)
@app.get("/webhook")
def webhook_verify(request: Request) -> PlainTextResponse:
    q = request.query_params
    token = os.getenv("IG_WEBHOOK_VERIFY_TOKEN", "")
    if q.get("hub.mode") == "subscribe" and token and q.get("hub.verify_token") == token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


def _signature_ok(body: bytes, header: Optional[str]) -> bool:
    secret = os.getenv("IG_APP_SECRET", "")
    if not secret:
        return True  # not configured -> accept (testers-only app); set IG_APP_SECRET to enforce
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


@app.post("/webhook")
async def webhook_receive(request: Request) -> PlainTextResponse:
    body = await request.body()
    if not _signature_ok(body, request.headers.get("X-Hub-Signature-256")):
        log.warning("webhook signature mismatch")
        raise HTTPException(403, "bad signature")
    try:
        payload = await request.json()
    except ValueError:
        return PlainTextResponse("ignored")
    for sig in instagram.parse_webhook(payload):
        pipeline.submit(sig)
    db.set_health("instagram", True, "webhook received")
    return PlainTextResponse("EVENT_RECEIVED")


# ---------------------------------------------------------------- confirm page
_BOT_MARKERS = ("facebookexternalhit", "facebookcatalog", "meta-externalagent", "meta-externalfetcher",
                "whatsapp", "telegrambot", "slackbot", "twitterbot", "discordbot", "bot", "crawler", "spider")


def _is_preview_bot(user_agent: str) -> bool:
    ua = user_agent.lower()
    return not ua or any(m in ua for m in _BOT_MARKERS)


def _match_or_404(token: str):
    m = db.get_match(token)
    if not m:
        raise HTTPException(404, "This link has expired or doesn't exist.")
    return m


@app.get("/c/{token}", response_class=HTMLResponse)
def confirm_page(token: str, request: Request) -> HTMLResponse:
    m = _match_or_404(token)
    order = db.get_order(token)
    # count real opens only: not link-preview crawlers (Instagram fetches the URL when the DM is sent),
    # and not refreshes after ordering
    if not order and not _is_preview_bot(request.headers.get("user-agent", "")):
        emit(m.signal_id, "confirm_open", {"token": token})
    return templates.TemplateResponse(request, "confirm.html", {
        "token": token, "r": m.result, "order": order,
        "location_label": _delivery_label(),
        "order_mode": os.getenv("ORDER_MODE", "basket"),
    })


@app.post("/c/{token}/confirm")
def confirm_order(token: str, addon: bool = Form(False)) -> JSONResponse:
    m = _match_or_404(token)
    existing = db.get_order(token)
    if existing and existing.get("checkout_attempted"):
        return JSONResponse({"status": existing["status"], "message": _order_message(existing["status"]), "repeat": True})
    try:
        from app import orders
        place = getattr(orders, "place")
    except (ImportError, AttributeError):
        emit(m.signal_id, "order", {"token": token, "status": "unavailable"}, "warn")
        return JSONResponse({"status": "unavailable",
                             "message": "Ordering isn't switched on yet — open the Careem app to order."}, 503)
    try:
        try:
            res = place(token, addon=addon)
        except TypeError:
            res = place(token)
    except Exception as e:  # noqa: BLE001  orders.place should never raise, but never retry if it does
        log.exception("orders.place failed")
        emit(m.signal_id, "error", {"where": "order", "error": f"{type(e).__name__}: {e}"[:300]}, "error")
        return JSONResponse({"status": "handed_to_app", "message": _order_message("handed_to_app")})
    if hasattr(res, "to_dict"):
        res = res.to_dict()
    res = res if isinstance(res, dict) else {}
    status = res.get("status") or (db.get_order(token) or {}).get("status", "handed_to_app")
    return JSONResponse({"status": status, "message": res.get("message") or _order_message(status), "aed": res.get("aed")})


def _delivery_label() -> str:
    try:
        from app import orders
        return orders.delivery_label() or os.getenv("DEMO_LOCATION_LABEL", "Demo venue")
    except Exception:  # noqa: BLE001
        return os.getenv("DEMO_LOCATION_LABEL", "Demo venue")


def _order_message(status: str) -> str:
    return {
        "basket": "Your basket is ready — complete payment in the Careem app ✅",
        "checked_out": "Order placed! Track it in the Careem app 🛵",
        "handed_to_app": "Finish in your Careem app — check your basket there.",
        "failed": "Something went wrong — please order in the Careem app.",
        "pending": "Working on it… check your Careem app.",
    }.get(status, "Check your Careem app.")


# ---------------------------------------------------------------- dashboard + API
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", {})


@app.get("/dashboard/classic", response_class=HTMLResponse)
def dashboard_classic(request: Request) -> HTMLResponse:
    """The previous dashboard (with the phone mock), kept as a fallback."""
    return templates.TemplateResponse(request, "dashboard_classic.html", {})


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    return stats.snapshot()


@app.get("/api/metrics")
def api_metrics() -> dict[str, Any]:
    from app import metrics
    return {**metrics.compute(), "health": stats.health(), "mode": stats.mode()}


@app.get("/api/events/{signal_id}")
def api_signal_events(signal_id: str) -> list[dict[str, Any]]:
    return db.events_for(signal_id)


@app.get("/api/stream")
async def api_stream(request: Request, after: Optional[int] = None) -> EventSourceResponse:
    last = request.headers.get("last-event-id")
    start = int(last) if last and last.isdigit() else (after if after is not None else events.last_event_id())
    return EventSourceResponse(events.stream(start))


@app.post("/api/mode")
async def api_mode(request: Request) -> dict[str, Any]:
    body = await request.json()
    mode = body.get("mode")
    from app import agent
    agent.set_mode(None if mode in (None, "", "default") else mode)
    emit(None, "fallback", {"reason": f"mode switched to {agent.current_mode()} from dashboard"}, "warn")
    return stats.mode()


@app.post("/simulate")
async def simulate(request: Request) -> dict[str, Any]:
    """Inject a Signal when Instagram is down. Replies are rendered in the dashboard phone mock only."""
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
    else:
        body = dict(await request.form())
    kind = body.get("kind") if body.get("kind") in ("comment", "dm_share") else "comment"
    text = (body.get("text") or "").strip()
    caption = (body.get("caption") or "").strip() or None
    if not text and not caption:
        raise HTTPException(400, "text or caption required")
    sig = pipeline.new_simulated_signal(kind, text, caption, body.get("media_id") or None,
                                        body.get("user") or "simulator")
    pipeline.submit(sig, simulated=True)
    return {"ok": True, "signal_id": sig.id}
