import json

import httpx
import pytest
from app.core.config import Settings
from app.domain.tools import AdapterHttpError, AdapterResponseError, AdapterSchemaError
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


def test_composio_ga4_schema_and_execution_failures_are_not_empty() -> None:
    schema_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"successful": True, "data": []})
        )
    )
    schema_port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=schema_client,
    )
    with pytest.raises(AdapterSchemaError) as schema_error:
        schema_port.run_pivot_report(start_date="28daysAgo", end_date="yesterday")
    assert schema_error.value.tool_status() == "malformed"

    failed_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"successful": False, "data": {}})
        )
    )
    failed_port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=failed_client,
    )
    with pytest.raises(AdapterResponseError) as response_error:
        failed_port.run_pivot_report(start_date="28daysAgo", end_date="yesterday")
    assert response_error.value.tool_status() == "error"


def test_composio_ga4_well_formed_no_data_returns_empty() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {
                        "dimensionHeaders": [{"name": "landingPage"}],
                        "metricHeaders": [{"name": "activeUsers", "type": "TYPE_INTEGER"}],
                        "pivotHeaders": [],
                        "metadata": {"currencyCode": "ILS", "timeZone": "Asia/Jerusalem"},
                    },
                },
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    assert port.run_pivot_report(start_date="2026-08-29", end_date="2026-08-29") == []


def test_composio_ga4_live_rowless_pivot_headers_return_empty() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {
                        "dimensionHeaders": [
                            {"name": "landingPage"},
                            {"name": "sessionSource"},
                        ],
                        "metricHeaders": [
                            {"name": "activeUsers", "type": "TYPE_INTEGER"},
                            {"name": "sessions", "type": "TYPE_INTEGER"},
                            {"name": "conversions", "type": "TYPE_FLOAT"},
                            {"name": "engagedSessions", "type": "TYPE_INTEGER"},
                        ],
                        "pivotHeaders": [{}, {}],
                        "metadata": {
                            "currencyCode": "ILS",
                            "timeZone": "Asia/Jerusalem",
                        },
                    },
                },
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    assert port.run_pivot_report(start_date="2026-08-03", end_date="2026-08-30") == []


def test_composio_ga4_rowless_nonempty_pivot_headers_fail_closed() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {
                        "dimensionHeaders": [
                            {"name": "landingPage"},
                            {"name": "sessionSource"},
                        ],
                        "metricHeaders": [
                            {"name": "activeUsers", "type": "TYPE_INTEGER"}
                        ],
                        "pivotHeaders": [
                            {
                                "pivotDimensionHeaders": [
                                    {"dimensionValues": [{"value": "/"}]}
                                ],
                                "rowCount": 1,
                            }
                        ],
                        "metadata": {
                            "currencyCode": "ILS",
                            "timeZone": "Asia/Jerusalem",
                        },
                    },
                },
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    with pytest.raises(AdapterSchemaError):
        port.run_pivot_report(start_date="2026-08-29", end_date="2026-08-29")


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("dimensionHeaders", [None]),
        ("dimensionHeaders", [{}]),
        ("dimensionHeaders", [{"name": " "}]),
        ("dimensionHeaders", [{"name": 7}]),
        ("metricHeaders", [None]),
        ("metricHeaders", [{}]),
        ("metricHeaders", [{"name": " ", "type": "TYPE_INTEGER"}]),
        ("metricHeaders", [{"name": "sessions"}]),
        ("metricHeaders", [{"name": "sessions", "type": "TYPE_UNKNOWN"}]),
        ("metricHeaders", [{"name": "sessions", "type": 7}]),
        ("metricHeaders", [{"name": "sessions", "type": {}}]),
        ("pivotHeaders", [None]),
        ("pivotHeaders", [{}]),
        ("pivotHeaders", [{}, {"unexpected": 1}]),
        ("pivotHeaders", [{}, {}, {}]),
        (
            "pivotHeaders",
            [{"pivotDimensionHeaders": None, "rowCount": 0}],
        ),
        (
            "pivotHeaders",
            [{"pivotDimensionHeaders": [None], "rowCount": 1}],
        ),
        (
            "pivotHeaders",
            [
                {
                    "pivotDimensionHeaders": [{"dimensionValues": None}],
                    "rowCount": 1,
                }
            ],
        ),
        (
            "pivotHeaders",
            [
                {
                    "pivotDimensionHeaders": [
                        {"dimensionValues": [{"value": 7}]}
                    ],
                    "rowCount": 1,
                }
            ],
        ),
        (
            "pivotHeaders",
            [{"pivotDimensionHeaders": [], "rowCount": True}],
        ),
        (
            "pivotHeaders",
            [{"pivotDimensionHeaders": [], "rowCount": -1}],
        ),
        (
            "pivotHeaders",
            [{"pivotDimensionHeaders": [], "rowCount": 1}],
        ),
        (
            "pivotHeaders",
            [
                {
                    "pivotDimensionHeaders": [{"dimensionValues": []}],
                    "rowCount": 1,
                }
            ],
        ),
    ],
)
def test_composio_ga4_malformed_no_data_headers_fail_closed(
    field: str, bad_value: object
) -> None:
    data: dict[str, object] = {
        "dimensionHeaders": [{"name": "landingPage"}],
        "metricHeaders": [{"name": "activeUsers", "type": "TYPE_INTEGER"}],
        "pivotHeaders": [],
        "metadata": {"currencyCode": "ILS", "timeZone": "Asia/Jerusalem"},
    }
    data[field] = bad_value
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"successful": True, "data": data},
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    with pytest.raises(AdapterSchemaError):
        port.run_pivot_report(start_date="2026-08-29", end_date="2026-08-29")


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("dimensionHeaders", [None]),
        ("metricHeaders", [None]),
        ("pivotHeaders", [None]),
        ("pivotHeaders", [{}, {}]),
        ("metadata", None),
    ],
)
def test_composio_ga4_populated_malformed_headers_fail_closed(
    field: str, bad_value: object
) -> None:
    data: dict[str, object] = {
        "rows": [
            {
                "dimensionValues": [{"value": "/"}, {"value": "google"}],
                "metricValues": [{"value": "12"}, {"value": "5"}],
            }
        ],
        "dimensionHeaders": [{"name": "landingPage"}],
        "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
        "pivotHeaders": [],
        "metadata": {"currencyCode": "ILS", "timeZone": "Asia/Jerusalem"},
    }
    data[field] = bad_value
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"successful": True, "data": data},
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    with pytest.raises(AdapterSchemaError):
        port.run_pivot_report(start_date="2026-08-01", end_date="2026-08-28")


