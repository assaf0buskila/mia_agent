"""Synthetic real-PostgreSQL acceptance for seven-entity atomic transactions."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from app.prototypes.assaf_os_domain import ENTITIES, SCHEMA
from app.prototypes.assaf_os_ledger import (
    MODELS,
    Client,
    LedgerBase,
    LedgerEvent,
    LedgerOutbox,
    LedgerProposal,
    LedgerReceipt,
    LedgerWorkspace,
    LocalLedgerPrimary,
    Payment,
)
from app.prototypes.assaf_os_primary import LocalBinding, LocalPrimaryError, LocalPrincipal
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

OWNER = LocalPrincipal("12345", "OS")
TELEGRAM = LocalPrincipal("12345", "TELEGRAM_APPROVAL")
MODEL = LocalPrincipal("12345", "MIA")
NOW = datetime(2026, 10, 14, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine():
    url = os.getenv("MIA_OS_LOCAL_PROTOTYPE_URL", "")
    if not url:
        pytest.skip("explicit disposable MIA_OS_LOCAL_PROTOTYPE_URL required")
    pg = create_engine(url, pool_size=4, max_overflow=0)
    LocalLedgerPrimary(pg, LocalBinding("guard", "12345"), enabled=True)._guard()
    yield pg
    pg.dispose()


@pytest.fixture
def ledger(engine):
    service = LocalLedgerPrimary(
        engine, LocalBinding("ws_" + uuid4().hex, "12345"), enabled=True, clock=lambda: NOW
    )
    service.initialize_local_schema()
    return service


def enum(name, index=0):
    return SCHEMA["enums"][name][index]


def data(entity, **changes):
    defaults = {
        "leads": {"name": "Synthetic lead", "path": enum("path"), "status": enum("lead_status")},
        "clients": {"name": "Synthetic client", "status": enum("client_status")},
        "works": {
            "title": "Synthetic work",
            "path": enum("path"),
            "status": enum("work_status"),
            "currency": "ILS",
            "agreed_amount_ils": 999999,
        },
        "payments": {
            "path": enum("path"),
            "date": "2026-10-14",
            "actual_amount_ils": 10000,
            "currency": "ILS",
            "kind": enum("payment_kind"),
        },
        "time": {
            "path": enum("path"),
            "date": "2026-10-14",
            "minutes": 60,
            "status": enum("time_status"),
        },
        "expenses": {
            "date": "2026-10-14",
            "category": "Synthetic",
            "platform": "Meta",
            "path": enum("path"),
            "amount_ils": 1000,
            "currency": "ILS",
            "payment_status": enum("payment_status"),
            "source_type": enum("expense_source"),
            "source_key": "synthetic-" + uuid4().hex,
        },
        "tasks": {"title": "Synthetic task", "status": enum("task_status")},
    }
    return defaults[entity] | changes


def propose(ledger, entity, values=None, **kwargs):
    return ledger.propose(
        OWNER, str(uuid4()), entity, data(entity) if values is None else values, **kwargs
    )


def approve(ledger, proposal, principal=OWNER):
    return ledger.decide(principal, proposal["proposal_id"], proposal["approval_id"])


def create(ledger, entity, **changes):
    result = approve(ledger, propose(ledger, entity, data(entity, **changes)))
    assert result["receipt"]["status"] == "committed"
    return result["records"][0]["record"]


def business(ledger, currency="ILS"):
    client = create(ledger, "clients")
    work = create(ledger, "works", person_id=client["person_id"], currency=currency)
    return {"person_id": client["person_id"], "work_id": work["work_id"]}


def counts(ledger):
    with Session(ledger.engine) as s:
        return {
            model.__tablename__: s.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_id == ledger.binding.workspace_id)
            )
            for model in (*MODELS.values(), LedgerReceipt, LedgerEvent, LedgerOutbox)
        }


def code(expected, operation):
    with pytest.raises(LocalPrimaryError) as exc:
        operation()
    assert exc.value.code == expected


def test_default_off_and_metadata_remain_isolated():
    from app.db.base import Base
    from app.prototypes.assaf_os_primary import LocalBase

    assert len(LocalBase.metadata.tables) == 6
    assert len(LedgerBase.metadata.tables) == 12
    assert not set(LedgerBase.metadata.tables) & set(Base.metadata.tables)
    pg = create_engine("postgresql+psycopg://127.0.0.1:1/assaf_os_local_unreachable")
    service = LocalLedgerPrimary(pg, LocalBinding("local", "12345"))
    code("prototype_disabled", service.initialize_local_schema)
    code("prototype_disabled", lambda: service.snapshot(OWNER))
    code("prototype_disabled", lambda: propose(service, "tasks"))
    pg.dispose()


def test_capacity_is_rechecked_under_approval_lock(ledger, monkeypatch):
    import app.prototypes.assaf_os_ledger as module

    monkeypatch.setattr(module, "MAX_RECORDS", 2)
    create(ledger, "tasks")
    proposals = [propose(ledger, "tasks", data("tasks", title=str(i))) for i in range(2)]
    gate = Barrier(2)

    def run(p):
        gate.wait(timeout=5)
        return approve(ledger, p)["receipt"]["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(run, proposals))
    assert sorted(statuses) == ["committed", "target_changed"]
    assert ledger.snapshot(OWNER)["record_counts"]["tasks"] == 2


def test_long_unicode_expense_source_commits_and_deduplicates_exactly(ledger):
    import random

    rng = random.Random(42)
    source_key = "".join(chr(rng.randrange(0x4E00, 0x9FFF)) for _ in range(1800))
    expense = create(ledger, "expenses", source_key=source_key)
    assert expense["source_key"] == source_key
    code(
        "duplicate_expense_source",
        lambda: propose(ledger, "expenses", data("expenses", source_key=source_key)),
    )


def test_promotion_preserves_existing_client_values_and_native_revision_semantics(ledger):
    lead = create(ledger, "leads")
    p = ledger.propose(
        OWNER, str(uuid4()), "promote_lead", {}, record_id=lead["lead_id"], expected_revision=1
    )
    approve(ledger, p)
    approve(
        ledger,
        propose(
            ledger,
            "clients",
            {"notes": "existing owner notes"},
            record_id=lead["person_id"],
            expected_revision=1,
        ),
    )
    p = ledger.propose(
        OWNER, str(uuid4()), "promote_lead", {}, record_id=lead["lead_id"], expected_revision=2
    )
    result = approve(ledger, p)
    client = next(item["record"] for item in result["records"] if item["entity"] == "clients")
    assert client["person_id"] == lead["person_id"]
    assert client["notes"] == "existing owner notes"
    assert client["revision"] == 3
    assert len(result["receipt"]["records"]) == 2


def test_summary_overflow_is_sanitized_and_competing_payments_fail_closed(ledger):
    refs = business(ledger)
    maximum = 9_007_199_254_740_991
    p1 = propose(ledger, "payments", data("payments", **refs, actual_amount_ils=maximum))
    p2 = propose(ledger, "payments", data("payments", **refs, actual_amount_ils=1))
    assert approve(ledger, p1)["receipt"]["status"] == "committed"
    assert approve(ledger, p2)["receipt"]["status"] == "target_changed"
    code(
        "amount_overflow",
        lambda: propose(ledger, "payments", data("payments", **refs, actual_amount_ils=1)),
    )
    assert ledger.snapshot(OWNER)["record_counts"]["payments"] == 1


def test_business_date_is_postgresql_date_without_timezone_shift(ledger):
    from datetime import date

    refs = business(ledger)
    payment = create(ledger, "payments", **refs, date="2026-10-14")
    with Session(ledger.engine) as s:
        row = s.get(Payment, (ledger.binding.workspace_id, payment["payment_id"]))
        assert type(row.business_date) is date
        assert row.business_date == date(2026, 10, 14)
        assert row.inputs["date"] == "2026-10-14"
        assert row.created_at.utcoffset() is not None


def test_expired_outer_deadline_replay_returns_original_proposal(ledger):
    request = str(uuid4())
    expires = NOW + timedelta(seconds=30)
    p = ledger.propose(OWNER, request, "tasks", data("tasks"), approval_expires_at=expires)
    ledger.clock = lambda: NOW + timedelta(minutes=1)
    assert ledger.propose(OWNER, request, "tasks", data("tasks"), approval_expires_at=expires) == p


def test_missing_and_malformed_principals_refused_before_checkout(ledger):
    from sqlalchemy import event

    checkouts = []

    def checkout(*args):
        checkouts.append(1)

    event.listen(ledger.engine, "checkout", checkout)
    try:
        for principal in (None, {}, LocalPrincipal("12345", []), LocalPrincipal(12345, "OS")):
            code("owner_required", lambda: ledger.snapshot(principal))
            code("owner_required", lambda: ledger.receipt(principal, str(uuid4())))
            code(
                "owner_required",
                lambda: ledger.propose(principal, str(uuid4()), "tasks", data("tasks")),
            )
            code(
                "owner_approval_required",
                lambda: ledger.decide(principal, "PRO_unknown", str(uuid4())),
            )
    finally:
        event.remove(ledger.engine, "checkout", checkout)
    assert checkouts == []


def test_all_seven_families_proposals_commit_exact_receipts_and_outbox(ledger):
    refs = business(ledger)
    create(ledger, "leads", phone="0521234567")
    create(ledger, "payments", **refs)
    create(ledger, "time", **refs)
    create(ledger, "expenses")
    create(ledger, "tasks", **refs, path=enum("path"), due_date="2026-10-15")
    snapshot = ledger.snapshot(OWNER)
    assert all(snapshot["record_counts"][e] == 1 for e in ENTITIES)
    assert snapshot["observed_version"] == 7
    with Session(ledger.engine) as s:
        outbox = s.scalars(
            select(LedgerOutbox).where(LedgerOutbox.workspace_id == ledger.binding.workspace_id)
        ).all()
        assert len(outbox) == 7
        assert all(row.status == "pending" and row.body["authority_epoch"] == 1 for row in outbox)


def test_proposal_is_detached_immutable_and_no_business_effect(ledger):
    p = propose(ledger, "expenses")
    assert all(value == 0 for value in counts(ledger).values())
    p["mutations"][0]["after"]["amount_ils"] = 99999
    result = approve(ledger, p)
    assert result["records"][0]["record"]["amount_ils"] == 1000


def test_proposal_and_decision_idempotency_across_transports(ledger):
    request = str(uuid4())
    p = ledger.propose(MODEL, request, "tasks", data("tasks"))
    assert ledger.propose(MODEL, request, "tasks", data("tasks")) == p
    code(
        "idempotency_mismatch",
        lambda: ledger.propose(MODEL, request, "tasks", data("tasks", title="other")),
    )
    first = approve(ledger, p, TELEGRAM)
    assert approve(ledger, p) == first
    code("decision_request_mismatch", lambda: ledger.decide(OWNER, p["proposal_id"], str(uuid4())))
    code(
        "already_decided",
        lambda: ledger.decide(OWNER, p["proposal_id"], p["rejection_id"], "reject"),
    )
    assert counts(ledger)[LedgerReceipt.__tablename__] == 1


@pytest.mark.parametrize("family", ["payments", "expenses", "tasks"])
def test_concurrent_browser_telegram_approval_once(ledger, family):
    values = data(family, **business(ledger)) if family == "payments" else data(family)
    p = propose(ledger, family, values)
    gate = Barrier(2)

    def run(principal):
        gate.wait(timeout=5)
        return approve(ledger, p, principal)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(run, [OWNER, TELEGRAM]))
    assert replies[0]["receipt"] == replies[1]["receipt"]
    with Session(ledger.engine) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(MODELS[family])
                .where(MODELS[family].workspace_id == ledger.binding.workspace_id)
            )
            == 1
        )


def test_promotion_preserves_identity_and_commits_lead_client_atomically(ledger, monkeypatch):
    lead = create(ledger, "leads", phone="0521234567", email="test@example.invalid")
    p = ledger.propose(
        OWNER, str(uuid4()), "promote_lead", {}, record_id=lead["lead_id"], expected_revision=1
    )
    before = ledger.snapshot(OWNER)
    original = ledger._insert_outbox

    def fail(*args):
        raise RuntimeError("synthetic-before-outbox")

    monkeypatch.setattr(ledger, "_insert_outbox", fail)
    with pytest.raises(RuntimeError):
        approve(ledger, p)
    assert ledger.snapshot(OWNER) == before
    with Session(ledger.engine) as s:
        assert s.get(Client, (ledger.binding.workspace_id, lead["person_id"])) is None
        assert (
            s.get(LedgerProposal, (ledger.binding.workspace_id, p["proposal_id"])).status
            == "pending"
        )
    monkeypatch.setattr(ledger, "_insert_outbox", original)
    result = approve(ledger, p)
    assert len(result["records"]) == 2
    client = next(r["record"] for r in result["records"] if r["entity"] == "clients")
    assert client["person_id"] == lead["person_id"]
    assert client["name"] == lead["name"]
    assert result["receipt"]["commit_version"] == before["observed_version"] + 1


def test_competing_refunds_cannot_overdraw_original(ledger):
    refs = business(ledger)
    original = create(ledger, "payments", **refs)
    values = data(
        "payments",
        **refs,
        kind=enum("payment_kind", 1),
        original_payment_id=original["payment_id"],
        actual_amount_ils=6000,
    )
    proposals = [propose(ledger, "payments", values) for _ in range(2)]
    gate = Barrier(2)

    def run(p):
        gate.wait(timeout=5)
        return approve(ledger, p)["receipt"]["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(run, proposals))
    assert sorted(statuses) == ["committed", "target_changed"]
    assert ledger.snapshot(OWNER)["record_counts"]["payments"] == 2


def test_competing_duplicate_sources_and_contacts_are_fenced(ledger):
    for entity, values in [
        ("expenses", data("expenses", source_key="same-source")),
        ("leads", data("leads", phone="0521234567")),
    ]:
        p1, p2 = propose(ledger, entity, values), propose(ledger, entity, values)
        assert approve(ledger, p1)["receipt"]["status"] == "committed"
        assert approve(ledger, p2)["receipt"]["status"] == "target_changed"


def test_edit_original_payment_cannot_invalidate_existing_refunds(ledger):
    refs = business(ledger)
    original = create(ledger, "payments", **refs)
    create(
        ledger,
        "payments",
        **refs,
        kind=enum("payment_kind", 1),
        original_payment_id=original["payment_id"],
        actual_amount_ils=6000,
    )
    code(
        "refund_exceeds_original",
        lambda: propose(
            ledger,
            "payments",
            {"actual_amount_ils": 5000},
            record_id=original["payment_id"],
            expected_revision=1,
        ),
    )
    assert ledger.snapshot(OWNER)["record_counts"]["payments"] == 2


def test_changed_dependency_prevents_pending_payment_commit(ledger):
    refs = business(ledger)
    p = propose(ledger, "payments", data("payments", **refs))
    create_result = approve(
        ledger,
        propose(
            ledger,
            "works",
            {"title": "updated title"},
            record_id=refs["work_id"],
            expected_revision=1,
        ),
    )
    assert create_result["receipt"]["status"] == "committed"
    assert approve(ledger, p)["receipt"]["status"] == "target_changed"


def test_unrelated_commit_does_not_expire_exact_dependencies(ledger):
    p = propose(ledger, "tasks")
    create(ledger, "expenses")
    assert approve(ledger, p)["receipt"]["status"] == "committed"


def test_lost_post_commit_response_recovers_same_receipt_without_reapply(ledger, monkeypatch):
    refs = business(ledger)
    p = propose(ledger, "payments", data("payments", **refs))
    read = ledger.receipt

    def lost(*args):
        raise ConnectionError("synthetic-post-commit-response-loss")

    monkeypatch.setattr(ledger, "receipt", lost)
    with pytest.raises(ConnectionError):
        approve(ledger, p)
    committed_counts = counts(ledger)
    monkeypatch.setattr(ledger, "receipt", read)
    recovered = read(OWNER, p["approval_id"])
    assert recovered["receipt"]["status"] == "committed"
    assert approve(ledger, p) == recovered
    assert counts(ledger) == committed_counts
    assert read(OWNER, str(uuid4()))["status"] == "unknown"


def test_expiry_outer_deadline_and_owner_workspace_epoch_boundaries(ledger):
    p = propose(ledger, "tasks", approval_expires_at=NOW + timedelta(seconds=30))
    code("owner_approval_required", lambda: approve(ledger, p, MODEL))
    code("owner_approval_required", lambda: approve(ledger, p, LocalPrincipal("99", "OS")))
    ledger.clock = lambda: NOW + timedelta(seconds=30)
    assert approve(ledger, p)["receipt"]["status"] == "expired"
    other = LocalLedgerPrimary(
        ledger.engine, LocalBinding("other_" + uuid4().hex, "12345"), enabled=True
    )
    other.initialize_local_schema()
    code("proposal_not_found", lambda: approve(other, p))
    assert other.receipt(OWNER, p["approval_id"])["status"] == "unknown"
    with Session(ledger.engine) as s, s.begin():
        s.execute(
            update(LedgerWorkspace)
            .where(LedgerWorkspace.workspace_id == ledger.binding.workspace_id)
            .values(epoch=2)
        )
    code("authority_changed", lambda: ledger.snapshot(OWNER))


def test_tampered_envelope_and_committed_record_fail_verification(ledger):
    p = propose(ledger, "tasks")
    with Session(ledger.engine) as s, s.begin():
        row = s.get(LedgerProposal, (ledger.binding.workspace_id, p["proposal_id"]))
        row.mutations = [
            row.mutations[0] | {"after": row.mutations[0]["after"] | {"title": "tampered"}}
        ]
    code("proposal_integrity_failed", lambda: approve(ledger, p))


def test_references_and_money_fail_before_proposal_or_effect(ledger):
    code(
        "client_not_found",
        lambda: propose(ledger, "works", data("works", person_id="OLD_imported_person")),
    )
    code(
        "invalid_actual_amount_ils",
        lambda: propose(
            ledger,
            "payments",
            data("payments", person_id="PER_fake", work_id="WOR_fake", actual_amount_ils=1.5),
        ),
    )
    assert all(v == 0 for v in counts(ledger).values())


def test_pagination_and_snapshot_versions_are_explicit(ledger):
    for index in range(3):
        create(ledger, "tasks", title=str(index))
    first = ledger.snapshot(OWNER, limit=2)
    assert len(first["records"]["tasks"]) == 2
    cursor = first["next_cursor"]["tasks"]
    second = ledger.snapshot(OWNER, limit=2, after={"tasks": cursor})
    assert len(second["records"]["tasks"]) == 1
    assert second["observed_version"] == first["observed_version"] == 3
    code("invalid_page_limit", lambda: ledger.snapshot(OWNER, limit=True))


def test_foreign_key_constraints_bind_workspace_even_bypassing_service(ledger):
    from sqlalchemy.exc import IntegrityError

    refs = business(ledger)
    other = LocalLedgerPrimary(
        ledger.engine, LocalBinding("other_" + uuid4().hex, "12345"), enabled=True
    )
    other.initialize_local_schema()
    with pytest.raises(IntegrityError), Session(ledger.engine) as s, s.begin():
        s.add(
            Payment(
                workspace_id=other.binding.workspace_id,
                record_id="PAY_cross_workspace",
                revision=1,
                person_id=refs["person_id"],
                work_id=refs["work_id"],
                amount_minor=100,
                inputs=data("payments", **refs),
                source="OS",
                created_at=NOW,
                updated_at=NOW,
            )
        )


def ledger_state(ledger):
    """Every durable value a read could disturb, not only row counts."""
    ws = ledger.binding.workspace_id
    with Session(ledger.engine) as s:
        workspace = s.get(LedgerWorkspace, ws)
        return {
            "counts": counts(ledger),
            "workspace": (
                workspace.owner_id,
                workspace.epoch,
                workspace.version,
                workspace.summaries,
            ),
            "proposals": sorted(
                (r.proposal_id, r.request_id, r.status, r.expires_at, r.envelope_digest)
                for r in s.scalars(select(LedgerProposal).where(LedgerProposal.workspace_id == ws))
            ),
            "receipts": sorted(
                (r.request_id, r.proposal_id, r.status, r.version, r.epoch)
                for r in s.scalars(select(LedgerReceipt).where(LedgerReceipt.workspace_id == ws))
            ),
            "outbox": sorted(
                (r.event_id, r.version, r.status)
                for r in s.scalars(select(LedgerOutbox).where(LedgerOutbox.workspace_id == ws))
            ),
        }


def lookup(ledger, request_id, principal=OWNER):
    before = ledger_state(ledger)
    result = ledger.creation_request(principal, request_id)
    assert ledger_state(ledger) == before
    return result


def test_creation_request_recovers_lost_creation_ack_without_second_proposal(ledger, monkeypatch):
    request = str(uuid4())
    original = ledger.propose

    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("synthetic-creation-ack-loss")

    monkeypatch.setattr(ledger, "propose", lost_ack)
    with pytest.raises(ConnectionError):
        ledger.propose(OWNER, request, "tasks", data("tasks"))
    monkeypatch.setattr(ledger, "propose", original)
    proposals = ledger_state(ledger)["proposals"]
    assert len(proposals) == 1

    found = lookup(ledger, request)
    proposal = found["proposal"]
    assert found["primary_source"] == "postgresql"
    assert found["workspace_id"] == ledger.binding.workspace_id
    assert found["authority_epoch"] == 1
    assert found["verified_primary"] is True
    assert found["observed_version"] == 0
    assert found["past_expiry"] is False
    assert found["decision"] is None
    assert proposal["request_id"] == request
    assert proposal["status"] == "pending"
    assert proposal["proposal_id"] == proposals[0][0]
    # An exact replay returns the identical immutable envelope and decision IDs.
    assert ledger.propose(OWNER, request, "tasks", data("tasks")) == proposal
    assert ledger_state(ledger)["proposals"] == proposals
    assert approve(ledger, proposal)["receipt"]["status"] == "committed"


@pytest.mark.parametrize(
    ("status", "decision"),
    [
        ("committed", "approve"),
        ("rejected", "reject"),
        ("expired", "approve"),
        ("expired", "reject"),
        ("target_changed", "approve"),
    ],
)
def test_creation_request_reports_terminal_status_and_exact_decision(ledger, status, decision):
    if status == "target_changed":
        task = create(ledger, "tasks")
        edit = {"record_id": task["task_id"], "expected_revision": 1}
        p = propose(ledger, "tasks", data("tasks", title="first edit"), **edit)
        approve(ledger, propose(ledger, "tasks", data("tasks", title="competing edit"), **edit))
    else:
        p = propose(ledger, "tasks", approval_expires_at=NOW + timedelta(seconds=30))
    if status == "expired":
        ledger.clock = lambda: NOW + timedelta(seconds=30)
    decision_id = p["approval_id"] if decision == "approve" else p["rejection_id"]
    receipt = ledger.decide(OWNER, p["proposal_id"], decision_id, decision)
    assert receipt["receipt"]["status"] == status

    found = lookup(ledger, p["request_id"])
    assert found["proposal"] == p | {"status": status}
    assert found["decision"] == {"request_id": decision_id, "status": status}
    assert found["observed_version"] == receipt["observed_version"]
    # The pointer is only a pointer: verified truth still comes from the receipt read.
    assert ledger.receipt(OWNER, found["decision"]["request_id"]) == receipt


def test_pending_past_expiry_is_reported_but_never_expired_by_a_read(ledger):
    p = propose(ledger, "tasks", approval_expires_at=NOW + timedelta(seconds=30))
    ledger.clock = lambda: NOW + timedelta(seconds=30)
    found = lookup(ledger, p["request_id"])
    assert found["proposal"]["status"] == "pending"
    assert found["past_expiry"] is True
    assert found["decision"] is None
    # Only an explicit decision may record expiry.
    assert approve(ledger, p)["receipt"]["status"] == "expired"


def test_unknown_creation_request_stays_unknown(ledger):
    p = propose(ledger, "tasks")
    unknown = str(uuid4())
    assert lookup(ledger, unknown) == {
        "status": "unknown",
        "request_id": unknown,
        "verified_primary": False,
    }
    # Decision IDs are not creation request IDs.
    assert lookup(ledger, p["approval_id"])["status"] == "unknown"
    other = LocalLedgerPrimary(
        ledger.engine,
        LocalBinding("other_" + uuid4().hex, "12345"),
        enabled=True,
        clock=lambda: NOW,
    )
    other.initialize_local_schema()
    assert lookup(other, p["request_id"])["status"] == "unknown"


def test_creation_request_refuses_malformed_input_and_principals_before_checkout(ledger):
    from sqlalchemy import event

    checkouts = []

    def checkout(*args):
        checkouts.append(1)

    event.listen(ledger.engine, "checkout", checkout)
    try:
        for bad in ("not-a-uuid", str(uuid4()).upper(), None, 7, ""):
            code("invalid_request_id", lambda bad=bad: ledger.creation_request(OWNER, bad))
        for principal in (None, {}, LocalPrincipal("99", "OS"), LocalPrincipal(12345, "OS")):
            code(
                "owner_required",
                lambda principal=principal: ledger.creation_request(principal, str(uuid4())),
            )
        disabled = LocalLedgerPrimary(ledger.engine, ledger.binding)
        code("prototype_disabled", lambda: disabled.creation_request(OWNER, str(uuid4())))
    finally:
        event.remove(ledger.engine, "checkout", checkout)
    assert checkouts == []


def test_creation_request_is_readable_from_every_owner_channel(ledger):
    p = propose(ledger, "tasks")
    for principal in (OWNER, MODEL, TELEGRAM):
        found = lookup(ledger, p["request_id"], principal)
        assert found["proposal"]["proposal_id"] == p["proposal_id"]


def test_creation_request_fails_closed_on_owner_epoch_envelope_or_status_tamper(ledger):
    p = propose(ledger, "tasks")
    ws = ledger.binding.workspace_id
    for column, value in (("owner_id", "99"), ("epoch", 2), ("approval_id", str(uuid4()))):
        with Session(ledger.engine) as s, s.begin():
            row = s.get(LedgerProposal, (ws, p["proposal_id"]))
            original = getattr(row, column)
            setattr(row, column, value)
        code(
            "primary_verification_failed",
            lambda: ledger.creation_request(OWNER, p["request_id"]),
        )
        with Session(ledger.engine) as s, s.begin():
            setattr(s.get(LedgerProposal, (ws, p["proposal_id"])), column, original)
    assert lookup(ledger, p["request_id"])["proposal"] == p
    with Session(ledger.engine) as s, s.begin():
        s.get(LedgerProposal, (ws, p["proposal_id"])).status = "committed"
    # A terminal status without its exact receipt is not believable.
    code("primary_verification_failed", lambda: ledger.creation_request(OWNER, p["request_id"]))
    with Session(ledger.engine) as s, s.begin():
        s.execute(
            update(LedgerWorkspace).where(LedgerWorkspace.workspace_id == ws).values(epoch=2)
        )
    code("authority_changed", lambda: ledger.creation_request(OWNER, p["request_id"]))


def test_concurrent_lookup_sees_one_snapshot_and_never_blocks_decision(ledger, monkeypatch):
    import threading

    p = propose(ledger, "tasks")
    read_workspace = ledger._read_workspace
    snapshot_taken, decided = threading.Event(), threading.Event()
    lookup_thread = {}

    def paused(s):
        result = read_workspace(s)
        if threading.get_ident() == lookup_thread.get("id"):
            snapshot_taken.set()
            assert decided.wait(timeout=20)
        return result

    monkeypatch.setattr(ledger, "_read_workspace", paused)

    def run_lookup():
        lookup_thread["id"] = threading.get_ident()
        return ledger.creation_request(OWNER, p["request_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(run_lookup)
        assert snapshot_taken.wait(timeout=20)
        # The open read-only snapshot holds no row locks, so the decision completes now.
        receipt = pool.submit(approve, ledger, p).result(timeout=20)
        decided.set()
        during = future.result(timeout=20)
    assert receipt["receipt"]["status"] == "committed"
    assert during["proposal"]["status"] == "pending"
    assert during["decision"] is None
    assert during["observed_version"] == 0
    after = ledger.creation_request(OWNER, p["request_id"])
    assert after["proposal"]["status"] == "committed"
    assert after["decision"] == {"request_id": p["approval_id"], "status": "committed"}
    assert after["observed_version"] == 1
