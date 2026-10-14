"""Real-PostgreSQL acceptance for bounded synthetic shadow staging."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import pytest
from app.prototypes.assaf_os_domain import ENTITIES, SCHEMA
from app.prototypes.assaf_os_ledger import LedgerBase
from app.prototypes.assaf_os_primary import (
    LocalBase,
    LocalBinding,
    LocalPrimaryError,
    LocalPrincipal,
    LocalTaskPrimary,
    _digest,
)
from app.prototypes.assaf_os_shadow_import import (
    MAX_PAYLOAD_BYTES,
    LocalShadowImporter,
    ShadowBase,
    ShadowBatch,
    ShadowCheckpoint,
    ShadowIssue,
    ShadowProvenance,
    ShadowRecord,
    ShadowWorkspace,
)
from app.prototypes.assaf_os_synthetic import make_synthetic_batch, record_inputs
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.orm import Session

OWNER = LocalPrincipal("12345", "OS")
MIA = LocalPrincipal("12345", "MIA")


@pytest.fixture(scope="module")
def engine():
    url = os.getenv("MIA_OS_LOCAL_PROTOTYPE_URL", "")
    if not url:
        pytest.skip("explicit disposable MIA_OS_LOCAL_PROTOTYPE_URL required")
    pg = create_engine(url, pool_size=6, max_overflow=0)
    LocalShadowImporter(pg, LocalBinding("guard", "12345"), enabled=True)._guard()
    yield pg
    pg.dispose()


@pytest.fixture
def importer(engine):
    service = LocalShadowImporter(
        engine,
        LocalBinding("shadow_" + uuid4().hex, "12345"),
        enabled=True,
        chunk_size=3,
    )
    service.initialize_local_schema()
    return service


def code(expected, operation):
    with pytest.raises(LocalPrimaryError) as exc:
        operation()
    assert exc.value.code == expected


def count(session, model, service):
    return session.scalar(
        select(func.count())
        .select_from(model)
        .where(model.workspace_id == service.binding.workspace_id)
    )


def test_default_off_and_shadow_metadata_are_isolated(engine):
    from app.db.base import Base

    assert len(ShadowBase.metadata.tables) == 6
    assert not set(ShadowBase.metadata.tables) & set(Base.metadata.tables)
    assert not set(ShadowBase.metadata.tables) & set(LocalBase.metadata.tables)
    assert not set(ShadowBase.metadata.tables) & set(LedgerBase.metadata.tables)
    service = LocalShadowImporter(engine, LocalBinding("disabled", "12345"))
    code("prototype_disabled", service.initialize_local_schema)
    code("prototype_disabled", lambda: service.stage(OWNER, {}))
    code("prototype_disabled", lambda: service.read_shadow(OWNER, str(uuid4())))
    code("prototype_disabled", lambda: service.reconcile(OWNER, str(uuid4())))


def test_shadow_service_exposes_no_transaction_api_when_both_schemas_exist(engine):
    binding = LocalBinding("shadow_no_writes_" + uuid4().hex, "12345")
    LocalTaskPrimary(engine, binding, enabled=True).initialize_local_schema()
    service = LocalShadowImporter(engine, binding, enabled=True)
    service.initialize_local_schema()
    for name in ("propose", "decide", "receipt", "snapshot"):
        assert not hasattr(service, name)


def test_initialization_is_race_safe_and_binding_is_immutable(engine):
    binding = LocalBinding("shadow_init_" + uuid4().hex, "12345")
    services = [LocalShadowImporter(engine, binding, enabled=True) for _ in range(2)]
    barrier = Barrier(2)

    def initialize(service):
        barrier.wait(timeout=5)
        service.initialize_local_schema()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(initialize, services))
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ShadowWorkspace)
                .where(ShadowWorkspace.workspace_id == binding.workspace_id)
            )
            == 1
        )
    changed = LocalShadowImporter(engine, LocalBinding(binding.workspace_id, "54321"), enabled=True)
    code("authority_changed", changed.initialize_local_schema)


def test_bad_headers_and_oversize_are_rejected_before_checkout(importer, monkeypatch):
    checkouts = []

    def checkout(*_args):
        checkouts.append(1)

    event.listen(importer.engine, "checkout", checkout)
    monkeypatch.setattr("app.prototypes.assaf_os_shadow_import.MAX_RECORDS", 11)
    try:
        batch = make_synthetic_batch(importer.binding.workspace_id)
        code("shadow_record_limit", lambda: importer.stage(OWNER, batch))
        bad_time = make_synthetic_batch(importer.binding.workspace_id)
        bad_time["observed_at"] = "2026-10-14T01:00:00"
        code("observed_at_must_be_utc", lambda: importer.stage(OWNER, bad_time))
        real_marker = make_synthetic_batch(importer.binding.workspace_id)
        real_marker["source_id"] = "google:forbidden"
        code("synthetic_source_required", lambda: importer.stage(OWNER, real_marker))
        assert checkouts == []
    finally:
        event.remove(importer.engine, "checkout", checkout)


def test_stage_reconcile_preserves_raw_provenance_and_financial_semantics(importer):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    result = importer.stage(MIA, batch)
    assert result == {
        "workspace_id": importer.binding.workspace_id,
        "batch_id": batch["batch_id"],
        "status": "complete",
        "payload_fingerprint": result["payload_fingerprint"],
        "total_records": 12,
        "staged_records": 12,
        "issue_count": 0,
        "duplicate": False,
        "checkpoint": {"next_ordinal": 12, "total_records": 12, "status": "complete"},
    }
    assert importer.read_shadow(OWNER, batch["batch_id"]) == batch["records"]

    manifest = importer.reconcile(MIA, batch["batch_id"])
    assert manifest["status"] == "accepted"
    assert manifest["issue_count"] == 0
    assert set(manifest["counts"]) == set(ENTITIES)
    assert sum(manifest["counts"].values()) == 12
    assert manifest["money_by_currency"] == {
        "EUR": {
            "received_minor": 5000,
            "refund_minor": 0,
            "net_payments_minor": 5000,
            "paid_expenses_minor": 0,
            "net_cash_minor": 5000,
        },
        "ILS": {
            "received_minor": 12345,
            "refund_minor": 2345,
            "net_payments_minor": 10000,
            "paid_expenses_minor": 1500,
            "net_cash_minor": 8500,
        },
    }
    assert manifest["refunds"] == [
        {
            "payment_id": "legacy_refund_A",
            "original_payment_id": "legacy_payment_A",
            "amount_minor": 2345,
            "currency": "ILS",
        }
    ]
    totals = {row["currency"]: row for row in manifest["summaries"]["totals"]}
    assert totals["ILS"]["effective_hourly_ils"] == 6667
    assert totals["EUR"]["effective_hourly_ils"] is None
    assert "missing_time" in totals["EUR"]["reason_codes"]
    assert manifest["references"]["unresolved"] == 0
    assert manifest["input_hashes"]["payments"]["legacy_payment_A"] == {
        "source_input_digest": batch["provenance"]["payments"]["legacy_payment_A"][
            "source_input_digest"
        ],
        "accepted_input_digest": batch["provenance"]["payments"]["legacy_payment_A"][
            "accepted_input_digest"
        ],
    }
    assert manifest["observed_at"] == "2026-10-14T01:00:00+00:00"

    with Session(importer.engine) as session:
        provenance = session.scalar(
            select(ShadowProvenance)
            .where(
                ShadowProvenance.workspace_id == importer.binding.workspace_id,
                ShadowProvenance.batch_id == batch["batch_id"],
            )
            .order_by(ShadowProvenance.ordinal)
        )
        first_entity = ENTITIES[0]
        first_id = next(iter(batch["provenance"][first_entity]))
        assert provenance.raw_provenance == batch["provenance"][first_entity][first_id]
        assert count(session, ShadowRecord, importer) == 12
        assert count(session, ShadowProvenance, importer) == 12
        assert count(session, ShadowIssue, importer) == 0


def test_duplicate_batch_replay_is_harmless_and_changed_payload_is_refused(importer):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    first = importer.stage(OWNER, batch)
    replay = importer.stage(OWNER, deepcopy(batch))
    assert replay["duplicate"] is True
    assert replay["payload_fingerprint"] == first["payload_fingerprint"]
    with Session(importer.engine) as session:
        assert count(session, ShadowRecord, importer) == 12
    changed = deepcopy(batch)
    changed["records"]["tasks"][0]["title"] = "Changed synthetic title"
    code("batch_payload_mismatch", lambda: importer.stage(OWNER, changed))
    assert importer.read_shadow(OWNER, batch["batch_id"]) == batch["records"]


def test_committed_chunk_resumes_and_failed_chunk_has_no_half_rows(importer, monkeypatch):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    calls = []

    def fail_after_first(_batch_id, next_ordinal):
        calls.append(next_ordinal)
        raise RuntimeError("synthetic_crash_between_chunks")

    monkeypatch.setattr(importer, "_after_chunk", fail_after_first)
    with pytest.raises(RuntimeError, match="synthetic_crash_between_chunks"):
        importer.stage(OWNER, batch)
    with Session(importer.engine) as session:
        checkpoint = session.get(
            ShadowCheckpoint, (importer.binding.workspace_id, batch["batch_id"])
        )
        assert checkpoint.next_ordinal == 3
        assert count(session, ShadowRecord, importer) == 3
    monkeypatch.setattr(importer, "_after_chunk", lambda *_args: None)
    resumed = importer.stage(OWNER, batch)
    assert resumed["duplicate"] is True
    assert resumed["staged_records"] == 12
    assert importer.reconcile(OWNER, batch["batch_id"])["status"] == "accepted"

    second = make_synthetic_batch(importer.binding.workspace_id)
    monkeypatch.setattr(
        importer,
        "_before_checkpoint",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("synthetic_failed_chunk")),
    )
    with pytest.raises(RuntimeError, match="synthetic_failed_chunk"):
        importer.stage(OWNER, second)
    with Session(importer.engine) as session:
        checkpoint = session.get(
            ShadowCheckpoint, (importer.binding.workspace_id, second["batch_id"])
        )
        assert checkpoint.next_ordinal == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(ShadowRecord)
                .where(
                    ShadowRecord.workspace_id == importer.binding.workspace_id,
                    ShadowRecord.batch_id == second["batch_id"],
                )
            )
            == 0
        )


def test_concurrent_duplicate_stage_serializes_without_duplicate_rows(importer):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return importer.stage(OWNER, deepcopy(batch))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: run(), range(2)))
    assert all(result["status"] == "complete" for result in results)
    assert sorted(result["duplicate"] for result in results) == [False, True]
    with Session(importer.engine) as session:
        assert count(session, ShadowBatch, importer) == 1
        assert count(session, ShadowRecord, importer) == 12
        assert count(session, ShadowProvenance, importer) == 12


def test_conflicts_missing_provenance_raw_change_and_unknown_coverage_quarantine(importer):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    batch["coverage"]["time"] = "unknown"
    lead = batch["records"]["leads"][0]
    lead["name"] = "Changed after source acceptance"
    lead_meta = batch["provenance"]["leads"][lead["lead_id"]]
    lead_meta["acceptance_kind"] = "owner_approved"
    task = batch["records"]["tasks"][0]
    del batch["provenance"]["tasks"][task["task_id"]]
    duplicate = deepcopy(task)
    batch["records"]["tasks"].append(duplicate)
    missing_id = deepcopy(task)
    del missing_id["task_id"]
    batch["records"]["tasks"].append(missing_id)

    staged = importer.stage(OWNER, batch)
    assert staged["total_records"] == 14
    assert staged["issue_count"] > 0
    shadow = importer.read_shadow(OWNER, batch["batch_id"])
    assert len(shadow["tasks"]) == 3
    assert shadow["tasks"][-1] == missing_id
    manifest = importer.reconcile(OWNER, batch["batch_id"])
    assert manifest["status"] == "quarantined"
    assert {
        "accepted_input_changed",
        "coverage_unknown",
        "duplicate_record_id",
        "missing_or_invalid_record_id",
        "provenance_missing_or_invalid",
        "source_input_changed",
        "state_validation_failed",
        "unaccepted_record",
    } <= set(manifest["issue_codes"])
    assert manifest["coverage"]["time"] == "unknown"
    assert manifest["summaries"] is None


def test_expected_summary_mismatch_and_wrong_owner_block_acceptance(importer):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    batch["expected_summary"] = {"synthetic_expected": "wrong"}
    importer.stage(OWNER, batch)
    manifest = importer.reconcile(OWNER, batch["batch_id"])
    assert manifest["status"] == "quarantined"
    assert manifest["expected_summary_match"] is False
    assert "expected_summary_mismatch" in manifest["issue_codes"]

    wrong = LocalPrincipal("99999", "OS")
    code("owner_required", lambda: importer.read_shadow(wrong, batch["batch_id"]))
    code("owner_required", lambda: importer.reconcile(wrong, batch["batch_id"]))
    code(
        "owner_required",
        lambda: importer.stage(wrong, make_synthetic_batch(importer.binding.workspace_id)),
    )


def test_deleted_provenance_blocks_read_reconcile_and_resume(importer):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    importer.stage(OWNER, batch)
    with Session(importer.engine) as session, session.begin():
        row = session.scalar(
            select(ShadowProvenance)
            .where(
                ShadowProvenance.workspace_id == importer.binding.workspace_id,
                ShadowProvenance.batch_id == batch["batch_id"],
            )
            .order_by(ShadowProvenance.ordinal)
        )
        session.delete(row)
    code(
        "shadow_batch_integrity_failed",
        lambda: importer.read_shadow(OWNER, batch["batch_id"]),
    )
    code(
        "shadow_batch_integrity_failed",
        lambda: importer.reconcile(OWNER, batch["batch_id"]),
    )
    code("shadow_batch_integrity_failed", lambda: importer.stage(OWNER, batch))


def test_valid_looking_raw_tamper_even_with_rehashed_row_is_refused(importer):
    from app.prototypes.assaf_os_primary import _digest

    batch = make_synthetic_batch(importer.binding.workspace_id)
    importer.stage(OWNER, batch)
    changed = deepcopy(batch["records"]["leads"][0])
    changed["name"] = "Synthetic tamper that remains domain-valid"
    with Session(importer.engine) as session, session.begin():
        session.execute(
            update(ShadowRecord)
            .where(
                ShadowRecord.workspace_id == importer.binding.workspace_id,
                ShadowRecord.batch_id == batch["batch_id"],
                ShadowRecord.ordinal == 0,
            )
            .values(raw_record=changed, raw_record_digest=_digest(changed))
        )
    code(
        "shadow_batch_integrity_failed",
        lambda: importer.read_shadow(OWNER, batch["batch_id"]),
    )
    code(
        "shadow_batch_integrity_failed",
        lambda: importer.reconcile(OWNER, batch["batch_id"]),
    )


def test_persisted_batch_epoch_drift_is_refused(importer):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    importer.stage(OWNER, batch)
    with Session(importer.engine) as session, session.begin():
        session.execute(
            update(ShadowBatch)
            .where(
                ShadowBatch.workspace_id == importer.binding.workspace_id,
                ShadowBatch.batch_id == batch["batch_id"],
            )
            .values(authority_epoch=importer.binding.authority_epoch + 1)
        )
    code("authority_changed", lambda: importer.read_shadow(OWNER, batch["batch_id"]))
    code("authority_changed", lambda: importer.reconcile(OWNER, batch["batch_id"]))
    code("authority_changed", lambda: importer.stage(OWNER, batch))


def test_promoted_lead_requires_client_but_unpromoted_identity_does_not(importer):
    promoted = make_synthetic_batch(importer.binding.workspace_id)
    for entity in ENTITIES:
        if entity != "leads":
            promoted["records"][entity] = []
            promoted["provenance"][entity] = {}
    importer.stage(OWNER, promoted)
    promoted_manifest = importer.reconcile(OWNER, promoted["batch_id"])
    assert promoted_manifest["status"] == "quarantined"
    assert promoted_manifest["references"]["unresolved"] == 1
    assert "unresolved_reference" in promoted_manifest["issue_codes"]

    unpromoted = make_synthetic_batch(importer.binding.workspace_id)
    for entity in ENTITIES:
        if entity != "leads":
            unpromoted["records"][entity] = []
            unpromoted["provenance"][entity] = {}
    lead = unpromoted["records"]["leads"][0]
    lead["status"] = SCHEMA["enums"]["lead_status"][0]
    digest = _digest(record_inputs("leads", lead))
    meta = unpromoted["provenance"]["leads"][lead["lead_id"]]
    meta["source_input_digest"] = digest
    meta["accepted_input_digest"] = digest
    importer.stage(OWNER, unpromoted)
    unpromoted_manifest = importer.reconcile(OWNER, unpromoted["batch_id"])
    assert unpromoted_manifest["status"] == "accepted"
    assert unpromoted_manifest["references"]["unresolved"] == 0
    lead_reference = unpromoted_manifest["references"]["items"][0]
    assert lead_reference["resolved"] is False
    assert lead_reference["required"] is False


def test_malformed_index_values_are_staged_and_quarantined_without_db_type_errors(importer):
    batch = make_synthetic_batch(importer.binding.workspace_id)
    task = batch["records"]["tasks"][0]
    original_task_id = task["task_id"]
    task["task_id"] = []
    lead_id = batch["records"]["leads"][0]["lead_id"]
    batch["provenance"]["leads"][lead_id]["accepted_revision"] = 10**30
    batch["provenance"]["tasks"]["x" * 400] = deepcopy(
        batch["provenance"]["tasks"][original_task_id]
    )
    result = importer.stage(OWNER, batch)
    assert result["status"] == "complete"
    manifest = importer.reconcile(OWNER, batch["batch_id"])
    assert manifest["status"] == "quarantined"
    assert {
        "invalid_accepted_revision",
        "missing_or_invalid_record_id",
        "orphan_provenance",
        "provenance_missing_or_invalid",
        "state_validation_failed",
    } <= set(manifest["issue_codes"])
    assert importer.read_shadow(OWNER, batch["batch_id"])["tasks"][0]["task_id"] == []


def test_strict_json_header_and_payload_bounds_fail_before_checkout(importer):
    checkouts = []
    rejected_batch_ids = []

    def checkout(*_args):
        checkouts.append(1)

    event.listen(importer.engine, "checkout", checkout)
    try:
        cases = []
        authority_bool = make_synthetic_batch(importer.binding.workspace_id)
        authority_bool["authority_epoch"] = True
        cases.append(("authority_changed", authority_bool))
        bad_coverage = make_synthetic_batch(importer.binding.workspace_id)
        bad_coverage["coverage"]["tasks"] = []
        cases.append(("invalid_batch_shape", bad_coverage))
        nan_record = make_synthetic_batch(importer.binding.workspace_id)
        nan_record["records"]["leads"][0]["name"] = float("nan")
        cases.append(("batch_not_json", nan_record))
        nul_record = make_synthetic_batch(importer.binding.workspace_id)
        nul_record["records"]["leads"][0]["name"] = "bad\x00text"
        cases.append(("batch_not_json", nul_record))
        long_source = make_synthetic_batch(importer.binding.workspace_id)
        long_source["source_id"] = "synthetic:" + "x" * 300
        cases.append(("synthetic_source_required", long_source))
        oversized = make_synthetic_batch(importer.binding.workspace_id)
        oversized["records"]["leads"][0]["name"] = "x" * (MAX_PAYLOAD_BYTES + 1)
        cases.append(("batch_payload_too_large", oversized))
        huge_epoch = make_synthetic_batch(importer.binding.workspace_id)
        huge_epoch["source_epoch"] = 10**30
        cases.append(("invalid_source_epoch", huge_epoch))
        tuple_with_nul = make_synthetic_batch(importer.binding.workspace_id)
        tuple_with_nul["records"]["tasks"][0]["notes"] = ("a\x00b",)
        cases.append(("batch_not_json", tuple_with_nul))
        overdeep = make_synthetic_batch(importer.binding.workspace_id)
        nested = "synthetic"
        for _ in range(1_100):
            nested = [nested]
        overdeep["records"]["tasks"][0]["notes"] = nested
        cases.append(("batch_json_too_complex", overdeep))
        for expected, batch in cases:
            rejected_batch_ids.append(batch["batch_id"])
            code(expected, lambda expected_batch=batch: importer.stage(OWNER, expected_batch))
        assert checkouts == []
    finally:
        event.remove(importer.engine, "checkout", checkout)
    with Session(importer.engine) as session:
        assert not session.scalars(
            select(ShadowBatch.batch_id).where(
                ShadowBatch.workspace_id == importer.binding.workspace_id,
                ShadowBatch.batch_id.in_(rejected_batch_ids),
            )
        ).all()


def test_empty_batch_completes_and_unknown_coverage_flows_into_summary(importer):
    empty = make_synthetic_batch(importer.binding.workspace_id)
    for entity in ENTITIES:
        empty["records"][entity] = []
        empty["provenance"][entity] = {}
    staged = importer.stage(OWNER, empty)
    assert staged["checkpoint"] == {
        "next_ordinal": 0,
        "total_records": 0,
        "status": "complete",
    }
    assert importer.reconcile(OWNER, empty["batch_id"])["status"] == "accepted"

    unknown = make_synthetic_batch(importer.binding.workspace_id)
    unknown["coverage"] = dict.fromkeys(ENTITIES, "unknown")
    importer.stage(OWNER, unknown)
    manifest = importer.reconcile(OWNER, unknown["batch_id"])
    assert manifest["status"] == "quarantined"
    assert manifest["summaries"]["coverage"] == "unknown"
    assert manifest["issue_codes"] == ["coverage_unknown"]
