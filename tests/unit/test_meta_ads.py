import inspect
import json
from datetime import UTC, datetime

import httpx
import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.campaigns import baseline_7d_time_range, previous_30d_time_range
from app.domain.events import Channel
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.meta_ads import (
    COMPOSIO_GET_INSIGHTS_TOOL,
    COMPOSIO_METAADS_VERSION,
    INSIGHT_FIELDS,
    CampaignInsights,
    ComposioMetaAdsPort,
    DisabledMetaAdsPort,
    FakeMetaAdsPort,
    MetaAdsPort,
    build_meta_ads_port,
    enrich_analytics_ack,
    format_insights_line,
    format_today_baseline_line,
)
from app.integrations.sheets import FakeSheetsPort

OWNER_SPEND_PHONE = "972509990005"
OWNER_PAUSE_PHONE = "972509990006"

SAMPLE_INSIGHTS = CampaignInsights(
    spend="₪1,234",
    impressions="45,678",
    clicks="890",
    ctr="1.95%",
)


def _patch_behavior_counts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        LeadStore,
        "count_behavior_events",
        lambda self, *, kind, occurred_from, occurred_to: 0,
    )


def test_fake_returns_snapshot_disabled_returns_none() -> None:
    fake = FakeMetaAdsPort(SAMPLE_INSIGHTS)
    disabled = DisabledMetaAdsPort()
    assert fake.get_insights() == SAMPLE_INSIGHTS
    assert disabled.get_insights() is None


def test_enrich_analytics_ack_fake_appends_spend_line() -> None:
    decision = classify_owner_task("how's the campaign spend")
    assert decision.task_type == OwnerTaskType.ANALYTICS
    ack = ack_for_owner_task(decision)
    enriched, _outcome = enrich_analytics_ack(
        ack, FakeMetaAdsPort(SAMPLE_INSIGHTS), kill_switch=False
    )
    assert "לא ביצעתי" in enriched
    assert "תקציבים או מודעות במטא" in enriched
    assert "7d אחרונים: spend ₪1,234, impr 45,678, clicks 890, CTR 1.95%." in enriched
    assert "בלי שינוי תקציב" in enriched


def test_enrich_analytics_ack_http_401_unauthorized_ack_unchanged() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_analytics_ack(ack, port, kill_switch=False)
    assert enriched == ack
    assert outcome.status == "unauthorized"
    assert outcome.result_count == 0
    assert "7d אחרונים" not in enriched


def test_enrich_analytics_ack_http_429_rate_limited_ack_unchanged() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(429))
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_analytics_ack(ack, port, kill_switch=False)
    assert enriched == ack
    assert outcome.status == "rate_limited"
    assert outcome.result_count == 0


def test_enrich_analytics_ack_secondary_http_error_keeps_primary_line() -> None:
    class PrimaryThenHttpError:
        def __init__(self) -> None:
            self.calls = 0

        def get_insights(
            self,
            *,
            date_preset: str | None = "last_7d",
            time_range: dict[str, str] | None = None,
        ) -> CampaignInsights | None:
            del date_preset, time_range
            self.calls += 1
            if self.calls == 1:
                return SAMPLE_INSIGHTS
            raise AdapterHttpError(401)

    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    settings = Settings(calendar_timezone="Asia/Jerusalem")
    port = PrimaryThenHttpError()
    enriched, outcome = enrich_analytics_ack(
        ack, port, kill_switch=False, settings=settings
    )
    assert outcome.status == "ok"
    assert "7d אחרונים: spend ₪1,234, impr 45,678, clicks 890, CTR 1.95%." in enriched
    assert port.calls >= 2


def test_enrich_analytics_ack_disabled_leaves_ack_unchanged() -> None:
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    enriched, _outcome = enrich_analytics_ack(ack, DisabledMetaAdsPort(), kill_switch=False)
    assert enriched == ack


def test_enrich_analytics_ack_kill_switch_skips_port_call() -> None:
    class RaisingMetaAdsPort:
        def get_insights(
            self,
            *,
            date_preset: str | None = "last_7d",
            time_range: dict[str, str] | None = None,
        ) -> CampaignInsights | None:
            del date_preset, time_range
            raise RuntimeError("must not call port when kill switch is on")

    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_analytics_ack(ack, RaisingMetaAdsPort(), kill_switch=True)
    assert enriched == ack
    assert outcome.status == "denied"


