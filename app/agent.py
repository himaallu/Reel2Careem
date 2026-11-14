"""[P1] resolve(signal) -> AgentResult  (Bedrock tool loop + fallback ladder). TODO P1-3.

Until then resolve() == fake_resolve() so P2 can build the pipeline against the real contract.
"""
from __future__ import annotations

from app.contracts import Addon, AgentResult, Alternative, Item, Merchant, Signal


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


def resolve(signal: Signal) -> AgentResult:
    return fake_resolve(signal)
