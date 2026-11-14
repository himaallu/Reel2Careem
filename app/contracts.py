"""Shared contracts between P1 (Brain) and P2 (Surfaces).

FROZEN after 12:00 unless both agree. Plain dataclasses + JSON schemas.
Every dataclass has to_dict()/from_dict() so it round-trips through SQLite JSON columns.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

Platform = Literal["instagram"]
SignalKind = Literal["dm_share", "comment"]
ResultStatus = Literal["found", "closest", "none", "not_buying"]
ResultMode = Literal["live", "cached", "scripted", "rules"]
Service = Literal["food", "groceries", "shops"]
OrderStatus = Literal["basket", "checked_out", "failed", "handed_to_app"]

SIGNAL_KINDS = ("dm_share", "comment")
RESULT_STATUSES = ("found", "closest", "none", "not_buying")
RESULT_MODES = ("live", "cached", "scripted", "rules")
SERVICES = ("food", "groceries", "shops")
ORDER_STATUSES = ("basket", "checked_out", "failed", "handed_to_app")
MAX_ALTERNATIVES = 3


def utc_now() -> str:
    """ISO-8601 UTC timestamp. All DB times are UTC; UI converts to Dubai time."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- Signal
@dataclass
class Signal:
    id: str                      # internal id, e.g. "ig:comment:<source_id>"
    platform: Platform
    kind: SignalKind
    source_id: str               # comment_id | message_id (dedupe key, UNIQUE in DB)
    user_ref: str                # IGSID or comment author username
    text: str
    media_id: Optional[str] = None
    media_url: Optional[str] = None
    caption: Optional[str] = None
    creator: Optional[str] = None
    restaurant: Optional[str] = None
    received_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})

    @staticmethod
    def make_id(platform: str, kind: str, source_id: str) -> str:
        return f"{platform}:{kind}:{source_id}"


# ---------------------------------------------------------------- AgentResult
@dataclass
class Merchant:
    name: str
    id: Optional[str] = None
    area: Optional[str] = None   # e.g. "Dubai Marina", from the Careem restaurant location (optional)


@dataclass
class Item:
    name: str
    id: Optional[str] = None
    price_aed: Optional[float] = None
    eta_min: Optional[int] = None
    image_url: Optional[str] = None


@dataclass
class Alternative:
    merchant: Merchant
    item: Item
    price_aed: Optional[float] = None
    eta_min: Optional[int] = None


@dataclass
class Addon:
    name: str
    price_aed: Optional[float] = None


@dataclass
class AgentResult:
    status: ResultStatus
    mode: ResultMode
    wanted: str                                  # what the user wanted, in words
    service: Service = "food"
    merchant: Optional[Merchant] = None          # None when status is none/not_buying
    item: Optional[Item] = None
    alternatives: list[Alternative] = field(default_factory=list)   # ≤ 3
    addon: Optional[Addon] = None
    confidence: float = 0.0                      # 0..1
    missed_demand: Optional[str] = None
    steps: list[str] = field(default_factory=list)
    latency_ms: int = 0

    def __post_init__(self) -> None:
        self.alternatives = self.alternatives[:MAX_ALTERNATIVES]
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentResult":
        m, it, ad = d.get("merchant"), d.get("item"), d.get("addon")
        return cls(
            status=d["status"],
            mode=d["mode"],
            wanted=d.get("wanted", ""),
            service=d.get("service", "food"),
            merchant=Merchant(**m) if m else None,
            item=Item(**it) if it else None,
            alternatives=[
                Alternative(
                    merchant=Merchant(**a["merchant"]),
                    item=Item(**a["item"]),
                    price_aed=a.get("price_aed"),
                    eta_min=a.get("eta_min"),
                )
                for a in d.get("alternatives") or []
            ],
            addon=Addon(**ad) if ad else None,
            confidence=d.get("confidence", 0.0),
            missed_demand=d.get("missed_demand"),
            steps=list(d.get("steps") or []),
            latency_ms=int(d.get("latency_ms") or 0),
        )


# ---------------------------------------------------------------- Match / Order
@dataclass
class Match:
    token: str
    signal_id: str
    result: AgentResult
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {"token": self.token, "signal_id": self.signal_id,
                "result": self.result.to_dict(), "created_at": self.created_at}


@dataclass
class Order:
    match_token: str
    status: OrderStatus
    aed: Optional[float] = None
    service: Service = "food"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- JSON schemas
# AGENT_RESULT_SCHEMA is what the LLM must return (mode/steps/latency_ms are filled by code).
_NUM_OR_NULL = {"type": ["number", "null"]}
_INT_OR_NULL = {"type": ["integer", "null"]}
_STR_OR_NULL = {"type": ["string", "null"]}

MERCHANT_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "id": _STR_OR_NULL, "area": _STR_OR_NULL},
    "required": ["name"],
}
ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "id": _STR_OR_NULL, "price_aed": _NUM_OR_NULL,
        "eta_min": _INT_OR_NULL, "image_url": _STR_OR_NULL,
    },
    "required": ["name"],
}
AGENT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": list(RESULT_STATUSES)},
        "mode": {"enum": list(RESULT_MODES)},
        "wanted": {"type": "string"},
        "service": {"enum": list(SERVICES)},
        "merchant": {"anyOf": [MERCHANT_SCHEMA, {"type": "null"}]},
        "item": {"anyOf": [ITEM_SCHEMA, {"type": "null"}]},
        "alternatives": {
            "type": "array", "maxItems": MAX_ALTERNATIVES,
            "items": {
                "type": "object",
                "properties": {"merchant": MERCHANT_SCHEMA, "item": ITEM_SCHEMA,
                               "price_aed": _NUM_OR_NULL, "eta_min": _INT_OR_NULL},
                "required": ["merchant", "item"],
            },
        },
        "addon": {"anyOf": [
            {"type": "object",
             "properties": {"name": {"type": "string"}, "price_aed": _NUM_OR_NULL},
             "required": ["name"]},
            {"type": "null"},
        ]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "missed_demand": _STR_OR_NULL,
        "steps": {"type": "array", "items": {"type": "string"}},
        "latency_ms": {"type": "integer"},
    },
    "required": ["status", "wanted", "service", "confidence"],
}

SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"}, "platform": {"enum": ["instagram"]},
        "kind": {"enum": list(SIGNAL_KINDS)}, "source_id": {"type": "string"},
        "user_ref": {"type": "string"}, "text": {"type": "string"},
        "media_id": _STR_OR_NULL, "media_url": _STR_OR_NULL, "caption": _STR_OR_NULL,
        "creator": _STR_OR_NULL, "restaurant": _STR_OR_NULL, "received_at": {"type": "string"},
    },
    "required": ["id", "platform", "kind", "source_id", "user_ref", "text"],
}
