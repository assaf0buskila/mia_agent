import json

import pytest
from app.api.inbound import process_inbound_texts
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner_tasks import ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.linkedin import (
    DisabledLinkedInPort,
    FakeLinkedInPort,
    LinkedInProfile,
    enrich_linkedin_ack,
)
from app.integrations.research import DisabledResearchPort
from app.integrations.sheets import FakeSheetsPort

OWNER_FRESH_LI_PROFILE_PHONE = "972509998621"

SAMPLE_PROFILE = LinkedInProfile(
    name="Assaf Web",
    headline="Growth & Sales Operator at AssafWeb",
)


def test_enrich_linkedin_ack_fake_freshness_cached() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_ack(
        ack,
        FakeLinkedInPort(SAMPLE_PROFILE),
        kill_switch=False,
    )
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert outcome.result_count == 1
    assert "פרופיל" in enriched
    dumped = json.dumps(outcome.model_dump()).lower()
    assert "linkedin.com" not in dumped
    assert "token" not in dumped
    assert "http" not in dumped
    assert "url" not in dumped


def test_enrich_linkedin_ack_disabled_freshness_unverified() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_ack(
        ack,
        DisabledLinkedInPort(),
        kill_switch=False,
    )
    assert enriched == ack
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_linkedin_ack_kill_switch_freshness_empty() -> None:
    class RaisingLinkedInPort:
        def get_my_profile(self) -> LinkedInProfile | None:
            raise RuntimeError("must not call port when kill switch is on")

    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_ack(
        ack,
        RaisingLinkedInPort(),
        kill_switch=True,
    )
    assert enriched == ack
    assert outcome.freshness == ""
    assert outcome.status == "denied"


def test_enrich_linkedin_ack_http_401_freshness_unverified() -> None:
    class HttpErrorLinkedInPort:
        def get_my_profile(self) -> LinkedInProfile | None:
            raise AdapterHttpError(401)

    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_ack(
        ack,
        HttpErrorLinkedInPort(),
        kill_switch=False,
    )
    assert enriched == ack
    assert outcome.status == "unauthorized"
    assert outcome.freshness == "unverified"


@pytest.mark.asyncio
async def test_inbound_linkedin_profile_freshness_persisted() -> None:
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
                    "id": "li.prof.fresh.inbound.1",
                    "from": OWNER_FRESH_LI_PROFILE_PHONE,
                    "text": "how's my linkedin",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_LI_PROFILE_PHONE},
            sheets=FakeSheetsPort(),
            linkedin=FakeLinkedInPort(SAMPLE_PROFILE),
            research=DisabledResearchPort(),
        )
        db.commit()
        row = store.get_tool_run("li.prof.fresh.inbound.1:tool:linkedin_profile")
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
        assert "http" not in dumped
        assert "url" not in dumped
        event = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="li.prof.fresh.inbound.1:tool:linkedin_profile",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload == {
            "tool": "linkedin_profile",
            "status": "ok",
            "result_count": 1,
        }
        assert "freshness" not in payload
        assert "Assaf" not in json.dumps(payload)
    finally:
        db.close()
