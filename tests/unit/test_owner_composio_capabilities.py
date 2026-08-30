"""Owner-only Composio reads and bounded Sheets operations use the registry."""

from app.capabilities.registry import (
    CAPABILITIES,
    CLIENT_CAPABILITIES,
    OWNER_CAPABILITIES,
    get_capability,
)
from app.capabilities.types import Sensitivity


def test_owner_composio_reads_are_registered() -> None:
    for name in (
        "linkedin.get_profile",
        "search_console.query",
        "analytics.get_traffic",
    ):
        spec = get_capability(name)
        assert spec is not None
        assert spec.sensitivity is Sensitivity.READ
        assert name in OWNER_CAPABILITIES
        assert name not in CLIENT_CAPABILITIES


def test_sheets_owner_capabilities_are_explicit_and_not_client_visible() -> None:
    names = {item.name for item in CAPABILITIES}
    assert {"sheets.read", "sheets.update", "sheets.append"}.issubset(names)
    for name in ("sheets.read", "sheets.update", "sheets.append"):
        assert name in OWNER_CAPABILITIES
        assert name not in CLIENT_CAPABILITIES
