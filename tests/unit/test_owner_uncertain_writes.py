"""owner_uncertain_writes: stuck provider writes surfaced honestly, never retried."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.db.models import IdempotencyRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_CALENDAR_CREATE,
    ACTION_GMAIL_SEND,
    ACTION_LINKEDIN_COMPOSIO_WRITE,
    DECISION_PENDING,
    RISK_R3,
    approval_expires_at,
)
from app.domain.owner.uncertain_writes import (
    OWNER_UNCERTAIN_WRITES_EMPTY,
    UNCERTAIN_WRITE_GRACE_SECONDS,
    describe_stuck_write,
    list_uncertain_writes,
)
from app.services.owner_actions import propose_owner_action
from app.tools.registries.owner_tools import ToolContext, execute_tool, get_tool, tool_names
from sqlalchemy import select

OWNER_ID = "550077"


def _session():
    init_db()
    return get_session_factory()()


def _principal() -> Principal:
    return Principal.owner(source="telegram", actor_id=OWNER_ID)


def _ctx(session, *, now: datetime) -> ToolContext:
    return ToolContext(
        principal=_principal(),
        store=LeadStore(session),
        brain=BrainStore(session),
        settings=get_settings(),
        embedding_port=FakeEmbeddingPort(),
        source_ref="telegram:test",
        now=now,
    )


def _backdate(session, *, key: str, when: datetime) -> None:
    row = session.scalars(select(IdempotencyRow).where(IdempotencyRow.key == key)).one()
    row.created_at = when.isoformat()
    session.commit()


# ------------------------------------------------------------- registry wiring


def test_owner_uncertain_writes_registered_as_a_read_only_owner_tool() -> None:
    assert "owner_uncertain_writes" in tool_names()
    spec = get_tool("owner_uncertain_writes")
    assert spec is not None
    assert spec.writes_memory is False


def test_owner_uncertain_writes_denied_for_visitor_principal() -> None:
    session = _session()
    try:
        ctx = _ctx(session, now=datetime.now(UTC))
        ctx.principal = Principal.client(source="website")
        result = execute_tool("owner_uncertain_writes", {}, ctx)
        assert result.ok is False
        assert "not available in this state" in result.error
    finally:
        session.close()


# ------------------------------------------------------------- empty / grace period


def test_format_uncertain_writes_empty_list_returns_the_constant() -> None:
    # Pure-function check: the module's DB is shared/StaticPool across the whole
    # test session (see app/db/session.py), so other tests' rows can be present
    # by the time any one test runs -- this is the one assertion that does not
    # depend on global store emptiness.
    from app.domain.owner.uncertain_writes import format_uncertain_writes

    assert format_uncertain_writes([]) == OWNER_UNCERTAIN_WRITES_EMPTY


def test_owner_uncertain_writes_respects_grace_period() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        now = datetime.now(UTC)
        key = "op_grace_test_unique_marker:execute"
        assert store.claim_provider_write(scope="approval", key=key)
        store.mark_provider_write_pending_review(scope="approval", key=key)
        # Fresh claim, no backdating: still inside the grace window, so this
        # specific key must not appear even though the shared table may hold
        # other stuck rows from other tests.
        cutoff = now - timedelta(seconds=UNCERTAIN_WRITE_GRACE_SECONDS)
        fresh_rows = store.list_stuck_provider_writes(older_than=cutoff, limit=10)
        assert key not in {row.key for row in fresh_rows}

        _backdate(
            session,
            key=key,
            when=now - timedelta(seconds=UNCERTAIN_WRITE_GRACE_SECONDS + 30),
        )
        stuck_rows = store.list_stuck_provider_writes(older_than=cutoff, limit=10)
        assert key in {row.key for row in stuck_rows}
    finally:
        session.close()


def test_owner_uncertain_writes_never_mutates_the_row() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        now = datetime.now(UTC)
        key = "op_readonly_test:execute"
        assert store.claim_provider_write(scope="approval", key=key)
        store.mark_provider_write_pending_review(scope="approval", key=key)
        _backdate(session, key=key, when=now - timedelta(minutes=10))
        before = store.get_provider_write_status(scope="approval", key=key)
        list_uncertain_writes(store, now=now, timezone="Asia/Jerusalem")
        list_uncertain_writes(store, now=now, timezone="Asia/Jerusalem")
        after = store.get_provider_write_status(scope="approval", key=key)
        assert before == after == "pending_review"
    finally:
        session.close()


def test_completed_and_failed_writes_are_excluded() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        now = datetime.now(UTC)
        completed_key = "op_completed_test:execute"
        assert store.claim_provider_write(scope="approval", key=completed_key)
        assert store.complete_provider_write(scope="approval", key=completed_key)
        _backdate(session, key=completed_key, when=now - timedelta(minutes=10))

        # "failed" is produced by the separate claim_operation/fail_operation
        # one-shot dedup (in_flight -> failed), not by claim_provider_write -- but
        # it shares the same table/scope, so it must still be excluded here.
        failed_key = "op_failed_test:approval"
        assert store.claim_operation(scope="approval", key=failed_key)
        store.fail_operation(scope="approval", key=failed_key)
        _backdate(session, key=failed_key, when=now - timedelta(minutes=10))

        assert store.get_provider_write_status(scope="approval", key=completed_key) == "completed"
        assert store.get_provider_write_status(scope="approval", key=failed_key) == "failed"
        cutoff = now - timedelta(seconds=UNCERTAIN_WRITE_GRACE_SECONDS)
        stuck_keys = {
            row.key for row in store.list_stuck_provider_writes(older_than=cutoff, limit=10)
        }
        # Neither a completed nor a failed write is ever "stuck" -- the status
        # filter, not the grace period, is what excludes them.
        assert completed_key not in stuck_keys
        assert failed_key not in stuck_keys
    finally:
        session.close()


def test_bounded_to_ten_newest_first() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        now = datetime.now(UTC)
        for i in range(15):
            key = f"op_bound_{i}:execute"
            assert store.claim_provider_write(scope="approval", key=key)
            store.mark_provider_write_pending_review(scope="approval", key=key)
            # Space them out and backdate each past the grace period, oldest first.
            _backdate(
                session,
                key=key,
                when=now - timedelta(minutes=20 - i),
            )
        items = list_uncertain_writes(store, now=now, timezone="Asia/Jerusalem")
        assert len(items) == 10
        # Newest (largest i, smallest age) first.
        times = [item.when_local for item in items]
        assert times == sorted(times, reverse=True)
    finally:
        session.close()


# ------------------------------------------------------------- target resolution


def test_owner_proposal_target_resolves_via_read_owner_action_no_connection_id() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        now = datetime.now(UTC)
        principal = _principal()
        proposal = propose_owner_action(
            store,
            principal=principal,
            source_ref="telegram:test",
            kind="gmail.create_draft",
            parameters={"to": "lead@example.com", "subject": "Hi", "body": "hey"},
            target={
                "recipient": "lead@example.com",
                "provider_binding": {"connected_account_id": "acct_secret_123"},
            },
        )
        key = f"{proposal.proposal_id}:execute"
        assert store.claim_provider_write(scope="approval", key=key)
        store.mark_provider_write_pending_review(scope="approval", key=key)
        _backdate(session, key=key, when=now - timedelta(minutes=10))

        kind, target = describe_stuck_write(store, scope="approval", key=key)
        assert kind == "gmail.create_draft"
        assert target == "lead@example.com"
        assert "acct_secret_123" not in target
    finally:
        session.close()


def test_calendar_create_target_reads_title_and_start_no_dump() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        resource_id = "cal_test_resource_1"
        parameters = json.dumps(
            {
                "end": "2026-09-15T11:00:00+03:00",
                "event_id": "",
                "start": "2026-09-15T10:00:00+03:00",
                "title": "Client call",
                "timezone": "Asia/Jerusalem",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        store.upsert_calendar_approval(
            channel="telegram",
            action=ACTION_CALENDAR_CREATE,
            risk=RISK_R3,
            payload_hash="irrelevant-for-display",
            decision=DECISION_PENDING,
            resource_id=resource_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
            proposed_parameters=parameters,
        )
        key = f"{resource_id}:execute:{ACTION_CALENDAR_CREATE}"
        assert store.claim_provider_write(scope="approval", key=key)
        store.mark_provider_write_pending_review(scope="approval", key=key)

        kind, target = describe_stuck_write(store, scope="approval", key=key)
        assert kind == "Calendar create"
        assert "Client call" in target
        assert "2026-09-15T10:00:00" in target
    finally:
        session.close()


def test_gmail_send_target_is_a_short_draft_reference() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        draft_id = "draft_abc123"
        key = f"{draft_id}:send:{ACTION_GMAIL_SEND}"
        assert store.claim_provider_write(scope="approval", key=key)
        store.mark_provider_write_pending_review(scope="approval", key=key)

        kind, target = describe_stuck_write(store, scope="approval", key=key)
        assert kind == "Gmail send"
        assert draft_id in target
    finally:
        session.close()


def test_linkedin_target_reads_slug_not_arguments() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        resource_id = "li_test_resource_1"
        parameters = json.dumps(
            {"arguments": {"text": "sensitive draft copy"}, "slug": "LINKEDIN_CREATE_POST"},
            sort_keys=True,
            separators=(",", ":"),
        )
        store.upsert_linkedin_approval(
            channel="telegram",
            action=ACTION_LINKEDIN_COMPOSIO_WRITE,
            risk=RISK_R3,
            payload_hash="irrelevant-for-display",
            decision=DECISION_PENDING,
            resource_id=resource_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
            proposed_parameters=parameters,
        )
        key = f"{resource_id}:execute"
        assert store.claim_provider_write(scope="linkedin_approval", key=key)
        store.mark_provider_write_pending_review(scope="linkedin_approval", key=key)

        kind, target = describe_stuck_write(store, scope="linkedin_approval", key=key)
        assert kind == "LinkedIn action"
        assert target == "LINKEDIN_CREATE_POST"
        assert "sensitive draft copy" not in target
    finally:
        session.close()


def test_unresolvable_key_falls_back_to_a_generic_honest_label() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        kind, target = describe_stuck_write(store, scope="owner_task", key="whatever_shape")
        assert kind == "owner_task write"
        assert target == ""
    finally:
        session.close()


# ------------------------------------------------------------- end-to-end via the tool


def test_owner_uncertain_writes_tool_lists_plain_words_target() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        now = datetime.now(UTC)
        principal = _principal()
        proposal = propose_owner_action(
            store,
            principal=principal,
            source_ref="telegram:test",
            kind="gmail.create_draft",
            parameters={"to": "lead@example.com", "subject": "Hi", "body": "hey"},
            target={"recipient": "lead@example.com"},
        )
        key = f"{proposal.proposal_id}:execute"
        assert store.claim_provider_write(scope="approval", key=key)
        store.mark_provider_write_pending_review(scope="approval", key=key)
        # Backdated just past the grace period (not further) so this row is the
        # newest stuck write and always lands inside the tool's top-10, even
        # alongside other tests' older rows in this session's shared store.
        _backdate(
            session,
            key=key,
            when=now - timedelta(seconds=UNCERTAIN_WRITE_GRACE_SECONDS + 5),
        )

        ctx = _ctx(session, now=now)
        result = execute_tool("owner_uncertain_writes", {}, ctx)
        assert result.ok is True
        assert "lead@example.com" in result.text
        assert "gmail.create_draft" in result.text
        assert "pending_review" in result.text
        assert "may or may not have happened" in result.text
        assert "not retry" in result.text
    finally:
        session.close()
