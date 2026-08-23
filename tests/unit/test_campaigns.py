import json
from datetime import UTC, datetime

import pytest
from app.api.inbound import process_inbound_texts
from app.db.models import CampaignRecommendationRow, CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.campaigns import (
    ANOMALY_CPL_SPIKE,
    ANOMALY_CREATIVE_FATIGUE,
    ANOMALY_INCOMPLETE,
    ANOMALY_NONE,
    ANOMALY_SPEND_UP_CLICKS_DOWN,
    ANOMALY_SPEND_UP_CLICKS_DOWN_30D,
    ANOMALY_SPEND_WITHOUT_CLICKS,
    ANOMALY_SPEND_WITHOUT_LEADS,
    ANOMALY_WEBSITE_FUNNEL_DROP,
    KIND_INVESTIGATE,
    KIND_UNCERTAIN,
    KIND_WATCH,
    analyze_insights,
    apply_campaign_recommendation_policy,
    baseline_7d_time_range,
    format_recommendation_line,
    last_7d_event_bounds,
    previous_7d_event_bounds,
    previous_7d_time_range,
    previous_30d_time_range,
)
from app.domain.events import (
    Channel,
    EventType,
    build_behavior_event,
    build_campaign_recommendation_event,
)
from app.integrations.base import RecordingMessagePort
from app.integrations.meta_ads import CampaignInsights, FakeMetaAdsPort
from app.integrations.sheets import FakeSheetsPort
from sqlalchemy import select

OWNER_SPEND_NO_CLICKS_PHONE = "972509990007"

_FULL_INSIGHTS = CampaignInsights(
    spend="₪1,234",
    impressions="45,678",
    clicks="890",
    ctr="1.95%",
)


def _from_insights(insights: CampaignInsights):
    return analyze_insights(
        spend=insights.spend,
        impressions=insights.impressions,
        clicks=insights.clicks,
        ctr=insights.ctr,
    )


def test_analyze_spend_with_clicks_watch() -> None:
    rec = _from_insights(_FULL_INSIGHTS)
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_spend_without_clicks_string_none() -> None:
    rec = _from_insights(CampaignInsights(spend="₪500", clicks=None))
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_CLICKS


def test_analyze_spend_with_zero_clicks_investigate() -> None:
    rec = _from_insights(CampaignInsights(spend="100", clicks="0"))
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_CLICKS


def test_analyze_all_empty_uncertain() -> None:
    rec = _from_insights(CampaignInsights())
    assert rec.kind == KIND_UNCERTAIN
    assert rec.anomaly == ANOMALY_INCOMPLETE


def test_previous_7d_time_range_friday_jerusalem() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    result = previous_7d_time_range(now=now, timezone="Asia/Jerusalem")
    assert result == {"since": "2026-08-07", "until": "2026-08-13"}


def test_baseline_7d_time_range_friday_jerusalem() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    result = baseline_7d_time_range(now=now, timezone="Asia/Jerusalem")
    assert result == {"since": "2026-08-14", "until": "2026-08-20"}


def test_baseline_7d_time_range_invalid_timezone() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    assert baseline_7d_time_range(now=now, timezone="Not/AZone") is None


def test_last_7d_event_bounds_friday_jerusalem() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    result = last_7d_event_bounds(now=now, timezone="Asia/Jerusalem")
    assert result == (
        "2026-08-14T21:00:00+00:00",
        "2026-08-21T21:00:00+00:00",
    )


def test_previous_7d_event_bounds_friday_jerusalem() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    result = previous_7d_event_bounds(now=now, timezone="Asia/Jerusalem")
    assert result == (
        "2026-08-06T21:00:00+00:00",
        "2026-08-13T21:00:00+00:00",
    )


def test_analyze_spend_without_leads_investigate() -> None:
    rec = analyze_insights(spend="100", clicks="10", lead_count=0)
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_LEADS


def test_analyze_lead_count_none_stays_watch() -> None:
    rec = analyze_insights(spend="100", clicks="10", lead_count=None)
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_spend_without_clicks_wins_over_leads() -> None:
    rec = analyze_insights(spend="100", clicks=None, lead_count=0)
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_CLICKS


