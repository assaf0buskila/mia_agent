"""A tool call reports what actually happened, not what reads nicely.

Two ways Mia used to lie to her own telemetry: a tool that ran out of time came back
`ok=True` because the owner-facing copy said "still checking", and a CRM read that
lost the Activity tab printed the same sentence as a genuinely empty tab. Both looked
healthy on a dashboard while an integration was down.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from uuid import uuid4

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.models import CrmOutboxRow
from app.db.session import get_session_factory, init_db, make_engine
from app.db.store import LeadStore
from app.graph.owner_agent import TOOL_DEADLINE_REPLY, _run_tool_with_timeout
from app.integrations.sheets import FakeSheetsPort
from app.services.crm_v2 import CONTACT_FIELDS, CrmService
from app.tools.registries.owner_tools import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    ToolContext,
    ToolResult,
    execute_tool,
)
from app.workers import crm_delivery
from app.workers.crm_delivery import CrmDeliveryWorker
from sqlalchemy.exc import DataError
from sqlalchemy.orm import sessionmaker


def _ctx(db, sheets=None) -> ToolContext:
    return ToolContext(
        principal=Principal.owner(source="telegram", actor_id="1"),
        store=LeadStore(db),
        brain=BrainStore(db),
        settings=Settings(_env_file=None),
        embedding_port=FakeEmbeddingPort(),
        sheets=sheets,
    )


class _CrmImportBrokenSheets(FakeSheetsPort):
    """Current CRM reads must fail closed when Contacts import is unavailable."""

    def read_crm_contacts_chunk(self, **_kwargs) -> list[list[str]]:
        raise RuntimeError("contacts import unavailable")


class _CurrentCrmSheets(FakeSheetsPort):
    def __init__(self) -> None:
        super().__init__()
        # Contacts A:N plus the stable Mia id in O, as imported by the v2 service.
        self.locked_contacts.append(
            ["Dana", "050-0000000"] + [""] * 12 + ["crm_outcome_truth_1"]
        )


def test_outcome_defaults_follow_ok() -> None:
    assert ToolResult(ok=True, text="x").outcome_label() == OUTCOME_SUCCESS
    assert ToolResult(ok=False, error="boom").outcome_label() == OUTCOME_FAILURE


def test_a_timeout_is_not_a_success() -> None:
    init_db()
    db = get_session_factory()()
    try:
        import app.graph.owner_agent as owner_agent

        finished = []

        def _hang(_name, _args, _ctx):
            import time

            time.sleep(0.1)
            finished.append(True)
            raise AssertionError("should have timed out")

        original = owner_agent.execute_tool
        original_timeout = owner_agent.TOOL_TIMEOUT_SECONDS
        owner_agent.execute_tool = _hang  # type: ignore[method-assign]
        owner_agent.TOOL_TIMEOUT_SECONDS = 0.01
        try:
            result = _run_tool_with_timeout("gmail_inbox", {}, _ctx(db))
        finally:
            owner_agent.execute_tool = original  # type: ignore[method-assign]
            owner_agent.TOOL_TIMEOUT_SECONDS = original_timeout

        # The owner still hears something honest and natural.
        assert finished == [True], "active work must drain before the DB closes"
        assert result.text == TOOL_DEADLINE_REPLY
        # But nothing counts it as a tool that worked.
        assert result.ok is False
        assert result.outcome_label() == OUTCOME_TIMEOUT
        payload = result.payload()
        assert payload["ok"] is False
        assert payload["outcome"] == OUTCOME_TIMEOUT
        # The copy survives into the model payload so the turn is not left blank.
        assert payload["result"] == TOOL_DEADLINE_REPLY
    finally:
        db.close()


def test_a_lost_crm_import_is_reported_as_failure() -> None:
    init_db()
    db = get_session_factory()()
    try:
        result = execute_tool(
            "crm_search", {"query": "Dana"}, _ctx(db, _CrmImportBrokenSheets())
        )
        assert result.ok is False
        assert result.outcome_label() == OUTCOME_FAILURE
        assert "stale target" in result.error
    finally:
        db.close()


def test_a_current_crm_import_is_a_clean_success() -> None:
    init_db()
    db = get_session_factory()()
    try:
        sheets = _CurrentCrmSheets()
        seeded = CrmService(db).capture(
            {"name": "Dana", "phone": "050-0000000"}, source_ref="seed:outcome-truth"
        )
        assert seeded.contact is not None
        sheets.locked_contacts[0] = [
            seeded.contact.fields.get(name, "") for name in CONTACT_FIELDS
        ] + [seeded.contact.id]
        db.commit()
        result = execute_tool("crm_search", {"query": "Dana"}, _ctx(db, sheets))
        assert result.ok is True
        assert result.outcome_label() == OUTCOME_SUCCESS
        assert "Dana" in result.text
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The CRM outbox tells the same truth about its own outcomes.
#
# "unknown" is the queue's way of saying "an external effect may already have
# happened and we cannot see it". Downgrading that to "failed" is the same class
# of lie as a timed-out tool reporting ok=True: it reads like a clean negative
# result, and it hands the job back to the delivery loop for a real re-send.
# ---------------------------------------------------------------------------


def _outbox_row(*, dedupe_key, payload_json, status, created_at, last_attempt_at=""):
    return CrmOutboxRow(
        id=uuid4().hex,
        dedupe_key=dedupe_key,
        aggregate_type="crm_contact",
        aggregate_id=uuid4().hex,
        destination="telegram",
        payload_json=payload_json,
        status=status,
        last_attempt_at=last_attempt_at,
        created_at=created_at,
    )


def _delivery_worker(tmp_path, name, handler):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    worker = CrmDeliveryWorker(
        session_factory=factory,
        sheets=FakeSheetsPort(),
        telegram_handler=handler,
        allowed_telegram_recipient_ids=frozenset({"999"}),
    )
    return engine, factory, worker


def _statuses(factory):
    with factory() as db:
        return {row.dedupe_key: row.status for row in db.query(CrmOutboxRow).all()}


def test_a_poison_unknown_job_does_not_stop_the_reconcile_cycle(tmp_path, caplog):
    """A row whose payload_json cannot be parsed must not wedge the queue.

    `_reconcile_unknown` runs before the delivery loop in `run_once`, so a single
    `json.JSONDecodeError` (a ValueError) escaping it aborts the entire cycle --
    every reconcile behind it and every pending delivery -- on every 5-second poll,
    permanently, because the poison row is never marked and so is re-read next time.
    """
    handled = []

    def handler(payload, reconcile_only):
        handled.append(payload["which"])
        return "confirmed"

    engine, factory, worker = _delivery_worker(tmp_path, "poison-reconcile.db", handler)
    try:
        with factory() as db:
            # Sorted first by (last_attempt_at, id): truncated mid-JSON, so
            # json.loads genuinely raises rather than returning a non-dict.
            db.add(_outbox_row(
                dedupe_key="telegram:poison",
                payload_json='{"recipient_id": "999", "which": "poi',
                status="unknown",
                last_attempt_at="2020-01-01T00:00:00+00:00",
                created_at="2020-01-01T00:00:00+00:00",
            ))
            db.add(_outbox_row(
                dedupe_key="telegram:healthy-unknown",
                payload_json=json.dumps(
                    {"recipient_id": "999", "which": "healthy-unknown"}
                ),
                status="unknown",
                last_attempt_at="2021-01-01T00:00:00+00:00",
                created_at="2021-01-01T00:00:00+00:00",
            ))
            db.add(_outbox_row(
                dedupe_key="telegram:pending",
                payload_json=json.dumps({"recipient_id": "999", "which": "pending"}),
                status="pending",
                created_at="2021-01-01T00:00:00+00:00",
            ))
            db.commit()

        with caplog.at_level(logging.WARNING, logger="app.workers.crm_delivery"):
            run = worker.run_once()  # must not raise

        assert run.confirmed == 2
        assert _statuses(factory) == {
            "telegram:poison": "failed",
            "telegram:healthy-unknown": "confirmed",
            "telegram:pending": "confirmed",
        }
        assert set(handled) == {"healthy-unknown", "pending"}
        assert any(
            "reason=reconcile_payload_invalid" in record.getMessage()
            for record in caplog.records
        ), "the guard must emit its own reason code"
    finally:
        engine.dispose()


def test_a_db_error_during_reconcile_leaves_the_job_unknown_not_failed(tmp_path, caplog):
    """The one mapping that differs from `_deliver`, and the reason for this chunk.

    `_deliver` maps SQLAlchemyError to "failed" so the job backs off and retries.
    `_reconcile_unknown` must not: `_claim_one` re-claims status in
    ("pending", "failed"), so "failed" hands a job whose external effect may already
    have landed back to the delivery loop for a REAL re-send -- and the DB error may
    have struck the very claim-check that prevents a duplicate owner ping. "We do not
    know" is the entire point of this queue.
    """

    def handler(payload, reconcile_only):
        raise DataError("select", {}, Exception("connection reset mid-readback"))

    engine, factory, worker = _delivery_worker(tmp_path, "reconcile-db-error.db", handler)
    try:
        with factory() as db:
            db.add(_outbox_row(
                dedupe_key="telegram:db-error",
                payload_json=json.dumps({"recipient_id": "999", "which": "db-error"}),
                status="unknown",
                last_attempt_at="2020-01-01T00:00:00+00:00",
                created_at="2020-01-01T00:00:00+00:00",
            ))
            db.commit()

        with caplog.at_level(logging.WARNING, logger="app.workers.crm_delivery"):
            run = worker.run_once()  # must not raise

        assert run.unknown == 1
        assert run.failed == 0
        assert run.claimed == 0, "an unknown job must never be re-claimed for a re-send"
        assert _statuses(factory) == {"telegram:db-error": "unknown"}
        assert any(
            "reason=reconcile_db_error" in record.getMessage()
            for record in caplog.records
        ), "the guard must emit its own reason code"
    finally:
        engine.dispose()


def test_a_permanently_unknown_job_does_not_starve_the_reconcile_queue(tmp_path):
    """`last_attempt_at` is the reconcile ordering column, and only `_claim_one` used
    to advance it. A job that reconciles back to "unknown" therefore kept its
    timestamp forever, so every poll re-read the same oldest MAX_BATCH rows and
    anything queued behind them was never reconciled at all.
    """

    def handler(payload, reconcile_only):
        # "stuck": a recipient claim exists and no confirmed receipt can be read
        # back, so the readback is genuinely inconclusive on every attempt.
        return "confirmed" if payload["which"] == "resolvable" else "unknown"

    engine, factory, worker = _delivery_worker(
        tmp_path, "reconcile-starvation.db", handler
    )
    try:
        with factory() as db:
            for index in range(25):
                db.add(_outbox_row(
                    dedupe_key=f"telegram:stuck-{index:02d}",
                    payload_json=json.dumps(
                        {"recipient_id": "999", "which": "stuck"}
                    ),
                    status="unknown",
                    last_attempt_at="2020-01-01T00:00:00+00:00",
                    created_at="2020-01-01T00:00:00+00:00",
                ))
            db.add(_outbox_row(
                dedupe_key="telegram:behind-them",
                payload_json=json.dumps(
                    {"recipient_id": "999", "which": "resolvable"}
                ),
                status="unknown",
                last_attempt_at="2020-06-01T00:00:00+00:00",
                created_at="2020-06-01T00:00:00+00:00",
            ))
            db.commit()

        for _ in range(3):
            worker.run_once()

        statuses = _statuses(factory)
        assert statuses["telegram:behind-them"] == "confirmed", (
            "the 26th job is starved behind 25 permanently unknown ones"
        )
        assert sum(value == "unknown" for value in statuses.values()) == 25
    finally:
        engine.dispose()


def _handled_exception_names(function: ast.FunctionDef, constants: dict) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        parts = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        for part in parts:
            if isinstance(part, ast.Name):
                names |= constants.get(part.id, {part.id})
    return names


def test_reconcile_never_handles_fewer_exception_classes_than_delivery() -> None:
    """Drift guard. The two boundaries diverged once already -- `_deliver` grew
    ValueError and SQLAlchemyError handling after a production incident and
    `_reconcile_unknown` did not. Read the handled classes off the source so an
    exception added to one side and not the other fails here, and off the shared
    module constants so the two lists cannot be edited apart.
    """
    module = ast.parse(Path(crm_delivery.__file__).read_text(encoding="utf-8"))
    constants: dict[str, set[str]] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = {
                        element.id
                        for element in node.value.elts
                        if isinstance(element, ast.Name)
                    }
    functions = {
        node.name: node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef)
    }
    delivered = _handled_exception_names(functions["_deliver"], constants)
    reconciled = _handled_exception_names(functions["_reconcile_unknown"], constants)

    assert {"SQLAlchemyError", "ValueError"} <= delivered
    assert constants, "the handled classes must live in shared constants, not literals"
    assert delivered <= reconciled, (
        "_reconcile_unknown must handle every exception class _deliver does: "
        f"missing {sorted(delivered - reconciled)}"
    )

    # Widening the boundary must not swallow `_finish`'s own invariant violation
    # ("CRM delivery lease ownership changed"), a RuntimeError that has to stop
    # loudly. It stays outside the guard only because the `_finish` call sits
    # outside the `try` in both methods -- assert that placement, since both
    # boundaries now catch RuntimeError.
    for name in ("_deliver", "_reconcile_unknown"):
        guarded = {
            node
            for try_node in ast.walk(functions[name])
            if isinstance(try_node, ast.Try)
            for statement in try_node.body
            for node in ast.walk(statement)
        }
        finishes = [
            node
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_finish"
        ]
        assert finishes, f"{name} must complete its job through _finish"
        assert not any(node in guarded for node in finishes), (
            f"{name} must call _finish outside its try, so a lease-ownership "
            "violation still raises"
        )
