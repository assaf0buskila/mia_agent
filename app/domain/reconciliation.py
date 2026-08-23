"""Flag-only reconciliation for stale transitional records. Never repairs or sends."""

from datetime import UTC, datetime

from pydantic import BaseModel, field_validator

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.events import sanitize_webhook_channel, sanitize_webhook_envelope_kind

STALE_AFTER_SECONDS = 300

ALLOWLISTED_FINDING_KINDS = frozenset(
    {"webhook_received", "sent_without_out", "handoff_expired"}
)

INSPECT_LIMIT = 50


class ReconciliationFinding(BaseModel):
    kind: str
    subject_key: str
    reason: str

    @field_validator("kind", "reason")
    @classmethod
    def _allowlisted_kind(cls, value: str) -> str:
        if value not in ALLOWLISTED_FINDING_KINDS:
            raise ValueError(f"invalid reconciliation kind: {value}")
        return value

    @field_validator("subject_key")
    @classmethod
    def _subject_key_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 255:
            raise ValueError("invalid subject_key")
        return cleaned


class ReconciliationSummary(BaseModel):
    webhook_received: int
    sent_without_out: int
    handoff_expired: int


class OpenFinding(BaseModel):
    kind: str
    subject_key: str
    channel: str = ""
    envelope_kind: str = ""

    @field_validator("kind")
    @classmethod
    def _allowlisted_kind(cls, value: str) -> str:
        if value not in ALLOWLISTED_FINDING_KINDS:
            raise ValueError(f"invalid reconciliation kind: {value}")
        return value

    @field_validator("subject_key")
    @classmethod
    def _subject_key_present(cls, value: str) -> str:
        cleaned = value.strip()
        if (
            not cleaned
            or "\n" in cleaned
            or "\r" in cleaned
            or len(cleaned) > 255
        ):
            raise ValueError("invalid subject_key")
        return cleaned

    @field_validator("channel")
    @classmethod
    def _sanitize_channel(cls, value: str) -> str:
        return sanitize_webhook_channel(value)

    @field_validator("envelope_kind")
    @classmethod
    def _sanitize_envelope_kind(cls, value: str) -> str:
        return sanitize_webhook_envelope_kind(value)


def _parse_webhook_subject_key(subject_key: str) -> tuple[str, str] | None:
    if ":" not in subject_key:
        return None
    provider, _, provider_event_id = subject_key.partition(":")
    if not provider.strip() or not provider_event_id.strip():
        return None
    return provider, provider_event_id


def _webhook_subject_key(*, provider: str, provider_event_id: str) -> str:
    return f"{provider}:{provider_event_id}"


def is_stale_received(*, claimed_at: str, now: datetime) -> bool:
    if not claimed_at:
        return True
    try:
        claimed_dt = datetime.fromisoformat(claimed_at)
    except ValueError:
        return True
    if claimed_dt.tzinfo is None:
        claimed_dt = claimed_dt.replace(tzinfo=UTC)
    return (now - claimed_dt).total_seconds() > STALE_AFTER_SECONDS


