"""Owner linkedin.get_profile — Composio profile read behind policy, never a slug."""

from __future__ import annotations

from typing import Any

from app.integrations.linkedin import LinkedInPort


def linkedin_get_profile(port: LinkedInPort, args: dict[str, Any]) -> dict[str, Any]:
    del args
    profile = port.get_my_profile()
    if profile is None:
        return {
            "found": False,
            "name": "",
            "headline": "",
            "profile": None,
            "missing_sections": [],
        }
    return {
        "found": True,
        "name": profile.name,
        "headline": profile.headline,
        "profile": profile.model_dump(),
        "missing_sections": profile.missing_sections(),
    }


def linkedin_handlers(port: LinkedInPort) -> dict[str, Any]:
    return {"linkedin.get_profile": lambda args: linkedin_get_profile(port, args)}
