"""[P1] Is this comment / DM someone who wants to buy the dish? (No reply to spam or chit-chat.)

    from app import intent
    r = intent.classify(signal)          # IntentResult(buying, reason, method, latency_ms)
    python -m app.intent "b kam?" "yummy 😋" "follow me for giveaways"

1. dm_share -> always buying (they sent the reel to Genie on purpose).
2. Keyword pre-filter (English, Arabic, Franco-Arabic, emoji): spam -> no; clear buying signal -> yes.
3. Ambiguous -> FAST_MODEL_ID (Haiku) on Bedrock, 4 s timeout, tiny JSON answer.
4. Bedrock down -> keywords only: a question counts as buying, anything else gets no reply.
Every decision carries its method ("dm_share" | "keyword" | "llm" | "fallback") and is emitted as an intent event.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from typing import Any, Optional

from app.contracts import Signal

log = logging.getLogger("genie.intent")
LLM_TIMEOUT_S = 4.0

SPAM = re.compile(
    r"(https?://|www\.|\.com\b|follow (me|us|back)|check (out )?my (page|profile|bio)|\bf4f\b|\bl4l\b|"
    r"giveaway|promo ?code|discount code|\bearn\b|crypto|bitcoin|forex|invest(ment)?|dm (me|us) for|"
    r"link in (my )?bio|collab\??\s*$|sponsor|onlyfans|whats ?app me|\+?\d{9,})", re.I)
NEGATION = re.compile(r"\b(not|don'?t|dont|never|no|hate|gross|disgusting|overpriced|ew+)\b", re.I)
BUY_EN = re.compile(
    r"\b(price|prices|pricing|how much|cost|costs|aed|dhs?|where( is| was| can)?|wher|location|address|"
    r"branch|link|order|ordering|deliver|delivery|available|menu|want|need|craving|crave|hungry|"
    r"yumm*y|yum+|delicious|tasty|looks? (so )?(good|amazing|great)|take my money|send (it|this)|"
    r"near me|open now|try (this|it)|recipe)\b|\?\s*$", re.I)
BUY_FRANCO = re.compile(r"\b(b ?kam|bkm|be ?kam|wen|wein|wain|wayn|abi|abgha|abga|ab8a|ateb|atlub|"
                        r"t[ao]wseel|tawsil|7elw|7lw|yamm+i)\b", re.I)
_AR = "\u0600-\u06FF"
# Arabic has no \b in Python regex: short words get explicit Arabic-letter boundaries
BUY_AR = re.compile(
    rf"(بكم|بكام|كم السعر|كم سعر|السعر|عنوان|ابغى|أبغى|بغيت|اطلب|أطلب|توصيل|يوصل|منيو|لذيذ|جوعان|متوفر|"
    rf"(?<![{_AR}])(?:و?سعره?|وين|فين|موقعه?|ابي|أبي|يم+)(?![{_AR}]))")
BUY_EMOJI = re.compile("[😋🤤😍🥵🔥💯🙏👀]")


@dataclass
class IntentResult:
    buying: bool
    reason: str
    method: str          # dm_share | keyword | llm | fallback
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def keyword_intent(text: str) -> Optional[tuple[bool, str]]:
    """(buying, reason) when the keywords are decisive, else None (ambiguous)."""
    t = (text or "").strip()
    if not t:
        return False, "empty comment"
    if SPAM.search(t):
        return False, "looks like spam / self-promotion"
    if re.fullmatch(r"(@[\w.]+\s*)+", t):
        return False, "only tags someone"
    for name, rx in (("arabic", BUY_AR), ("franco", BUY_FRANCO), ("english", BUY_EN)):
        m = rx.search(t)
        if m:
            if NEGATION.search(t):
                return None      # "not hungry", "overpriced?" -> let the model decide
            return True, f"{name} buying signal {m.group(0).strip()!r}"
    if BUY_EMOJI.search(t) and not NEGATION.search(t):
        return True, "craving emoji"
    return None


# ================================================================ LLM (Haiku) for ambiguous cases
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="intent")
_PROMPT = """A person commented on (or messaged about) a food post on Instagram. Decide if they show interest \
in getting this food (asking price/location/delivery, craving it, wanting to try it) versus spam, \
self-promotion, unrelated chat or a negative opinion. Comment may be English, Arabic or Franco-Arabic.
Answer with JSON only: {{"buying": true|false, "reason": "<6 words>"}}

