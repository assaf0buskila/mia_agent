import inspect
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from app.core.capabilities import CapabilityId, require_alive
from app.db.models import HandoffTokenRow, WebhookEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, build_message_out_event
from app.domain.reconciliation import (
    evaluate_reconciliation,
    inspect_open_findings,
    run_reconciliation,
)
from app.workers import reconcile as reconcile_module
from app.workers.reconcile import main
from sqlalchemy import select

PROVIDER = "whatsapp"
STALE_EVENT_ID = "evt.recon.wh.1"
RECENT_EVENT_ID = "evt.recon.wh.2"
SENT_EVENT_ID = "evt.recon.wh.sent.1"
CLAIM_EVENT_ID = "evt.recon.wh.claim.1"
MARK_EVENT_ID = "evt.recon.wh.mark.1"
INSPECT_EVENT_ID = "evt.recon.inspect.1"
INSPECT_EVENT_ID_2 = "evt.recon.inspect.2"
HANDOFF_SESSION = "recon_handoff_session_1"
HANDOFF_PHONE = "972509994501"
_FIXED_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _seed_webhook(
    store: LeadStore,
    *,
    provider: str,
    provider_event_id: str,
    status: str,
    claimed_at: str = "",
    channel: str = "",
    envelope_kind: str = "",
) -> None:
    store.session.add(
        WebhookEventRow(
            provider=provider,
            provider_event_id=provider_event_id,
            status=status,
            claimed_at=claimed_at,
            channel=channel,
            envelope_kind=envelope_kind,
        )
    )
    store.session.flush()


def _delete_webhook(store: LeadStore, *, provider: str, provider_event_id: str) -> None:
    row = store.session.scalars(
        select(WebhookEventRow).where(
            WebhookEventRow.provider == provider,
            WebhookEventRow.provider_event_id == provider_event_id,
        )
    ).one_or_none()
    if row is not None:
        store.session.delete(row)
        store.session.flush()


def _delete_finding(store: LeadStore, *, kind: str, subject_key: str) -> None:
    row = store.get_reconciliation_finding(kind=kind, subject_key=subject_key)
    if row is not None:
        store.session.delete(row)
        store.session.flush()


def _delete_handoff_by_session(store: LeadStore, session_id: str) -> None:
    rows = list(
        store.session.scalars(
            select(HandoffTokenRow).where(
                HandoffTokenRow.website_session_id == session_id
            )
        ).all()
    )
    for row in rows:
        store.session.delete(row)
        store.session.flush()


def test_claim_webhook_sets_claimed_at_non_empty() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        claimed = store.claim_webhook(provider=PROVIDER, provider_event_id=CLAIM_EVENT_ID)
        assert claimed is True
        row = store.session.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == PROVIDER,
                WebhookEventRow.provider_event_id == CLAIM_EVENT_ID,
            )
        ).one()
        assert row.claimed_at != ""
        datetime.fromisoformat(row.claimed_at)
    finally:
        _delete_webhook(store, provider=PROVIDER, provider_event_id=CLAIM_EVENT_ID)
        db.commit()
        db.close()


def test_mark_webhook_rejects_unknown_status_row_unchanged() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        store.claim_webhook(provider=PROVIDER, provider_event_id=MARK_EVENT_ID)
        store.mark_webhook(
            provider=PROVIDER,
            provider_event_id=MARK_EVENT_ID,
            status="bogus_status",
        )
        row = store.session.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == PROVIDER,
                WebhookEventRow.provider_event_id == MARK_EVENT_ID,
            )
        ).one()
        assert row.status == "received"
    finally:
        _delete_webhook(store, provider=PROVIDER, provider_event_id=MARK_EVENT_ID)
        db.commit()
        db.close()


