import inspect
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.core.config import Settings
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import (
    Channel,
    build_deal_updated_event,
    build_meeting_offered_event,
)
from app.domain.pacing import (
    compute_pacing,
    compute_performance,
    parse_monthly_budget,
)
from app.integrations.base import RecordingMessagePort
from app.integrations.instagram_insights import DisabledInstagramInsightsPort
from app.integrations.meta_ads import CampaignInsights, FakeMetaAdsPort, enrich_analytics_ack
from app.integrations.sheets import (
    BudgetMirrorRow,
    FakeSheetsPort,
    maybe_mirror_campaign_control,
)
from sqlalchemy import select

_AUG_21_JERUSALEM = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
OWNER_SHCAM_PHONE = "972509991201"


class CountingBudgetSheetsPort(FakeSheetsPort):
    def __init__(self) -> None:
        super().__init__()
        self.budget_calls = 0

    def upsert_budget(self, row: BudgetMirrorRow) -> None:
        self.budget_calls += 1
        super().upsert_budget(row)


def test_parse_monthly_budget_rejects_shekel_empty_zero() -> None:
    assert parse_monthly_budget("") is None
    assert parse_monthly_budget("   ") is None
    assert parse_monthly_budget("0") is None
    assert parse_monthly_budget("₪5000") is None
    assert parse_monthly_budget("5,000") is None
    assert parse_monthly_budget("5000.50") == 5000.50


