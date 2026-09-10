import hashlib
import hmac

import pytest
from app.core.errors import MergeRejected, PolicyDenied, WebhookRejected
from app.core.redact import redact
from app.core.risk import RiskAction, RiskLevel, assert_allowed, decide
from app.core.webhooks import verify_webhook
from app.domain.events import Channel
from app.domain.identity import ChannelIdentity, IdentityIndex
from app.domain.sales import SalesState, compute_missing_fields, manual_step_established


def test_redact_strips_pii_and_secrets() -> None:
    cleaned = redact(
        {"email": "a@b.com", "phone": "050", "text": "hi", "nested": {"api_key": "x"}}
    )
    assert cleaned["email"] == "[redacted]"
    assert cleaned["phone"] == "[redacted]"
    assert cleaned["text"] == "hi"
    assert cleaned["nested"]["api_key"] == "[redacted]"


def test_risk_policy_gates_meta_writes_and_denies_destructive() -> None:
    assert decide(RiskAction(name="read_insights", risk=RiskLevel.R0_READ)).value == "auto"
    assert decide(
        RiskAction(
            name="ig_reply",
            risk=RiskLevel.R2_CUSTOMER_MESSAGE,
            in_approved_scope=True,
        )
    ).value == "auto"
    assert decide(
        RiskAction(
            name="ig_reply",
            risk=RiskLevel.R2_CUSTOMER_MESSAGE,
            in_approved_scope=False,
        )
    ).value == "approval"
    assert decide(
        RiskAction(name="meta_budget", risk=RiskLevel.R4_FINANCIAL_MARKETING)
    ).value == "approval"
    with pytest.raises(PolicyDenied):
        assert_allowed(RiskAction(name="delete", risk=RiskLevel.R5_DESTRUCTIVE))
    with pytest.raises(PolicyDenied):
        decide(RiskAction(name="read", risk=RiskLevel.R0_READ), kill_switch=True)


def test_webhook_hmac_and_replay_window() -> None:
    secret = "test-secret"
    body = b'{"id":"1"}'
    ts = 1_700_000_000
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    verify_webhook(secret=secret, body=body, signature_hex=signature, timestamp=ts, now=ts)
    with pytest.raises(WebhookRejected):
        verify_webhook(
            secret=secret,
            body=body,
            signature_hex="deadbeef",
            timestamp=ts,
            now=ts,
        )
    with pytest.raises(WebhookRejected):
        verify_webhook(
            secret=secret,
            body=body,
            signature_hex=signature,
            timestamp=ts,
            now=ts + 10_000,
        )


def test_identity_reuses_exact_channel_id_and_rejects_weak_merge() -> None:
    index = IdentityIndex()
    first = index.observe(
        ChannelIdentity(channel=Channel.WHATSAPP, external_id="+97250", verified=True)
    )
    again = index.observe(
        ChannelIdentity(channel=Channel.WHATSAPP, external_id="+97250", verified=True)
    )
    other = index.observe(
        ChannelIdentity(channel=Channel.INSTAGRAM, external_id="ig_9", verified=False)
    )
    assert first.customer_id == again.customer_id
    assert first.customer_id != other.customer_id
    with pytest.raises(MergeRejected):
        index.merge(first.customer_id, other.customer_id, verified=False)
    merged = index.merge(first.customer_id, other.customer_id, verified=True)
    assert merged.customer_id == first.customer_id
    assert len(merged.identities) == 2


def test_compute_missing_fields_preserves_historical_order() -> None:
    assert compute_missing_fields(SalesState(lead_id="x")) == [
        "decision_maker",
        "timeline",
        "metric",
    ]
    complete = SalesState(
        lead_id="x",
        authority_known=True,
        timeline_known=True,
        metric_known=True,
    )
    assert compute_missing_fields(complete) == []


def test_manual_step_established_accepts_explicit_or_historical_progress() -> None:
    assert manual_step_established(SalesState(lead_id="cold")) is False
    assert manual_step_established(SalesState(lead_id="explicit", manual_step_known=True))
    assert manual_step_established(SalesState(lead_id="reflected", reflected=True))
    assert manual_step_established(SalesState(lead_id="hypothesis", hypothesis_offered=True))
