"""V2-31: the os-authority CLI on the FakeLedger (V2_PG_CONTRACTS.md section 16 and section 20).

Everything runs on fakes: the in-memory ledger (its ``AuthorityAdmin``), fake probes and a
temporary evidence directory. Nothing here touches a database, a network or a secret store.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest
from app.services.os_ledger import api
from app.workers import os_authority as cli

from tests.fakes.os_ledger_builders import commit_direct, commit_import, ctx, task
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE, build_fake_env

SHA = "a" * 40
OTHER_SHA = "b" * 40
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SECRET = "SECRET-hmac-" + "x" * 40
PEPPER = "PEPPER-secret-" + "y" * 40
DRIVER_TEXT = "driver-text-with-" + SECRET


def at(**delta: float) -> datetime:
    return NOW + timedelta(**delta)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Run(NamedTuple):
    code: int
    out: Any  # parsed JSON, or the raw text for freeze-plan
    err: str


class FakeProbes:
    def __init__(self, env):
        self.env = env
        self.fence = ("unavailable", "")
        self.main = True
        self.ledger = True
        self.cards = 0
        self.unknown = 0
        self.pending = 0
        self.mirror_state = "empty"
        self.broken: set[str] = set()
        self.snapshots = 0
        self.closed = False

    def _use(self, name):
        if name in self.broken:
            raise RuntimeError(DRIVER_TEXT)

    def sheets_fence(self):
        self._use("fence")
        return self.fence

    def sheets_snapshot_pending(self):
        self._use("snapshot")
        self.snapshots += 1
        return self.pending

    def main_role_safe(self):
        self._use("main")
        return self.main

    def ledger_role_safe(self):
        self._use("ledger")
        return self.ledger

    def open_cards(self):
        self._use("cards")
        return self.cards

    def unknown_receipts(self):
        self._use("unknown")
        return self.unknown

    def mirror(self, workspace):
        self._use("mirror")
        return self.mirror_state

    def non_import_receipts(self, workspace, epoch):
        self._use("receipts")
        return sum(
            1 for row in self.env.rows("os_receipts")
            if row["kind"] != "import" and row["epoch"] == epoch
        )

    def close(self):
        self.closed = True


class World:
    def __init__(self, tmp_path):
        self.env = build_fake_env()
        self.settings = SimpleNamespace(
            assaf_os_freeze=True, os_statement_pepper="", assaf_os_backend="sheets",
            assaf_os_ledger_enabled=False, assaf_os_authority_epoch=0,
            assaf_os_hmac_secret=SECRET,
        )
        self.probes = FakeProbes(self.env)
        self.dir = tmp_path / "authority"
        self.clock = [NOW]
        self.deps = cli.Deps(
            settings=self.settings, admin=self.env, probes=self.probes,
            evidence=cli.EvidenceStore(self.dir), now=lambda: self.clock[0], owner_id=OWNER_ID,
            workspace=WORKSPACE, build_sha=SHA,
        )

    def run(self, *argv: str, approved: str = SHA, build: str = SHA) -> Run:
        out, err = io.StringIO(), io.StringIO()
        code = cli.run(
            [*argv, "--approved-sha", approved], env={"MIA_BUILD_SHA": build},
            deps_factory=lambda env: self.deps, stdout=out, stderr=err,
        )
        text = out.getvalue()
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = text
        return Run(code, parsed, err.getvalue())

    def put(self, name: str, document: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_text(json.dumps(document), encoding="utf-8")

    def get(self, name: str) -> Any:
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def drop(self, name: str) -> None:
        (self.dir / name).unlink()

    def state(self) -> api.AuthoritySnapshot:
        return self.env.state(WORKSPACE)


def signed_manifest(captured: datetime, signed: datetime, **over: Any) -> dict:
    manifest = {"counts": {"leads": 3, "clients": 2}, "ids": ["LEA_1", "LEA_2"]}
    digest = api.digest(manifest)
    document = {
        "v": 1, "manifest": manifest, "manifest_digest": digest,
        "xlsx_captured_at": stamp(captured),
        "signoff": {
            "owner_id": OWNER_ID, "signed_at": stamp(signed), "manifest_digest": digest,
            "xlsx_captured_at": stamp(captured), "drift_changed_cells": 0,
        },
    }
    for key, value in over.items():
        if key.startswith("signoff_"):
            document["signoff"][key.removeprefix("signoff_")] = value
        else:
            document[key] = value
    return document


def good_evidence(world: World) -> None:
    world.put(cli.BACKUP, {
        "healthy": True, "age_minutes": 10, "last_success_utc": stamp(at(minutes=-10)),
        "last_object": "15min/mia.dump.enc",
    })
    world.put(cli.DRILL, {
        "started_utc": stamp(at(days=-2)), "passed": True, "rto_met": True, "checks": [],
    })
    world.put(cli.FRONTEND, {
        "v": 1, "cloudflare_version": "ce8332db-4a1e", "bundle_sha256": "c" * 64,
        "recorded_at": stamp(at(days=-1)),
    })
    world.put(cli.JOURNAL, {
        "v": 1, "measured_at": stamp(at(hours=-5)), "max_rows": 2000,
        "tabs": {"_assaf_os_proposals": 32, "_assaf_os_receipts": 19, "_assaf_os_replay": 51},
    })
    world.put(cli.DRAIN, {
        "v": 1, "kind": "drain", "recorded_at": stamp(at(hours=-3)), "epoch": 1,
        "release_sha": SHA, "freeze": True, "sheets_pending": 0, "sheets_unknown_receipts": 0,
        "cards_open": 0,
    })
    world.put(cli.PREFLIGHT, {
        "v": 1, "kind": "preflight", "epoch": 1, "release_sha": SHA, "sheets": "unavailable",
        "fence_probe_at": stamp(at(hours=-2)), "last_probe_at": stamp(at(hours=-1)), "checks": [],
    })
    world.put(cli.MANIFEST, signed_manifest(at(minutes=-90), at(minutes=-30)))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def ready(world):
    assert world.run("init").code == 0
    good_evidence(world)
    return world


def codes(run: Run) -> list[str]:
    assert run.err.startswith("os_authority_refused:"), run.err
    return run.err.removeprefix("os_authority_refused:").strip().split(",")


# --------------------------------------------------------------------------- the contract table


def test_the_precondition_table_is_the_contract_table():
    assert cli.PRECONDITIONS == (
        "release_sha_mismatch", "sheets_not_fenced", "freeze_off", "capture_before_fence",
        "main_role_unsafe", "ledger_role_unsafe", "sheets_pending_proposals",
        "sheets_unknown_receipts", "approval_cards_open", "backup_too_old",
        "restore_drill_too_old", "mirror_not_verified", "manifest_not_signed",
        "fe_v2_reader_not_live", "journal_capacity",
    )
    assert cli.COMMANDS == (
        "init", "preflight", "freeze-plan", "activate", "fence", "rollback", "status",
    )


def test_the_all_green_world_activates_core_without_bumping_the_epoch(ready):
    before = ready.state()
    run = ready.run("activate")
    assert run.code == 0 and run.err == ""
    after = ready.state()
    assert (before.epoch, before.states["core"]) == (1, "shadow")
    assert (after.epoch, after.states["core"], after.states["statements"]) == (1, "active", "shadow")
    assert after.activated_at["core"] is not None and after.activated_at["statements"] is None
    assert [c["status"] for c in run.out["checks"]] == ["pass"] * len(cli.PRECONDITIONS)
    activation = ready.get(cli.ACTIVATION)
    assert (activation["epoch"], activation["release_sha"]) == (1, SHA)


def test_activate_is_idempotent_once_active_and_skips_the_checks(ready):
    assert ready.run("activate").code == 0
    ready.settings.assaf_os_freeze = False  # the switch step turned the freeze off
    ready.probes.fence = ("live", "")
    again = ready.run("activate")
    assert again.code == 0 and again.out["outcome"] == "already_active"
    assert ready.state().epoch == 1


BREAKERS = {
    "sheets_not_fenced": lambda w: setattr(w.probes, "fence", ("live", "")),
    "freeze_off": lambda w: setattr(w.settings, "assaf_os_freeze", False),
    "capture_before_fence": lambda w: w.put(
        cli.MANIFEST, signed_manifest(at(hours=-2, minutes=-1), at(minutes=-30))),
    "main_role_unsafe": lambda w: setattr(w.probes, "main", False),
    "ledger_role_unsafe": lambda w: setattr(w.probes, "ledger", False),
    "sheets_pending_proposals": lambda w: w.put(
        cli.DRAIN, {**w.get(cli.DRAIN), "sheets_pending": 2}),
    "sheets_unknown_receipts": lambda w: setattr(w.probes, "unknown", 1),
    "approval_cards_open": lambda w: setattr(w.probes, "cards", 1),
    "backup_too_old": lambda w: w.put(
        cli.BACKUP, {**w.get(cli.BACKUP), "last_success_utc": stamp(at(minutes=-61))}),
    "restore_drill_too_old": lambda w: w.put(
        cli.DRILL, {**w.get(cli.DRILL), "started_utc": stamp(at(days=-8))}),
    "mirror_not_verified": lambda w: setattr(w.probes, "mirror_state", "unverified"),
    "manifest_not_signed": lambda w: w.put(
        cli.MANIFEST, signed_manifest(at(minutes=-90), at(minutes=-30), signoff_manifest_digest="0" * 64)),
    "fe_v2_reader_not_live": lambda w: w.drop(cli.FRONTEND),
    "journal_capacity": lambda w: w.put(
        cli.JOURNAL, {**w.get(cli.JOURNAL), "tabs": {"_assaf_os_proposals": 1900}}),
}


def test_every_precondition_but_the_sha_has_a_breaker():
    assert set(BREAKERS) | {"release_sha_mismatch"} == set(cli.PRECONDITIONS)


@pytest.mark.parametrize("code", sorted(BREAKERS))
def test_each_precondition_refuses_alone_and_changes_nothing(ready, code):
    BREAKERS[code](ready)
    before = ready.state()
    run = ready.run("activate")
    assert run.code == 1 and codes(run) == [code]
    assert run.out["outcome"] == "refused"
    failing = [c["code"] for c in run.out["checks"] if c["status"] != "pass"]
    assert failing == [code]
    assert ready.state() == before and before.states["core"] == "shadow"
    assert not (ready.dir / cli.ACTIVATION).exists()


@pytest.mark.parametrize("code", sorted(BREAKERS))
def test_each_breaker_breaks_only_its_own_check(ready, monkeypatch, code):
    # Neutralize the one check: activation then succeeds, so the fixture broke nothing else.
    BREAKERS[code](ready)
    monkeypatch.setitem(cli._CHECKS, code, lambda c, s, _code=code: cli.Check(_code, "pass"))
    assert ready.run("activate").code == 0
    assert ready.state().states["core"] == "active"


def test_a_release_sha_that_is_not_the_image_refuses_before_anything_runs(ready):
    run = ready.run("activate", approved=OTHER_SHA)
    assert run.code == 1 and codes(run) == ["release_sha_mismatch"]
    assert ready.probes.snapshots == 0 and ready.state().states["core"] == "shadow"
    assert ready.run("status", approved="c" * 39).code == 1
    assert codes(ready.run("status", build=OTHER_SHA)) == ["release_sha_mismatch"]
    assert codes(ready.run("status", build="")) == ["release_sha_mismatch"]


def test_a_probe_error_fails_that_check_closed_and_leaks_nothing(ready, caplog):
    for name, code in (("fence", "sheets_not_fenced"), ("main", "main_role_unsafe"),
                       ("ledger", "ledger_role_unsafe"), ("unknown", "sheets_unknown_receipts"),
                       ("cards", "approval_cards_open"), ("mirror", "mirror_not_verified")):
        ready.probes.broken = {name}
        with caplog.at_level(logging.DEBUG):
            run = ready.run("activate")
        assert run.code == 1 and codes(run) == [code], name
        assert SECRET not in json.dumps(run.out) + run.err + caplog.text
        assert "driver-text" not in json.dumps(run.out) + run.err + caplog.text
    assert ready.state().states["core"] == "shadow"


@pytest.mark.parametrize("name,code", [
    (cli.BACKUP, "backup_too_old"), (cli.DRILL, "restore_drill_too_old"),
    (cli.FRONTEND, "fe_v2_reader_not_live"), (cli.JOURNAL, "journal_capacity"),
    (cli.DRAIN, "sheets_pending_proposals"), (cli.MANIFEST, "manifest_not_signed"),
])
def test_unreadable_evidence_fails_its_check_closed(ready, name, code):
    (ready.dir / name).write_text("{not json", encoding="utf-8")
    run = ready.run("activate")
    assert run.code == 1 and code in codes(run)
    assert ready.state().states["core"] == "shadow"


def test_a_symlinked_or_oversized_evidence_file_is_refused(ready, monkeypatch):
    path = ready.dir / cli.BACKUP
    monkeypatch.setattr(type(path), "is_symlink", lambda self: self.name == cli.BACKUP)
    assert codes(ready.run("activate")) == ["backup_too_old"]
    monkeypatch.undo()
    path.write_text(json.dumps({"pad": "x" * (cli.MAX_EVIDENCE_BYTES + 1)}), encoding="utf-8")
    assert codes(ready.run("activate")) == ["backup_too_old"]


def test_evidence_from_another_epoch_or_release_is_stale(ready):
    ready.put(cli.DRAIN, {**ready.get(cli.DRAIN), "release_sha": OTHER_SHA})
    assert codes(ready.run("activate")) == ["sheets_pending_proposals"]
    ready.put(cli.DRAIN, {**ready.get(cli.DRAIN), "release_sha": SHA, "epoch": 2})
    assert codes(ready.run("activate")) == ["sheets_pending_proposals"]
    ready.put(cli.DRAIN, {**ready.get(cli.DRAIN), "epoch": 1, "freeze": False})
    assert codes(ready.run("activate")) == ["sheets_pending_proposals"]
    ready.put(cli.DRAIN, {**ready.get(cli.DRAIN), "freeze": True, "recorded_at": stamp(at(hours=-1))})
    assert codes(ready.run("activate")) == ["sheets_pending_proposals"]  # drained after the fence
    ready.put(cli.PREFLIGHT, {**ready.get(cli.PREFLIGHT), "epoch": 2})
    run = ready.run("activate")
    assert {"capture_before_fence", "sheets_pending_proposals"} <= set(codes(run))


@pytest.mark.parametrize("over,reason", [
    ({"signoff_owner_id": "999"}, "signoff_mismatch"),
    ({"signoff_xlsx_captured_at": stamp(at(minutes=-91))}, "signoff_mismatch"),
    ({"signoff_signed_at": stamp(at(minutes=-100))}, "signoff_time"),
    ({"signoff_signed_at": stamp(at(minutes=30))}, "signoff_time"),
    ({"signoff_drift_changed_cells": 1}, "drift"),
    ({"signoff_drift_changed_cells": True}, "drift"),
    ({"manifest_digest": "0" * 64, "signoff_manifest_digest": "0" * 64}, "digest_mismatch"),
])
def test_a_sign_off_must_bind_digest_owner_time_and_the_drift_proof(ready, over, reason):
    ready.put(cli.MANIFEST, signed_manifest(at(minutes=-90), at(minutes=-30), **over))
    run = ready.run("activate")
    assert run.code == 1 and codes(run) == ["manifest_not_signed"]
    detail = next(c for c in run.out["checks"] if c["code"] == "manifest_not_signed")["detail"]
    assert detail == reason


def test_a_manifest_of_the_wrong_shape_is_not_signed(ready):
    document = signed_manifest(at(minutes=-90), at(minutes=-30))
    document["extra"] = 1
    ready.put(cli.MANIFEST, document)
    # Nothing in it can be trusted, so its capture time cannot prove the order either.
    run = ready.run("activate")
    assert codes(run) == ["capture_before_fence", "manifest_not_signed"]
    details = {c["code"]: c["detail"] for c in run.out["checks"] if c["status"] != "pass"}
    assert details == {"capture_before_fence": "no_capture_time", "manifest_not_signed": "shape"}


def test_the_journal_headroom_is_ninety_percent_of_the_cap(ready):
    for rows, ok in ((1800, True), (1801, False)):
        ready.put(cli.JOURNAL, {**ready.get(cli.JOURNAL), "tabs": {"_assaf_os_replay": rows}})
        assert (ready.run("preflight").code == 0) is ok
    ready.put(cli.JOURNAL, {**ready.get(cli.JOURNAL), "tabs": {"x": 1}, "measured_at": stamp(at(days=-8))})
    assert ready.run("preflight").code == 1


# --------------------------------------------------------------------------- init


def test_init_creates_epoch_one_and_is_idempotent(world):
    first = world.run("init")
    assert first.code == 0 and first.out["outcome"] == "created"
    assert (first.out["epoch"], first.out["states"]) == (
        1, {"core": "shadow", "statements": "shadow"})
    second = world.run("init")
    assert second.code == 0 and second.out["outcome"] == "existing"
    assert len(world.env.rows("os_workspaces")) == 1
    assert world.state().epoch == 1


def test_init_pins_the_pepper_fingerprint_once_and_never_prints_the_pepper(world, caplog):
    world.settings.os_statement_pepper = PEPPER
    with caplog.at_level(logging.DEBUG):
        run = world.run("init")
        again = world.run("init")
    assert run.code == 0 and run.out["pepper_pinned"] is True and again.code == 0
    expected = hmac.new(
        PEPPER.encode(), b"assaf-os-statement-pepper-fingerprint.v1", hashlib.sha256
    ).hexdigest()
    row = world.env.rows("os_workspaces")[0]
    assert row["statement_pepper_fp"] == expected == cli.pepper_fingerprint(PEPPER)
    assert PEPPER not in json.dumps(run.out) + run.err + caplog.text
    world.settings.os_statement_pepper = "Z" * 40
    changed = world.run("init")
    assert changed.code == 1 and codes(changed) == ["ledger_unavailable:statement_pepper_mismatch"]
    assert world.env.rows("os_workspaces")[0]["statement_pepper_fp"] == expected


def test_init_without_a_pepper_pins_nothing(world):
    run = world.run("init")
    assert run.out["pepper_pinned"] is False
    assert world.env.rows("os_workspaces")[0]["statement_pepper_fp"] is None


def test_a_short_pepper_is_refused_before_any_pin():
    with pytest.raises(cli.Refusal) as refused:
        cli.pepper_fingerprint("short")
    assert refused.value.codes == ["pepper_invalid"]


def test_init_refuses_a_workspace_owned_by_someone_else(world):
    world.env.ensure_workspace(WORKSPACE, "999", 1)
    run = world.run("init")
    assert run.code == 1 and codes(run) == ["authority_changed:workspace_conflict"]


def test_init_never_runs_before_the_workspace_is_known_to_exist(world):
    assert codes(world.run("activate")) == ["workspace_missing"]
    for command in ("status", "fence", "rollback"):
        assert codes(world.run(command)) == ["workspace_missing"], command
    assert codes(world.run("preflight")) == ["workspace_missing"]


# --------------------------------------------------------------------------- activate states


@pytest.mark.parametrize("path", [("fenced",), ("fenced", "restoring")])
def test_activate_only_runs_from_shadow(ready, path):
    for step in path:
        ready.env.transition(WORKSPACE, "core", step, expected_epoch=1, actor_owner_id=OWNER_ID)
    run = ready.run("activate")
    assert run.code == 1 and codes(run) == ["authority_state_invalid"]


# --------------------------------------------------------------------------- fence and rollback


def test_fence_refuses_every_ledger_write_and_is_idempotent(ready):
    ctx_ = ctx()
    assert ready.run("activate").code == 0
    commit_direct(ready.env, ctx_, [task()])  # a business write while active
    first = ready.run("fence")
    assert first.code == 0 and first.out["fenced"] == ["core"]
    assert ready.state().states == {"core": "fenced", "statements": "shadow"}
    with pytest.raises(api.LedgerError, match="^authority_not_active$"):
        commit_direct(ready.env, ctx_, [task("TAS_t2")])
    with pytest.raises(api.LedgerError, match="^authority_not_active$"):
        commit_import(ready.env, ctx_, [task("TAS_t3")], "d" * 64)
    again = ready.run("fence")
    assert again.code == 0 and again.out["fenced"] == []
    assert ready.state().epoch == 1


def test_fence_also_stops_an_active_statements_domain(ready):
    ready.env.transition(WORKSPACE, "statements", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    run = ready.run("fence")
    assert run.out["fenced"] == ["core", "statements"]
    assert ready.state().states == {"core": "fenced", "statements": "fenced"}


def test_fence_refuses_a_restoring_ledger(ready):
    for step in ("fenced", "restoring"):
        ready.env.transition(WORKSPACE, "core", step, expected_epoch=1, actor_owner_id=OWNER_ID)
    assert codes(ready.run("fence")) == ["authority_state_invalid"]


def test_rollback_before_the_first_write_leaves_only_import_rows(ready):
    commit_import(ready.env, ctx(), [task("TAS_i1")], "e" * 64)
    assert ready.run("activate").code == 0
    run = ready.run("rollback")
    assert run.code == 0 and run.out["outcome"] == "fenced"
    assert ready.state().states["core"] == "fenced" and ready.state().epoch == 1
    assert {r["kind"] for r in ready.env.rows("os_receipts")} == {"import"}
    steps = " ".join(run.out["next_steps"])
    assert "MIA_ASSAF_OS_BACKEND=sheets" in steps and "MIA_ASSAF_OS_AUTHORITY_EPOCH=0" in steps
    assert "ASSAF_OS_MODE back to LIVE" in steps and "init --rearm" in steps
    assert ready.run("rollback").code == 0  # idempotent


def test_rollback_after_the_first_business_write_is_refused_and_changes_nothing(ready):
    commit_import(ready.env, ctx(), [task("TAS_i1")], "e" * 64)
    assert ready.run("activate").code == 0
    commit_direct(ready.env, ctx(), [task("TAS_w1")])
    run = ready.run("rollback")
    assert run.code == 1 and codes(run) == ["rollback_after_first_write"]
    assert ready.state().states["core"] == "active"
    assert ready.run("fence").code == 0  # roll forward: fence is still available


def test_rollback_refuses_a_restoring_ledger(ready):
    for step in ("fenced", "restoring"):
        ready.env.transition(WORKSPACE, "core", step, expected_epoch=1, actor_owner_id=OWNER_ID)
    assert codes(ready.run("rollback")) == ["authority_state_invalid"]


def test_a_rolled_back_cutover_re_arms_with_one_idempotent_epoch_bump(ready):
    commit_import(ready.env, ctx(), [task("TAS_i1")], "e" * 64)
    assert ready.run("activate").code == 0
    assert ready.run("rollback").code == 0
    rearmed = ready.run("init", "--rearm")
    assert rearmed.code == 0 and rearmed.out["outcome"] == "rearmed"
    assert (rearmed.out["epoch"], rearmed.out["states"]["core"]) == (2, "shadow")
    repeat = ready.run("init", "--rearm")
    assert repeat.code == 0 and repeat.out["outcome"] == "already_shadow"
    assert ready.state().epoch == 2
    # A caller still holding epoch 1 replays the bumping edge: a no-op at the new epoch.
    snapshot = ready.env.transition(
        WORKSPACE, "core", "shadow", expected_epoch=1, actor_owner_id=OWNER_ID)
    assert snapshot.epoch == 2
    # The next attempt re-imports at the new epoch and activates without another bump.
    ready.env.knobs.settings_epoch = 2
    commit_import(ready.env, ctx(epoch=2), [task("TAS_i2")], "f" * 64, epoch=2)
    good_evidence(ready)
    for name, doc in ((cli.DRAIN, ready.get(cli.DRAIN)), (cli.PREFLIGHT, ready.get(cli.PREFLIGHT))):
        ready.put(name, {**doc, "epoch": 2})
    assert ready.run("activate").code == 0
    assert (ready.state().epoch, ready.state().states["core"]) == (2, "active")


def test_re_arm_is_refused_unless_fenced_and_never_after_a_business_write(ready):
    assert ready.run("activate").code == 0
    assert codes(ready.run("init", "--rearm")) == ["authority_state_invalid"]
    commit_direct(ready.env, ctx(), [task("TAS_w1")])
    ready.run("fence")
    assert codes(ready.run("init", "--rearm")) == ["rearm_after_first_write"]
    assert ready.state().epoch == 1


# --------------------------------------------------------------------------- preflight


def test_preflight_before_the_import_reports_pending_not_a_failure(world):
    world.run("init")
    good_evidence(world)
    world.drop(cli.MANIFEST)
    run = world.run("preflight")
    assert run.code == 0 and run.out["ready_to_activate"] is False
    statuses = {c["code"]: c["status"] for c in run.out["checks"]}
    assert statuses["manifest_not_signed"] == "pending" == statuses["capture_before_fence"]
    assert {s for c, s in statuses.items() if c not in {"manifest_not_signed",
                                                        "capture_before_fence"}} == {"pass"}
    assert codes(world.run("activate")) == ["capture_before_fence", "manifest_not_signed"]


def test_preflight_never_takes_the_mutating_snapshot(ready):
    ready.run("preflight")
    ready.run("activate")
    assert ready.probes.snapshots == 0


def test_preflight_records_the_earliest_unbroken_fence_probe(world):
    world.run("init")
    world.clock[0] = at(hours=-5)
    world.run("preflight")
    first = world.get(cli.PREFLIGHT)["fence_probe_at"]
    assert first == stamp(at(hours=-5))
    world.clock[0] = at(hours=-4)
    world.run("preflight")
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == first  # kept while unbroken
    world.probes.fence = ("live", "")
    world.clock[0] = at(hours=-3)
    assert world.run("preflight").code == 1
    assert "fence_probe_at" not in world.get(cli.PREFLIGHT)  # broken: no capture can rely on it
    world.probes.fence = ("unavailable", "")
    world.clock[0] = at(hours=-2)
    world.run("preflight")
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == stamp(at(hours=-2))
    world.probes.fence = ("unknown", "transport")
    world.run("preflight")
    assert world.get(cli.PREFLIGHT)["sheets"] == "unknown"


def test_a_capture_taken_before_the_continuous_fence_cannot_activate(ready):
    # The fence was seen at -5h, broken at -3h and seen again at -2h. A capture at -4h is stale.
    ready.put(cli.PREFLIGHT, {**ready.get(cli.PREFLIGHT), "fence_probe_at": stamp(at(hours=-2))})
    ready.put(cli.MANIFEST, signed_manifest(at(hours=-4), at(minutes=-30)))
    assert codes(ready.run("activate")) == ["capture_before_fence"]


def test_the_whole_cutover_in_order_with_cli_written_evidence(world):
    world.settings.os_statement_pepper = PEPPER
    assert world.run("init").code == 0
    world.probes.fence = ("live", "")
    world.clock[0] = at(hours=-6)
    drain = world.run("preflight", "--drain", "--final-accept")
    assert drain.code == 0 and world.probes.snapshots == 1
    assert world.get(cli.DRAIN)["sheets_pending"] == 0
    world.probes.fence = ("unavailable", "")
    world.clock[0] = at(hours=-5)
    assert world.run("preflight").code == 0
    world.put(cli.MANIFEST, signed_manifest(at(hours=-4), at(hours=-3)))
    world.put(cli.BACKUP, {"last_success_utc": stamp(at(minutes=-5))})
    world.put(cli.DRILL, {"started_utc": stamp(at(days=-1)), "passed": True})
    world.put(cli.FRONTEND, {"v": 1, "cloudflare_version": "ce8332db-4a1e",
                             "bundle_sha256": "c" * 64, "recorded_at": stamp(at(days=-2))})
    world.put(cli.JOURNAL, {"v": 1, "measured_at": stamp(at(hours=-1)), "max_rows": 2000,
                            "tabs": {"_assaf_os_replay": 51}})
    world.clock[0] = NOW
    preflight = world.run("preflight")
    assert preflight.code == 0 and preflight.out["ready_to_activate"] is True
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == stamp(at(hours=-5))
    assert world.run("activate").code == 0
    assert world.state().states["core"] == "active"
    status = world.run("status")
    assert status.out["states"]["core"] == "active" and status.out["settings"]["freeze"] is True


# --------------------------------------------------------------------------- drain (T2)


def test_the_drain_needs_the_explicit_final_accept_flag(ready):
    run = ready.run("preflight", "--drain")
    assert run.code == 1 and codes(run) == ["final_accept_flag_required"]
    assert ready.probes.snapshots == 0


def test_the_drain_refuses_without_the_freeze_or_with_a_fenced_sheet(ready):
    ready.probes.fence = ("live", "")
    ready.settings.assaf_os_freeze = False
    assert codes(ready.run("preflight", "--drain", "--final-accept")) == ["freeze_off"]
    ready.settings.assaf_os_freeze = True
    ready.probes.fence = ("unavailable", "")
    assert codes(ready.run("preflight", "--drain", "--final-accept")) == [
        "drain_requires_live_sheet"]
    ready.probes.fence = ("unknown", "transport")
    assert codes(ready.run("preflight", "--drain", "--final-accept")) == [
        "drain_requires_live_sheet"]
    assert ready.probes.snapshots == 0


def test_the_drain_records_what_is_still_open_and_fails_on_any(ready):
    ready.probes.fence = ("live", "")
    ready.probes.pending, ready.probes.unknown, ready.probes.cards = 2, 1, 3
    run = ready.run("preflight", "--drain", "--final-accept")
    assert run.code == 1 and codes(run) == [
        "sheets_pending_proposals", "sheets_unknown_receipts", "approval_cards_open"]
    document = ready.get(cli.DRAIN)
    assert (document["sheets_pending"], document["sheets_unknown_receipts"],
            document["cards_open"], document["freeze"]) == (2, 1, 3, True)
    ready.probes.pending = ready.probes.unknown = ready.probes.cards = 0
    ok = ready.run("preflight", "--drain", "--final-accept")
    assert ok.code == 0 and ready.get(cli.DRAIN)["sheets_pending"] == 0


# --------------------------------------------------------------------------- freeze-plan


def test_freeze_plan_prints_the_exact_keys_and_changes_nothing(ready):
    drain = ready.run("freeze-plan")
    assert drain.code == 0 and "MIA_ASSAF_OS_FREEZE=true\n" in drain.out
    assert "sudo mia settings" in drain.out
    before = ready.state()
    early = ready.run("freeze-plan", "--phase", "switch")
    assert early.code == 1 and codes(early) == ["authority_state_invalid"]
    assert ready.run("activate").code == 0
    switch = ready.run("freeze-plan", "--phase", "switch")
    lines = [line for line in switch.out.splitlines() if not line.startswith("#")]
    assert lines == [
        "MIA_ASSAF_OS_BACKEND=postgres", "MIA_ASSAF_OS_LEDGER_ENABLED=true",
        "MIA_ASSAF_OS_AUTHORITY_EPOCH=1", "MIA_ASSAF_OS_FREEZE=false",
    ]
    revert = ready.run("freeze-plan", "--phase", "revert")
    assert [line for line in revert.out.splitlines() if not line.startswith("#")] == [
        "MIA_ASSAF_OS_BACKEND=sheets", "MIA_ASSAF_OS_LEDGER_ENABLED=false",
        "MIA_ASSAF_OS_AUTHORITY_EPOCH=0",
    ]
    assert before.states["core"] == "shadow" and ready.state().epoch == 1
    for text in (drain.out, switch.out, revert.out):
        assert SECRET not in text and PEPPER not in text


def test_freeze_plan_after_a_re_arm_names_the_new_epoch(ready):
    ready.run("activate")
    ready.run("rollback")
    ready.run("init", "--rearm")
    assert ready.run("freeze-plan", "--phase", "revert").code == 0
    ready.env.transition(WORKSPACE, "core", "active", expected_epoch=2, actor_owner_id=OWNER_ID)
    switch = ready.run("freeze-plan", "--phase", "switch")
    assert "MIA_ASSAF_OS_AUTHORITY_EPOCH=2" in switch.out


# --------------------------------------------------------------------------- status and hygiene


def test_status_reports_authority_settings_and_evidence_without_secrets(ready, caplog):
    ready.settings.assaf_os_authority_epoch = 1
    with caplog.at_level(logging.DEBUG):
        run = ready.run("status")
    assert run.code == 0
    assert run.out["settings"] == {
        "backend": "sheets", "ledger_enabled": False, "authority_epoch": 1,
        "epoch_matches": True, "freeze": True,
    }
    assert run.out["evidence"][cli.DRAIN]["epoch"] == 1
    assert run.out["evidence"][cli.ACTIVATION] is None
    assert SECRET not in json.dumps(run.out) + caplog.text


def test_no_command_prints_or_logs_a_secret_a_dsn_or_driver_text(ready, caplog):
    ready.settings.os_statement_pepper = PEPPER
    ready.probes.broken = {"main"}
    outputs = []
    with caplog.at_level(logging.DEBUG):
        for argv in (("init",), ("preflight",), ("activate",), ("status",), ("fence",),
                     ("freeze-plan", "--phase", "switch"), ("rollback",), ("freeze-plan",)):
            run = ready.run(*argv)
            outputs.append(json.dumps(run.out) + run.err)
    text = "\n".join(outputs) + caplog.text
    for forbidden in (SECRET, PEPPER, "driver-text", "postgresql://"):
        assert forbidden not in text
    for record in caplog.records:
        assert record.getMessage().startswith("os_authority op=")


def test_the_job_always_disposes_its_probes(ready):
    ready.run("status")
    assert ready.probes.closed is True


def test_a_ledger_error_is_a_fixed_refusal_never_its_message(ready, monkeypatch):
    def boom(*args, **kwargs):
        raise api.LedgerError("ledger_unavailable", reason="read_failed")

    monkeypatch.setattr(ready.env._impl, "state", boom)
    run = ready.run("status")
    assert run.code == 1 and codes(run) == ["ledger_unavailable:read_failed"]


def test_an_unexpected_error_is_internal_error_with_no_text(ready, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(DRIVER_TEXT)

    monkeypatch.setattr(ready.env._impl, "state", boom)
    run = ready.run("status")
    assert run.code == 1 and codes(run) == ["internal_error"]
    assert "driver-text" not in run.err + json.dumps(run.out)


def test_the_arguments_are_required_and_exact(capsys):
    with pytest.raises(SystemExit) as missing:
        cli.run(["status"], env={"MIA_BUILD_SHA": SHA})
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as unknown:
        cli.run(["migrate", "--approved-sha", SHA], env={"MIA_BUILD_SHA": SHA})
    assert unknown.value.code == 2
    with pytest.raises(SystemExit) as phase:
        cli.run(["freeze-plan", "--phase", "x", "--approved-sha", SHA], env={"MIA_BUILD_SHA": SHA})
    assert phase.value.code == 2
    capsys.readouterr()


# --------------------------------------------------------------------------- live wiring


def test_live_wiring_refuses_missing_dsns_and_an_ambiguous_owner(monkeypatch):
    from app.core import config

    settings = SimpleNamespace(
        telegram_owner_user_id_set=lambda: {"123"}, assaf_os_workspace_id="assaf-os-main",
        owner_os_ledger_database_url="",
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    with pytest.raises(cli.Refusal) as refused:
        cli.live_deps({"MIA_BUILD_SHA": SHA})
    assert refused.value.codes == ["dsn_missing"]
    settings.telegram_owner_user_id_set = lambda: {"123", "456"}
    with pytest.raises(cli.Refusal) as ambiguous:
        cli.live_deps({"MIA_BUILD_SHA": SHA})
    assert ambiguous.value.codes == ["owner_ambiguous"]
    settings.telegram_owner_user_id_set = lambda: {"123"}
    settings.assaf_os_workspace_id = "x"
    with pytest.raises(cli.Refusal) as workspace:
        cli.live_deps({"MIA_BUILD_SHA": SHA})
    assert workspace.value.codes == ["workspace_invalid"]


def test_live_wiring_without_the_ledger_implementation_is_a_fixed_refusal(monkeypatch):
    from app.core import config

    settings = SimpleNamespace(
        telegram_owner_user_id_set=lambda: {"123"}, assaf_os_workspace_id="assaf-os-main",
        owner_os_ledger_database_url="",
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_loader", lambda module, name: (_ for _ in ()).throw(
        cli.Refusal("ledger_implementation_missing")))
    env = {
        "MIA_BUILD_SHA": SHA, cli.PRIVILEGED_DSN_ENV: "postgresql://priv@127.0.0.1/mia",
        cli.MAIN_DSN_ENV: "postgresql://app@127.0.0.1/mia",
    }
    with pytest.raises(cli.Refusal) as missing:
        cli.live_deps(env)
    assert missing.value.codes == ["ledger_implementation_missing"]
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(["status", "--approved-sha", SHA], env=env, stdout=out, stderr=err)
    assert code == 1 and err.getvalue() == "os_authority_refused:ledger_implementation_missing\n"
    assert "postgresql://" not in out.getvalue() + err.getvalue()