def test_stale_received_webhook_finding() -> None:
    init_db()
    db = get_session_factory()()
    stale_subject = f"{PROVIDER}:{STALE_EVENT_ID}"
    recent_subject = f"{PROVIDER}:{RECENT_EVENT_ID}"
    stale_claimed = (_FIXED_NOW - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=STALE_EVENT_ID,
            status="received",
            claimed_at=stale_claimed,
        )
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=RECENT_EVENT_ID,
            status="received",
            claimed_at=_FIXED_NOW.isoformat(),
        )
        db.commit()
        findings = evaluate_reconciliation(store, now=_FIXED_NOW)
        stale = [item for item in findings if item.subject_key == stale_subject]
        recent = [item for item in findings if item.subject_key == recent_subject]
        assert len(stale) == 1
        assert stale[0].kind == "webhook_received"
        assert len(recent) == 0
        summary = run_reconciliation(
            store, kill_switch=False, demo_active=False, now=_FIXED_NOW
        )
        assert summary.webhook_received >= 1
        finding = store.get_reconciliation_finding(
            kind="webhook_received", subject_key=stale_subject
        )
        assert finding is not None
        assert finding.open is True
    finally:
        _delete_finding(store, kind="webhook_received", subject_key=stale_subject)
        _delete_webhook(store, provider=PROVIDER, provider_event_id=STALE_EVENT_ID)
        _delete_webhook(store, provider=PROVIDER, provider_event_id=RECENT_EVENT_ID)
        db.commit()
        db.close()


def test_sent_without_out_finding_cleared_by_out_event() -> None:
    init_db()
    db = get_session_factory()()
    subject_key = f"{PROVIDER}:{SENT_EVENT_ID}"
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=SENT_EVENT_ID,
            status="sent",
        )
        db.commit()
        findings = evaluate_reconciliation(store, now=_FIXED_NOW)
        matched = [item for item in findings if item.subject_key == subject_key]
        assert len(matched) == 1
        assert matched[0].kind == "sent_without_out"
        run_reconciliation(store, kill_switch=False, demo_active=False, now=_FIXED_NOW)
        db.commit()
        assert (
            store.get_reconciliation_finding(
                kind="sent_without_out", subject_key=subject_key
            )
            is not None
        )
        store.save_canonical_event(
            provider=PROVIDER,
            event=build_message_out_event(
                provider=PROVIDER,
                channel=Channel.WHATSAPP,
                inbound_provider_event_id=SENT_EVENT_ID,
                conversation_id=HANDOFF_PHONE,
                text="reply",
            ),
        )
        db.commit()
        rescanned = evaluate_reconciliation(store, now=_FIXED_NOW)
        rematched = [item for item in rescanned if item.subject_key == subject_key]
        assert len(rematched) == 0
        run_reconciliation(store, kill_switch=False, demo_active=False, now=_FIXED_NOW)
        db.commit()
        closed = store.get_reconciliation_finding(
            kind="sent_without_out", subject_key=subject_key
        )
        assert closed is not None
        assert closed.open is False
    finally:
        _delete_finding(store, kind="sent_without_out", subject_key=subject_key)
        out_row = store.get_canonical_event(
            provider=PROVIDER, provider_event_id=f"{SENT_EVENT_ID}:out"
        )
        if out_row is not None:
            store.session.delete(out_row)
        _delete_webhook(store, provider=PROVIDER, provider_event_id=SENT_EVENT_ID)
        db.commit()
        db.close()


def test_expired_unconsumed_handoff_finding_uses_token_hash() -> None:
    init_db()
    db = get_session_factory()()
    token_hash = ""
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=HANDOFF_SESSION
        )
        store.issue_handoff_token(lead_id, HANDOFF_SESSION)
        row = db.scalars(
            select(HandoffTokenRow).where(HandoffTokenRow.lead_id == lead_id)
        ).one()
        token_hash = row.token_hash
        row.expires_at = (_FIXED_NOW - timedelta(minutes=5)).isoformat()
        db.commit()
        findings = evaluate_reconciliation(store, now=_FIXED_NOW)
        matched = [item for item in findings if item.subject_key == token_hash]
        assert len(matched) == 1
        assert matched[0].kind == "handoff_expired"
        assert matched[0].subject_key == token_hash
        assert "mia1_" not in matched[0].subject_key
        run_reconciliation(store, kill_switch=False, demo_active=False, now=_FIXED_NOW)
        db.commit()
        finding = store.get_reconciliation_finding(
            kind="handoff_expired", subject_key=token_hash
        )
        assert finding is not None
    finally:
        if token_hash:
            _delete_finding(store, kind="handoff_expired", subject_key=token_hash)
        _delete_handoff_by_session(store, HANDOFF_SESSION)
        db.commit()
        db.close()


