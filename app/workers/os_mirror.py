"""Sheets mirror publisher: a no-op stub from V2-16s. V2-29 fills it (§14)."""

from __future__ import annotations

from typing import Any


def start_if_enabled(settings: Any) -> None:
    """Start the publisher when the mirror is enabled. The stub never starts anything."""
    return None
