import importlib
import inspect
import json
from datetime import UTC, datetime, timedelta

from app.core.capabilities import CapabilityId, require_alive
from app.core.risk import RiskLevel
from app.db.models import CanonicalEventRow, IdempotencyRow, WebhookEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.calendar_booking import _persist_meeting_booked_event
from app.domain.events import Channel, EventType
from app.domain.idempotency import ALLOWLISTED_OPERATION_SCOPES, IdempotencyStore
from app.domain.policies.execution_policy import ExecutionMode, policy_for
from sqlalchemy import select

WEB_IDEM_EXTERNAL = "web_idem_1"
CAL_IDEM_EXTERNAL = "cal_idem_lead"
WH_EVENT_ID = "evt.idem.wh.1"
WH_PROVIDER = "whatsapp"
WH_TTL_FRESH = "wh.ttl.fresh.1"
WH_TTL_STALE = "wh.ttl.stale.1"
WH_TTL_PROCESSED = "wh.ttl.processed.1"
WH_TTL_SENT = "wh.ttl.sent.1"


def _delete_idempotency_row(db, *, scope: str, key: str) -> None:
    row = db.scalars(
        select(IdempotencyRow).where(
            IdempotencyRow.scope == scope,
            IdempotencyRow.key == key,
        )
    ).one_or_none()
    if row is not None:
        db.delete(row)
        db.flush()


def _delete_webhook(db, *, provider: str, provider_event_id: str) -> None:
    row = db.scalars(
        select(WebhookEventRow).where(
            WebhookEventRow.provider == provider,
            WebhookEventRow.provider_event_id == provider_event_id,
        )
    ).one_or_none()
    if row is not None:
        db.delete(row)
        db.flush()


def test_sheets_mirror_scope_allowlisted() -> None:
    assert "sheets_mirror" in ALLOWLISTED_OPERATION_SCOPES
    assert "follow_up" in ALLOWLISTED_OPERATION_SCOPES
    assert "calendar_cancellation" in ALLOWLISTED_OPERATION_SCOPES


def test_lead_store_satisfies_idempotency_store_protocol() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert isinstance(store, IdempotencyStore)
    finally:
        db.close()


def test_claim_operation_first_true_second_false_same_scope_key() -> None:
    init_db()
    db = get_session_factory()()
    scope = "canonical"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=WEB_IDEM_EXTERNAL
        )
        key = f"{lead_id}:created"
        assert store.claim_operation(scope=scope, key=key) is True
        assert store.claim_operation(scope=scope, key=key) is False
        db.commit()
        row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one()
        datetime.fromisoformat(row.created_at)
    finally:
        if "key" in locals():
            _delete_idempotency_row(db, scope=scope, key=key)
        db.commit()
        db.close()


def test_claim_operation_different_keys_both_true() -> None:
    init_db()
    db = get_session_factory()()
    scope = "approval"
    key_a = "lead_a:approval:proposal_handoff"
    key_b = "lead_b:approval:proposal_handoff"
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope=scope, key=key_a) is True
        assert store.claim_operation(scope=scope, key=key_b) is True
        db.commit()
    finally:
        _delete_idempotency_row(db, scope=scope, key=key_a)
        _delete_idempotency_row(db, scope=scope, key=key_b)
        db.commit()
        db.close()


def test_claim_operation_unknown_scope_false_no_row() -> None:
    init_db()
    db = get_session_factory()()
    scope = "webhook"
    key = "evt.unknown.1"
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope=scope, key=key) is False
        db.commit()
        row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        assert row is None
    finally:
        db.close()


def test_claim_operation_empty_key_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope="canonical", key="") is False
        assert store.claim_operation(scope="", key="some_key") is False
        db.commit()
        assert (
            db.scalars(select(IdempotencyRow).where(IdempotencyRow.key == "")).one_or_none()
            is None
        )
    finally:
        db.close()