def test_analyze_compare_wins_over_leads() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        previous_clicks="50",
        lead_count=0,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN


def test_format_spend_without_leads_line_no_digits() -> None:
    rec = analyze_insights(spend="100", clicks="10", lead_count=0)
    line = format_recommendation_line(rec)
    assert "לידים" in line
    assert "בלי שינוי תקציב" in line
    assert "100" not in line


def test_build_campaign_recommendation_event_spend_without_leads() -> None:
    event = build_campaign_recommendation_event(
        kind="investigate",
        anomaly="spend_without_leads",
    )
    assert event.payload == {"kind": "investigate", "anomaly": "spend_without_leads"}


def test_analyze_cpl_spike_vs_previous() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        previous_clicks="5",
        lead_count=2,
        previous_lead_count=4,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_CPL_SPIKE


def test_analyze_cpl_spike_wins_over_creative_fatigue() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="50",
        ctr="0.8",
        frequency="3",
        previous_spend="100",
        previous_clicks="50",
        previous_ctr="1.9",
        previous_frequency="1.5",
        lead_count=2,
        previous_lead_count=4,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_CPL_SPIKE


def test_analyze_cpl_spike_previous_lead_count_none_watch() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        lead_count=2,
        previous_lead_count=None,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_cpl_spike_previous_lead_count_zero_watch() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        lead_count=2,
        previous_lead_count=0,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_spend_without_clicks_wins_over_cpl() -> None:
    rec = analyze_insights(
        spend="200",
        clicks=None,
        previous_spend="100",
        lead_count=2,
        previous_lead_count=4,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_CLICKS


def test_analyze_compare_wins_over_cpl() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        previous_clicks="50",
        lead_count=2,
        previous_lead_count=4,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN


def test_analyze_lead_count_zero_not_cpl_spike() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        lead_count=0,
        previous_lead_count=4,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_LEADS


def test_format_cpl_spike_line_no_digits() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        previous_clicks="5",
        lead_count=2,
        previous_lead_count=4,
    )
    line = format_recommendation_line(rec)
    assert "עלות ליד" in line
    assert "בלי שינוי תקציב" in line
    assert "200" not in line
    assert "100" not in line


def test_build_campaign_recommendation_event_cpl_spike() -> None:
    event = build_campaign_recommendation_event(
        kind="investigate",
        anomaly="cpl_spike",
    )
    assert event.payload == {"kind": "investigate", "anomaly": "cpl_spike"}


def test_analyze_creative_fatigue_vs_previous() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        ctr="0.8%",
        frequency="3.0",
        previous_spend="90",
        previous_clicks="10",
        previous_ctr="1.9%",
        previous_frequency="1.5",
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_CREATIVE_FATIGUE


def test_analyze_creative_fatigue_previous_frequency_none_watch() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        ctr="0.8%",
        frequency="3.0",
        previous_ctr="1.9%",
        previous_frequency=None,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_creative_fatigue_ctr_up_not_fatigue() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        ctr="2.0%",
        frequency="3.0",
        previous_ctr="1.9%",
        previous_frequency="1.5",
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_spend_without_clicks_wins_over_creative_fatigue() -> None:
    rec = analyze_insights(
        spend="100",
        clicks=None,
        ctr="0.8%",
        frequency="3.0",
        previous_ctr="1.9%",
        previous_frequency="1.5",
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_CLICKS


def test_analyze_compare_wins_over_creative_fatigue() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        previous_clicks="50",
        ctr="0.8%",
        frequency="3.0",
        previous_ctr="1.9%",
        previous_frequency="1.5",
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN


def test_analyze_lead_count_zero_not_creative_fatigue() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        lead_count=0,
        ctr="0.8%",
        frequency="3.0",
        previous_ctr="1.9%",
        previous_frequency="1.5",
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_LEADS


def test_format_creative_fatigue_line_no_digits() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        ctr="0.8%",
        frequency="3.0",
        previous_ctr="1.9%",
        previous_frequency="1.5",
    )
    line = format_recommendation_line(rec)
    assert "תדירות" in line
    assert "קריאייטיב" in line
    assert "בלי שינוי תקציב" in line
    assert "3.0" not in line
    assert "1.5" not in line


