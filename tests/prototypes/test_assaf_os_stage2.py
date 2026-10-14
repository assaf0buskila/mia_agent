"""Combined milestone acceptance using invented fixed source rows only."""

import os
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.prototypes.assaf_os_adapter import AdapterError, LocalPrimaryAdapter, TrustedOwnerContext
from app.prototypes.assaf_os_domain import ENTITIES, ID_KEYS, validate_state
from app.prototypes.assaf_os_ledger import (
    LedgerEvent,
    LedgerOutbox,
    LedgerReceipt,
    LocalLedgerPrimary,
)
from app.prototypes.assaf_os_primary import LocalBinding, LocalPrimaryError, LocalPrincipal, _digest
from app.prototypes.assaf_os_shadow_import import LocalShadowImporter
from app.prototypes.assaf_os_summaries import build_summaries
from app.prototypes.assaf_os_synthetic import make_synthetic_batch, record_inputs, synthetic_records
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

OWNER = LocalPrincipal("12345", "OS")


@pytest.fixture(scope="module")
def stage2_engine():
    url = os.getenv("MIA_OS_LOCAL_PROTOTYPE_URL", "")
    if not url:
        pytest.skip("explicit disposable MIA_OS_LOCAL_PROTOTYPE_URL required")
    engine = create_engine(url, pool_size=4, max_overflow=0)
    LocalLedgerPrimary(engine, LocalBinding("guard", "12345"), enabled=True)._guard()
    yield engine
    engine.dispose()


@pytest.fixture
def combined(stage2_engine):
    binding = LocalBinding("combined_" + uuid4().hex, "12345")
    ledger = LocalLedgerPrimary(stage2_engine, binding, enabled=True)
    ledger.initialize_local_schema()
    shadow = LocalShadowImporter(stage2_engine, binding, enabled=True, chunk_size=2)
    shadow.initialize_local_schema()
    context = TrustedOwnerContext(
        numeric_owner=12345,
        workspace=binding.workspace_id,
        authority_epoch=1,
        channel="OS",
        scopes=frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve"}),
        auth_expires_at=datetime.now(UTC) + timedelta(hours=1),
        mutation_allowed=True,
        browser_mutation_verified=True,
    )
    server = {"context": context}
    adapter = LocalPrimaryAdapter(ledger, lambda: server["context"], enabled=True)
    return ledger, shadow, adapter, server


def receipt_counts(ledger):
    with Session(ledger.engine) as session:
        return [
            session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_id == ledger.binding.workspace_id)
            )
            for model in (LedgerReceipt, LedgerEvent, LedgerOutbox)
        ]


def by_id(records):
    return {entity: {row[ID_KEYS[entity]]: row for row in records[entity]} for entity in ENTITIES}


def test_shadow_import_has_no_business_rows_receipts_or_outbox(combined):
    ledger, shadow, adapter, _ = combined
    batch = make_synthetic_batch(ledger.binding.workspace_id)
    shadow.stage(OWNER, batch)
    manifest = shadow.reconcile(OWNER, batch["batch_id"])
    assert manifest["status"] == "accepted"
    assert manifest["issue_count"] == 0
    assert manifest["counts"] == {entity: len(batch["records"][entity]) for entity in ENTITIES}
    assert by_id(shadow.read_shadow(OWNER, batch["batch_id"])) == by_id(batch["records"])
    assert adapter.snapshot()["record_counts"] == dict.fromkeys(ENTITIES, 0)
    assert receipt_counts(ledger) == [0, 0, 0]
    assert adapter.snapshot()["primary_source"] == "postgresql"
    assert "source" not in adapter.snapshot()


def test_explicit_approval_is_separate_from_shadow_acceptance(combined):
    ledger, shadow, adapter, _ = combined
    batch = make_synthetic_batch(ledger.binding.workspace_id)
    shadow.stage(OWNER, batch)
    shadow.reconcile(OWNER, batch["batch_id"])
    proposal = adapter.propose(
        str(uuid4()),
        "clients",
        {"name": "Synthetic new client", "status": synthetic_records()["clients"][0]["status"]},
    )
    with pytest.raises(AdapterError) as exc:
        adapter.decide(proposal["proposal_id"], proposal["approval_id"])
    assert exc.value.code == "explicit_owner_decision_required"
    assert receipt_counts(ledger) == [0, 0, 0]
    result = adapter.decide(
        proposal["proposal_id"], proposal["approval_id"], explicit_owner_decision=True
    )
    assert result["verified_receipt"] is True
    assert result["mirror"]["status"] == "pending"
    assert receipt_counts(ledger) == [1, 1, 1]
    assert by_id(shadow.read_shadow(OWNER, batch["batch_id"])) == by_id(batch["records"])


