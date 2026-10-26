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
from app.services.os_ledger import api, authority
from app.services.os_ledger import mirror_projection as mp
from app.services.os_ledger.outbox import PgOutbox, destination_binding
from app.workers import os_authority as cli
from app.workers import os_mirror
from sqlalchemy import create_engine, text
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
    admin_engine = create_engine(url)
    world.deps.admin = authority.PgAuthorityAdmin(admin_engine)

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
        admin_engine.dispose()


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


def revert_and_reimport(world):
    reverted = world.run("freeze-plan", "--phase", "revert")
    assert reverted.code == 0
    for key in keys_of(reverted):
        name, value = key.split("=", 1)
        attr = name.removeprefix("MIA_").lower()
        setattr(world.settings, attr, {"false": False, "0": 0}.get(value, value))
    world.env.knobs.backend = "sheets"
    world.env.knobs.settings_epoch = 0
    world.settings.assaf_os_freeze = world.env.knobs.freeze = False
    assert world.run("init", "--rearm").out["epoch"] == 2
    drain = world.run("freeze-plan", "--phase", "drain")
    assert drain.code == 0 and "MIA_ASSAF_OS_AUTHORITY_EPOCH=2" in keys_of(drain)
    apply_drain(world, epoch=2)
    world.env.knobs.freeze = True
    world.env.knobs.settings_epoch = 2
    # The external drain/fence/capture probes are fakes; the fresh import and CLI are real.
    good_evidence(world)
    for name in (cli.DRAIN, cli.PREFLIGHT):
        world.put(name, {**world.get(name), "epoch": 2})


def mirror_publisher(world, deps):
    return os_mirror.MirrorPublisher(
        settings_provider=lambda: world.settings, deps_factory=lambda _: deps,
        clock=lambda: world.clock[0], lease_owner="wp1b",
    )


