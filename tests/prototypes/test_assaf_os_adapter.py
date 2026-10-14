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
    error_code("adapter_disabled", lambda: adapter.creation_request(str(uuid4())))

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
    for bad in ("not-a-uuid", str(uuid4()).upper(), None, 7):
        error_code("invalid_request_id", lambda bad=bad: adapter.creation_request(bad))
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
    for method in ("snapshot", "propose", "decide", "receipt", "creation_request"):
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

        creation_request = decide

    decision_id = str(uuid4())
    adapter = LocalPrimaryAdapter(ExplodingLedger(), context, enabled=True)
    creation_id = str(uuid4())
    lookup_failure = error_code(
        "inconclusive", lambda: adapter.creation_request(creation_id)
    )
    assert lookup_failure.request_id == creation_id
    assert "database" not in str(lookup_failure)
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


def test_creation_request_recovers_lost_creation_ack_with_policy_closed(
    adapter_setup, monkeypatch
):
    adapter, ledger, state = adapter_setup
    request = str(uuid4())
    original_propose = ledger.propose

    def lost_ack(*args, **kwargs):
        original_propose(*args, **kwargs)
        raise ConnectionError("synthetic detail must not escape")

    monkeypatch.setattr(ledger, "propose", lost_ack)
    failure = error_code("inconclusive", lambda: adapter.propose(request, "tasks", task_input()))
    assert failure.request_id == request
    monkeypatch.setattr(ledger, "propose", original_propose)

    # Recovery is a read: it works while mutation policy and browser verification are closed.
    state["context"] = replace(
        state["context"], mutation_allowed=False, browser_mutation_verified=False
    )
    found = adapter.creation_request(request)
    assert found["api_version"] == API_VERSION
    assert found["primary_source"] == "postgresql"
    assert found["workspace_id"] == state["context"].workspace
    assert found["authority_epoch"] == 1
    assert found["status"] == "pending"
    assert found["request_id"] == request
    assert found["verified_primary"] is True
    assert found["verified_receipt"] is False
    assert found["observed_version"] == 0
    assert found["commit_version"] is None
    assert found["past_expiry"] is False
    assert found["decision"] is None
    assert found["mirror"] == {"status": "not_required", "separate": True}
    proposal = found["proposal"]
    assert proposal["request_id"] == request

    state["context"] = replace(
        state["context"], mutation_allowed=True, browser_mutation_verified=True
    )
    replay = adapter.propose(request, "tasks", task_input())
    assert {k: replay[k] for k in proposal} == proposal
    receipt = adapter.decide(
        proposal["proposal_id"], proposal["approval_id"], explicit_owner_decision=True
    )
    decided = adapter.creation_request(request)
    assert decided["status"] == "committed"
    assert decided["decision"] == {"request_id": proposal["approval_id"], "status": "committed"}
    assert adapter.receipt(decided["decision"]["request_id"]) == receipt


def test_creation_request_unknown_stays_unknown(adapter_setup):
    adapter, _, state = adapter_setup
    unknown = str(uuid4())
    result = adapter.creation_request(unknown)
    assert result == {
        "api_version": API_VERSION,
        "primary_source": "postgresql",
        "workspace_id": state["context"].workspace,
        "authority_epoch": 1,
        "status": "unknown",
        "request_id": unknown,
        "observed_version": None,
        "commit_version": None,
        "verified_primary": False,
        "verified_receipt": False,
        "mirror": {"status": "pending", "separate": True},
    }


def test_creation_request_rechecks_session_scope_and_authority_every_call(adapter_setup):
    adapter, _, state = adapter_setup
    request = str(uuid4())
    adapter.propose(request, "tasks", task_input())
    calls = state["calls"]
    assert adapter.creation_request(request)["status"] == "pending"
    assert state["calls"] == calls + 1
    good = state["context"]
    expired = datetime.now(UTC) - timedelta(seconds=1)
    refusals = [
        ("authentication_expired", replace(good, auth_expires_at=expired)),
        ("scope_denied", replace(good, scopes=frozenset({"assaf-os:propose"}))),
        ("authority_changed", replace(good, numeric_owner=99)),
        ("authority_changed", replace(good, workspace="other_workspace")),
        ("authority_changed", replace(good, authority_epoch=2)),
    ]
    for expected, bad in refusals:
        state["context"] = bad
        error_code(expected, lambda: adapter.creation_request(request))
    state["context"] = good
    assert adapter.creation_request(request)["status"] == "pending"


