import json
from datetime import UTC, datetime

import pytest
from app.api.inbound import process_inbound_texts
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner_tasks import ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.linkedin import FakeLinkedInPort, LinkedInProfile
from app.integrations.linkedin_analytics import (
    DisabledLinkedInAnalyticsPort,
    FakeLinkedInAnalyticsPort,
    LinkedInAnalyticsSnapshot,
    enrich_linkedin_analytics_ack,
)
from app.integrations.research import DisabledResearchPort
from app.integrations.sheets import FakeSheetsPort

OWNER_FRESH_LI_PHONE = "972509998611"
OWNER_FRESH_LI_EMPTY_PHONE = "972509998612"
FRIDAY_JERUSALEM = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

SAMPLE_PROFILE = LinkedInProfile(
    name="Assaf Web",
    headline="Growth & Sales Operator at AssafWeb",
)

SAMPLE_SNAPSHOT = LinkedInAnalyticsSnapshot(
    impressions=100,
    members_reached=80,
    reactions=12,
    comments=3,
    reshares=2,
    link_clicks=5,
)


def test_enrich_linkedin_analytics_ack_fake_freshness_cached() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        FakeLinkedInAnalyticsPort(SAMPLE_SNAPSHOT),
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert outcome.result_count == 6
    assert "חשיפות 100" in enriched
    dumped = json.dumps(outcome.model_dump()).lower()
    assert "linkedin.com" not in dumped
    assert "token" not in dumped
    assert "member" not in dumped
    assert "http" not in dumped


def test_enrich_linkedin_analytics_ack_disabled_freshness_unverified() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        DisabledLinkedInAnalyticsPort(),
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert enriched == ack
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_linkedin_analytics_ack_empty_snapshot_freshness_unverified() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        FakeLinkedInAnalyticsPort(LinkedInAnalyticsSnapshot()),
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert enriched == ack
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_linkedin_analytics_ack_kill_switch_freshness_empty() -> None:
    class RaisingAnalyticsPort:
        def get_member_analytics(self, *, start, end):
            del start, end
            raise RuntimeError("must not call port when kill switch is on")

    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        RaisingAnalyticsPort(),
        kill_switch=True,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert enriched == ack
    assert outcome.freshness == ""
    assert outcome.status == "denied"


def test_enrich_linkedin_analytics_ack_http_401_freshness_unverified() -> None:
    class HttpErrorAnalyticsPort:
        def get_member_analytics(self, *, start, end):
            del start, end
            raise AdapterHttpError(401)

    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        HttpErrorAnalyticsPort(),
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert enriched == ack
    assert outcome.status == "unauthorized"
    assert outcome.freshness == "unverified"


def test_enrich_linkedin_analytics_ack_partial_freshness_cached() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    partial = LinkedInAnalyticsSnapshot(impressions=100, reactions=12)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        FakeLinkedInAnalyticsPort(partial),
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert "חשיפות 100" in enriched
    assert outcome.status == "partial"
    assert outcome.freshness == "cached"
    assert outcome.result_count == 2


@pytest.mark.asyncio
async def test_inbound_linkedin_freshness_persisted() -> None:
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
                    "id": "li.fresh.inbound.1",
                    "from": OWNER_FRESH_LI_PHONE,
                    "text": "how's my linkedin",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_LI_PHONE},
            sheets=FakeSheetsPort(),
            linkedin=FakeLinkedInPort(SAMPLE_PROFILE),
            linkedin_analytics=FakeLinkedInAnalyticsPort(SAMPLE_SNAPSHOT),
            research=DisabledResearchPort(),
        )
        db.commit()
        row = store.get_tool_run("li.fresh.inbound.1:tool:linkedin_analytics")
        assert row is not None
        assert row.freshness == "cached"
        assert row.status == "ok"
        dumped = json.dumps(
            {
                "tool": row.tool,
                "status": row.status,
                "result_count": row.result_count,
                "freshness": row.freshness,
            }
        ).lower()
        assert "linkedin.com" not in dumped
        assert "token" not in dumped
        assert "member" not in dumped
        assert "http" not in dumped
        event = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="li.fresh.inbound.1:tool:linkedin_analytics",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload == {
            "tool": "linkedin_analytics",
            "status": "ok",
            "result_count": 6,
        }
        assert "freshness" not in payload
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_linkedin_empty_freshness_unverified() -> None:
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
                    "id": "li.fresh.empty.1",
                    "from": OWNER_FRESH_LI_EMPTY_PHONE,
                    "text": "how's my linkedin",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_LI_EMPTY_PHONE},
            linkedin=FakeLinkedInPort(SAMPLE_PROFILE),
            linkedin_analytics=DisabledLinkedInAnalyticsPort(),
            research=DisabledResearchPort(),
        )
        db.commit()
        row = store.get_tool_run("li.fresh.empty.1:tool:linkedin_analytics")
        assert row is not None
        assert row.freshness == "unverified"
        assert row.status == "empty"
    finally:
        db.close()
