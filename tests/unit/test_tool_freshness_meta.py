import json
from datetime import UTC, datetime

import httpx
import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.models import ToolRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, persist_tool_outcome
from app.domain.owner_tasks import ack_for_owner_task, classify_owner_task
from app.domain.policies.freshness import FreshnessStamp, FreshnessStatus, overlay_stale
from app.domain.tools import ToolOutcome, clamp_tool_freshness
from app.integrations.base import RecordingMessagePort
from app.integrations.meta_ads import (
    CampaignInsights,
    ComposioMetaAdsPort,
    DisabledMetaAdsPort,
    FakeMetaAdsPort,
    campaign_budget_outcome,
    enrich_analytics_ack,
)
from sqlalchemy import select

OWNER_FRESH_PHONE = "972509998501"
OWNER_FRESH_EMPTY_PHONE = "972509998502"
OWNER_FRESH_PACING_PHONE = "972509998503"
SAMPLE_INSIGHTS = CampaignInsights(
    spend="₪1,234",
    impressions="45,678",
    clicks="890",
    ctr="1.95%",
)


def test_overlay_stale_ok_becomes_stale() -> None:
    stamp = FreshnessStamp(
        fact="campaign_metrics",
        source="meta_ads_port",
        fetched_at=datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC),
        version="none",
        status=FreshnessStatus.STALE.value,
    )
    assert overlay_stale(base_status="ok", stamp=stamp) == "stale"


def test_overlay_stale_unauthorized_unchanged() -> None:
    stamp = FreshnessStamp(
        fact="campaign_metrics",
        source="meta_ads_port",
        fetched_at=datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC),
        version="none",
        status=FreshnessStatus.STALE.value,
    )
    assert overlay_stale(base_status="unauthorized", stamp=stamp) == "unauthorized"


def test_enrich_analytics_ack_fake_freshness_cached() -> None:
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_analytics_ack(
        ack, FakeMetaAdsPort(SAMPLE_INSIGHTS), kill_switch=False
    )
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert "7d אחרונים: spend ₪1,234" in enriched


def test_enrich_analytics_ack_disabled_freshness_unverified() -> None:
    decision = classify_owner_task("how's the campaign spend")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_analytics_ack(
        ack, DisabledMetaAdsPort(), kill_switch=False
    )
    assert enriched == ack
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_analytics_ack_kill_switch_freshness_empty() -> None:
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
    enriched, outcome = enrich_analytics_ack(
        ack, RaisingMetaAdsPort(), kill_switch=True
    )
    assert enriched == ack
    assert outcome.freshness == ""
    assert outcome.status == "denied"


def test_enrich_analytics_ack_http_401_freshness_unverified() -> None:
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
    assert outcome.freshness == "unverified"


def test_persist_tool_outcome_writes_freshness_no_payload_key() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        outcome = ToolOutcome(
            tool="meta_ads_insights",
            status="ok",
            result_count=1,
            freshness="cached",
        )
        persist_tool_outcome(
            store,
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            inbound_provider_event_id="tool.fresh.meta.persist.1",
            conversation_id="wa_fresh_1",
            lead_id=None,
            outcome=outcome,
        )
        db.commit()
        row = store.get_tool_run("tool.fresh.meta.persist.1:tool:meta_ads_insights")
        assert row is not None
        assert row.freshness == "cached"
        event = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="tool.fresh.meta.persist.1:tool:meta_ads_insights",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload == {
            "tool": "meta_ads_insights",
            "status": "ok",
            "result_count": 1,
        }
        assert "freshness" not in payload
    finally:
        db.close()


def test_persist_tool_outcome_default_freshness_empty() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="tool.fresh.default.1",
            conversation_id="web_fresh_default",
            lead_id=None,
            outcome=ToolOutcome(
                tool="calendar_find_free_slots", status="ok", result_count=2
            ),
        )
        db.commit()
        row = store.get_tool_run(
            "tool.fresh.default.1:tool:calendar_find_free_slots"
        )
        assert row is not None
        assert row.freshness == ""
    finally:
        db.close()


def test_clamp_tool_freshness_unknown_to_empty() -> None:
    assert clamp_tool_freshness("bogus") == ""