Post caption: {caption}
Comment: {text}"""


def llm_intent(text: str, caption: Optional[str], client: Any = None) -> tuple[bool, str]:
    from app.agent import bedrock_client

    client = client or bedrock_client()
    model = os.getenv("FAST_MODEL_ID", "global.anthropic.claude-haiku-5-5")
    prompt = _PROMPT.format(caption=(caption or "-")[:400], text=text[:400])
    fut = _pool.submit(client.converse, modelId=model, inferenceConfig={"maxTokens": 400},
                       messages=[{"role": "user", "content": [{"text": prompt}]}])
    resp = fut.result(timeout=LLM_TIMEOUT_S)
    out = " ".join(c.get("text", "") for c in resp["output"]["message"]["content"] if "text" in c)
    m = re.search(r"\{.*?\}", out, re.S)
    if not m:
        raise ValueError(f"no JSON in intent answer: {out[:80]!r}")
    v = json.loads(m.group(0))
    if not isinstance(v.get("buying"), bool):
        raise ValueError("intent answer has no boolean 'buying'")
    return v["buying"], str(v.get("reason") or "")[:80]


def fallback_intent(text: str) -> tuple[bool, str]:
    """Bedrock down: be conservative (no spam) — only a question gets a reply."""
    if "?" in text or "؟" in text:
        return True, "question on our post (keywords only, model unavailable)"
    return False, "unclear without the model — not replying"


# ================================================================ entry point
def classify(signal: Signal, *, client: Any = None) -> IntentResult:
    """Never raises."""
    t0 = time.monotonic()

    def done(buying: bool, reason: str, method: str) -> IntentResult:
        r = IntentResult(buying, reason, method, int((time.monotonic() - t0) * 1000))
        _emit(signal.id, r)
        return r

    if signal.kind == "dm_share":
        return done(True, "shared a post with Genie", "dm_share")
    kw = keyword_intent(signal.text)
    if kw is not None:
        return done(kw[0], kw[1], "keyword")
    try:
        buying, reason = llm_intent(signal.text, signal.caption, client)
        _health(True, "intent ok")
        return done(buying, reason, "llm")
    except FutureTimeout:
        why = f"intent model timed out after {LLM_TIMEOUT_S:.0f}s"
    except Exception as e:  # noqa: BLE001
        why = f"intent model failed: {type(e).__name__}: {str(e)[:120]}"
    log.warning(why)
    _health(False, why)
    buying, reason = fallback_intent(signal.text)
    return done(buying, reason, "fallback")


def is_buying(signal: Signal) -> bool:
    return classify(signal).buying


def _emit(signal_id: str, r: IntentResult) -> None:
    try:
        from app.events import emit
        emit(signal_id, "intent", r.to_dict(), "info" if r.method != "fallback" else "warn")
    except Exception:  # noqa: BLE001
        pass


def _health(ok: bool, detail: str) -> None:
    try:
        from app import db
        db.set_health("bedrock", ok, detail[:300])
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    import sys

    logging.basicConfig(level=logging.WARNING)
    from app import db

    db.init_db()
    for i, text in enumerate(sys.argv[1:] or ["price?"]):
        s = Signal(id=f"cli:intent:{i}", platform="instagram", kind="comment", source_id=f"cli-{time.time()}-{i}",
                   user_ref="cli", text=text)
        r = classify(s)
        print(f"{'🛒' if r.buying else '🚫'} {text!r:40} {r.method:9} {r.latency_ms:>5} ms  {r.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
