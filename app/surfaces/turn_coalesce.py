"""Stitch rapid owner turns and keep the last asked toolkit in view.

Telegram (and tests) debounce a burst into one user message. History still comes
from `list_conversation_turns`. This module does not store phone numbers.
"""

from __future__ import annotations

import threading

from app.domain.memory import ConversationTurn

COALESCE_WAIT_S = 1.5
OWNER_TURN_TIMEOUT_S = 45.0
HANG_REPLY = "הבדיקה לא עברה כרגע (תם הזמן). תנסה שוב."
FAIL_REPLY = "הבדיקה לא עברה כרגע. תנסה שוב."

_CONTINUE_MARKERS = (
    "תמשיך",
    "תמשיכי",
    "continue",
    "go on",
    "עוד נתונים",
    "תמשיך נתונים",
)
_TOOLKIT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "GA/GSC",
        (
            "search console",
            "gsc",
            "google analytics",
            "ga4",
            "אנליטיקס",
            "קונסול",
            "seo",
            "on the site",
            "באתר",
            "לאתר",
        ),
    ),
    ("Instagram", ("instagram", "אינסטגרם")),
    (
        "CRM",
        (
            "contacts",
            "crm",
            "איש קשר",
            "sheets",
            "sheet",
            "שיט",
            "האקסל",
            "אקסל",
            "excel",
            "גוגל שיטס",
        ),
    ),
)

_lock = threading.Lock()
_pending: dict[str, list[dict[str, str]]] = {}


def reset_pending_turns() -> None:
    with _lock:
        _pending.clear()


def enqueue_turn(key: str, item: dict[str, str]) -> None:
    if not key:
        return
    with _lock:
        _pending.setdefault(key, []).append(dict(item))


def claim_burst(key: str, leader_id: str) -> list[dict[str, str]] | None:
    """Take the queued burst when `leader_id` is still the newest message."""
    if not key or not leader_id:
        return None
    with _lock:
        bucket = _pending.get(key) or []
        if not bucket or bucket[-1].get("id") != leader_id:
            return None
        claimed = list(bucket)
        _pending[key] = []
        return claimed


def take_if_still_pending(key: str, item_id: str) -> list[dict[str, str]] | None:
    """Safety claim if a burst is still waiting after the leader should have run."""
    if not key or not item_id:
        return None
    with _lock:
        bucket = _pending.get(key) or []
        if not any(row.get("id") == item_id for row in bucket):
            return None
        if bucket[-1].get("id") != item_id:
            return None
        claimed = list(bucket)
        _pending[key] = []
        return claimed


def stitch_texts(parts: list[str]) -> str:
    return "\n".join(part.strip() for part in parts if part.strip())


def merge_claimed_items(claimed: list[dict[str, str]]) -> dict[str, str]:
    if not claimed:
        return {}
    merged = dict(claimed[-1])
    merged["text"] = stitch_texts([row.get("text") or "" for row in claimed])
    return merged


def is_continue_utterance(text: str) -> bool:
    lowered = text.lower()
    return any(marker in text or marker in lowered for marker in _CONTINUE_MARKERS)


def detect_asked_toolkit(text: str) -> str:
    lowered = text.lower()
    for name, markers in _TOOLKIT_MARKERS:
        if any(marker in lowered or marker in text for marker in markers):
            return name
    return ""


def last_toolkit_from_history(
    history: tuple[ConversationTurn, ...] | list[ConversationTurn],
) -> str:
    for turn in reversed(history):
        found = detect_asked_toolkit(turn.text)
        if found:
            return found
    return ""


def prepare_owner_utterance(
    text: str,
    history: tuple[ConversationTurn, ...] | list[ConversationTurn] = (),
) -> str:
    """Prefix a continue/asked-toolkit hint so the model does not revive Instagram."""
    stripped = text.strip()
    asked = detect_asked_toolkit(stripped)
    last = last_toolkit_from_history(history)
    if is_continue_utterance(stripped) and last:
        return (
            f"Continue the last asked toolkit ({last}). "
            "Do not switch to Instagram or CRM unless he asked.\n\n"
            f"{stripped}"
        )
    if asked:
        return (
            f"Answer the asked toolkit first ({asked}). "
            "Do not start with Instagram or CRM unless he asked that.\n\n"
            f"{stripped}"
        )
    return stripped