def test_lost_ack_recovered_by_exact_receipt_after_policy_closes(combined, monkeypatch):
    ledger, _, adapter, server = combined
    proposal = adapter.propose(
        str(uuid4()),
        "tasks",
        record_inputs("tasks", synthetic_records()["tasks"][0])
        | {"person_id": None, "work_id": None},
    )
    read = ledger.receipt

    def lost(*args):
        raise ConnectionError("synthetic-response-loss")

    monkeypatch.setattr(ledger, "receipt", lost)
    with pytest.raises(AdapterError) as exc:
        adapter.decide(
            proposal["proposal_id"], proposal["approval_id"], explicit_owner_decision=True
        )
    assert exc.value.code == "inconclusive"
    assert exc.value.decision_id == proposal["approval_id"]
    assert receipt_counts(ledger) == [1, 1, 1]
    monkeypatch.setattr(ledger, "receipt", read)
    server["context"] = replace(server["context"], mutation_allowed=False)
    recovered = adapter.receipt(proposal["approval_id"])
    assert recovered["verified_receipt"] is True
    assert recovered["receipt"]["request_id"] == proposal["approval_id"]
    assert receipt_counts(ledger) == [1, 1, 1]


def test_shadow_crash_resumes_exact_batch_without_half_chunk(combined, monkeypatch):
    ledger, shadow, _, _ = combined
    batch = make_synthetic_batch(ledger.binding.workspace_id)
    after = shadow._after_chunk
    failures = []

    def crash(batch_id, ordinal):
        if not failures:
            failures.append(ordinal)
            raise ConnectionError("synthetic-after-chunk-loss")
        return after(batch_id, ordinal)

    monkeypatch.setattr(shadow, "_after_chunk", crash)
    with pytest.raises(ConnectionError):
        shadow.stage(OWNER, batch)
    assert failures == [2]
    shadow.stage(OWNER, batch)
    assert by_id(shadow.read_shadow(OWNER, batch["batch_id"])) == by_id(batch["records"])
    assert receipt_counts(ledger) == [0, 0, 0]


def test_changed_raw_source_is_quarantined_and_not_a_business_receipt(combined):
    ledger, shadow, _, _ = combined
    batch = make_synthetic_batch(ledger.binding.workspace_id)
    batch["records"]["payments"][0]["actual_amount_ils"] = 9999
    shadow.stage(OWNER, batch)
    manifest = shadow.reconcile(OWNER, batch["batch_id"])
    assert manifest["issues"]
    assert receipt_counts(ledger) == [0, 0, 0]
    exact_original = deepcopy(batch)
    shadow.stage(OWNER, exact_original)
    exact_original["records"]["payments"][0]["actual_amount_ils"] = 8888
    with pytest.raises(LocalPrimaryError) as exc:
        shadow.stage(OWNER, exact_original)
    assert exc.value.code == "batch_payload_mismatch"


def test_seven_entity_fixture_preserves_ids_revisions_and_known_financial_totals():
    rows = synthetic_records()
    validate_state(rows)
    assert sum(map(len, rows.values())) == 12
    assert set(rows) == set(ENTITIES)
    assert rows["clients"][0]["person_id"] == rows["leads"][0]["person_id"] == "legacy_person_A"
    assert rows["leads"][0]["revision"] == 3
    assert rows["clients"][0]["revision"] == 4
    assert rows["tasks"][0]["revision"] == 6
    totals = {r["currency"]: r for r in build_summaries(rows)["totals"]}
    assert totals["ILS"]["paid_income_ils"] == 10000
    assert totals["ILS"]["paid_expenses_ils"] == 1500
    assert totals["ILS"]["net_cash_ils"] == 8500
    assert totals["ILS"]["recorded_minutes"] == 90
    assert totals["ILS"]["effective_hourly_ils"] == 6667
    assert totals["EUR"]["paid_income_ils"] == 5000
    assert totals["EUR"]["effective_hourly_ils"] is None
    assert "missing_time" in totals["EUR"]["reason_codes"]


def test_source_and_accepted_provenance_are_explicit_and_detached():
    batch = make_synthetic_batch(
        "synthetic_workspace", batch_id="11111111-1111-4111-8111-111111111111"
    )
    assert batch["source_id"].startswith("synthetic:")
    for entity in ENTITIES:
        for row in batch["records"][entity]:
            provenance = batch["provenance"][entity][row[ID_KEYS[entity]]]
            assert provenance["source_input_digest"] == _digest(record_inputs(entity, row))
            assert provenance["accepted_input_digest"] == provenance["source_input_digest"]
            assert provenance["accepted_revision"] == row["revision"]
            assert provenance["acceptance_kind"] == "synthetic_native_acceptance"
    batch["records"]["payments"][0]["actual_amount_ils"] = 1
    assert (
        make_synthetic_batch("synthetic_workspace")["records"]["payments"][0]["actual_amount_ils"]
        == 12345
    )
