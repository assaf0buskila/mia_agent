import importlib
import inspect
import json

import pytest
from app.core.config import get_settings
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, EventType, build_business_value_event
from app.domain.followups import STATUS_RECOVERED, apply_follow_up_policy
from app.domain.meetings.booking import _persist_meeting_booked_event
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState
from app.domain.value import ValueKind, count_business_value, persist_business_value
from app.graph.orchestrator import build_graph
from app.graph.state import empty_state
from sqlalchemy import select


def test_build_business_value_event_payload_and_rejects_unknown_kind() -> None:
    event = build_business_value_event(
        provider="website",
        channel=Channel.WEBSITE,
        lead_id="lead_value_build",
        kind=ValueKind.QUALIFIED.value,
        conversation_id="web_value_build",
    )
    assert event.event_type == EventType.BUSINESS_VALUE
    assert event.idempotency_key == "lead_value_build:value:qualified"
    assert event.payload == {"kind": "qualified", "estimated_value_ils": ""}
    assert "₪" not in json.dumps(event.payload)
    with pytest.raises(ValueError, match="unknown business value kind"):
        build_business_value_event(
            provider="website",
            channel=Channel.WEBSITE,
            lead_id="lead_value_build",
            kind="deal_won",
        )


def test_persist_business_value_duplicate_writes_once_and_counts() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_value_dup_1"
        )
        db.commit()
        assert (
            persist_business_value(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                lead_id=lead_id,
                kind=ValueKind.QUALIFIED,
            )
            is True
        )
        assert (
            persist_business_value(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                lead_id=lead_id,
                kind=ValueKind.QUALIFIED,
            )
            is False
        )
        db.commit()
        row = store.get_canonical_event(
            provider="website", provider_event_id=f"{lead_id}:value:qualified"
        )
        assert row is not None
        payload = json.loads(row.payload_json)
        assert payload["estimated_value_ils"] == ""
        assert not isinstance(payload["estimated_value_ils"], (int, float))
        assert count_business_value(store, kind=ValueKind.QUALIFIED, lead_id=lead_id) == 1
    finally:
        db.close()


def test_graph_persists_business_value_qualified_on_fit_transition() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_value_qual_1"
        )
        store.save_sales(
            SalesState(
                lead_id=lead_id,
                fit=FitLevel.POSSIBLE,
                pain_level=PainLevel.P2,
                workflow_known=True,
                buying_reality_known=False,
                authority_known=False,
            )
        )
        db.commit()
        run_id = "run_value_qual_1"
        build_graph(store).invoke(
            empty_state(
                run_id=run_id,
                thread_id="web_value_qual_1",
                channel="website",
                lead_id=lead_id,
                latest_message="i decide",
            )
        )
        db.commit()
        row = store.get_canonical_event(
            provider="website", provider_event_id=f"{lead_id}:value:qualified"
        )
        assert row is not None
        assert row.event_type == EventType.BUSINESS_VALUE.value
        payload = json.loads(row.payload_json)
        assert payload == {"kind": "qualified", "estimated_value_ils": ""}
        qual_row = store.get_canonical_event(
            provider="website", provider_event_id=f"{run_id}:qual"
        )
        assert qual_row is not None
    finally:
        db.close()


def test_graph_persists_business_value_handoff_and_existing_handoff() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_value_hand_1"
        )
        store.save_sales(
            SalesState(lead_id=lead_id, owner_required=True, workflow_known=True)
        )
        db.commit()
        run_id = "run_value_hand_1"
        build_graph(store).invoke(
            empty_state(
                run_id=run_id,
                thread_id="web_value_hand_1",
                channel="website",
                lead_id=lead_id,
                latest_message="ok",
            )
        )
        db.commit()
        handoff_row = store.get_canonical_event(
            provider="website", provider_event_id=f"{run_id}:handoff"
        )
        assert handoff_row is not None
        assert handoff_row.event_type == "handoff"
        value_row = store.get_canonical_event(
            provider="website", provider_event_id=f"{lead_id}:value:handoff"
        )
        assert value_row is not None
        assert value_row.event_type == EventType.BUSINESS_VALUE.value
        assert json.loads(value_row.payload_json) == {
            "kind": "handoff",
            "estimated_value_ils": "",
        }
    finally:
        db.close()


def test_follow_up_recover_persists_business_value_recovered() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_value_rec_1"
        )
        sales = SalesState(
            lead_id=lead_id,
            fit=FitLevel.GOOD,
            workflow_known=True,
            impact_confirmed=True,
            reflected=True,
            hypothesis_offered=True,
            buying_reality_known=True,
            willingness_to_meet=True,
        )
        store.save_sales(sales)
        settings = get_settings()
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDLE_OBJECTION.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        db.commit()
        assert store.get_follow_up(lead_id) is not None
        assert store.get_follow_up(lead_id).status == STATUS_RECOVERED
        row = store.get_canonical_event(
            provider="website", provider_event_id=f"{lead_id}:value:recovered"
        )
        assert row is not None
        assert json.loads(row.payload_json) == {
            "kind": "recovered",
            "estimated_value_ils": "",
        }
    finally:
        db.close()


def test_persist_meeting_booked_also_persists_business_value_booked() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_value_book_1"
        )
        db.commit()
        _persist_meeting_booked_event(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            lead_id=lead_id,
            conversation_id="web_value_book_1",
            scheduled_at="2026-09-01T10:00:00+00:00",
        )
        db.commit()
        booked_row = store.get_canonical_event(
            provider="website", provider_event_id=f"{lead_id}:booked"
        )
        assert booked_row is not None
        assert booked_row.event_type == "meeting_booked"
        value_row = store.get_canonical_event(
            provider="website", provider_event_id=f"{lead_id}:value:booked"
        )
        assert value_row is not None
        assert json.loads(value_row.payload_json) == {
            "kind": "booked",
            "estimated_value_ils": "",
        }
    finally:
        db.close()


def test_value_module_has_no_forbidden_imports() -> None:
    source = inspect.getsource(importlib.import_module("app.domain.value"))
    for token in ("app.graph", "MessagePort", "select_next_action"):
        assert token not in source


def test_estimated_value_ils_never_numeric_in_payload() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_ids: list[str] = []
        for kind in ValueKind:
            _, lead_id = store.open_channel_lead(
                channel=Channel.WEBSITE, external_id=f"web_value_ils_{kind.value}"
            )
            lead_ids.append(lead_id)
            persist_business_value(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                lead_id=lead_id,
                kind=kind,
            )
        db.commit()
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type == EventType.BUSINESS_VALUE.value,
                    CanonicalEventRow.lead_id.in_(lead_ids),
                )
            )
        )
        assert len(rows) == len(ValueKind)
        for row in rows:
            payload = json.loads(row.payload_json)
            assert payload["estimated_value_ils"] == ""
            assert "₪" not in row.payload_json
            assert not isinstance(payload["estimated_value_ils"], (int, float))
    finally:
        db.close()


def test_count_business_value_filters_by_lead_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_a = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_value_cnt_a"
        )
        _, lead_b = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_value_cnt_b"
        )
        persist_business_value(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            lead_id=lead_a,
            kind=ValueKind.QUALIFIED,
        )
        db.commit()
        assert count_business_value(store, kind=ValueKind.QUALIFIED, lead_id=lead_a) == 1
        assert count_business_value(store, kind=ValueKind.QUALIFIED, lead_id=lead_b) == 0
    finally:
        db.close()
