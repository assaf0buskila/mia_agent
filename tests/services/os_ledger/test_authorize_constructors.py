"""V2-19 (V2_PG_CONTRACTS.md §9, I4-I6, I8, I12): the production trusted-context constructors.

Each constructor re-checks the numeric owner allow-list (never a username), takes the ledger
workspace and epoch from settings, and sets ``auth_expires_at`` from the session or credential.
The second half uses contexts minted here against the V2-16f FakeLedger: a wrong owner, workspace
or epoch is refused by the ledger too, and the kill switch blocks propose, decide and commit.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.api import owner_sessions
from app.api.owner_sessions import OwnerSessionPrincipal
from app.capabilities.types import Principal
from app.core.config import MiaEnv, Settings
from app.db.models import ApprovalRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED, DECISION_REJECTED
from app.services.os_ledger import api, authorize
from app.services.owner_actions import decide_owner_action, propose_owner_action

from tests.fakes.os_ledger_builders import TASK_OPEN, rid
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE, build_fake_fixture

ALL_OS = frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve", "assaf-os:direct"})
JOBS = ("import", "mirror", "verifier", "authority", "retention")
ASSISTANT_SCOPES = "merchant_rules:read own_proposals:read statement:propose"


def settings(**changes) -> Settings:
    values = dict(
        _env_file=None, env=MiaEnv.TEST, telegram_owner_user_ids=OWNER_ID, kill_switch=False,
        assaf_os_workspace_id=WORKSPACE, assaf_os_authority_epoch=1, assaf_os_backend="postgres",
        assaf_os_ledger_enabled=True,
    )
    return Settings(**(values | changes))


def principal(*, owner=OWNER_ID, scopes=ALL_OS, expires_at=None) -> OwnerSessionPrincipal:
    return OwnerSessionPrincipal(
        owner_id=owner, scopes=frozenset(scopes), session_hash="h" * 64,
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=30),
    )


def proven(value: OwnerSessionPrincipal) -> OwnerSessionPrincipal:
    """What ``owner_mutation_scope`` returns after Origin + session + CSRF pass (tests only)."""
    return owner_sessions._attach_mutation_proof(value)


def refused(call, *args) -> str:
    with pytest.raises(api.LedgerError) as exc:
        call(*args)
    assert exc.value.write_outcome == "not_started"
    return exc.value.code


# ---------------------------------------------------------------------------------------------
# OS: from_owner_read / from_owner_mutation


def test_from_owner_read_binds_the_session_never_a_verified_mutation():
    session = principal(scopes=ALL_OS | {"owner-os:read:contacts"})
    for source in (session, proven(session)):
        ctx = authorize.from_owner_read(source, settings())
        assert type(ctx) is api.TrustedOwnerContext
        assert (ctx.channel, ctx.numeric_owner, ctx.workspace, ctx.authority_epoch) == (
            "OS", int(OWNER_ID), WORKSPACE, 1)
        assert ctx.browser_mutation_verified is False
        assert ctx.scopes == ALL_OS  # only the assaf-os scopes reach the ledger
        assert ctx.auth_expires_at == session.expires_at
        assert ctx.credential_id is None


def test_from_owner_mutation_needs_the_proof_only_owner_mutation_scope_sets():
    session = principal()
    assert refused(authorize.from_owner_mutation, session, settings()) == (
        "browser_mutation_verification_required")
    forged = dataclasses.replace(session, _mutation_proof=object())
    assert refused(authorize.from_owner_mutation, forged, settings()) == (
        "browser_mutation_verification_required")
    ctx = authorize.from_owner_mutation(proven(session), settings())
    assert (ctx.channel, ctx.browser_mutation_verified) == ("OS", True)
    assert ctx.auth_expires_at == session.expires_at


def test_the_mutation_proof_is_not_part_of_principal_equality_or_repr():
    session = principal()
    assert proven(session) == session
    assert "proof" not in repr(proven(session))


@pytest.mark.parametrize("build", [authorize.from_owner_read, authorize.from_owner_mutation])
@pytest.mark.parametrize(
    ("owner", "allow_list"),
    [
        ("999", OWNER_ID),  # not the owner
        ("@assaf", "@assaf"),  # a username never grants
        ("0123", "0123"),  # not canonical: str(int) would differ from the ledger's owner_id
        ("١٢٣", "١٢٣"),  # non-ASCII digits pass str.isdigit()
        (" 123", OWNER_ID),
        ("", OWNER_ID),
    ],
)
def test_os_constructors_refuse_any_owner_outside_the_numeric_allow_list(build, owner, allow_list):
    config = settings(telegram_owner_user_ids=allow_list)
    assert refused(build, proven(principal(owner=owner)), config) == "owner_auth_required"


@pytest.mark.parametrize("build", [authorize.from_owner_read, authorize.from_owner_mutation])
@pytest.mark.parametrize(
    "expires_at",
    [datetime.now(UTC) - timedelta(seconds=1), datetime(2099, 1, 1)],  # expired; naive
    ids=["expired", "naive"],
)
def test_os_constructors_refuse_an_expired_session(build, expires_at):
    session = proven(principal(expires_at=expires_at))
    assert refused(build, session, settings()) == "owner_auth_required"


@pytest.mark.parametrize("build", [authorize.from_owner_read, authorize.from_owner_mutation])
def test_os_constructors_refuse_anything_but_a_session_principal(build):
    session = proven(principal())
    lookalike = SimpleNamespace(**{f.name: getattr(session, f.name)
                                   for f in dataclasses.fields(session)})
    for value in (lookalike, Principal.owner(source="telegram", actor_id=OWNER_ID), None):
        assert refused(build, value, settings()) == "owner_auth_required"


def test_os_constructors_refuse_a_wrong_scope():
    assert refused(authorize.from_owner_read, principal(scopes={"assaf-os:propose"}),
                   settings()) == "scope_denied"
    assert refused(authorize.from_owner_mutation, proven(principal(scopes={"assaf-os:read"})),
                   settings()) == "scope_denied"
    assert refused(authorize.from_owner_mutation,
                   proven(principal(scopes={"quick_leads.create", "business-ledger:write"})),
                   settings()) == "scope_denied"


@pytest.mark.parametrize(
    "changes",
    [{"assaf_os_workspace_id": ""}, {"assaf_os_workspace_id": "bad workspace"},
     {"assaf_os_authority_epoch": 0}],
    ids=["no-workspace", "bad-workspace", "epoch-0"],
)
def test_every_constructor_refuses_a_missing_workspace_or_epoch(changes):
    config = settings(**changes)
    builds = [
        lambda: authorize.from_owner_read(principal(), config),
        lambda: authorize.from_owner_mutation(proven(principal()), config),
        lambda: authorize.from_mia_tool(Principal.owner(source="telegram", actor_id=OWNER_ID),
                                        config),
        lambda: authorize.for_worker("mirror", config),
        lambda: authorize.from_assistant(credential(), config),
    ]
    for build in builds:
        assert refused(build) == "ledger_unavailable"


# ---------------------------------------------------------------------------------------------
# MIA: from_mia_tool


def test_from_mia_tool_binds_the_owner_principal_for_one_short_call():
    before = datetime.now(UTC)
    ctx = authorize.from_mia_tool(Principal.owner(source="telegram", actor_id=OWNER_ID),
                                  settings())
    assert (ctx.channel, ctx.numeric_owner, ctx.workspace, ctx.authority_epoch) == (
        "MIA", int(OWNER_ID), WORKSPACE, 1)
    assert (ctx.scopes, ctx.browser_mutation_verified, ctx.credential_id) == (
        frozenset(), False, None)
    assert before < ctx.auth_expires_at <= datetime.now(UTC) + authorize.CONTEXT_TTL


@pytest.mark.parametrize(
    "value",
    [
        Principal.client(source="website", actor_id=OWNER_ID),  # a visitor never gets owner tools
        Principal.owner(source="telegram", actor_id="999"),
        Principal.owner(source="telegram", actor_id="@assaf"),
        Principal.owner(source="telegram", actor_id=""),
        SimpleNamespace(graph=Principal.owner(source="t").graph, source="telegram",
                        actor_id=OWNER_ID, is_owner=lambda: True),
        None,
    ],
)
def test_from_mia_tool_refuses_anything_but_the_numeric_owner(value):
    assert refused(authorize.from_mia_tool, value, settings()) == "owner_auth_required"


# ---------------------------------------------------------------------------------------------
# TELEGRAM_APPROVAL: from_telegram_decision, only after the ApprovalRow compare-and-set


@pytest.fixture
def store():
    init_db()
    session = get_session_factory()()
    try:
        yield LeadStore(session)
    finally:
        session.rollback()
        session.close()


OWNER = Principal.owner(source="telegram", actor_id=OWNER_ID)


def owner_settings(**changes) -> Settings:
    return settings(**changes)


def card(store, *, decision: str | None = DECISION_APPROVED) -> ApprovalRow:
    """A real owner card; decided through the production CAS and committed, unless None."""
    proposal = propose_owner_action(
        store, principal=OWNER, source_ref=f"tg:v2-19:{uuid.uuid4().hex}",
        kind="assaf_os.approve", parameters={"proposal_id": "PRO_x"}, target={"note": "v2-19"},
    )
    store.session.commit()
    if decision is not None:
        result = decide_owner_action(store, principal=OWNER, approval_id=proposal.approval_id,
                                     decision=decision)
        assert result.status == "decided"
        store.session.commit()
    return store.get_approval_by_approval_id(proposal.approval_id)


@pytest.mark.parametrize("decision", [DECISION_APPROVED, DECISION_REJECTED])
def test_from_telegram_decision_after_the_committed_cas(store, decision):
    row = card(store, decision=decision)
    expiry = datetime.fromisoformat(row.expires_at)
    ctx = authorize.from_telegram_decision(row, owner_settings())
    assert (ctx.channel, ctx.numeric_owner, ctx.workspace, ctx.authority_epoch) == (
        "TELEGRAM_APPROVAL", int(OWNER_ID), WORKSPACE, 1)
    assert (ctx.scopes, ctx.browser_mutation_verified, ctx.credential_id) == (
        frozenset(), False, None)
    assert ctx.auth_expires_at <= expiry
    assert ctx.auth_expires_at <= datetime.now(UTC) + authorize.CONTEXT_TTL
    authorize.require(ctx, "decide")
    assert refused(authorize.require, ctx, "read") == "channel_operation_denied"


def test_from_telegram_decision_refuses_a_card_without_a_decision(store):
    row = card(store, decision=None)
    assert refused(authorize.from_telegram_decision, row, owner_settings()) == (
        "owner_approval_required")


def test_from_telegram_decision_refuses_a_decision_that_is_not_the_stored_cas(store):
    row = card(store, decision=None)
    row.decision = DECISION_APPROVED  # set in memory, never through the CAS
    row.approver = OWNER_ID
    row.approved_at = datetime.now(UTC).isoformat()
    assert refused(authorize.from_telegram_decision, row, owner_settings()) == (
        "owner_approval_required")
    store.session.rollback()
    transient = ApprovalRow(
        channel="telegram", action="owner_external_write", risk="R3", payload_hash="0" * 64,
        decision=DECISION_APPROVED, approver=OWNER_ID, resource_type="owner_proposal",
        resource_id="op_" + "0" * 40, expires_at=(datetime.now(UTC) + timedelta(hours=1))
        .isoformat(), approval_id="a" * 32, actor_id=OWNER_ID, approved_at="x",
    )
    assert refused(authorize.from_telegram_decision, transient, owner_settings()) == (
        "owner_approval_required")


def test_from_telegram_decision_refuses_an_expired_or_executed_card(store):
    row = card(store)
    row.expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    store.session.commit()
    assert refused(authorize.from_telegram_decision, row, owner_settings()) == (
        "owner_approval_required")
    executed = card(store)
    executed.executed_at = datetime.now(UTC).isoformat()
    store.session.commit()
    assert refused(authorize.from_telegram_decision, executed, owner_settings()) == (
        "owner_approval_required")


def test_from_telegram_decision_rechecks_the_numeric_owner(store):
    row = card(store)
    assert refused(authorize.from_telegram_decision, row,
                   owner_settings(telegram_owner_user_ids="999")) == "owner_auth_required"
    lookalike = SimpleNamespace(**{c.key: getattr(row, c.key)
                                   for c in ApprovalRow.__table__.columns})
    assert refused(authorize.from_telegram_decision, lookalike, owner_settings()) == (
        "owner_auth_required")


# ---------------------------------------------------------------------------------------------
# ASSISTANT_*: from_assistant (the credential type is V2-37's; read here by attribute)


def credential(**changes):
    values = dict(
        workspace_id=WORKSPACE, credential_id="AC_" + "b" * 32, owner_id=OWNER_ID, epoch=1,
        kind="claude", scopes=ASSISTANT_SCOPES, revoked_at=None,
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize(("kind", "channel"), [("claude", "ASSISTANT_CLAUDE"),
                                               ("gpt", "ASSISTANT_GPT")])
def test_from_assistant_binds_the_credential(kind, channel):
    key = credential(kind=kind)
    ctx = authorize.from_assistant(key, settings())
    assert (ctx.channel, ctx.numeric_owner, ctx.workspace, ctx.authority_epoch) == (
        channel, int(OWNER_ID), WORKSPACE, 1)
    assert ctx.credential_id == key.credential_id
    assert ctx.scopes == frozenset(ASSISTANT_SCOPES.split())
    assert ctx.browser_mutation_verified is False
    assert ctx.auth_expires_at == key.expires_at


@pytest.mark.parametrize(
    "changes",
    [
        {"revoked_at": datetime.now(UTC)},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"expires_at": datetime(2099, 1, 1)},
        {"owner_id": "999"},
        {"owner_id": "@assaf"},
        {"epoch": 2},
        {"workspace_id": "other"},
        {"kind": "paste"},
        {"credential_id": "AC_short"},
        {"scopes": ASSISTANT_SCOPES + " statement:approve"},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_from_assistant_refuses_uniformly(changes):
    assert refused(authorize.from_assistant, credential(**changes), settings()) == (
        "assistant_auth_required")


# ---------------------------------------------------------------------------------------------
# WORKER: for_worker


@pytest.mark.parametrize("job", JOBS)
def test_for_worker_binds_the_single_owner_and_one_job(job):
    ctx = authorize.for_worker(job, settings())
    assert (ctx.channel, ctx.numeric_owner, ctx.scopes) == ("WORKER", int(OWNER_ID),
                                                          frozenset({job}))
    assert (ctx.workspace, ctx.authority_epoch, ctx.browser_mutation_verified) == (
        WORKSPACE, 1, False)
    assert ctx.auth_expires_at <= datetime.now(UTC) + authorize.CONTEXT_TTL


@pytest.mark.parametrize("allow_list", ["", "123,456", "abc"])
def test_for_worker_needs_exactly_one_numeric_owner(allow_list):
    config = settings(telegram_owner_user_ids=allow_list)
    assert refused(authorize.for_worker, "mirror", config) == "ledger_unavailable"


@pytest.mark.parametrize("job", ["admin", "", "MIRROR", None])
def test_for_worker_refuses_an_unknown_job(job):
    assert refused(authorize.for_worker, job, settings()) == "scope_denied"


# ---------------------------------------------------------------------------------------------
# End to end against the FakeLedger: the ledger refuses what the binding cannot see.


@pytest.fixture
def ledger():
    fake, knobs = build_fake_fixture()
    fake.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    fake.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    fake.knobs = knobs
    return fake


def propose(ledger, ctx):
    return ledger.propose(ctx, api.ProposeInput(rid(), "tasks", None, 0,
                                                {"title": "V2-19", "status": TASK_OPEN}))


def test_minted_contexts_work_on_the_ledger(ledger, store):
    mutation = authorize.from_owner_mutation(proven(principal()), settings())
    authorize.require(mutation, "propose")
    proposal = propose(ledger, mutation)
    mia = authorize.from_mia_tool(OWNER, settings())
    authorize.require(mia, "propose")
    assert propose(ledger, mia).source == "MIA"
    read = authorize.from_owner_read(principal(), settings())
    authorize.require(read, "read")
    assert {p.proposal_id for p in ledger.pending(read, after=None, limit=10).items} >= {
        proposal.proposal_id}
    telegram = authorize.from_telegram_decision(card(store), settings())
    authorize.require(telegram, "decide")
    result = ledger.decide(telegram, proposal.proposal_id, proposal.approval_id, "approve")
    assert result.receipt.status == "committed"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"telegram_owner_user_ids": "456"}, "authority_changed"),  # a different numeric owner
        ({"assaf_os_workspace_id": "other"}, "ledger_unavailable"),  # no such workspace row
        ({"assaf_os_authority_epoch": 2}, "authority_changed"),  # another epoch (I12)
    ],
    ids=["owner", "workspace", "epoch"],
)
def test_a_wrong_owner_workspace_or_epoch_is_refused_by_the_ledger(ledger, changes, code):
    config = settings(**changes)
    owner = config.telegram_owner_user_id_set().pop()
    read = authorize.from_owner_read(principal(owner=owner), config)
    write = authorize.from_owner_mutation(proven(principal(owner=owner)), config)
    assert refused(ledger.head, read) == code
    assert refused(propose, ledger, write) == code


def test_the_kill_switch_blocks_propose_decide_and_commit_with_minted_contexts(ledger, store):
    config = settings()
    os_write = authorize.from_owner_mutation(proven(principal()), config)
    pending = propose(ledger, os_write)
    ledger.knobs.kill_switch = True
    mia = authorize.from_mia_tool(OWNER, config)
    telegram = authorize.from_telegram_decision(card(store), config)
    assert refused(propose, ledger, os_write) == "kill_switch"
    assert refused(propose, ledger, mia) == "kill_switch"
    for ctx in (os_write, telegram):
        assert refused(ledger.decide, ctx, pending.proposal_id, pending.approval_id,
                       "approve") == "kill_switch"
    for kind in ("owner_direct", "owner_undo"):  # commit, undo and abandon open these
        with pytest.raises(api.LedgerError) as exc:
            with ledger.transaction(os_write, domain="core", kind=kind):
                pass
        assert exc.value.code == "kill_switch"
    # Reads stay available, so the owner can still see what is pending.
    read = authorize.from_owner_read(principal(), config)
    assert [p.proposal_id for p in ledger.pending(read, after=None, limit=10).items] == [
        pending.proposal_id]
