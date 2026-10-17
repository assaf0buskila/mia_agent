"""§8/§20 V2-18: scoped accept/reject equals full accept/reject, on the fake and on PG.

Two proofs over random commit/undo/decide sequences (``test_summaries_property_engine.py``):
* per call: ``DualValidator`` runs ``FullStateValidator`` (the fake: its prototype default) and
  ``ScopedStateValidator`` on the SAME post-apply state; every call must give the same outcome,
  code and reason;
* end to end: the twin's reference ledger (full validator) and the ledger under test agree on every
  step's outcome and on every row.
The scripted cases pin the refusals only a post-apply check can see (a tombstone whose live,
cancelled or done referrers ``has_dependents`` would not count).
"""

from __future__ import annotations

import pytest
from app.services.os_ledger.validate_scoped import ScopedStateValidator

from tests.fakes.os_ledger_builders import (
    PAYMENT_CANCELLED,
    PAYMENT_REFUND,
    TASK_DONE,
    client,
    payment,
    task,
    time_entry,
    tombstone,
    update,
    work,
)
from tests.services.os_ledger.conftest import pg_enabled
from tests.services.os_ledger.test_summaries_property_engine import (
    DualValidator,
    FakeFullValidator,
    FakeScope,
    compare_state,
    fake_twin,
    pg_twin,
    run,
)

FAKE_SEEDS = range(12)
PG_SEEDS = (101, 202, 303)
POST_APPLY_REASONS = {"client_not_found", "work_not_found", "refund_original_not_found"}


def needs_pg():
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")


def check(dual: DualValidator, where: str) -> set[str]:
    assert dual.calls, f"{where}: the validator never ran"
    differ = [c for c in dual.calls if c[0] != c[1]]
    assert not differ, f"{where}: full != scoped on {differ[:5]}"
    return {c[0][1] for c in dual.calls if c[0] is not None}


def test_fake_scoped_equals_full_over_random_sequences():
    seen: set[str] = set()
    for seed in FAKE_SEEDS:
        dual = DualValidator(ScopedStateValidator(scope=FakeScope), FakeFullValidator())
        stats = run(fake_twin(seed, validator=dual), seed, 150)
        seen |= check(dual, f"fake seed {seed}")
        assert stats["accepted"] > 20 and stats["refused"], stats
    assert POST_APPLY_REASONS <= seen, seen  # not vacuous: every post-apply refusal was hit


def test_pg_scoped_equals_full_over_random_sequences():
    needs_pg()
    from app.services.os_ledger.validate_full import FullStateValidator

    seen: set[str] = set()
    for seed in PG_SEEDS:
        dual = DualValidator(ScopedStateValidator(), FullStateValidator())
        stats = run(pg_twin(seed, validator=dual), seed, 70)
        seen |= check(dual, f"pg seed {seed}")
        assert stats["accepted"] > 10, stats
    assert seen & POST_APPLY_REASONS, seen


# --- scripted: the refusals that only the post-apply state shows --------------------------


def twins():
    yield "fake", lambda: fake_twin(7, validator=DualValidator(
        ScopedStateValidator(scope=FakeScope), FakeFullValidator()))
    if pg_enabled():
        from app.services.os_ledger.validate_full import FullStateValidator

        yield "pg", lambda: pg_twin(7, validator=DualValidator(
            ScopedStateValidator(), FullStateValidator()))


def script(twin, steps):
    outcomes = []
    for number, mutations in enumerate(steps):
        both = twin.both(lambda side, m=mutations, n=number: side.commit(
            m, f"00000000-0000-4000-8000-{n:012d}"))
        assert both[0] == both[1], both
        outcomes.append(both[0][0] if both[0][0] == "ok" else both[0][2])
        compare_state(twin, f"step {number}")
    return outcomes


SCENARIOS = {
    # a cancelled payment is not live, so has_dependents ignores it; the state check does not
    "client_with_a_cancelled_payment": (
        [[client(), work(), payment(kind=PAYMENT_CANCELLED)],
         [tombstone("clients", client().after)]],
        ["ok", "client_not_found"]),
    "work_with_a_done_task": (
        [[client(), work(), task(work_id="WOR_w1", person_id="PER_c1", status=TASK_DONE)],
         [tombstone("works", work().after)]],
        ["ok", "work_not_found"]),
    "original_with_a_refund": (
        [[client(), work(), payment()],
         [payment("PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                  actual_amount_ils=400)],
         [tombstone("payments", payment().after)]],
        ["ok", "ok", "refund_original_not_found"]),
    "a_tombstoned_referrer_does_not_count": (
        [[client(), work(), time_entry()],
         [tombstone("time", time_entry().after)],
         [tombstone("works", work().after)],
         [tombstone("clients", client().after)]],
        ["ok", "ok", "ok", "ok"]),
    "a_bulk_takes_the_referrers_with_it": (
        [[client(), work(), time_entry(), payment()],
         [tombstone("clients", client().after), tombstone("works", work().after),
          tombstone("time", time_entry().after), tombstone("payments", payment().after)]],
        ["ok", "ok"]),
    "a_work_person_change_breaks_its_time_entry": (
        [[client(), client("PER_c2"), work(), time_entry()],
         [update(work().after, person_id="PER_c2")]],
        ["ok", "work_person_mismatch"]),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scripted_post_apply_refusals_agree_on_every_backend(name):
    steps, expected = SCENARIOS[name]
    for kind, build in twins():
        twin = build()
        assert script(twin, steps) == expected, (kind, name)
