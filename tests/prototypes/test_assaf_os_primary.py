"""Real opt-in disposable PostgreSQL tests; never connect to configured application DB."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.prototypes.assaf_os_primary import (
    TASK_STATUSES,
    AuditEvent,
    LocalBase,
    LocalBinding,
    LocalPrimaryError,
    LocalPrincipal,
    LocalTaskPrimary,
    Outbox,
    Proposal,
    Receipt,
    Task,
    TaskInput,
    Workspace,
)
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.orm import Session

OWNER = LocalPrincipal("12345", "OS")
TELEGRAM = LocalPrincipal("12345", "TELEGRAM_APPROVAL")
MODEL = LocalPrincipal("12345", "MIA")
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def pg_engine():
    url = os.getenv("MIA_OS_LOCAL_PROTOTYPE_URL", "")
    if not url:
        pytest.skip("explicit disposable MIA_OS_LOCAL_PROTOTYPE_URL required")
    engine = create_engine(url, pool_size=4, max_overflow=0)
    service = LocalTaskPrimary(engine, LocalBinding("test_guard", "12345"), enabled=True)
    service._guard()
    with engine.connect() as c:
        assert c.scalar(text("SHOW server_version_num")) is not None
    yield engine
    engine.dispose()


@pytest.fixture
def primary(pg_engine):
    binding = LocalBinding("ws_" + uuid4().hex, "12345")
    service = LocalTaskPrimary(pg_engine, binding, enabled=True, clock=lambda: NOW)
    service.initialize_local_schema()
    return service


def task(title="Synthetic task", **changes):
    return TaskInput(title=title, status=TASK_STATUSES[0], **changes)


def propose(service, **changes):
    return service.propose(OWNER, str(uuid4()), task(), **changes)


def approve(service, proposal, principal=OWNER):
    return service.decide(principal, proposal["proposal_id"], proposal["approval_id"])


def counts(service):
    with Session(service.engine) as s:
        return [
            s.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.workspace_id == service.binding.workspace_id,
                )
            )
            for model in (Task, Receipt, AuditEvent, Outbox)
        ]


def assert_code(code, operation):
    with pytest.raises(LocalPrimaryError) as exc:
        operation()
    assert exc.value.code == code


def test_default_off_does_not_connect():
    engine = create_engine("postgresql+psycopg://127.0.0.1:1/assaf_os_local_unreachable")
    service = LocalTaskPrimary(engine, LocalBinding("local", "12345"))
    assert_code("prototype_disabled", service.initialize_local_schema)
    assert_code("prototype_disabled", lambda: service.propose(OWNER, str(uuid4()), task()))
    assert_code("prototype_disabled", lambda: service.receipt(OWNER, str(uuid4())))
    engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "postgresql+psycopg://external.example/assaf_os_local_x",
        "postgresql+psycopg://127.0.0.1/mia",
        "postgresql+psycopg://127.0.0.1/assaf_os_local_x?host=external",
    ],
)
def test_non_disposable_destinations_refused_without_connection(url):
    engine = create_engine(url)
    service = LocalTaskPrimary(engine, LocalBinding("local", "12345"), enabled=True)
    assert_code("disposable_loopback_database_required", service.initialize_local_schema)
    engine.dispose()


def test_task_input_strict_and_reference_scope_explicit():
    with pytest.raises(ValidationError):
        task("   ")
    with pytest.raises(ValidationError):
        TaskInput(title="x", status="unrecognized")
    with pytest.raises(ValidationError):
        task(person_id="PER_unimplemented")
    with pytest.raises(ValidationError):
        task(work_id="WOR_unimplemented")
    with pytest.raises(ValidationError):
        task(extra="unowned")
    assert task(due_date=date(2026, 10, 14)).model_dump(mode="json")["due_date"] == "2026-10-14"


def test_constructed_invalid_model_cannot_enter_primary(primary):
    invalid = TaskInput.model_construct(title="x", status="invalid")
    with pytest.raises(ValidationError):
        primary.propose(OWNER, str(uuid4()), invalid)
    assert counts(primary) == [0, 0, 0, 0]


def test_metadata_is_not_runtime_base():
    from app.db.base import Base

    assert not set(LocalBase.metadata.tables) & set(Base.metadata.tables)
    assert len(LocalBase.metadata.tables) == 6


def test_proposal_does_not_write_task_and_response_is_not_mutable_authority(primary):
    p = propose(primary)
    assert counts(primary) == [0, 0, 0, 0]
    p["after"]["input"]["title"] = "forged replacement"
    result = approve(primary, p)
    assert result["task"]["input"]["title"] == "Synthetic task"


def test_atomic_approval_receipt_audit_outbox_and_fresh_read(primary):
    observed_connections = []
    event.listen(primary.engine, "checkout", lambda *args: observed_connections.append(1))
    p = primary.propose(
        MODEL, str(uuid4()), task(notes="literal =text", due_date=date(2026, 10, 14))
    )
    observed_connections.clear()
    result = approve(primary, p, TELEGRAM)
    assert len(observed_connections) >= 2  # commit session then separate primary verification.
    assert result["primary_source"] == "postgresql"
    assert result["verified_primary"] is True
    assert result["receipt"]["status"] == "committed"
    assert result["receipt"]["revision"] == result["task"]["revision"] == 1
    assert result["observed_version"] >= result["receipt"]["commit_version"]
    assert result["task"]["input"]["due_date"] == "2026-10-14"
    assert result["mirror"] == {"status": "pending"}
    assert counts(primary) == [1, 1, 1, 1]
    with Session(primary.engine) as s:
        stored = s.scalar(select(Outbox).where(Outbox.workspace_id == primary.binding.workspace_id))
        assert stored.body == result["task"]
        assert stored.status == "pending"
        assert s.get(Task, (primary.binding.workspace_id, p["task_id"])).source == "MIA"


def test_wrong_owner_and_model_cannot_approve(primary):
    p = propose(primary)
    assert_code(
        "owner_approval_required", lambda: approve(primary, p, LocalPrincipal("67890", "OS"))
    )
    assert_code("owner_approval_required", lambda: approve(primary, p, MODEL))
    assert_code(
        "owner_required", lambda: primary.receipt(LocalPrincipal("67890", "OS"), p["approval_id"])
    )
    assert counts(primary) == [0, 0, 0, 0]


def test_absent_and_malformed_principals_are_refused_before_database_checkout(primary):
    p = propose(primary)
    approve(primary, p)
    connections = []

    def checkout(*args):
        connections.append(1)

    event.listen(primary.engine, "checkout", checkout)
    try:
        for invalid in (
            None,
            {},
            SimpleNamespace(owner_id="12345", source="OS"),
            LocalPrincipal("12345", "PUBLIC"),
            LocalPrincipal(12345, "OS"),
            LocalPrincipal("12345", []),
            LocalPrincipal("12345", {}),
        ):
            assert_code("owner_required", lambda: primary.receipt(invalid, p["approval_id"]))
            assert_code("owner_required", lambda: primary.propose(invalid, str(uuid4()), task()))
            assert_code("owner_approval_required", lambda: approve(primary, p, invalid))
    finally:
        event.remove(primary.engine, "checkout", checkout)
    assert connections == []
    assert counts(primary) == [1, 1, 1, 1]


def test_wrong_decision_id_cannot_consume_proposal(primary):
    p = propose(primary)
    assert_code(
        "decision_request_mismatch", lambda: primary.decide(OWNER, p["proposal_id"], str(uuid4()))
    )
    assert approve(primary, p)["receipt"]["status"] == "committed"


def test_propose_idempotency_is_content_bound(primary):
    request = str(uuid4())
    p = primary.propose(OWNER, request, task())
    assert primary.propose(OWNER, request, task()) == p
    assert_code("idempotency_mismatch", lambda: primary.propose(OWNER, request, task("changed")))
    assert_code("idempotency_mismatch", lambda: primary.propose(MODEL, request, task()))
    with Session(primary.engine) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(Proposal)
                .where(
                    Proposal.workspace_id == primary.binding.workspace_id,
                )
            )
            == 1
        )


def test_exact_expiry_is_terminal_without_business_write(primary):
    p = propose(primary)
    primary.clock = lambda: NOW + timedelta(minutes=10)
    result = approve(primary, p)
    assert result["receipt"]["status"] == "expired"
    assert result["receipt"]["committed_at"] is None
    assert counts(primary) == [0, 1, 1, 0]
    assert approve(primary, p) == result


def test_expiry_does_not_prevent_committed_receipt_recovery(primary):
    p = propose(primary)
    result = approve(primary, p)
    primary.clock = lambda: NOW + timedelta(days=1)
    assert approve(primary, p) == result
    assert counts(primary) == [1, 1, 1, 1]


def test_rejection_and_opposite_decision_do_not_commit(primary):
    p = propose(primary)
    r = primary.decide(TELEGRAM, p["proposal_id"], p["rejection_id"], "reject")
    assert r["receipt"]["status"] == "rejected"
    assert_code("already_decided", lambda: approve(primary, p))
    assert counts(primary) == [0, 1, 1, 0]


def test_revision_change_invalidates_old_proposal(primary):
    original = propose(primary)
    approve(primary, original)
    first = primary.propose(
        OWNER, str(uuid4()), task("first"), task_id=original["task_id"], expected_revision=1
    )
    second = primary.propose(
        OWNER, str(uuid4()), task("second"), task_id=original["task_id"], expected_revision=1
    )
    assert approve(primary, second)["task"]["revision"] == 2
    old = approve(primary, first)
    assert old["receipt"]["status"] == "target_changed"
    assert old["task"]["input"]["title"] == "second"
    assert counts(primary) == [1, 3, 3, 2]


def test_current_inputs_fingerprint_checked_even_if_revision_was_tampered(primary):
    original = propose(primary)
    approve(primary, original)
    p = primary.propose(
        OWNER, str(uuid4()), task("next"), task_id=original["task_id"], expected_revision=1
    )
    with primary.engine.begin() as c:
        c.execute(
            update(Task)
            .where(
                Task.workspace_id == primary.binding.workspace_id,
                Task.task_id == original["task_id"],
            )
            .values(inputs=task("tampered raw data").model_dump(mode="json"))
        )
    assert approve(primary, p)["receipt"]["status"] == "target_changed"
    assert counts(primary)[-1] == 1


def test_two_channels_concurrently_approve_once(primary):
    p, start = propose(primary), Barrier(2)

    def run(principal):
        start.wait(timeout=5)
        return approve(primary, p, principal)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run, OWNER), pool.submit(run, TELEGRAM)
        assert a.result(timeout=10) == b.result(timeout=10)
    assert counts(primary) == [1, 1, 1, 1]


def test_concurrent_approve_vs_reject_has_one_terminal_result(primary):
    p, start = propose(primary), Barrier(2)

    def run(principal, decision, request):
        start.wait(timeout=5)
        try:
            return primary.decide(principal, p["proposal_id"], request, decision)["receipt"][
                "status"
            ]
        except LocalPrimaryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run, OWNER, "approve", p["approval_id"])
        b = pool.submit(run, TELEGRAM, "reject", p["rejection_id"])
        results = {a.result(timeout=10), b.result(timeout=10)}
    assert "already_decided" in results
    assert results & {"committed", "rejected"}
    task_count, receipts, audits, exports = counts(primary)
    assert receipts == audits == 1
    assert task_count == exports == int("committed" in results)


def test_fault_after_task_receipt_event_rolls_everything_back(primary, monkeypatch):
    p = propose(primary)

    def fail(*args):
        raise RuntimeError("synthetic_transaction_fault")

    with monkeypatch.context() as m:
        m.setattr(primary, "_insert_outbox", fail)
        with pytest.raises(RuntimeError, match="synthetic_transaction_fault"):
            approve(primary, p)
    assert counts(primary) == [0, 0, 0, 0]
    with Session(primary.engine) as s:
        assert s.get(Proposal, (primary.binding.workspace_id, p["proposal_id"])).status == "pending"
        assert s.get(Workspace, primary.binding.workspace_id).version == 0
    assert approve(primary, p)["receipt"]["status"] == "committed"


def test_lost_response_after_commit_recovers_exact_receipt_without_duplicate(primary, monkeypatch):
    p = propose(primary)

    def lost(*args):
        raise OSError("synthetic_post_commit_response_loss")

    with monkeypatch.context() as m:
        m.setattr(primary, "receipt", lost)
        with pytest.raises(OSError, match="synthetic_post_commit_response_loss"):
            approve(primary, p)
    assert counts(primary) == [1, 1, 1, 1]
    recovered = primary.receipt(OWNER, p["approval_id"])
    assert recovered["receipt"]["status"] == "committed"
    assert approve(primary, p) == recovered  # explicit duplicate, no automatic retry exists.
    assert counts(primary) == [1, 1, 1, 1]


def test_unknown_receipt_does_not_claim_success(primary):
    r = primary.receipt(OWNER, str(uuid4()))
    assert r["status"] == "unknown" and r["verified_primary"] is False
    assert counts(primary) == [0, 0, 0, 0]


def test_authority_epoch_change_fails_closed(primary):
    p = propose(primary)
    with primary.engine.begin() as c:
        c.execute(
            update(Workspace)
            .where(
                Workspace.workspace_id == primary.binding.workspace_id,
            )
            .values(epoch=2)
        )
    assert_code("authority_changed", lambda: approve(primary, p))
    assert counts(primary) == [0, 0, 0, 0]


def test_workspace_isolation(primary):
    p = propose(primary)
    other = LocalTaskPrimary(
        primary.engine,
        LocalBinding("other_" + uuid4().hex, "12345"),
        enabled=True,
        clock=lambda: NOW,
    )
    other.initialize_local_schema()
    assert_code("proposal_not_found", lambda: approve(other, p))
    assert other.receipt(OWNER, p["approval_id"])["status"] == "unknown"
    assert counts(other) == [0, 0, 0, 0]


def test_envelope_tamper_is_refused(primary):
    p = propose(primary)
    with primary.engine.begin() as c:
        c.execute(
            update(Proposal)
            .where(
                Proposal.workspace_id == primary.binding.workspace_id,
                Proposal.proposal_id == p["proposal_id"],
            )
            .values(after={**p["after"], "input": task("tampered").model_dump(mode="json")})
        )
    assert_code("proposal_integrity_failed", lambda: approve(primary, p))
    assert counts(primary) == [0, 0, 0, 0]


def test_primary_read_detects_same_revision_data_drift(primary):
    p = propose(primary)
    approve(primary, p)
    with primary.engine.begin() as c:
        c.execute(
            update(Task)
            .where(
                Task.workspace_id == primary.binding.workspace_id,
                Task.task_id == p["task_id"],
            )
            .values(inputs=task("drift").model_dump(mode="json"))
        )
    assert_code("primary_verification_failed", lambda: primary.receipt(OWNER, p["approval_id"]))
