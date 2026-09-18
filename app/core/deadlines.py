"""Shared arithmetic for bounding one child call inside a parent turn deadline.

The owner turn has one hard wall-clock budget (`deadline_at`, a `monotonic()`
timestamp). Every expensive child call inside that turn -- a model request, a
tool dispatch -- must be bounded so that:

- it never starts at all once too little time is left to plausibly finish
  (`minimum`), and
- when it does start, it never gets more than what is actually left after
  reserving room for the final answer to reach the owner (`reserve`).

This module is the ONE place that arithmetic lives. Two call sites computing
it slightly differently was exactly how the production bug shipped (a model
call given the *entire* remaining parent budget starved every fallback rung).

Semantic trap, read this before touching a call site: `child_call_timeout`
returning `None` means "do not start this call" -- the caller must not even
attempt it. That is a completely different `None` from `timeout=None` passed
into `LlmClient.complete`, which means "no explicit bound, use the client's
own default timeout" -- i.e. GO. Never forward a `None` returned from
`child_call_timeout` straight into a `timeout=` keyword argument; it will be
silently reinterpreted as "unbounded" instead of "refused".
"""

from __future__ import annotations

from time import monotonic


def remaining_seconds(deadline_at: float | None) -> float | None:
    """Seconds left before the parent deadline. `None` only when unbounded."""
    if deadline_at is None:
        return None
    return deadline_at - monotonic()


def child_call_timeout(
    *,
    deadline_at: float | None,
    configured: float,
    reserve: float = 0.0,
    minimum: float,
) -> float | None:
    """The bound for ONE expensive child call, or `None` when it must not start at all.

    `configured` is the caller's own ceiling for this kind of call (e.g. one model
    attempt, one tool dispatch) and must be a positive number -- a caller passing
    zero or a negative ceiling has a bug, not an unbounded call, so this raises
    rather than silently allowing an unbounded or backwards timeout.

    `deadline_at is None` means the parent turn itself has no deadline (e.g. a
    direct unit-test call with no turn budget at all). Even then the child call
    is not unbounded: the parent being unbounded must still bound the child, so
    this returns `configured` unchanged.

    Otherwise the child may use whatever is left of the parent budget after
    `reserve` is set aside for whatever must run after this call finishes (the
    final send, later pipeline stages). If that leaves less than `minimum`
    seconds, the call is not worth starting at all and this returns `None`.
    """
    if configured <= 0:
        raise ValueError("child_call_timeout: configured must be > 0")
    if deadline_at is None:
        # Unbounded parent still bounds the child.
        return configured
    usable = deadline_at - monotonic() - reserve
    if usable < minimum:
        return None
    return min(configured, usable)
