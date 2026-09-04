"""Owner Gmail tools. Reads plus the deterministic draft path; nothing here sends."""

from __future__ import annotations

from typing import Any

from app.capabilities.mail import mail_handlers
from app.capabilities.policy import execute_capability
from app.core.errors import PermissionDenied
from app.domain.events import Channel
from app.domain.gmail_drafts import apply_owner_gmail_draft
from app.domain.gmail_query import normalize_gmail_query
from app.domain.gmail_summaries import apply_owner_gmail_summary
from app.domain.tools import AdapterHttpError
from app.integrations.gmail import (
    DisabledGmailPort,
    GmailPort,
    InboundEmail,
    build_gmail_port,
    format_email_body,
    format_inbox_rows,
)
from app.tools.owner.types import ToolContext, ToolResult, _empty, _house_unavailable


def _gmail_port(ctx: ToolContext) -> GmailPort | None:
    port = ctx.gmail
    if port is None and ctx.settings.composio_ready():
        port = build_gmail_port(ctx.settings)
    if port is None or isinstance(port, DisabledGmailPort):
        return None
    return port


def _gmail_inbox(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    port = _gmail_port(ctx)
    if port is None:
        return _house_unavailable(ctx, "Gmail")
    try:
        payload = execute_capability(
            "mail.search",
            principal=ctx.principal,
            args={},
            handlers=mail_handlers(port),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="mail read denied")
    except AdapterHttpError as exc:
        return ToolResult(ok=False, error=f"Gmail read failed ({exc.tool_status()})")
    rows = payload.get("rows") or []
    text = format_inbox_rows(rows, timezone=ctx.timezone(), now=ctx.now)
    return _empty(text, "אין מיילים בתיבה.")


def _gmail_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    port = _gmail_port(ctx)
    if port is None:
        return _house_unavailable(ctx, "Gmail")
    normalized = normalize_gmail_query(query, now=ctx.now)
    try:
        payload = execute_capability(
            "mail.search",
            principal=ctx.principal,
            args={"query": normalized.query},
            handlers=mail_handlers(port),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="mail read denied")
    rows = payload.get("rows") or []
    text = format_inbox_rows(rows, timezone=ctx.timezone(), now=ctx.now)
    if not rows and normalized.changed:
        # Normalization rewrote the owner's phrasing before it hit Gmail and still came
        # back empty. Surface that instead of letting a silently-adjusted query look like
        # a clean "nothing found" -- the model needs this to decide whether to retry with
        # different wording or a Gmail operator, rather than reporting a dead end.
        text = (
            f"{text}\n\n"
            f'(Query was adjusted from "{query}" to "{normalized.query}" before '
            "searching, and still found nothing. Try different wording, an operator "
            "like from:/subject:, or a wider time range.)"
        )
    return ToolResult(ok=True, text=text)


def _gmail_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    message_id = str(args.get("message_id") or "").strip()
    if not message_id:
        return ToolResult(ok=False, error="message_id is required")
    port = _gmail_port(ctx)
    if port is None:
        return _house_unavailable(ctx, "Gmail")
    try:
        payload = execute_capability(
            "mail.read",
            principal=ctx.principal,
            args={"message_id": message_id},
            handlers=mail_handlers(port),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="mail read denied")
    if not payload.get("found"):
        return ToolResult(ok=True, text="לא מצאתי את המייל.")
    fetched = InboundEmail(
        message_id=str(payload.get("message_id") or message_id),
        sender=str(payload.get("sender") or ""),
        subject=str(payload.get("subject") or ""),
        text=str(payload.get("text") or ""),
        thread_id=str(payload.get("thread_id") or ""),
        timestamp=str(payload.get("timestamp") or ""),
    )
    body = format_email_body(fetched, timezone=ctx.timezone(), now=ctx.now)
    return ToolResult(ok=True, text=body)


def _gmail_create_draft(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    to = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not to or not (subject or body):
        return ToolResult(ok=False, error="to and subject or body are required")
    port = _gmail_port(ctx)
    if port is None:
        return _house_unavailable(ctx, "Gmail")
    text = f"שלח מייל ל {to} נושא: {subject}\n{body}"
    reply = apply_owner_gmail_draft(
        ctx.store,
        text=text,
        channel=Channel.TELEGRAM,
        port=port,
        kill_switch=ctx.kill_switch,
        demo_active=ctx.demo_active,
    )
    return ToolResult(ok=True, text=reply)


def _gmail_summary(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip() or "סיכום מייל"
    ack = apply_owner_gmail_summary(
        ctx.store,
        text=query,
        kill_switch=ctx.kill_switch,
        demo_active=ctx.demo_active,
    )
    return _empty(ack, "No Gmail thread matched. Name a thread: or lead id.")