def set_leftover(world, status="conflict"):
    engine = create_engine(os.environ[PG_URL_ENV])
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE assaf_os.os_mirror_state SET status=:status, last_error_code='drift' "
                "WHERE workspace_id=:ws"
            ), {"ws": WORKSPACE, "status": status})
    finally:
        engine.dispose()


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
        assert len(sheets.writes) == 1 and sheets.reads == 2
        assert {mp.tab_of(a1) for a1, _ in sheets.writes[0]} == set(mp.MIRROR_TABS)
        assert f"TAS_p1_{MANIFEST_DIGEST[:6]}" in sheets.all_cells()
        assert [row["status"] for row in world.env.rows("os_outbox")] == ["sent"]
        state, = world.env.rows("os_mirror_state")
        assert state["verified_version"] == state["projected_version"] == boundary.out["version"]
        world.clock[0] += timedelta(seconds=900)
        assert publisher.tick() == os_mirror.TickResult("verified", "readback_equal", 0)
        assert world.run("status").out["mirror"] == "verified"
        assert len(sheets.writes) == 1 and sheets.reads == 3

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
        assert len(sheets.writes) == 1 and sheets.reads == 2

        revert_and_reimport(world)
        behind = world.run("status")
        assert behind.out["epoch"] == 2 and behind.out["version"] > old_version
        assert behind.out["mirror"] == "unverified", "old verification is behind T5"
        # A clean old projection is sufficient for activation, but not for T7's status.
        activate_for_t7(world)
        assert world.run("status").out["mirror"] == "unverified"
        assert len(sheets.writes) == 1 and sheets.reads == 2

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
            assert len(sheets.writes) == 1 and sheets.reads == 2
            world.settings.os_mirror_spreadsheet_id = "wp12-fake-mirror"

        assert publisher.tick() == os_mirror.TickResult("verified", "readback_equal", 1)
        boundary = world.run("status")
        assert boundary.out["epoch"] == 2 and boundary.out["mirror"] == "verified"
        assert boundary.out["non_import_receipts"] == 0
        state, = world.env.rows("os_mirror_state")
        assert state["verified_version"] == state["projected_version"] == boundary.out["version"]
        assert state["verified_version"] > old_version
        assert len(sheets.writes) == 2 and sheets.reads == 3

    def test_open_rollback_anti_entropy_revert_rearm_reaches_verified_t7(cutover):
        world, deps, sheets = cutover
        activate_for_t7(world)
        publisher = mirror_publisher(world, deps)
        assert publisher.tick().outcome == "verified"
        old_version = world.state().version
        assert world.run("freeze-plan", "--phase", "open").code == 0
        world.settings.assaf_os_freeze = world.env.knobs.freeze = False
        assert world.run("rollback").code == 0
        world.clock[0] += timedelta(seconds=900)
        assert publisher.tick() == os_mirror.TickResult("paused", "authority_fenced", 0)
        state, = world.env.rows("os_mirror_state")
        assert (state["status"], state["projected_version"], state["verified_version"],
                state["last_error_code"]) == (
                    "fenced", old_version, old_version, "authority_fenced")
        assert len(sheets.writes) == 1 and sheets.reads == 2
        revert_and_reimport(world)
        assert publisher.tick() == os_mirror.TickResult("paused", "disabled")
        assert world.run("status").out["mirror"] == "unverified"
        activate_for_t7(world)
        assert publisher.tick() == os_mirror.TickResult("verified", "readback_equal", 1)
        boundary = world.run("status").out
        assert boundary["epoch"] == 2 and boundary["mirror"] == "verified"
        assert boundary["version"] > old_version and boundary["non_import_receipts"] == 0
        state, = world.env.rows("os_mirror_state")
        assert state["status"] == "synced" and state["last_error_code"] is None
        assert state["projected_version"] == state["verified_version"] == boundary["version"]
        assert len(sheets.writes) == 2 and sheets.reads == 3

    def test_crash_after_fenced_clean_attempt_blocks_pre_activation_probe(cutover):
        world, deps, sheets = cutover
        activate_for_t7(world)
        publisher = mirror_publisher(world, deps)
        assert publisher.tick().outcome == "verified"
        version = world.state().version
        assert world.run("freeze-plan", "--phase", "open").code == 0
        world.settings.assaf_os_freeze = world.env.knobs.freeze = False
        assert world.run("rollback").code == 0
        world.clock[0] += timedelta(seconds=900)
        assert publisher.tick() == os_mirror.TickResult("paused", "authority_fenced", 0)
        assert world.probes.mirror(WORKSPACE, None) == "verified"
        writes, reads = len(sheets.writes), sheets.reads
        deps.outbox.record_attempt(version, world.clock[0])
        revert_and_reimport(world)
        # Crash before record_result, even if this same-version write reached the workbook.
        assert world.probes.mirror(WORKSPACE, None) == "unverified"
        refused = world.run("preflight")
        assert refused.code == 1 and codes(refused) == ["mirror_not_verified"]
        assert world.run("activate").code == 1
        assert (len(sheets.writes), sheets.reads) == (writes, reads)

    def test_drift_survives_open_rollback_tick_until_guarded_reset_and_t7(cutover):
        world, deps, sheets = cutover
        activate_for_t7(world)
        publisher = mirror_publisher(world, deps)
        assert publisher.tick().outcome == "verified"
        version = world.state().version
        sheets.grid[mp.ENTITY_TABS["tasks"]][(50, 30)] = "hand-edit"
        world.clock[0] += timedelta(seconds=900)
        assert publisher.tick() == os_mirror.TickResult("conflict", "drift", 0)
        state, = world.env.rows("os_mirror_state")
        assert (state["status"], state["projected_version"], state["verified_version"],
                state["last_error_code"]) == ("conflict", version, version, "drift")
        assert world.run("freeze-plan", "--phase", "open").code == 0
        world.settings.assaf_os_freeze = world.env.knobs.freeze = False
        assert world.run("rollback").code == 0
        world.clock[0] += timedelta(seconds=900)
        assert publisher.tick().outcome == "paused"
        revert_and_reimport(world)
        refused = world.run("preflight")
        assert refused.code == 1 and codes(refused) == ["mirror_not_verified"]
        state, = world.env.rows("os_mirror_state")
        assert state["last_error_code"] == "authority_fenced_unclean"
        assert "hand-edit" in sheets.all_cells()
        writes, reads = len(sheets.writes), sheets.reads
        reset = world.run("mirror-reset", "--expected-epoch", "2")
        assert reset.code == 0 and reset.out["outcome"] == "reset"
        assert len(sheets.writes) == writes and sheets.reads == reads
        assert "hand-edit" in sheets.all_cells(), "reset changes progress only"
        ready = world.run("preflight")
        assert ready.code == 0 and ready.out["ready_to_activate"] is True
        activate_for_t7(world)
        assert mirror_publisher(world, deps).tick().outcome == "verified"
        assert len(sheets.writes) == writes + 1 and sheets.reads == reads + 2
        assert {mp.tab_of(a1) for a1, _ in sheets.writes[-1]} == set(mp.MIRROR_TABS)
        assert "hand-edit" not in sheets.all_cells()
        assert world.run("status").out["mirror"] == "verified"

    @pytest.mark.parametrize("leftover", ["conflict", "unknown", "pending", "fenced"])
    def test_uncertain_leftover_reset_is_idempotent_and_t7_reproves(cutover, leftover):
        world, deps, sheets = cutover
        activate_for_t7(world)
        assert mirror_publisher(world, deps).tick().outcome == "verified"
        assert world.run("rollback").code == 0
        set_leftover(world, leftover)
        revert_and_reimport(world)
        refused = world.run("preflight")
        assert refused.code == 1 and codes(refused) == ["mirror_not_verified"]
        assert world.run("activate").code == 1
        before = world.env.rows("os_mirror_state")[0]
        outbox = world.env.rows("os_outbox")
        receipts = world.env.rows("os_receipts")
        version = world.state().version
        for expected in ("reset", "already_empty"):
            reset = world.run("mirror-reset", "--expected-epoch", "2")
            assert reset.code == 0 and reset.out["outcome"] == expected, reset.err
        state, = world.env.rows("os_mirror_state")
        assert state["destination_binding"] == before["destination_binding"]
        assert state["status"] == "pending"
        assert state["projected_version"] == state["verified_version"] == 0
        assert state["last_attempt_at"] is state["last_verified_at"] is None
        assert state["last_error_code"] is None
        assert state["watermark"] == state["scan_coverage"] == {}
        assert world.state().version == version
        assert world.env.rows("os_outbox") == outbox
        assert world.env.rows("os_receipts") == receipts
        audits = list(world.dir.glob("mirror-reset-*.json"))
        assert audits and all(world.get(p.name)["kind"] == "mirror_reset" for p in audits)
        assert len(sheets.writes) == 1 and sheets.reads == 2, "reset never calls the workbook"
        assert world.run("preflight").out["ready_to_activate"] is True
        assert world.run("status").out["mirror"] != "verified"
        # Even an equal workbook must be rewritten, never accepted by a retry read shortcut.
        activate_for_t7(world)
        assert mirror_publisher(world, deps).tick().outcome == "verified"
        assert len(sheets.writes) == 2
        assert {mp.tab_of(a1) for a1, _ in sheets.writes[-1]} == set(mp.MIRROR_TABS)
        assert world.run("status").out["mirror"] == "verified"
        state, = world.env.rows("os_mirror_state")
        assert state["verified_version"] == state["projected_version"] == version

    @pytest.mark.parametrize("guard,reason", [
        ("freeze_off", "freeze_off"), ("active", "authority_changed:core_active"),
        ("epoch", "authority_changed:epoch_mismatch"), ("sha", "release_sha_mismatch"),
        ("destination", "authority_changed:mirror_destination_changed"),
    ])
    def test_mirror_reset_refuses_without_changing_state(cutover, guard, reason):
        world, deps, sheets = cutover
        activate_for_t7(world)
        assert mirror_publisher(world, deps).tick().outcome == "verified"
        if guard != "active":
            assert world.run("rollback").code == 0
        if guard == "freeze_off":
            world.settings.assaf_os_freeze = False
        if guard == "destination":
            world.settings.os_mirror_spreadsheet_id = "another-destination"
        before = world.env.rows("os_mirror_state")
        result = world.run(
            "mirror-reset", "--expected-epoch", "2" if guard == "epoch" else "1",
            approved="b" * 40 if guard == "sha" else "a" * 40,
        )
        world.settings.assaf_os_freeze = True
        assert result.code == 1 and codes(result) == [reason]
        assert world.env.rows("os_mirror_state") == before
        assert not list(world.dir.glob("mirror-reset-*.json"))
        assert len(sheets.writes) == 1 and sheets.reads == 2

    @pytest.mark.parametrize("epoch_args", [[], ["--expected-epoch", "0"]])
    def test_mirror_reset_requires_a_positive_epoch(cutover, epoch_args):
        world, _, sheets = cutover
        before = world.env.rows("os_mirror_state")
        refused = world.run("mirror-reset", *epoch_args)
        assert refused.code == 1 and codes(refused) == ["expected_epoch_required"]
        assert world.env.rows("os_mirror_state") == before
        assert sheets.writes == [] and sheets.reads == 0

    def test_mirror_reset_audit_failure_leaves_progress_intact(cutover, monkeypatch):
        world, deps, sheets = cutover
        activate_for_t7(world)
        assert mirror_publisher(world, deps).tick().outcome == "verified"
        assert world.run("rollback").code == 0
        set_leftover(world)
        before = world.env.rows("os_mirror_state")

        def cannot_audit(_name, _document):
            raise cli.Refusal("evidence_unwritable")

        monkeypatch.setattr(world.deps.evidence, "write", cannot_audit)
        refused = world.run("mirror-reset", "--expected-epoch", "1")
        assert refused.code == 1 and codes(refused) == ["evidence_unwritable"]
        assert world.env.rows("os_mirror_state") == before
        assert len(sheets.writes) == 1 and sheets.reads == 2

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
