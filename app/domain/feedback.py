"""Owner correction event store (persist-only; never activates prompts)."""

import re
from enum import StrEnum

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.learning import MAX_INSTRUCTION_BODY

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})

_REMEMBER_SCOPE_PHRASES: tuple[str, ...] = (
    "remember this",
    "לזכור",
    "תזכרי",
)


class CorrectionScope(StrEnum):
    THIS_TURN = "this_turn"
    REMEMBER = "remember"


def _keyword_in_text(text: str, keyword: str) -> bool:
    """Match phrases as substrings; match single tokens on ASCII word boundaries."""
    haystack = text.translate(_APOSTROPHES).lower()
    needle = keyword.lower()
    if " " in needle:
        return needle in haystack
    return (
        re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    )


def classify_correction_scope(text: str) -> CorrectionScope:
    if any(_keyword_in_text(text, phrase) for phrase in _REMEMBER_SCOPE_PHRASES):
        return CorrectionScope.REMEMBER
    return CorrectionScope.THIS_TURN


def persist_owner_correction(
    store,
    *,
    provider: str,
    provider_event_id: str,
    body: str,
    kill_switch: bool,
    scope: CorrectionScope | None = None,
) -> bool:
    """R1 assert_allowed; persist logged correction; return True if written. Never raises."""
    try:
        assert_allowed(
            RiskAction(name="owner_correction_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        trimmed = body[:MAX_INSTRUCTION_BODY]
        resolved_scope = scope if scope is not None else classify_correction_scope(body)
        return store.save_owner_correction(
            provider=provider,
            provider_event_id=provider_event_id,
            scope=resolved_scope.value,
            body=trimmed,
            status="logged",
        )
    except (PolicyDenied, RuntimeError):
        return False