def test_claim_webhook_true_then_false_failed_retry_still_true() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert (
            store.claim_webhook(provider=WH_PROVIDER, provider_event_id=WH_EVENT_ID) is True
        )
        assert (
            store.claim_webhook(provider=WH_PROVIDER, provider_event_id=WH_EVENT_ID) is False
        )
        store.mark_webhook(
            provider=WH_PROVIDER,
            provider_event_id=WH_EVENT_ID,
            status="failed",
        )
        assert (
            store.claim_webhook(provider=WH_PROVIDER, provider_event_id=WH_EVENT_ID) is True
        )
        db.commit()
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=WH_EVENT_ID)
        db.commit()
        db.close()


def test_claim_webhook_fresh_received_second_claim_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.claim_webhook(provider=WH_PROVIDER, provider_event_id=WH_TTL_FRESH) is True
        assert store.claim_webhook(provider=WH_PROVIDER, provider_event_id=WH_TTL_FRESH) is False
        db.commit()
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=WH_TTL_FRESH)
        db.commit()
        db.close()


def test_claim_webhook_stale_received_reclaims_and_refreshes_claimed_at() -> None:
    init_db()
    db = get_session_factory()()
    now = datetime.now(UTC)
    stale_claimed_at = (now - timedelta(seconds=301)).isoformat()
    try:
        store = LeadStore(db)
        db.add(
            WebhookEventRow(
                provider=WH_PROVIDER,
                provider_event_id=WH_TTL_STALE,
                status="received",
                claimed_at=stale_claimed_at,
            )
        )
        db.flush()
        assert store.claim_webhook(provider=WH_PROVIDER, provider_event_id=WH_TTL_STALE) is True
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == WH_TTL_STALE,
            )
        ).one()
        assert row.status == "received"
        assert row.claimed_at != stale_claimed_at
        refreshed = datetime.fromisoformat(row.claimed_at)
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
        assert refreshed >= now - timedelta(seconds=5)
        db.commit()
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=WH_TTL_STALE)
        db.commit()
        db.close()


def test_claim_webhook_stale_received_ten_minutes_ago_reclaims() -> None:
    init_db()
    db = get_session_factory()()
    stale_claimed_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    event_id = "wh.ttl.stale.10m.1"
    try:
        store = LeadStore(db)
        db.add(
            WebhookEventRow(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                status="received",
                claimed_at=stale_claimed_at,
            )
        )
        db.flush()
        assert store.claim_webhook(provider=WH_PROVIDER, provider_event_id=event_id) is True
        db.commit()
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=event_id)
        db.commit()
        db.close()


def test_claim_webhook_empty_claimed_at_on_received_reclaims() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "wh.ttl.empty_claimed.1"
    try:
        store = LeadStore(db)
        db.add(
            WebhookEventRow(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                status="received",
                claimed_at="",
            )
        )
        db.flush()
        assert store.claim_webhook(provider=WH_PROVIDER, provider_event_id=event_id) is True
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == event_id,
            )
        ).one()
        assert row.claimed_at != ""
        datetime.fromisoformat(row.claimed_at)
        db.commit()
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=event_id)
        db.commit()
        db.close()


def test_claim_webhook_processed_old_claimed_at_still_false() -> None:
    init_db()
    db = get_session_factory()()
    stale_claimed_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        db.add(
            WebhookEventRow(
                provider=WH_PROVIDER,
                provider_event_id=WH_TTL_PROCESSED,
                status="processed",
                claimed_at=stale_claimed_at,
            )
        )
        db.flush()
        assert (
            store.claim_webhook(provider=WH_PROVIDER, provider_event_id=WH_TTL_PROCESSED)
            is False
        )
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == WH_TTL_PROCESSED,
            )
        ).one()
        assert row.claimed_at == stale_claimed_at
        db.commit()
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=WH_TTL_PROCESSED)
        db.commit()
        db.close()


def test_claim_webhook_sent_old_claimed_at_still_false() -> None:
    init_db()
    db = get_session_factory()()
    stale_claimed_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        db.add(
            WebhookEventRow(
                provider=WH_PROVIDER,
                provider_event_id=WH_TTL_SENT,
                status="sent",
                claimed_at=stale_claimed_at,
            )
        )
        db.flush()
        assert store.claim_webhook(provider=WH_PROVIDER, provider_event_id=WH_TTL_SENT) is False
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == WH_TTL_SENT,
            )
        ).one()
        assert row.claimed_at == stale_claimed_at
        db.commit()
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=WH_TTL_SENT)
        db.commit()
        db.close()


