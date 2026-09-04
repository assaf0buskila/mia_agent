"""One failed send must not redeliver replies the batch already delivered.

Providers batch messages. `get_db` rolls the whole request back on any exception, so
a send failure for the second customer used to erase the first customer's committed
"sent" claim. The provider then retried the batch and the first customer received the
same reply again — once per retry — and a duplicate lead was minted each time.

Per-item commit keeps both guarantees: the item that failed is still reclaimed and
retried (`claim_webhook` reclaims a rolled-back claim), while the item that succeeded
is marked `sent` and skipped as a duplicate.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from app.api.deps import get_whatsapp_port
from app.db.models import WebhookEventRow
from app.db.session import get_session_factory, init_db
from app.integrations.base import OutboundMessage
from app.integrations.whatsapp import WhatsAppSendError
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

GOOD = "972500000001"
BAD = "972500000002"


class PartiallyFailingPort:
    """Delivers to the first customer, fails for the second — a real provider blip."""

    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def send(self, message: OutboundMessage) -> None:
        if message.conversation_id == BAD:
            raise WhatsAppSendError("WhatsApp send failed: HTTP 503")
        self.delivered.append(message.conversation_id)


def _signed() -> tuple[bytes, dict[str, str]]:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": GOOD,
                                    "id": "wamid.batch.good",
                                    "type": "text",
                                    "text": {"body": "יש לי מספרה"},
                                },
                                {
                                    "from": BAD,
                                    "id": "wamid.batch.bad",
                                    "type": "text",
                                    "text": {"body": "יש לי מרפאה"},
                                },
                            ]
                        }
                    }
                ]
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    return raw, {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def test_failed_send_does_not_redeliver_the_successful_one(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setenv("MIA_WHATSAPP_SENDER", "composio")
    init_db()
    port = PartiallyFailingPort()
    app.dependency_overrides[get_whatsapp_port] = lambda: port
    raw, headers = _signed()
    try:
        with TestClient(app) as client:
            client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            after_first = list(port.delivered)
            # The provider retries the whole batch after the non-2xx.
            client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)

    assert after_first == [GOOD], "the reachable customer should have been answered"
    # The regression: GOOD must not be answered twice by the retry.
    assert port.delivered.count(GOOD) == 1, (
        f"customer {GOOD} received the same reply {port.delivered.count(GOOD)} times"
    )

    db = get_session_factory()()
    try:
        rows = {
            r.provider_event_id: r.status
            for r in db.scalars(select(WebhookEventRow)).all()
        }
    finally:
        db.close()
    # The delivered item is durably claimed; the failed one stays retryable.
    assert rows.get("wamid.batch.good") == "sent"
    assert rows.get("wamid.batch.bad") in {None, "received", "failed"}