def evaluate_reconciliation(
    store,
    *,
    now: datetime | None = None,
    handoff_send_enabled: bool = True,
) -> list[ReconciliationFinding]:
    """Read-only scan. Never writes.

    `handoff_send_enabled=False` suppresses `handoff_expired`. Under ADR-024
    `compose_handoff_text` does not put the token in the customer's wa.me prefill, so the
    customer never sends it back and `consume_handoff_token` is never reached. Every
    issued token therefore expires unconsumed an hour later, and
    `list_expired_unconsumed_handoffs` has no retention window — so each website→WhatsApp
    click became a permanent open finding that no later scan could close.

    That is the designed outcome of ADR-024, not an integration failure, and counting it
    as one buries the findings that do matter: `webhook_received` means an inbound message
    was claimed and then dropped. When official Cloud API inbound lands and
    `MIA_WHATSAPP_HANDOFF_SEND` flips true, an unconsumed token becomes meaningful again
    and this scan resumes.
    """
    effective_now = now or datetime.now(UTC)
    findings: list[ReconciliationFinding] = []

    for row in store.list_webhooks_by_status(status="received"):
        if not row.provider_event_id.strip():
            continue
        if not is_stale_received(claimed_at=row.claimed_at, now=effective_now):
            continue
        subject_key = _webhook_subject_key(
            provider=row.provider, provider_event_id=row.provider_event_id
        )
        findings.append(
            ReconciliationFinding(
                kind="webhook_received",
                subject_key=subject_key,
                reason="webhook_received",
            )
        )

    for row in store.list_webhooks_by_status(status="sent"):
        if not row.provider_event_id.strip():
            continue
        out_id = f"{row.provider_event_id}:out"
        if store.get_canonical_event(provider=row.provider, provider_event_id=out_id) is not None:
            continue
        subject_key = _webhook_subject_key(
            provider=row.provider, provider_event_id=row.provider_event_id
        )
        findings.append(
            ReconciliationFinding(
                kind="sent_without_out",
                subject_key=subject_key,
                reason="sent_without_out",
            )
        )

    if not handoff_send_enabled:
        return findings

    now_iso = effective_now.isoformat()
    for row in store.list_expired_unconsumed_handoffs(now_iso=now_iso):
        if not row.token_hash.strip():
            continue
        findings.append(
            ReconciliationFinding(
                kind="handoff_expired",
                subject_key=row.token_hash,
                reason="handoff_expired",
            )
        )

    return findings


def apply_reconciliation_policy(
    store,
    *,
    findings: list[ReconciliationFinding],
    kill_switch: bool,
    demo_active: bool,
) -> None:
    if demo_active or kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="reconciliation_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=False,
        )
    except PolicyDenied:
        return
    current_keys: set[tuple[str, str]] = set()
    for finding in findings:
        if finding.kind not in ALLOWLISTED_FINDING_KINDS:
            continue
        current_keys.add((finding.kind, finding.subject_key))
        store.upsert_reconciliation_finding(
            kind=finding.kind,
            subject_key=finding.subject_key,
            reason=finding.reason,
            open=True,
        )
    for row in store.list_open_reconciliation_findings():
        if (row.kind, row.subject_key) in current_keys:
            continue
        store.upsert_reconciliation_finding(
            kind=row.kind,
            subject_key=row.subject_key,
            reason=row.reason,
            open=False,
        )


def inspect_open_findings(store) -> list[OpenFinding]:
    """Read-only SoR inspect. Never writes. Cap INSPECT_LIMIT."""
    candidates: list[tuple[str, str]] = []
    for row in store.list_open_reconciliation_findings():
        if row.kind not in ALLOWLISTED_FINDING_KINDS:
            continue
        candidates.append((row.kind, row.subject_key))
    findings: list[OpenFinding] = []
    for kind, subject_key in sorted(candidates):
        if len(findings) >= INSPECT_LIMIT:
            break
        channel = ""
        envelope_kind = ""
        if kind in {"webhook_received", "sent_without_out"}:
            parsed = _parse_webhook_subject_key(subject_key)
            if parsed is not None:
                provider, provider_event_id = parsed
                row = store.get_webhook(
                    provider=provider, provider_event_id=provider_event_id
                )
                if row is not None:
                    channel = sanitize_webhook_channel(row.channel or "")
                    envelope_kind = sanitize_webhook_envelope_kind(
                        row.envelope_kind or ""
                    )
        try:
            findings.append(
                OpenFinding(
                    kind=kind,
                    subject_key=subject_key,
                    channel=channel,
                    envelope_kind=envelope_kind,
                )
            )
        except ValueError:
            continue
    return findings


def run_reconciliation(
    store,
    *,
    kill_switch: bool,
    demo_active: bool,
    now: datetime | None = None,
    handoff_send_enabled: bool = True,
) -> ReconciliationSummary:
    findings = evaluate_reconciliation(
        store, now=now, handoff_send_enabled=handoff_send_enabled
    )
    apply_reconciliation_policy(
        store,
        findings=findings,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )
    counts = {kind: 0 for kind in ALLOWLISTED_FINDING_KINDS}
    for finding in findings:
        if finding.kind in counts:
            counts[finding.kind] += 1
    return ReconciliationSummary(
        webhook_received=counts["webhook_received"],
        sent_without_out=counts["sent_without_out"],
        handoff_expired=counts["handoff_expired"],
    )
