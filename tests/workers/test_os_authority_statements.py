"""V2-40: ``mia os-authority activate-statements`` (contract §22 E40).

``activate`` moves only ``core``. Every statement batch (the assistant's only write, §17) also needs
``statements`` active, so after the cutover this one transition is what the assistant waits for.
The cases run on the in-memory ledger and, when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set, on the
real ``PgAuthorityAdmin`` too (the ``CASES`` rule of test_os_authority.py).
"""

from __future__ import annotations

import json
import logging

import pytest
from app.services.os_ledger import api
from app.workers import os_authority as cli

from tests.fakes.os_ledger_builders import commit_direct, ctx, task
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE
from tests.workers.test_os_authority import (
    CASES,
    OTHER_SHA,
    PEPPER,
    SHA,
    World,
    codes,
    good_evidence,
)

COMMAND = "activate-statements"


def after_open(world: World) -> None:
    """The settings of a finished cutover: T6 switch and T7 mirror applied, the freeze lifted."""
    world.settings.assaf_os_backend = "postgres"
    world.settings.assaf_os_ledger_enabled = True
    world.settings.assaf_os_authority_epoch = world.state().epoch
    world.settings.assaf_os_freeze = False
    world.settings.kill_switch = False


def first_write(world: World) -> None:
    """G16/G17: one owner write at the current epoch (the point of no simple rollback)."""
    commit_direct(world.env, ctx(epoch=world.state().epoch), [task("TAS_first")])


@pytest.fixture(params=CASES)
def live(request, tmp_path):
    """Core active, the switch applied, the freeze off, one owner write, the pepper pinned."""
    world = World(tmp_path, request.param)
    world.settings.os_statement_pepper = PEPPER
    assert world.run("init").code == 0
    good_evidence(world)
    assert world.run("activate").code == 0
    after_open(world)
    first_write(world)
    return world


def untouched(world: World):
    state = world.state()
    return (state.epoch, dict(state.states), sorted(p.name for p in world.dir.iterdir()))


def batch_admitted(world: World) -> bool:
    try:
        with world.env.transaction(ctx(), domain="statements", kind="statement_batch"):
            return True
    except api.LedgerError as error:
        assert error.code == "authority_not_active", error
        return False


def test_the_command_is_listed():
    assert COMMAND in cli.COMMANDS


def test_a_finished_cutover_activates_statements_without_bumping_the_epoch(live):
    assert not batch_admitted(live)
    run = live.run(COMMAND)
    assert run.code == 0 and run.err == ""
    assert run.out["outcome"] == "activated"
    after = live.state()
    states = (after.epoch, after.states["core"], after.states["statements"])
    assert states == (1, "active", "active")
    assert after.activated_at["statements"] is not None
    evidence = live.get(cli.STATEMENTS_ACTIVATION)
    assert (evidence["kind"], evidence["epoch"], evidence["release_sha"]) == (
        "statements_activation", 1, SHA)
    assert evidence["activated_at"]
    assert batch_admitted(live)


def test_a_repeat_is_already_active_and_transitions_nothing(live, monkeypatch):
    assert live.run(COMMAND).code == 0
    first = live.get(cli.STATEMENTS_ACTIVATION)

    def no_transition(*args, **kwargs):
        raise AssertionError("transition called on a repeat")

    monkeypatch.setattr(live.env, "transition", no_transition)
    again = live.run(COMMAND)
    assert again.code == 0 and again.out["outcome"] == "already_active"
    assert live.get(cli.STATEMENTS_ACTIVATION) == first


def test_a_repeat_repairs_missing_evidence_without_inventing_a_time(live):
    assert live.run(COMMAND).code == 0
    live.drop(cli.STATEMENTS_ACTIVATION)
    again = live.run(COMMAND)
    assert again.code == 0 and again.out["outcome"] == "already_active"
    repaired = live.get(cli.STATEMENTS_ACTIVATION)
    assert (repaired["epoch"], repaired["activated_at"]) == (1, None)


REFUSALS = {
    "switch_not_applied": lambda w: setattr(w.settings, "assaf_os_backend", "sheets"),
    "freeze_on": lambda w: setattr(w.settings, "assaf_os_freeze", True),
    "kill_switch": lambda w: setattr(w.settings, "kill_switch", True),
    "pepper_missing": lambda w: setattr(w.settings, "os_statement_pepper", ""),
    "pepper_mismatch": lambda w: setattr(w.settings, "os_statement_pepper", "Z" * 48),
}


@pytest.mark.parametrize("code", sorted(REFUSALS))
def test_each_refusal_leaves_the_authority_and_evidence_untouched(live, code):
    REFUSALS[code](live)
    before = untouched(live)
    run = live.run(COMMAND)
    assert run.code == 1 and codes(run) == [code]
    assert run.out["ok"] is False
    assert untouched(live) == before
    assert not batch_admitted(live)