def test_build_campaign_recommendation_event_creative_fatigue() -> None:
    event = build_campaign_recommendation_event(
        kind="investigate",
        anomaly="creative_fatigue",
    )
    assert event.payload == {"kind": "investigate", "anomaly": "creative_fatigue"}


def test_analyze_opens_without_conversations_is_funnel_drop() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=3,
        conversation_count=0,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_WEBSITE_FUNNEL_DROP


def test_analyze_opens_zero_stays_watch() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=0,
        conversation_count=0,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_website_funnel_drop_vs_previous() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=10,
        conversation_count=2,
        previous_opened_count=5,
        previous_conversation_count=8,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_WEBSITE_FUNNEL_DROP


def test_analyze_starts_up_not_funnel_drop() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=10,
        conversation_count=8,
        previous_opened_count=5,
        previous_conversation_count=2,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_previous_opened_none_not_funnel_compare() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=10,
        conversation_count=2,
        previous_opened_count=None,
        previous_conversation_count=8,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_previous_opened_none_with_starts_stays_watch() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=10,
        conversation_count=2,
        previous_opened_count=None,
        previous_conversation_count=8,
    )
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_spend_without_clicks_wins_over_opens_without() -> None:
    rec = analyze_insights(
        spend="100",
        clicks=None,
        opened_count=3,
        conversation_count=0,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_CLICKS


def test_analyze_creative_fatigue_wins_over_funnel() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        ctr="0.8%",
        frequency="3.0",
        previous_ctr="1.9%",
        previous_frequency="1.5",
        opened_count=3,
        conversation_count=0,
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_CREATIVE_FATIGUE


def test_format_funnel_lines_no_digits() -> None:
    opens_rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=3,
        conversation_count=0,
    )
    drop_rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=10,
        conversation_count=2,
        previous_opened_count=5,
        previous_conversation_count=8,
    )
    opens_line = format_recommendation_line(opens_rec)
    drop_line = format_recommendation_line(drop_rec)
    assert "משפך" in opens_line
    assert "בלי שינוי תקציב" in opens_line
    assert "3" not in opens_line
    assert "משפך" in drop_line
    assert "בלי שינוי תקציב" in drop_line
    assert "10" not in drop_line
    assert "5" not in drop_line
    assert "8" not in drop_line
    assert "2" not in drop_line


def test_build_campaign_recommendation_event_funnel_anomalies() -> None:
    drop = build_campaign_recommendation_event(
        kind="investigate",
        anomaly="website_funnel_drop",
    )
    assert drop.payload == {
        "kind": "investigate",
        "anomaly": "website_funnel_drop",
    }


def test_count_behavior_events_far_window() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        occurred = datetime(1999, 6, 3, 12, 0, tzinfo=UTC)
        for index, kind in enumerate(
            ("mia_opened", "mia_opened", "conversation_started")
        ):
            event = build_behavior_event(
                session_id=f"beh_far_{index}",
                lead_id=f"lead_beh_far_{index}",
                payload={"kind": kind},
                occurred_at=occurred,
            )
            store.save_canonical_event(provider="website", event=event)
        ignored = build_behavior_event(
            session_id="beh_far_ignored",
            lead_id="lead_beh_far_ignored",
            payload={"kind": "page_viewed", "path": "/demo"},
            occurred_at=occurred,
        )
        store.save_canonical_event(provider="website", event=ignored)
        db.commit()
        window_from = "1999-06-01T00:00:00+00:00"
        window_to = "1999-06-08T00:00:00+00:00"
        assert store.count_behavior_events(
            kind="mia_opened",
            occurred_from=window_from,
            occurred_to=window_to,
        ) == 2
        assert store.count_behavior_events(
            kind="conversation_started",
            occurred_from=window_from,
            occurred_to=window_to,
        ) == 1
        assert store.count_behavior_events(
            kind="unknown_kind",
            occurred_from=window_from,
            occurred_to=window_to,
        ) == 0
    finally:
        db.close()


