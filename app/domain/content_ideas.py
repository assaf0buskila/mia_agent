"""Owner content ideas from organic performance (persist-only; no posts, no publish)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.content_insights import ALLOWLISTED_MEDIA_TYPES
from app.domain.followups import follow_up_due_on

if TYPE_CHECKING:
    from app.db.store import LeadStore

ALLOWLISTED_KINDS = frozenset(
    {"more_image", "more_video", "more_carousel_album", "more_reels"}
)

_MEDIA_TYPE_TO_KIND: dict[str, str] = {
    "IMAGE": "more_image",
    "VIDEO": "more_video",
    "CAROUSEL_ALBUM": "more_carousel_album",
    "REELS": "more_reels",
}

_KIND_HE: dict[str, str] = {
    "more_reels": "עוד רילס",
    "more_video": "עוד וידאו",
    "more_image": "עוד תמונה",
    "more_carousel_album": "עוד קרוסלה",
}

_NO_PUBLISH_LINE = "אלה רעיונות בלבד. לא כתבתי פוסט ולא פרסמתי."
_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ContentIdeaSnapshot(BaseModel):
    idea_date: str
    kinds: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("idea_date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        if _DATE_ISO.fullmatch(value) is None:
            raise ValueError("invalid idea_date")
        return value

    @field_validator("kinds")
    @classmethod
    def _validate_kinds(cls, value: list[str]) -> list[str]:
        if len(value) > 3:
            raise ValueError("kinds max 3")
        seen: set[str] = set()
        ordered: list[str] = []
        for kind in value:
            if kind not in ALLOWLISTED_KINDS:
                raise ValueError(f"invalid kind: {kind}")
            if kind in seen:
                continue
            seen.add(kind)
            ordered.append(kind)
        return ordered


class ContentIdeaRecord(BaseModel):
    idea_date: str
    kinds: list[str] = Field(default_factory=list)


def _views_rank_key(views: str) -> tuple[int, int]:
    if views.isdigit():
        return (0, -int(views))
    return (1, 0)


def _rank_insights(rows: list) -> list:
    return sorted(
        rows,
        key=lambda row: (-row.lead_signals, *_views_rank_key(row.views)),
    )


def compute_content_idea_snapshot(
    store: LeadStore,
    *,
    timezone: str,
    now: datetime | None = None,
) -> ContentIdeaSnapshot | None:
    instant = now if now is not None else datetime.now(UTC)
    try:
        idea_date = follow_up_due_on(now=instant, timezone=timezone, offset_days=0)
    except (ValueError, OSError, KeyError):
        return None
    kinds: list[str] = []
    seen_kinds: set[str] = set()
    for row in _rank_insights(store.list_content_insights()):
        if row.media_type not in ALLOWLISTED_MEDIA_TYPES:
            continue
        kind = _MEDIA_TYPE_TO_KIND.get(row.media_type)
        if kind is None or kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        kinds.append(kind)
        if len(kinds) >= 3:
            break
    return ContentIdeaSnapshot(idea_date=idea_date, kinds=kinds)


def format_content_ideas_ack(snapshot: ContentIdeaSnapshot) -> str:
    lines = ["רעיונות לתוכן (לא פוסטים מוכנים):"]
    if snapshot.kinds:
        for kind in snapshot.kinds:
            label = _KIND_HE.get(kind, kind)
            lines.append(f"• {label} — על בסיס אותות ליד בנתונים הקיימים.")
    else:
        lines.append("אין נתוני ביצועי תוכן. לא יצרתי רעיונות.")
    lines.append(_NO_PUBLISH_LINE)
    return "\n".join(lines)


def apply_content_idea_policy(
    store: LeadStore,
    *,
    snapshot: ContentIdeaSnapshot,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    if demo_active or kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="content_idea_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_content_idea(idea_date=snapshot.idea_date, kinds=snapshot.kinds)


def apply_owner_content_ideas(
    store: LeadStore,
    *,
    timezone: str,
    kill_switch: bool,
    demo_active: bool,
    now: datetime | None = None,
) -> str | None:
    """Compute idea kinds from content_insights, optionally persist, return Hebrew ack."""
    if demo_active:
        return None
    snapshot = compute_content_idea_snapshot(store, timezone=timezone, now=now)
    if snapshot is None:
        return None
    if not kill_switch:
        apply_content_idea_policy(
            store,
            snapshot=snapshot,
            kill_switch=kill_switch,
            demo_active=demo_active,
        )
    return format_content_ideas_ack(snapshot)
