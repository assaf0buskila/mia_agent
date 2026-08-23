import importlib
import json

import pytest
from app.api.deps import get_sheets_port
from app.api.inbound import process_inbound_texts
from app.db.models import CanonicalEventRow, DealRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.deals import (
    CONFIDENCE_UNKNOWN,
    CONFIDENCE_UTM,
    STAGE_MEETING_OFFERED,
    STAGE_PROPOSAL,
    apply_deal_policy,
)
from app.domain.events import Channel
from app.domain.sales import NextAction
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort
from app.integrations.sheets import FakeSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

WEB_SESSION_SHEETS = "web_deal_sheet_997009"
WA_PHONE_MEETING = "972509997101"


def _run_clinic_funnel_to_meeting(client: TestClient, session_id: str) -> str:
    messages = [
        "We run a clinic and miss calls all day.",
        "ok that's right",
        "I decide this quarter",
        "let's book a meeting",
    ]
    lead_id = ""
    for text in messages:
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": text},
        )
        assert response.status_code == 200
        body = response.json()
        lead_id = body["lead_id"]
    assert body["next_action"] == NextAction.OFFER_MEETING.value
    return lead_id


def _deal_for_lead(db, lead_id: str) -> DealRow | None:
    return db.scalars(select(DealRow).where(DealRow.lead_id == lead_id)).one_or_none()


def _deal_events_for_lead(db, lead_id: str) -> list[CanonicalEventRow]:
    return list(
        db.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.lead_id == lead_id,
                CanonicalEventRow.event_type == "deal_updated",
            )
        )
    )


def test_offer_meeting_persists_deal_meeting_offered_unknown_confidence() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        row = _deal_for_lead(db, lead_id)
        assert row is not None
        assert row.stage == STAGE_MEETING_OFFERED
        assert row.expected_value == ""
        assert row.closed_value == ""
        assert row.source == Channel.WEBSITE.value
        assert row.attribution_confidence == CONFIDENCE_UNKNOWN
        events = _deal_events_for_lead(db, lead_id)
        assert len(events) == 1
        assert events[0].provider_event_id == f"{lead_id}:deal:{STAGE_MEETING_OFFERED}"
    finally:
        db.close()


def test_website_utm_then_offer_meeting_confidence_utm() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post(
            "/v1/website/sessions",
            params={"utm_source": "meta", "utm_campaign": "yuma"},
        )
        session_id = created.json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        row = _deal_for_lead(db, lead_id)
        assert row is not None
        assert row.attribution_confidence == CONFIDENCE_UTM
    finally:
        db.close()


def test_proposal_handoff_persists_deal_proposal() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "Please send me a proposal"},
        )
        assert response.status_code == 200
        assert response.json()["next_action"] == NextAction.HANDOFF.value
        lead_id = response.json()["lead_id"]
    db = get_session_factory()()
    try:
        row = _deal_for_lead(db, lead_id)
        assert row is not None
        assert row.stage == STAGE_PROPOSAL
        assert row.expected_value == ""
        assert row.closed_value == ""
    finally:
        db.close()


def test_meeting_offered_then_proposal_upgrades_stage() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
        proposal = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "Please send me a proposal"},
        )
        assert proposal.json()["next_action"] == NextAction.HANDOFF.value
    db = get_session_factory()()
    try:
        row = _deal_for_lead(db, lead_id)
        assert row is not None
        assert row.stage == STAGE_PROPOSAL
        events = _deal_events_for_lead(db, lead_id)
        stages = {json.loads(event.payload_json)["stage"] for event in events}
        assert STAGE_MEETING_OFFERED in stages
        assert STAGE_PROPOSAL in stages
    finally:
        db.close()