def test_claim_webhook_insert_stores_channel_and_kind() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "envl.wh.insert.1"
    try:
        store = LeadStore(db)
        assert (
            store.claim_webhook(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                channel="whatsapp",
                envelope_kind="text",
            )
            is True
        )
        db.commit()
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == event_id,
            )
        ).one()
        assert row.channel == "whatsapp"
        assert row.envelope_kind == "text"
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=event_id)
        db.commit()
        db.close()


def test_claim_webhook_invalid_channel_stores_empty() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "envl.wh.badchan.1"
    try:
        store = LeadStore(db)
        assert (
            store.claim_webhook(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                channel="sms",
                envelope_kind="text",
            )
            is True
        )
        db.commit()
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == event_id,
            )
        ).one()
        assert row.channel == ""
        assert row.envelope_kind == "text"
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=event_id)
        db.commit()
        db.close()


def test_claim_webhook_duplicate_does_not_rewrite_envelope() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "envl.wh.dup.1"
    try:
        store = LeadStore(db)
        assert (
            store.claim_webhook(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                channel="whatsapp",
                envelope_kind="text",
            )
            is True
        )
        assert (
            store.claim_webhook(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                channel="instagram",
                envelope_kind="audio",
            )
            is False
        )
        db.commit()
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == event_id,
            )
        ).one()
        assert row.channel == "whatsapp"
        assert row.envelope_kind == "text"
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=event_id)
        db.commit()
        db.close()


def test_claim_webhook_failed_reclaim_fills_empty_envelope() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "envl.wh.reclaim.1"
    try:
        store = LeadStore(db)
        assert store.claim_webhook(provider=WH_PROVIDER, provider_event_id=event_id) is True
        store.mark_webhook(provider=WH_PROVIDER, provider_event_id=event_id, status="failed")
        assert (
            store.claim_webhook(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                channel="whatsapp",
                envelope_kind="text",
            )
            is True
        )
        db.commit()
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == event_id,
            )
        ).one()
        assert row.channel == "whatsapp"
        assert row.envelope_kind == "text"
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=event_id)
        db.commit()
        db.close()


def test_claim_webhook_failed_reclaim_keeps_nonempty_envelope() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "envl.wh.keep.1"
    try:
        store = LeadStore(db)
        assert (
            store.claim_webhook(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                channel="whatsapp",
                envelope_kind="text",
            )
            is True
        )
        store.mark_webhook(provider=WH_PROVIDER, provider_event_id=event_id, status="failed")
        assert (
            store.claim_webhook(
                provider=WH_PROVIDER,
                provider_event_id=event_id,
                channel="instagram",
                envelope_kind="audio",
            )
            is True
        )
        db.commit()
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == WH_PROVIDER,
                WebhookEventRow.provider_event_id == event_id,
            )
        ).one()
        assert row.channel == "whatsapp"
        assert row.envelope_kind == "text"
    finally:
        _delete_webhook(db, provider=WH_PROVIDER, provider_event_id=event_id)
        db.commit()
        db.close()


def test_duplicate_persist_meeting_booked_event_writes_one_canonical() -> None:
    init_db()
    db = get_session_factory()()
    scheduled_at = "2026-09-01T10:00:00+00:00"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=CAL_IDEM_EXTERNAL
        )
        db.commit()
        for _ in range(2):
            _persist_meeting_booked_event(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                lead_id=lead_id,
                conversation_id=CAL_IDEM_EXTERNAL,
                scheduled_at=scheduled_at,
            )
        db.commit()
        booked_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.MEETING_BOOKED.value,
                )
            ).all()
        )
        assert len(booked_rows) == 1
        payload = json.loads(booked_rows[0].payload_json)
        parsed = datetime.fromisoformat(payload["scheduled_at"])
        assert parsed.tzinfo is not None
        assert parsed.astimezone(UTC).isoformat() == "2026-09-01T10:00:00+00:00"
        value_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.BUSINESS_VALUE.value,
                    CanonicalEventRow.idempotency_key == f"{lead_id}:value:booked",
                )
            ).all()
        )
        assert len(value_rows) == 1
        idem_row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == "calendar_create",
                IdempotencyRow.key == f"{lead_id}:booked",
            )
        ).one()
        assert idem_row.status == "completed"
        datetime.fromisoformat(idem_row.created_at)
    finally:
        db.close()


