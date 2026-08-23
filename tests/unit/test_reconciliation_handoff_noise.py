"""`handoff_expired` must not accumulate as an integration failure while ADR-024 holds.

Under ADR-024 the handoff token is deliberately kept out of the customer's wa.me prefill,
so it can never be sent back and never consumed. Every issued token expires unconsumed an
hour later, and `list_expired_unconsumed_handoffs` has no retention window — so before this
fix each website→WhatsApp click became a permanent open finding, and `/health`
`ops.integration_failures` grew forever while burying real dropped-webhook findings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.reconciliation import (
    evaluate_reconciliation,
    run_reconciliation,
)


def _store() -> LeadStore:
    init_db()
    return LeadStore(get_session_factory()())


def _expired_handoff(store: LeadStore, session_id: str) -> None:
    _customer_id, lead_id = store.open_channel_lead(
        channel=Channel.WEBSITE, external_id=session_id
    )
    store.issue_handoff_token(lead_id, session_id)
    store.session.commit()


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(hours=2)


def test_expired_tokens_are_not_failures_while_handoff_send_is_off() -> None:
    store = _store()
    _expired_handoff(store, "web_noise_off_1")
    findings = evaluate_reconciliation(
        store, now=_later(), handoff_send_enabled=False
    )
    assert [f for f in findings if f.kind == "handoff_expired"] == []


def test_expired_tokens_are_failures_once_handoff_send_is_on() -> None:
    """When official inbound lands and the flag flips, an unconsumed token matters again."""
    store = _store()
    _expired_handoff(store, "web_noise_on_1")
    findings = evaluate_reconciliation(
        store, now=_later(), handoff_send_enabled=True
    )
    assert any(f.kind == "handoff_expired" for f in findings)


def test_the_summary_reports_zero_expired_while_gated() -> None:
    store = _store()
    _expired_handoff(store, "web_noise_off_2")
    summary = run_reconciliation(
        store,
        kill_switch=False,
        demo_active=False,
        now=_later(),
        handoff_send_enabled=False,
    )
    assert summary.handoff_expired == 0


def test_a_gated_scan_closes_findings_a_previous_scan_left_open() -> None:
    """The live count only drops if the next clean scan actually closes the stale rows."""
    store = _store()
    _expired_handoff(store, "web_noise_close_1")

    run_reconciliation(
        store, kill_switch=False, demo_active=False, now=_later(), handoff_send_enabled=True
    )
    store.session.commit()
    opened = [
        row for row in store.list_open_reconciliation_findings()
        if row.kind == "handoff_expired"
    ]
    assert opened, "expected the ungated scan to open a handoff_expired finding"

    run_reconciliation(
        store, kill_switch=False, demo_active=False, now=_later(), handoff_send_enabled=False
    )
    store.session.commit()
    still_open = [
        row for row in store.list_open_reconciliation_findings()
        if row.kind == "handoff_expired"
    ]
    assert still_open == []


def test_real_dropped_webhooks_are_still_reported_while_gated() -> None:
    """Suppressing the noise must not suppress the finding that means a lost message."""
    store = _store()
    store.claim_webhook(
        provider="whatsapp",
        provider_event_id="wamid.dropped.1",
        channel=Channel.WHATSAPP.value,
        envelope_kind="message",
    )
    store.session.commit()
    findings = evaluate_reconciliation(
        store, now=_later(), handoff_send_enabled=False
    )
    assert any(f.kind == "webhook_received" for f in findings)
