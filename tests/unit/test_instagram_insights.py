import inspect
import json

import httpx
import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.content_insights import apply_content_insight_policy
from app.domain.events import Channel, build_attribution_event
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.instagram_insights import (
    ContentInsight,
    DisabledInstagramInsightsPort,
    FakeInstagramInsightsPort,
    GraphInstagramInsightsPort,
    InstagramInsightBudgetExceeded,
    build_instagram_insights_port,
    enrich_content_insights_ack,
    format_content_insights_line,
)
from app.integrations.sheets import FakeSheetsPort

OWNER_IG_CONTENT_PHONE = "972509990081"
OWNER_SHCNT_PHONE = "972509991301"
MEDIA_ID_1 = "17841400112233445566"
MEDIA_ID_2 = "17841400998877665544"
MEDIA_ID_ATTR = "99887766554433221100"

SAMPLE_ITEMS = [
    ContentInsight(
        media_id=MEDIA_ID_1,
        media_type="IMAGE",
        caption="Launch hook",
        timestamp="2026-09-01T10:00:00+0000",
        permalink="https://instagram.com/p/abc",
        account="assafweb",
        views="1200",
        reach="900",
        likes="45",
        comments="3",
        saved="12",
    ),
    ContentInsight(
        media_id=MEDIA_ID_2,
        media_type="REELS",
        caption="Reel hook",
        timestamp="2026-09-02T10:00:00+0000",
        permalink="https://instagram.com/p/def",
        account="assafweb",
        views="5000",
        reach="4200",
        likes="210",
    ),
]


def test_fake_returns_items_disabled_returns_empty() -> None:
    fake = FakeInstagramInsightsPort(SAMPLE_ITEMS)
    disabled = DisabledInstagramInsightsPort()
    assert fake.list_recent_insights() == SAMPLE_ITEMS
    assert disabled.list_recent_insights() == []


def test_graph_port_parses_media_and_insights_no_urls() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/media"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": MEDIA_ID_1,
                            "media_type": "IMAGE",
                            "caption": "Launch hook",
                            "timestamp": "2026-09-01T10:00:00+0000",
                            "permalink": "https://instagram.com/p/abc",
                            "media_url": "https://cdn.example/photo.jpg",
                        },
                        {"id": "bad-id", "media_type": "IMAGE"},
                        {"id": MEDIA_ID_2, "media_type": "STORY"},
                    ]
                },
            )
        if request.url.path.endswith("/insights"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"name": "views", "values": [{"value": 1200}]},
                        {"name": "reach", "total_value": {"value": 900}},
                        {"name": "likes", "values": [{"value": 45}]},
                        {"name": "comments", "values": [{"value": 3}]},
                        {"name": "saved", "values": [{"value": 12}]},
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = GraphInstagramInsightsPort(
        access_token="ig-token",
        account_id="123456789",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=client,
    )
    items = port.list_recent_insights(limit=5)
    assert len(items) == 1
    assert items[0].media_id == MEDIA_ID_1
    assert items[0].media_type == "IMAGE"
    assert items[0].views == "1200"
    assert items[0].reach == "900"
    assert items[0].likes == "45"
    assert items[0].comments == "3"
    assert items[0].saved == "12"
    serialized = json.dumps([item.model_dump() for item in items]).lower()
    assert items[0].caption == "Launch hook"
    assert items[0].timestamp == "2026-09-01T10:00:00+0000"
    assert items[0].permalink == "https://instagram.com/p/abc"
    assert "cdn.example" not in serialized
    assert "media_url" not in serialized
    assert any(
        "/media" in call and "caption" in call and "permalink" in call for call in calls
    )
    assert any("/media" in call for call in calls)
    assert any("/insights" in call for call in calls)
    assert all("access_token" not in call for call in calls)