def test_format_insights_line_omits_missing_metrics() -> None:
    partial = CampaignInsights(spend="₪500", clicks="42")
    assert format_insights_line(partial) == "7d אחרונים: spend ₪500, clicks 42."
    assert format_insights_line(CampaignInsights()) == ""


def test_format_insights_line_includes_frequency_when_set() -> None:
    insights = CampaignInsights(spend="100", clicks="50", ctr="1.2%", frequency="2.1")
    assert format_insights_line(insights) == (
        "7d אחרונים: spend 100, clicks 50, CTR 1.2%, freq 2.1."
    )


def test_format_insights_line_omits_frequency_when_missing() -> None:
    insights = CampaignInsights(spend="100", clicks="50", ctr="1.2%")
    line = format_insights_line(insights)
    assert "freq" not in line
    assert line == "7d אחרונים: spend 100, clicks 50, CTR 1.2%."


def test_composio_meta_ads_port_maps_frequency() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "data": [
                        {
                            "spend": "100",
                            "clicks": "50",
                            "ctr": "1.2",
                            "frequency": "2.1",
                        }
                    ]
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    insights = port.get_insights()
    assert insights is not None
    assert insights.frequency == "2.1"


@pytest.mark.asyncio
async def test_owner_analytics_spend_insights_in_sent_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_behavior_counts_zero(monkeypatch)
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
                    "id": "evt.owner.meta.spend.1",
                    "from": OWNER_SPEND_PHONE,
                    "text": "how's the campaign spend",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_SPEND_PHONE},
            sheets=sheets,
            meta_ads=FakeMetaAdsPort(SAMPLE_INSIGHTS),
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.meta.spend.1"
        )
        assert task is not None
        assert task.task_type == "analytics"
        assert task.status == "logged"
        assert sheets.rows == {}
        sent = port.sent[0].text
        assert "7d אחרונים: spend ₪1,234" in sent
        assert "לא ביצעתי" in sent
        assert "how the business works" not in sent
        assert "יום רגיל בעסק" not in sent
        tool_row = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="evt.owner.meta.spend.1:tool:meta_ads_insights",
        )
        assert tool_row is not None
        payload = json.loads(tool_row.payload_json)
        assert payload["status"] == "ok"
        assert payload["result_count"] == 1
        serialized = json.dumps(payload).lower()
        assert "spend" not in serialized
        assert "1234" not in serialized
        assert tool_row.lead_id is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_pause_budget_logged_not_executed_insights_may_append() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.meta.pause.1",
                    "from": OWNER_PAUSE_PHONE,
                    "text": "pause the campaign budget",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PAUSE_PHONE},
            meta_ads=FakeMetaAdsPort(SAMPLE_INSIGHTS),
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.meta.pause.1"
        )
        assert task is not None
        assert task.task_type == "analytics"
        assert task.status == "logged"
        ack = port.sent[0].text
        assert "תקציבים או מודעות במטא" in ack
        assert "לא ביצעתי" in ack
        assert "7d אחרונים: spend ₪1,234" in ack
    finally:
        db.close()


def test_protocol_has_no_write_methods() -> None:
    forbidden = ("create", "update", "delete", "pause")
    protocol_methods = {
        name
        for name, _ in inspect.getmembers(MetaAdsPort, predicate=inspect.isfunction)
    }
    for name in protocol_methods:
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden)

    for impl in (
        DisabledMetaAdsPort(),
        FakeMetaAdsPort(SAMPLE_INSIGHTS),
        ComposioMetaAdsPort(
            api_key="cmp-test",
            user_id="user-123",
            account_id="act_123",
        ),
    ):
        for name in dir(impl):
            if name.startswith("_"):
                continue
            lowered = name.lower()
            assert not any(token in lowered for token in forbidden)


def test_build_meta_ads_port_live_when_all_three_credentials_set() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        meta_ads_account_id="act_999",
    )
    port = build_meta_ads_port(settings)
    assert isinstance(port, ComposioMetaAdsPort)
    assert not isinstance(port, DisabledMetaAdsPort)


