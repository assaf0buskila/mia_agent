"""Promotion of a staged batch through the frozen ledger API (contract section 15, E1-rev, E20).

Runs on the FakeLedger and, when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set, on the V2-16 PostgreSQL
ledger (the ``pg`` case of the ``ledger`` fixture). The workbooks are synthetic; the prototype
stays the normalization oracle."""

from datetime import timedelta

import pytest
from app.services.os_ledger import api
from app.services.os_ledger import promote as promote_module
from app.services.os_ledger.import_workbook import manifest_digest, prepare, stage
from app.services.os_ledger.promote import promote

from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger_builders import (
    EXPENSE_ACTUAL,
    EXPENSE_PAID,
    LEAD_NEW,
    PATH_LEARN,
    TASK_OPEN,
    TIME_ACTIVE,
    fresh_workspace,
)
from tests.fakes.os_ledger_env import OWNER_ID
from tests.fixtures.workbooks.staging import (
    IMPORT_WS,
    E20Ledger,
    next_epoch,
    record_rows,
    snapshot,
    workbook,
    worker,
)
from tests.fixtures.workbooks.synthetic import (
    synthetic_records,
    tables_from_records,
    task_record,
)


@pytest.fixture
def shadow(ledger):
    fresh_workspace(ledger, IMPORT_WS)
    return ledger


def staged(env, tmp_path, records=None, *, epoch=1, revisions=None, name="synthetic.xlsx"):
    tables = tables_from_records(records or synthetic_records(), revisions)
    return stage(env, worker(epoch), workbook(tmp_path, tables, name))


def run(env, result, *, epoch=1, cancellations=(), **kwargs):
    signed = manifest_digest(result.batch, cancellations=cancellations)
    return promote(env, worker(epoch), result.batch, signed, cancellations=cancellations, **kwargs)


def batch_row(env, result):
    return next(b for b in env.rows("os_import_batches") if b["batch_id"] == result.batch.batch_id)


