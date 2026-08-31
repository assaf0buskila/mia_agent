import json

import httpx
import pytest
from app.capabilities.policy import execute_capability
from app.capabilities.sheets import sheets_handlers
from app.capabilities.types import Principal
from app.domain.tools import AdapterHttpError
from app.integrations.sheets import (
    COMPOSIO_ADD_SHEET_TOOL,
    COMPOSIO_GET_SHEET_NAMES_TOOL,
    COMPOSIO_VALUES_APPEND_TOOL,
    COMPOSIO_VALUES_GET_TOOL,
    COMPOSIO_VALUES_UPDATE_TOOL,
    CRM_WORKSPACE_SCHEMA_RANGE,
    CRM_WORKSPACE_SCHEMA_VERSION,
    CRM_WORKSPACE_TABS,
    LEADS_SHEET_NAME,
    ComposioSheetsPort,
    FakeSheetsPort,
    normalize_owner_spreadsheet_id,
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


def test_owner_sheets_pasted_google_url_uses_allowlisted_id_and_bounded_preview() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"successful": True, "data": {"values": [["ok"]]}})

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    handlers = sheets_handlers(port, allowed_spreadsheet_ids=frozenset({_SHEET}))
    url = f"https://docs.google.com/spreadsheets/d/{_SHEET}/edit?gid=123#gid=123"
    assert execute_capability(
        "sheets.read",
        principal=_OWNER,
        args={"spreadsheet_id": url, "range": None},
        handlers=handlers,
    ) == {"count": 1, "rows": [["ok"]]}
    assert requests[0]["arguments"] == {"spreadsheetId": _SHEET, "range": "A1:J20"}


def test_owner_sheets_lists_tabs_only_inside_the_allowlisted_sheet() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            json={"successful": True, "data": {"sheetNames": ["01 Leads", "10 Mia Activity"]}},
        )

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    handlers = sheets_handlers(port, allowed_spreadsheet_ids=frozenset({_SHEET}))
    url = f"https://docs.google.com/spreadsheets/d/{_SHEET}/edit"
    assert execute_capability(
        "sheets.list_tabs",
        principal=_OWNER,
        args={"spreadsheet_id": url},
        handlers=handlers,
    ) == {"count": 2, "tabs": ["01 Leads", "10 Mia Activity"]}
    assert requests[0][0].endswith(COMPOSIO_GET_SHEET_NAMES_TOOL)
    assert requests[0][1] == {
        "user_id": "user-test",
        "version": "20260826_00",
        "arguments": {"spreadsheetId": _SHEET},
    }