def test_kill_switch_skips_persist() -> None:
    init_db()
    db = get_session_factory()()
    subject_key = f"{PROVIDER}:{STALE_EVENT_ID}"
    stale_claimed = (_FIXED_NOW - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=STALE_EVENT_ID,
            status="received",
            claimed_at=stale_claimed,
        )
        db.commit()
        before = store.get_reconciliation_finding(
            kind="webhook_received", subject_key=subject_key
        )
        before_open = None if before is None else before.open
        summary = run_reconciliation(
            store, kill_switch=True, demo_active=False, now=_FIXED_NOW
        )
        assert summary.webhook_received >= 1
        db.commit()
        after = store.get_reconciliation_finding(
            kind="webhook_received", subject_key=subject_key
        )
        if before is None:
            assert after is None
        else:
            assert after is not None
            assert after.open is before_open
    finally:
        _delete_webhook(store, provider=PROVIDER, provider_event_id=STALE_EVENT_ID)
        db.commit()
        db.close()


def test_demo_skips_persist() -> None:
    init_db()
    db = get_session_factory()()
    subject_key = f"{PROVIDER}:{STALE_EVENT_ID}"
    stale_claimed = (_FIXED_NOW - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=STALE_EVENT_ID,
            status="received",
            claimed_at=stale_claimed,
        )
        db.commit()
        before = store.get_reconciliation_finding(
            kind="webhook_received", subject_key=subject_key
        )
        before_open = None if before is None else before.open
        summary = run_reconciliation(
            store, kill_switch=False, demo_active=True, now=_FIXED_NOW
        )
        assert summary.webhook_received >= 1
        db.commit()
        after = store.get_reconciliation_finding(
            kind="webhook_received", subject_key=subject_key
        )
        if before is None:
            assert after is None
        else:
            assert after is not None
            assert after.open is before_open
    finally:
        _delete_webhook(store, provider=PROVIDER, provider_event_id=STALE_EVENT_ID)
        db.commit()
        db.close()


def test_run_reconciliation_summary_counts_and_worker_has_no_message_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        summary = run_reconciliation(
            store, kill_switch=False, demo_active=False, now=_FIXED_NOW
        )
        dumped = summary.model_dump()
        assert set(dumped.keys()) == {
            "webhook_received",
            "sent_without_out",
            "handoff_expired",
        }
        for value in dumped.values():
            assert isinstance(value, int)
            assert value >= 0
    finally:
        db.close()
    source = inspect.getsource(reconcile_module)
    assert "MessagePort" not in source
    assert "app.integrations.base" not in source


def test_require_alive_reconciliation() -> None:
    require_alive(CapabilityId.RECONCILIATION)


def test_inspect_open_findings_returns_kind_and_subject() -> None:
    init_db()
    db = get_session_factory()()
    subject_key = f"{PROVIDER}:{INSPECT_EVENT_ID}"
    stale_claimed = (_FIXED_NOW - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=INSPECT_EVENT_ID,
            status="received",
            claimed_at=stale_claimed,
        )
        db.commit()
        run_reconciliation(
            store, kill_switch=False, demo_active=False, now=_FIXED_NOW
        )
        db.commit()
        findings = inspect_open_findings(store)
        matched = [item for item in findings if item.subject_key == subject_key]
        assert len(matched) == 1
        assert matched[0].kind == "webhook_received"
        assert matched[0].subject_key == subject_key
        assert matched[0].channel == ""
        assert matched[0].envelope_kind == ""
    finally:
        _delete_finding(store, kind="webhook_received", subject_key=subject_key)
        _delete_webhook(store, provider=PROVIDER, provider_event_id=INSPECT_EVENT_ID)
        db.commit()
        db.close()


