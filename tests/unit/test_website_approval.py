from datetime import UTC, datetime

from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_WEBSITE_EDIT,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    RESOURCE_WEBSITE,
    WEBSITE_RESOURCE_ID,
    ack_for_approval_result,
    apply_owner_approval_decision,
    apply_website_edit_approval_policy,
    extract_website_edit_parts,
    website_edit_payload_hash,
)
from app.domain.events import Channel


def _clear_website_approval(store: LeadStore) -> None:
    row = store.get_approval_by_resource(
        RESOURCE_WEBSITE, WEBSITE_RESOURCE_ID, ACTION_WEBSITE_EDIT
    )
    if row is not None:
        store.session.delete(row)
        store.session.flush()


def test_extract_website_edit_parts() -> None:
    before, after = extract_website_edit_parts(
        "propose website change before: Old title after: New title"
    )
    assert before == "Old title"
    assert after == "New title"


def test_website_edit_payload_hash_stable() -> None:
    h1 = website_edit_payload_hash(
        action=ACTION_WEBSITE_EDIT, risk="R3", before="a", after="b"
    )
    h2 = website_edit_payload_hash(
        action=ACTION_WEBSITE_EDIT, risk="R3", before="a", after="b"
    )
    assert h1 == h2
    assert len(h1) == 64


def test_persist_pending_website_approval() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _clear_website_approval(store)
        ok = apply_website_edit_approval_policy(
            store,
            before="Old hero",
            after="New hero",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        assert ok is True
        db.commit()
        row = store.get_approval_by_resource(
            RESOURCE_WEBSITE, WEBSITE_RESOURCE_ID, ACTION_WEBSITE_EDIT
        )
        assert row is not None
        assert row.decision == DECISION_PENDING
        assert row.lead_id is None
    finally:
        db.close()


def test_approve_website_change_persist_only() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _clear_website_approval(store)
        apply_website_edit_approval_policy(
            store,
            before="A",
            after="B",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text="approve website change",
            channel=Channel.WHATSAPP,
            kill_switch=False,
            now=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        )
        db.commit()
        assert result.status == "decided"
        assert result.decision == DECISION_APPROVED
        ack = ack_for_approval_result(result)
        assert "Cursor" in ack
        assert "git-push" in ack
        row = store.get_approval_by_resource(
            RESOURCE_WEBSITE, WEBSITE_RESOURCE_ID, ACTION_WEBSITE_EDIT
        )
        assert row is not None
        assert row.decision == DECISION_APPROVED
    finally:
        db.close()


def test_reject_website_change() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _clear_website_approval(store)
        apply_website_edit_approval_policy(
            store,
            before="A",
            after="B",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text="reject website change",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.decision == DECISION_REJECTED
    finally:
        db.close()


def test_kill_switch_skips_website_approval_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _clear_website_approval(store)
        ok = apply_website_edit_approval_policy(
            store,
            before="A",
            after="B",
            channel=Channel.WHATSAPP,
            kill_switch=True,
        )
        assert ok is False
        row = store.get_approval_by_resource(
            RESOURCE_WEBSITE, WEBSITE_RESOURCE_ID, ACTION_WEBSITE_EDIT
        )
        assert row is None
    finally:
        db.close()