@pytest.mark.asyncio
async def test_inbound_meta_freshness_persisted() -> None:
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
                    "id": "tool.fresh.meta.inbound.1",
                    "from": OWNER_FRESH_PHONE,
                    "text": "how's the campaign spend",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_PHONE},
            meta_ads=FakeMetaAdsPort(SAMPLE_INSIGHTS),
        )
        db.commit()
        row = store.get_tool_run("tool.fresh.meta.inbound.1:tool:meta_ads_insights")
        assert row is not None
        assert row.freshness == "cached"
        assert row.status == "ok"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_meta_empty_freshness_unverified() -> None:
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
                    "id": "tool.fresh.meta.empty.1",
                    "from": OWNER_FRESH_EMPTY_PHONE,
                    "text": "how's the campaign spend",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_EMPTY_PHONE},
            meta_ads=DisabledMetaAdsPort(),
        )
        db.commit()
        row = db.scalars(
            select(ToolRunRow).where(
                ToolRunRow.provider_event_id
                == "tool.fresh.meta.empty.1:tool:meta_ads_insights"
            )
        ).one()
        assert row.freshness == "unverified"
        assert row.status == "empty"
    finally:
        db.close()


def test_campaign_budget_outcome_present_live() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = campaign_budget_outcome(present=True, now=now)
    assert outcome.tool == "meta_ads_pacing"
    assert outcome.freshness == "live"
    assert outcome.status == "ok"


def test_campaign_budget_outcome_missing_unverified() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = campaign_budget_outcome(present=False, now=now)
    assert outcome.tool == "meta_ads_pacing"
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_analytics_ack_budget_extra_outcome_live() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(
            campaign_monthly_budget="5000",
            calendar_timezone="Asia/Jerusalem",
        )
        last_7d = CampaignInsights(spend="100", clicks="10", impressions="1000", ctr="1.5")
        mtd = CampaignInsights(
            spend="1500",
            clicks="50",
            impressions="5000",
            ctr="1.0",
            date_preset="this_month",
        )
        extras: list[ToolOutcome] = []
        enriched, outcome = enrich_analytics_ack(
            "ack",
            FakeMetaAdsPort(last_7d, mtd_snapshot=mtd),
            kill_switch=False,
            store=store,
            settings=settings,
            extra_outcomes=extras,
        )
        assert outcome.freshness == "cached"
        assert "spend 100" in enriched
        assert "קצב:" in enriched
        assert len(extras) == 2
        assert extras[0].tool == "meta_ads_pacing"
        assert extras[0].freshness == "live"
        assert extras[1].tool == "website_session_events"
        assert extras[1].freshness == "cached"
    finally:
        db.close()


def test_enrich_analytics_ack_budget_missing_spend_extra_unverified() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(
            campaign_monthly_budget="5000",
            calendar_timezone="Asia/Jerusalem",
        )
        last_7d = CampaignInsights(spend="100", clicks="10")
        extras: list[ToolOutcome] = []
        enriched, _outcome = enrich_analytics_ack(
            "ack",
            FakeMetaAdsPort(last_7d, mtd_snapshot=None),
            kill_switch=False,
            store=store,
            settings=settings,
            extra_outcomes=extras,
        )
        assert "קצב:" in enriched
        assert len(extras) == 2
        assert extras[0].tool == "meta_ads_pacing"
        assert extras[0].freshness == "unverified"
        assert extras[1].tool == "website_session_events"
        assert extras[1].freshness == "cached"
    finally:
        db.close()


def test_enrich_analytics_ack_no_budget_skips_extra_outcome() -> None:
    last_7d = CampaignInsights(spend="100", clicks="10")
    extras: list[ToolOutcome] = []
    enriched, outcome = enrich_analytics_ack(
        "ack",
        FakeMetaAdsPort(last_7d),
        kill_switch=False,
        settings=Settings(campaign_monthly_budget=""),
        extra_outcomes=extras,
    )
    assert outcome.freshness == "cached"
    assert "קצב:" not in enriched
    assert extras == []


@pytest.mark.asyncio
async def test_inbound_meta_pacing_freshness_persisted(monkeypatch) -> None:
    monkeypatch.setenv("MIA_CAMPAIGN_MONTHLY_BUDGET", "5000")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        last_7d = CampaignInsights(spend="100", clicks="10")
        mtd = CampaignInsights(spend="1500", clicks="50", date_preset="this_month")
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "tool.fresh.pacing.inbound.1",
                    "from": OWNER_FRESH_PACING_PHONE,
                    "text": "how's the campaign spend",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_PACING_PHONE},
            meta_ads=FakeMetaAdsPort(last_7d, mtd_snapshot=mtd),
        )
        db.commit()
        insights_row = store.get_tool_run(
            "tool.fresh.pacing.inbound.1:tool:meta_ads_insights"
        )
        pacing_row = store.get_tool_run(
            "tool.fresh.pacing.inbound.1:tool:meta_ads_pacing"
        )
        assert insights_row is not None
        assert insights_row.freshness == "cached"
        assert pacing_row is not None
        assert pacing_row.freshness == "live"
        assert pacing_row.status == "ok"
    finally:
        db.close()
