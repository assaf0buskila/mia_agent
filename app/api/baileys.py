"""Inbound webhook for the Baileys WhatsApp sidecar.

Baileys speaks the reverse-engineered WhatsApp Web protocol from Node, so the socket
lives in `services/whatsapp-baileys` and posts each inbound message here. This module
is transport only: once the payload is normalised it goes through exactly the same
`process_inbound_texts` as the Meta webhook, so Mia's behaviour cannot drift between
the two.

Off unless `MIA_WHATSAPP_BAILEYS_TOKEN` is set. Like every other inbound path it fails
closed: no configured token means no accepted request, never an open door.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_whatsapp_port
from app.api.inbound import process_inbound_texts
from app.core.config import get_settings
from app.core.errors import WebhookRejected
from app.db.store import LeadStore
from app.domain.events import Channel
from app.integrations.base import MessagePort

router = APIRouter(prefix="/v1/whatsapp/baileys", tags=["whatsapp"])

_MAX_TEXT = 4000


def _verify(authorization: str | None) -> None:
    """Constant-time bearer check against the shared sidecar token."""
    expected = get_settings().whatsapp_baileys_token.strip()
    if not expected:
        raise WebhookRejected("baileys token is not configured")
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not presented or not hmac.compare_digest(presented, expected):
        raise WebhookRejected("baileys token mismatch")


def parse_baileys_messages(payload: object) -> list[dict[str, str]]:
    """Normalise the sidecar payload into the item shape the inbound path expects.

    Anything malformed is dropped rather than raising: one bad entry in a batch must
    not cost the whole delivery, and the sidecar cannot be trusted to be well behaved
    just because it holds a token.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("messages")
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        message_id = str(entry.get("id") or "").strip()
        sender = str(entry.get("from") or "").strip()
        text = str(entry.get("text") or "").strip()
        if not message_id or not sender or not text:
            continue
        items.append(
            {
                "id": f"baileys:{message_id}",
                "from": sender,
                "text": text[:_MAX_TEXT],
            }
        )
    return items


@router.post("/webhook")
async def baileys_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    port: MessagePort = Depends(get_whatsapp_port),
) -> dict[str, object]:
    _verify(authorization)
    settings = get_settings()
    if settings.kill_switch:
        return {
            "processed": 0,
            "duplicates": 0,
            "sent": False,
            "sent_count": 0,
            "killed": True,
        }
    items = parse_baileys_messages(await request.json())
    if not items:
        return {"processed": 0, "duplicates": 0, "sent": False, "sent_count": 0}
    return await process_inbound_texts(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        items=items,
        store=LeadStore(db),
        port=port,
        kill_switch=settings.kill_switch,
        owner_ids=settings.whatsapp_owner_phone_set(),
    )
