"""[P2] Read-only aggregates for the dashboard (KPIs, funnel, leaderboards, latency, feed)."""
from __future__ import annotations

import json
import os
import time
from typing import Any

from app import db

_MERCHANT = "json_extract(m.result_json,'$.merchant.name')"


def _q(sql: str, *args: Any) -> list[dict[str, Any]]:
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _one(sql: str, *args: Any) -> Any:
    rows = _q(sql, *args)
    return next(iter(rows[0].values())) if rows else None


def kpis() -> dict[str, Any]:
    return {
        "signals": _one("SELECT COUNT(*) FROM signals") or 0,
        "dms_sent": _one("SELECT COUNT(DISTINCT signal_id) FROM events WHERE stage='dm_sent'") or 0,
        "confirm_opens": _one("SELECT COUNT(DISTINCT signal_id) FROM events WHERE stage='confirm_open'") or 0,
        "orders": _one("SELECT COUNT(*) FROM orders WHERE status IN ('basket','checked_out','handed_to_app')") or 0,
        "aed": round(_one("SELECT COALESCE(SUM(aed),0) FROM orders WHERE status IN ('basket','checked_out')") or 0, 2),
        "avg_latency_ms": int(_one("SELECT AVG(json_extract(result_json,'$.latency_ms')) FROM matches") or 0),
        "demo_rows": _one("SELECT COUNT(*) FROM signals WHERE demo=1") or 0,
    }


def funnel() -> list[dict[str, Any]]:
    buying = _one("""SELECT COUNT(DISTINCT signal_id) FROM events
                     WHERE stage='intent' AND json_extract(detail,'$.buying')=1""") or 0
    matched = _one("SELECT COUNT(*) FROM matches WHERE json_extract(result_json,'$.status') IN ('found','closest')") or 0
    k = kpis()
    return [
        {"stage": "Signals", "n": k["signals"]},
        {"stage": "Buying intent", "n": buying},
        {"stage": "Matched on Careem", "n": matched},
        {"stage": "DM sent", "n": k["dms_sent"]},
        {"stage": "Confirm opened", "n": k["confirm_opens"]},
        {"stage": "Ordered", "n": k["orders"]},
    ]


def leaderboards(limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    base = f"""FROM signals s LEFT JOIN matches m ON m.signal_id = s.id
               LEFT JOIN orders o ON o.token = m.token"""
    agg = """COUNT(DISTINCT s.id) AS signals,
             COUNT(DISTINCT CASE WHEN json_extract(m.result_json,'$.status') IN ('found','closest') THEN s.id END) AS matched,
             COUNT(DISTINCT o.token) AS orders, COALESCE(SUM(o.aed),0) AS aed"""
    return {
        "reel": _q(f"""SELECT COALESCE(substr(MAX(s.caption),1,48), s.media_id, 'DM text') AS label, {agg} {base}
                       GROUP BY COALESCE(s.media_id, 'none') ORDER BY signals DESC LIMIT ?""", limit),
        "creator": _q(f"""SELECT COALESCE(s.creator,'—') AS label, {agg} {base}
                          GROUP BY s.creator ORDER BY signals DESC LIMIT ?""", limit),
        "restaurant": _q(f"""SELECT COALESCE({_MERCHANT},'— not found') AS label, {agg} {base}
                             GROUP BY {_MERCHANT} ORDER BY signals DESC LIMIT ?""", limit),
        "platform": _q(f"""SELECT s.platform || ' · ' || s.kind AS label, {agg} {base}
                           GROUP BY s.platform, s.kind ORDER BY signals DESC LIMIT ?""", limit),
    }


def latency() -> list[dict[str, Any]]:
    agent_ms = _one("SELECT AVG(json_extract(result_json,'$.latency_ms')) FROM matches") or 0
    total_ms = _one("SELECT AVG(json_extract(detail,'$.total_ms')) FROM events WHERE stage='dm_sent'") or 0
    return [
        {"stage": "Agent (find dish)", "ms": int(agent_ms)},
        {"stage": "Signal → DM sent", "ms": int(total_ms)},
        {"stage": "Delivery overhead", "ms": max(0, int(total_ms - agent_ms))},
    ]


def feed(limit: int = 30) -> list[dict[str, Any]]:
    rows = _q("""SELECT s.*, m.token, m.result_json, m.mode AS result_mode,
                        (SELECT detail FROM events e WHERE e.signal_id=s.id AND e.stage='dm_sent' ORDER BY id DESC LIMIT 1) AS dm,
                        (SELECT stage FROM events e WHERE e.signal_id=s.id ORDER BY id DESC LIMIT 1) AS last_stage
                 FROM signals s LEFT JOIN matches m ON m.signal_id=s.id
                 ORDER BY s.received_at DESC LIMIT ?""", limit)
    for r in rows:
        r["result"] = json.loads(r.pop("result_json")) if r.get("result_json") else None
        r["dm"] = json.loads(r["dm"]) if r.get("dm") else None
    return rows


def health() -> dict[str, Any]:
    from app import pollers
    h = db.get_health()
    last = pollers.state.get("last_poll")
    return {
        "components": h,
        "last_poll_s_ago": round(time.time() - last, 1) if last else None,
        "poll_error": pollers.state.get("last_error"),
        "watching": pollers.watch_ids(),
    }


def mode() -> dict[str, Any]:
    try:
        from app import agent
        agent_mode = agent.current_mode()
    except Exception:  # noqa: BLE001
        agent_mode = os.getenv("AGENT_MODE", "live")
    return {"agent_mode": agent_mode, "order_mode": os.getenv("ORDER_MODE", "basket")}


def snapshot() -> dict[str, Any]:
    from app import events
    return {
        "kpis": kpis(), "funnel": funnel(), "leaderboards": leaderboards(), "latency": latency(),
        "feed": feed(), "missed": db.recent_missed(15), "health": health(), "mode": mode(),
        "last_event_id": events.last_event_id(),
    }
