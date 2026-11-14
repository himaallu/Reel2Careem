"""[P1] resolve(signal) -> AgentResult: Bedrock Converse tool loop over READ-ONLY Careem tools.

    python -m app.agent --caption "Best shawarma in the Marina @shawarmastation" --text "price?"
    python -m app.agent --target sandbox --caption "sushi night" --text "where is this?"

Budget: 20 s overall, ≤ 12 model turns, Bedrock read_timeout 15 s, no SDK retries.
Every name/id/price/ETA in the result must appear in a tool result (grounding check) or it is dropped;
an ungrounded main dish fails the run so the fallback ladder takes over (badge stays honest).
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from app.contracts import (
    AGENT_RESULT_SCHEMA, Addon, AgentResult, Alternative, Item, Merchant, Signal,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
log = logging.getLogger("genie.agent")

MAX_TURNS = 12
BUDGET_S = float(os.getenv("AGENT_BUDGET_S", "20"))
TOOL_TIMEOUT_S = 6.0
MAX_TOOL_CHARS = 6000          # per tool result sent to the model
MAX_IMAGE_BYTES = 3_750_000    # Bedrock image limit
SUBMIT = "submit_result"
DEFAULT_AGENT_TOOLS = (
    "food_dish_search", "food_merchant_search", "food_dynamic_search", "food_get_restaurant_menu",
    "food_search_restaurant_items", "food_get_item_options", "global_search", "search_groceries_items",
)
PLAYBOOK = (Path(__file__).parent / "prompts" / "careem_playbook.md").read_text(encoding="utf-8")

SYSTEM_PROMPT = f"""You are Careem Genie. People react to food posts on Instagram ("price?", "where is this?", \
"yummy", "بكم"). Your job: find the dish from the post on Careem Food near the user, so they can order it.

Find, using the tools:
1. The exact dish (or its closest match) and the restaurant selling it. If the post names a restaurant, \
look there first (food_merchant_search, then food_search_restaurant_items or food_get_restaurant_menu); \
otherwise use food_dish_search.
2. 2-3 similar dishes at OTHER restaurants for price comparison (food_dish_search results are ideal).
3. One cheap add-on (side, drink or dessert) from the chosen restaurant's menu.

Be fast: aim for 2-4 tool turns and call independent tools in parallel in the same turn. \
Location is added automatically - never pass latitude or longitude.

Rules:
- Every merchant name/id, item name/id, price and ETA you report must be copied from a tool result. Never invent.
- price_aed = the item's price total; eta_min = the restaurant's delivery average (minutes).
- status: "found" = the dish (or a near-identical variant) is available; "closest" = only similar dishes; \
"none" = nothing suitable on Careem (put what they wanted in missed_demand); "not_buying" = not about food.
- confidence: 0-1, how sure you are this is the dish in the post.
- Finish by calling {SUBMIT} exactly once. Do not write any other final text.