def test_graph_port_media_list_401_raises_adapter_error() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": {"message": "unauthorized"}})
    )
    client = httpx.Client(transport=transport)
    port = GraphInstagramInsightsPort(
        access_token="ig-token",
        account_id="123456789",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=client,
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.list_recent_insights(limit=5)
    assert exc_info.value.status_code == 401


def test_enrich_content_insights_ack_http_401_unauthorized_ack_unchanged() -> None:
    class HttpErrorInsightsPort:
        def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]:
            del limit
            raise AdapterHttpError(401)

    decision = classify_owner_task("analyze instagram content")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_content_insights_ack(
        ack,
        HttpErrorInsightsPort(),
        store=None,
        kill_switch=False,
    )
    assert enriched == ack
    assert outcome.status == "unauthorized"
    assert outcome.result_count == 0
    assert "תוכן:" not in enriched


def test_enrich_reports_bounded_instagram_read_as_partial_not_empty() -> None:
    class BudgetLimitedInsightsPort:
        def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]:
            del limit
            raise InstagramInsightBudgetExceeded()

    enriched, outcome = enrich_content_insights_ack(
        "ack",
        BudgetLimitedInsightsPort(),
        store=None,
        kill_switch=False,
    )
    assert enriched == "ack"
    assert outcome.status == "partial"
    assert outcome.result_count == 0


