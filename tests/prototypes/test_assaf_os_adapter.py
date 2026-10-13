"""Bounded adapter tests; all values and PostgreSQL workspaces are synthetic."""

import inspect
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.prototypes.assaf_os_adapter import (
    API_VERSION,
    AdapterError,
    LocalPrimaryAdapter,
    TrustedOwnerContext,
)
from app.prototypes.assaf_os_domain import SCHEMA
from app.prototypes.assaf_os_ledger import LocalLedgerPrimary
from app.prototypes.assaf_os_primary import LocalBinding
from sqlalchemy import create_engine

OWNER = 12345
NOW = datetime(2026, 10, 14, 0, tzinfo=UTC)
ALL_SCOPES = frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve"})


def context(workspace="synthetic_workspace", **changes):
    value = TrustedOwnerContext(
        numeric_owner=OWNER,
        workspace=workspace,
        authority_epoch=1,
        channel="OS",
        scopes=ALL_SCOPES,
        auth_expires_at=datetime.now(UTC) + timedelta(hours=1),
        mutation_allowed=True,
        browser_mutation_verified=True,
    )
    return replace(value, **changes)


def task_input(title="Synthetic adapter task"):
    return {"title": title, "status": SCHEMA["enums"]["task_status"][0]}


def error_code(expected, operation, *, decision_id=None):
    with pytest.raises(AdapterError) as caught:
        operation()
    assert caught.value.code == expected
    assert caught.value.decision_id == decision_id
    return caught.value


class NoCallLedger:
    def __init__(self):
        self.binding = LocalBinding("synthetic_workspace", str(OWNER), 1)
        self.calls = []

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError("ledger must not be called")

        return unexpected


def test_default_off_refuses_before_authorize_or_ledger_call():
    ledger = NoCallLedger()
    authorizations = []
    adapter = LocalPrimaryAdapter(ledger, lambda: authorizations.append(1) or context())

    error_code("adapter_disabled", adapter.contract)
    error_code("adapter_disabled", adapter.snapshot)
    error_code(
        "adapter_disabled",
        lambda: adapter.propose(str(uuid4()), "tasks", task_input()),
    )
    error_code("adapter_disabled", lambda: adapter.receipt(str(uuid4())))

    assert authorizations == []
    assert ledger.calls == []


def test_underlying_ledger_default_off_guard_is_retained():
    pg = create_engine("postgresql+psycopg://127.0.0.1:1/assaf_os_local_unreachable")
    ledger = LocalLedgerPrimary(pg, LocalBinding("synthetic_workspace", str(OWNER)))
    adapter = LocalPrimaryAdapter(ledger, context, enabled=True)
    try:
        error_code("prototype_disabled", adapter.snapshot)
    finally:
        pg.dispose()


@pytest.mark.parametrize(
    "bad_context",
    [
        None,
        {},
        context(numeric_owner=True),
        context(numeric_owner=0),
        context(workspace="bad workspace"),
        context(authority_epoch=True),
        context(channel="BROWSER"),
        context(scopes={"assaf-os:read"}),
        context(scopes=frozenset({"assaf-os:read", "unknown"})),
        context(auth_expires_at=datetime(2026, 10, 14)),
        context(mutation_allowed=1),
        context(browser_mutation_verified=1),
    ],
)
def test_malformed_trusted_context_never_calls_ledger(bad_context):
    ledger = NoCallLedger()
    adapter = LocalPrimaryAdapter(ledger, lambda: bad_context, enabled=True)
    error_code("invalid_trusted_context", adapter.snapshot)
    assert ledger.calls == []


def test_expired_callback_authority_mismatch_and_callback_failure_are_sanitized():
    ledger = NoCallLedger()
    expired = context(auth_expires_at=datetime.now(UTC) - timedelta(microseconds=1))
    error_code(
        "authentication_expired",
        LocalPrimaryAdapter(ledger, lambda: expired, enabled=True).snapshot,
    )
    error_code(
        "authority_changed",
        LocalPrimaryAdapter(
            ledger, lambda: context(workspace="other_workspace"), enabled=True
        ).snapshot,
    )

    def unavailable():
        raise RuntimeError("secret callback detail")

    error_code(
        "authorization_unavailable",
        LocalPrimaryAdapter(ledger, unavailable, enabled=True).snapshot,
    )
    assert ledger.calls == []