Careem's agent playbook (rides/checkout parts do not apply to you; you only have read-only tools):
{PLAYBOOK}"""


class AgentError(RuntimeError):
    """The live agent could not produce a trustworthy result (caller falls back)."""


# ================================================================ Bedrock client
_bedrock: Any = None


def bedrock_client() -> Any:
    global _bedrock
    if _bedrock is None:
        import boto3
        from botocore.config import Config

        _bedrock = boto3.client(
            "bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"),
            config=Config(read_timeout=15, connect_timeout=3, retries={"max_attempts": 1}),
        )
    return _bedrock


def _model_id() -> str:
    return os.getenv("AGENT_MODEL_ID", "global.anthropic.claude-sonnet-5-5")


# ================================================================ tools
def _strip_location(schema: dict[str, Any]) -> dict[str, Any]:
    s = json.loads(json.dumps(schema))
    s.pop("$schema", None)
    for k in ("latitude", "longitude"):
        (s.get("properties") or {}).pop(k, None)
    if "required" in s:
        s["required"] = [r for r in s["required"] if r not in ("latitude", "longitude")]
    return s


def _submit_schema() -> dict[str, Any]:
    s = json.loads(json.dumps(AGENT_RESULT_SCHEMA))
    for k in ("mode", "steps", "latency_ms"):
        s["properties"].pop(k, None)
    return s


def tool_config(mcp: Any) -> dict[str, Any]:
    wanted = [t.strip() for t in os.getenv("AGENT_TOOLS", ",".join(DEFAULT_AGENT_TOOLS)).split(",") if t.strip()]
    specs = {s["name"]: s for s in mcp.tool_specs()}       # READ_ONLY ∩ server, read-only annotated
    tools = [{"toolSpec": {"name": n, "description": specs[n]["description"][:1500],
                           "inputSchema": {"json": _strip_location(specs[n]["inputSchema"])}}}
             for n in wanted if n in specs]
    if not tools:
        raise AgentError("no read-only Careem tools available")
    tools.append({"toolSpec": {
        "name": SUBMIT, "description": "Submit the final result. Call exactly once, at the end.",
        "inputSchema": {"json": _submit_schema()},
    }})
    return {"tools": tools}


# ================================================================ compaction (what the model sees)
_DROP_KEYS = re.compile(
    r"(_localized$|^link$|_link$|^coordinate$|^currency$|^brand_media$|^_score$|^ads_|^keywords|^logo_url$"
    r"|^highlighted_text$|^ddf_|^image_url$|^contract_id$|^city_id$|^superapp|^busyness$|^menu_layout$)"
)


def compact(value: Any, depth: int = 0, list_limit: int = 8) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _DROP_KEYS.search(k) or v in (None, "", [], {}):
                continue
            if k == "delivery" and isinstance(v, dict):
                v = {x: v[x] for x in ("average", "range", "fee") if x in v}
            elif k == "price" and isinstance(v, dict) and "total" in v:
                v = {x: v[x] for x in ("total", "original") if x in v and (x == "total" or v[x] != v["total"])}
            elif k == "cuisines" and isinstance(v, list):
                v = [c.get("name") for c in v if isinstance(c, dict)][:4]
            elif k == "name" and isinstance(v, dict) and "value" in v:
                v = v["value"]
            out[k] = compact(v, depth + 1, list_limit)
        return out
    if isinstance(value, list):
        return [compact(v, depth + 1, list_limit) for v in value[:list_limit]]
    if isinstance(value, str) and len(value) > 160:
        return value[:157] + "..."
    return value


def compact_text(value: Any) -> str:
    for limit in (8, 5, 3, 2):
        s = json.dumps(compact(value, list_limit=limit), ensure_ascii=False, separators=(",", ":"))
        if len(s) <= MAX_TOOL_CHARS:
            return s
    return s[:MAX_TOOL_CHARS] + "…(truncated)"


# ================================================================ grounding
class Evidence:
    """What the tools actually returned, indexed for the grounding check.

    items:       every dict with an id, a name ("name" or "item") and a price -> its OWN price numbers
                 (not nested option prices) and the restaurant it was found under.
    restaurants: every dict with an id + name + delivery/items/menu -> its delivery ETA numbers.
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.restaurants: list[dict[str, Any]] = []
        self.images: dict[str, str] = {}   # item id -> image_url (stripped from what the model sees)

    def add(self, value: Any, rest: Optional[dict[str, Any]] = None) -> None:
        if isinstance(value, list):
            for v in value:
                self.add(v, rest)
            return
        if not isinstance(value, dict):
            return
        name = value.get("name") if isinstance(value.get("name"), str) else (value.get("name") or {}).get("value") \
            if isinstance(value.get("name"), dict) else value.get("item")
        if value.get("restaurant_id") is not None and rest is None:
            rest = {"id": str(value["restaurant_id"]), "name": None, "eta": set()}
        if value.get("id") is not None and name and any(k in value for k in ("delivery", "menu", "brand", "brand_id")):
            rest = {"id": str(value["id"]), "name": str(name).lower(), "eta": _numbers(value.get("delivery"))}
            self.restaurants.append(rest)
        elif value.get("id") is not None and name and "price" in value:
            self.items.append({"id": str(value["id"]), "name": str(name).lower(),
                               "prices": _item_prices(value["price"]), "rest": rest})
            if isinstance(value.get("image_url"), str):
                self.images[str(value["id"])] = value["image_url"]
        for k, v in value.items():
            if isinstance(v, (dict, list)) and k != "price":
                self.add(v, rest)

    def find_item(self, item: dict[str, Any], merchant: Optional[dict[str, Any]],
                  price: Any = None) -> Optional[dict[str, Any]]:
        iid, iname = _sid(item.get("id")), _low(item.get("name"))
        for rec in self.items:
            if not ((iid and rec["id"] == iid) or (iname and rec["name"] == iname)):
                continue
            if iname and rec["name"] != iname:
                continue
            if merchant and rec["rest"] and not _same_rest(rec["rest"], merchant):
                continue
            if price is not None and _num(price) not in rec["prices"]:
                continue
            return rec
        return None

    def find_restaurant(self, merchant: dict[str, Any]) -> Optional[dict[str, Any]]:
        return next((r for r in self.restaurants if _same_rest(r, merchant)), None)