@pytest.mark.parametrize("bad_rows", [None, {}, "not-a-list"])
def test_composio_ga4_present_malformed_rows_still_fail(bad_rows: object) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {
                        "rows": bad_rows,
                        "dimensionHeaders": [],
                        "metricHeaders": [],
                        "pivotHeaders": [],
                        "metadata": {},
                    },
                },
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    with pytest.raises(AdapterSchemaError):
        port.run_pivot_report(start_date="2026-08-29", end_date="2026-08-29")


@pytest.mark.parametrize(
    "bad_row",
    [
        None,
        {"metricValues": [{"value": "1"}, {"value": "2"}]},
        {
            "dimensionValues": [{"value": "/"}, {"value": "google"}],
        },
        {
            "dimensionValues": "not-a-list",
            "metricValues": [{"value": "1"}, {"value": "2"}],
        },
        {
            "dimensionValues": [{"value": 7}, {"value": "google"}],
            "metricValues": [{"value": "1"}, {"value": "2"}],
        },
        {
            "dimensionValues": [{"value": "/"}, {"value": "google"}],
            "metricValues": [{"value": True}, {"value": "2"}],
        },
        {
            "dimensionValues": [{"value": "/"}, {"value": "google"}],
            "metricValues": [{"value": "1"}, {"value": {}}],
        },
        {
            "dimensionValues": [],
            "dimensions": [{"value": "/"}, {"value": "google"}],
            "metricValues": [{"value": "1"}, {"value": "2"}],
        },
        {
            "dimensionValues": [{"value": "/"}, {"value": "google"}],
            "metricValues": [],
            "metrics": [{"value": "1"}, {"value": "2"}],
        },
    ],
)
def test_composio_ga4_malformed_row_elements_fail_closed(bad_row: object) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"successful": True, "data": {"rows": [bad_row]}},
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    with pytest.raises(AdapterSchemaError):
        port.run_pivot_report(start_date="2026-08-01", end_date="2026-08-28")


def test_composio_ga4_historical_two_metric_row_stays_mapped() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {
                        "rows": [
                            {
                                "dimensionValues": [
                                    {"value": "/"},
                                    {"value": "google"},
                                ],
                                "metricValues": [
                                    {"value": "12"},
                                    {"value": "5"},
                                ],
                            }
                        ]
                    },
                },
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    rows = port.run_pivot_report(start_date="2026-08-01", end_date="2026-08-28")

    assert len(rows) == 1
    assert rows[0].sessions == "12"
    assert rows[0].engaged_sessions == "5"
    assert rows[0].users is None
    assert rows[0].conversions is None


