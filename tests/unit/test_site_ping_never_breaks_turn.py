"""A failing owner ping must never cost the visitor their reply.

`TelegramPort.send` raises `TelegramSendError`, a `MiaError` carrying http_status 502.
`ping_assaf_async` used to catch only `RuntimeError`, so a Telegram outage escaped into
the website route and turned a composed, persisted reply into a 502 the visitor saw
instead of Mia's answer.
"""

from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.integrations.telegram import TelegramSendError
from app.surfaces.site import (
    SiteSession,
    _ping_assaf,
    ping_assaf_async,
    reset_site_book,
    site_book,
)


class ExplodingTelegramPort:
    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message: object) -> None:
        self.attempts += 1
        raise TelegramSendError("Telegram sendMessage failed: HTTP 500")


def _settings() -> Settings:
    return Settings(telegram_owner_user_ids="12345")


def test_telegram_outage_does_not_escape_the_ping() -> None:
    reset_site_book()
    session = SiteSession(session_id="web_ping_fail")
    port = ExplodingTelegramPort()
    sent = asyncio.run(ping_assaf_async(_settings(), port, session))
    assert sent is False
    assert port.attempts == 1


def test_site_book_open_is_idempotent_under_the_lock() -> None:
    reset_site_book()
    book = site_book()
    first = book.open("web_lock_1")
    again = book.open("web_lock_1")
    assert first is again
    assert book.exists("web_lock_1")


def test_denied_sync_claim_neither_sends_nor_reports_delivery() -> None:
    class SyncPort:
        def __init__(self) -> None:
            self.attempts = 0

        def send(self, message: object) -> None:
            self.attempts += 1

    port = SyncPort()
    delivered = _ping_assaf(
        _settings(),
        port,  # type: ignore[arg-type]
        SiteSession(session_id="web_claim_denied"),
        claim=lambda _recipient: False,
    )

    assert delivered is False
    assert port.attempts == 0