@pytest.mark.parametrize(
    "api_key,user_id,account_id",
    [
        ("", "", ""),
        ("cmp-live", "", ""),
        ("", "user-123", ""),
        ("cmp-live", "user-123", ""),
        ("cmp-live", "user-123", "   "),
        ("   ", "user-123", "act_123"),
        ("cmp-live", "   ", "act_123"),
    ],
)
def test_build_meta_ads_port_disabled_when_any_credential_missing(
    api_key: str,
    user_id: str,
    account_id: str,
) -> None:
    settings = Settings(
        composio_api_key=api_key,
        composio_user_id=user_id,
        meta_ads_account_id=account_id,
    )
    port = build_meta_ads_port(settings)
    assert isinstance(port, DisabledMetaAdsPort)


def test_composio_meta_ads_port_http_500_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.get_insights()
    assert exc_info.value.status_code == 500


class _RaisingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.HTTPError("network error")


def test_composio_meta_ads_port_network_error_raises_adapter_error() -> None:
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.get_insights()
    assert exc_info.value.status_code is None


def test_composio_meta_ads_port_unsuccessful_response_returns_none() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": {}, "error": "tool failed", "successful": False},
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    assert port.get_insights() is None


def test_composio_meta_ads_port_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"data": [{"spend": "100"}]},
                "error": None,
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-abc",
        account_id="123",
        client=client,
    )
    port.get_insights()

    assert str(captured["url"]).endswith(f"/{COMPOSIO_GET_INSIGHTS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["user_id"] == "user-abc"
    assert body["version"] == COMPOSIO_METAADS_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["object_id"] == "act_123"
    assert arguments["level"] == "account"
    assert arguments["date_preset"] == "last_7d"
    assert arguments["fields"] == INSIGHT_FIELDS
    assert arguments["limit"] == 1
    assert "text" not in body
    assert "text" not in arguments
    assert "time_range" not in arguments
    serialized = json.dumps(body)
    for forbidden in ("CREATE", "UPDATE", "DELETE", "UPLOAD", "PAUSE"):
        assert forbidden not in serialized.upper()


def test_composio_meta_ads_port_account_id_act_prefix_preserved() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {"data": []}, "error": None, "successful": True},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-abc",
        account_id="act_456",
        client=client,
    )
    port.get_insights()
    body = captured["json"]
    assert isinstance(body, dict)
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["object_id"] == "act_456"


def test_composio_meta_ads_port_maps_metrics_missing_spend_not_zero_filled() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "data": [
                        {
                            "impressions": 45678,
                            "clicks": "890",
                            "ctr": "1.95",
                        }
                    ]
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    insights = port.get_insights()
    assert insights is not None
    assert insights.spend is None
    assert insights.impressions == "45678"
    assert insights.clicks == "890"
    assert insights.ctr == "1.95"
    assert insights.date_preset == "last_7d"


def test_composio_meta_ads_port_maps_full_row_from_list_data() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "spend": "1234.56",
                        "impressions": "45,678",
                        "clicks": 890,
                        "ctr": 1.95,
                    }
                ],
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    insights = port.get_insights()
    assert insights is not None
    assert insights.spend == "1234.56"
    assert insights.impressions == "45,678"
    assert insights.clicks == "890"
    assert insights.ctr == "1.95"


def test_composio_meta_ads_port_maps_flat_data_dict() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {"spend": "50", "clicks": "3"},
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    insights = port.get_insights()
    assert insights is not None
    assert insights.spend == "50"
    assert insights.clicks == "3"
    assert insights.impressions is None


def test_port_rejects_both_date_preset_and_time_range() -> None:
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
    )
    assert port.get_insights(
        date_preset="last_7d",
        time_range={"since": "2026-08-07", "until": "2026-08-13"},
    ) is None


def test_port_rejects_time_range_span_over_31_days() -> None:
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
    )
    assert port.get_insights(
        date_preset=None,
        time_range={"since": "2026-06-22", "until": "2026-07-23"},
    ) is None


def test_port_accepts_30_day_time_range() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {"data": [{"spend": "200", "clicks": "40"}]},
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-123",
        account_id="act_123",
        client=client,
    )
    insights = port.get_insights(
        date_preset=None,
        time_range={"since": "2026-06-22", "until": "2026-07-21"},
    )
    assert insights is not None
    assert insights.spend == "200"