def _creation_response(status="pending", decision=None, past_expiry=False, **proposal):
    approval_id, rejection_id = proposal.pop("ids", (APPROVAL, REJECTION))
    return {
        "primary_source": "postgresql",
        "workspace_id": "synthetic_workspace",
        "authority_epoch": 1,
        "verified_primary": True,
        "observed_version": 3,
        "past_expiry": past_expiry,
        "decision": decision,
        "proposal": {
            "workspace_id": "synthetic_workspace",
            "proposal_id": "PRO_" + "a" * 32,
            "request_id": FAKE_REQUEST,
            "owner_id": str(OWNER),
            "epoch": 1,
            "approval_id": approval_id,
            "rejection_id": rejection_id,
            "status": status,
        }
        | proposal,
    }


FAKE_REQUEST = str(uuid4())
APPROVAL = "1" * 8 + "-1111-4111-8111-" + "1" * 12
REJECTION = "2" * 8 + "-2222-4222-8222-" + "2" * 12


class ScriptedLedger:
    binding = SimpleNamespace(
        workspace_id="synthetic_workspace", owner_id=str(OWNER), authority_epoch=1
    )

    def __init__(self, response):
        self.response = response

    def creation_request(self, principal, request_id):
        return self.response


def test_creation_result_accepts_each_exact_status_decision_pair():
    pairs = [
        ("committed", APPROVAL),
        ("target_changed", APPROVAL),
        ("rejected", REJECTION),
        ("expired", APPROVAL),
        ("expired", REJECTION),
    ]
    for status, decision_id in pairs:
        response = _creation_response(
            status, {"request_id": decision_id, "status": status}, past_expiry=None
        )
        adapter = LocalPrimaryAdapter(ScriptedLedger(response), context, enabled=True)
        assert adapter.creation_request(FAKE_REQUEST)["decision"]["request_id"] == decision_id
    adapter = LocalPrimaryAdapter(ScriptedLedger(_creation_response()), context, enabled=True)
    assert adapter.creation_request(FAKE_REQUEST)["status"] == "pending"


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            _creation_response(
                "committed", {"request_id": REJECTION, "status": "committed"}, past_expiry=None
            ),
            id="committed-under-rejection-id",
        ),
        pytest.param(
            _creation_response(
                "rejected", {"request_id": APPROVAL, "status": "rejected"}, past_expiry=None
            ),
            id="rejected-under-approval-id",
        ),
        pytest.param(
            _creation_response(
                "target_changed", {"request_id": REJECTION, "status": "target_changed"},
                past_expiry=None,
            ),
            id="target-changed-under-rejection-id",
        ),
        pytest.param(
            _creation_response("pending", {"request_id": APPROVAL, "status": "committed"}),
            id="pending-with-decision",
        ),
        pytest.param(
            _creation_response("committed", None, past_expiry=None), id="terminal-no-decision"
        ),
        pytest.param(
            _creation_response(
                "committed", {"request_id": APPROVAL, "status": "rejected"}, past_expiry=None
            ),
            id="decision-status-disagrees",
        ),
        pytest.param(
            _creation_response(
                "committed", {"request_id": APPROVAL, "status": "committed"}, past_expiry=True
            ),
            id="terminal-with-expiry-flag",
        ),
        pytest.param(_creation_response(past_expiry=None), id="pending-without-expiry-flag"),
        pytest.param(_creation_response(workspace_id="other_workspace"), id="foreign-workspace"),
        pytest.param(_creation_response(owner_id="99"), id="wrong-owner"),
        pytest.param(_creation_response(epoch=2), id="wrong-epoch"),
        pytest.param(_creation_response(request_id=str(uuid4())), id="other-request"),
        pytest.param(_creation_response(ids=(APPROVAL, APPROVAL)), id="same-decision-ids"),
        pytest.param(_creation_response(status="consumed"), id="unknown-status"),
    ],
)
def test_creation_result_rejects_inconsistent_primary_responses(response):
    adapter = LocalPrimaryAdapter(ScriptedLedger(response), context, enabled=True)
    failure = error_code("inconclusive", lambda: adapter.creation_request(FAKE_REQUEST))
    assert failure.request_id == FAKE_REQUEST


def test_creation_request_tamper_is_inconclusive_not_unknown(adapter_setup):
    from app.prototypes.assaf_os_ledger import LedgerProposal
    from sqlalchemy.orm import Session

    adapter, ledger, _ = adapter_setup
    request = str(uuid4())
    proposal = adapter.propose(request, "tasks", task_input())
    with Session(ledger.engine) as s, s.begin():
        s.get(LedgerProposal, (ledger.binding.workspace_id, proposal["proposal_id"])).epoch = 2
    failure = error_code("inconclusive", lambda: adapter.creation_request(request))
    assert failure.request_id == request
