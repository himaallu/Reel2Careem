"""Business metrics for the dashboard: every number is derived from what the DB actually recorded.

One pass builds a per-signal journey (received -> buying intent -> matched -> DM -> confirm opened -> ordered),
then everything else is a group-by over those journeys. Read-only. GET /api/metrics returns compute().
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app import db

ORDERED = ("basket", "checked_out")


def _q(sql: str, *args: Any) -> list[dict[str, Any]]:
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _json(s: Any) -> dict[str, Any]:
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


_FRANCO = re.compile(r"\b(b ?kam|bkm|wen|wein|wain|abi|abgha|7elw|7lw|yalla|shu|kif|mat3am|ta3am)\b|[a-z][2357][a-z]", re.I)


def language(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None
    if re.search("[؀-ۿ]", t):
        return "Arabic"
    if _FRANCO.search(t):
        return "Franco-Arabic"
    return "English" if re.search("[A-Za-z]", t) else "Emoji only"


def _pct(a: float, b: float) -> Optional[float]:
    return round(100.0 * a / b, 1) if b else None


def _pq(values: list[float], q: float) -> Optional[int]:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return int(v[min(len(v) - 1, int(round(q * (len(v) - 1))))])


# ================================================================ journeys
def journeys() -> list[dict[str, Any]]:
    sigs = _q("SELECT * FROM signals ORDER BY received_at")
    ev: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in _q("SELECT signal_id, stage, detail, ts FROM events WHERE signal_id IS NOT NULL ORDER BY id"):
        ev[e["signal_id"]].append(e)
    matches = {m["signal_id"]: m for m in _q("SELECT * FROM matches")}
    orders = {o["token"]: o for o in _q("SELECT * FROM orders")}
    out = []
    for s in sigs:
        evs = ev.get(s["id"], [])
        stages = {e["stage"] for e in evs}
        intent = next((_json(e["detail"]) for e in reversed(evs) if e["stage"] == "intent"), None)
        dm = next((e for e in reversed(evs) if e["stage"] == "dm_sent"), None)
        m = matches.get(s["id"])
        res = _json(m["result_json"]) if m else {}
        o = orders.get(m["token"]) if m else None
        merchant = res.get("merchant") or {}
        item = res.get("item") or {}
        status = res.get("status")
        out.append({
            "id": s["id"], "kind": s["kind"], "user": s["user_ref"], "text": s["text"] or "",
            "caption": s["caption"] or "", "creator": s["creator"], "media_id": s["media_id"],
            "received_at": s["received_at"], "demo": bool(s.get("demo")),
            "lang": language(s["text"] or ""),
            "buying": (intent or {}).get("buying") if intent else (True if s["kind"] == "dm_share" else None),
            "intent_method": (intent or {}).get("method") or (intent or {}).get("how"),
            "status": status, "matched": status in ("found", "closest"),
            "mode": res.get("mode") or (m or {}).get("mode"),
            "merchant": merchant.get("name"), "area": merchant.get("area"),
            "item": item.get("name"), "price": item.get("price_aed"),
            "wanted": res.get("wanted"), "agent_ms": res.get("latency_ms"),
            "dm": dm is not None, "dm_text": _json(dm["detail"]).get("text") if dm else None,
            "dm_ms": _json(dm["detail"]).get("total_ms") if dm else None,
            "dm_ts": dm["ts"] if dm else None,
            "simulated": any(_json(e["detail"]).get("simulated") for e in evs if e["stage"] in ("dm_sent", "reply_public")),
            "opened": "confirm_open" in stages,
            "order_status": o["status"] if o else None,
            "ordered": bool(o and o["status"] in ORDERED),
            "aed": float(o["aed"]) if o and o["status"] in ORDERED and o["aed"] is not None else 0.0,
            "order_ts": o["created_at"] if o else None,
            "fallback": "fallback" in stages,
        })
    return out


# ================================================================ aggregates
def _group(js: list[dict[str, Any]], key: str, label_none: str, limit: int = 8) -> list[dict[str, Any]]:
    g: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for j in js:
        g[j.get(key) or label_none].append(j)
    rows = []
    for k, v in g.items():
        orders = sum(x["ordered"] for x in v)
        rows.append({"label": k, "signals": len(v), "buying": sum(bool(x["buying"]) for x in v),
                     "dms": sum(x["dm"] for x in v), "opens": sum(x["opened"] for x in v), "orders": orders,
                     "aed": round(sum(x["aed"] for x in v), 2), "conversion": _pct(orders, len(v))})
    rows.sort(key=lambda r: (r["orders"], r["aed"], r["signals"]), reverse=True)
    return rows[:limit]


def _timeline(js: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    times = [t for t in (_ts(j["received_at"]) for j in js) if t]
    start = max(min(times), now - timedelta(hours=6)) if times else now - timedelta(hours=1)
    span_min = max(10, (now - start).total_seconds() / 60)
    step = next(s for s in (2, 5, 10, 15, 30, 60) if span_min / s <= 24)
    n = int(span_min // step) + 1
    t0 = now - timedelta(minutes=step * (n - 1))
    t0 = t0.replace(second=0, microsecond=0, minute=t0.minute - t0.minute % step)
    buckets = {"signals": [0] * n, "dms": [0] * n, "orders": [0] * n}

    def put(series: str, ts: Optional[str]) -> None:
        t = _ts(ts)
        if t and t >= t0:
            i = int((t - t0).total_seconds() // (step * 60))
            if 0 <= i < n:
                buckets[series][i] += 1

    for j in js:
        put("signals", j["received_at"])
        if j["dm"]:
            put("dms", j["dm_ts"])
        if j["ordered"]:
            put("orders", j["order_ts"])
    return {"step_min": step, "start": t0.isoformat(),
            "labels": [(t0 + timedelta(minutes=step * i)).isoformat() for i in range(n)], **buckets}


def compute() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    js = journeys()
    n = len(js)
    buying = sum(bool(j["buying"]) for j in js)
    matched = sum(j["matched"] for j in js)
    dms = sum(j["dm"] for j in js)
    dm_dish = sum(j["dm"] and j["matched"] for j in js)
    opens = sum(j["opened"] for j in js)
    orders = sum(j["ordered"] for j in js)
    gmv = round(sum(j["aed"] for j in js), 2)
    handed = sum(j["order_status"] == "handed_to_app" for j in js)
    e2e = [j["dm_ms"] for j in js if j["dm_ms"]]
    agent = [j["agent_ms"] for j in js if j["agent_ms"]]

    funnel_steps = [("Signals", n), ("Buying intent", buying), ("Matched on Careem", matched),
                    ("DM with the dish", dm_dish), ("Confirm opened", opens), ("Ordered", orders)]
    funnel = [{"stage": s, "n": v, "of_prev": _pct(v, funnel_steps[i - 1][1]) if i else None,
               "of_first": _pct(v, n)} for i, (s, v) in enumerate(funnel_steps)]

    def split(key: str, order: list[str], pred=lambda j: True) -> list[dict[str, Any]]:
        c = Counter(j[key] for j in js if pred(j) and j[key])
        rows = [{"label": k, "n": c.get(k, 0)} for k in order if c.get(k, 0)]
        rows += [{"label": k, "n": v} for k, v in c.items() if k not in order]
        tot = sum(r["n"] for r in rows)
        for r in rows:
            r["pct"] = _pct(r["n"], tot)
        return rows

    src = []
    for kind, label in (("comment", "Comment on collab post"), ("dm_share", "Reel shared in DM")):
        sub = [j for j in js if j["kind"] == kind]
        if sub:
            o = sum(j["ordered"] for j in sub)
            src.append({"label": label, "signals": len(sub), "orders": o, "aed": round(sum(j["aed"] for j in sub), 2),
                        "conversion": _pct(o, len(sub))})

    dishes = Counter(j["item"] for j in js if j["matched"] and j["item"])
    missed = Counter((j["wanted"] or "").strip() for j in js if j["status"] == "none" and j["wanted"])
    for r in db.recent_missed(50):
        if not any(j["id"] == r["signal_id"] for j in js):
            missed[r["wanted"]] += 1

    feed = []
    for j in reversed(js[-14:]):
        stage = ("ordered" if j["ordered"] else "opened" if j["opened"] else "dm" if j["dm"] else
                 "matched" if j["matched"] else "no intent" if j["buying"] is False else
                 "not found" if j["status"] == "none" else "received")
        feed.append({k: j[k] for k in ("id", "kind", "user", "text", "caption", "creator", "item", "merchant", "area",
                                       "price", "mode", "status", "dm_text", "simulated", "received_at", "demo",
                                       "aed", "lang")} | {"stage": stage})

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "demo_rows": sum(j["demo"] for j in js),
        "kpis": {
            "signals": n, "buying": buying, "intent_rate": _pct(buying, n),
            "matched": matched, "match_rate": _pct(matched, buying),
            "dms": dms, "dm_dish": dm_dish, "opens": opens, "open_rate": _pct(opens, dm_dish),
            "orders": orders, "order_rate": _pct(orders, opens), "conversion": _pct(orders, n),
            "gmv": gmv, "aov": round(gmv / orders, 2) if orders else None, "handed_to_app": handed,
            "e2e_p50_ms": _pq(e2e, 0.5), "e2e_p95_ms": _pq(e2e, 0.95),
            "agent_p50_ms": _pq(agent, 0.5), "agent_p95_ms": _pq(agent, 0.95),
            "creators": len({j["creator"] for j in js if j["creator"]}),
            "restaurants": len({j["merchant"] for j in js if j["merchant"]}),
        },
        "funnel": funnel,
        "timeline": _timeline(js, now),
        "by_creator": _group([j for j in js if j["creator"]], "creator", "—"),
        "by_content": _group([dict(j, content=(j["caption"] or "").split("\n")[0][:60] or j["media_id"])
                              for j in js if j["caption"] or j["media_id"]], "content", "—", 6),
        "by_restaurant": _group([j for j in js if j["merchant"]], "merchant", "—", 6),
        "by_area": _group([j for j in js if j["matched"]], "area", "Area unknown", 6),
        "by_source": src,
        "languages": split("lang", ["English", "Arabic", "Franco-Arabic", "Emoji only"]),
        "answer_modes": split("mode", ["live", "rules", "cached", "scripted"], lambda j: j["status"] is not None),
        "top_dishes": [{"label": k, "n": v} for k, v in dishes.most_common(6)],
        "missed": [{"label": k, "n": v} for k, v in missed.most_common(6)],
        "feed": feed,
    }
