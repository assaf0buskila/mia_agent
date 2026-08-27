"""LinkedIn / GSC / GA4 are owner READs through the registry. Sheets is not."""

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


def test_sheets_has_no_owner_capability() -> None:
    names = {item.name for item in CAPABILITIES}
    assert not any(name.startswith("sheets.") for name in names)