def test_count_behavior_events_ignores_invalid_json() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        occurred = datetime(1988, 1, 15, 12, 0, tzinfo=UTC)
        event = build_behavior_event(
            session_id="beh_valid_json",
            lead_id="lead_beh_valid_json",
            payload={"kind": "mia_opened"},
            occurred_at=occurred,
        )
        store.save_canonical_event(provider="website", event=event)
        db.add(
            CanonicalEventRow(
                event_id="evt_bad_json",
                provider="website",
                provider_event_id="beh_bad_json",
                event_type="behavior",
                channel="website",
                occurred_at=occurred.isoformat(),
                idempotency_key="beh_bad_json",
                lead_id="lead_bad_json",
                conversation_id="sess_bad_json",
                actor_role="system",
                payload_json="{not-json",
            )
        )
        db.add(
            CanonicalEventRow(
                event_id="evt_non_object_json",
                provider="website",
                provider_event_id="beh_non_object_json",
                event_type="behavior",
                channel="website",
                occurred_at=occurred.isoformat(),
                idempotency_key="beh_non_object_json",
                lead_id="lead_non_object_json",
                conversation_id="sess_non_object_json",
                actor_role="system",
                payload_json='["mia_opened"]',
            )
        )
        db.commit()
        window_from = "1988-01-10T00:00:00+00:00"
        window_to = "1988-01-20T00:00:00+00:00"
        assert store.count_behavior_events(
            kind="mia_opened",
            occurred_from=window_from,
            occurred_to=window_to,
        ) == 1
    finally:
        db.close()


