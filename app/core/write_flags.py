"""Named write capability flags. Do not override R4/R5 or kill switch."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.risk import RiskLevel

if TYPE_CHECKING:
    from app.core.config import Settings

_WRITE_FLAG_NAMES = frozenset(
    {
        "calendar_write",
        "gmail_send",
        "meta_write",
    }
)


def named_write_may_auto(*, enabled: bool, risk: RiskLevel) -> bool:
    if risk == RiskLevel.R5_DESTRUCTIVE:
        return False
    if risk == RiskLevel.R4_FINANCIAL_MARKETING:
        return False
    return enabled


def write_flag_enabled(settings: Settings, name: str) -> bool:
    if name not in _WRITE_FLAG_NAMES:
        return False
    return bool(getattr(settings, name, False))
