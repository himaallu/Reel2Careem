"""[P2] DM / comment copy templates. Pure functions of AgentResult -> str (Instagram DM limit ~1000 chars)."""
from __future__ import annotations

from typing import Optional

from app.contracts import AgentResult

DM_LIMIT = 1000

COMMENT_REPLY = "Sent you a DM 📩 — check your message requests if you don't follow us!"


def _aed(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.2f}"


def _eta(v: Optional[int]) -> str:
    return f" · ~{v} min" if v else ""


def _alts(r: AgentResult, n: int = 3) -> list[str]:
    out = []
    for a in r.alternatives[:n]:
        price = a.price_aed if a.price_aed is not None else a.item.price_aed
        out.append(f"• {a.item.name} — {a.merchant.name} · AED {_aed(price)}{_eta(a.eta_min or a.item.eta_min)}")
    return out


def _cap(text: str) -> str:
    return text if len(text) <= DM_LIMIT else text[: DM_LIMIT - 1] + "…"


def comment_reply(_: Optional[AgentResult] = None) -> str:
    return COMMENT_REPLY


def dm_text(r: AgentResult, link: Optional[str]) -> str:
    """The one DM Genie sends for a result."""
    pay = f"\nPay in Careem 👉 {link}" if link else ""
    stale = r.mode == "cached"
    head_found = "Careem is taking a breather 😅 — here's what I found earlier today:\n" if stale else ""

    if r.status == "found" and r.item and r.merchant:
        lines = [f"{head_found}🧞 Found it on Careem! {r.item.name} from {r.merchant.name} · "
                 f"AED {_aed(r.item.price_aed)}{_eta(r.item.eta_min)}"]
        alts = _alts(r)
        if alts:
            lines.append("Similar near you:\n" + "\n".join(alts))
        if r.addon:
            lines.append(f"Add {r.addon.name} for AED {_aed(r.addon.price_aed)}?")
        return _cap("\n".join(lines) + pay)

    if r.status == "closest":
        lines = [f"{head_found}Couldn't find that exact dish on Careem, but these are close:"]
        if r.item and r.merchant:
            lines.append(f"⭐ {r.item.name} — {r.merchant.name} · AED {_aed(r.item.price_aed)}{_eta(r.item.eta_min)}")
        lines += _alts(r)
        if r.addon:
            lines.append(f"Add {r.addon.name} for AED {_aed(r.addon.price_aed)}?")
        return _cap("\n".join(lines) + (pay if r.item else ""))

    # none (not_buying never gets a DM)
    alts = _alts(r)
    if alts:
        return _cap("Not on Careem yet — I've told the team you want it! 🙌\nMeanwhile, these are close:\n"
                    + "\n".join(alts))
    return "Not on Careem yet — I've told the team you want it! 🙌"


def no_caption_dm() -> str:
    return "🧞 Got your reel! I couldn't tell which dish it is — reply with the dish or restaurant name and I'll find it on Careem."
