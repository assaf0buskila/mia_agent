"""A failed CRM write must never be reported to Assaf as a success.

Composio answers HTTP 200 with `successful: false` when a write was accepted but did
not happen. `_execute_upsert` returned quietly on that, and the handler reported
"Wrote Contacts" on any non-exception — so a rejected write looked done.
"""

from __future__ import annotations

import httpx
import pytest
from app.domain.tools import AdapterHttpError, AdapterResponseError, AdapterSchemaError
from app.integrations.sheets import ComposioSheetsPort

_SHEET = "1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI"
_CELLS = ["דנה", "0501234567", "", "", "", "", "", "אתר", "", "", "", "", "", ""]


def _port(handler) -> ComposioSheetsPort:
    return ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-test",
        spreadsheet_id=_SHEET,
        allowed_spreadsheet_ids=frozenset({_SHEET}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _respond(status: int, body: object):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return handler


def test_a_real_write_succeeds() -> None:
    port = _port(_respond(200, {"successful": True, "data": {}}))
    port.write_locked_contact(_CELLS, key_column="טלפון")  # must not raise


def test_http_failure_raises() -> None:
    port = _port(_respond(502, {"error": "bad gateway"}))
    with pytest.raises(AdapterHttpError):
        port.write_locked_contact(_CELLS, key_column="טלפון")


def test_http_200_with_successful_false_is_a_failure() -> None:
    """The regression: this used to return quietly and be reported as a success."""
    port = _port(_respond(200, {"successful": False, "error": "quota"}))
    with pytest.raises(AdapterResponseError):
        port.write_locked_contact(_CELLS, key_column="טלפון")


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        {"no_successful_key": True},
        {"successful": "yes"},
        ["a", "list"],
    ],
    ids=["not_json", "missing_key", "wrong_type", "not_an_object"],
)
def test_a_malformed_provider_response_is_a_failure(body) -> None:
    port = _port(_respond(200, body))
    with pytest.raises(AdapterSchemaError):
        port.write_locked_contact(_CELLS, key_column="טלפון")


def test_the_activity_append_is_verified_too() -> None:
    port = _port(_respond(200, {"successful": False}))
    with pytest.raises(AdapterResponseError):
        port.append_locked_activity(["2026-09-05", "מיה", "telegram", "רשמה", "נרשם"])


def test_no_secret_reaches_the_error_text() -> None:
    """Provider detail is useful; the api key is not."""
    port = _port(_respond(200, {"successful": False, "error": "quota exceeded"}))
    try:
        port.write_locked_contact(_CELLS, key_column="טלפון")
    except AdapterHttpError as exc:
        assert "cmp-test" not in str(exc)
        assert "cmp-test" not in repr(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a failure")


def test_the_crm_write_scope_is_allowlisted() -> None:
    """Idempotency claims are refused for a scope that is not on the allowlist."""
    from app.domain.idempotency import ALLOWLISTED_OPERATION_SCOPES

    assert "owner_crm_write" in ALLOWLISTED_OPERATION_SCOPES