@pytest.mark.parametrize(
    "data_patch",
    [
        {
            "dimensionHeaders": [
                {"name": "sessionSource"},
                {"name": "landingPage"},
            ]
        },
        {
            "dimensionHeaders": [
                {"name": "landingPage"},
                {"name": "sessionSource"},
                {"name": "country"},
            ]
        },
        {
            "metricHeaders": [
                {"name": "sessions", "type": "TYPE_INTEGER"},
                {"name": "activeUsers", "type": "TYPE_INTEGER"},
                {"name": "conversions", "type": "TYPE_FLOAT"},
                {"name": "engagedSessions", "type": "TYPE_INTEGER"},
            ]
        },
        {
            "metricHeaders": [
                {"name": "activeUsers", "type": "TYPE_INTEGER"},
                {"name": "sessions", "type": "TYPE_INTEGER"},
                {"name": "conversions", "type": "TYPE_FLOAT"},
                {"name": "engagedSessions", "type": "TYPE_INTEGER"},
                {"name": "eventCount", "type": "TYPE_INTEGER"},
            ]
        },
    ],
)
def test_composio_ga4_semantic_header_drift_fails_closed(
    data_patch: dict[str, object],
) -> None:
    data: dict[str, object] = {
        "rows": [
            {
                "dimensionValues": [{"value": "/pricing"}, {"value": "google"}],
                "metricValues": [
                    {"value": "10"},
                    {"value": "12"},
                    {"value": "1"},
                    {"value": "5"},
                ],
            }
        ],
        "dimensionHeaders": [
            {"name": "landingPage"},
            {"name": "sessionSource"},
        ],
        "metricHeaders": [
            {"name": "activeUsers", "type": "TYPE_INTEGER"},
            {"name": "sessions", "type": "TYPE_INTEGER"},
            {"name": "conversions", "type": "TYPE_FLOAT"},
            {"name": "engagedSessions", "type": "TYPE_INTEGER"},
        ],
        "pivotHeaders": [],
        "metadata": {},
    }
    data.update(data_patch)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"successful": True, "data": data}
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    with pytest.raises(AdapterSchemaError):
        port.run_pivot_report(start_date="2026-08-01", end_date="2026-08-28")


def test_composio_ga4_extra_dimension_value_fails_closed() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {
                        "rows": [
                            {
                                "dimensionValues": [
                                    {"value": "/pricing"},
                                    {"value": "google"},
                                    {"value": "IL"},
                                ],
                                "metricValues": [
                                    {"value": "10"},
                                    {"value": "12"},
                                    {"value": "1"},
                                    {"value": "5"},
                                ],
                            }
                        ]
                    },
                },
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    with pytest.raises(AdapterSchemaError):
        port.run_pivot_report(start_date="2026-08-01", end_date="2026-08-28")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            "dimensionHeaders": [
                {"name": "landingPage"},
                {"name": "sessionSource"},
            ]
        },
        {
            "metricHeaders": [
                {"name": "activeUsers", "type": "TYPE_INTEGER"},
                {"name": "sessions", "type": "TYPE_INTEGER"},
                {"name": "conversions", "type": "TYPE_FLOAT"},
                {"name": "engagedSessions", "type": "TYPE_INTEGER"},
            ]
        },
    ],
)
def test_composio_ga4_four_metric_rows_require_both_header_sets(
    headers: dict[str, object],
) -> None:
    data: dict[str, object] = {
        "rows": [
            {
                "dimensionValues": [{"value": "/"}, {"value": "google"}],
                "metricValues": [
                    {"value": "10"},
                    {"value": "12"},
                    {"value": "1"},
                    {"value": "5"},
                ],
            }
        ],
        **headers,
    }
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"successful": True, "data": data}
            )
        )
    )
    port = ComposioGa4Port(
        api_key="cmp-test",
        user_id="user-123",
        property_id="properties/1",
        client=client,
    )

    with pytest.raises(AdapterSchemaError):
        port.run_pivot_report(start_date="2026-08-01", end_date="2026-08-28")


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
                            "metricValues": [
                                {"value": "10"},
                                {"value": "12"},
                                {"value": "1"},
                                {"value": "5"},
                            ],
                        }
                    ],
                    "dimensionHeaders": [
                        {"name": "landingPage"},
                        {"name": "sessionSource"},
                    ],
                    "metricHeaders": [
                        {"name": "activeUsers", "type": "TYPE_INTEGER"},
                        {"name": "sessions", "type": "TYPE_INTEGER"},
                        {"name": "conversions", "type": "TYPE_FLOAT"},
                        {"name": "engagedSessions", "type": "TYPE_INTEGER"},
                    ],
                    "pivotHeaders": [
                        {
                            "pivotDimensionHeaders": [
                                {"dimensionValues": [{"value": "/"}]}
                            ],
                            "rowCount": 1,
                        }
                    ],
                    "metadata": {
                        "currencyCode": "ILS",
                        "timeZone": "Asia/Jerusalem",
                    },
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
    assert rows[0].users == "10"
    assert rows[0].sessions == "12"
    assert rows[0].conversions == "1"
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
