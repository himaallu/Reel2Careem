"""Intent classifier tests (offline: fake Bedrock client for the model path).

    python -m tests.test_intent
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from app import db, intent  # noqa: E402
from app.contracts import Signal  # noqa: E402

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        failures.append(what)


def sig(text: str, kind: str = "comment") -> Signal:
    t = f"{time.time():.6f}"
    return Signal(id=f"t:{t}", platform="instagram", kind=kind, source_id=t, user_ref="u", text=text,
                  caption="Crispy chicken shawarma at Operation Falafel")


class Fake:
    def __init__(self, answer: str | None = None, exc: Exception | None = None, delay: float = 0) -> None:
        self.answer, self.exc, self.delay, self.calls = answer, exc, delay, 0

    def converse(self, **kw):  # noqa: ANN003
        self.calls += 1
        time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return {"output": {"message": {"content": [{"text": self.answer}]}}}


BUYING = ["yummy", "price?", "where is this?", "بكم", "b kam", "how much is this", "I need this 🤤", "😋😋",
          "wen el mat3am?", "بكم الشاورما؟", "وين المطعم", "do you deliver to JLT", "abi wa7ed", "link?",
          "looks so good", "craving this rn", "Yummmm"]
NOT_BUYING = ["follow me for more food pics", "check my page!!", "giveaway on my profile 🎁", "@sara @omar",
              "", "https://bit.ly/x", "Earn $500 a day, DM me for details", "promo code FOOD20 on my bio"]


def main() -> int:
    db.init_db()
    never = Fake(exc=AssertionError("model must not be called for keyword cases"))

    wrong = [t for t in BUYING if not intent.classify(sig(t), client=never).buying]
    check(not wrong and never.calls == 0, f"{len(BUYING)} buying comments -> buy, 0 model calls {wrong}")
    wrong = [t for t in NOT_BUYING if intent.classify(sig(t), client=never).buying]
    check(not wrong and never.calls == 0, f"{len(NOT_BUYING)} spam/empty comments -> no reply {wrong}")
    check(intent.classify(sig("yummy! follow me for more"), client=never).buying is False, "spam beats 'yummy'")
    r = intent.classify(sig("", kind="dm_share"), client=never)
    check(r.buying and r.method == "dm_share", "shared reel always counts")

    # ambiguous -> model
    m = Fake('{"buying": false, "reason": "negative opinion"}')
    r = intent.classify(sig("not hungry, too oily"), client=m)
    check(m.calls == 1 and r.method == "llm" and r.buying is False, f"negated craving -> model -> no ({r.reason})")
    m = Fake('Sure! {"buying": true, "reason": "wants to try it"}')
    r = intent.classify(sig("omg my sister would love this"), client=m)
    check(r.method == "llm" and r.buying, "ambiguous praise -> model -> yes (JSON inside prose parsed)")

    # model down / slow / garbage -> conservative keywords-only fallback
    r = intent.classify(sig("my sister would love this"), client=Fake(exc=RuntimeError("ExpiredToken")))
    check(r.method == "fallback" and r.buying is False, "Bedrock down: unclear statement -> no reply")
    r = intent.classify(sig("is this the dubai mall one"), client=Fake(exc=RuntimeError("down")))
    check(r.method == "fallback" and r.buying is False, "Bedrock down: no '?' -> no reply")
    intent.LLM_TIMEOUT_S = 0.5
    t = time.monotonic()
    r = intent.classify(sig("my sister would love this ؟"), client=Fake('{"buying": true}', delay=2))
    check(time.monotonic() - t < 1.0, f"slow model cut off at the timeout ({time.monotonic() - t:.1f}s)")
    check(r.buying and r.method in ("fallback", "keyword"), f"Arabic question mark counts in fallback ({r.method})")
    r = intent.classify(sig("hmm interesting"), client=Fake("I cannot decide"))
    check(r.method == "fallback" and r.buying is False, "model answer without JSON -> fallback")

    # events
    s = sig("price?")
    intent.classify(s, client=never)
    ev = [e for e in db.events_for(s.id) if e["stage"] == "intent"]
    check(len(ev) == 1 and '"buying": true' in ev[0]["detail"], "intent event emitted for the dashboard")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
