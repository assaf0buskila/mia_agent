"""The owner must not lose a reply he already waited and paid for.

`TelegramPort.send` raises `TelegramSendError` (a `MiaError`) on any 4xx/5xx — a 429
flood limit is likely on a reply long enough to be split into 4096-char chunks. The
owner path caught only `RuntimeError`, which had only ever matched the not-configured
`DisabledMessagePort`, so a live Telegram error threw away a completed turn and left
the webhook row stuck in `received`.
"""

from __future__ import annotations

import asyncio

from app.domain.tools import AdapterHttpError
from app.integrations.base import OutboundMessage
from app.integrations.telegram import TelegramSendError
from app.workers.telegram_owner import _send_owner_notice


class ExplodingPort:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.attempts = 0

    async def send(self, message: OutboundMessage) -> None:
        self.attempts += 1
        raise self._error


def _item() -> dict[str, str]:
    return {"id": "tg.owner.1", "from": "12345", "text": "מה קרה היום?"}


def test_telegram_send_error_does_not_escape_the_owner_notice() -> None:
    port = ExplodingPort(TelegramSendError("Telegram sendMessage failed: HTTP 429"))
    sent = asyncio.run(_send_owner_notice(item=_item(), port=port, text="שלום"))
    assert sent is False
    assert port.attempts == 1


def test_adapter_http_error_does_not_escape_the_owner_notice() -> None:
    port = ExplodingPort(AdapterHttpError(503))
    sent = asyncio.run(_send_owner_notice(item=_item(), port=port, text="שלום"))
    assert sent is False
    assert port.attempts == 1


def test_tool_budget_clears_the_adapter_ceiling_and_stays_under_the_turn_guard() -> None:
    """A tool budget below the adapter timeout guarantees discarded real answers."""
    from app.domain.two_state import (
        SLOW_HOUSE_TOOLS,
        TOOL_RECOVERY_SECONDS,
        TOOL_TIMEOUT_SECONDS,
    )

    # House adapters allow themselves 20s; anything below that always times out first.
    assert TOOL_TIMEOUT_SECONDS > 20
    # The whole turn is wrapped in asyncio.wait_for(..., 45) in telegram_owner, so a
    # slow budget above that buys nothing — the outer guard fires first.
    assert TOOL_TIMEOUT_SECONDS + TOOL_RECOVERY_SECONDS < 45
    # Multi-provider reads fan out to two or more adapters and need the slow budget.
    for name in ("seo_snapshot", "website_kpis", "research_search", "owner_system_audit"):
        assert name in SLOW_HOUSE_TOOLS


def test_every_slow_tool_is_a_real_registry_tool() -> None:
    from app.domain.two_state import SLOW_HOUSE_TOOLS
    from app.tools.registries.owner_tools import tool_names

    assert SLOW_HOUSE_TOOLS <= set(tool_names())