def test_compute_pacing_spend_none_uncertain_remaining_empty() -> None:
    snap = compute_pacing(
        monthly_budget=5000.0,
        spend_mtd=None,
        now=_AUG_21_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert snap.status == "uncertain"
    assert snap.spend == ""
    assert snap.remaining == ""
    assert snap.expected_spend == "3387.10"
    assert snap.projected == ""
    assert snap.over_under == ""


def test_compute_pacing_mid_month_expected_fraction() -> None:
    snap = compute_pacing(
        monthly_budget=3100.0,
        spend_mtd=1000.0,
        now=_AUG_21_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert snap.expected_spend == "2100.00"
    assert snap.spend == "1000.00"


def test_compute_pacing_projected_over_budget() -> None:
    snap = compute_pacing(
        monthly_budget=1000.0,
        spend_mtd=800.0,
        now=_AUG_21_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert snap.status == "over"
    assert snap.projected != ""
    assert float(snap.projected) > 1000.0


def test_compute_pacing_invalid_timezone_uncertain() -> None:
    snap = compute_pacing(
        monthly_budget=5000.0,
        spend_mtd=1000.0,
        now=_AUG_21_JERUSALEM,
        timezone="Not/A_Zone",
    )
    assert snap.status == "uncertain"
    assert snap.expected_spend == ""
    assert snap.projected == ""


def test_enrich_analytics_ack_empty_budget_skips_pacing_persist(monkeypatch) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(campaign_monthly_budget="")
        insights = CampaignInsights(spend="100", clicks="10", impressions="1000", ctr="1")
        enriched, _ = enrich_analytics_ack(
            "ack",
            FakeMetaAdsPort(insights),
            kill_switch=False,
            store=store,
            settings=settings,
        )
        assert enriched.startswith("ack")
        assert store.get_campaign_pacing() is None
        assert store.get_campaign_performance() is None
    finally:
        db.close()


def test_enrich_analytics_ack_budget_persist_and_sheets_mirror() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
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
        enriched, outcome = enrich_analytics_ack(
            "ack",
            FakeMetaAdsPort(last_7d, mtd_snapshot=mtd),
            kill_switch=False,
            store=store,
            settings=settings,
            sheets=sheets,
        )
        assert outcome.status == "ok"
        assert "קצב:" in enriched
        pacing = store.get_campaign_pacing()
        assert pacing is not None
        assert pacing.monthly_budget == "5000.00"
        assert pacing.spend == "1500.00"
        performance = store.get_campaign_performance()
        assert performance is not None
        assert performance.revenue == ""
        assert performance.roas == ""
        assert performance.qualified_cpl == ""
        assert "account" in sheets.budget_rows
        assert sheets.budget_rows["account"].monthly_budget == "5000.00"
        assert "account" in sheets.performance_rows
        perf_row = sheets.performance_rows["account"]
        assert perf_row.revenue == ""
        assert perf_row.roas == ""
        assert perf_row.qualified_cpl == ""
    finally:
        db.close()


def test_campaign_sheets_mirror_claim_skips_second_upsert() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = CountingBudgetSheetsPort()
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
        port = FakeMetaAdsPort(last_7d, mtd_snapshot=mtd)
        inbound_id = "evt.pacing.sheet.claim.1"
        enrich_analytics_ack(
            "ack",
            port,
            kill_switch=False,
            store=store,
            settings=settings,
            sheets=sheets,
            inbound_id=inbound_id,
        )
        assert sheets.budget_calls == 1
        enrich_analytics_ack(
            "ack",
            port,
            kill_switch=False,
            store=store,
            settings=settings,
            sheets=sheets,
            inbound_id=inbound_id,
        )
        assert sheets.budget_calls == 1
    finally:
        db.close()


def test_enrich_analytics_ack_kill_switch_skips_pacing_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(campaign_monthly_budget="5000")
        insights = CampaignInsights(spend="100", clicks="10")
        mtd = CampaignInsights(spend="200", clicks="5", date_preset="this_month")
        enrich_analytics_ack(
            "ack",
            FakeMetaAdsPort(insights, mtd_snapshot=mtd),
            kill_switch=True,
            store=store,
            settings=settings,
        )
        assert store.get_campaign_pacing() is None
        assert store.get_campaign_performance() is None
    finally:
        db.close()


def test_enrich_analytics_ack_campaign_mirror_extra_outcome(monkeypatch) -> None:
    monkeypatch.setattr("app.integrations.sheets.elapsed_ms", lambda _started: 12)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
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
        extras: list = []
        enrich_analytics_ack(
            "ack",
            FakeMetaAdsPort(last_7d, mtd_snapshot=mtd),
            kill_switch=False,
            store=store,
            settings=settings,
            sheets=sheets,
            extra_outcomes=extras,
            inbound_id="shcam.1",
        )
        campaign_extras = [o for o in extras if o.tool == "sheets_mirror_campaign"]
        assert len(campaign_extras) == 1
        outcome = campaign_extras[0]
        assert outcome.status == "ok"
        assert outcome.result_count > 0
        assert outcome.latency_ms == 12
        payload = {
            "tool": outcome.tool,
            "status": outcome.status,
            "result_count": outcome.result_count,
        }
        assert "latency_ms" not in payload
    finally:
        db.close()


def test_enrich_analytics_ack_campaign_mirror_claim_fail_skips_extra(monkeypatch) -> None:
    monkeypatch.setattr("app.integrations.sheets.elapsed_ms", lambda _started: 12)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = CountingBudgetSheetsPort()
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
        port = FakeMetaAdsPort(last_7d, mtd_snapshot=mtd)
        inbound_id = "shcam.2"
        enrich_analytics_ack(
            "ack",
            port,
            kill_switch=False,
            store=store,
            settings=settings,
            sheets=sheets,
            extra_outcomes=[],
            inbound_id=inbound_id,
        )
        extras: list = []
        enrich_analytics_ack(
            "ack",
            port,
            kill_switch=False,
            store=store,
            settings=settings,
            sheets=sheets,
            extra_outcomes=extras,
            inbound_id=inbound_id,
        )
        assert [o.tool for o in extras if o.tool == "sheets_mirror_campaign"] == []
        assert sheets.budget_calls == 1
    finally:
        db.close()


def test_maybe_mirror_campaign_control_kill_switch_denied(monkeypatch) -> None:
    monkeypatch.setattr("app.integrations.sheets.elapsed_ms", lambda _started: 12)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        settings = Settings(
            campaign_monthly_budget="5000",
            calendar_timezone="Asia/Jerusalem",
        )
        pacing = compute_pacing(
            monthly_budget=5000.0,
            spend_mtd=1000.0,
            now=_AUG_21_JERUSALEM,
            timezone="Asia/Jerusalem",
        )
        pacing_row = SimpleNamespace(
            campaign=pacing.campaign,
            monthly_budget=pacing.monthly_budget,
            spend=pacing.spend,
            expected_spend=pacing.expected_spend,
            remaining=pacing.remaining,
            projected=pacing.projected,
            over_under=pacing.over_under,
            status=pacing.status,
        )
        monkeypatch.setattr(store, "get_campaign_pacing", lambda scope="account": pacing_row)
        monkeypatch.setattr(store, "get_campaign_performance", lambda scope="account": None)
        outcome = maybe_mirror_campaign_control(
            store=store,
            sheets=sheets,
            settings=settings,
            kill_switch=True,
            inbound_id="shcam.ks.1",
        )
        assert outcome is not None
        assert outcome.tool == "sheets_mirror_campaign"
        assert outcome.status == "denied"
        assert outcome.result_count == 0
        assert outcome.latency_ms == 12
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_analytics_inbound_persists_campaign_mirror_tool_run(monkeypatch) -> None:
    monkeypatch.setenv("MIA_CAMPAIGN_MONTHLY_BUDGET", "5000")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        last_7d = CampaignInsights(spend="100", clicks="10", impressions="1000", ctr="1.5")
        mtd = CampaignInsights(
            spend="1500",
            clicks="50",
            impressions="5000",
            ctr="1.0",
            date_preset="this_month",
        )
        inbound_id = "shcam.in.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": inbound_id,
                    "from": OWNER_SHCAM_PHONE,
                    "text": "how's the campaign spend",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_SHCAM_PHONE},
            sheets=sheets,
            meta_ads=FakeMetaAdsPort(last_7d, mtd_snapshot=mtd),
            instagram_insights=DisabledInstagramInsightsPort(),
        )
        db.commit()
        row = store.get_tool_run(f"{inbound_id}:tool:sheets_mirror_campaign")
        assert row is not None
        assert row.status == "ok"
        assert row.result_count > 0
        assert store.get_tool_run(f"{inbound_id}:tool:sheets_mirror") is None
        payload = json.loads(
            db.scalar(
                select(CanonicalEventRow.payload_json).where(
                    CanonicalEventRow.provider_event_id
                    == f"{inbound_id}:tool:sheets_mirror_campaign"
                )
            )
        )
        assert payload["tool"] == "sheets_mirror_campaign"
        assert "latency_ms" not in payload
    finally:
        db.close()


def test_pacing_module_has_no_message_port() -> None:
    import app.domain.pacing as pacing_mod

    source = inspect.getsource(pacing_mod)
    assert "MessagePort" not in source


def test_require_alive_campaign_pacing() -> None:
    require_alive(CapabilityId.CAMPAIGN_PACING)


def test_count_canonical_events_in_range_rejects_unknown_type() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert (
            store.count_canonical_events_in_range(
                event_type="behavior",
                occurred_from="1970-01-01T00:00:00+00:00",
                occurred_to="2999-01-01T00:00:00+00:00",
            )
            == 0
        )
    finally:
        db.close()


def test_compute_performance_missing_spend_still_counts_meetings_deals() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        occurred = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
        before = compute_performance(
            store,
            insights=CampaignInsights(clicks="10", ctr="2%"),
            timezone="Asia/Jerusalem",
            now=_AUG_21_JERUSALEM,
        )
        store.save_canonical_event(
            provider="test",
            event=build_meeting_offered_event(
                provider="test",
                channel=Channel.WEBSITE,
                run_id="pacing_perf_seed_run",
                lead_id="pacing_perf_seed_lead",
                conversation_id="pacing_perf_seed_conv",
                occurred_at=occurred,
            ),
        )
        store.save_canonical_event(
            provider="test",
            event=build_deal_updated_event(
                provider="test",
                channel=Channel.WEBSITE,
                lead_id="pacing_perf_seed_lead",
                stage="meeting_offered",
                source="website",
                attribution_confidence="unknown",
                occurred_at=occurred,
            ),
        )
        db.commit()
        snap = compute_performance(
            store,
            insights=CampaignInsights(clicks="10", ctr="2%"),
            timezone="Asia/Jerusalem",
            now=_AUG_21_JERUSALEM,
        )
        assert snap.spend == ""
        assert snap.cpc == ""
        assert snap.cpl == ""
        assert int(snap.meetings) == int(before.meetings) + 1
        assert int(snap.deals) == int(before.deals) + 1
        assert snap.revenue == ""
        assert snap.roas == ""
        assert snap.qualified_cpl == ""
        assert snap.ctr == "2%"
    finally:
        db.close()
