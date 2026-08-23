"""Conversation memory read model.

Rebuilt from `canonical_events` message_in / message_out rows, so there is no second
system of record and no schema change. Text only: no SDK objects, no secrets, no
provider payloads. Untrusted prospect text stays data — a turn never becomes an
instruction.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

MAX_TURNS = 24
MAX_TURN_CHARS = 700

ROLE_MIA = "mia"
_PROSPECT_ROLES = frozenset({"prospect", "business_lead", "customer"})
_OWNER_ROLES = frozenset({"owner"})


class ConversationTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    text: str


def normalize_turn_role(actor_role: str, event_type: str) -> str:
    """Collapse provider actor roles onto `mia` / `prospect` / `owner`."""
    if event_type == "message_out" or actor_role == ROLE_MIA:
        return ROLE_MIA
    if actor_role in _OWNER_ROLES:
        return "owner"
    if actor_role in _PROSPECT_ROLES:
        return "prospect"
    return "prospect"


def clip_turn_text(text: str) -> str:
    return text.strip()[:MAX_TURN_CHARS]


def counterpart_turns(turns: list[ConversationTurn]) -> list[ConversationTurn]:
    """Turns written by the human, i.e. everything Mia did not say."""
    return [turn for turn in turns if turn.role != ROLE_MIA]


def mia_turns(turns: list[ConversationTurn]) -> list[ConversationTurn]:
    return [turn for turn in turns if turn.role == ROLE_MIA]


def human_turn_count(turns: list[ConversationTurn]) -> int:
    return len(counterpart_turns(turns))


def render_transcript(turns: list[ConversationTurn], *, mia_label: str = "MIA") -> str:
    """Plain transcript for a prompt. Labels are fixed; turn text is never trusted."""
    lines: list[str] = []
    for turn in turns:
        label = mia_label if turn.role == ROLE_MIA else turn.role.upper()
        lines.append(f"{label}: {turn.text}")
    return "\n".join(lines)


def _normalize_for_similarity(text: str) -> str:
    folded = "".join(
        char if (char.isalnum() or char.isspace()) else " " for char in text.lower()
    )
    return " ".join(folded.split())


def repeats_previous_mia_turn(
    candidate: str, turns: list[ConversationTurn], *, threshold: float = 0.86
) -> bool:
    """True when the candidate reply is effectively a prior Mia line said again."""
    normalized = _normalize_for_similarity(candidate)
    if not normalized:
        return False
    for turn in mia_turns(turns):
        previous = _normalize_for_similarity(turn.text)
        if not previous:
            continue
        if normalized == previous:
            return True
        if _token_overlap(normalized, previous) >= threshold:
            return True
    return False


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    smaller = min(len(left_tokens), len(right_tokens))
    if smaller < 4:
        return 0.0
    return len(left_tokens & right_tokens) / smaller
