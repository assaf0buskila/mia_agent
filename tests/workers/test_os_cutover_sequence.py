"""WP12 / E36: T6 -> T7 reaches the first-write boundary with the freeze still ON.

Uses the existing cutover evidence/CLI world, real ledger/authority/outbox and CLI SQL probes,
and the mirror suite's fake workbook. PostgreSQL cases exist only when its URL is set, just
like test_os_authority_live_pg: CI must execute them, never skip them. No real Sheets calls.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta

import pytest
from app.db.os_ledger_migrate import RUNTIME_ROLE
from app.services.os_ledger import api
from app.services.os_ledger import mirror_projection as mp
from app.services.os_ledger.outbox import PgOutbox, destination_binding
from app.workers import os_authority as cli
from app.workers import os_mirror
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from tests.fakes.contexts import for_worker
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE
from tests.services.os_ledger.conftest import PG_URL_ENV, pg_enabled
from tests.workers.mirror_fakes import FakeSheets
from tests.workers.test_os_authority import (
    MANIFEST_DIGEST,
    World,
    apply_drain,
    codes,
    good_evidence,
    keys_of,
)


@pytest.fixture
def cutover(tmp_path, monkeypatch):
    world = World(tmp_path, "pg")
    # T1 keys are already applied; the freeze stays on even during the T5 import.
    apply_drain(world)
    world.env.knobs.freeze = True
    world.env.knobs.backend = "sheets"
    world.settings.kill_switch = False
    world.settings.assaf_os_mirror_enabled = False
    world.settings.assaf_os_workspace_id = WORKSPACE
    world.settings.os_mirror_spreadsheet_id = "wp12-fake-mirror"
    url = os.environ[PG_URL_ENV]
    runtime_url = make_url(url).set(username=RUNTIME_ROLE, password=None)
    world.settings.database_url = make_url(url).set(
        username="v216_main", password=None,
    ).render_as_string(hide_password=False)
    world.settings.owner_os_ledger_database_url = runtime_url.render_as_string(hide_password=False)

    # Only the external/evidence probes stay fake. Status and activation observe real SQL.
    probes = cli.LiveProbes(
        world.settings, OWNER_ID, main_url="", ledger_url="", privileged_url=url,
        now=lambda: world.clock[0],
    )
    for name in ("mirror", "non_import_receipts", "import_promoted"):
        monkeypatch.setattr(world.probes, name, getattr(probes, name))
    engine = create_engine(runtime_url)

    def still_frozen():
        assert world.settings.assaf_os_freeze is True
        assert world.env.knobs.freeze is True

    sheets = FakeSheets(mp.MIRROR_TABS, guard=still_frozen)
    deps = os_mirror.MirrorDeps(
        reader=world.env, context=lambda: for_worker(
            "mirror", epoch=world.settings.assaf_os_authority_epoch,
        ),
        outbox=PgOutbox(engine, WORKSPACE), transport=sheets,
        destination_binding=destination_binding(world.settings.os_mirror_spreadsheet_id),
    )
    try:
        assert world.run("init").code == 0
        good_evidence(world)
        assert probes.import_promoted(WORKSPACE, 1, MANIFEST_DIGEST) is True
        before = world.run("status")
        assert before.code == 0
        assert before.out["states"]["core"] == "shadow"
        assert before.out["settings"]["backend"] == "sheets"
        assert before.out["settings"]["freeze"] is True
        assert before.out["non_import_receipts"] == 0
        assert before.out["mirror"] == "empty"
        assert len(world.env.rows("os_outbox")) == 1, "T5 queued the import projection"
        yield world, deps, sheets
        still_frozen()
    finally:
        probes.close()
        engine.dispose()


def activate_for_t7(world):
    preflight = world.run("preflight")
    assert preflight.code == 0 and preflight.out["ready_to_activate"] is True
    activated = world.run("activate")
    assert activated.code == 0 and activated.out["outcome"] == "activated"
    assert activated.out["states"]["core"] == "active"
    switch = world.run("freeze-plan", "--phase", "switch")
    assert switch.code == 0 and keys_of(switch) == ["MIA_ASSAF_OS_BACKEND=postgres"]
    world.settings.assaf_os_backend = "postgres"
    world.env.knobs.backend = "postgres"
    mirror = world.run("freeze-plan", "--phase", "mirror")
    assert mirror.code == 0 and keys_of(mirror) == ["MIA_ASSAF_OS_MIRROR_ENABLED=true"]
    world.settings.assaf_os_mirror_enabled = True


if pg_enabled():

    def test_t6_t7_verifies_mirror_before_first_write_while_frozen(cutover):
        world, deps, sheets = cutover
        activate_for_t7(world)

        publisher = os_mirror.MirrorPublisher(
            settings_provider=lambda: world.settings, deps_factory=lambda _: deps,
            clock=lambda: world.clock[0], lease_owner="wp12",
        )
        result = publisher.tick()
        boundary = world.run("status")
        assert boundary.code == 0
        assert boundary.out["settings"]["freeze"] is True
        assert boundary.out["settings"]["backend"] == "postgres"
        assert boundary.out["settings"]["epoch_matches"] is True
        assert boundary.out["states"]["core"] == "active"
        assert boundary.out["epoch"] == 1
        assert boundary.out["non_import_receipts"] == 0
        assert boundary.out["mirror"] == "verified", result
        assert (result.outcome, result.reason, result.events) == ("verified", "readback_equal", 1)
        assert len(sheets.writes) == sheets.reads == 1
        assert {mp.tab_of(a1) for a1, _ in sheets.writes[0]} == set(mp.MIRROR_TABS)
        assert f"TAS_p1_{MANIFEST_DIGEST[:6]}" in sheets.all_cells()
        assert [row["status"] for row in world.env.rows("os_outbox")] == ["sent"]
        state, = world.env.rows("os_mirror_state")
        assert state["verified_version"] == state["projected_version"] == boundary.out["version"]
        world.clock[0] += timedelta(seconds=900)
        assert publisher.tick() == os_mirror.TickResult("verified", "readback_equal", 0)
        assert world.run("status").out["mirror"] == "verified"
        assert len(sheets.writes) == 1 and sheets.reads == 2

    @pytest.mark.parametrize("retry_failure", [None, "target_changed", "deps_failed"])
    def test_rollback_rearm_import_requires_current_mirror_verification(
        cutover, retry_failure,
    ):
        world, deps, sheets = cutover
        activate_for_t7(world)
        publisher = os_mirror.MirrorPublisher(
            settings_provider=lambda: world.settings, deps_factory=lambda _: deps,
            clock=lambda: world.clock[0], lease_owner="wp12-retry",
        )
        assert publisher.tick().outcome == "verified"
        old_version = world.run("status").out["version"]
        assert world.run("status").out["mirror"] == "verified"
        assert world.run("rollback").code == 0
        before = world.env.rows("os_outbox")
        assert publisher.tick() == os_mirror.TickResult("paused", "os_frozen")
        assert world.env.rows("os_outbox") == before
        assert len(sheets.writes) == sheets.reads == 1

        assert world.run("init", "--rearm").out["epoch"] == 2
        world.settings.assaf_os_backend = world.env.knobs.backend = "sheets"
        world.settings.assaf_os_mirror_enabled = False
        apply_drain(world, epoch=2)
        world.env.knobs.settings_epoch = 2
        good_evidence(world)  # the fresh T5 import advances the ledger version at epoch 2
        for name in (cli.DRAIN, cli.PREFLIGHT):
            world.put(name, {**world.get(name), "epoch": 2})
        behind = world.run("status")
        assert behind.out["epoch"] == 2 and behind.out["version"] > old_version
        assert behind.out["mirror"] == "unverified", "old verification is behind T5"
        # A clean old projection is sufficient for activation, but not for T7's status.
        activate_for_t7(world)
        assert world.run("status").out["mirror"] == "unverified"
        assert len(sheets.writes) == sheets.reads == 1

        if retry_failure == "target_changed":
            world.settings.os_mirror_spreadsheet_id = "another-wp12-mirror"
            retry_deps = replace(deps, destination_binding=destination_binding(
                world.settings.os_mirror_spreadsheet_id,
            ))
            retry = os_mirror.MirrorPublisher(
                settings_provider=lambda: world.settings, deps_factory=lambda _: retry_deps,
                clock=lambda: world.clock[0],
            )
            assert retry.tick() == os_mirror.TickResult("refused", "mirror_destination_changed")
        elif retry_failure == "deps_failed":
            def fail_deps(_settings):
                raise RuntimeError("deps unavailable")

            retry = os_mirror.MirrorPublisher(
                settings_provider=lambda: world.settings, deps_factory=fail_deps,
                clock=lambda: world.clock[0],
            )
            assert retry.tick() == os_mirror.TickResult("deferred", "deps_failed")
        if retry_failure is not None:
            assert world.run("status").out["mirror"] == "unverified"
            assert len(sheets.writes) == sheets.reads == 1
            world.settings.os_mirror_spreadsheet_id = "wp12-fake-mirror"

        assert publisher.tick() == os_mirror.TickResult("verified", "readback_equal", 1)
        boundary = world.run("status")
        assert boundary.out["epoch"] == 2 and boundary.out["mirror"] == "verified"
        assert boundary.out["non_import_receipts"] == 0
        state, = world.env.rows("os_mirror_state")
        assert state["verified_version"] == state["projected_version"] == boundary.out["version"]
        assert state["verified_version"] > old_version
        assert len(sheets.writes) == sheets.reads == 2

    @pytest.mark.parametrize("core", ["shadow", "fenced"])
    def test_unactivated_core_cannot_publish_under_freeze(cutover, core, monkeypatch):
        world, deps, sheets = cutover
        if core == "fenced":
            assert world.run("fence").code == 0
        assert world.state().states["core"] == core
        # T6/T7 plans refuse this sequence. Even if settings are applied out of order,
        # the worker must independently keep the frozen, unactivated ledger paused.
        for phase in ("switch", "mirror"):
            refused = world.run("freeze-plan", "--phase", phase)
            assert refused.code == 1 and codes(refused) == ["authority_state_invalid"]
        world.settings.assaf_os_backend = "postgres"
        world.env.knobs.backend = "postgres"
        world.settings.assaf_os_mirror_enabled = True
        before = world.env.rows("os_outbox")
        observed = []
        read_authority = api.core_authority_state

        def authority_spy(settings):
            state = read_authority(settings)
            observed.append(state)
            return state

        monkeypatch.setattr(api, "core_authority_state", authority_spy)
        result = os_mirror.run_once(
            deps, world.settings, now=world.clock[0], lease_owner="wp12-negative",
            anti_entropy=False,
        )
        assert (result.outcome, result.reason) == ("paused", "os_frozen")
        assert observed == [core], "the real authority read succeeded with the expected state"
        boundary = world.run("status")
        assert boundary.code == 0
        assert boundary.out["settings"]["freeze"] is True
        assert boundary.out["states"]["core"] == core
        assert boundary.out["mirror"] == "empty"
        assert boundary.out["non_import_receipts"] == 0
        assert world.env.rows("os_outbox") == before
        assert world.env.rows("os_mirror_state") == []
        assert sheets.writes == [] and sheets.reads == 0