def test_port_accepts_last_30d_date_preset() -> None:
    fake = FakeMetaAdsPort(
        snapshot_30d=CampaignInsights(spend="300", clicks="30", date_preset="last_30d"),
    )
    insights = fake.get_insights(date_preset="last_30d")
    assert insights is not None
    assert insights.spend == "300"
    assert fake.calls[-1]["date_preset"] == "last_30d"
    assert fake.calls[-1]["time_range"] is None


def test_fake_last_30d_vs_7d_vs_30d_time_range_snapshots() -> None:
    last_7d = CampaignInsights(spend="70", clicks="7")
    last_30d = CampaignInsights(spend="300", clicks="30")
    previous_7d = CampaignInsights(spend="60", clicks="6")
    previous_30d = CampaignInsights(spend="200", clicks="20")
    fake = FakeMetaAdsPort(
        last_7d,
        previous_snapshot=previous_7d,
        snapshot_30d=last_30d,
        previous_30d_snapshot=previous_30d,
    )
    assert fake.get_insights() == last_7d
    assert fake.get_insights(
        date_preset=None,
        time_range={"since": "2026-08-07", "until": "2026-08-13"},
    ) == previous_7d
    assert fake.get_insights(date_preset="last_30d") == last_30d
    assert fake.get_insights(
        date_preset=None,
        time_range={"since": "2026-06-22", "until": "2026-07-21"},
    ) == previous_30d


def test_fake_previous_snapshot_only_when_time_range_set() -> None:
    previous = CampaignInsights(spend="100", clicks="50")
    fake = FakeMetaAdsPort(
        CampaignInsights(spend="200", clicks="10"),
        previous_snapshot=previous,
    )
    assert fake.get_insights() == CampaignInsights(spend="200", clicks="10")
    assert fake.get_insights(
        date_preset=None,
        time_range={"since": "2026-08-07", "until": "2026-08-13"},
    ) == previous
    assert len(fake.calls) == 2


def test_composio_meta_ads_port_time_range_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"data": [{"spend": "100", "clicks": "50"}]},
                "error": None,
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-abc",
        account_id="123",
        client=client,
    )
    insights = port.get_insights(
        date_preset=None,
        time_range={"since": "2026-08-07", "until": "2026-08-13"},
    )
    assert insights is not None
    assert insights.spend == "100"
    assert insights.date_preset == ""
    body = captured["json"]
    assert isinstance(body, dict)
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert "date_preset" not in arguments
    assert arguments["time_range"] == {"since": "2026-08-07", "until": "2026-08-13"}
    serialized = json.dumps(body)
    for forbidden in ("CREATE", "UPDATE", "DELETE", "UPLOAD", "PAUSE"):
        assert forbidden not in serialized.upper()


def test_enrich_analytics_ack_compare_recommend_line() -> None:
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    settings = Settings(calendar_timezone="Asia/Jerusalem")
    current = CampaignInsights(spend="200", clicks="10")
    previous = CampaignInsights(spend="100", clicks="50")
    enriched, outcome = enrich_analytics_ack(
        ack,
        FakeMetaAdsPort(current, previous_snapshot=previous),
        kill_switch=False,
        settings=settings,
    )
    assert "הוצאה עלתה וקליקים ירדו" in enriched
    assert "בלי שינוי תקציב" in enriched
    assert outcome.status == "ok"
    assert outcome.result_count == 1
    fake = FakeMetaAdsPort(current, previous_snapshot=previous)
    enrich_analytics_ack(ack, fake, kill_switch=False, settings=settings)
    assert len(fake.calls) == 4
    assert fake.calls[1]["date_preset"] is None
    assert fake.calls[1]["time_range"] is not None
    assert fake.calls[2]["date_preset"] == "today"
    assert fake.calls[2]["time_range"] is None
    assert fake.calls[3]["date_preset"] is None
    assert fake.calls[3]["time_range"] is not None


