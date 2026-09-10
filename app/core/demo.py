"""Demo mode helpers. Fail-closed in prod; synthetic data only."""

from __future__ import annotations

from app.core.config import MiaEnv, Settings

DEMO_LABEL = "synthetic"

SYNTHETIC_ATTRIBUTION: dict[str, str] = {
    "utm_source": "mia_demo",
    "utm_medium": "demo",
    "utm_campaign": "synthetic",
}

def demo_mode_active(settings: Settings) -> bool:
    """True only when demo flag is on and env is not production."""
    return settings.demo_mode and settings.env != MiaEnv.PROD