def test_proposal_then_offer_meeting_does_not_downgrade() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_deal_nodn_997005"
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            kill_switch=False,
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        db.commit()
        row = _deal_for_lead(db, lead_id)
        assert row is not None
        assert row.stage == STAGE_PROPOSAL
        events = _deal_events_for_lead(db, lead_id)
        assert len(events) == 2
        stages = {json.loads(event.payload_json)["stage"] for event in events}
        assert stages == {STAGE_MEETING_OFFERED, STAGE_PROPOSAL}
    finally:
        db.close()


def test_proposal_first_does_not_emit_meeting_offered_event() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_deal_propfirst_997015"
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            kill_switch=False,
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        db.commit()
        row = _deal_for_lead(db, lead_id)
        assert row is not None
        assert row.stage == STAGE_PROPOSAL
        events = _deal_events_for_lead(db, lead_id)
        assert len(events) == 1
        assert json.loads(events[0].payload_json)["stage"] == STAGE_PROPOSAL
    finally:
        db.close()


def test_stop_action_does_not_persist_deal() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_deal_stop_997006"
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.STOP.value,
            kill_switch=False,
        )
        db.commit()
        assert _deal_for_lead(db, lead_id) is None
        assert _deal_events_for_lead(db, lead_id) == []
    finally:
        db.close()


def test_disqualify_does_not_create_deal() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "I'm a student with a school project"},
        )
        assert response.json()["next_action"] == NextAction.DISQUALIFY.value
        lead_id = response.json()["lead_id"]
    db = get_session_factory()()
    try:
        assert _deal_for_lead(db, lead_id) is None
        assert _deal_events_for_lead(db, lead_id) == []
    finally:
        db.close()


def test_kill_switch_skips_deal_persist(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        assert _deal_for_lead(db, lead_id) is None
        assert _deal_events_for_lead(db, lead_id) == []
    finally:
        db.close()


def test_deals_module_never_imports_message_port() -> None:
    deals = importlib.import_module("app.domain.deals")
    source = importlib.import_module("inspect").getsource(deals)
    assert "MessagePort" not in source
    assert "integrations.base" not in source


def test_fake_sheets_port_after_offer_meeting_has_deal_row_empty_values() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = WEB_SESSION_SHEETS
        store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()

        fake = FakeSheetsPort()
        app.dependency_overrides[get_sheets_port] = lambda: fake
        try:
            with TestClient(app) as client:
                lead_id = _run_clinic_funnel_to_meeting(client, session_id)
                assert lead_id in fake.deal_rows
                deal = fake.deal_rows[lead_id]
                assert deal.stage == STAGE_MEETING_OFFERED
                assert deal.expected_value == ""
                assert deal.closed_value == ""
                assert deal.source == Channel.WEBSITE.value
        finally:
            app.dependency_overrides.pop(get_sheets_port, None)
    finally:
        db.close()


def test_deal_updated_event_payload_has_no_value_keys() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        events = _deal_events_for_lead(db, lead_id)
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload == {
            "stage": STAGE_MEETING_OFFERED,
            "source": Channel.WEBSITE.value,
            "attribution_confidence": CONFIDENCE_UNKNOWN,
        }
        assert "expected_value" not in payload
        assert "closed_value" not in payload
        serialized = json.dumps(events[0].payload_json) + json.dumps(events[0].source_json)
        for forbidden in ("expected_value", "closed_value", "@"):
            assert forbidden not in serialized.lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_whatsapp_clinic_funnel_persists_deal() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        messages = [
            "We run a clinic and miss calls all day.",
            "ok that's right",
            "I decide this quarter",
            "let's book a meeting",
        ]
        for index, text in enumerate(messages):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{"id": f"wamid.deal.{index}", "from": WA_PHONE_MEETING, "text": text}],
                store=store,
                port=port,
                kill_switch=False,
                calendar=DisabledCalendarPort(),
                sheets=FakeSheetsPort(),
            )
            db.commit()
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=WA_PHONE_MEETING
        )
        row = _deal_for_lead(db, lead_id)
        assert row is not None
        assert row.stage == STAGE_MEETING_OFFERED
        assert row.source == Channel.WHATSAPP.value
    finally:
        db.close()