def test_enrich_analytics_ack_30d_compare_when_7d_watch() -> None:
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    settings = Settings(calendar_timezone="Asia/Jerusalem")
    current = CampaignInsights(spend="100", clicks="50")
    insights_30d = CampaignInsights(spend="400", clicks="5")
    previous_30d = CampaignInsights(spend="200", clicks="40")
    fake = FakeMetaAdsPort(
        current,
        snapshot_30d=insights_30d,
        previous_30d_snapshot=previous_30d,
    )
    enriched, outcome = enrich_analytics_ack(
        ack,
        fake,
        kill_switch=False,
        settings=settings,
    )
    expected_30d = previous_30d_time_range(
        now=datetime.now(UTC),
        timezone=settings.calendar_timezone,
    )
    assert "7d אחרונים:" in enriched
    assert "שלושים" in enriched
    assert "בלי שינוי תקציב" in enriched
    assert outcome.status == "ok"
    assert outcome.result_count == 1
    assert any(call["date_preset"] == "last_30d" for call in fake.calls)
    assert expected_30d is not None
    assert any(
        call["date_preset"] is None and call["time_range"] == expected_30d
        for call in fake.calls
    )


def test_enrich_analytics_ack_30d_spend_without_clicks_does_not_override_7d_watch() -> None:
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    settings = Settings(calendar_timezone="Asia/Jerusalem")
    fake = FakeMetaAdsPort(
        CampaignInsights(spend="100", clicks="50"),
        snapshot_30d=CampaignInsights(spend="400", clicks=None),
    )
    enriched, outcome = enrich_analytics_ack(
        ack,
        fake,
        kill_switch=False,
        settings=settings,
    )
    assert outcome.status == "ok"
    assert "תקינים" in enriched
    assert "בלי קליקים" not in enriched
    assert "שלושים" not in enriched


def test_enrich_analytics_ack_spend_without_clicks_skips_30d_fetch() -> None:
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    settings = Settings(calendar_timezone="Asia/Jerusalem")
    current = CampaignInsights(spend="500", clicks=None)
    fake = FakeMetaAdsPort(
        current,
        snapshot_30d=CampaignInsights(spend="400", clicks="5"),
        previous_30d_snapshot=CampaignInsights(spend="200", clicks="40"),
    )
    enriched, _outcome = enrich_analytics_ack(
        ack,
        fake,
        kill_switch=False,
        settings=settings,
    )
    assert "בלי קליקים" in enriched
    assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
    assert len(fake.calls) == 4


def test_enrich_analytics_ack_spend_without_leads_from_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _patch_behavior_counts_zero(monkeypatch)
        monkeypatch.setattr(
            LeadStore,
            "count_canonical_events",
            lambda self, *, event_type, occurred_from, occurred_to: 0,
        )
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(CampaignInsights(spend="100", clicks="50"))
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "לידים" in enriched
        assert "בלי שינוי תקציב" in enriched
        assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
        assert fake.calls[0]["date_preset"] == "last_7d"
    finally:
        db.close()


def test_enrich_analytics_ack_cpl_spike_from_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _patch_behavior_counts_zero(monkeypatch)
        lead_counts = iter([2, 4])

        def _count(self, *, event_type, occurred_from, occurred_to):
            del self, event_type, occurred_from, occurred_to
            return next(lead_counts)

        monkeypatch.setattr(LeadStore, "count_canonical_events", _count)
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(
            CampaignInsights(spend="200", clicks="10"),
            previous_snapshot=CampaignInsights(spend="100", clicks="5"),
        )
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "עלות ליד" in enriched
        assert "בלי שינוי תקציב" in enriched
        assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
    finally:
        db.close()


def test_enrich_analytics_ack_creative_fatigue_from_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _patch_behavior_counts_zero(monkeypatch)
        lead_counts = iter([1, 0])

        def _count(self, *, event_type, occurred_from, occurred_to):
            del self, event_type, occurred_from, occurred_to
            return next(lead_counts)

        monkeypatch.setattr(LeadStore, "count_canonical_events", _count)
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(
            CampaignInsights(
                spend="100",
                clicks="50",
                frequency="3",
                ctr="0.8",
            ),
            previous_snapshot=CampaignInsights(
                spend="90",
                clicks="50",
                frequency="1.5",
                ctr="1.9",
            ),
        )
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "תדירות" in enriched
        assert "קריאייטיב" in enriched
        assert "בלי שינוי תקציב" in enriched
        assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
    finally:
        db.close()


