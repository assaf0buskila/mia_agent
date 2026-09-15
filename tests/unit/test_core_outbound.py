"""`send_inbound_reply`'s prospect gates: shadow/scope skip and human-takeover.

No test covered this module at all before C7b. `is_human_takeover` is a live guard
(also consulted by `app/core/outbound.py:34` here and `app/domain/followups.py` for
scheduled follow-ups) protecting a real parked-lead row in production today -- see
HANDOFF section 0 for the `/health` evidence. Nothing should be able to weaken or
delete that gate silently in future without a test noticing.
"""

from __future__ import annotations

import pytest
from app.core.config import AutomationMode
from app.core.outbound import send_inbound_reply
from app.domain.events import Channel
from app.integrations.base import OutboundMessage


class _RecordingPort:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)


class _TakeoverStore:
    """Minimal double: only `is_human_takeover` is consulted on this path."""

    def __init__(self, *, human_takeover: bool) -> None:
        self._human_takeover = human_takeover

    def is_human_takeover(self, lead_id: str) -> bool:
        del lead_id
        return self._human_takeover


def _message() -> OutboundMessage:
    return OutboundMessage(
        conversation_id="lead_takeover_gate",
        text="hi",
        channel=Channel.WEBSITE.value,
        idempotency_key="evt.takeover.1",
    )


@pytest.mark.asyncio
async def test_human_takeover_blocks_a_prospect_reply_without_sending() -> None:
    port = _RecordingPort()
    sent = await send_inbound_reply(
        port=port,
        message=_message(),
        kill_switch=False,
        automation_mode=AutomationMode.HYBRID,
        actor_role="prospect",
        lead_id="lead_takeover_gate",
        store=_TakeoverStore(human_takeover=True),
    )
    assert sent is False
    assert port.sent == []


@pytest.mark.asyncio
async def test_prospect_reply_sends_when_not_human_takeover() -> None:
    """The gate actually discriminates on the flag, not on `actor_role` alone."""
    port = _RecordingPort()
    sent = await send_inbound_reply(
        port=port,
        message=_message(),
        kill_switch=False,
        automation_mode=AutomationMode.HYBRID,
        actor_role="prospect",
        lead_id="lead_takeover_gate",
        store=_TakeoverStore(human_takeover=False),
    )
    assert sent is True
    assert len(port.sent) == 1


@pytest.mark.asyncio
async def test_human_takeover_flag_is_never_consulted_for_a_non_prospect_actor() -> None:
    """Owner/system replies must never be blocked by a customer-side takeover flag."""

    class _ExplodingStore:
        def is_human_takeover(self, lead_id: str) -> bool:
            raise AssertionError("must not be consulted for a non-prospect actor")

    port = _RecordingPort()
    sent = await send_inbound_reply(
        port=port,
        message=_message(),
        kill_switch=False,
        automation_mode=AutomationMode.HYBRID,
        actor_role="owner",
        lead_id="lead_takeover_gate",
        store=_ExplodingStore(),
    )
    assert sent is True
    assert len(port.sent) == 1
