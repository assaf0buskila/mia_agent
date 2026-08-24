"""AI run "engine truth" line: replies that actually ran, not a model-string check.

`/health` reporting `owner_agent: ready` only proved that a model string was
non-empty. The owner agent silently ran the pre-brain keyword classifier in
production for a full day and only Assaf's gut caught it (ADR-029). "Ready" only
means something when it comes from real run data: every sales/owner turn persists
an `AiRunRow` (`app/domain/ai_runs.py::persist_ai_run`), and `MODEL_CANNED` marks a
turn that never reached a real model. This module aggregates that log so a canned
spike is visible on the owner brief instead of silent in a health check.

LIMITATION (read before trusting the numbers): `AiRunRow` carries no timestamp
column (no `created_at` / `occurred_at`). `LeadStore.aggregate_ai_runs` therefore
cannot filter to a calendar day today; `occurred_from` / `occurred_to` are accepted
for interface parity with the rest of the owner-brief reads (`count_canonical_events`,
`count_behavior_events`, ...) but are NOT applied as a real filter. Every call
aggregates the full `ai_runs` table, since the beginning of the deployment, not
"today". A real day window needs a migration adding a timestamp column to
`ai_runs`, which is out of scope for this slice — see ADR-029's Consequences.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from app.domain.followups import local_day_bounds_utc_iso

if TYPE_CHECKING:
    from app.db.store import LeadStore


class AiRunAggregate(BaseModel):
    """Frozen all-time aggregate over persisted `AiRunRow`s.

    See the module docstring: this is NOT scoped to a calendar day.
    """

    model_config = ConfigDict(frozen=True)

    total_runs: int = Field(ge=0)
    canned_runs: int = Field(ge=0, description="Runs labelled MODEL_CANNED: no real model ran.")
    median_latency_ms: int = Field(ge=0)
    p95_latency_ms: int = Field(ge=0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_usd: int = Field(ge=0)


def compute_engine_health(
    store: LeadStore, *, timezone: str, now: datetime | None = None
) -> AiRunAggregate | None:
    """Compute the engine-truth aggregate. None only on an invalid `timezone`.

    `timezone` / `now` exist to match the calling convention of the other owner-brief
    reads (`compute_daily_brief`, `compute_website_funnel`), but per the module
    docstring the underlying store read cannot honor a day window yet: the result is
    an all-time total regardless of `now`.
    """
    instant = now if now is not None else datetime.now(UTC)
    bounds = local_day_bounds_utc_iso(now=instant, timezone=timezone)
    if bounds is None:
        return None
    occurred_from, occurred_to = bounds
    return store.aggregate_ai_runs(occurred_from=occurred_from, occurred_to=occurred_to)


def format_engine_health(aggregate: AiRunAggregate) -> str:
    """Hebrew, owner-facing, one line. No PII, no lead ids, no hyphen/dash.

    A high `canned_runs` count relative to `total_runs` is the signal that the real
    model is failing silently and Mia is running on the deterministic canned
    fallback only — that is the whole point of this line existing.
    """
    return (
        f"מנוע (מאז ההתחלה): {aggregate.total_runs} תגובות רצו · "
        f"חציון {aggregate.median_latency_ms} מילישניות · "
        f"{aggregate.canned_runs} ללא מודל אמיתי"
    )