def test_enrich_analytics_ack_cpl_spike_wins_over_creative_fatigue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _patch_behavior_counts_zero(monkeypatch)
        lead_counts = iter([2, 4])

        def _count(self, *, event_type, occurred_from, occurred_to):
            del self, event_type, occurred_from, occurred_to
            return next(lead_counts)

        monkeypatch.setattr(LeadStore, "count_canonical_events", _count)
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(
            CampaignInsights(
                spend="200",
                clicks="50",
                frequency="3",
                ctr="0.8",
            ),
            previous_snapshot=CampaignInsights(
                spend="100",
                clicks="50",
                frequency="1.5",
                ctr="1.9",
            ),
        )
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "עלות ליד" in enriched
        assert "תדירות" not in enriched
        assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
    finally:
        db.close()


def test_enrich_analytics_ack_no_store_skips_leads_check() -> None:
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    settings = Settings(calendar_timezone="Asia/Jerusalem")
    fake = FakeMetaAdsPort(CampaignInsights(spend="100", clicks="50"))
    enriched, outcome = enrich_analytics_ack(
        ack,
        fake,
        kill_switch=False,
        settings=settings,
    )
    assert outcome.status == "ok"
    assert "תקינים" in enriched
    assert "לידים" not in enriched


def test_enrich_analytics_ack_30d_compare_persists_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _patch_behavior_counts_zero(monkeypatch)
        monkeypatch.setattr(
            LeadStore,
            "count_canonical_events",
            lambda self, *, event_type, occurred_from, occurred_to: 1,
        )
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(
            CampaignInsights(spend="100", clicks="50"),
            snapshot_30d=CampaignInsights(spend="400", clicks="5"),
            previous_30d_snapshot=CampaignInsights(spend="200", clicks="40"),
        )
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        db.commit()
        assert outcome.status == "ok"
        assert "שלושים" in enriched
        row = store.get_campaign_recommendation()
        assert row is not None
        assert row.anomaly == "spend_up_clicks_down_30d"
        payload = json.loads(row.payload_json)
        assert set(payload.keys()) == {"kind", "anomaly"}
        assert "400" not in json.dumps(payload)
    finally:
        db.close()


def test_enrich_analytics_ack_creative_fatigue_wins_over_funnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        monkeypatch.setattr(
            LeadStore,
            "count_canonical_events",
            lambda self, *, event_type, occurred_from, occurred_to: 1,
        )

        def _behavior_count_must_not_run(self, *, kind, occurred_from, occurred_to):
            del self, kind, occurred_from, occurred_to
            raise AssertionError("behavior counts must not run when fatigue wins")

        monkeypatch.setattr(LeadStore, "count_behavior_events", _behavior_count_must_not_run)
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(
            CampaignInsights(
                spend="100",
                clicks="50",
                frequency="3",
                ctr="0.8",
            ),
            previous_snapshot=CampaignInsights(
                spend="100",
                clicks="50",
                frequency="1.5",
                ctr="1.9",
            ),
        )
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "תדירות" in enriched
        assert "פתיחות" not in enriched
        assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
    finally:
        db.close()


def test_enrich_analytics_ack_cpl_spike_wins_over_funnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_counts = iter([2, 4])

        def _count(self, *, event_type, occurred_from, occurred_to):
            del self, event_type, occurred_from, occurred_to
            return next(lead_counts)

        monkeypatch.setattr(LeadStore, "count_canonical_events", _count)

        def _behavior_count_must_not_run(self, *, kind, occurred_from, occurred_to):
            del self, kind, occurred_from, occurred_to
            raise AssertionError("behavior counts must not run when CPL spike wins")

        monkeypatch.setattr(LeadStore, "count_behavior_events", _behavior_count_must_not_run)
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(
            CampaignInsights(spend="200", clicks="50"),
            previous_snapshot=CampaignInsights(spend="100", clicks="50"),
        )
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "עלות ליד" in enriched
        assert "פתיחות" not in enriched
        assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
    finally:
        db.close()


