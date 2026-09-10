import re

import pytest
from app.api.deps import get_sheets_port
from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.db.session import init_db
from app.integrations.sheets import (
    ComposioSheetsPort,
    DisabledSheetsPort,
    FakeSheetsPort,
    build_sheets_port,
)
from app.main import app
from fastapi.testclient import TestClient


def _identify_website_visitor(client: TestClient, session_id: str) -> None:
    response = client.post(
        f"/v1/website/sessions/{session_id}/messages",
        json={"text": "צריכים אתר", "phone": "0501234567", "name": "דנה"},
    )
    assert response.status_code == 200
    assert response.json()["next_action"] in {"handoff", "confirm_contact"}
    assert response.json()["lead_id"] == ""


def test_disabled_sheets_port_is_no_op() -> None:
    port = DisabledSheetsPort()
    port.ensure_crm_workspace()
    assert port.list_sheet_names(spreadsheet_id="sheet-abc") == []
    assert port.read_values(spreadsheet_id="sheet-abc", a1_range="Sheet1!A1:B2") == []
    port.update_values(spreadsheet_id="sheet-abc", a1_range="Sheet1!A1:B2", values=[["a", "b"]])
    port.append_values(spreadsheet_id="sheet-abc", a1_range="Sheet1!A1:B2", values=[["a", "b"]])
    port.write_locked_contact(["dana", "0501234567"], key_column="A")
    port.append_locked_activity(["note"])
    assert port.read_locked_contacts() == []


def test_website_session_create_never_returns_a_lead_id() -> None:
    init_db()
    fake = FakeSheetsPort()
    app.dependency_overrides[get_sheets_port] = lambda: fake
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/website/sessions",
                params={"utm_source": "meta", "utm_campaign": "yuma"},
            )
            assert response.status_code == 200
            assert response.json()["lead_id"] == ""
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)


def test_sheets_write_r1_denied_when_kill_switch_on() -> None:
    with pytest.raises(PolicyDenied):
        assert_allowed(
            RiskAction(name="sheets_write", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=True,
        )


def test_build_sheets_port_live_when_all_three_credentials_set() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        sheets_spreadsheet_id="sheet-abc",
    )
    port = build_sheets_port(settings)
    assert isinstance(port, ComposioSheetsPort)
    assert not isinstance(port, DisabledSheetsPort)


def test_build_sheets_port_live_without_spreadsheet_id() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        sheets_spreadsheet_id="",
    )
    assert isinstance(build_sheets_port(settings), ComposioSheetsPort)


@pytest.mark.parametrize(
    "api_key,user_id,spreadsheet_id",
    [
        ("", "", ""),
        ("cmp-live", "", ""),
        ("", "user-123", ""),
        ("   ", "user-123", "sheet-abc"),
        ("cmp-live", "   ", "sheet-abc"),
    ],
)
def test_build_sheets_port_disabled_when_composio_credentials_missing(
    api_key: str,
    user_id: str,
    spreadsheet_id: str,
) -> None:
    settings = Settings(
        composio_api_key=api_key,
        composio_user_id=user_id,
        sheets_spreadsheet_id=spreadsheet_id,
    )
    port = build_sheets_port(settings)
    assert isinstance(port, DisabledSheetsPort)


def test_composio_sheets_port_protocol_has_only_allowlisted_owner_operations() -> None:
    forbidden = frozenset({"clear", "delete", "create", "format", "share", "search"})
    for name in dir(ComposioSheetsPort):
        if name.startswith("_"):
            continue
        words = re.findall(r"[a-z]+", name.lower())
        assert not forbidden.intersection(words)
