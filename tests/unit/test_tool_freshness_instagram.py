import json

import pytest
from app.api.inbound import process_inbound_texts
from app.db.models import ToolRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.content_insights import ContentInsight
from app.domain.events import Channel
from app.domain.owner.tasks import ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.instagram_insights import (
    DisabledInstagramInsightsPort,
    FakeInstagramInsightsPort,
    enrich_content_insights_ack,
)
from sqlalchemy import select

OWNER_FRESH_IG_PHONE = "972509998601"
OWNER_FRESH_IG_EMPTY_PHONE = "972509998602"
MEDIA_ID_1 = "17841400112233445566"
MEDIA_ID_2 = "17841400998877665544"

SAMPLE_ITEMS = [
    ContentInsight(
        media_id=MEDIA_ID_1,
        media_type="IMAGE",
        views="1200",
        reach="900",
        likes="45",
        comments="3",
        saved="12",
    ),
    ContentInsight(
        media_id=MEDIA_ID_2,
        media_type="REELS",
        views="5000",
        reach="4200",
        likes="210",
    ),
]


def test_enrich_content_insights_ack_fake_freshness_cached() -> None:
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
        assert outcome.freshness == "cached"
        assert outcome.status == "ok"
        assert outcome.result_count == 2
        assert "Instagram Insights" in enriched
        dumped = json.dumps(outcome.model_dump()).lower()
        assert "caption" not in dumped
        assert "http" not in dumped
        assert "@" not in dumped
        assert MEDIA_ID_1 not in dumped
    finally:
        db.close()


def test_enrich_content_insights_ack_disabled_freshness_unverified() -> None:
    decision = classify_owner_task("analyze instagram content")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_content_insights_ack(
        ack,
        DisabledInstagramInsightsPort(),
        store=None,
        kill_switch=False,
    )
    assert enriched == ack
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_content_insights_ack_empty_list_freshness_unverified() -> None:
    decision = classify_owner_task("analyze instagram content")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_content_insights_ack(
        ack,
        FakeInstagramInsightsPort([]),
        store=None,
        kill_switch=False,
    )
    assert enriched == ack
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_content_insights_ack_kill_switch_freshness_empty() -> None:
    class RaisingInsightsPort:
        def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]:
            del limit
            raise RuntimeError("must not call port when kill switch is on")

    decision = classify_owner_task("analyze instagram content")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_content_insights_ack(
        ack,
        RaisingInsightsPort(),
        store=None,
        kill_switch=True,
    )
    assert enriched == ack
    assert outcome.freshness == ""
    assert outcome.status == "denied"


def test_enrich_content_insights_ack_http_401_freshness_unverified() -> None:
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
    assert outcome.freshness == "unverified"


@pytest.mark.asyncio
async def test_inbound_ig_freshness_persisted() -> None:
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
                    "id": "ig.fresh.inbound.1",
                    "from": OWNER_FRESH_IG_PHONE,
                    "text": "analyze instagram content",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_IG_PHONE},
            instagram_insights=FakeInstagramInsightsPort(SAMPLE_ITEMS),
        )
        db.commit()
        row = store.get_tool_run("ig.fresh.inbound.1:tool:instagram_insights")
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
        assert "caption" not in dumped
        assert "http" not in dumped
        assert "@" not in dumped
        assert MEDIA_ID_1 not in dumped
        event = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="ig.fresh.inbound.1:tool:instagram_insights",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload == {
            "tool": "instagram_insights",
            "status": "ok",
            "result_count": 2,
        }
        assert "freshness" not in payload
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_ig_empty_freshness_unverified() -> None:
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
                    "id": "ig.fresh.empty.1",
                    "from": OWNER_FRESH_IG_EMPTY_PHONE,
                    "text": "analyze instagram content",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_IG_EMPTY_PHONE},
            instagram_insights=DisabledInstagramInsightsPort(),
        )
        db.commit()
        row = db.scalars(
            select(ToolRunRow).where(
                ToolRunRow.provider_event_id
                == "ig.fresh.empty.1:tool:instagram_insights"
            )
        ).one()
        assert row.freshness == "unverified"
        assert row.status == "empty"
    finally:
        db.close()
