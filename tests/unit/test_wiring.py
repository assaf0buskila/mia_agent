from datetime import UTC, datetime

import pytest
from app.core.capabilities import CapabilityId, require_alive
from app.domain.events import CanonicalEvent, Channel, EventType
from app.graph.state import empty_state
from app.integrations.base import DisabledMessagePort, OutboundMessage


def test_canonical_event_is_serializable() -> None:
    event = CanonicalEvent(
        event_id="evt_1",
        event_type=EventType.MESSAGE_IN,
        channel=Channel.WEBSITE,
        occurred_at=datetime.now(UTC),
        idempotency_key="idem_1",
        payload={"text": "hello"},
    )
    dumped = event.model_dump(mode="json")
    assert dumped["channel"] == "website"
    restored = CanonicalEvent.model_validate(dumped)
    assert restored.event_id == "evt_1"


def test_graph_state_has_no_non_serializable_defaults() -> None:
    state = empty_state(run_id="run_1", thread_id="thread_1", channel="website")
    assert state["approval_required"] is False
    assert isinstance(state["errors"], list)
    assert isinstance(state["cost"], dict)


@pytest.mark.asyncio
async def test_disabled_message_port_refuses_send() -> None:
    port = DisabledMessagePort()
    with pytest.raises(RuntimeError, match="not alive"):
        await port.send(
            OutboundMessage(
                conversation_id="c1",
                text="hi",
                channel="whatsapp",
                idempotency_key="k1",
            )
        )


def test_require_alive_rejects_specified_capability() -> None:
    with pytest.raises(RuntimeError):
        require_alive(CapabilityId.AWS_RUNTIME)


def test_require_alive_canonical_events_passes() -> None:
    require_alive(CapabilityId.CANONICAL_EVENTS)
    require_alive(CapabilityId.APPROVALS)
    require_alive(CapabilityId.DEALS)
    require_alive(CapabilityId.MEETINGS)
    require_alive(CapabilityId.CONTENT_PERFORMANCE)
    require_alive(CapabilityId.DUE_SCAN)
    require_alive(CapabilityId.OWNER_BRIEF)
