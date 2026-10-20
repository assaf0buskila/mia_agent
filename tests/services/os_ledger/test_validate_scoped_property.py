"""§8/§20 V2-18: scoped accept/reject equals full accept/reject, on the fake and on PG.

Two proofs over random commit/undo/decide sequences (``test_summaries_property_engine.py``):
* per call: ``DualValidator`` runs ``FullStateValidator`` (the fake: its prototype default) and
  ``ScopedStateValidator`` on the SAME post-apply state; every call must give the same outcome,
  code and reason;
* end to end: the twin's reference ledger (full validator) and the ledger under test agree on every
  step's outcome and on every row.
The scripted cases pin the refusals only a post-apply check can see (a tombstone whose referrers
are not live: cancelled or done), and, since §22 E20, the has_dependents refusals apply makes
itself and apply's own refund-graph refusals.
"""

from __future__ import annotations

import pytest
from app.services.os_ledger.validate_scoped import ScopedStateValidator

from tests.fakes.os_ledger_builders import (
    PAYMENT_CANCELLED,
    PAYMENT_REFUND,
    TASK_DONE,
    WORK_CANCELLED,
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

FAKE_SEEDS = range(8)
# Seeds whose 70-step sequences reach the post-apply refusals (both reasons between them), so the
# PG run is not vacuous after §22 E20 moved the live-referrer refusals into apply.
PG_SEEDS = (100, 113, 140)
# §22 E20 (V2-16 final) refuses a tombstone under a LIVE referrer in apply (has_dependents), and
# V2-16's apply already validates each mutation in full, so the post-apply validators see only a
# tombstone whose referrers are not live. A refund is live unless cancelled, and a cancelled
# refund carries no original, so the post-apply refund_original_not_found is now unreachable. The
# refund graph is still covered by the scripted re-point scenarios below, where apply refuses it.
POST_APPLY_REASONS = {"client_not_found", "work_not_found"}


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
    assert POST_APPLY_REASONS <= seen, seen  # the seeds reach both post-apply refusals


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
        # A refusal is reported by its reason, or by its code when it has none (E20's
        # has_dependents comes from apply itself, before any validator).
        outcomes.append(both[0][0] if both[0][0] == "ok" else (both[0][2] or both[0][1]))
        compare_state(twin, f"step {number}")
    return outcomes


SCENARIOS = {
    # §22 E20 (V2-16 final): the live work blocks the client's tombstone in apply itself
    # (has_dependents), before any validator; the cancelled payment is not live and does not.
    "client_with_a_cancelled_payment": (
        [[client(), work(), payment(kind=PAYMENT_CANCELLED)],
         [tombstone("clients", client().after)]],
        ["ok", "has_dependents"]),
    # A cancelled work is not live, so E20 lets the client's tombstone through to the post-apply
    # check, which refuses it (review: pins a post-apply client_not_found deterministically).
    "client_with_a_cancelled_work": (
        [[client(), work(status=WORK_CANCELLED)],
         [tombstone("clients", client().after)]],
        ["ok", "client_not_found"]),
    "work_with_a_done_task": (
        [[client(), work(), task(work_id="WOR_w1", person_id="PER_c1", status=TASK_DONE)],
         [tombstone("works", work().after)]],
        ["ok", "work_not_found"]),
    # §22 E20: a live refund now blocks tombstoning its original in apply (has_dependents) ...
    "original_with_a_refund": (
        [[client(), work(), payment()],
         [payment("PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                  actual_amount_ils=400)],
         [tombstone("payments", payment().after)]],
        ["ok", "ok", "has_dependents"]),
    # ... so the refund graph is exercised by re-pointing the refund, which apply's own
    # per-mutation validation refuses (the post-apply refund-graph check is a backstop E20 makes
    # unreachable).
    "a_refund_repointed_to_a_missing_original": (
        [[client(), work(), payment()],
         [payment("PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                  actual_amount_ils=400)],
         [update(payment("PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                         actual_amount_ils=400).after, original_payment_id="PAY_none")]],
        ["ok", "ok", "refund_original_not_found"]),
    "a_refund_repointed_to_a_cancelled_payment": (
        [[client(), work(), payment(), payment("PAY_p2")],
         [payment("PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                  actual_amount_ils=400)],
         [update(payment("PAY_p2").after, kind=PAYMENT_CANCELLED)],
         [update(payment("PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                         actual_amount_ils=400).after, original_payment_id="PAY_p2")]],
        ["ok", "ok", "ok", "refund_original_not_found"]),
    "a_tombstoned_referrer_does_not_count": (
        [[client(), work(), time_entry()],
         [tombstone("time", time_entry().after)],
         [tombstone("works", work().after)],
         [tombstone("clients", client().after)]],
        ["ok", "ok", "ok", "ok"]),
    "a_bulk_takes_the_referrers_with_it": (
        [[client(), work(), time_entry(), payment()],
         [tombstone("time", time_entry().after), tombstone("payments", payment().after),
          tombstone("works", work().after), tombstone("clients", client().after)]],
        ["ok", "ok"]),
    # apply validates each mutation against the state so far, so a referrer must go first
    "a_bulk_in_the_wrong_order_is_refused": (
        [[client(), work(), time_entry()],
         [tombstone("clients", client().after), tombstone("works", work().after),
          tombstone("time", time_entry().after)]],
        ["ok", "client_not_found"]),
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