def test_enrich_analytics_ack_zero_conversations_is_funnel_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        monkeypatch.setattr(
            LeadStore,
            "count_canonical_events",
            lambda self, *, event_type, occurred_from, occurred_to: 1,
        )
        behavior_counts = iter([4, 0])

        def _behavior_count(self, *, kind, occurred_from, occurred_to):
            del self, kind, occurred_from, occurred_to
            return next(behavior_counts)

        monkeypatch.setattr(LeadStore, "count_behavior_events", _behavior_count)
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(CampaignInsights(spend="100", clicks="50"))
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "ירידה" in enriched
        assert "משפך" in enriched
        assert "בלי שינוי תקציב" in enriched
        assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
    finally:
        db.close()


def test_enrich_analytics_ack_website_funnel_drop_from_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        monkeypatch.setattr(
            LeadStore,
            "count_canonical_events",
            lambda self, *, event_type, occurred_from, occurred_to: 1,
        )
        behavior_counts = iter([10, 2, 5, 8])

        def _behavior_count(self, *, kind, occurred_from, occurred_to):
            del self, kind, occurred_from, occurred_to
            return next(behavior_counts)

        monkeypatch.setattr(LeadStore, "count_behavior_events", _behavior_count)
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        fake = FakeMetaAdsPort(CampaignInsights(spend="100", clicks="50"))
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "ירידה" in enriched
        assert "משפך" in enriched
        assert "בלי שינוי תקציב" in enriched
        assert not any(call["date_preset"] == "last_30d" for call in fake.calls)
    finally:
        db.close()


def test_composio_meta_ads_port_30d_time_range_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"data": [{"spend": "200", "clicks": "40"}]},
                "error": None,
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-abc",
        account_id="123",
        client=client,
    )
    insights = port.get_insights(
        date_preset=None,
        time_range={"since": "2026-06-22", "until": "2026-07-21"},
    )
    assert insights is not None
    body = captured["json"]
    assert isinstance(body, dict)
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert "date_preset" not in arguments
    assert arguments["time_range"] == {"since": "2026-06-22", "until": "2026-07-21"}
    serialized = json.dumps(body)
    for forbidden in ("CREATE", "UPDATE", "DELETE", "UPLOAD", "PAUSE"):
        assert forbidden not in serialized.upper()


def test_composio_meta_ads_port_last_30d_date_preset_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"data": [{"spend": "300", "clicks": "30"}]},
                "error": None,
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-abc",
        account_id="123",
        client=client,
    )
    insights = port.get_insights(date_preset="last_30d")
    assert insights is not None
    body = captured["json"]
    assert isinstance(body, dict)
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["date_preset"] == "last_30d"
    assert "time_range" not in arguments
    serialized = json.dumps(body)
    for forbidden in ("CREATE", "UPDATE", "DELETE", "UPLOAD", "PAUSE"):
        assert forbidden not in serialized.upper()


def test_composio_meta_ads_port_today_preset_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"data": [{"spend": "10", "clicks": "2"}]},
                "error": None,
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioMetaAdsPort(
        api_key="cmp-test",
        user_id="user-abc",
        account_id="123",
        client=client,
    )
    insights = port.get_insights(date_preset="today")
    assert insights is not None
    assert insights.spend == "10"
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["version"] == COMPOSIO_METAADS_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["date_preset"] == "today"
    assert arguments["fields"] == INSIGHT_FIELDS
    assert "time_range" not in arguments
    serialized = json.dumps(body)
    for forbidden in ("CREATE", "UPDATE", "DELETE", "UPLOAD", "PAUSE"):
        assert forbidden not in serialized.upper()


def test_format_today_baseline_divides_additive_by_seven() -> None:
    today = CampaignInsights(spend="14", impressions="700", clicks="21")
    baseline = CampaignInsights(spend="70", impressions="3500", clicks="105")
    line = format_today_baseline_line(today, baseline)
    assert line == (
        "היום עד עכשיו מול ממוצע שבעה ימים מלאים: "
        "spend 14 / 10; impr 700 / 500; clicks 21 / 15."
    )


def test_format_today_baseline_ctr_not_divided() -> None:
    today = CampaignInsights(ctr="2.5")
    baseline = CampaignInsights(ctr="1.75")
    line = format_today_baseline_line(today, baseline)
    assert line == (
        "היום עד עכשיו מול ממוצע שבעה ימים מלאים: CTR 2.5 / 1.75."
    )


