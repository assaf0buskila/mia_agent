"""Owner read answers that return real data instead of the generic status digest.

Read-only by construction. These functions answer "what is waiting for me?" and
"what happened on the website?" from Postgres. Deciding an approval and replying to
a lead stay on their existing typed paths, so a free-form owner question can never
become a write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.lead_label import lead_display
from app.domain.sales import FitLevel, PainLevel, SalesState, manual_step_established

if TYPE_CHECKING:
    from app.db.store import LeadStore

_MAX_LISTED = 8


def format_pending_approvals_ack(store: LeadStore, *, limit: int = _MAX_LISTED) -> str:
    """List what is actually waiting, not just how many. Approving stays explicit."""
    rows = store.list_all_pending_approvals()
    if not rows:
        return "אין כרגע שום דבר שמחכה לאישור."
    cap = max(1, limit)
    lines = [f"מחכים לאישור: {len(rows)}"]
    for row in rows[:cap]:
        # Campaign and website approvals carry no lead, so fall back to the resource
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
    parts = [lead_display(sales.lead_id, sales.headline)]
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


def format_website_conversations_ack(store: LeadStore) -> str:
    """What the website conversations actually produced, ranked by depth.

    The counts describe the sample that was read, and the total is reported
    separately so a capped read never looks like the whole book.
    """
    snapshots = store.list_sales_snapshots()
    if not snapshots:
        return "אין עדיין שיחות מהאתר לנתח."
    total = store.count_sales_snapshots()
    engaged = [item for item in snapshots if _discovery_depth(item) >= 2]
    offered = [item for item in snapshots if item.whatsapp_handoff_offered]
    waiting = [item for item in snapshots if item.owner_required]
    header = f"שיחות מהאתר: {total}"
    if total > len(snapshots):
        header += f" (בדקתי {len(snapshots)} אחרונות)"
    lines = [
        f"{header} · "
        f"discovery משמעותי {len(engaged)} · "
        f"הוצע וואטסאפ {len(offered)} · "
        f"מחכות לך {len(waiting)}"
    ]
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
    return "\n".join(lines)
