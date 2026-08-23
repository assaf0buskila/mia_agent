"""Deal stage persistence (§32): one row per lead on offer_meeting or handoff."""

from datetime import UTC, datetime

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.events import Channel, build_deal_updated_event

STAGE_MEETING_OFFERED = "meeting_offered"
STAGE_PROPOSAL = "proposal"
ALLOWLISTED_STAGES = frozenset({STAGE_MEETING_OFFERED, STAGE_PROPOSAL})
CONFIDENCE_UTM = "utm"
CONFIDENCE_IG = "ig"
CONFIDENCE_META_AD = "meta_ad"
CONFIDENCE_UNKNOWN = "unknown"
ALLOWLISTED_CONFIDENCE = frozenset({
    CONFIDENCE_UTM,
    CONFIDENCE_IG,
    CONFIDENCE_META_AD,
    CONFIDENCE_UNKNOWN,
})

_UTM_CONFIDENCE_KEYS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
})
_IG_CONFIDENCE_KEYS = frozenset({
    "ig_content_id",
    "ig_trigger_source",
    "ig_ref",
})

_STAGE_RANK = {
    STAGE_MEETING_OFFERED: 0,
    STAGE_PROPOSAL: 1,
}


def confidence_from_attribution(payload: dict[str, str] | None) -> str:
    if not payload:
        return CONFIDENCE_UNKNOWN
    if any(payload.get(key) for key in _UTM_CONFIDENCE_KEYS):
        return CONFIDENCE_UTM
    if any(payload.get(key) for key in _IG_CONFIDENCE_KEYS):
        return CONFIDENCE_IG
    if (
        payload.get("meta_ad_id")
        or payload.get("meta_post_id")
        or payload.get("meta_campaign_id")
    ):
        return CONFIDENCE_META_AD
    return CONFIDENCE_UNKNOWN


def stage_for_action(action: str) -> str | None:
    action_key = str(action).lower().strip()
    if action_key == "offer_meeting":
        return STAGE_MEETING_OFFERED
    if action_key == "handoff":
        return STAGE_PROPOSAL
    return None


def apply_deal_policy(
    store,
    *,
    lead_id: str,
    channel: Channel,
    action: str,
    kill_switch: bool,
    now: datetime | None = None,
) -> None:
    """Persist deal stage on offer_meeting or handoff. Never sends; swallows PolicyDenied only."""
    stage = stage_for_action(action)
    if stage is None:
        return
    if kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="deal_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return

    confidence = confidence_from_attribution(store.get_attribution_payload(lead_id))
    existing = store.get_deal(lead_id)
    if existing is not None:
        existing_rank = _STAGE_RANK.get(existing.stage, -1)
        new_rank = _STAGE_RANK.get(stage, -1)
        if new_rank < existing_rank:
            return
    effective_now = now or datetime.now(UTC)
    store.upsert_deal(
        lead_id=lead_id,
        stage=stage,
        source=channel.value,
        attribution_confidence=confidence,
    )
    store.save_canonical_event(
        provider=channel.value,
        event=build_deal_updated_event(
            provider=channel.value,
            channel=channel,
            lead_id=lead_id,
            stage=stage,
            source=channel.value,
            attribution_confidence=confidence,
            occurred_at=effective_now,
        ),
    )