def test_analyze_negative_opened_count_stays_watch() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=-1,
        conversation_count=0,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_negative_conversation_count_skips_opens_without() -> None:
    rec = analyze_insights(
        spend="100",
        clicks="10",
        opened_count=3,
        conversation_count=-1,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_previous_30d_time_range_friday_jerusalem() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    result = previous_30d_time_range(now=now, timezone="Asia/Jerusalem")
    assert result == {"since": "2026-06-22", "until": "2026-07-21"}
    since = datetime.fromisoformat(result["since"]).date()
    until = datetime.fromisoformat(result["until"]).date()
    assert (until - since).days + 1 == 30


def test_analyze_spend_up_clicks_down_vs_previous() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        previous_clicks="50",
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN


def test_analyze_spend_up_clicks_down_30d_vs_previous() -> None:
    rec = analyze_insights(
        spend="400",
        clicks="5",
        previous_spend="200",
        previous_clicks="40",
        compare_window="30d",
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN_30D


def test_analyze_7d_compare_still_spend_up_clicks_down_not_30d() -> None:
    rec = analyze_insights(
        spend="400",
        clicks="5",
        previous_spend="200",
        previous_clicks="40",
        compare_window="7d",
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN


def test_analyze_previous_clicks_missing_not_spend_up_clicks_down() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        previous_clicks=None,
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_previous_30d_clicks_missing_not_spend_up_clicks_down_30d() -> None:
    rec = analyze_insights(
        spend="400",
        clicks="5",
        previous_spend="200",
        previous_clicks=None,
        compare_window="30d",
    )
    assert rec.kind == KIND_WATCH
    assert rec.anomaly == ANOMALY_NONE


def test_analyze_spend_without_clicks_wins_over_compare() -> None:
    rec = analyze_insights(
        spend="200",
        clicks=None,
        previous_spend="100",
        previous_clicks="50",
    )
    assert rec.kind == KIND_INVESTIGATE
    assert rec.anomaly == ANOMALY_SPEND_WITHOUT_CLICKS


def test_format_spend_up_clicks_down_line_no_digits() -> None:
    rec = analyze_insights(
        spend="200",
        clicks="10",
        previous_spend="100",
        previous_clicks="50",
    )
    line = format_recommendation_line(rec)
    assert "הוצאה עלתה וקליקים ירדו" in line
    assert "בלי שינוי תקציב" in line
    assert "200" not in line
    assert "100" not in line
    assert "10" not in line
    assert "50" not in line


def test_format_spend_up_clicks_down_30d_line_no_digits() -> None:
    rec = analyze_insights(
        spend="400",
        clicks="5",
        previous_spend="200",
        previous_clicks="40",
        compare_window="30d",
    )
    line = format_recommendation_line(rec)
    assert "שלושים" in line
    assert "בלי שינוי תקציב" in line
    assert "400" not in line
    assert "200" not in line
    assert "5" not in line
    assert "40" not in line


def test_format_lines_hebrew_markers_no_spend_digits() -> None:
    spend_value = "₪500"
    investigate = format_recommendation_line(
        _from_insights(CampaignInsights(spend=spend_value, clicks=None))
    )
    uncertain = format_recommendation_line(_from_insights(CampaignInsights()))
    watch = format_recommendation_line(_from_insights(_FULL_INSIGHTS))

    assert "בלי קליקים" in investigate
    assert "בלי שינוי תקציב" in investigate
    assert "חסרים מדדים" in uncertain
    assert "בלי שינוי תקציב" in watch

    for line in (investigate, uncertain, watch):
        assert "500" not in line
        assert "1234" not in line


def test_apply_persists_row_and_event_payload_keys_only() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        rec = _from_insights(CampaignInsights(spend="100", clicks="5"))
        apply_campaign_recommendation_policy(store, rec=rec, kill_switch=False)
        db.commit()

        row = store.get_campaign_recommendation()
        assert row is not None
        assert row.scope == "account"
        assert row.kind == KIND_WATCH
        assert row.anomaly == ANOMALY_NONE
        payload = json.loads(row.payload_json)
        assert set(payload.keys()) == {"kind", "anomaly"}

        event = store.get_canonical_event(
            provider="meta",
            provider_event_id="meta:campaign:recommendation",
        )
        assert event is not None
        assert event.event_type == EventType.CAMPAIGN_RECOMMENDATION.value
        event_payload = json.loads(event.payload_json)
        assert set(event_payload.keys()) == {"kind", "anomaly"}
        serialized = json.dumps(event_payload).lower()
        for forbidden in ("@", "email", "phone", "spend"):
            assert forbidden not in serialized
    finally:
        db.close()


def test_apply_kill_switch_skips_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        before_rows = list(db.scalars(select(CampaignRecommendationRow)).all())
        before_events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.provider == "meta",
                    CanonicalEventRow.provider_event_id == "meta:campaign:recommendation",
                )
            ).all()
        )
        rec = _from_insights(CampaignInsights(spend="100", clicks=None))
        apply_campaign_recommendation_policy(store, rec=rec, kill_switch=True)
        db.commit()
        after_rows = list(db.scalars(select(CampaignRecommendationRow)).all())
        after_events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.provider == "meta",
                    CanonicalEventRow.provider_event_id == "meta:campaign:recommendation",
                )
            ).all()
        )
        assert len(after_rows) == len(before_rows)
        assert len(after_events) == len(before_events)
        for row in after_rows:
            assert row.kind != KIND_INVESTIGATE
    finally:
        db.close()


def test_apply_never_calls_message_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        rec = _from_insights(CampaignInsights(spend="200", clicks=None))
        apply_campaign_recommendation_policy(store, rec=rec, kill_switch=False)
        db.commit()
        assert port.sent == []
        assert store.get_campaign_recommendation() is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_analytics_spend_without_clicks_persists_recommendation() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        insights = CampaignInsights(spend="₪500", impressions="1000", clicks=None)
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.meta.noclicks.1",
                    "from": OWNER_SPEND_NO_CLICKS_PHONE,
                    "text": "how's the campaign spend",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_SPEND_NO_CLICKS_PHONE},
            sheets=FakeSheetsPort(),
            meta_ads=FakeMetaAdsPort(insights),
        )
        db.commit()

        sent = port.sent[0].text
        assert "בלי קליקים" in sent
        assert "לא ביצעתי" in sent

        rows = list(db.scalars(select(CampaignRecommendationRow)).all())
        assert len(rows) == 1
        assert rows[0].kind == KIND_INVESTIGATE
        assert rows[0].anomaly == ANOMALY_SPEND_WITHOUT_CLICKS

        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type
                    == EventType.CAMPAIGN_RECOMMENDATION.value
                )
            ).all()
        )
        assert len(events) == 1
    finally:
        db.close()
