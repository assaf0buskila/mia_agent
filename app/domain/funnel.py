"""Website conversion funnel: how far website traffic gets before it converts.

Read-only by construction: no writes, no PII, no lead ids. Mia is a sales operator
with every turn logged, but until this module nothing ever aggregated those events
into a funnel, so nobody could tell whether the website actually converts (see
ADR-029). This is a scorecard, not a drill-down: naming a specific conversation
stays the job of `app/domain/owner_reads.py`.

Every stage is an EVENT count on the canonical event / behavior log for the
requested local calendar day, taken as-is from counters that already exist
elsewhere in the codebase, with one exception: `engaged`. `SalesStateRow` and
`LeadRow` carry no created/updated timestamp, so there is no way to ask "how many
conversations reached discovery depth 2 today" — the store can only return a
recency-ordered sample of the most recent conversations (`list_sales_snapshots`),
regardless of when they happened. `engaged` is that sample, not a same-day count.
Treat it as directional, not exact, until leads/sales-state carry a timestamp
(a migration, out of scope for this slice).

None of these counters are distinct-visitor counts. `sessions` and `conversations`
count `mia_opened` / `conversation_started` behavior events, so a visitor who opens
the widget twice in one day is counted twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.domain.followups import local_day_bounds_utc_iso
from app.domain.owner_reads import discovery_depth

if TYPE_CHECKING:
    from app.db.store import LeadStore

_ENGAGED_MIN_DEPTH = 2


def _pct(numerator: int, denominator: int) -> int:
    """Integer percent, clamped to 0..100.

    The clamp is not cosmetic, it is required for correctness. These counters do not
    all share one window: `engaged` is a recency-ordered sample across all time (see
    the module docstring) while `conversations` is same-day, and a single conversation
    can produce more than one `meeting_offered` event. So a numerator CAN legitimately
    exceed its denominator, and an unclamped value would blow the `le=100` bound on
    the model fields and raise `ValidationError` from inside the owner's daily brief.
    A rate above 100 percent is meaningless to display anyway, so it reads as 100.
    """
    if denominator <= 0:
        return 0
    return max(0, min(100, round(100 * numerator / denominator)))


class WebsiteFunnel(BaseModel):
    """One local-day snapshot of the website sales funnel. See module docstring
    for what each counter genuinely measures and its known limits.
    """

    day: str

    sessions: int = Field(ge=0, description="`mia_opened` behavior events, same day.")
    conversations: int = Field(
        ge=0, description="`conversation_started` behavior events, same day."
    )
    engaged: int = Field(
        ge=0,
        description=(
            "Sales snapshots with discovery_depth >= 2, from a recency-ordered "
            "sample (list_sales_snapshots default limit). NOT day-scoped: "
            "SalesStateRow has no timestamp. See module docstring."
        ),
    )
    meetings_offered: int = Field(
        ge=0, description="`meeting_offered` canonical events, same day."
    )
    whatsapp_offered: int = Field(
        ge=0, description="`whatsapp_handoff_offered` behavior events, same day."
    )
    whatsapp_clicked: int = Field(
        ge=0, description="`whatsapp_handoff` behavior events, same day."
    )
    meetings_booked: int = Field(
        ge=0, description="`meeting_booked` canonical events, same day."
    )
    handoffs: int = Field(ge=0, description="`handoff` canonical events, same day.")

    conversation_rate_pct: int = Field(ge=0, le=100)
    engaged_rate_pct: int = Field(ge=0, le=100)
    meeting_offer_rate_pct: int = Field(ge=0, le=100)
    whatsapp_click_rate_pct: int = Field(ge=0, le=100)
    meeting_booked_rate_pct: int = Field(ge=0, le=100)

    def has_signal(self) -> bool:
        """False when every counter is zero, so a quiet day does not print a
        wall of zeros in the owner brief."""
        return any(
            (
                self.sessions,
                self.conversations,
                self.engaged,
                self.meetings_offered,
                self.whatsapp_offered,
                self.whatsapp_clicked,
                self.meetings_booked,
                self.handoffs,
            )
        )


def compute_website_funnel(
    store: LeadStore, *, timezone: str, now: datetime | None = None
) -> WebsiteFunnel | None:
    """Compute today's (local `timezone`) website funnel. None on a bad timezone.

    Read-only: no writes, no PII, no lead ids. Mirrors
    `app/domain/owner_briefs.py::compute_daily_brief`'s local-day-bounds pattern.
    """
    instant = now if now is not None else datetime.now(UTC)
    bounds = local_day_bounds_utc_iso(now=instant, timezone=timezone)
    if bounds is None:
        return None
    occurred_from, occurred_to = bounds
    day = occurred_from[:10]

    sessions = store.count_behavior_events(
        kind="mia_opened", occurred_from=occurred_from, occurred_to=occurred_to
    )
    conversations = store.count_behavior_events(
        kind="conversation_started",
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    # Not day-scoped: see module docstring and the `engaged` field description.
    snapshots = store.list_sales_snapshots()
    engaged = sum(
        1 for item in snapshots if discovery_depth(item) >= _ENGAGED_MIN_DEPTH
    )
    meetings_offered = store.count_canonical_events(
        event_type="meeting_offered",
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    whatsapp_offered = store.count_behavior_events(
        kind="whatsapp_handoff_offered",
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    whatsapp_clicked = store.count_behavior_events(
        kind="whatsapp_handoff", occurred_from=occurred_from, occurred_to=occurred_to
    )
    meetings_booked = store.count_canonical_events(
        event_type="meeting_booked",
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    handoffs = store.count_canonical_events(
        event_type="handoff", occurred_from=occurred_from, occurred_to=occurred_to
    )

    return WebsiteFunnel(
        day=day,
        sessions=sessions,
        conversations=conversations,
        engaged=engaged,
        meetings_offered=meetings_offered,
        whatsapp_offered=whatsapp_offered,
        whatsapp_clicked=whatsapp_clicked,
        meetings_booked=meetings_booked,
        handoffs=handoffs,
        conversation_rate_pct=_pct(conversations, sessions),
        engaged_rate_pct=_pct(engaged, conversations),
        meeting_offer_rate_pct=_pct(meetings_offered, conversations),
        whatsapp_click_rate_pct=_pct(whatsapp_clicked, whatsapp_offered),
        meeting_booked_rate_pct=_pct(meetings_booked, meetings_offered),
    )


def format_website_funnel(funnel: WebsiteFunnel) -> str:
    """Hebrew, owner-facing, labeled lines. Lead-id free, no PII, no hyphen/dash."""
    return "\n".join(
        [
            f"משפך באתר {funnel.day}",
            f"פתיחות: {funnel.sessions}",
            f"שיחות: {funnel.conversations} ({funnel.conversation_rate_pct}%)",
            f"discovery משמעותי: {funnel.engaged} ({funnel.engaged_rate_pct}%)",
            f"פגישה הוצעה: {funnel.meetings_offered} ({funnel.meeting_offer_rate_pct}%)",
            f"וואטסאפ הוצע: {funnel.whatsapp_offered}",
            f"וואטסאפ נלחץ: {funnel.whatsapp_clicked} ({funnel.whatsapp_click_rate_pct}%)",
            f"פגישות נקבעו: {funnel.meetings_booked} ({funnel.meeting_booked_rate_pct}%)",
            f"העברות: {funnel.handoffs}",
        ]
    )