def refused(code, call):
    with pytest.raises(api.LedgerError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def with_tasks(*tasks):
    records = synthetic_records()
    records["tasks"] = list(tasks)
    return records


def chain_records():
    """Two clients; refund chains whose ids sort against the dependency order; a linked task."""
    records = synthetic_records()
    records["clients"].append({"person_id": "PER_TWO", "revision": 1, "name": "Second",
                               "status": "פעיל"})
    records["works"].append({
        "work_id": "WOR_TWO", "person_id": "PER_TWO", "revision": 1, "title": "Second work",
        "path": "לימוד", "status": "פעילה", "currency": "ILS", "agreed_amount_ils": 300})
    paid = {"person_id": "PER_TWO", "work_id": "WOR_TWO", "revision": 1, "path": "לימוד",
            "date": "2026-10-01", "currency": "ILS", "kind": "התקבל"}
    records["payments"] += [
        {**paid, "payment_id": "PAY_ABC", "actual_amount_ils": 100},
        {**paid, "payment_id": "PAY_ZED", "kind": "החזר", "original_payment_id": "PAY_ABC",
         "actual_amount_ils": 10},
        {**paid, "payment_id": "PAY_YYY", "actual_amount_ils": 50},
        {**paid, "payment_id": "PAY_BBB", "kind": "החזר", "original_payment_id": "PAY_YYY",
         "actual_amount_ils": 5},
    ]
    records["tasks"] = [task_record("TAS_TWO", person_id="PER_TWO", work_id="WOR_TWO")]
    return records


CHAIN_IDS = [("tasks", "TAS_TWO"), ("payments", "PAY_ABC"), ("payments", "PAY_ZED"),
             ("payments", "PAY_YYY"), ("payments", "PAY_BBB"), ("works", "WOR_TWO"),
             ("clients", "PER_TWO")]
CHAIN_CANCELLATIONS = [
    {"entity": entity, "record_id": record_id} for entity, record_id in CHAIN_IDS]


def test_promote_writes_every_record_at_its_manifest_revision(shadow, tmp_path):
    result = staged(shadow, tmp_path, revisions={"PER_ONE": 5, "PAY_USD": 3})
    before = snapshot(shadow)
    out = run(shadow, result)
    assert (out.created, out.updated, out.unchanged, out.cancelled, out.replayed) == (
        6, 0, 0, 0, False)
    assert record_rows(shadow, "os_clients")["PER_ONE"]["revision"] == 5
    payments = {k: v["revision"] for k, v in record_rows(shadow, "os_payments").items()}
    assert payments == {"PAY_ILS": 1, "PAY_USD": 3, "PAY_REFUND": 1}
    assert {r["revision"] for r in shadow.rows("os_works")} == {1}
    assert batch_row(shadow, result)["status"] == "imported"
    assert batch_row(shadow, result)["completed_at"] is not None
    digest = result.manifest_digest
    request_id = api.import_request_uuid(1, digest)
    (receipt,) = shadow.rows("os_receipts")
    assert (receipt["request_id"], receipt["kind"], receipt["status"], receipt["epoch"]) == (
        request_id, "import", "committed", 1)
    assert tuple(receipt["records"]) == ()  # kind import: the manifest is the record of truth
    (proposal,) = shadow.rows("os_proposals")
    assert (proposal["kind"], proposal["source"], proposal["entity"]) == (
        "import", "IMPORT", "import")
    assert (proposal["record_id"], proposal["expected_revision"]) == (result.batch.batch_id, 0)
    assert proposal["dependency_digest"] == digest and tuple(proposal["mutations"]) == ()
    assert proposal["request_digest"] == api.digest({"import": digest, "epoch": 1})
    assert proposal["expires_at"] - proposal["created_at"] == timedelta(
        seconds=shadow.knobs.undo_window_s)
    (outbox,) = shadow.rows("os_outbox")
    assert outbox["body"] == {"authority_epoch": 1, "refs": [], "full": True}
    assert shadow.version(IMPORT_WS) == before["version"] + 1
    assert len(shadow.rows("os_events")) == 1


def test_promote_imports_every_entity_type(shadow, tmp_path):
    records = chain_records()
    records["leads"] = [{
        "lead_id": "LEA_ONE", "person_id": "PER_LEAD", "revision": 1, "name": "Lead",
        "phone": "0501234567", "path": PATH_LEARN, "status": LEAD_NEW}]
    records["time"] = [{
        "time_id": "TIM_ONE", "person_id": "PER_ONE", "work_id": "WOR_ILS", "revision": 1,
        "path": PATH_LEARN, "date": "2026-10-01", "minutes": 30, "status": TIME_ACTIVE}]
    records["expenses"] = [{
        "expense_id": "EXP_ONE", "revision": 1, "date": "2026-10-01", "category": "Ads",
        "platform": "Meta", "path": PATH_LEARN, "amount_ils": 500, "currency": "ILS",
        "payment_status": EXPENSE_PAID, "source_type": EXPENSE_ACTUAL, "source_key": "key-1"}]
    records["tasks"].append(task_record("TAS_ONE", status=TASK_OPEN))
    result = staged(shadow, tmp_path, records)
    assert result.status == "validated", result.reasons
    out = run(shadow, result)
    assert out.created == sum(len(rows) for rows in records.values()) and out.unchanged == 0
    for table, ids in {
        "os_leads": {"LEA_ONE"}, "os_clients": {"PER_ONE", "PER_TWO"},
        "os_works": {"WOR_ILS", "WOR_USD", "WOR_TWO"}, "os_time": {"TIM_ONE"},
        "os_expenses": {"EXP_ONE"}, "os_tasks": {"TAS_ONE", "TAS_TWO"},
    }.items():
        assert set(record_rows(shadow, table)) == ids
    assert len(shadow.rows("os_payments")) == 7


def test_promote_is_idempotent_within_an_epoch(shadow, tmp_path):
    result = staged(shadow, tmp_path)
    first = run(shadow, result)
    after_first = snapshot(shadow)
    again = run(shadow, result)
    assert again.replayed and again.receipt == first.receipt
    assert again.request_id == first.request_id
    assert (again.created, again.updated, again.unchanged, again.cancelled) == (0, 0, 0, 0)
    assert snapshot(shadow) == after_first
    assert len(shadow.rows("os_receipts")) == 1


def test_the_same_workbook_promotes_again_at_the_next_epoch(shadow, tmp_path):
    book = tables_from_records(synthetic_records(), {"PER_ONE": 5})
    first_stage = stage(shadow, worker(), workbook(tmp_path, book))
    first = run(shadow, first_stage)
    next_epoch(shadow, epoch=1)
    second_stage = stage(shadow, worker(2), workbook(tmp_path, book))
    before = snapshot(shadow)
    second = run(shadow, second_stage, epoch=2)
    assert second.request_id == api.import_request_uuid(2, second_stage.manifest_digest)
    assert second.request_id != first.request_id and not second.replayed
    assert (second.created, second.updated, second.unchanged, second.cancelled) == (0, 0, 6, 0)
    assert second.receipt.epoch == 2 and first.receipt.epoch == 1
    assert record_rows(shadow, "os_clients")["PER_ONE"]["revision"] == 5
    assert snapshot(shadow)["os_receipts"] == before["os_receipts"] + 1
    assert snapshot(shadow)["version"] == before["version"] + 1
    assert [o["body"]["full"] for o in shadow.rows("os_outbox")] == [True, True]
    # A replay at epoch 2 is a replay of the epoch-2 request only.
    assert run(shadow, second_stage, epoch=2).replayed


def test_a_newer_manifest_revision_is_written_as_is_and_the_rest_stays_untouched(
    shadow, tmp_path
):
    first = staged(shadow, tmp_path, revisions={"PER_ONE": 5})
    run(shadow, first)
    next_epoch(shadow, epoch=1)
    changed = synthetic_records()
    changed["clients"][0]["name"] = "Renamed in Sheets"
    second = staged(shadow, tmp_path, changed, epoch=2, revisions={"PER_ONE": 9},
                    name="second.xlsx")
    out = run(shadow, second, epoch=2)
    assert (out.created, out.updated, out.unchanged) == (0, 1, 5)
    # The Sheets revision is written as it is (a jump from 5 to 9), never current + 1.
    client = record_rows(shadow, "os_clients")["PER_ONE"]
    assert client["revision"] == 9 and client["inputs"]["name"] == "Renamed in Sheets"
    assert {r["revision"] for r in shadow.rows("os_works")} == {1}


@pytest.mark.parametrize("revision", [5, 4, 1])
def test_a_manifest_revision_that_is_not_newer_is_refused(shadow, tmp_path, revision):
    run(shadow, staged(shadow, tmp_path, revisions={"PER_ONE": 5}))
    next_epoch(shadow, epoch=1)
    changed = synthetic_records()
    changed["clients"][0]["name"] = "Edited at an old revision"
    second = staged(shadow, tmp_path, changed, epoch=2, revisions={"PER_ONE": revision},
                    name="second.xlsx")
    before = snapshot(shadow)
    error = refused("target_changed", lambda: run(shadow, second, epoch=2))
    assert error.details == {"reason": "import_revision_not_newer"}
    assert snapshot(shadow) == before
    assert batch_row(shadow, second)["status"] == "validated"
    assert record_rows(shadow, "os_clients")["PER_ONE"]["revision"] == 5


def test_quarantine_blocks_promotion(shadow, tmp_path):
    records = synthetic_records()
    tables = tables_from_records(records)
    tables["clients"][1][1] = "Unaccepted edit"  # the name column, after acceptance
    result = stage(shadow, worker(), workbook(tmp_path, tables))
    assert result.status == "quarantined"
    before = snapshot(shadow)
    error = refused("import_quarantine_unresolved",
                    lambda: promote(shadow, worker(), result.batch, result.manifest_digest))
    assert error.details == {"count": 2} and error.status == 409
    assert snapshot(shadow) == before
    assert batch_row(shadow, result)["status"] == "quarantined"


def test_a_wrong_or_stale_manifest_digest_is_refused(shadow, tmp_path):
    result = staged(shadow, tmp_path)
    before = snapshot(shadow)
    for wrong in ("0" * 64, result.manifest_digest.upper(), "", None):
        refused("manifest_digest_mismatch",
                lambda wrong=wrong: promote(shadow, worker(), result.batch, wrong))
    # The digest of a manifest with cancellations is not the digest of one without.
    other = [{"entity": "tasks", "record_id": "TAS_X"}]
    signed = manifest_digest(result.batch, cancellations=other)
    refused("manifest_digest_mismatch", lambda: promote(shadow, worker(), result.batch, signed))
    assert snapshot(shadow) == before and batch_row(shadow, result)["status"] == "validated"


def test_promote_needs_core_to_be_shadow(shadow, tmp_path):
    result = staged(shadow, tmp_path)
    shadow.transition(IMPORT_WS, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    before = snapshot(shadow)
    error = refused("authority_not_active", lambda: run(shadow, result))
    assert error.status == 503
    assert snapshot(shadow) == before


def test_promote_refuses_a_batch_staged_for_another_epoch_or_workspace(shadow, tmp_path):
    result = staged(shadow, tmp_path)
    before = snapshot(shadow)
    refused("authority_changed", lambda: promote(
        shadow, worker(2), result.batch, result.manifest_digest))
    refused("authority_changed", lambda: promote(
        shadow, worker(workspace="ws-other"), result.batch, result.manifest_digest))
    assert snapshot(shadow) == before


def test_promote_is_a_worker_job_only(shadow, tmp_path):
    result = staged(shadow, tmp_path)
    for channel in ("OS", "MIA", "TELEGRAM_APPROVAL"):
        refused("channel_operation_denied", lambda channel=channel: promote(
            shadow, make_ctx(channel, workspace=IMPORT_WS), result.batch, result.manifest_digest))
    assert shadow.rows("os_receipts") == []


def test_promote_follows_the_batch_status_machine_in_the_ledger(shadow, tmp_path):
    result = staged(shadow, tmp_path)
    with shadow.transaction(worker(), domain="core", kind="import") as tx:
        tx.set_import_status(result.batch.batch_id, "failed")  # the owner abandoned the batch
    before = snapshot(shadow)
    refused("import_status_invalid", lambda: run(shadow, result))
    assert snapshot(shadow) == before and shadow.rows("os_clients") == []
    assert batch_row(shadow, result)["status"] == "failed"


def test_a_batch_that_was_never_staged_is_refused_and_leaves_no_trace(shadow, tmp_path):
    batch = prepare(workbook(tmp_path, tables_from_records(synthetic_records())),
                    workspace_id=IMPORT_WS, authority_epoch=1)
    before = snapshot(shadow)
    # PostgreSQL raises the batch foreign key as ledger_integrity_failed; the fake models the same
    # constraint as its own invariant assertion. Either way the whole transaction rolls back.
    with pytest.raises((api.LedgerError, AssertionError)) as caught:
        promote(shadow, worker(), batch, manifest_digest(batch))
    if isinstance(caught.value, api.LedgerError):
        assert (caught.value.code, caught.value.reason) == (
            "ledger_integrity_failed", "import_status_batch_fk")
    else:
        assert "import_status_batch_fk" in str(caught.value)
    assert snapshot(shadow) == before and shadow.rows("os_clients") == []


def test_an_orphan_blocks_promotion_unless_the_signed_manifest_cancels_it(shadow, tmp_path):
    both = with_tasks(task_record("TAS_ONE"), task_record("TAS_OLD"))
    run(shadow, staged(shadow, tmp_path, both))
    next_epoch(shadow, epoch=1)
    second = staged(shadow, tmp_path, with_tasks(task_record("TAS_ONE")), epoch=2,
                    name="second.xlsx")
    before = snapshot(shadow)
    error = refused("import_orphan_rows", lambda: run(shadow, second, epoch=2))
    assert error.details == {"counts": {"tasks": 1}} and error.status == 409
    assert snapshot(shadow) == before
    assert batch_row(shadow, second)["status"] == "validated"
    cancel = [{"entity": "tasks", "record_id": "TAS_OLD"}]
    # Approving the digest of the manifest without the cancellation does not sign it.
    refused("manifest_digest_mismatch", lambda: promote(
        shadow, worker(2), second.batch, second.manifest_digest, cancellations=cancel))
    out = run(shadow, second, epoch=2, cancellations=cancel)
    assert (out.created, out.updated, out.unchanged, out.cancelled) == (0, 0, 6 + 1, 1)
    tasks = record_rows(shadow, "os_tasks")
    assert tasks["TAS_OLD"]["deleted_at"] is not None and tasks["TAS_ONE"]["deleted_at"] is None
    assert tasks["TAS_OLD"]["revision"] == 2
    assert batch_row(shadow, second)["status"] == "imported"
    assert out.receipt.records == ()


def test_each_orphan_is_counted_per_entity(shadow, tmp_path):
    run(shadow, staged(shadow, tmp_path, chain_records()))
    next_epoch(shadow, epoch=1)
    second = staged(shadow, tmp_path, epoch=2, name="second.xlsx")
    error = refused("import_orphan_rows", lambda: run(shadow, second, epoch=2))
    assert error.details == {"counts": {"clients": 1, "works": 1, "payments": 4, "tasks": 1}}
    partial = CHAIN_CANCELLATIONS[:-1]  # the client is still unsigned
    error = refused("import_orphan_rows", lambda: run(
        shadow, second, epoch=2, cancellations=partial))
    assert error.details == {"counts": {"clients": 1}}


def test_a_signed_cancellation_that_is_not_an_orphan_is_a_stale_manifest(shadow, tmp_path):
    run(shadow, staged(shadow, tmp_path, with_tasks(task_record("TAS_ONE"))))
    next_epoch(shadow, epoch=1)
    second = staged(shadow, tmp_path, with_tasks(task_record("TAS_ONE")), epoch=2,
                    name="second.xlsx")
    before = snapshot(shadow)
    error = refused("validation_error", lambda: run(
        shadow, second, epoch=2, cancellations=[{"entity": "tasks", "record_id": "TAS_NOPE"}]))
    assert error.details == {"reason": "cancellation_not_live"}
    assert snapshot(shadow) == before


def test_cancellations_apply_dependents_first_under_erratum_e20(shadow, tmp_path):
    run(shadow, staged(shadow, tmp_path, chain_records()))
    next_epoch(shadow, epoch=1)
    strict = E20Ledger(shadow)
    second = stage(strict, worker(2), workbook(
        tmp_path, tables_from_records(synthetic_records()), "second.xlsx"))
    out = run(strict, second, epoch=2, cancellations=CHAIN_CANCELLATIONS)
    assert out.cancelled == 7 and out.unchanged == 6
    order = strict.order
    position = {ref: order.index(ref) for ref in CHAIN_IDS}
    # Every record is cancelled before the record it depends on: task and refunds first.
    assert position[("tasks", "TAS_TWO")] < position[("works", "WOR_TWO")]
    assert position[("payments", "PAY_ZED")] < position[("payments", "PAY_ABC")]
    assert position[("payments", "PAY_BBB")] < position[("payments", "PAY_YYY")]
    assert position[("payments", "PAY_ABC")] < position[("works", "WOR_TWO")]
    assert position[("payments", "PAY_YYY")] < position[("works", "WOR_TWO")]
    assert position[("works", "WOR_TWO")] < position[("clients", "PER_TWO")]
    assert record_rows(shadow, "os_tasks")["TAS_TWO"]["deleted_at"] is not None
    assert record_rows(shadow, "os_clients")["PER_TWO"]["inputs"]["status"] == "לא פעיל"
    assert record_rows(shadow, "os_works")["WOR_TWO"]["inputs"]["status"] == "בוטלה"
    assert record_rows(shadow, "os_payments")["PAY_ZED"]["inputs"]["kind"] == "בוטל"


def test_the_ledger_has_dependents_refusal_surfaces_and_rolls_everything_back(
    shadow, tmp_path, monkeypatch
):
    run(shadow, staged(shadow, tmp_path, chain_records()))
    next_epoch(shadow, epoch=1)
    strict = E20Ledger(shadow)
    second = stage(strict, worker(2), workbook(
        tmp_path, tables_from_records(synthetic_records()), "second.xlsx"))
    # Parents first is wrong for a cancellation: the client would be cancelled while it has
    # dependents. (Proves the strict ledger bites, so the passing order above is the real thing.)
    reverse = tuple(reversed(promote_module._CANCEL_ORDER))
    monkeypatch.setattr(promote_module, "_CANCEL_ORDER", reverse)
    before = snapshot(shadow)
    error = refused("has_dependents", lambda: run(
        strict, second, epoch=2, cancellations=CHAIN_CANCELLATIONS))
    assert error.status == 409 and error.details["dependents"]
    assert snapshot(shadow) == before
    assert batch_row(shadow, second)["status"] == "validated"
    assert record_rows(shadow, "os_clients")["PER_TWO"]["inputs"]["status"] == "פעיל"


def test_creates_apply_parents_first_even_when_ids_sort_the_other_way(shadow, tmp_path):
    strict = E20Ledger(shadow)
    result = stage(strict, worker(), workbook(
        tmp_path, tables_from_records(chain_records()), "chain.xlsx"))
    run(strict, result)
    order = strict.order
    assert order.index(("clients", "PER_TWO")) < order.index(("works", "WOR_TWO"))
    assert order.index(("works", "WOR_TWO")) < order.index(("payments", "PAY_ABC"))
    assert order.index(("payments", "PAY_ABC")) < order.index(("payments", "PAY_ZED"))
    assert order.index(("payments", "PAY_YYY")) < order.index(("payments", "PAY_BBB"))
    assert order.index(("payments", "PAY_BBB")) < order.index(("tasks", "TAS_TWO"))


def test_a_workbook_record_that_the_ledger_deleted_is_refused(shadow, tmp_path):
    both = with_tasks(task_record("TAS_ONE"), task_record("TAS_OLD"))
    run(shadow, staged(shadow, tmp_path, both))
    next_epoch(shadow, epoch=1)
    cancel = [{"entity": "tasks", "record_id": "TAS_OLD"}]
    gone = staged(shadow, tmp_path, with_tasks(task_record("TAS_ONE")), epoch=2, name="b.xlsx")
    run(shadow, gone, epoch=2, cancellations=cancel)
    next_epoch(shadow, epoch=2)
    back = staged(shadow, tmp_path, both, epoch=3, name="c.xlsx")
    before = snapshot(shadow)
    error = refused("target_changed", lambda: run(shadow, back, epoch=3))
    assert error.details == {"reason": "import_target_deleted"}
    assert snapshot(shadow) == before


def test_a_custom_expiry_window_is_the_proposal_expiry(shadow, tmp_path):
    result = staged(shadow, tmp_path)
    run(shadow, result, undo_window_seconds=45)
    (proposal,) = shadow.rows("os_proposals")
    assert proposal["expires_at"] - proposal["created_at"] == timedelta(seconds=45)


def test_promote_logs_codes_and_counts_only(shadow, tmp_path, caplog):
    caplog.set_level("INFO", logger="mia.os_import")
    result = staged(shadow, tmp_path)
    refused("manifest_digest_mismatch", lambda: promote(shadow, worker(), result.batch, "0" * 64))
    run(shadow, result)
    run(shadow, result)
    text = caplog.text
    assert "os_import op=promote outcome=refused reason=manifest_digest_mismatch" in text
    assert "os_import op=promote outcome=ok reason=committed created=6 updated=0" in text
    assert "os_import op=promote outcome=ok reason=replayed created=0 updated=0" in text
    for leaked in ("Synthetic only", "PER_ONE", "WOR_ILS", "PAY_USD", str(tmp_path)):
        assert leaked not in text
