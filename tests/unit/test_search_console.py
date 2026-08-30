import json

import httpx
import pytest
from app.core.config import Settings
from app.domain.tools import AdapterHttpError, AdapterResponseError, AdapterSchemaError
from app.integrations.search_console import (
    COMPOSIO_GSC_VERSION,
    COMPOSIO_INSPECT_URL_TOOL,
    COMPOSIO_LIST_SITES_TOOL,
    COMPOSIO_SEARCH_ANALYTICS_TOOL,
    ComposioSearchConsolePort,
    DisabledSearchConsolePort,
    FakeSearchConsolePort,
    SearchAnalyticsRow,
    UrlInspectionResult,
    build_search_console_port,
    pick_gsc_site,
)


def test_fake_returns_rows_disabled_returns_empty() -> None:
    row = SearchAnalyticsRow(page="/", clicks="3", impressions="100", ctr="0.03")
    fake = FakeSearchConsolePort(analytics_rows=[row])
    disabled = DisabledSearchConsolePort()
    assert fake.query_search_analytics(
        start_date="2026-01-01", end_date="2026-01-28", dimensions=["page"]
    ) == [row]
    assert disabled.query_search_analytics(
        start_date="2026-01-01", end_date="2026-01-28", dimensions=["page"]
    ) == []


def test_build_search_console_port_live_when_credentials_set() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        gsc_site_url="https://www.assafweb.com/",
    )
    port = build_search_console_port(settings)
    assert isinstance(port, ComposioSearchConsolePort)


def test_build_search_console_port_live_without_site_url() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        gsc_site_url="",
    )
    assert isinstance(build_search_console_port(settings), ComposioSearchConsolePort)


@pytest.mark.parametrize(
    "api_key,user_id",
    [
        ("", ""),
        ("cmp", ""),
        ("", "user"),
    ],
)
def test_build_search_console_port_disabled(api_key: str, user_id: str) -> None:
    settings = Settings(
        composio_api_key=api_key,
        composio_user_id=user_id,
        gsc_site_url="https://www.assafweb.com/",
    )
    assert isinstance(build_search_console_port(settings), DisabledSearchConsolePort)


def test_composio_search_console_http_401_raises() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    client = httpx.Client(transport=transport)
    port = ComposioSearchConsolePort(
        api_key="cmp-test",
        user_id="user-123",
        site_url="https://www.assafweb.com/",
        client=client,
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.list_sites()
    assert exc_info.value.status_code == 401


def test_composio_search_console_schema_and_execution_failures_are_not_empty() -> None:
    schema_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"successful": True, "data": []})
        )
    )
    schema_port = ComposioSearchConsolePort(
        api_key="cmp-test",
        user_id="user-123",
        site_url="https://www.assafweb.com/",
        client=schema_client,
    )
    with pytest.raises(AdapterSchemaError) as schema_error:
        schema_port.query_search_analytics(
            start_date="2026-01-01", end_date="2026-01-28", dimensions=["page"]
        )
    assert schema_error.value.tool_status() == "malformed"

    failed_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"successful": False, "data": {}})
        )
    )
    failed_port = ComposioSearchConsolePort(
        api_key="cmp-test",
        user_id="user-123",
        site_url="https://www.assafweb.com/",
        client=failed_client,
    )
    with pytest.raises(AdapterResponseError) as response_error:
        failed_port.query_search_analytics(
            start_date="2026-01-01", end_date="2026-01-28", dimensions=["page"]
        )
    assert response_error.value.tool_status() == "error"


def test_composio_search_console_analytics_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {
                    "rows": [
                        {
                            "keys": ["/"],
                            "clicks": 5,
                            "impressions": 200,
                            "ctr": 0.025,
                            "position": 8.1,
                        }
                    ]
                },
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioSearchConsolePort(
        api_key="cmp-test",
        user_id="user-abc",
        site_url="https://www.assafweb.com/",
        client=client,
    )
    rows = port.query_search_analytics(
        start_date="2026-01-01",
        end_date="2026-01-28",
        dimensions=["page"],
    )
    assert len(rows) == 1
    assert rows[0].clicks == "5"
    assert rows[0].impressions == "200"
    assert str(captured["url"]).endswith(f"/{COMPOSIO_SEARCH_ANALYTICS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["version"] == COMPOSIO_GSC_VERSION
    args = body["arguments"]
    assert isinstance(args, dict)
    assert args["site_url"] == "https://www.assafweb.com/"
    assert args["start_date"] == "2026-01-01"
    assert args["end_date"] == "2026-01-28"


def test_composio_list_sites_execute_url() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "data": {"siteEntry": [{"siteUrl": "https://www.assafweb.com/"}]},
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioSearchConsolePort(
        api_key="cmp-test",
        user_id="user-abc",
        site_url="https://www.assafweb.com/",
        client=client,
    )
    sites = port.list_sites()
    assert sites == ["https://www.assafweb.com/"]
    assert str(captured["url"]).endswith(f"/{COMPOSIO_LIST_SITES_TOOL}")


def test_composio_inspect_url_maps_status() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "inspectionResult": {
                        "indexStatusResult": {
                            "verdict": "PASS",
                            "coverageState": "Submitted and indexed",
                        }
                    }
                },
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioSearchConsolePort(
        api_key="cmp-test",
        user_id="user-123",
        site_url="https://www.assafweb.com/",
        client=client,
    )
    result = port.inspect_url("https://www.assafweb.com/")
    assert result == UrlInspectionResult(
        url="https://www.assafweb.com/",
        indexing_status="PASS",
        coverage_state="Submitted and indexed",
    )
    assert COMPOSIO_INSPECT_URL_TOOL.startswith("GOOGLE_SEARCH_CONSOLE")


def test_pick_gsc_site_prefers_assafweb() -> None:
    sites = [
        "sc-domain:cafe-ana.com",
        "sc-domain:assafweb.com",
        "https://mochi-israel.com/",
    ]
    assert pick_gsc_site(sites) == "sc-domain:assafweb.com"
    assert pick_gsc_site(sites, preferred="https://www.assafweb.com/") == (
        "https://www.assafweb.com/"
    )
    assert pick_gsc_site([]) == ""
    assert pick_gsc_site(["sc-domain:cafe-ana.com", "https://mochi-israel.com/"]) == ""
    assert pick_gsc_site(["https://only.example/"]) == "https://only.example/"