@pytest.mark.parametrize("field", ["ledger_disabled", "epoch_mismatch"])
def test_the_switch_must_be_fully_applied(live, field):
    if field == "ledger_disabled":
        live.settings.assaf_os_ledger_enabled = False
    else:
        live.settings.assaf_os_authority_epoch = 2
    run = live.run(COMMAND)
    assert run.code == 1 and codes(run) == ["switch_not_applied"]
    assert live.state().states["statements"] == "shadow"


def test_a_missing_kill_switch_setting_fails_closed(live):
    del live.settings.kill_switch
    run = live.run(COMMAND)
    assert run.code == 1 and codes(run) == ["kill_switch"]


@pytest.mark.parametrize("case", CASES)
def test_no_owner_write_yet_is_refused(tmp_path, case):
    """Before the first non-import receipt RB-1 (rollback) is still allowed; it fences only core,
    and ``init --rearm`` would then carry an active ``statements`` to the next epoch."""
    world = World(tmp_path, case)
    world.settings.os_statement_pepper = PEPPER
    assert world.run("init").code == 0
    good_evidence(world)
    assert world.run("activate").code == 0
    after_open(world)
    before = untouched(world)
    run = world.run(COMMAND)
    assert run.code == 1 and codes(run) == ["no_first_write"]
    assert untouched(world) == before


@pytest.mark.parametrize("case", CASES)
def test_core_must_be_active(tmp_path, case):
    world = World(tmp_path, case)
    world.settings.os_statement_pepper = PEPPER
    assert world.run("init").code == 0
    after_open(world)
    run = world.run(COMMAND)
    assert run.code == 1 and codes(run) == ["core_not_active"]
    assert world.state().states == {"core": "shadow", "statements": "shadow"}


def test_a_fenced_statements_domain_is_refused(live):
    live.env.transition(WORKSPACE, "statements", "fenced", expected_epoch=1,
                        actor_owner_id=OWNER_ID)
    run = live.run(COMMAND)
    assert run.code == 1 and codes(run) == ["authority_state_invalid"]
    assert live.state().states["statements"] == "fenced"


def test_a_wrong_release_sha_is_refused_before_anything(live):
    run = live.run(COMMAND, approved=OTHER_SHA)
    assert run.code == 1 and codes(run) == ["release_sha_mismatch"]
    assert live.state().states["statements"] == "shadow"


@pytest.mark.parametrize("case", CASES)
def test_an_unpinned_pepper_is_pinned_then_statements_activate(tmp_path, case):
    """``init`` ran before the pepper was configured: activation pins the configured one (the
    same write-once pin ``init`` makes), and a different pepper afterwards would mismatch."""
    world = World(tmp_path, case)
    assert world.run("init").code == 0  # no pepper configured yet: nothing pinned
    good_evidence(world)
    assert world.run("activate").code == 0
    after_open(world)
    first_write(world)
    world.settings.os_statement_pepper = PEPPER
    run = world.run(COMMAND)
    assert run.code == 0 and run.out["outcome"] == "activated"
    with pytest.raises(api.LedgerError):
        world.env.pin_statement_pepper(WORKSPACE, cli.pepper_fingerprint("Q" * 48))


def test_a_short_pepper_is_refused_without_printing_it(live, caplog):
    live.settings.os_statement_pepper = "short"
    with caplog.at_level(logging.DEBUG):
        run = live.run(COMMAND)
    assert run.code == 1 and codes(run) == ["pepper_invalid"]
    assert "short" not in json.dumps(run.out) + run.err


def test_nothing_printed_or_logged_carries_the_pepper_or_its_fingerprint(live, caplog):
    fingerprint = cli.pepper_fingerprint(PEPPER)
    with caplog.at_level(logging.DEBUG):
        ok = live.run(COMMAND)
    live.settings.os_statement_pepper = "Z" * 48
    live.env.transition(WORKSPACE, "statements", "fenced", expected_epoch=1,
                        actor_owner_id=OWNER_ID)
    with caplog.at_level(logging.DEBUG):
        refused = live.run(COMMAND)
    text = json.dumps(ok.out) + ok.err + json.dumps(refused.out) + refused.err + caplog.text
    text += json.dumps(live.get(cli.STATEMENTS_ACTIVATION))
    for secret in (PEPPER, fingerprint, "Z" * 48):
        assert secret not in text


def test_rollback_after_statements_activation_is_still_refused(live):
    """The no_first_write rule means RB-1 can never meet an active statements domain."""
    assert live.run(COMMAND).code == 0
    run = live.run("rollback")
    assert run.code == 1 and codes(run) == ["rollback_after_first_write"]
    assert live.state().states == {"core": "active", "statements": "active"}
