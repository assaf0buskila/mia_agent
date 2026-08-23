import json

import httpx
import pytest
from app.core.config import Settings
from app.domain.tools import AdapterHttpError
from app.integrations.ga4 import (
    COMPOSIO_GA4_VERSION,
    COMPOSIO_LIST_CONVERSION_EVENTS_TOOL,
    COMPOSIO_PIVOT_REPORT_TOOL,
    ComposioGa4Port,
    DisabledGa4Port,
    FakeGa4Port,
    Ga4PivotRow,
    build_ga4_port,
    normalize_ga4_property_id,
    pick_ga4_property,
)


def test_normalize_ga4_property_id() -> None:
    assert normalize_ga4_property_id("properties/123456789") == "properties/123456789"
    assert normalize_ga4_property_id("123456789") == "properties/123456789"
    assert normalize_ga4_property_id("bad/id") is None
    assert normalize_ga4_property_id("") is None


def test_fake_returns_rows_disabled_empty() -> None:
    row = Ga4PivotRow(landing_page="/", sessions="10", engaged_sessions="4")
    fake = FakeGa4Port(pivot_rows=[row], conversion_events=["generate_lead"])
    disabled = DisabledGa4Port()
    assert fake.run_pivot_report(start_date="2026-01-01", end_date="2026-01-28") == [row]
    assert fake.list_conversion_events() == ["generate_lead"]
    assert disabled.run_pivot_report(start_date="a", end_date="b") == []


def test_build_ga4_port_live_when_credentials_and_property_set() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        ga4_property_id="properties/999",
    )
    assert isinstance(build_ga4_port(settings), ComposioGa4Port)


def test_build_ga4_port_live_without_property_id() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        ga4_property_id="",
    )
    assert isinstance(build_ga4_port(settings), ComposioGa4Port)


def test_build_ga4_port_disabled_on_bad_property() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        ga4_property_id="not-valid",
    )
    assert isinstance(build_ga4_port(settings), DisabledGa4Port)


def test_composio_ga4_http_401_raises() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    client = httpx.Client(transport=transport)
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.run_pivot_report(start_date="28daysAgo", end_date="yesterday")
    assert exc_info.value.status_code == 401


def test_composio_pivot_request_shape() -> None:
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
                            "dimensionValues": [{"value": "/"}, {"value": "google"}],
                            "metricValues": [{"value": "12"}, {"value": "5"}],
                        }
                    ]
                },
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-abc",
        property_id="properties/42",
        client=client,
    )
    rows = port.run_pivot_report(start_date="28daysAgo", end_date="yesterday")
    assert len(rows) == 1
    assert rows[0].sessions == "12"
    assert rows[0].engaged_sessions == "5"
    assert str(captured["url"]).endswith(f"/{COMPOSIO_PIVOT_REPORT_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["version"] == COMPOSIO_GA4_VERSION
    args = body["arguments"]
    assert isinstance(args, dict)
    assert args["property"] == "properties/42"
    assert args["pivots"] == [
        {"fieldNames": ["landingPage"], "limit": 10},
        {"fieldNames": ["sessionSource"], "limit": 10},
    ]


def test_composio_list_conversion_events_url() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"conversionEvents": [{"eventName": "generate_lead"}]},
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-abc",
        property_id="properties/42",
        client=client,
    )
    events = port.list_conversion_events()
    assert events == ["generate_lead"]
    assert str(captured["url"]).endswith(f"/{COMPOSIO_LIST_CONVERSION_EVENTS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    args = body["arguments"]
    assert isinstance(args, dict)
    assert args["parent"] == "properties/42"


def test_pick_ga4_property_prefers_assafweb() -> None:
    summaries = [
        ("properties/1", "Cafe Ana"),
        ("properties/2", "AssafWeb"),
    ]
    assert pick_ga4_property(summaries) == "properties/2"
    assert pick_ga4_property(summaries, preferred="123") == "properties/123"
    assert pick_ga4_property([]) is None
    assert pick_ga4_property(
        [("properties/1", "Cafe Ana"), ("properties/3", "Mochi")]
    ) is None
    assert pick_ga4_property([("properties/9", "Only")]) == "properties/9"