def test_main_inspect_stdout_includes_open_findings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_db()
    db = get_session_factory()()
    subject_key = f"{PROVIDER}:{INSPECT_EVENT_ID_2}"
    stale_claimed = (_FIXED_NOW - timedelta(minutes=10)).isoformat()
    original_argv = sys.argv
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=INSPECT_EVENT_ID_2,
            status="received",
            claimed_at=stale_claimed,
        )
        db.commit()
    finally:
        db.close()
    try:
        sys.argv = ["mia-reconcile", "--inspect"]
        main()
    finally:
        sys.argv = original_argv
    captured = capsys.readouterr()
    body = json.loads(captured.out.strip())
    assert set(body.keys()) == {
        "webhook_received",
        "sent_without_out",
        "handoff_expired",
        "open_count",
        "open_findings",
    }
    for key in ("webhook_received", "sent_without_out", "handoff_expired", "open_count"):
        assert isinstance(body[key], int)
    assert isinstance(body["open_findings"], list)
    matched = [
        item for item in body["open_findings"] if item["subject_key"] == subject_key
    ]
    assert len(matched) == 1
    assert matched[0]["kind"] == "webhook_received"
    assert matched[0]["channel"] == ""
    assert matched[0]["envelope_kind"] == ""
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _delete_finding(store, kind="webhook_received", subject_key=subject_key)
        _delete_webhook(store, provider=PROVIDER, provider_event_id=INSPECT_EVENT_ID_2)
        db.commit()
    finally:
        db.close()


def test_main_stdout_counts_only(capsys: pytest.CaptureFixture[str]) -> None:
    init_db()
    db = get_session_factory()()
    subject_key = f"{PROVIDER}:{STALE_EVENT_ID}"
    now = datetime.now(UTC)
    stale_claimed = (now - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=STALE_EVENT_ID,
            status="received",
            claimed_at=stale_claimed,
        )
        db.commit()
    finally:
        db.close()
    main()
    captured = capsys.readouterr()
    body = json.loads(captured.out.strip())
    assert set(body.keys()) == {
        "webhook_received",
        "sent_without_out",
        "handoff_expired",
    }
    for value in body.values():
        assert isinstance(value, int)
    assert STALE_EVENT_ID not in captured.out
    assert "evt.recon" not in captured.out
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        finding = store.get_reconciliation_finding(
            kind="webhook_received", subject_key=subject_key
        )
        assert finding is not None
    finally:
        _delete_finding(store, kind="webhook_received", subject_key=subject_key)
        _delete_webhook(store, provider=PROVIDER, provider_event_id=STALE_EVENT_ID)
        db.commit()
        db.close()


def test_evaluate_reconciliation_never_calls_mark_webhook() -> None:
    from app.domain import reconciliation as reconciliation_module

    source = inspect.getsource(reconciliation_module.evaluate_reconciliation)
    assert "mark_webhook" not in source


ENVELOPE_INSPECT_EVENT_ID = "envl.insp.received.1"
ENVELOPE_SENT_EVENT_ID = "envl.insp.sent.1"
ENVELOPE_HANDOFF_SESSION = "envl.insp.handoff.session"
ENVELOPE_IGREF_EVENT_ID = "igref:envl.insp.sender:ad"
VISITOR_PHRASE = "visitor asked about pricing"


def test_inspect_overlays_webhook_received_envelope() -> None:
    init_db()
    db = get_session_factory()()
    subject_key = f"{PROVIDER}:{ENVELOPE_INSPECT_EVENT_ID}"
    stale_claimed = (_FIXED_NOW - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=ENVELOPE_INSPECT_EVENT_ID,
            status="received",
            claimed_at=stale_claimed,
            channel="whatsapp",
            envelope_kind="audio",
        )
        db.commit()
        run_reconciliation(
            store, kill_switch=False, demo_active=False, now=_FIXED_NOW
        )
        db.commit()
        findings = inspect_open_findings(store)
        matched = [item for item in findings if item.subject_key == subject_key]
        assert len(matched) == 1
        assert matched[0].channel == "whatsapp"
        assert matched[0].envelope_kind == "audio"
        dumped = json.dumps(matched[0].model_dump())
        assert "@" not in dumped
        assert VISITOR_PHRASE not in dumped
    finally:
        _delete_finding(store, kind="webhook_received", subject_key=subject_key)
        _delete_webhook(
            store, provider=PROVIDER, provider_event_id=ENVELOPE_INSPECT_EVENT_ID
        )
        db.commit()
        db.close()