def test_public_malformed_values_fail_before_authorization_or_checkout():
    ledger = NoCallLedger()
    authorizations = []
    adapter = LocalPrimaryAdapter(
        ledger, lambda: authorizations.append(1) or context(), enabled=True
    )
    error_code("invalid_page_limit", lambda: adapter.snapshot(limit=True))
    error_code("invalid_page_cursor", lambda: adapter.snapshot(after={"tasks": "!"}))
    error_code("invalid_request_id", lambda: adapter.receipt("not-a-uuid"))
    error_code(
        "invalid_input_shape",
        lambda: adapter.propose(str(uuid4()), "tasks", []),
    )
    error_code(
        "invalid_proposal_id",
        lambda: adapter.decide("PRO_bad", str(uuid4()), explicit_owner_decision=True),
    )
    assert authorizations == []
    assert ledger.calls == []


def test_context_fields_are_not_public_operation_arguments():
    prohibited = {
        "numeric_owner",
        "owner_id",
        "workspace",
        "workspace_id",
        "authority_epoch",
        "channel",
        "source",
        "scopes",
        "mutation_allowed",
        "browser_mutation_verified",
    }
    for method in ("snapshot", "propose", "decide", "receipt"):
        parameters = inspect.signature(getattr(LocalPrimaryAdapter, method)).parameters
        assert prohibited.isdisjoint(parameters)


@pytest.fixture(scope="module")
def engine():
    url = os.getenv("MIA_OS_LOCAL_PROTOTYPE_URL", "")
    if not url:
        pytest.skip("explicit disposable MIA_OS_LOCAL_PROTOTYPE_URL required")
    pg = create_engine(url, pool_size=4, max_overflow=0)
    LocalLedgerPrimary(
        pg, LocalBinding("adapter_guard", str(OWNER)), enabled=True
    )._guard()
    yield pg
    pg.dispose()


@pytest.fixture
def adapter_setup(engine):
    workspace = "adapter_" + uuid4().hex
    ledger = LocalLedgerPrimary(
        engine,
        LocalBinding(workspace, str(OWNER)),
        enabled=True,
        clock=lambda: NOW,
    )
    ledger.initialize_local_schema()
    state = {"context": context(workspace), "calls": 0}

    def authorize():
        state["calls"] += 1
        return state["context"]

    return LocalPrimaryAdapter(ledger, authorize, enabled=True), ledger, state


def test_contract_and_snapshot_are_honest_versioned_primary_reads(adapter_setup):
    adapter, _, state = adapter_setup
    contract = adapter.contract()
    snapshot = adapter.snapshot()
    assert state["calls"] == 2
    assert contract == {
        "api_version": API_VERSION,
        "primary_source": "postgresql",
        "workspace_id": state["context"].workspace,
        "authority_epoch": 1,
        "enabled": True,
        "context_trust": "synthetic_server_callback_only_not_authentication",
        "approval": "explicit_owner_decision_only",
        "mirror": {"status": "pending", "separate": True},
    }
    assert snapshot["api_version"] == API_VERSION
    assert snapshot["primary_source"] == "postgresql"
    assert snapshot["observed_version"] == 0
    assert snapshot["commit_version"] is None
    assert snapshot["verified_primary"] is True
    assert snapshot["verified_receipt"] is False
    assert snapshot["mirror"] == {"status": "pending", "separate": True}


def test_policy_scope_and_browser_checks_reapply_on_every_mutation(adapter_setup):
    adapter, _, state = adapter_setup
    request = str(uuid4())
    state["context"] = replace(state["context"], mutation_allowed=False)
    error_code(
        "mutation_disabled",
        lambda: adapter.propose(request, "tasks", task_input()),
    )
    # Policy-off still permits authorized reads and exact receipt recovery.
    assert adapter.snapshot()["verified_primary"] is True
    assert adapter.receipt(str(uuid4()))["status"] == "unknown"

    state["context"] = replace(
        state["context"], mutation_allowed=True, browser_mutation_verified=False
    )
    error_code(
        "browser_mutation_verification_required",
        lambda: adapter.propose(request, "tasks", task_input()),
    )
    state["context"] = replace(
        state["context"], browser_mutation_verified=True, scopes=frozenset({"assaf-os:read"})
    )
    error_code(
        "scope_denied",
        lambda: adapter.propose(request, "tasks", task_input()),
    )


