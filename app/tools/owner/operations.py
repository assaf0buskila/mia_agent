"""Owner operating reads: briefs, leads, approvals, status and the connection audit.

`_owner_system_audit` aggregates the provider reads that live in the leaf modules.
It imports them inside the function so this module keeps no module-scope dependency
on sheets/gmail/calendar/analytics.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domain.briefs import apply_owner_meeting_brief
from app.domain.content_ideas import apply_owner_content_ideas
from app.domain.handoff.hot import format_hot_leads_ack
from app.domain.lead_reviews import apply_owner_lead_review, format_lead_matches
from app.domain.owner_briefs import apply_owner_brief
from app.domain.owner_connection_audit import OwnerAuditResult, format_owner_connection_audit
from app.domain.owner_notify import apply_owner_notify
from app.domain.owner_reads import format_pending_approvals_ack, format_website_conversations_ack
from app.domain.owner_snapshot import format_operator_snapshot_ack
from app.domain.owner_status import format_owner_status_ack
from app.domain.owner_weeklies import apply_owner_weekly
from app.domain.whatsapp_drafts import draft_whatsapp_for_assaf
from app.tools.owner.types import ToolContext, ToolResult, _empty

# ----------------------------------------------------------------- owner reads


def _daily_brief(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        apply_owner_brief(
            ctx.store,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
            now=ctx.now,
        ),
        "No activity recorded for today yet.",
    )


def _weekly_brief(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        apply_owner_weekly(
            ctx.store,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
            now=ctx.now,
        ),
        "No activity recorded for this week yet.",
    )


def _hot_leads(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        format_hot_leads_ack(ctx.store, principal=ctx.principal),
        "No hot leads right now.",
    )


def _pending_approvals(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(format_pending_approvals_ack(ctx.store), "Nothing is waiting for approval.")


def _website_conversations(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(format_website_conversations_ack(ctx.store), "No website conversations yet.")


def _owner_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        format_owner_status_ack(ctx.store, principal=ctx.principal, timezone=ctx.timezone()),
        "Nothing to report.",
    )


def _operator_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        format_operator_snapshot_ack(ctx.store, timezone=ctx.timezone()),
        "Nothing to report.",
    )


def _owner_system_audit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Run the defined Owner operating-surface audit in one model tool call.

    The individual provider reads remain bounded and retain their own policy checks.
    This aggregates their results so a broad request cannot be cut short by the
    agent loop's normal per-turn call budget or described as a provider limitation.
    """
    del args

    from app.tools.owner.analytics import _instagram_insights, _linkedin_snapshot, _website_kpis
    from app.tools.owner.calendar import _calendar_agenda, _calendar_availability
    from app.tools.owner.gmail import _gmail_inbox
    from app.tools.owner.sheets import _sheets_read

    def probe(label: str, callback: Callable[[], ToolResult]) -> OwnerAuditResult:
        try:
            result = callback()
        except Exception as exc:  # noqa: BLE001 - one unavailable integration must not hide others
            return OwnerAuditResult(label=label, ok=False, text=type(exc).__name__)
        return OwnerAuditResult(label=label, ok=result.ok, text=result.text or result.error)

    audit_sheet = ctx.settings.resolved_sheets_spreadsheet_id()
    sheets_result = probe(
        "Google Sheets (גיליון מורשה)",
        lambda: _sheets_read(ctx, {"spreadsheet_id": audit_sheet, "range": None}),
    )

    results = [
        probe("Gmail", lambda: _gmail_inbox(ctx, {})),
        probe("Calendar agenda (today)", lambda: _calendar_agenda(ctx, {"range": "today"})),
        probe("Calendar availability", lambda: _calendar_availability(ctx, {})),
        probe("LinkedIn profile", lambda: _linkedin_snapshot(ctx, {})),
        probe("Instagram Insights", lambda: _instagram_insights(ctx, {})),
        probe("AssafWeb SEO, GSC and GA4", lambda: _website_kpis(ctx, {})),
        sheets_result,
        probe("Hot leads", lambda: _hot_leads(ctx, {})),
        probe("Pending approvals", lambda: _pending_approvals(ctx, {})),
        probe("Website conversations", lambda: _website_conversations(ctx, {})),
        probe("Daily brief", lambda: _daily_brief(ctx, {})),
        probe(
            "New booked meetings",
            lambda: ToolResult(
                ok=True,
                text=format_operator_snapshot_ack(
                    ctx.store,
                    principal=ctx.principal,
                    timezone=ctx.timezone(),
                    matched_types=["owner_notify"],
                ),
            ),
        ),
    ]
    return ToolResult(ok=True, text=format_owner_connection_audit(results), max_chars=11_000)


def _lead_review(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or args.get("lead_id") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    ack = apply_owner_lead_review(
        ctx.store,
        text=query,
        kill_switch=ctx.kill_switch,
        demo_active=ctx.demo_active,
    )
    if ack:
        return ToolResult(ok=True, text=ack)
    return _empty(format_lead_matches(ctx.store, query), "No matching lead.")


def _find_leads(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    return _empty(format_lead_matches(ctx.store, query), "No matching lead.")


def _meeting_brief(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    lead_id = str(args.get("lead_id") or "").strip()
    if not lead_id:
        return ToolResult(ok=False, error="lead_id is required")
    return _empty(
        apply_owner_meeting_brief(
            ctx.store,
            text=lead_id,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
        ),
        f"No meeting brief available for {lead_id}.",
    )


def _whatsapp_draft_assaf(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx
    drafted = draft_whatsapp_for_assaf(
        body=str(args.get("body") or ""),
        destination=str(args.get("destination") or "assaf"),
    )
    if isinstance(drafted, str):
        return ToolResult(ok=True, text=drafted)
    return ToolResult(
        ok=True,
        text=f"WhatsApp draft for Assaf (not sent): {drafted.body}",
    )


def _booked_meetings(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        apply_owner_notify(
            ctx.store,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
        ),
        "Nothing new was booked.",
    )


def _content_ideas(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        apply_owner_content_ideas(
            ctx.store,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
        ),
        "No content ideas available.",
    )