def test_inspect_overlays_sent_without_out_envelope() -> None:
    init_db()
    db = get_session_factory()()
    subject_key = f"{PROVIDER}:{ENVELOPE_SENT_EVENT_ID}"
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=PROVIDER,
            provider_event_id=ENVELOPE_SENT_EVENT_ID,
            status="sent",
            channel="whatsapp",
            envelope_kind="text",
        )
        db.commit()
        run_reconciliation(
            store, kill_switch=False, demo_active=False, now=_FIXED_NOW
        )
        db.commit()
        findings = inspect_open_findings(store)
        matched = [item for item in findings if item.subject_key == subject_key]
        assert len(matched) == 1
        assert matched[0].kind == "sent_without_out"
        assert matched[0].channel == "whatsapp"
        assert matched[0].envelope_kind == "text"
    finally:
        _delete_finding(store, kind="sent_without_out", subject_key=subject_key)
        _delete_webhook(
            store, provider=PROVIDER, provider_event_id=ENVELOPE_SENT_EVENT_ID
        )
        db.commit()
        db.close()


def test_inspect_handoff_expired_has_empty_envelope() -> None:
    init_db()
    db = get_session_factory()()
    token_hash = ""
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=ENVELOPE_HANDOFF_SESSION
        )
        store.issue_handoff_token(lead_id, ENVELOPE_HANDOFF_SESSION)
        row = db.scalars(
            select(HandoffTokenRow).where(HandoffTokenRow.lead_id == lead_id)
        ).one()
        token_hash = row.token_hash
        row.expires_at = (_FIXED_NOW - timedelta(minutes=5)).isoformat()
        db.commit()
        run_reconciliation(
            store, kill_switch=False, demo_active=False, now=_FIXED_NOW
        )
        db.commit()
        findings = inspect_open_findings(store)
        matched = [item for item in findings if item.subject_key == token_hash]
        assert len(matched) == 1
        assert matched[0].kind == "handoff_expired"
        assert matched[0].channel == ""
        assert matched[0].envelope_kind == ""
    finally:
        if token_hash:
            _delete_finding(store, kind="handoff_expired", subject_key=token_hash)
        _delete_handoff_by_session(store, ENVELOPE_HANDOFF_SESSION)
        db.commit()
        db.close()


def test_inspect_igref_subject_key_first_colon_split() -> None:
    init_db()
    db = get_session_factory()()
    provider = Channel.INSTAGRAM.value
    subject_key = f"{provider}:{ENVELOPE_IGREF_EVENT_ID}"
    stale_claimed = (_FIXED_NOW - timedelta(minutes=10)).isoformat()
    try:
        store = LeadStore(db)
        _seed_webhook(
            store,
            provider=provider,
            provider_event_id=ENVELOPE_IGREF_EVENT_ID,
            status="received",
            claimed_at=stale_claimed,
            channel="instagram",
            envelope_kind="referral",
        )
        db.commit()
        run_reconciliation(
            store, kill_switch=False, demo_active=False, now=_FIXED_NOW
        )
        db.commit()
        findings = inspect_open_findings(store)
        matched = [item for item in findings if item.subject_key == subject_key]
        assert len(matched) == 1
        assert matched[0].channel == "instagram"
        assert matched[0].envelope_kind == "referral"
    finally:
        _delete_finding(store, kind="webhook_received", subject_key=subject_key)
        _delete_webhook(
            store, provider=provider, provider_event_id=ENVELOPE_IGREF_EVENT_ID
        )
        db.commit()
        db.close()
