"""A budget exhaustion must read as a timeout, not as an unclassified error.

`run_owner_agent` exits cooperatively with `completion="deadline_exceeded"` when the
owner turn's budget is spent -- and, once every expensive child call is bounded, that
cooperative exit is the *normal* way a genuinely slow turn ends, rather than waiting for
a provider read timeout to surface. `classify_note_agent_failure` had no needle for it,
so the owner was told "(שגיאה)" -- a generic error -- for the one failure class he can
actually act on by retrying or asking something smaller.

The provider-read-timeout wording ("timeout") was already mapped, which is why this went
unnoticed: the two paths produce the same user-visible outcome but only one was labelled.
"""

from __future__ import annotations

import pytest
from app.domain.owner.brain import classify_note_agent_failure, format_note_agent_failure
from app.surfaces.turn_coalesce import HANG_REPLY

TIMEOUT_LABEL = "תם הזמן"


@pytest.mark.parametrize(
    ("reason", "completion"),
    [
        # The cooperative exits `run_owner_agent` produces once the budget is spent.
        ("deadline exceeded", "deadline_exceeded"),
        ("deadline exceeded", ""),
        ("", "deadline_exceeded"),
        # The pre-existing provider-surfaced shapes, kept as a control.
        ("llm request failed: read timeout [m:timeout]", "provider_error"),
        ("llm request failed: deadline exceeded [m:timeout]", "provider_error"),
    ],
    ids=[
        "cooperative-both",
        "cooperative-reason-only",
        "cooperative-completion-only",
        "provider-read-timeout",
        "provider-deadline",
    ],
)
def test_every_budget_exhaustion_reads_as_a_timeout(reason: str, completion: str) -> None:
    assert classify_note_agent_failure(reason, completion) == TIMEOUT_LABEL


def test_cooperative_deadline_reply_matches_the_worker_timeout_notice() -> None:
    """One symptom, one sentence, whichever layer noticed the budget was gone.

    The worker's `HANG_REPLY` and the brain's timeout line are deliberately the same
    text: Assaf should not have to care which layer ran out. Telling them apart is the
    job of the `timeout_stage` reason code in the logs, never of the owner's message.
    """
    assert format_note_agent_failure("deadline exceeded", "deadline_exceeded") == HANG_REPLY


def test_a_non_timeout_failure_is_not_relabelled_as_a_timeout() -> None:
    """The new needles must not swallow the classes that are genuinely not timeouts."""
    assert classify_note_agent_failure("step budget spent", "budget_exhausted") != TIMEOUT_LABEL
    assert classify_note_agent_failure("refused", "refused") != TIMEOUT_LABEL
    assert classify_note_agent_failure("truncated", "truncated") != TIMEOUT_LABEL
    assert classify_note_agent_failure("", "") != TIMEOUT_LABEL
