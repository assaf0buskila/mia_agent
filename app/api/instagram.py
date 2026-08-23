import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_instagram_port
from app.api.inbound import process_inbound_texts
from app.core.config import get_settings
from app.core.errors import WebhookRejected
from app.core.webhooks import verify_meta_signature
from app.db.store import LeadStore
from app.domain.attribution import sanitize_instagram_attribution
from app.domain.events import Channel
from app.integrations.base import MessagePort

router = APIRouter(prefix="/v1/instagram", tags=["instagram"])


def _story_id_from_message(message: dict[str, Any]) -> str | None:
    reply_to = message.get("reply_to")
    if not isinstance(reply_to, dict):
        return None
    story = reply_to.get("story")
    if isinstance(story, dict):
        story_id = story.get("id")
        if story_id is not None and str(story_id).strip():
            return str(story_id)
    elif isinstance(story, str) and story.strip():
        return story.strip()
    return None


def _referral_raw_from_event(msg_event: dict[str, Any]) -> dict[str, str | None]:
    raw: dict[str, str | None] = {}
    message = msg_event.get("message", {})
    if not isinstance(message, dict):
        message = {}
    referral = msg_event.get("referral")
    if not isinstance(referral, dict):
        referral = message.get("referral")
    if isinstance(referral, dict):
        if referral.get("source") is not None:
            raw["ig_trigger_source"] = str(referral.get("source", ""))
        if referral.get("ref") is not None:
            raw["ig_ref"] = str(referral.get("ref", ""))
        if referral.get("ad_id") is not None:
            raw["meta_ad_id"] = str(referral.get("ad_id", ""))
        ads_context = referral.get("ads_context_data")
        if isinstance(ads_context, dict) and ads_context.get("post_id") is not None:
            raw["meta_post_id"] = str(ads_context.get("post_id", ""))
    story_id = _story_id_from_message(message)
    if story_id is not None:
        raw["ig_content_id"] = story_id
        raw.setdefault("ig_trigger_source", "STORY")
    return raw


def _stable_attribution_id(attribution: dict[str, str]) -> str | None:
    for key in ("meta_ad_id", "ig_ref", "ig_content_id"):
        value = attribution.get(key)
        if value:
            return value
    return None


def parse_inbound_texts(payload: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    seen_mids: set[str] = set()

    def add_message(
        *,
        mid: str,
        sender: str,
        text: str,
        msg_event: dict[str, Any] | None = None,
    ) -> None:
        if not sender:
            return
        attribution: dict[str, str] = {}
        if msg_event is not None:
            attribution = sanitize_instagram_attribution(_referral_raw_from_event(msg_event))
            if (
                attribution.get("ig_trigger_source") == "STORY"
                and "ig_content_id" not in attribution
            ):
                attribution.pop("ig_trigger_source", None)
        if not mid:
            if not attribution:
                return
            stable = _stable_attribution_id(attribution)
            if stable is None:
                return
            mid = f"igref:{sender}:{stable}"
        if not text and not attribution:
            return
        if mid in seen_mids:
            return
        seen_mids.add(mid)
        item: dict[str, str] = {"id": mid, "from": sender, "text": text}
        item.update(attribution)
        messages.append(item)

    for entry in payload.get("entry", []):
        for msg_event in entry.get("messaging", []):
            message = msg_event.get("message", {})
            if not isinstance(message, dict):
                message = {}
            if message.get("is_echo"):
                continue
            add_message(
                mid=str(message.get("mid", "")),
                sender=str(msg_event.get("sender", {}).get("id", "")),
                text=str(message.get("text", "")),
                msg_event=msg_event if isinstance(msg_event, dict) else None,
            )

        for change in entry.get("changes", []):
            value = change.get("value", {})
            for item in value.get("messages", []):
                if item.get("type") not in (None, "text"):
                    continue
                body = item.get("text", {}).get("body", "")
                add_message(
                    mid=str(item.get("id", "")),
                    sender=str(item.get("from", "")),
                    text=str(body),
                )
    return messages


@router.get("/webhook")
def verify_subscription(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
) -> PlainTextResponse:
    settings = get_settings()
    if not settings.instagram_verify_token:
        raise WebhookRejected("instagram verify token is not configured")
    if hub_mode == "subscribe" and hub_verify_token == settings.instagram_verify_token:
        return PlainTextResponse(hub_challenge)
    raise WebhookRejected("instagram verify token mismatch")


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
    port: MessagePort = Depends(get_instagram_port),
) -> dict:
    settings = get_settings()
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    verify_meta_signature(secret=settings.instagram_app_secret, body=body, header=signature)

    if settings.kill_switch:
        return {
            "processed": 0,
            "duplicates": 0,
            "sent": False,
            "sent_count": 0,
            "killed": True,
        }

    payload = json.loads(body.decode() or "{}")
    store = LeadStore(db)
    return await process_inbound_texts(
        provider="instagram",
        channel=Channel.INSTAGRAM,
        items=parse_inbound_texts(payload),
        store=store,
        port=port,
        kill_switch=settings.kill_switch,
    )
