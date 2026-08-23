"""Demo mode helpers. Fail-closed in prod; synthetic data only."""

from __future__ import annotations

from app.core.config import MiaEnv, Settings

DEMO_LABEL = "synthetic"

SYNTHETIC_ATTRIBUTION: dict[str, str] = {
    "utm_source": "mia_demo",
    "utm_medium": "demo",
    "utm_campaign": "synthetic",
}

SCRIPTED_MESSAGES: tuple[tuple[str, str], ...] = (
    ("We run a clinic and miss calls all day.", "deepen_pain"),
    ("we call everyone back by hand from a list", "reflect"),
    ("about two hours every day", "offer_whatsapp"),
    ("I decide this quarter", "offer_hypothesis"),
    ("let's book a meeting", "offer_meeting"),
)


def demo_mode_active(settings: Settings) -> bool:
    """True only when demo flag is on and env is not production."""
    return settings.demo_mode and settings.env != MiaEnv.PROD
