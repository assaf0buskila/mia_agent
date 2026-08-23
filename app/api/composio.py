import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.inbound import process_inbound_texts
from app.core.config import get_settings
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.core.webhooks import verify_composio_signature
from app.db.store import LeadStore
from app.domain.events import Channel, persist_tool_outcome
from app.domain.tools import AdapterHttpError, ToolOutcome
from app.integrations.base import DisabledMessagePort
from app.integrations.gmail import (
    GMAIL_NEW_MESSAGE_TRIGGER,
    build_gmail_port,
    build_inbound_text,
    gmail_results_outcome,
    hydrate_gmail_item,
    parse_sender_email,
)
from app.integrations.whatsapp import parse_composio_whatsapp_inbound

router = APIRouter(prefix="/v1/composio", tags=["composio"])


def extract_trigger_slug(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata") or {}
    slug = metadata.get("trigger_slug")
    if slug:
        return str(slug)
    slug = payload.get("trigger_slug")
    if slug:
        return str(slug)
    trigger = payload.get("trigger") or {}
    slug = trigger.get("slug")
    if slug:
        return str(slug)
    return ""


def parse_gmail_trigger_item(data: dict[str, Any]) -> dict[str, str] | None:
    message_id = str(data.get("message_id") or data.get("id") or "").strip()
    sender = parse_sender_email(str(data.get("sender") or ""))
    text = build_inbound_text(
        subject=str(data.get("subject") or ""),
        message_text=str(data.get("message_text") or ""),
    )
    if not message_id or not sender:
        return None
    item: dict[str, str] = {"id": message_id, "from": sender, "text": text}
    thread_id = str(data.get("thread_id") or data.get("threadId") or "").strip()
    if thread_id:
        item["thread_id"] = thread_id
    return item


def composio_user_matches(payload: dict[str, Any], expected_user_id: str) -> bool:
    incoming = str((payload.get("metadata") or {}).get("user_id") or "").strip()
    expected = expected_user_id.strip()
    if expected and incoming and incoming != expected:
        return False
    return True


@router.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    body = await request.body()
    verify_composio_signature(
        secret=settings.composio_webhook_secret,
        body=body,
        webhook_id=request.headers.get("webhook-id", ""),
        webhook_timestamp=request.headers.get("webhook-timestamp", ""),
        webhook_signature=request.headers.get("webhook-signature", ""),
    )

    if settings.kill_switch:
        return {
            "processed": 0,
            "duplicates": 0,
            "sent": False,
            "killed": True,
        }

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"processed": 0, "ignored": True}
    if not isinstance(payload, dict):
        return {"processed": 0, "ignored": True}
    if not composio_user_matches(payload, settings.composio_user_id):
        return {"processed": 0, "ignored": True}
    trigger_slug = extract_trigger_slug(payload)
    if trigger_slug.startswith("WHATSAPP_"):
        parse_composio_whatsapp_inbound(payload)
        return {"processed": 0, "ignored": True}
    if trigger_slug != GMAIL_NEW_MESSAGE_TRIGGER:
        return {"processed": 0, "ignored": True}

    data = payload.get("data") or {}
    item = parse_gmail_trigger_item(data)
    if item is None:
        return {"processed": 0, "duplicates": 0, "sent": False}

    assert_allowed(
        RiskAction(name="gmail_read", risk=RiskLevel.R0_READ),
        kill_switch=settings.kill_switch,
    )

    trigger_text_empty = not item.get("text", "").strip()
    hydrate_error: AdapterHttpError | None = None
    if trigger_text_empty:
        try:
            item = hydrate_gmail_item(item, build_gmail_port(settings))
        except AdapterHttpError as exc:
            hydrate_error = exc

    store = LeadStore(db)
    result = await process_inbound_texts(
        provider="gmail",
        channel=Channel.GMAIL,
        items=[item],
        store=store,
        port=DisabledMessagePort(),
        kill_switch=settings.kill_switch,
    )

    if trigger_text_empty:
        inbound_row = store.get_canonical_event(
            provider="gmail", provider_event_id=item["id"]
        )
        inbound_correlation = inbound_row.correlation_id if inbound_row else ""
        now = datetime.now(UTC)
        if hydrate_error is not None:
            persist_tool_outcome(
                store,
                provider="gmail",
                channel=Channel.GMAIL,
                inbound_provider_event_id=item["id"],
                conversation_id=item.get("thread_id") or item["id"],
                lead_id=None,
                outcome=ToolOutcome(
                    tool="gmail_fetch",
                    status=hydrate_error.tool_status(),
                    result_count=0,
                ),
                correlation_id=inbound_correlation,
            )
        else:
            present = bool(item.get("text", "").strip())
            persist_tool_outcome(
                store,
                provider="gmail",
                channel=Channel.GMAIL,
                inbound_provider_event_id=item["id"],
                conversation_id=item.get("thread_id") or item["id"],
                lead_id=None,
                outcome=gmail_results_outcome(present=present, now=now),
                correlation_id=inbound_correlation,
            )

    return result
