"""Import transactions: manifest revisions, request ids per epoch (§5.3, §15)."""

import pytest
from app.services.os_ledger import api

from tests.fakes.contexts import for_worker
from tests.fakes.os_ledger_builders import (
    TASK_OPEN,
    commit_direct,
    commit_import,
    counts,
    ctx,
    fresh_workspace,
    import_draft,
    window_s,
    wire,
)
from tests.fakes.os_ledger_env import OWNER_ID

IMPORT_WS = "ws-i"


def import_ctx(epoch=1):
    return ctx(workspace=IMPORT_WS, epoch=epoch)


def manifest_mutation(record_id, revision, before=None, **inputs):
    values = {"title": "Imported", "status": TASK_OPEN} | inputs
    after = wire("tasks", record_id, revision, **values)
    return api.Mutation("tasks", record_id, "upsert", before, after)


def revision_of(env, record_id):
    return next(r["revision"] for r in env.rows("os_tasks") if r["record_id"] == record_id)


@pytest.fixture
def shadow(ledger):
    fresh_workspace(ledger, IMPORT_WS)
    return ledger


def test_an_import_create_carries_the_manifest_revision(shadow):
    commit_import(shadow, import_ctx(), [manifest_mutation("TAS_m1", 5)], api.digest({"m": 1}))
    assert revision_of(shadow, "TAS_m1") == 5
    assert shadow.rows("os_outbox")[0]["body"]["full"] is True


def test_an_import_create_below_revision_one_is_refused(shadow):
    with pytest.raises(api.LedgerError, match="^target_changed$"):
        commit_import(shadow, import_ctx(), [manifest_mutation("TAS_m1", 0)],
                      api.digest({"m": 1}))
    assert shadow.rows("os_tasks") == [] and shadow.rows("os_receipts") == []


def test_an_import_update_needs_a_revision_greater_than_the_current_one(shadow):
    first = manifest_mutation("TAS_m1", 5)
    commit_import(shadow, import_ctx(), [first], api.digest({"m": 1}))
    for number, revision in enumerate((5, 4, 1), start=2):
        with pytest.raises(api.LedgerError, match="^target_changed$"):
            commit_import(shadow, import_ctx(), [
                manifest_mutation("TAS_m1", revision, first.after, title="Not newer")],
                api.digest({"m": number}))
    commit_import(shadow, import_ctx(), [
        manifest_mutation("TAS_m1", 9, first.after, title="Newer")], api.digest({"m": 9}))
    assert revision_of(shadow, "TAS_m1") == 9


def test_an_import_update_still_needs_the_current_row_to_match_its_before_image(shadow):
    first = manifest_mutation("TAS_m1", 5)
    commit_import(shadow, import_ctx(), [first], api.digest({"m": 1}))
    stale = {**first.after, "revision": 3}
    with pytest.raises(api.LedgerError, match="^target_changed$"):
        commit_import(shadow, import_ctx(), [manifest_mutation("TAS_m1", 9, stale)],
                      api.digest({"m": 2}))
    assert revision_of(shadow, "TAS_m1") == 5


def test_other_kinds_keep_the_plus_one_revision_rule(ledger):
    with pytest.raises(api.LedgerError, match="^target_changed$"):
        commit_direct(ledger, ctx(), [manifest_mutation("TAS_m1", 5)])
    assert ledger.rows("os_tasks") == []


def test_an_import_request_is_replayable_within_its_epoch(shadow):
    manifest = api.digest({"m": 1})
    proposal, receipt = commit_import(shadow, import_ctx(), [manifest_mutation("TAS_m1", 2)],
                                      manifest)
    request_id = api.import_request_uuid(1, manifest)
    assert proposal.request_id == receipt.request_id == request_id
    worker = for_worker("import", workspace=IMPORT_WS)
    found = shadow.creation_request(worker, request_id)
    assert found.found and found.proposal.proposal_id == proposal.proposal_id
    assert found.proposal.status == "committed" and found.decision == receipt
    assert shadow.receipt(worker, request_id).receipt == receipt
    with shadow.transaction(import_ctx(), domain="core", kind="import") as tx:
        assert tx.proposal_by_request(request_id).proposal_id == proposal.proposal_id
        assert tx.receipt(request_id) == receipt
    before = counts(shadow)
    with pytest.raises(api.LedgerError, match="^idempotency_mismatch$"):
        with shadow.transaction(import_ctx(), domain="core", kind="import") as tx:
            tx.insert_proposal(import_draft(tx, "other-batch", manifest, 1, window_s(shadow)))
    assert counts(shadow) == before


def test_the_same_manifest_at_a_new_epoch_is_a_new_request(shadow):
    manifest = api.digest({"m": 1})
    first_mutation = manifest_mutation("TAS_m1", 1)
    first_proposal, first = commit_import(shadow, import_ctx(), [first_mutation], manifest)
    for state in ("active", "fenced"):
        shadow.transition(IMPORT_WS, "core", state, expected_epoch=1, actor_owner_id=OWNER_ID)
    back = shadow.transition(IMPORT_WS, "core", "shadow", expected_epoch=1, actor_owner_id=OWNER_ID)
    assert back.epoch == 2
    shadow.knobs.settings_epoch = 2
    second_proposal, second = commit_import(
        shadow, import_ctx(2),
        [manifest_mutation("TAS_m1", 2, first_mutation.after, title="Re-imported")],
        manifest, epoch=2)
    assert first.request_id == api.import_request_uuid(1, manifest)
    assert second.request_id == api.import_request_uuid(2, manifest) != first.request_id
    assert (first_proposal.epoch, second_proposal.epoch) == (1, 2)
    assert (first.epoch, second.epoch) == (1, 2)
    assert revision_of(shadow, "TAS_m1") == 2
    worker = for_worker("import", workspace=IMPORT_WS, epoch=2)
    assert shadow.receipt(worker, second.request_id).found
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        shadow.receipt(worker, first.request_id)
    assert len([r for r in shadow.rows("os_receipts") if r["kind"] == "import"]) == 2
