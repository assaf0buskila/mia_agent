import json

import httpx
import pytest
from app.capabilities.policy import execute_capability
from app.capabilities.sheets import sheets_handlers
from app.capabilities.types import Principal
from app.domain.tools import AdapterHttpError
from app.integrations.sheets import (
    COMPOSIO_VALUES_APPEND_TOOL,
    COMPOSIO_VALUES_GET_TOOL,
    COMPOSIO_VALUES_UPDATE_TOOL,
    ComposioSheetsPort,
    FakeSheetsPort,
)

_OWNER = Principal.owner(source="test", actor_id="123")
_SHEET = "sheet-allowed"


def _port(client: httpx.Client) -> ComposioSheetsPort:
    return ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-test",
        allowed_spreadsheet_ids=frozenset({_SHEET}),
        client=client,
    )


def test_owner_sheets_read_update_append_request_shapes() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), json.loads(request.content)))
        if str(request.url).endswith(COMPOSIO_VALUES_GET_TOOL):
            return httpx.Response(200, json={"successful": True, "data": {"values": [["a", "b"]]}})
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    handlers = sheets_handlers(port, allowed_spreadsheet_ids=frozenset({_SHEET}))
    assert execute_capability(
        "sheets.read",
        principal=_OWNER,
        args={"spreadsheet_id": _SHEET, "range": "KPI!A1:B2"},
        handlers=handlers,
    ) == {"count": 1, "rows": [["a", "b"]]}
    execute_capability(
        "sheets.update",
        principal=_OWNER,
        args={"spreadsheet_id": _SHEET, "range": "KPI!A1:B1", "values": [["a", "b"]]},
        handlers=handlers,
    )
    execute_capability(
        "sheets.append",
        principal=_OWNER,
        args={"spreadsheet_id": _SHEET, "range": "KPI!A2:B2", "values": [["c", "d"]]},
        handlers=handlers,
    )
    assert [url.rsplit("/", 1)[-1] for url, _ in requests] == [
        COMPOSIO_VALUES_GET_TOOL,
        COMPOSIO_VALUES_UPDATE_TOOL,
        COMPOSIO_VALUES_APPEND_TOOL,
    ]
    assert all(body["arguments"]["spreadsheetId"] == _SHEET for _, body in requests)
    assert requests[1][1]["arguments"]["valueInputOption"] == "RAW"
    assert requests[2][1]["arguments"]["valueInputOption"] == "RAW"


def test_owner_sheets_outside_allowlist_and_bad_values_do_not_call_http() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="allowlisted"):
        port.read_values(spreadsheet_id="sheet-other", a1_range="KPI!A1")
    with pytest.raises(ValueError, match="formula"):
        port.update_values(spreadsheet_id=_SHEET, a1_range="KPI!A1", values=[["=SUM(A:A)"]])
    with pytest.raises(ValueError, match="must not be empty"):
        port.append_values(spreadsheet_id=_SHEET, a1_range="KPI!A1", values=[["   "]])
    with pytest.raises(ValueError, match="bounded A1"):
        port.append_values(spreadsheet_id=_SHEET, a1_range="KPI", values=[["x"]])
    assert called is False


def test_owner_sheets_normalization_preserves_internal_spaces_in_nonempty_cells() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    port.append_values(spreadsheet_id=_SHEET, a1_range="KPI!A1", values=[["x  y"]])
    assert requests[0]["arguments"]["values"] == [["x  y"]]


@pytest.mark.parametrize("a1_range", ["A1:XFD999999", "Z99:A1", "KPI!A1:K21"])
def test_owner_sheets_oversized_or_reversed_range_never_calls_http(a1_range: str) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"successful": True, "data": {"values": []}})

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError):
        port.read_values(spreadsheet_id=_SHEET, a1_range=a1_range)
    with pytest.raises(ValueError):
        port.append_values(spreadsheet_id=_SHEET, a1_range=a1_range, values=[["x"]])
    assert called is False


def test_owner_sheets_values_must_fit_target_range_before_http() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="target A1"):
        port.update_values(spreadsheet_id=_SHEET, a1_range="KPI!A1:B1", values=[["1", "2", "3"]])
    assert called is False


def test_owner_sheets_empty_handler_allowlist_denies_fake_port() -> None:
    with pytest.raises(Exception, match="allowlisted"):
        execute_capability(
            "sheets.read",
            principal=_OWNER,
            args={"spreadsheet_id": _SHEET, "range": "KPI!A1"},
            handlers=sheets_handlers(FakeSheetsPort()),
        )


@pytest.mark.parametrize("a1_range", ["KPI!A1:K21", "Z99:A1"])
def test_owner_sheets_handler_bounds_fake_port_before_port_call(a1_range: str) -> None:
    port = FakeSheetsPort()
    with pytest.raises(Exception, match="range"):
        execute_capability(
            "sheets.append",
            principal=_OWNER,
            args={"spreadsheet_id": _SHEET, "range": a1_range, "values": [["x"]]},
            handlers=sheets_handlers(port, allowed_spreadsheet_ids=frozenset({_SHEET})),
        )
    assert port.owner_operations == []


def test_owner_sheets_read_allows_negative_data_and_raw_write_allows_negative_value() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"successful": True, "data": {"values": [["-12", "=stored"]]}}
            )
        )
    )
    port = _port(client)
    assert port.read_values(spreadsheet_id=_SHEET, a1_range="KPI!A1:B1") == [["-12", "=stored"]]
    with pytest.raises(ValueError, match="formula"):
        port.update_values(spreadsheet_id=_SHEET, a1_range="KPI!A1", values=[["=no"]])


def test_owner_sheets_policy_denies_client_and_kill_switch_before_fake_write() -> None:
    port = FakeSheetsPort()
    args = {"spreadsheet_id": _SHEET, "range": "KPI!A1", "values": [["x"]]}
    with pytest.raises(Exception):
        execute_capability(
            "sheets.update",
            principal=Principal.client(source="web"),
            args=args,
            handlers=sheets_handlers(port),
        )
    with pytest.raises(Exception):
        execute_capability(
            "sheets.append",
            principal=_OWNER,
            args=args,
            handlers=sheets_handlers(port),
            kill_switch=True,
        )
    assert port.owner_operations == []


def test_owner_sheets_http_auth_is_classified_without_credentials() -> None:
    port = _port(httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(401))))
    with pytest.raises(AdapterHttpError) as error:
        port.read_values(spreadsheet_id=_SHEET, a1_range="KPI!A1")
    assert error.value.tool_status() == "unauthorized"
