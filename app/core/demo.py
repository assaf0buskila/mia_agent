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
    # ADR-028: the continuation gate offers the booked meeting first (the website's
    # default exit); WhatsApp is offered on the next continuation-ready turn once the
    # meeting was offered and not taken.
    ("about two hours every day", "offer_meeting"),
    ("I decide this quarter", "offer_whatsapp"),
    ("let's book a meeting", "offer_meeting"),
)


def demo_mode_active(settings: Settings) -> bool:
    """True only when demo flag is on and env is not production."""
    return settings.demo_mode and settings.env != MiaEnv.PROD