def test_graph_port_insights_400_skips_media() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": MEDIA_ID_1, "media_type": "VIDEO"},
                        {"id": MEDIA_ID_2, "media_type": "VIDEO"},
                    ]
                },
            )
        if MEDIA_ID_1 in str(request.url):
            return httpx.Response(400, json={"error": {"message": "unsupported"}})
        if MEDIA_ID_2 in str(request.url):
            return httpx.Response(
                200,
                json={"data": [{"name": "views", "values": [{"value": 99}]}]},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = GraphInstagramInsightsPort(
        access_token="ig-token",
        account_id="123456789",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=client,
    )
    items = port.list_recent_insights(limit=5)
    assert len(items) == 2
    assert items[0].media_id == MEDIA_ID_1
    assert items[0].views is None
    assert items[1].media_id == MEDIA_ID_2
    assert items[1].views == "99"


def test_graph_port_retries_supported_metrics_individually_after_mixed_batch_rejection() -> None:
    requested_metrics: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(
                200, json={"data": [{"id": MEDIA_ID_1, "media_type": "IMAGE"}]}
            )
        if not request.url.path.endswith("/insights"):
            return httpx.Response(200, json={"username": "assafweb"})
        metric = str(request.url.params.get("metric") or "")
        requested_metrics.append(metric)
        if "," in metric:
            return httpx.Response(400, json={"error": {"message": "mixed metrics unsupported"}})
        values = {"reach": 900, "likes": 45, "comments": 3, "saved": 12}
        if metric == "views":
            return httpx.Response(400, json={"error": {"message": "unsupported for image"}})
        return httpx.Response(
            200, json={"data": [{"name": metric, "values": [{"value": values[metric]}]}]}
        )

    port = GraphInstagramInsightsPort(
        access_token="ig-token",
        account_id="123456789",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    items = port.list_recent_insights(limit=1)
    assert len(items) == 1
    assert items[0].views is None
    assert items[0].reach == "900"
    assert items[0].likes == "45"
    assert items[0].comments == "3"
    assert items[0].saved == "12"
    assert requested_metrics == [
        "views,reach,likes,comments,saved",
        "views",
        "reach",
        "likes",
        "comments",
        "saved",
    ]


@pytest.mark.parametrize("status_code", [401, 429, 503])
def test_graph_port_terminal_insight_failure_stops_without_metric_fallback(
    status_code: int,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/media"):
            return httpx.Response(
                200, json={"data": [{"id": MEDIA_ID_1, "media_type": "IMAGE"}]}
            )
        if not request.url.path.endswith("/insights"):
            return httpx.Response(200, json={"username": "assafweb"})
        return httpx.Response(status_code, json={"error": {"message": "provider down"}})

    port = GraphInstagramInsightsPort(
        access_token="ig-token",
        account_id="123456789",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.list_recent_insights(limit=1)
    assert exc_info.value.status_code == status_code
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        ({"message": "Invalid OAuth access token", "code": 190}, 401),
        ({"message": "Application request limit reached", "code": 4}, 429),
    ],
)
def test_graph_port_stops_mixed_metric_fallback_on_terminal_400_provider_error(
    error: dict[str, object], expected_status: int
) -> None:
    requested_metrics: list[str] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/media"):
            return httpx.Response(
                200, json={"data": [{"id": MEDIA_ID_1, "media_type": "IMAGE"}]}
            )
        if not request.url.path.endswith("/insights"):
            return httpx.Response(200, json={"username": "assafweb"})
        metric = str(request.url.params.get("metric") or "")
        requested_metrics.append(metric)
        if "," in metric:
            return httpx.Response(
                400, json={"error": {"message": "mixed metrics unsupported"}}
            )
        return httpx.Response(400, json={"error": error})

    port = GraphInstagramInsightsPort(
        access_token="ig-token",
        account_id="123456789",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.list_recent_insights(limit=1)
    assert exc_info.value.status_code == expected_status
    assert requested_metrics == ["views,reach,likes,comments,saved", "views"]
    assert calls == 4


def test_graph_port_does_not_retry_generic_metric_error_individually() -> None:
    requested_metrics: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(
                200, json={"data": [{"id": MEDIA_ID_1, "media_type": "IMAGE"}]}
            )
        if not request.url.path.endswith("/insights"):
            return httpx.Response(200, json={"username": "assafweb"})
        metric = str(request.url.params.get("metric") or "")
        requested_metrics.append(metric)
        return httpx.Response(400, json={"error": {"message": "unsupported metric"}})

    port = GraphInstagramInsightsPort(
        access_token="ig-token",
        account_id="123456789",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    items = port.list_recent_insights(limit=1)
    assert len(items) == 1
    assert items[0].media_id == MEDIA_ID_1
    assert items[0].views is None
    assert requested_metrics == ["views,reach,likes,comments,saved"]


def test_graph_port_enforces_total_audit_call_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.integrations.instagram_insights._insight_budget_for_limit",
        lambda _limit: 11,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/media"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": str(int(MEDIA_ID_1) + index), "media_type": "IMAGE"}
                        for index in range(5)
                    ]
                },
            )
        return httpx.Response(
            400, json={"error": {"message": "mixed metrics unsupported"}}
        )

    port = GraphInstagramInsightsPort(
        access_token="ig-token",
        account_id="123456789",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(InstagramInsightBudgetExceeded):
        port.list_recent_insights(limit=5)
    assert len(calls) == 11


def test_enrich_appends_hebrew_line_and_persists() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        decision = classify_owner_task("analyze instagram content")
        ack = ack_for_owner_task(decision)
        enriched, outcome = enrich_content_insights_ack(
            ack,
            FakeInstagramInsightsPort(SAMPLE_ITEMS),
            store,
            kill_switch=False,
        )
        db.commit()
        assert "תוכן: 2 פוסטים, לידים מתוכן 0." in enriched
        rows = [
            row
            for row in store.list_content_insights()
            if row.media_id in {MEDIA_ID_1, MEDIA_ID_2}
        ]
        assert len(rows) == 2
        first = next(row for row in rows if row.media_id == MEDIA_ID_1)
        assert first.lead_signals == 0
        assert outcome.status == "ok"
        assert outcome.result_count == 2
    finally:
        db.close()


def test_attribution_matching_ig_content_id_increments_lead_signals() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _customer_id, lead_id = store.open_channel_lead(
            channel=Channel.INSTAGRAM,
            external_id="ig_attr_insights_881",
        )
        store.save_canonical_event(
            provider="instagram",
            event=build_attribution_event(
                provider="instagram",
                channel=Channel.INSTAGRAM,
                lead_id=lead_id,
                conversation_id="ig_attr_insights_881",
                payload={"ig_content_id": MEDIA_ID_ATTR},
            ),
        )
        apply_content_insight_policy(
            store,
            items=[
                ContentInsight(media_id=MEDIA_ID_ATTR, media_type="IMAGE", views="10")
            ],
            kill_switch=False,
        )
        db.commit()
        rows = store.list_content_insights()
        matching = [row for row in rows if row.media_id == MEDIA_ID_ATTR]
        assert len(matching) == 1
        assert matching[0].lead_signals == 1
        assert store.count_attribution_for_ig_content(MEDIA_ID_ATTR) == 1
        assert store.count_attribution_for_ig_content("not-digits") == 0
    finally:
        db.close()


def test_enrich_kill_switch_denied_no_http() -> None:
    class RaisingInsightsPort:
        def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]:
            del limit
            raise RuntimeError("must not call port when kill switch is on")

    ack = ack_for_owner_task(classify_owner_task("instagram content performance"))
    enriched, outcome = enrich_content_insights_ack(
        ack,
        RaisingInsightsPort(),
        store=None,
        kill_switch=True,
    )
    assert enriched == ack
    assert outcome.status == "denied"


