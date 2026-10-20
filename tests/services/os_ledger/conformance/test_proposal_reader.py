"""§22 E14(1): the read-only ``LedgerReader.proposal(ctx, proposal_id)`` (V2-18).

It follows §8 isolation (REPEATABLE READ READ ONLY on PG, never a write), so it answers under the
kill switch and with core fenced, and it is subject to the I12 epoch filter like ``pending()``:
a proposal of another epoch reads as ``None``. It returns the STORED row whatever its status or
kind (§22 E16); the route, not the reader, turns a non-``proposal`` kind into a 404.
"""

from dataclasses import replace

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import abandon_draft, counts, ctx, propose_task, rid, window_s
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE


def test_proposal_reads_one_stored_proposal_read_only_at_the_context_epoch(ledger):
    # ``LedgerEnv.LEDGER_SURFACE`` (tests/fakes/os_ledger_env.py, outside V2-18's files) predates
    # E14(1), so the case reaches the implementation directly.
    reader = ledger._impl
    pending = propose_task(ledger)
    decided = propose_task(ledger)
    ledger.decide(ctx(), decided.proposal_id, decided.rejection_id, "reject")
    with ledger.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        direct = tx.insert_proposal(abandon_draft(tx, rid(), window_s(ledger)))
    before = (counts(ledger), api.since_token(ledger.head(ctx())))

    assert reader.proposal(ctx(), pending.proposal_id) == pending
    assert reader.proposal(ctx(), decided.proposal_id) == replace(decided, status="rejected")
    assert reader.proposal(ctx(), direct.proposal_id) == direct
    assert reader.proposal(ctx(), "PRO_" + "0" * 32) is None
    assert reader.proposal(ctx(), 12) is None
    ledger.knobs.kill_switch = True  # a read, so the kill switch does not refuse it
    assert reader.proposal(ctx(), pending.proposal_id) == pending
    ledger.knobs.kill_switch = False
    assert (counts(ledger), api.since_token(ledger.head(ctx()))) == before

    ledger.bump_epoch(WORKSPACE, expected_epoch=1)
    ledger.knobs.settings_epoch = 2
    assert reader.proposal(ctx(epoch=2), pending.proposal_id) is None  # I12, like pending()
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        reader.proposal(ctx(), pending.proposal_id)
    fresh = propose_task(ledger, ctx(epoch=2))
    ledger.transition(WORKSPACE, "core", "fenced", expected_epoch=2, actor_owner_id=OWNER_ID)
    assert reader.proposal(ctx(epoch=2), fresh.proposal_id) == fresh  # a fenced core still reads
