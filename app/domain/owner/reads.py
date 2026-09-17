"""Owner read answers that return real data instead of the generic status digest.

Read-only by construction. These functions answer "what is waiting for me?" and
"what happened on the website?" from Postgres. Deciding an approval and replying to
a lead stay on their existing typed paths, so a free-form owner question can never
become a write.

Read-only is not the same as trusted. The website reads below render text that a visitor
typed on assafweb.com into ` · `-joined, newline-separated lines that the owner loop then
reads as one tool result, so every visitor-authored field goes through
`sanitize_untrusted_line` before it is interpolated. Without that, a visitor could end a
field with a newline and mint a whole extra lead row in Assaf's brief.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.brain.context import sanitize_untrusted_line
from app.domain.lead_label import lead_display
from app.domain.sales import FitLevel, PainLevel, SalesState, manual_step_established

if TYPE_CHECKING:
    from app.db.store import CapturedWebsiteLead, LeadStore

_MAX_LISTED = 8


def _visitor_text(value: str) -> str:
    """One line, always. Visitor-authored fields only -- never a server-minted id."""
    return sanitize_untrusted_line(value, subject="owner website read")


def format_pending_approvals_ack(store: LeadStore, *, limit: int = _MAX_LISTED) -> str:
    """List what is actually waiting, not just how many. Approving stays explicit."""
    rows = store.list_all_pending_approvals()
    if not rows:
        return "אין כרגע שום דבר שמחכה לאישור."
    cap = max(1, limit)
    lines = [f"מחכים לאישור: {len(rows)}"]
    for row in rows[:cap]:
        # Website approvals carry no lead, so fall back to the resource
        # they act on rather than printing an empty subject.
        subject = row.lead_id or row.resource_id or row.approval_id
        lines.append(f"{subject} · {row.action}")
    if len(rows) > cap:
        lines.append(f"ועוד {len(rows) - cap}")
    lines.append("אישור צריך להיות מפורש על ליד מסוים. לא מאשרת הכל ביחד.")
    return "\n".join(lines)


def discovery_depth(sales: SalesState) -> int:
    """How far one conversation actually got. Used to rank and to gate 'engaged'.

    The single shared definition: owner reads (this module) and the website
    conversion funnel (`app/domain/funnel.py`) both call this instead of forking
    their own scoring. Never used to sell or gate a reply, only to report.
    """
    depth = 0
    if sales.workflow_known:
        depth += 1
    if manual_step_established(sales):
        depth += 1
    if sales.pain_level >= PainLevel.P2:
        depth += 1
    if sales.impact_confirmed:
        depth += 1
    if sales.buying_reality_known:
        depth += 1
    return depth


# Thin backward-compat alias: keep the old private name importable in case any
# other code still reaches for it.
_discovery_depth = discovery_depth


def _lead_line(sales: SalesState) -> str:
    # Lead with who they are. The state flags are the detail, not the identity.
    # `headline` and `display_name` are derived from what the visitor said; `lead_id` is
    # server-minted and is left alone so the id Assaf acts on is never rewritten here.
    parts = [
        lead_display(
            sales.lead_id,
            _visitor_text(sales.headline),
            _visitor_text(sales.display_name),
        )
    ]
    if sales.workflow_known:
        parts.append("workflow")
    if manual_step_established(sales):
        parts.append("שלב ידני")
    if sales.impact_confirmed:
        parts.append("עלות מאומתת")
    if sales.whatsapp_handoff_offered:
        parts.append("הוצע וואטסאפ")
    if sales.willingness_to_meet is True:
        parts.append("רוצה פגישה")
    if sales.owner_required:
        parts.append("מחכה לך")
    return " · ".join(parts)


def _ranked_snapshots(snapshots: list[SalesState]) -> list[SalesState]:
    return sorted(
        snapshots,
        key=lambda item: (_discovery_depth(item), int(item.pain_level)),
        reverse=True,
    )


def top_website_lead_id(store: LeadStore) -> str | None:
    """The conversation "what's most interesting?" is about when nothing was named.

    A drill-down right after a counts-only brief has no id in the transcript to
    attach to, so the anchor comes from the same ranking the read itself uses.
    Conversations that never got past the first question are excluded: pointing
    Assaf at an empty one would be worse than admitting there is nothing.
    """
    snapshots = store.list_sales_snapshots()
    ranked = [
        item for item in _ranked_snapshots(snapshots) if _discovery_depth(item) >= 1
    ]
    return ranked[0].lead_id if ranked else None


def _captured_lead_line(lead: CapturedWebsiteLead) -> str:
    """v2 has no sales-workflow state to render; show the fields C3a fills instead."""
    fields = lead.fields
    # Every one of these four is website visitor free text (`business` is the one-time
    # `state.business_context` latch), so each is flattened before it is joined into a line.
    name = _visitor_text(fields.get("name", ""))
    business = _visitor_text(fields.get("business", ""))
    want = _visitor_text(fields.get("want", ""))
    next_step = _visitor_text(fields.get("next_step", ""))
    label = name or business or lead.contact_id
    parts = [label]
    if business and business != label:
        parts.append(business[:60])
    if want:
        parts.append(f"צריך: {want[:60]}")
    if next_step:
        parts.append(f"הבא: {next_step[:60]}")
    return " · ".join(parts)


def format_website_conversations_ack(store: LeadStore) -> str:
    """What the website conversations actually produced, ranked by depth.

    The counts describe the sample that was read, and the total is reported
    separately so a capped read never looks like the whole book. Legacy
    ``SalesState`` conversations and v2 CRM website captures are two disjoint data
    sources today (the legacy website path has no live caller), so they are listed
    in separate sections rather than force-ranked together.
    """
    snapshots = store.list_sales_snapshots()
    v2_leads = store.list_captured_website_leads(limit=_MAX_LISTED)
    if not snapshots and not v2_leads:
        return "אין עדיין שיחות מהאתר לנתח."
    total_legacy = store.count_sales_snapshots()
    total_v2 = store.count_captured_website_leads()
    lines: list[str] = []
    if snapshots:
        engaged = [item for item in snapshots if _discovery_depth(item) >= 2]
        offered = [item for item in snapshots if item.whatsapp_handoff_offered]
        waiting = [item for item in snapshots if item.owner_required]
        header = f"שיחות מהאתר: {total_legacy + total_v2}"
        if total_legacy > len(snapshots):
            header += f" (בדקתי {len(snapshots)} אחרונות)"
        lines.append(
            f"{header} · "
            f"discovery משמעותי {len(engaged)} · "
            f"הוצע וואטסאפ {len(offered)} · "
            f"מחכות לך {len(waiting)}"
        )
        ranked = _ranked_snapshots(snapshots)
        interesting = [item for item in ranked if _discovery_depth(item) >= 1][:_MAX_LISTED]
        if interesting:
            lines.append("הכי מעניינות:")
            lines.extend(_lead_line(item) for item in interesting)
        else:
            lines.append("אף שיחה עוד לא עברה את השלב הראשון.")
        poor = [item for item in snapshots if item.fit == FitLevel.POOR]
        if poor:
            lines.append(f"לא מתאימות: {len(poor)}")
    else:
        lines.append(f"שיחות מהאתר: {total_legacy + total_v2}")
    if v2_leads:
        lines.append("לידים חדשים מהאתר:")
        lines.extend(_captured_lead_line(lead) for lead in v2_leads)
        if total_v2 > len(v2_leads):
            lines.append(f"ועוד {total_v2 - len(v2_leads)}")
    return "\n".join(lines)
