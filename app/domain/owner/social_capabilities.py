"""Social capability truth: what Mia can actually do on LinkedIn and Instagram.

Computed from configuration only -- the caller resolves each port the same way the
real read tools do (see `app/tools/owner/analytics.py::_social_capabilities`) and
this module only formats the result. No provider call happens anywhere in this
file. Instagram publishing stays policy-denied regardless of configuration
(`app/domain/owner/composio_effects.py`, unchanged); LinkedIn writes stay on their
named approval path (`app/domain/owner/linkedin_writes.py`, unchanged). Nothing
here grants a new write capability.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SocialCapabilities:
    linkedin_configured: bool
    instagram_configured: bool


def format_social_capabilities(caps: SocialCapabilities) -> str:
    """One factual line per capability -- never a blanket "connected" verdict."""
    linkedin_read = "available" if caps.linkedin_configured else "not configured"
    instagram_read = "available" if caps.instagram_configured else "not configured"
    lines = [
        "Social capabilities (from configuration; not a live provider check):",
        f"- LinkedIn profile read: {linkedin_read}. Own profile only -- no reach, "
        "follower, or post analytics on any path.",
        "- LinkedIn post or comment: exact approval only, then execution -- not "
        "yet verified live; whether it is actually visible on LinkedIn afterward "
        "is not independently re-checked.",
        f"- Instagram insights read: {instagram_read}. Per-post metrics only; a "
        "missing metric comes back unavailable, never zero or invented.",
        "- Instagram publishing: not available -- denied by policy, regardless "
        "of configuration.",
        "- Instagram direct messages and ads: not available on any path.",
        "- No scheduling on either platform.",
        "- A draft, caption, or content idea is data to write from, not a "
        "published post; it still needs its own separate approval to go out.",
    ]
    return "\n".join(lines)
