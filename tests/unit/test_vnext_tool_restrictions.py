import pytest
from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.core.errors import CapabilityUnavailable, PermissionDenied


def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(CapabilityUnavailable):
        execute_capability(
            "composio.GMAIL_SEND_EMAIL",
            principal=Principal.owner(source="test"),
            handlers={},
        )


def test_owner_cannot_run_unregistered_handler() -> None:
    with pytest.raises(CapabilityUnavailable):
        execute_capability(
            "mail.read",
            principal=Principal.owner(source="test"),
            args={"message_id": "x"},
            handlers={},
        )


def test_client_cannot_use_owner_leads_capability() -> None:
    with pytest.raises(PermissionDenied):
        execute_capability(
            "leads.get_recent",
            principal=Principal.client(source="test"),
            handlers={"leads.get_recent": lambda _args: {"leads": []}},
        )