def test_mia_can_propose_but_cannot_decide_and_telegram_can_decide(adapter_setup):
    adapter, _, state = adapter_setup
    state["context"] = replace(
        state["context"], channel="MIA", browser_mutation_verified=False
    )
    request = str(uuid4())
    proposal = adapter.propose(request, "tasks", task_input())
    assert proposal["source"] == "MIA"
    assert proposal["primary_source"] == "postgresql"
    assert proposal["verified_primary"] is False
    assert proposal["verified_receipt"] is False
    assert proposal["observed_version"] is None
    error_code(
        "owner_approval_required",
        lambda: adapter.decide(
            proposal["proposal_id"],
            proposal["approval_id"],
            explicit_owner_decision=True,
        ),
    )

    state["context"] = replace(state["context"], channel="TELEGRAM_APPROVAL")
    receipt = adapter.decide(
        proposal["proposal_id"],
        proposal["approval_id"],
        explicit_owner_decision=True,
    )
    assert receipt["verified_receipt"] is True
    assert receipt["receipt"]["status"] == "committed"
    assert receipt["commit_version"] == 1
    assert receipt["observed_version"] >= receipt["commit_version"]
    assert receipt["mirror"] == {"status": "pending", "separate": True}


def test_decision_requires_exact_explicit_owner_boolean(adapter_setup):
    adapter, _, _ = adapter_setup
    proposal = adapter.propose(str(uuid4()), "tasks", task_input())
    for explicit in (False, 1, None):
        error_code(
            "explicit_owner_decision_required",
            lambda explicit=explicit: adapter.decide(
                proposal["proposal_id"],
                proposal["approval_id"],
                explicit_owner_decision=explicit,
            ),
        )


def test_idempotent_proposal_replay_and_payload_mismatch(adapter_setup):
    adapter, _, _ = adapter_setup
    request = str(uuid4())
    first = adapter.propose(request, "tasks", task_input())
    assert adapter.propose(request, "tasks", task_input()) == first
    error_code(
        "idempotency_mismatch",
        lambda: adapter.propose(request, "tasks", task_input("changed")),
    )


def test_expired_outer_deadline_allows_exact_replay_but_refuses_new_request(adapter_setup):
    adapter, ledger, state = adapter_setup
    # The synthetic ledger clock starts before this already wall-clock-expired deadline.
    deadline = datetime.now(UTC) - timedelta(minutes=1)
    ledger.clock = lambda: deadline - timedelta(minutes=1)
    request = str(uuid4())
    first = adapter.propose(
        request,
        "tasks",
        task_input(),
        approval_expires_at=deadline,
    )

    ledger.clock = lambda: deadline + timedelta(minutes=1)
    assert (
        adapter.propose(
            request,
            "tasks",
            task_input(),
            approval_expires_at=deadline,
        )
        == first
    )
    error_code(
        "invalid_approval_expiry",
        lambda: adapter.propose(
            str(uuid4()),
            "tasks",
            task_input(),
            approval_expires_at=deadline,
        ),
    )
    assert state["calls"] == 3


def test_lost_commit_ack_is_inconclusive_until_same_decision_receipt(adapter_setup, monkeypatch):
    adapter, ledger, state = adapter_setup
    proposal = adapter.propose(str(uuid4()), "tasks", task_input())
    original_receipt = ledger.receipt

    def lost_ack(*args, **kwargs):
        raise ConnectionError("synthetic detail must not escape")

    monkeypatch.setattr(ledger, "receipt", lost_ack)
    failure = error_code(
        "inconclusive",
        lambda: adapter.decide(
            proposal["proposal_id"],
            proposal["approval_id"],
            explicit_owner_decision=True,
        ),
        decision_id=proposal["approval_id"],
    )
    assert failure.request_id is None
    assert str(failure) == "inconclusive"

    monkeypatch.setattr(ledger, "receipt", original_receipt)
    recovered = adapter.receipt(proposal["approval_id"])
    assert recovered["verified_receipt"] is True
    assert recovered["receipt"]["request_id"] == proposal["approval_id"]
    assert recovered["receipt"]["status"] == "committed"
    assert recovered["commit_version"] == 1
    assert state["calls"] == 3


def test_authority_callback_is_rechecked_before_receipt_recovery(adapter_setup):
    adapter, _, state = adapter_setup
    unknown_id = str(uuid4())
    assert adapter.receipt(unknown_id)["status"] == "unknown"
    state["context"] = replace(
        state["context"], auth_expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    error_code("authentication_expired", lambda: adapter.receipt(unknown_id))


def test_unknown_decision_outcomes_never_leak_exception_details():
    class ExplodingLedger:
        binding = SimpleNamespace(
            workspace_id="synthetic_workspace", owner_id=str(OWNER), authority_epoch=1
        )

        def decide(self, *args, **kwargs):
            raise RuntimeError("database host and secret diagnostic")

    decision_id = str(uuid4())
    adapter = LocalPrimaryAdapter(ExplodingLedger(), context, enabled=True)
    failure = error_code(
        "inconclusive",
        lambda: adapter.decide(
            "PRO_" + "a" * 32,
            decision_id,
            explicit_owner_decision=True,
        ),
        decision_id=decision_id,
    )
    assert "database" not in str(failure)
