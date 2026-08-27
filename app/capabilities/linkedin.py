"""Owner linkedin.get_profile — Composio profile read behind policy, never a slug."""

from __future__ import annotations

from typing import Any

from app.integrations.linkedin import LinkedInPort


def linkedin_get_profile(port: LinkedInPort, args: dict[str, Any]) -> dict[str, Any]:
    del args
    profile = port.get_my_profile()
    if profile is None:
        return {"found": False, "name": "", "headline": ""}
    return {
        "found": True,
        "name": profile.name,
        "headline": profile.headline,
    }


def linkedin_handlers(port: LinkedInPort) -> dict[str, Any]:
    return {"linkedin.get_profile": lambda args: linkedin_get_profile(port, args)}
