"""Instagram organic content insight persist policy (§16 / §19). No Graph calls here."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed

ALLOWLISTED_MEDIA_TYPES = frozenset({"IMAGE", "VIDEO", "CAROUSEL_ALBUM", "REELS"})
_MEDIA_ID_RE = re.compile(r"^\d{1,32}$")


class ContentInsight(BaseModel):
    media_id: str
    media_type: str = ""
    account: str = ""
    post_name: str = ""
    views: str | None = None
    reach: str | None = None
    likes: str | None = None
    comments: str | None = None
    saved: str | None = None


class ContentInsightRecord(BaseModel):
    media_id: str
    media_type: str = ""
    views: str = ""
    reach: str = ""
    likes: str = ""
    comments: str = ""
    saved: str = ""
    lead_signals: int = Field(default=0, ge=0)


def is_allowlisted_media_id(media_id: str) -> bool:
    return _MEDIA_ID_RE.fullmatch(media_id) is not None


def apply_content_insight_policy(
    store,
    *,
    items: list[ContentInsight],
    kill_switch: bool,
) -> None:
    if kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="content_insight_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    for item in items:
        if not is_allowlisted_media_id(item.media_id):
            continue
        if item.media_type not in ALLOWLISTED_MEDIA_TYPES:
            continue
        lead_signals = store.count_attribution_for_ig_content(item.media_id)
        store.upsert_content_insight(
            media_id=item.media_id,
            media_type=item.media_type,
            views=item.views or "",
            reach=item.reach or "",
            likes=item.likes or "",
            comments=item.comments or "",
            saved=item.saved or "",
            lead_signals=lead_signals,
        )