def test_configured_mia_sheet_initializes_fixed_crm_workspace_idempotently() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tool = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content)
        requests.append((tool, body))
        if tool == COMPOSIO_GET_SHEET_NAMES_TOOL:
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {"sheetNames": [LEADS_SHEET_NAME, "10 Mia Activity"]},
                },
            )
        if tool == COMPOSIO_VALUES_GET_TOOL:
            a1_range = body["arguments"]["range"]
            if a1_range == CRM_WORKSPACE_SCHEMA_RANGE:
                return httpx.Response(200, json={"successful": True, "data": {"values": []}})
            sheet_name = a1_range.split("!", 1)[0]
            headers = next(
                headers for name, headers in CRM_WORKSPACE_TABS if name == sheet_name
            )
            return httpx.Response(
                200, json={"successful": True, "data": {"values": [headers]}}
            )
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-test",
        spreadsheet_id=_SHEET,
        allowed_spreadsheet_ids=frozenset({_SHEET, "sheet-other"}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    port.ensure_crm_workspace()
    first_call_count = len(requests)
    port.ensure_crm_workspace()

    assert len(requests) == first_call_count
    assert requests[0][0] == COMPOSIO_GET_SHEET_NAMES_TOOL
    add_requests = [body for tool, body in requests if tool == COMPOSIO_ADD_SHEET_TOOL]
    assert len(add_requests) == 6
    assert all(body["arguments"]["spreadsheetId"] == _SHEET for body in add_requests)
    assert not any(
        body["arguments"]["properties"]["title"] == LEADS_SHEET_NAME for body in add_requests
    )
    update_requests = [body for tool, body in requests if tool == COMPOSIO_VALUES_UPDATE_TOOL]
    assert len(update_requests) == 7
    assert not any(
        body["arguments"]["range"].startswith(f"{LEADS_SHEET_NAME}!")
        for body in update_requests
    )
    assert all(body["arguments"]["spreadsheetId"] == _SHEET for body in update_requests)
    marker = next(
        body
        for body in update_requests
        if body["arguments"]["range"] == CRM_WORKSPACE_SCHEMA_RANGE
    )
    assert marker["arguments"]["values"] == [[CRM_WORKSPACE_SCHEMA_VERSION]]


def test_crm_schema_marker_skips_repair_only_after_every_header_is_current() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tool = str(request.url).rsplit("/", 1)[-1]
        requests.append(tool)
        if tool == COMPOSIO_GET_SHEET_NAMES_TOOL:
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {"sheetNames": [name for name, _headers in CRM_WORKSPACE_TABS]},
                },
            )
        if tool == COMPOSIO_VALUES_GET_TOOL:
            request_body = json.loads(request.content)
            a1_range = request_body["arguments"]["range"]
            if a1_range != CRM_WORKSPACE_SCHEMA_RANGE:
                sheet_name = a1_range.split("!", 1)[0]
                return httpx.Response(
                    200,
                    json={
                        "successful": True,
                        "data": {
                            "values": [
                                headers
                                for name, headers in CRM_WORKSPACE_TABS
                                if name == sheet_name
                            ]
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {"values": [[CRM_WORKSPACE_SCHEMA_VERSION]]},
                },
            )
        raise AssertionError("schema marker should prevent every write")

    port = ComposioSheetsPort(
        api_key="cmp-marker-test",
        user_id="user-marker-test",
        spreadsheet_id="sheet-marker-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    port.ensure_crm_workspace()
    assert requests.count(COMPOSIO_GET_SHEET_NAMES_TOOL) == 1
    assert requests.count(COMPOSIO_VALUES_GET_TOOL) == len(CRM_WORKSPACE_TABS) + 1
    assert COMPOSIO_ADD_SHEET_TOOL not in requests
    assert COMPOSIO_VALUES_UPDATE_TOOL not in requests


def test_crm_schema_marker_repairs_a_damaged_fixed_header() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tool = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content)
        requests.append((tool, body))
        if tool == COMPOSIO_GET_SHEET_NAMES_TOOL:
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {"sheetNames": [name for name, _headers in CRM_WORKSPACE_TABS]},
                },
            )
        if tool == COMPOSIO_VALUES_GET_TOOL:
            a1_range = body["arguments"]["range"]
            if a1_range == CRM_WORKSPACE_SCHEMA_RANGE:
                values = [[CRM_WORKSPACE_SCHEMA_VERSION]]
            else:
                sheet_name = a1_range.split("!", 1)[0]
                values = next(
                    [headers]
                    for name, headers in CRM_WORKSPACE_TABS
                    if name == sheet_name
                )
                if sheet_name == LEADS_SHEET_NAME:
                    values = [["Damaged header"]]
            return httpx.Response(200, json={"successful": True, "data": {"values": values}})
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = ComposioSheetsPort(
        api_key="cmp-repair-test",
        user_id="user-repair-test",
        spreadsheet_id="sheet-repair-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    port.ensure_crm_workspace()

    updates = [body for tool, body in requests if tool == COMPOSIO_VALUES_UPDATE_TOOL]
    assert len(updates) == 2
    assert {body["arguments"]["range"] for body in updates} == {
        f"{LEADS_SHEET_NAME}!A1:F1",
        CRM_WORKSPACE_SCHEMA_RANGE,
    }


def test_crm_workspace_without_a_configured_sheet_is_a_noop() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-test",
        spreadsheet_id="",
        allowed_spreadsheet_ids=frozenset({_SHEET}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    port.ensure_crm_workspace()
    assert called is False


def test_owner_sheets_tab_discovery_rejects_an_unallowlisted_reference_before_http() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"successful": True, "data": {"sheetNames": []}})

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception, match="allowlisted"):
        execute_capability(
            "sheets.list_tabs",
            principal=_OWNER,
            args={"spreadsheet_id": "another-sheet"},
            handlers=sheets_handlers(port, allowed_spreadsheet_ids=frozenset({_SHEET})),
        )
    assert called is False


def test_owner_sheets_url_convenience_never_widens_writes() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = _port(httpx.Client(transport=httpx.MockTransport(handler)))
    url = f"https://docs.google.com/spreadsheets/d/{_SHEET}/edit"
    with pytest.raises(ValueError, match="allowlisted"):
        port.update_values(spreadsheet_id=url, a1_range="A1", values=[["x"]])
    with pytest.raises(ValueError, match="allowlisted"):
        port.append_values(spreadsheet_id=url, a1_range="A1", values=[["x"]])
    assert called is False


@pytest.mark.parametrize(
    "reference",
    [
        "https://evil.example/spreadsheets/d/sheet-allowed/edit",
        "https://docs.google.com/document/d/sheet-allowed/edit",
        "http://docs.google.com/spreadsheets/d/sheet-allowed/edit",
    ],
)
def test_owner_sheets_rejects_non_google_sheet_urls(reference: str) -> None:
    assert normalize_owner_spreadsheet_id(reference) == ""


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