def test_never_imports_message_port() -> None:
    import app.integrations.instagram_insights as module

    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "instagram.py" not in source


def test_classify_analyze_instagram_content_is_analytics() -> None:
    decision = classify_owner_task("analyze instagram content")
    assert decision.task_type == OwnerTaskType.ANALYTICS
    assert decision.needs_clarification is False


def test_build_port_live_when_token_and_account_set() -> None:
    settings = Settings(
        instagram_access_token="ig-live",
        instagram_account_id="123456789",
    )
    port = build_instagram_insights_port(settings)
    assert isinstance(port, GraphInstagramInsightsPort)


def test_build_port_disabled_when_credentials_missing() -> None:
    settings = Settings(instagram_access_token="", instagram_account_id="")
    port = build_instagram_insights_port(settings)
    assert isinstance(port, DisabledInstagramInsightsPort)


def test_format_content_insights_line_empty_when_no_items() -> None:
    assert format_content_insights_line([]) == ""


def test_format_content_insights_detail_names_posts_and_refuses_anonymous_totals() -> None:
    from app.integrations.instagram_insights import format_content_insights_detail

    named = ContentInsight(
        media_id=MEDIA_ID_1,
        media_type="REELS",
        caption="Hook line",
        timestamp="2026-09-01T10:00:00+0000",
        permalink="https://instagram.com/p/abc",
        account_kind="playground",
        views="1000",
        reach="800",
    )
    missing_id = ContentInsight(
        media_id="",
        media_type="IMAGE",
        views="9999",
    )
    text = format_content_insights_detail([named, missing_id])
    assert "Hook line" in text
    assert "2026-09-01T10:00:00+0000" in text
    assert "https://instagram.com/p/abc" in text
    assert "playground" in text
    assert "views=1000" in text
    assert "media identity missing" in text
    assert "9999" not in text
    assert "combined view/reach" in text.lower() or "No combined view/reach totals" in text


@pytest.mark.asyncio
async def test_owner_analytics_inbound_tool_result_instagram_insights() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.ig.content.1",
                    "from": OWNER_IG_CONTENT_PHONE,
                    "text": "analyze instagram content",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_IG_CONTENT_PHONE},
            sheets=sheets,
            instagram_insights=FakeInstagramInsightsPort(SAMPLE_ITEMS),
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.ig.content.1"
        )
        assert task is not None
        assert task.task_type == "analytics"
        sent = port.sent[0].text
        assert "תוכן: 2 פוסטים" in sent
        tool_row = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="evt.owner.ig.content.1:tool:instagram_insights",
        )
        assert tool_row is not None
        payload = json.loads(tool_row.payload_json)
        assert payload["status"] == "ok"
        assert payload["result_count"] == 2
    finally:
        db.close()


def test_build_insights_composio_when_sender_composio() -> None:
    settings = Settings(
        instagram_sender="composio",
        composio_api_key="cmp-key",
        composio_user_id="user-abc",
        instagram_access_token="ig-live",
        instagram_account_id="123456789",
    )
    from app.integrations.instagram_insights import ComposioInstagramInsightsPort

    port = build_instagram_insights_port(settings)
    assert isinstance(port, ComposioInstagramInsightsPort)


def test_build_insights_direct_keeps_graph_when_token_set() -> None:
    settings = Settings(
        instagram_sender="direct",
        composio_api_key="cmp-key",
        composio_user_id="user-abc",
        instagram_access_token="ig-live",
        instagram_account_id="123456789",
    )
    port = build_instagram_insights_port(settings)
    assert isinstance(port, GraphInstagramInsightsPort)