def test_format_today_baseline_omits_missing_pairs() -> None:
    today = CampaignInsights(spend="10", clicks="5")
    baseline = CampaignInsights(spend="70")
    line = format_today_baseline_line(today, baseline)
    assert line == (
        "היום עד עכשיו מול ממוצע שבעה ימים מלאים: spend 10 / 10."
    )
    assert "clicks" not in line


def test_format_today_baseline_no_pairs_empty() -> None:
    assert format_today_baseline_line(CampaignInsights(), CampaignInsights()) == ""


def test_format_today_baseline_omits_frequency() -> None:
    today = CampaignInsights(spend="10", frequency="3")
    baseline = CampaignInsights(spend="70", frequency="2")
    line = format_today_baseline_line(today, baseline)
    assert "freq" not in line
    assert line == (
        "היום עד עכשיו מול ממוצע שבעה ימים מלאים: spend 10 / 10."
    )


def test_format_today_baseline_compact_decimals() -> None:
    today = CampaignInsights(spend="1.20")
    baseline = CampaignInsights(spend="7.07")
    line = format_today_baseline_line(today, baseline)
    assert "1.2 / 1.01" in line


def test_enrich_analytics_ack_appends_today_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_behavior_counts_zero(monkeypatch)
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    settings = Settings(calendar_timezone="Asia/Jerusalem")
    baseline_range = baseline_7d_time_range(
        now=datetime.now(UTC),
        timezone=settings.calendar_timezone,
    )
    assert baseline_range is not None
    fake = FakeMetaAdsPort(
        CampaignInsights(spend="100", clicks="50"),
        today_snapshot=CampaignInsights(spend="14", clicks="7", ctr="1.5"),
        time_range_snapshots={
            (baseline_range["since"], baseline_range["until"]): CampaignInsights(
                spend="70",
                clicks="35",
                ctr="1.2",
            ),
        },
    )
    enriched, outcome = enrich_analytics_ack(
        ack,
        fake,
        kill_switch=False,
        settings=settings,
    )
    assert outcome.status == "ok"
    assert "היום עד עכשיו מול ממוצע שבעה ימים מלאים" in enriched
    assert "spend 14 / 10" in enriched
    assert any(
        call["date_preset"] == "today" and call["time_range"] is None
        for call in fake.calls
    )
    assert any(
        call["date_preset"] is None and call["time_range"] == baseline_range
        for call in fake.calls
    )


def test_enrich_analytics_ack_today_unavailable_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_behavior_counts_zero(monkeypatch)
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    settings = Settings(calendar_timezone="Asia/Jerusalem")
    fake = FakeMetaAdsPort(CampaignInsights(spend="100", clicks="50"))
    enriched, outcome = enrich_analytics_ack(
        ack,
        fake,
        kill_switch=False,
        settings=settings,
    )
    assert outcome.status == "ok"
    assert "היום עד עכשיו" not in enriched
    assert "7d אחרונים:" in enriched
    assert "בלי שינוי תקציב" in enriched


def test_enrich_analytics_ack_comparison_does_not_change_anomaly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_behavior_counts_zero(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        decision = classify_owner_task("how's the campaign spend")
        ack = ack_for_owner_task(decision)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        baseline_range = baseline_7d_time_range(
            now=datetime.now(UTC),
            timezone=settings.calendar_timezone,
        )
        assert baseline_range is not None
        fake = FakeMetaAdsPort(
            CampaignInsights(spend="500", clicks=None),
            today_snapshot=CampaignInsights(spend="14", clicks="7"),
            time_range_snapshots={
                (baseline_range["since"], baseline_range["until"]): CampaignInsights(
                    spend="70",
                    clicks="35",
                ),
            },
        )
        enriched, outcome = enrich_analytics_ack(
            ack,
            fake,
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert outcome.status == "ok"
        assert "בלי קליקים" in enriched
        assert "היום עד עכשיו" in enriched
        rec = store.get_campaign_recommendation(scope="account")
        assert rec is not None
        assert rec.anomaly == "spend_without_clicks"
    finally:
        db.close()
