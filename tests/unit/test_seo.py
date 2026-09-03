import json

import pytest
from app.api.inbound import process_inbound_texts
from app.capabilities.types import Principal
from app.db.models import CanonicalEventRow, SeoRecommendationRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.domain.seo import enrich_seo_ack
from app.integrations.base import RecordingMessagePort
from app.integrations.ga4 import DisabledGa4Port, FakeGa4Port, Ga4PivotRow
from app.integrations.research import DisabledResearchPort
from app.integrations.search_console import (
    DisabledSearchConsolePort,
    FakeSearchConsolePort,
    SearchAnalyticsRow,
)
from app.integrations.seo_audit import FakeSeoAuditPort, SeoAuditSnapshot
from app.integrations.sheets import FakeSheetsPort
from sqlalchemy import select

OWNER_SEO_PHONE = "972509990020"
_OWNER = Principal.owner(source="test")


def test_classify_seo_phrases() -> None:
    decision = classify_owner_task("check seo on the site")
    assert decision.task_type == OwnerTaskType.SEO
    assert decision.needs_clarification is False
    he = classify_owner_task("בדיקת seo לאתר")
    assert he.task_type == OwnerTaskType.SEO
    assert classify_owner_task("how is ga4 traffic this week").task_type == OwnerTaskType.SEO
    assert classify_owner_task("גוגל אנליטיקס לאתר").task_type == OwnerTaskType.SEO
    assert classify_owner_task("google search console clicks").task_type == OwnerTaskType.SEO


def test_classify_bare_ctr_stays_analytics() -> None:
    decision = classify_owner_task("how is campaign ctr")
    assert decision.task_type == OwnerTaskType.ANALYTICS


def test_enrich_seo_ack_fake_appends_facts_and_website_line() -> None:
    decision = classify_owner_task("check seo")
    ack = ack_for_owner_task(decision)
    assert "לא אשנה את האתר בלי אישור" in ack
    gsc = FakeSearchConsolePort(
        analytics_rows=[
            SearchAnalyticsRow(page="/", impressions="500", ctr="0.01", clicks="5"),
            SearchAnalyticsRow(page="/about", impressions="400", ctr="0.05", clicks="20"),
        ]
    )
    ga4 = FakeGa4Port(
        pivot_rows=[Ga4PivotRow(landing_page="/", sessions="100")],
        conversion_events=["generate_lead"],
    )
    audit = FakeSeoAuditPort(
        SeoAuditSnapshot(url="https://www.assafweb.com/", title="AssafWeb", h1_count=1)
    )
    enriched, outcomes = enrich_seo_ack(
        ack, gsc, ga4, audit, principal=_OWNER, kill_switch=False
    )
    assert "נתוני חיפוש (GSC)" in enriched
    assert "תנועה (GA4)" in enriched
    assert "ביקורת דף בית" in enriched
    assert "numbers from the API" in enriched
    assert "GA4 property" in enriched
    assert "GSC" in enriched
    assert "המלצה:" in enriched
    assert len(outcomes) == 3
    assert all(outcome.status == "ok" for outcome in outcomes)


def test_enrich_seo_ack_disabled_no_fake_metrics() -> None:
    decision = classify_owner_task("search console")
    ack = ack_for_owner_task(decision)
    enriched, outcomes = enrich_seo_ack(
        ack,
        DisabledSearchConsolePort(),
        DisabledGa4Port(),
        FakeSeoAuditPort(None),
        principal=_OWNER,
        kill_switch=False,
    )
    assert "CTR 0" not in enriched
    assert "סשנים 0" not in enriched
    assert outcomes[0].status == "empty"
    assert outcomes[1].status == "empty"


def test_enrich_seo_ack_kill_switch_skips_ports() -> None:
    class RaisingPort:
        def query_search_analytics(self, **_kwargs):
            raise RuntimeError("must not call")

        def list_sites(self):
            raise RuntimeError("must not call")

        def inspect_url(self, _url):
            raise RuntimeError("must not call")

    ack = ack_for_owner_task(classify_owner_task("seo audit"))
    enriched, outcomes = enrich_seo_ack(
        ack,
        RaisingPort(),  # type: ignore[arg-type]
        DisabledGa4Port(),
        FakeSeoAuditPort(None),
        principal=_OWNER,
        kill_switch=True,
    )
    assert enriched == ack
    assert outcomes[0].status == "denied"


@pytest.mark.asyncio
async def test_owner_seo_inbound_fake_ports() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        gsc = FakeSearchConsolePort(
            analytics_rows=[
                SearchAnalyticsRow(page="/", impressions="100", ctr="0.02", clicks="2"),
                SearchAnalyticsRow(page="/x", impressions="100", ctr="0.08", clicks="8"),
            ]
        )
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "evt.owner.seo.1",
                "from": OWNER_SEO_PHONE,
                "text": "check seo",
                "source": "audio",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_SEO_PHONE},
            sheets=FakeSheetsPort(),
            research=DisabledResearchPort(),
            search_console=gsc,
            ga4=FakeGa4Port(pivot_rows=[Ga4PivotRow(landing_page="/", sessions="50")]),
            seo_audit=FakeSeoAuditPort(
                SeoAuditSnapshot(url="https://www.assafweb.com/", title="T", h1_count=1)
            ),
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.seo.1"
        )
        assert task is not None
        assert task.task_type == "seo"
        sent = port.sent[0].text
        assert "לא ביצעתי" in sent
        assert "לא אשנה את האתר בלי אישור" in sent
        assert "נתוני חיפוש (GSC)" in sent
        rec = db.scalars(select(SeoRecommendationRow)).one_or_none()
        assert rec is not None
        tool_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.provider_event_id.like("evt.owner.seo.1:tool:%")
                )
            )
        )
        assert len(tool_rows) >= 2
        for row in tool_rows:
            payload = json.loads(row.payload_json)
            assert "text" not in payload
    finally:
        db.close()