def _numbers(v: Any) -> set[float]:
    if isinstance(v, bool):
        return set()
    if isinstance(v, (int, float)):
        return {float(v)}
    if isinstance(v, dict):
        return set().union(*(_numbers(x) for x in v.values())) if v else set()
    return set()


def _item_prices(price: Any) -> set[float]:
    """The item's own price: total (and original if discounted); never discount/0 fields."""
    if isinstance(price, dict):
        return {float(price[k]) for k in ("total", "original") if isinstance(price.get(k), (int, float))}
    return _numbers(price)


def _sid(v: Any) -> Optional[str]:
    return None if v is None else str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def _low(v: Any) -> Optional[str]:
    return str(v).strip().lower() if v else None


def _same_rest(rec: dict[str, Any], merchant: dict[str, Any]) -> bool:
    mid, mname = _sid(merchant.get("id")), _low(merchant.get("name"))
    if mid and rec.get("id") == mid:
        return not (mname and rec.get("name") and rec["name"] != mname)
    return bool(mname and rec.get("name") == mname and not mid)


def _grounded_item(ev: Evidence, merchant: Optional[dict], item: Optional[dict],
                   price: Any = None, eta: Any = None) -> bool:
    """Item exists under that merchant with that price; merchant exists with that ETA."""
    if not merchant or not item:
        return False
    for p in {x for x in (price, item.get("price_aed")) if x is not None} or {None}:
        if ev.find_item(item, merchant, p) is None:
            return False
    rest = ev.find_restaurant(merchant)
    rec = ev.find_item(item, merchant)
    if rest is None and not (rec and rec["rest"] and _same_rest(rec["rest"], merchant)):
        return False
    for e in {x for x in (eta, item.get("eta_min")) if x is not None}:
        if rest is None or _num(e) not in rest["eta"]:
            return False
    return True


