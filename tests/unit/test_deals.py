import importlib
import json

from app.db.models import CanonicalEventRow, DealRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.deals import (
    CONFIDENCE_UNKNOWN,
    STAGE_MEETING_OFFERED,
    STAGE_PROPOSAL,
    apply_deal_policy,
)
from app.domain.events import Channel
from app.domain.sales import NextAction
from sqlalchemy import select

WEB_SESSION_SHEETS = "web_deal_sheet_997009"
WA_PHONE_MEETING = "972509997101"


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


def test_apply_deal_policy_skips_persist_when_killed() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_deal_kill_policy_997264"
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=True,
        )
        db.commit()
        assert _deal_for_lead(db, lead_id) is None
        assert _deal_events_for_lead(db, lead_id) == []
    finally:
        db.close()


def test_deals_module_never_imports_message_port() -> None:
    deals = importlib.import_module("app.domain.deals")
    source = importlib.import_module("inspect").getsource(deals)
    assert "MessagePort" not in source
    assert "integrations.base" not in source


def test_deal_updated_event_payload_has_no_value_keys() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_deal_payload_997341"
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        db.commit()
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