def test_composio_insights_parses_media_no_captions_or_urls() -> None:
    captured: list[dict] = []
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        body = json.loads(request.content)
        captured.append(body)
        slug = str(request.url).rsplit("/", 1)[-1]
        if slug == "INSTAGRAM_GET_IG_USER_MEDIA":
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {
                        "data": [
                            {
                                "id": MEDIA_ID_1,
                                "media_type": "IMAGE",
                                "caption": "Launch hook",
                                "timestamp": "2026-09-01T10:00:00+0000",
                                "permalink": "https://instagram.com/p/abc",
                                "media_url": "https://cdn.example/photo.jpg",
                            },
                            {"id": "bad-id", "media_type": "IMAGE"},
                            {"id": MEDIA_ID_2, "media_type": "STORY"},
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "successful": True,
                "data": {
                    "data": [
                        {"name": "views", "values": [{"value": 1200}]},
                        {"name": "reach", "total_value": {"value": 900}},
                        {"name": "likes", "values": [{"value": 45}]},
                    ]
                },
            },
        )

    from app.integrations.instagram import (
        COMPOSIO_GET_MEDIA_INSIGHTS_TOOL,
        COMPOSIO_GET_USER_MEDIA_TOOL,
        COMPOSIO_INSTAGRAM_VERSION,
    )
    from app.integrations.instagram_insights import ComposioInstagramInsightsPort

    port = ComposioInstagramInsightsPort(
        api_key="cmp-test",
        user_id="user-abc",
        account_id="17841400000000000",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    items = port.list_recent_insights(limit=5)
    assert len(items) == 1
    assert items[0].media_id == MEDIA_ID_1
    assert items[0].views == "1200"
    assert items[0].reach == "900"
    serialized = json.dumps([item.model_dump() for item in items])
    assert items[0].caption == "Launch hook"
    assert items[0].permalink == "https://instagram.com/p/abc"
    assert "cdn.example" not in serialized
    assert urls[0].endswith(f"/{COMPOSIO_GET_USER_MEDIA_TOOL}")
    assert urls[1].endswith(f"/{COMPOSIO_GET_MEDIA_INSIGHTS_TOOL}")
    assert captured[0]["version"] == COMPOSIO_INSTAGRAM_VERSION
    media_args = captured[0]["arguments"]
    assert media_args["fields"] == "id,media_type,caption,timestamp,permalink"
    assert media_args["ig_user_id"] == "17841400000000000"
    insight_args = captured[1]["arguments"]
    assert insight_args["ig_media_id"] == MEDIA_ID_1
    assert insight_args["metric"] == ["views", "reach", "likes", "comments", "saved"]


def test_composio_retries_only_classified_mixed_metric_incompatibility() -> None:
    requested_metrics: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        arguments = body["arguments"]
        if "fields" in arguments:
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {"data": [{"id": MEDIA_ID_1, "media_type": "IMAGE"}]},
                },
            )
        metric = arguments["metric"]
        requested_metrics.append(metric)
        if len(metric) > 1:
            return httpx.Response(
                200,
                json={
                    "successful": False,
                    "error": {"message": "mixed metrics unsupported"},
                },
            )
        return httpx.Response(
            200,
            json={
                "successful": True,
                "data": {
                    "data": [{"name": metric[0], "values": [{"value": 7}]}]
                },
            },
        )

    from app.integrations.instagram_insights import ComposioInstagramInsightsPort

    port = ComposioInstagramInsightsPort(
        api_key="cmp-test",
        user_id="user-abc",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    items = port.list_recent_insights(limit=1)
    assert len(items) == 1
    assert items[0].views == "7"
    assert requested_metrics == [
        ["views", "reach", "likes", "comments", "saved"],
        ["views"],
        ["reach"],
        ["likes"],
        ["comments"],
        ["saved"],
    ]


def test_composio_provider_failure_does_not_fan_out_metrics() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if "fields" in body["arguments"]:
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {"data": [{"id": MEDIA_ID_1, "media_type": "IMAGE"}]},
                },
            )
        return httpx.Response(503, json={"message": "upstream unavailable"})

    from app.integrations.instagram_insights import ComposioInstagramInsightsPort

    port = ComposioInstagramInsightsPort(
        api_key="cmp-test",
        user_id="user-abc",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.list_recent_insights(limit=1)
    assert exc_info.value.status_code == 503
    assert calls == 2
