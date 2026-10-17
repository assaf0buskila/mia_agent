"""V2-20: PostgresOsBackend, the ledger-backed OsBackend (V2_PG_CONTRACTS.md sections 7 and 10).

Everything runs on the FakeLedger: the real PG ledger (V2-16) is not required. The backend builds
the section 10.2 bodies through ``contract`` and refuses to hand out a body its own strict readers
would refuse.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
from types import SimpleNamespace

import pytest
from app.core.config import MiaEnv, Settings
from app.services import assaf_os
from app.services.os_backend_pg import PageQuery, PostgresOsBackend
from app.services.os_ledger import api, contract

from tests.contract.shapes import v2_20
from tests.contract.shapes.v2_20 import (
    build_world,
    decide,
    propose,
    query,
    seed,
    uuid_v4,
)
from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger_builders import commit_direct, counts, tombstone, update
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE


@pytest.fixture
def world():
    w = build_world()
    seed(w)
    return w


def refused(code):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


# ---- construction ------------------------------------------------------------------------------


def make(world, **changes):
    options = dict(ledger=world.ledger, contexts=v2_20.StaticContexts(world.owner, world.owner),
                   clock=world.ledger._now)
    options.update(changes)
    settings = options.pop("settings", world.settings)
    owner = options.pop("owner", OWNER_ID)
    actor = options.pop("actor", "OS")
    return PostgresOsBackend(settings, owner, actor=actor, **options)


def test_the_backend_declares_its_kind(world):
    assert world.backend.kind == "postgres"


@pytest.mark.parametrize("owner", ["999", "@assaf", "", "0", "12 3", "-1"])
def test_only_a_numeric_allow_listed_owner_may_build_the_backend(world, owner):
    with refused("operation_unavailable"):
        make(world, owner=owner)


@pytest.mark.parametrize("actor", ["WORKER", "ASSISTANT_CLAUDE", "os", "", None])
def test_only_the_three_v1_actors_may_build_the_backend(world, actor):
    with refused("channel_operation_denied"):
        make(world, actor=actor)


def test_a_sheets_setting_cannot_build_the_ledger_backend(world):
    sheets = Settings(_env_file=None, env=MiaEnv.TEST, telegram_owner_user_ids=OWNER_ID)
    with refused("backend_not_postgres"):
        make(world, settings=sheets)


def test_the_ledger_is_built_from_settings_when_none_is_injected(world, monkeypatch):
    seen = []

    def build(settings):
        seen.append(settings)
        return world.ledger

    monkeypatch.setattr(api, "build_ledger", build)
    backend = make(world, ledger=None)
    assert seen == [world.settings] and backend.schema()["authority_epoch"] == 1


def test_a_missing_ledger_implementation_fails_closed_at_construction(world, monkeypatch):
    def build(_settings):
        raise api.LedgerError("ledger_unavailable", reason="implementation_missing")

    monkeypatch.setattr(api, "build_ledger", build)
    with refused("ledger_unavailable"):
        make(world, ledger=None)


# ---- the context seam (section 9 constructors) -------------------------------------------------


class Authorize:
    """A stand-in for app.services.os_ledger.authorize (V2-19) recording how it was called."""

    def __init__(self, ctx):
        self.ctx, self.calls = ctx, []

    def _issue(self, name, principal, settings):
        self.calls.append((name, principal, settings))
        return self.ctx

    def from_owner_read(self, principal, settings):
        return self._issue("from_owner_read", principal, settings)

    def from_owner_mutation(self, principal, settings):
        return self._issue("from_owner_mutation", principal, settings)

    def from_mia_tool(self, principal, settings):
        return self._issue("from_mia_tool", principal, settings)

    def from_telegram_decision(self, approval, settings):
        return self._issue("from_telegram_decision", approval, settings)


def with_authorize(world, monkeypatch, actor, principal):
    stub = Authorize(make_ctx("MIA" if actor == "MIA" else "OS", owner=int(OWNER_ID),
                              workspace=WORKSPACE))
    monkeypatch.setattr("app.services.os_backend_pg._load_authorize", lambda: stub)
    backend = make(world, actor=actor, principal=principal, contexts=None)
    return backend, stub


def test_an_os_backend_uses_the_read_constructor_for_reads_and_the_mutation_one_for_writes(
    world, monkeypatch
):
    principal = SimpleNamespace(owner_id=OWNER_ID)
    backend, stub = with_authorize(world, monkeypatch, "OS", principal)
    backend.schema()
    proposal = backend.propose(assaf_os.ProposeRequest(
        request_id=uuid_v4(1), entity="tasks", record_id=None, expected_revision=0,
        input={"title": "x", "status": "לביצוע"}))
    backend.decide("reject", proposal["proposal_id"],
                   assaf_os.DecisionRequest(request_id=proposal["rejection_request_id"]))
    assert [call[0] for call in stub.calls] == [
        "from_owner_read", "from_owner_mutation", "from_owner_mutation"]
    assert all(call[1] is principal and call[2] is world.settings for call in stub.calls)


def test_a_mia_backend_uses_the_mia_tool_constructor_and_cannot_decide(world, monkeypatch):
    principal = SimpleNamespace(actor_id=OWNER_ID)
    backend, stub = with_authorize(world, monkeypatch, "MIA", principal)
    backend.schema()
    before, built = counts(world.env), len(stub.calls)
    with refused("owner_approval_required"):
        backend.decide("approve", "PRO_x", assaf_os.DecisionRequest(request_id=uuid_v4(2)))
    # The refusal comes before any context is built, so the model never even reaches the ledger.
    assert counts(world.env) == before and len(stub.calls) == built
    assert {call[0] for call in stub.calls} == {"from_mia_tool"}


def test_a_telegram_decision_uses_the_telegram_constructor_with_the_approval(world, monkeypatch):
    approval = SimpleNamespace(approval_id="ap-1")
    backend, stub = with_authorize(world, monkeypatch, "TELEGRAM_APPROVAL", approval)
    with refused("proposal_not_found"):
        backend.decide("approve", "PRO_missing", assaf_os.DecisionRequest(request_id=uuid_v4(3)))
    assert stub.calls[-1][:2] == ("from_telegram_decision", approval)


def test_a_missing_authorize_module_fails_closed(world, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.services.os_ledger.authorize", None)
    backend = make(world, contexts=None)
    with refused("ledger_unavailable"):
        backend.schema()


def test_a_context_that_is_not_trusted_is_refused(world):
    class Forged:
        def for_read(self):
            return SimpleNamespace(numeric_owner=1)

    backend = make(world, contexts=Forged())
    with refused("invalid_trusted_context"):
        backend.schema()


# ---- schema ------------------------------------------------------------------------------------


def test_the_schema_body_is_bound_to_the_ledger_epoch_without_observed_version(world):
    body = world.backend.schema()
    contract.read_schema_v2(body)
    assert body["workspace_id"] == WORKSPACE and body["authority_epoch"] == 1
    assert "observed_version" not in body and body["money_unit"] == "minor"
    assert [tab["key"] for tab in body["tabs"]] == [
        "summary", "leads", "clients", "works", "payments", "time", "expenses", "tasks",
        "settings"]


def test_the_schema_is_a_fresh_copy_every_call(world):
    first = world.backend.schema()
    first["tabs"][0]["name"] = "changed"
    first["enums"]["path"].append("x")
    again = world.backend.schema()
    assert again["tabs"][0]["name"] != "changed" and "x" not in again["enums"]["path"]


def test_the_schema_reads_the_ledger_so_a_missing_workspace_is_unavailable():
    w = build_world()
    w.ledger._workspaces.clear()
    with refused("ledger_unavailable"):
        w.backend.schema()


def test_the_schema_epoch_is_the_ledgers_not_a_stale_context():
    w = build_world()
    stale = make_ctx("OS", owner=int(OWNER_ID), workspace=WORKSPACE, epoch=2)
    backend = PostgresOsBackend(
        w.settings, OWNER_ID, actor="OS", ledger=w.ledger,
        contexts=v2_20.StaticContexts(stale, stale), clock=w.ledger._now)
    with refused("authority_changed"):
        backend.schema()


# ---- snapshot: pages ---------------------------------------------------------------------------


def test_the_default_query_is_the_first_tasks_page_open_tasks_first(world):
    body = world.backend.snapshot()
    contract.read_snapshot_v2(body)
    assert body["entity"] == "tasks" and body["total"] == 3 and body["coverage"] == "complete"
    assert [r["task_id"] for r in body["records"]] == ["TAS_t1", "TAS_t2", "TAS_t3"]
    assert body["next_cursor"] is None and body["unchanged"] is False
    assert body["observed_at"] == "2026-10-16T12:00:00.000Z"


def test_snapshot_accepts_none_a_page_query_and_a_duck_typed_query(world):
    assert world.backend.snapshot(None)["entity"] == "tasks"
    assert world.backend.snapshot(PageQuery(entity="leads"))["entity"] == "leads"
    duck = SimpleNamespace(entity="clients", after=None, limit=10, since=None)
    assert world.backend.snapshot(duck)["entity"] == "clients"


def test_paging_walks_every_row_once_and_a_write_invalidates_the_cursor(world):
    first = world.backend.snapshot(query("tasks", limit=2))
    assert len(first["records"]) == 2 and first["next_cursor"]
    second = world.backend.snapshot(query("tasks", after=first["next_cursor"], limit=2))
    seen = [r["task_id"] for r in first["records"] + second["records"]]
    assert seen == ["TAS_t1", "TAS_t2", "TAS_t3"] and second["next_cursor"] is None
    third = world.backend.snapshot(query("tasks", limit=2))
    commit_direct(world.env, world.owner,
                  [update(v2_20.current(world, "tasks", "TAS_t1"), title="x")],
                  request_id=uuid_v4(0x7100))
    with refused("cursor_invalidated"):
        world.backend.snapshot(query("tasks", after=third["next_cursor"], limit=2))


def test_a_forged_cursor_is_invalid_cursor(world):
    with refused("invalid_cursor"):
        world.backend.snapshot(query("tasks", after="forged"))


@pytest.mark.parametrize(
    "bad",
    [
        {"entity": "promote_lead"},
        {"entity": "nope"},
        {"entity": None},
        {"limit": 0},
        {"limit": 201},
        {"limit": True},
        {"limit": "50"},
        {"after": "x" * 513},
        {"since": "x" * 65},
        {"since": "1.2"},
        {"since": "a.b.c"},
        {"after": "c", "since": "1.2.3"},
        {"after": "has space"},
    ],
)
def test_an_invalid_query_is_refused_before_any_read(world, bad):
    spy = Spy(world.ledger)
    backend = make(world, ledger=spy)
    q = SimpleNamespace(**({"entity": "tasks", "after": None, "limit": 50, "since": None} | bad))
    with refused("invalid_query"):
        backend.snapshot(q)
    assert spy.calls == []


def test_the_page_summary_labels_and_pending_count_come_from_the_ledger(world):
    propose(world, "tasks", {"title": "x", "status": "לביצוע"})
    body = world.backend.snapshot(query("tasks"))
    assert body["pending_count"] == 1
    assert body["summary"]["monthly"] == list(world.ledger.monthly_summary(world.owner))
    assert body["summary"]["coverage"] == "complete"
    assert body["labels"]["clients.person_id"] == {"PER_c1": "דנה כהן"}
    assert body["labels"]["works.work_id"] == {"WOR_w1": "סדנת אוטומציה"}


def test_the_summary_coverage_follows_the_monthly_rows(world):
    rows = [dict(world.ledger.monthly_summary(world.owner)[0])]
    rows[0]["coverage"] = "partial"
    world.ledger.monthly_summary = lambda ctx: tuple(rows)
    assert world.backend.snapshot()["summary"]["coverage"] == "partial"
    rows[0]["coverage"] = "unknown"
    assert world.backend.snapshot()["summary"]["coverage"] == "unknown"
    world.ledger.monthly_summary = lambda ctx: ()
    assert world.backend.snapshot()["summary"] == {"coverage": "complete", "monthly": []}


def test_an_expired_pending_proposal_is_not_counted(world):
    propose(world, "tasks", {"title": "x", "status": "לביצוע"})
    world.ledger.advance(601)
    assert world.backend.snapshot()["pending_count"] == 0


def test_the_pending_count_walks_every_page(world, monkeypatch):
    monkeypatch.setattr("app.services.os_backend_pg.PENDING_COUNT_PAGE", 2)
    for n in range(5):
        propose(world, "tasks", {"title": f"t{n}", "status": "לביצוע"})
    assert world.backend.snapshot()["pending_count"] == 5


# ---- snapshot: the since token -----------------------------------------------------------------


def test_since_is_the_head_token_and_unchanged_answers_with_the_binding_only(world):
    first = world.backend.snapshot(query())
    head = world.ledger.head(world.owner)
    assert first["since"] == api.since_token(head)
    again = world.backend.snapshot(query(since=first["since"]))
    contract.read_snapshot_v2(again)
    assert again["unchanged"] is True and again["since"] == first["since"]
    assert set(again) == {
        "api_version", "source", "workspace_id", "authority_epoch", "observed_version",
        "unchanged", "since", "mirror"}
    assert again["observed_version"] == first["observed_version"]


def test_a_propose_changes_the_token_but_not_the_version(world):
    first = world.backend.snapshot(query())
    propose(world, "tasks", {"title": "x", "status": "לביצוע"})
    again = world.backend.snapshot(query(since=first["since"]))
    assert again["unchanged"] is False and again["pending_count"] == 1
    assert again["observed_version"] == first["observed_version"]
    assert again["since"] != first["since"]


def test_a_commit_changes_the_token_and_the_version(world):
    first = world.backend.snapshot(query())
    commit_direct(world.env, world.owner,
                  [update(v2_20.current(world, "tasks", "TAS_t1"), title="y")],
                  request_id=uuid_v4(0x7101))
    again = world.backend.snapshot(query(since=first["since"]))
    assert again["unchanged"] is False and again["observed_version"] > first["observed_version"]


def test_a_mirror_verification_changes_the_token(world):
    first = world.backend.snapshot(query())
    state = world.ledger._workspaces[WORKSPACE]["tables"]["os_mirror_state"]
    state["main"] = {"verified_version": first["observed_version"]}
    assert world.backend.snapshot(query(since=first["since"]))["unchanged"] is False


@pytest.mark.parametrize("part", [0, 1, 2])
def test_a_since_token_that_differs_in_any_one_part_is_not_unchanged(world, part):
    first = world.backend.snapshot(query())
    parts = first["since"].split(".")
    parts[part] = str(int(parts[part]) + 1)
    assert world.backend.snapshot(query(since=".".join(parts)))["unchanged"] is False


def test_reading_never_writes(world):
    before = counts(world.env)
    for _ in range(3):
        world.backend.schema()
        first = world.backend.snapshot(query("tasks", limit=1))
        world.backend.snapshot(query(since=first["since"]))
        world.backend.snapshot(query("expenses"))
        world.backend.pending()
        world.backend.deleted("tasks")
        world.backend.receipt(uuid_v4(0x9997))
        world.backend.proposal_request(uuid_v4(0x9996))
    assert counts(world.env) == before


class Spy:
    """Records which ledger methods the backend calls, in order."""

    def __init__(self, ledger):
        self._ledger, self.calls = ledger, []

    def __getattr__(self, name):
        target = getattr(self._ledger, name)

        def call(*args, **kwargs):
            self.calls.append(name)
            return target(*args, **kwargs)

        return call


def test_the_head_is_read_before_the_page_and_every_other_read_comes_after_the_page(world):
    spy = Spy(world.ledger)
    make(world, ledger=spy).snapshot(query("tasks"))
    assert spy.calls[0] == "head" and spy.calls.index("page") == 1
    assert set(spy.calls[2:]) == {"monthly_summary", "labels", "pending"}


def test_an_unchanged_answer_reads_only_the_head(world):
    first = world.backend.snapshot(query())
    spy = Spy(world.ledger)
    make(world, ledger=spy).snapshot(query(since=first["since"]))
    assert spy.calls == ["head"]


# ---- mirror ------------------------------------------------------------------------------------


def mirror_of(world, **settings):
    w = make(world, settings=v2_20.settings_for(**settings))
    return w.snapshot()["mirror"]


def test_the_mirror_is_disabled_while_the_setting_is_off(world):
    assert mirror_of(world) == {
        "state": "disabled", "last_verified_at": None, "verified_version": 0, "lag_s": None}


def test_the_default_mirror_view_is_derived_from_the_head_alone(world):
    commit_direct(world.env, world.owner,
                  [update(v2_20.current(world, "tasks", "TAS_t1"), title="again")],
                  request_id=uuid_v4(0x7103))
    head = world.ledger.head(world.owner)
    assert head.observed_version >= 2
    enabled = {"assaf_os_mirror_enabled": True}
    assert mirror_of(world, **enabled)["state"] == "unknown"
    world.ledger._workspaces[WORKSPACE]["tables"]["os_mirror_state"]["m"] = {
        "verified_version": head.observed_version - 1}
    lagging = mirror_of(world, **enabled)
    assert lagging["state"] == "lagging" and lagging["verified_version"] == (
        head.observed_version - 1)
    world.ledger._workspaces[WORKSPACE]["tables"]["os_mirror_state"]["m"] = {
        "verified_version": head.observed_version}
    verified = mirror_of(world, **enabled)
    assert verified["state"] == "verified" and verified["lag_s"] is None
    assert verified["last_verified_at"] is None


def test_an_injected_mirror_provider_decides_the_view(world):
    seen = []

    def provider(head):
        seen.append(head)
        return v2_20.verified_mirror(head)

    body = make(world, mirror=provider).snapshot()
    assert body["mirror"]["state"] == "verified" and body["mirror"]["lag_s"] == 0
    assert seen and isinstance(seen[0], api.Head)


def test_an_invalid_injected_mirror_view_is_an_integrity_failure(world):
    backend = make(world, mirror=lambda head: {"state": "fine"})
    with refused("ledger_integrity_failed"):
        backend.snapshot()


# ---- pending, deleted --------------------------------------------------------------------------


def test_pending_lists_only_current_pending_proposals_as_proposal_v2(world):
    first = propose(world, "tasks", {"title": "a", "status": "לביצוע"})
    second = propose(world, "tasks", {"title": "b", "status": "לביצוע"})
    decide(world, "reject", second)
    body = world.backend.pending()
    contract.read_pending_list(body)
    assert [item["proposal_id"] for item in body["items"]] == [first["proposal_id"]]
    assert body["next_cursor"] is None and "observed_version" in body


def test_pending_pages_with_a_cursor(world):
    for n in range(3):
        propose(world, "tasks", {"title": f"p{n}", "status": "לביצוע"})
    first = world.backend.pending(limit=2)
    second = world.backend.pending(after=first["next_cursor"], limit=2)
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert second["next_cursor"] is None


@pytest.mark.parametrize("bad", [{"limit": 0}, {"limit": 201}, {"limit": True},
                                 {"after": "x" * 513}, {"after": 5}])
def test_pending_and_deleted_validate_their_query(world, bad):
    with refused("invalid_query"):
        world.backend.pending(**bad)
    with refused("invalid_query"):
        world.backend.deleted("tasks", **bad)


def test_deleted_lists_tombstones_newest_first(world):
    for record_id, key in (("TAS_t3", 0x7201), ("TAS_t2", 0x7202)):
        before = v2_20.current(world, "tasks", record_id)
        commit_direct(world.env, world.owner, [tombstone("tasks", before)],
                      request_id=uuid_v4(key))
        world.ledger.advance(1)
    body = world.backend.deleted("tasks")
    contract.READERS["deleted"](body)
    assert [r["task_id"] for r in body["records"]] == ["TAS_t2", "TAS_t3"]
    assert world.backend.snapshot(query("tasks"))["total"] == 1


def test_deleted_refuses_an_unknown_entity(world):
    with refused("invalid_query"):
        world.backend.deleted("promote_lead")


# ---- propose, proposal, decide -----------------------------------------------------------------


def request(entity="tasks", **changes):
    values = dict(request_id=uuid_v4(0x300), entity=entity, record_id=None, expected_revision=0,
                  input={"title": "בדיקה", "status": "לביצוע"})
    values.update(changes)
    return assaf_os.ProposeRequest(**values)


def test_propose_returns_a_pending_proposal_v2_and_writes_one_proposal(world):
    before = counts(world.env)
    body = world.backend.propose(request())
    contract.read_proposal_v2(body)
    assert body["status"] == "pending" and body["kind"] == "proposal"
    assert body["actor_source"] == "OS" and body["entity"] == "tasks"
    assert body["input"] == {"title": "בדיקה", "status": "לביצוע"}
    assert body["before"] is None and body["after"]["title"] == "בדיקה"
    assert body["record_id"].startswith("TAS_") and body["expected_revision"] == 0
    assert body["request_id"] == uuid_v4(0x300)
    after = counts(world.env)
    assert after["os_proposals"] == before["os_proposals"] + 1 and after["version"] == before[
        "version"]
    assert "observed_version" not in body


def test_an_update_proposal_input_is_only_the_changed_fields(world):
    body = world.backend.propose(request(
        record_id="TAS_t1", expected_revision=1,
        input={"title": "להכין הצעת מחיר", "status": "בתהליך"}))
    assert body["input"] == {"status": "בתהליך"}
    assert body["before"]["status"] == "לביצוע" and body["after"]["status"] == "בתהליך"
    assert body["before"]["revision"] == 1 and body["after"]["revision"] == 2


def test_a_promotion_proposal_shows_the_lead_and_the_new_client(world):
    body = world.backend.propose(request(
        "promote_lead", record_id="LEA_l1", expected_revision=1, input={}))
    contract.read_proposal_v2(body)
    assert body["entity"] == "promote_lead" and body["record_id"] == "LEA_l1"
    assert body["input"] == {}
    assert body["before"]["lead"]["lead_id"] == "LEA_l1" and body["before"]["client"] is None
    assert body["after"]["lead"]["status"] == "הפך ללקוח"
    assert body["after"]["client"]["person_id"] == "PER_l1"


def test_propose_replay_returns_the_stored_proposal_and_a_changed_body_is_a_mismatch(world):
    first = world.backend.propose(request())
    again = world.backend.propose(request())
    assert again == first
    with refused("idempotency_mismatch"):
        world.backend.propose(request(input={"title": "אחר", "status": "לביצוע"}))


def test_a_non_uuid_request_id_is_refused_by_the_ledger(world):
    with refused("invalid_request_id"):
        world.backend.propose(request(request_id="not-a-uuid-at-all"))


def test_propose_is_refused_under_the_kill_switch_and_the_freeze_writing_nothing(world):
    before = counts(world.env)
    world.knobs.kill_switch = True
    with refused("kill_switch"):
        world.backend.propose(request())
    world.knobs.kill_switch = False
    world.knobs.freeze = True
    with refused("os_frozen"):
        world.backend.propose(request(request_id=uuid_v4(0x301)))
    assert counts(world.env) == before


def test_proposal_reads_a_pending_and_a_decided_proposal(world):
    pending = world.backend.propose(request())
    body = world.backend.proposal(pending["proposal_id"])
    contract.read_proposal_v2(body)
    assert body == pending
    decide(world, "approve", pending)
    decided = world.backend.proposal(pending["proposal_id"])
    # A lone proposal reader is pending-only (the frontend's readProposalV2); a decided body is
    # read with the statuses the ledger may return for a proposal-kind row.
    contract.read_proposal_v2(decided, statuses=contract.PROPOSAL_STATUSES)
    assert decided["status"] == "committed"


def test_a_replayed_propose_of_a_decided_proposal_returns_the_stored_body(world):
    """I1: a replay returns the stored result whatever its status, not an integrity failure."""
    pending = world.backend.propose(request())
    decide(world, "approve", pending)
    again = world.backend.propose(request())
    assert again["status"] == "committed" and again["proposal_id"] == pending["proposal_id"]
    assert {k: v for k, v in again.items() if k != "status"} == {
        k: v for k, v in pending.items() if k != "status"}


def test_the_backend_guards_a_proposal_body_with_explicit_statuses(world, monkeypatch):
    seen = []
    real = contract.read_proposal_v2

    def spy(value, **kwargs):
        seen.append(kwargs)
        return real(value, **kwargs)

    monkeypatch.setattr(contract, "read_proposal_v2", spy)
    pending = world.backend.propose(request())
    world.backend.proposal(pending["proposal_id"])
    assert len(seen) == 2
    assert all(call.get("statuses") == contract.PROPOSAL_STATUSES for call in seen)
    assert all(call.get("kinds", ("proposal",)) == ("proposal",) for call in seen)


def test_proposal_of_an_unknown_id_or_another_kind_is_404(world):
    with refused("proposal_not_found"):
        world.backend.proposal("PRO_missing")
    v2_20.receipt_direct_abandoned(world)
    with world.ledger.transaction(world.owner, domain="core", kind="owner_direct") as tx:
        direct = tx.proposal_by_request(uuid_v4(0x7004))
    with refused("proposal_not_found"):
        world.backend.proposal(direct.proposal_id)


def test_proposal_from_another_epoch_is_refused(world):
    pending = world.backend.propose(request())
    table = world.ledger._workspaces[WORKSPACE]["tables"]["os_proposals"]
    table[pending["proposal_id"]] = dataclasses.replace(table[pending["proposal_id"]], epoch=2)
    with refused("authority_changed"):
        world.backend.proposal(pending["proposal_id"])


def test_approve_commits_and_returns_receipt_v2_with_the_current_record(world):
    pending = world.backend.propose(request())
    body = decide(world, "approve", pending)
    contract.read_receipt_v2(body)
    assert body["status"] == "committed" and body["kind"] == "proposal"
    assert body["request_id"] == pending["approval_request_id"]
    assert body["proposal_id"] == pending["proposal_id"] and body["actor_source"] == "OS"
    assert body["committed_at"] == "2026-10-16T12:00:00.000Z" and body["undo"] is None
    assert [r["entity"] for r in body["records"]] == ["tasks"]
    assert body["records"][0]["record"]["title"] == "בדיקה"
    assert body["commit_version"] == body["observed_version"] and body["undo_of"] is None
    assert body["server_now"] == "2026-10-16T12:00:00.000Z"


def test_reject_expiry_and_target_changed_are_receipts_not_errors(world):
    rejected = decide(world, "reject", world.backend.propose(request()))
    assert rejected["status"] == "rejected" and rejected["records"] == []
    assert rejected["committed_at"] is None
    late = world.backend.propose(request(request_id=uuid_v4(0x302)))
    world.ledger.advance(601)
    expired = decide(world, "approve", late)
    assert expired["status"] == "expired" and expired["committed_at"] is None
    stale = world.backend.propose(request(
        request_id=uuid_v4(0x303), record_id="TAS_t1", expected_revision=1,
        input={"status": "בתהליך"}))
    commit_direct(world.env, world.owner,
                  [update(v2_20.current(world, "tasks", "TAS_t1"), title="zz")],
                  request_id=uuid_v4(0x7102))
    changed = decide(world, "approve", stale)
    assert changed["status"] == "target_changed" and changed["records"]
    assert changed["records"][0]["record"]["title"] == "zz"


def test_decide_replay_returns_the_same_receipt_and_a_second_decision_is_refused(world):
    pending = world.backend.propose(request())
    first = decide(world, "approve", pending)
    assert decide(world, "approve", pending) == first
    with refused("already_decided"):
        decide(world, "reject", pending)


def test_a_wrong_decision_id_is_a_mismatch_writing_nothing(world):
    pending = world.backend.propose(request())
    before = counts(world.env)
    with refused("decision_request_mismatch"):
        world.backend.decide("approve", pending["proposal_id"],
                             assaf_os.DecisionRequest(request_id=pending["rejection_request_id"]))
    assert counts(world.env) == before


def test_decide_on_another_kind_is_404_with_zero_rows(world):
    v2_20.receipt_direct_abandoned(world)
    with world.ledger.transaction(world.owner, domain="core", kind="owner_direct") as tx:
        direct = tx.proposal_by_request(uuid_v4(0x7004))
    before = counts(world.env)
    with refused("proposal_not_found"):
        world.backend.decide("approve", direct.proposal_id,
                             assaf_os.DecisionRequest(request_id=direct.approval_id))
    assert counts(world.env) == before


def test_an_operation_other_than_approve_or_reject_is_refused(world):
    pending = world.backend.propose(request())
    with refused("validation_error"):
        world.backend.decide("undo", pending["proposal_id"],
                             assaf_os.DecisionRequest(request_id=pending["approval_request_id"]))


# ---- receipt and proposal-request recovery -----------------------------------------------------


def test_receipt_by_its_key_and_unknown_when_absent(world):
    pending = world.backend.propose(request())
    decide(world, "approve", pending)
    body = world.backend.receipt(pending["approval_request_id"])
    contract.read_receipt_v2(body)
    assert body["status"] == "committed" and body["request_id"] == pending["approval_request_id"]
    unknown = world.backend.receipt(uuid_v4(0x9990))
    contract.read_receipt_v2(unknown)
    assert unknown["status"] == "unknown" and unknown["reason_codes"] == ["receipt_not_found"]
    assert unknown["request_id"] == uuid_v4(0x9990) and unknown["server_now"]


def test_receipt_refuses_a_non_uuid(world):
    with refused("invalid_request_id"):
        world.backend.receipt("nope")


def test_an_owner_direct_receipt_carries_the_undo_window_and_its_end_states(world):
    key = uuid_v4(0x7300)
    commit_direct(world.env, world.owner,
                  [update(v2_20.current(world, "tasks", "TAS_t1"), status="בוצע")],
                  request_id=key)
    body = world.backend.receipt(key)
    assert body["kind"] == "owner_direct" and body["actor_source"] == "OS_DIRECT"
    assert body["undo"] == {"state": "available", "until": "2026-10-16T12:00:30.000Z",
                            "undo_request_id": None}
    world.ledger.advance(31)
    assert world.backend.receipt(key)["undo"]["state"] == "expired"
    assert world.backend.receipt(key)["server_now"] == "2026-10-16T12:00:31.000Z"


def test_proposal_request_recovers_a_lost_acknowledgement_by_reading(world):
    pending = world.backend.propose(request())
    found = world.backend.proposal_request(pending["request_id"])
    contract.read_proposal_request_v2(found)
    assert found["status"] == "found" and found["receipt"] is None
    assert found["proposal"] == pending and found["reason_codes"] == []
    decide(world, "approve", pending)
    done = world.backend.proposal_request(pending["request_id"])
    contract.read_proposal_request_v2(done)
    assert done["proposal"]["status"] == "committed"
    assert done["receipt"]["request_id"] == pending["approval_request_id"]
    assert done["receipt"]["records"]
    unknown = world.backend.proposal_request(uuid_v4(0x9991))
    assert unknown["status"] == "unknown" and unknown["reason_codes"] == [
        "proposal_request_unknown"]
    assert unknown["proposal"] is None and unknown["receipt"] is None


def test_a_committed_batch_that_wrote_no_record_is_recoverable_by_reading(world):
    """A statement batch of personal-only lines has no expense op (section 12.2), so it commits
    with ``records == []``. The I1 recovery reads of that real commit must not fail."""
    request_id, approval_id = v2_20.committed_batch(world)
    receipt = world.backend.receipt(approval_id)
    contract.read_receipt_v2(receipt)
    assert receipt["status"] == "committed" and receipt["kind"] == "statement_batch"
    assert receipt["records"] == [] and receipt["committed_at"] and receipt["undo"] is None
    found = world.backend.proposal_request(request_id)
    contract.read_proposal_request_v2(found)
    assert found["status"] == "found" and found["proposal"]["status"] == "committed"
    assert found["receipt"] == receipt


def test_a_proposal_request_never_reads_across_epochs(world):
    pending = world.backend.propose(request())
    stale = make_ctx("OS", owner=int(OWNER_ID), workspace=WORKSPACE, epoch=2)
    backend = make(world, contexts=v2_20.StaticContexts(stale, stale))
    with refused("authority_changed"):
        backend.proposal_request(pending["request_id"])


# ---- fail closed, privacy ----------------------------------------------------------------------


def test_a_ledger_answer_that_breaks_the_contract_is_an_integrity_failure(world):
    rows = [{**dict(world.ledger.monthly_summary(world.owner)[0]), "unexpected": 1}]
    world.ledger.monthly_summary = lambda ctx: tuple(rows)
    with refused("ledger_integrity_failed"):
        world.backend.snapshot()


def test_a_record_with_a_nested_value_is_an_integrity_failure(world):
    real = world.ledger.page

    def page(ctx, entity, *, after, limit):
        result = real(ctx, entity, after=after, limit=limit)
        broken = dict(result.records[0], nested={"a": 1})
        return api.Page(result.entity, (broken, *result.records[1:]), result.next_cursor,
                        result.total, result.observed_version)

    world.ledger.page = page
    with refused("ledger_integrity_failed"):
        world.backend.snapshot()


def test_ledger_errors_pass_through_unchanged(world):
    world.ledger._workspaces.clear()
    with refused("ledger_unavailable"):
        world.backend.snapshot()


def test_no_body_is_labelled_google_sheets_and_none_has_a_v1_key(world):
    pending = world.backend.propose(request())
    decided = decide(world, "approve", pending)
    bodies = [
        world.backend.schema(), world.backend.snapshot(), world.backend.pending(),
        pending, decided, world.backend.receipt(decided["request_id"]),
        world.backend.proposal_request(pending["request_id"]), world.backend.deleted("tasks"),
    ]
    text = json.dumps(bodies, ensure_ascii=False)
    for forbidden in ("google_sheets", "workbook_id", '"mode"', "assaf-os.v1"):
        assert forbidden not in text


def test_logs_carry_reason_codes_and_never_record_text(world, caplog):
    caplog.set_level(logging.DEBUG)
    pending = world.backend.propose(request())
    decide(world, "approve", pending)
    world.backend.snapshot()
    world.knobs.freeze = True
    with refused("os_frozen"):
        world.backend.propose(request(request_id=uuid_v4(0x304)))
    text = caplog.text
    for secret in ("בדיקה", "דנה כהן", "Meta Platforms", "dana@example.com", pending["proposal_id"],
                   pending["request_id"]):
        assert secret not in text
    assert "os_backend_pg" in text and "reason=os_frozen" in text


def test_the_backend_touches_only_the_surface_the_conformance_suite_proves(world):
    """``LedgerEnv`` exposes exactly the methods V2-16's PG ledger must pass the suite for, so a
    backend that works over it works over PG."""
    backend = make(world, ledger=world.env)
    pending = backend.propose(request())
    decide(world, "approve", pending)
    second = backend.propose(request(request_id=uuid_v4(0x305)))
    backend.schema()
    first = backend.snapshot(query("tasks", limit=1))
    backend.snapshot(query(since=first["since"]))
    backend.pending()
    backend.deleted("tasks")
    backend.proposal(second["proposal_id"])
    backend.receipt(pending["approval_request_id"])
    backend.proposal_request(second["request_id"])
    backend.decide("reject", second["proposal_id"],
                   assaf_os.DecisionRequest(request_id=second["rejection_request_id"]))


def test_the_model_actor_never_reaches_the_ledger_to_decide(world):
    spy = Spy(world.ledger)
    mia = make_ctx("MIA", owner=int(OWNER_ID), workspace=WORKSPACE)
    backend = make(world, actor="MIA", ledger=spy, contexts=v2_20.StaticContexts(mia, mia))
    with refused("owner_approval_required"):
        backend.decide("approve", "PRO_x", assaf_os.DecisionRequest(request_id=uuid_v4(4)))
    assert spy.calls == []