def _num(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_result(raw: dict[str, Any], ev: Evidence, *, mode: str, steps: list[str], t0: float) -> AgentResult:
    status = raw.get("status")
    if status not in ("found", "closest", "none", "not_buying"):
        raise AgentError(f"bad status {status!r}")
    m, it = raw.get("merchant"), raw.get("item")
    merchant = item = None
    if status in ("found", "closest"):
        if not _grounded_item(ev, m, it):
            raise AgentError("main dish is not grounded in tool results")
        merchant = Merchant(name=m["name"], id=str(m["id"]) if m.get("id") is not None else None)
        item = Item(name=it["name"], id=str(it["id"]) if it.get("id") is not None else None,
                    price_aed=it.get("price_aed"), eta_min=it.get("eta_min"),
                    image_url=it.get("image_url") or ev.images.get(str(it.get("id"))))
    alts: list[Alternative] = []
    for a in raw.get("alternatives") or []:
        am, ai = a.get("merchant"), a.get("item")
        if not _grounded_item(ev, am, ai, a.get("price_aed"), a.get("eta_min")):
            steps.append(f"dropped ungrounded alternative {ai and ai.get('name')!r}")
            continue
        if merchant and str(am.get("id")) == merchant.id and ai.get("name") == item.name:
            continue
        alts.append(Alternative(
            merchant=Merchant(am["name"], str(am["id"]) if am.get("id") is not None else None),
            item=Item(ai["name"], str(ai["id"]) if ai.get("id") is not None else None,
                      ai.get("price_aed", a.get("price_aed")), ai.get("eta_min", a.get("eta_min"))),
            price_aed=a.get("price_aed", ai.get("price_aed")), eta_min=a.get("eta_min", ai.get("eta_min")),
        ))
    addon = None
    ad = raw.get("addon")
    if ad and ad.get("name") and ev.find_item({"name": ad["name"]}, m if merchant else None, ad.get("price_aed")):
        addon = Addon(name=ad["name"], price_aed=ad.get("price_aed"))
    elif ad:
        steps.append(f"dropped ungrounded add-on {ad.get('name')!r}")
    return AgentResult(
        status=status, mode=mode, wanted=str(raw.get("wanted") or "")[:200],
        service=raw.get("service") if raw.get("service") in ("food", "groceries", "shops") else "food",
        merchant=merchant, item=item, alternatives=alts[:3], addon=addon,
        confidence=float(raw.get("confidence") or 0.0),
        missed_demand=raw.get("missed_demand") or (raw.get("wanted") if status == "none" else None),
        steps=steps, latency_ms=int((time.monotonic() - t0) * 1000),
    )


# ================================================================ input
def _user_content(signal: Signal) -> list[dict[str, Any]]:
    parts = [f"Platform: {signal.platform} ({signal.kind})", f"User wrote: {signal.text!r}"]
    if signal.caption:
        parts.append(f"Post caption: {signal.caption!r}")
    if signal.creator:
        parts.append(f"Creator: {signal.creator}")
    if signal.restaurant:
        parts.append(f"Restaurant tagged on the post: {signal.restaurant}")
    content: list[dict[str, Any]] = []
    img = _fetch_image(signal.media_url) if signal.media_url and not signal.caption else None
    if img:
        content.append({"image": {"format": img[0], "source": {"bytes": img[1]}}})
        parts.append("The attached image is the post's thumbnail: identify the dish from it.")
    content.append({"text": "\n".join(parts)})
    return content


def _fetch_image(url: str) -> Optional[tuple[str, bytes]]:
    import httpx

    try:
        r = httpx.get(url, timeout=4, follow_redirects=True)
        r.raise_for_status()
        fmt = r.headers.get("content-type", "").split("/")[-1].split(";")[0].replace("jpg", "jpeg")
        if fmt not in ("jpeg", "png", "gif", "webp") or len(r.content) > MAX_IMAGE_BYTES:
            return None
        return fmt, r.content
    except Exception as e:  # noqa: BLE001
        log.warning("thumbnail download failed: %s", type(e).__name__)
        return None


# ================================================================ the loop
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent")


def _emit(signal_id: str, stage: str, detail: Any, level: str = "info") -> None:
    try:
        from app.events import emit
        emit(signal_id, stage, detail, level)
    except Exception:  # noqa: BLE001
        pass


def _health(ok: bool, detail: str) -> None:
    try:
        from app import db
        db.set_health("bedrock", ok, detail[:300])
    except Exception:  # noqa: BLE001
        pass


def run_agent(signal: Signal, *, mcp: Any = None, client: Any = None) -> AgentResult:
    """Live Bedrock + Careem run. Raises AgentError (or any exception) on failure."""
    from app import careem_mcp

    t0 = time.monotonic()
    deadline = t0 + BUDGET_S
    mcp = mcp or careem_mcp.get()
    client = client or bedrock_client()
    mode = "live" if mcp.target == "production" else "scripted"
    tools = tool_config(mcp)
    messages: list[dict[str, Any]] = [{"role": "user", "content": _user_content(signal)}]
    ev = Evidence()
    steps: list[str] = []
    nudged = False
    kwargs: dict[str, Any] = {}
    if os.getenv("AGENT_EFFORT"):
        kwargs["additionalModelRequestFields"] = {"output_config": {"effort": os.environ["AGENT_EFFORT"]}}

    for turn in range(1, MAX_TURNS + 1):
        left = deadline - time.monotonic()
        if left < 1.5:
            raise AgentError(f"budget {BUDGET_S:.0f}s exhausted after {turn - 1} turns")
        t_turn = time.monotonic()
        fut = _pool.submit(
            client.converse, modelId=_model_id(), system=[{"text": SYSTEM_PROMPT}], messages=messages,
            toolConfig=tools, inferenceConfig={"maxTokens": 2048}, **kwargs,
        )
        try:
            resp = fut.result(timeout=left)
        except FutureTimeout:
            raise AgentError(f"Bedrock turn {turn} exceeded the {BUDGET_S:.0f}s budget") from None
        except Exception as e:  # noqa: BLE001
            _health(False, f"{type(e).__name__}: {e}")
            raise AgentError(f"Bedrock error: {type(e).__name__}: {str(e)[:200]}") from None
        _health(True, f"turn {turn} ok")
        msg = resp["output"]["message"]
        messages.append(msg)      # appended unchanged: keeps reasoning blocks valid for the next turn
        stop = resp.get("stopReason")
        uses = [c["toolUse"] for c in msg.get("content", []) if "toolUse" in c]
        said = " ".join(c["text"] for c in msg.get("content", []) if "text" in c).strip()
        ms = int((time.monotonic() - t_turn) * 1000)
        _emit(signal.id, "agent_step", {
            "turn": turn, "ms": ms, "stop": stop, "said": said[:300],
            "tools": [u["name"] for u in uses], "tokens": resp.get("usage", {}),
        })

        submit = next((u for u in uses if u["name"] == SUBMIT), None)
        if submit is not None:
            steps.append(f"turn {turn}: submit_result ({ms} ms)")
            res = build_result(submit.get("input") or {}, ev, mode=mode, steps=steps, t0=t0)
            return res

        if stop == "tool_use" and uses:
            steps.append(f"turn {turn}: " + ", ".join(f"{u['name']}({_args_summary(u.get('input'))})" for u in uses)
                         + f" ({ms} ms)")
            messages.append({"role": "user", "content": _run_tools(uses, signal, mcp, ev, deadline)})
            continue
        if stop in ("end_turn", "max_tokens"):
            raw = _json_from_text(said)
            if raw:
                steps.append(f"turn {turn}: result as text ({ms} ms)")
                return build_result(raw, ev, mode=mode, steps=steps, t0=t0)
            if nudged:
                raise AgentError("model ended without a result after a nudge")
            nudged = True
            messages.append({"role": "user", "content": [{"text": f"Now call {SUBMIT} with your result."}]})
            continue
        raise AgentError(f"model stopped with {stop!r}")
    raise AgentError(f"no result after {MAX_TURNS} turns")


def _run_tools(uses: list[dict[str, Any]], signal: Signal, mcp: Any, ev: Evidence,
               deadline: float) -> list[dict[str, Any]]:
    from app import careem_mcp

    def one(u: dict[str, Any]) -> dict[str, Any]:
        name, args = u["name"], dict(u.get("input") or {})
        args.pop("latitude", None)
        args.pop("longitude", None)
        timeout = max(1.0, min(TOOL_TIMEOUT_S, deadline - time.monotonic() - 1))
        try:
            out = mcp.call(name, args, signal_id=signal.id, timeout=timeout)
            ev.add(out)
            status = "error" if isinstance(out, dict) and out.get("error") and len(out) <= 3 else "success"
            return {"toolResult": {"toolUseId": u["toolUseId"], "status": status,
                                   "content": [{"text": compact_text(out)}]}}
        except careem_mcp.ToolNotAllowed as e:
            text = f"not allowed: {e}"
        except Exception as e:  # noqa: BLE001
            text = f"{type(e).__name__}: {str(e)[:300]}"
        return {"toolResult": {"toolUseId": u["toolUseId"], "status": "error", "content": [{"text": text}]}}

    return list(_pool.map(one, uses))


def _args_summary(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    return ", ".join(f"{k}={str(v)[:30]}" for k, v in args.items() if k not in ("latitude", "longitude"))


def _json_from_text(text: str) -> Optional[dict[str, Any]]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) and "status" in v else None
    except ValueError:
        return None


# ================================================================ entry point used by the pipeline
_mode_override: Optional[str] = None


def set_mode(mode: Optional[str]) -> None:
    """Dashboard mode switch: 'live' | 'cached' | 'scripted' (None = back to AGENT_MODE)."""
    global _mode_override
    _mode_override = mode if mode in ("live", "cached", "scripted") else None


def current_mode() -> str:
    return _mode_override or os.getenv("AGENT_MODE", "live").strip().lower()


def resolve(signal: Signal) -> AgentResult:
    """Never raises. live → fallback ladder (fallbacks.py, P1-5) → fake_resolve as the last resort."""
    mode = current_mode()
    reason = f"AGENT_MODE={mode}"
    if mode == "live":
        try:
            result = run_agent(signal)
            _after(signal, result)
            return result
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
            log.warning("live agent failed: %s", reason)
    _emit(signal.id, "fallback", {"reason": reason[:300], "from": mode}, "warn")
    try:
        from app import fallbacks
        result = fallbacks.fallback_resolve(signal, reason=reason, mode=mode)
    except (ImportError, AttributeError):
        result = fake_resolve(signal)
        result.steps.append(f"fallback: {reason[:120]}")
    _after(signal, result)
    return result


def _after(signal: Signal, result: AgentResult) -> None:
    if result.status == "none" and result.missed_demand:
        try:
            from app import db
            db.add_missed(signal.id, result.missed_demand)
        except Exception:  # noqa: BLE001
            pass


def fake_resolve(signal: Signal) -> AgentResult:
    """Deterministic stand-in result (mode=scripted so it is never mistaken for live data)."""
    return AgentResult(
        status="found", mode="scripted", wanted=signal.caption or signal.text, service="food",
        merchant=Merchant(name="Demo Shawarma House", id="demo-m1"),
        item=Item(name="Chicken Shawarma Wrap", id="demo-i1", price_aed=18.0, eta_min=25),
        alternatives=[
            Alternative(Merchant("Al Demo Grill", "demo-m2"), Item("Shawarma Plate", "demo-i2"), 24.0, 30),
            Alternative(Merchant("Wrap Lab", "demo-m3"), Item("Classic Shawarma", "demo-i3"), 16.5, 35),
        ],
        addon=Addon(name="Garlic Sauce", price_aed=3.0),
        confidence=0.9, steps=["fake_resolve"], latency_ms=5,
    )


# ================================================================ CLI
def main() -> int:
    p = argparse.ArgumentParser(description="Run the Genie agent once and print the AgentResult")
    p.add_argument("--text", default="price?", help="comment / DM text")
    p.add_argument("--caption", default=None)
    p.add_argument("--creator", default=None)
    p.add_argument("--restaurant", default=None)
    p.add_argument("--image", default=None, help="thumbnail URL (used when there is no caption)")
    p.add_argument("--target", choices=["production", "sandbox"], default=None)
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    if a.target:
        os.environ["CAREEM_TARGET"] = a.target
    from app import db
    db.init_db()
    sig = Signal(id=f"cli:{int(time.time())}", platform="instagram", kind="comment", source_id=f"cli-{time.time()}",
                 user_ref="cli", text=a.text, caption=a.caption, creator=a.creator, restaurant=a.restaurant,
                 media_url=a.image)
    t = time.monotonic()
    try:
        res = run_agent(sig)
    except Exception as e:  # noqa: BLE001
        print(f"❌ live agent failed after {time.monotonic() - t:.1f}s: {type(e).__name__}: {e}")
        return 1
    print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
    print(f"\n✅ {res.status} in {res.latency_ms} ms, mode={res.mode}, {len(res.alternatives)} alternatives, "
          f"add-on={'yes' if res.addon else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