def test_idempotency_module_no_forbidden_imports() -> None:
    source = inspect.getsource(importlib.import_module("app.domain.idempotency"))
    for token in ("app.graph", "MessagePort"):
        assert token not in source


def test_require_alive_fde_idempotency() -> None:
    require_alive(CapabilityId.FDE_IDEMPOTENCY)


def test_fde_idempotency_policy_is_deterministic_r1() -> None:
    policy = policy_for(CapabilityId.FDE_IDEMPOTENCY)
    assert policy.execution_mode == ExecutionMode.DETERMINISTIC
    assert policy.risk == RiskLevel.R1_LOW_WRITE


def test_complete_then_claim_false_and_result_ok() -> None:
    init_db()
    db = get_session_factory()()
    scope = "canonical"
    key = "ttl.idem.complete"
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope=scope, key=key) is True
        store.complete_operation(scope=scope, key=key, result_json='{"ok": true}')
        assert store.claim_operation(scope=scope, key=key) is False
        result = json.loads(store.get_operation_result(scope=scope, key=key))
        assert result == {"ok": True}
        db.commit()
    finally:
        _delete_idempotency_row(db, scope=scope, key=key)
        db.commit()
        db.close()


def test_expired_in_flight_reclaim_true() -> None:
    init_db()
    db = get_session_factory()()
    scope = "approval"
    key = "ttl.idem.expired"
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope=scope, key=key) is True
        row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one()
        row.expires_at = "2020-01-01T00:00:00+00:00"
        db.flush()
        assert store.claim_operation(scope=scope, key=key) is True
        db.commit()
        row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one()
        assert row.status == "in_flight"
        assert row.result_json == "{}"
    finally:
        _delete_idempotency_row(db, scope=scope, key=key)
        db.commit()
        db.close()


def test_empty_expires_at_in_flight_claim_false() -> None:
    init_db()
    db = get_session_factory()()
    scope = "owner_task"
    key = "ttl.idem.empty_expires"
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope=scope, key=key) is True
        row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one()
        row.expires_at = ""
        db.flush()
        assert store.claim_operation(scope=scope, key=key) is False
        db.commit()
    finally:
        _delete_idempotency_row(db, scope=scope, key=key)
        db.commit()
        db.close()


def test_failed_status_reclaim_true() -> None:
    init_db()
    db = get_session_factory()()
    scope = "calendar_reschedule"
    key = "ttl.idem.failed"
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope=scope, key=key) is True
        store.fail_operation(scope=scope, key=key)
        assert store.claim_operation(scope=scope, key=key) is True
        db.commit()
        row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one()
        assert row.status == "in_flight"
    finally:
        _delete_idempotency_row(db, scope=scope, key=key)
        db.commit()
        db.close()


def test_complete_operation_rejects_extra_keys_and_message_text() -> None:
    init_db()
    db = get_session_factory()()
    scope = "canonical"
    key_extra = "ttl.idem.extra_keys"
    key_message = "ttl.idem.message_text"
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope=scope, key=key_extra) is True
        store.complete_operation(
            scope=scope,
            key=key_extra,
            result_json='{"ok": true, "message": "secret lead text"}',
        )
        assert json.loads(store.get_operation_result(scope=scope, key=key_extra)) == {}

        assert store.claim_operation(scope=scope, key=key_message) is True
        store.complete_operation(
            scope=scope,
            key=key_message,
            result_json='{"phone": "+972501234567"}',
        )
        assert json.loads(store.get_operation_result(scope=scope, key=key_message)) == {}
        db.commit()
    finally:
        _delete_idempotency_row(db, scope=scope, key=key_extra)
        _delete_idempotency_row(db, scope=scope, key=key_message)
        db.commit()
        db.close()
