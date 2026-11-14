"""[P2, shared] emit(signal_id, stage, detail, level) -> DB + SSE broadcast.

Skeleton version: writes to DB only. P2 adds the SSE broadcast (P2-6); keep the signature.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app import db

log = logging.getLogger("genie.events")


def emit(signal_id: Optional[str], stage: str, detail: Any = "", level: str = "info") -> dict[str, Any]:
    ev = db.add_event(signal_id, stage, detail, level)
    log.info("[%s] %s %s", stage, signal_id or "-", str(ev["detail"])[:200])
    return ev
