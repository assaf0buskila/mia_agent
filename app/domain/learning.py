"""Owner instruction proposal store (propose-only; never activates prompts)."""

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed

MAX_INSTRUCTION_BODY = 2000
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})

_CORRECTION_PHRASES: tuple[str, ...] = (
    "that's wrong",
    "correction",
    "זה לא נכון",
)

_BEHAVIOR_RULE_PHRASES: tuple[str, ...] = (
    "never say",
    "always say",
    "אל תגידי",
    "תמיד תגידי",
)


class InstructionKind(StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"
    BEHAVIOR_RULE = "behavior_rule"
    CORRECTION = "correction"


_WRITABLE_KINDS: frozenset[InstructionKind] = frozenset(
    {
        InstructionKind.PREFERENCE,
        InstructionKind.BEHAVIOR_RULE,
        InstructionKind.CORRECTION,
    }
)


class ProposedInstruction(BaseModel):
    kind: InstructionKind
    body: str = Field(max_length=MAX_INSTRUCTION_BODY)
    status: str = "proposed"


def _keyword_in_text(text: str, keyword: str) -> bool:
    """Match phrases as substrings; match single tokens on ASCII word boundaries."""
    haystack = text.translate(_APOSTROPHES).lower()
    needle = keyword.lower()
    if " " in needle:
        return needle in haystack
    return (
        re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    )


def classify_instruction_kind(text: str) -> InstructionKind:
    """Classify durable owner instruction kind (preference path only; first match wins)."""
    if any(_keyword_in_text(text, phrase) for phrase in _CORRECTION_PHRASES):
        return InstructionKind.CORRECTION
    if any(_keyword_in_text(text, phrase) for phrase in _BEHAVIOR_RULE_PHRASES):
        return InstructionKind.BEHAVIOR_RULE
    return InstructionKind.PREFERENCE


def propose_owner_instruction(
    *,
    store,
    provider: str,
    provider_event_id: str,
    body: str,
    kill_switch: bool,
    kind: InstructionKind | None = None,
) -> bool:
    """R1 assert_allowed; persist proposed; return True if written. Never raises."""
    try:
        assert_allowed(
            RiskAction(name="owner_instruction_propose", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        resolved = kind if kind is not None else classify_instruction_kind(body)
        if resolved not in _WRITABLE_KINDS:
            return False
        trimmed = body[:MAX_INSTRUCTION_BODY]
        store.save_proposed_instruction(
            provider=provider,
            provider_event_id=provider_event_id,
            kind=resolved.value,
            body=trimmed,
            status="proposed",
        )
        return True
    except (PolicyDenied, RuntimeError):
        return False
