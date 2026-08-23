"""Business value count events (FDE operating layer): qualified / booked / recovered / handoff."""

from __future__ import annotations

import json
from enum import StrEnum

from sqlalchemy import select

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.db.models import CanonicalEventRow
from app.domain.events import Channel, EventType, build_business_value_event

_VALUE_KINDS = frozenset({"qualified", "booked", "recovered", "handoff"})


class ValueKind(StrEnum):
    QUALIFIED = "qualified"
    BOOKED = "booked"
    RECOVERED = "recovered"
    HANDOFF = "handoff"


def persist_business_value(
    store,
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    kind: ValueKind,
    conversation_id: str = "",
) -> bool:
    if not lead_id or kind.value not in _VALUE_KINDS:
        return False
    try:
        assert_allowed(
            RiskAction(name="business_value_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=False,
        )
    except PolicyDenied:
        return False
    event = build_business_value_event(
        provider=provider,
        channel=channel,
        lead_id=lead_id,
        kind=kind.value,
        conversation_id=conversation_id,
    )
    if (
        store.get_canonical_event(
            provider=provider, provider_event_id=event.idempotency_key
        )
        is not None
    ):
        return False
    store.save_canonical_event(provider=provider, event=event)
    return (
        store.get_canonical_event(
            provider=provider, provider_event_id=event.idempotency_key
        )
        is not None
    )


def count_business_value(store, *, kind: ValueKind, lead_id: str) -> int:
    if not lead_id or kind.value not in _VALUE_KINDS:
        return 0
    query = select(CanonicalEventRow.payload_json).where(
        CanonicalEventRow.event_type == EventType.BUSINESS_VALUE.value,
        CanonicalEventRow.lead_id == lead_id,
    )
    count = 0
    for payload_json in store.session.scalars(query).all():
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("kind") == kind.value:
            count += 1
    return count
