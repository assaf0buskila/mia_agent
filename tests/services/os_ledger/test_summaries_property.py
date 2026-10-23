"""§8/§20 V2-18: incremental summaries equal a full recompute, on the fake and on PG.

The twin engine (``test_summaries_property_engine.py``) runs one random commit/undo/decide
sequence on a reference ledger (the full recompute) and on a ledger with ``IncrementalSummaries``,
and after every step compares every ``os_summary_monthly`` row (month, version, summary,
calculated_at), ``os_workspaces.summaries``, the ``monthly_summary`` read and the outcome, so an
apply the full recompute refuses (``amount_overflow``) is refused identically. Late steps add
amounts near the safe-integer limit, which drive the maintainer onto its exact full fallback and,
after a tombstone, back.
"""

from __future__ import annotations

import pytest
from app.services.os_ledger.summaries_incremental import IncrementalSummaries

from tests.fakes.os_ledger_builders import (
    client,
    expense,
    payment,
    time_entry,
    tombstone,
    update,
    work,
)
from tests.fakes.os_ledger_env import WORKSPACE
from tests.services.os_ledger.conftest import pg_enabled
from tests.services.os_ledger.test_summaries_property_engine import (
    MAX_SAFE,
    DualValidator,
    FakeMonthStore,
    compare_state,
    fake_twin,
    pg_twin,
    run,
)

FAKE_SEEDS = range(8)
PG_SEEDS = (11, 22, 33)


def needs_pg():
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")


def counting_pg_store():
    from app.services.os_ledger.summaries_incremental import PgMonthStore

    class Counting(PgMonthStore):
        calls = 0

        def full(self, applied):
            Counting.calls += 1
            return super().full(applied)

    return Counting


def test_fake_incremental_equals_full_over_random_sequences():
    fallbacks = refused = 0
    for seed in FAKE_SEEDS:
        twin = fake_twin(seed, summaries=IncrementalSummaries(store=FakeMonthStore))
        stats = run(twin, seed, 150)
        fallbacks += twin.sut.ledger._workspaces[WORKSPACE].get("full_fallbacks", 0)
        refused += sum(stats["refused"].values())
        assert stats["accepted"] > 20, stats
    assert fallbacks > 0 and refused > 0  # the fallback and refusals were both exercised


def test_pg_incremental_equals_full_over_random_sequences():
    needs_pg()
    store = counting_pg_store()
    for seed in PG_SEEDS:
        stats = run(pg_twin(seed, summaries=IncrementalSummaries(store=store)), seed, 70)
        assert stats["accepted"] > 10, stats
    assert store.calls > 0


def test_pg_the_v2_18_defaults_together_equal_the_full_ledger():
    """The configuration ``build_ledger()`` ships: scoped validator AND incremental summaries."""
    needs_pg()
    from app.services.os_ledger.validate_full import FullStateValidator
    from app.services.os_ledger.validate_scoped import ScopedStateValidator

    for seed in (44, 55):
        dual = DualValidator(ScopedStateValidator(), FullStateValidator())
        run(pg_twin(seed, validator=dual, summaries=IncrementalSummaries()), seed, 60)
        assert dual.calls and all(full == scoped for full, scoped in dual.calls)


# --- scripted: what a month row must look like after each change ---------------------------


def twins():
    yield "fake", lambda: fake_twin(5, summaries=IncrementalSummaries(store=FakeMonthStore))
    if pg_enabled():
        yield "pg", lambda: pg_twin(5, summaries=IncrementalSummaries())


def script(twin, steps):
    outcomes = []
    for number, mutations in enumerate(steps):
        both = twin.both(lambda side, m=mutations, n=number: side.commit(
            m, f"00000000-0000-4000-8000-{n:012d}"))
        assert both[0] == both[1], both
        outcomes.append(both[0][0] if both[0][0] == "ok" else both[0][2])
        compare_state(twin, f"step {number}")
    return outcomes


USD_WORK = work("WOR_w2", currency="USD")
SCENARIOS = {
    # a work's currency moves its time minutes between currency rows of every month it touches
    "work_currency_moves_time_rows": (
        [[client(), work(), time_entry(), time_entry("TIM_t2", date="2026-09-03")],
         [update(work().after, currency="USD")],
         [update({**work().after, "revision": 2, "currency": "USD"}, currency=None)]],
        ["ok", "ok", "ok"]),
    # a month that loses all its data keeps an empty row on PG and disappears on the fake
    "a_month_emptied_by_a_tombstone_and_a_move": (
        [[client(), work(), payment(), expense(date="2026-08-02")],
         [tombstone("payments", payment().after)],
         [update(expense(date="2026-08-02").after, date="2026-10-02")]],
        ["ok", "ok", "ok"]),
    "a_date_move_rewrites_both_months": (
        [[client(), work(), USD_WORK, payment(), payment("PAY_u", work_id="WOR_w2",
                                                         currency="USD")],
         [update(payment().after, date="2026-07-31")]],
        ["ok", "ok"]),
    # the full recompute refuses an overflow at apply; the incremental one must refuse it too
    "an_overflow_is_refused_exactly_like_the_full_recompute": (
        [[client(), work(), payment(actual_amount_ils=MAX_SAFE)],
         [payment("PAY_2", actual_amount_ils=1)],
         [tombstone("payments", payment(actual_amount_ils=MAX_SAFE).after)],
         [payment("PAY_3", actual_amount_ils=5)]],
        ["ok", "amount_overflow", "ok", "ok"]),
    "an_hourly_overflow_is_refused_exactly_like_the_full_recompute": (
        [[client(), work(), time_entry(minutes=1)],
         [payment(actual_amount_ils=MAX_SAFE // 30)]],
        ["ok", "weighted_hourly_overflow"]),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scripted_month_rows_agree_on_every_backend(name):
    steps, expected = SCENARIOS[name]
    for kind, build in twins():
        assert script(build(), steps) == expected, (kind, name)
