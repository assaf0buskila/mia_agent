"""V2-31: the os-authority CLI (V2_PG_CONTRACTS.md section 16 and section 20).

The probes, the evidence directory and the clock are fakes. The ledger's ``AuthorityAdmin`` is the
in-memory FakeLedger, and the tests that exercise the authority state machine (init, activate,
fence, rollback, re-arm, the whole cutover) run a second time against the REAL
``PgAuthorityAdmin`` on a disposable database when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set (the
V2-16f ``ledger`` rule: a ``pg`` case exists only when it can run, so it is never skipped).
Nothing here touches a network or a secret store.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest
from app.services.os_ledger import api
from app.workers import os_authority as cli

from tests.fakes.os_ledger_builders import commit_direct, commit_import, ctx, task
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE, build_fake_env
from tests.services.os_ledger.conftest import PG_URL_ENV, build_pg_env, pg_enabled

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


CASES = ["fake", *(["pg"] if pg_enabled() else [])]


class World:
    def __init__(self, tmp_path, case="fake"):
        self.case = case
        self.env = build_fake_env() if case == "fake" else build_pg_env(os.environ[PG_URL_ENV])
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


# The five journal tabs of the archive workbook (UnifiedRuntime.gs ASSAF_OS_META), spelled out here
# on purpose: test_os_authority_probes.py pins the CLI's own list to the Script's source, and these
# literals are what an operator is told to measure.
JOURNAL_NAMES = (
    "_assaf_os_accepted", "_assaf_os_proposals", "_assaf_os_receipts", "_assaf_os_replay",
    "_assaf_os_issues",
)


def journal_tabs(**over: Any) -> dict[str, Any]:
    """A fresh `journal.json` tab map: every journal tab, the ones the cutover grew a little."""
    tabs: dict[str, Any] = dict.fromkeys(JOURNAL_NAMES, 0)
    tabs.update({"_assaf_os_proposals": 32, "_assaf_os_receipts": 19, "_assaf_os_replay": 51})
    tabs.update(over)
    return tabs


def journal_document(*, measured: datetime | None = None, **over: Any) -> dict[str, Any]:
    tabs = over.pop("tabs", None)
    document = {
        "v": 1, "measured_at": stamp(measured or at(hours=-5)), "max_rows": 2000,
        "tabs": journal_tabs() if tabs is None else tabs,
    }
    return document | over


def operator_evidence(world: World) -> None:
    """The four files the operator places (`backup`, `restore-drill`, `fe-v2`, `journal`)."""
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
    world.put(cli.JOURNAL, journal_document())


def good_evidence(world: World) -> None:
    operator_evidence(world)
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


@pytest.fixture(params=CASES)
def any_world(request, tmp_path):
    """``world`` over every ledger implementation that can run here."""
    return World(tmp_path, request.param)


@pytest.fixture
def any_ready(any_world):
    assert any_world.run("init").code == 0
    good_evidence(any_world)
    return any_world


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


def test_the_all_green_world_activates_core_without_bumping_the_epoch(any_ready):
    ready = any_ready
    before = ready.state()
    run = ready.run("activate")
    assert run.code == 0 and run.err == ""
    after = ready.state()
    assert (before.epoch, before.states["core"]) == (1, "shadow")
    states = (after.epoch, after.states["core"], after.states["statements"])
    assert states == (1, "active", "shadow")
    assert after.activated_at["core"] is not None and after.activated_at["statements"] is None
    assert [c["status"] for c in run.out["checks"]] == ["pass"] * len(cli.PRECONDITIONS)
    activation = ready.get(cli.ACTIVATION)
    assert (activation["epoch"], activation["release_sha"]) == (1, SHA)


def test_activate_is_idempotent_once_active_and_skips_the_checks(any_ready):
    ready = any_ready
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
        cli.MANIFEST,
        signed_manifest(at(minutes=-90), at(minutes=-30), signoff_manifest_digest="0" * 64)),
    "fe_v2_reader_not_live": lambda w: w.drop(cli.FRONTEND),
    "journal_capacity": lambda w: w.put(
        cli.JOURNAL, {**w.get(cli.JOURNAL), "tabs": journal_tabs(_assaf_os_proposals=1900)}),
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
    ready.put(
        cli.DRAIN, {**ready.get(cli.DRAIN), "freeze": True, "recorded_at": stamp(at(hours=-1))}
    )
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
        ready.put(cli.JOURNAL, journal_document(tabs=journal_tabs(_assaf_os_replay=rows)))
        assert (ready.run("preflight").code == 0) is ok
    ready.put(cli.JOURNAL, journal_document(measured=at(days=-8)))
    assert ready.run("preflight").code == 1


def journal_detail(run: Run) -> str:
    return next(c for c in run.out["checks"] if c["code"] == "journal_capacity")["detail"]


@pytest.mark.parametrize("tabs", [
    {"anything": 0},
    {"_assaf_os_replay": 51},  # the one-tab file the old check passed
    {name: 0 for name in JOURNAL_NAMES if name != "_assaf_os_accepted"},
    {name: 0 for name in JOURNAL_NAMES if name != "_assaf_os_issues"},
    {**dict.fromkeys(JOURNAL_NAMES, 0), "_assaf_os_extra": 0},
    # five entries, but one is not a journal tab: a count alone proves nothing
    {**{name: 0 for name in JOURNAL_NAMES if name != "_assaf_os_issues"}, "_assaf_os_issue": 0},
    # the three tabs the runbook example used to list
    {"_assaf_os_proposals": 32, "_assaf_os_receipts": 19, "_assaf_os_replay": 51},
], ids=["anything", "one-tab", "no-accepted", "no-issues", "extra", "misspelled", "three-tabs"])
def test_the_journal_measure_must_name_exactly_the_five_journal_tabs(ready, tabs):
    # `_assaf_os_accepted` at 1950 of 2000 is the case that must never pass by being left out.
    ready.put(cli.JOURNAL, journal_document(tabs=tabs))
    run = ready.run("activate")
    assert run.code == 1 and codes(run) == ["journal_capacity"]
    assert journal_detail(run) == "tabs"
    assert ready.state().states["core"] == "shadow"


def test_a_full_accepted_tab_blocks_activation_when_it_is_measured(ready):
    ready.put(cli.JOURNAL, journal_document(tabs=journal_tabs(_assaf_os_accepted=1950)))
    run = ready.run("activate")
    assert codes(run) == ["journal_capacity"] and journal_detail(run) == "at_cap"


@pytest.mark.parametrize("cap,detail", [
    (1, "max_rows"), (1999, "max_rows"), (2001, "max_rows"), (100_000, "max_rows"),
    (0, "invalid"), (-2000, "invalid"), (True, "invalid"), ("2000", "invalid"), (2000.0, "invalid"),
    (None, "invalid"),
])
def test_the_journal_cap_is_the_scripts_own_cap_and_nothing_else(ready, cap, detail):
    # A larger number would let a full tab through (1950 of 100000); a smaller one is not the
    # Script's cap. Only the value the Script enforces (ASSAF_OS_MAX_ROWS) proves any headroom.
    ready.put(cli.JOURNAL, journal_document(tabs=journal_tabs(_assaf_os_accepted=1950),
                                            max_rows=cap))
    run = ready.run("activate")
    assert run.code == 1 and codes(run) == ["journal_capacity"]
    assert journal_detail(run) == detail


def test_the_journal_measure_still_needs_every_tab_to_be_a_count(ready):
    for bad in (-1, True, 1.5, "7", None):
        ready.put(cli.JOURNAL, journal_document(tabs=journal_tabs(_assaf_os_receipts=bad)))
        run = ready.run("activate")
        assert codes(run) == ["journal_capacity"] and journal_detail(run) == "invalid", bad


# --------------------------------------------------------------------------- init


def test_init_creates_epoch_one_and_is_idempotent(any_world):
    world = any_world
    first = world.run("init")
    assert first.code == 0 and first.out["outcome"] == "created"
    assert (first.out["epoch"], first.out["states"]) == (
        1, {"core": "shadow", "statements": "shadow"})
    second = world.run("init")
    assert second.code == 0 and second.out["outcome"] == "existing"
    assert len(world.env.rows("os_workspaces")) == 1
    assert world.state().epoch == 1


def test_init_pins_the_pepper_fingerprint_once_and_never_prints_the_pepper(any_world, caplog):
    world = any_world
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


def test_init_without_a_pepper_pins_nothing(any_world):
    world = any_world
    run = world.run("init")
    assert run.out["pepper_pinned"] is False
    assert world.env.rows("os_workspaces")[0]["statement_pepper_fp"] is None


def test_a_short_pepper_is_refused_before_any_pin():
    with pytest.raises(cli.Refusal) as refused:
        cli.pepper_fingerprint("short")
    assert refused.value.codes == ["pepper_invalid"]


def test_init_refuses_a_workspace_owned_by_someone_else(any_world):
    world = any_world
    world.env.ensure_workspace(WORKSPACE, "999", 1)
    run = world.run("init")
    assert run.code == 1 and codes(run) == ["authority_changed:workspace_conflict"]


def test_init_never_runs_before_the_workspace_is_known_to_exist(any_world):
    world = any_world
    assert codes(world.run("activate")) == ["workspace_missing"]
    for command in ("status", "fence", "rollback"):
        assert codes(world.run(command)) == ["workspace_missing"], command
    assert codes(world.run("preflight")) == ["workspace_missing"]


# --------------------------------------------------------------------------- activate states


@pytest.mark.parametrize("path", [("fenced",), ("fenced", "restoring")])
def test_activate_only_runs_from_shadow(any_ready, path):
    ready = any_ready
    for step in path:
        ready.env.transition(WORKSPACE, "core", step, expected_epoch=1, actor_owner_id=OWNER_ID)
    run = ready.run("activate")
    assert run.code == 1 and codes(run) == ["authority_state_invalid"]


# --------------------------------------------------------------------------- fence and rollback


def test_fence_refuses_every_ledger_write_and_is_idempotent(any_ready):
    ready = any_ready
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


def test_fence_also_stops_an_active_statements_domain(any_ready):
    ready = any_ready
    ready.env.transition(
        WORKSPACE, "statements", "active", expected_epoch=1, actor_owner_id=OWNER_ID
    )
    run = ready.run("fence")
    assert run.out["fenced"] == ["core", "statements"]
    assert ready.state().states == {"core": "fenced", "statements": "fenced"}


def test_fence_refuses_a_restoring_ledger(any_ready):
    ready = any_ready
    for step in ("fenced", "restoring"):
        ready.env.transition(WORKSPACE, "core", step, expected_epoch=1, actor_owner_id=OWNER_ID)
    assert codes(ready.run("fence")) == ["authority_state_invalid"]


def test_rollback_before_the_first_write_leaves_only_import_rows(any_ready):
    ready = any_ready
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


def test_rollback_after_the_first_business_write_is_refused_and_changes_nothing(any_ready):
    ready = any_ready
    commit_import(ready.env, ctx(), [task("TAS_i1")], "e" * 64)
    assert ready.run("activate").code == 0
    commit_direct(ready.env, ctx(), [task("TAS_w1")])
    run = ready.run("rollback")
    assert run.code == 1 and codes(run) == ["rollback_after_first_write"]
    assert ready.state().states["core"] == "active"
    assert ready.run("fence").code == 0  # roll forward: fence is still available


def test_rollback_refuses_a_restoring_ledger(any_ready):
    ready = any_ready
    for step in ("fenced", "restoring"):
        ready.env.transition(WORKSPACE, "core", step, expected_epoch=1, actor_owner_id=OWNER_ID)
    assert codes(ready.run("rollback")) == ["authority_state_invalid"]


def test_a_rolled_back_cutover_re_arms_with_one_idempotent_epoch_bump(any_ready):
    ready = any_ready
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


def test_re_arm_is_refused_unless_fenced_and_never_after_a_business_write(any_ready):
    ready = any_ready
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


# ----------------------------------------------------- abort and retry of T3 (one epoch, one SHA)
#
# The T3 rollback sets ASSAF_OS_MODE back to LIVE and lifts the freeze without touching the ledger,
# so the epoch and the SHA do not change: the only thing that can tell a retry from the aborted
# attempt is what the probes saw. The fence time of the aborted attempt must never be carried into
# the retry, and the drain taken before it must not be reused.


def drain_at(world: World, hours: float) -> Run:
    """T2: the one signed snapshot, taken while the Sheet is live and the freeze is on."""
    world.probes.fence = ("live", "")
    world.clock[0] = at(hours=hours)
    return world.run("preflight", "--drain", "--final-accept")


def fence_at(world: World, hours: float) -> Run:
    """T3: the Script is fenced, and the preflight right after it records the fence time."""
    world.probes.fence = ("unavailable", "")
    world.clock[0] = at(hours=hours)
    return world.run("preflight")


def script_live_again(world: World, hours: float) -> Run:
    """The T3 rollback as the runbook documents it: the Script is LIVE, and one preflight runs."""
    world.probes.fence = ("live", "")
    world.clock[0] = at(hours=hours)
    return world.run("preflight")


def sign_capture(world: World, captured: float, signed: float) -> None:
    world.put(cli.MANIFEST, signed_manifest(at(hours=captured), at(hours=signed)))


def activate_now(world: World) -> Run:
    """T6: the operator's evidence is placed fresh, right before `activate` (it is dated to NOW)."""
    operator_evidence(world)
    world.probes.fence = ("unavailable", "")
    world.clock[0] = NOW
    return world.run("activate")


def started(world: World) -> World:
    assert world.run("init").code == 0
    return world


def failing(run: Run) -> dict[str, str]:
    return {c["code"]: c["detail"] for c in run.out["checks"] if c["status"] != "pass"}


def hard(run: Run) -> dict[str, str]:
    """Only the checks that FAIL (a preflight before the operator evidence also shows `pending`)."""
    return {c["code"]: c["detail"] for c in run.out["checks"] if c["status"] == "fail"}


def test_a_retry_that_redoes_the_drain_does_not_carry_the_aborted_fence_time(any_world):
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    assert fence_at(world, -9).code == 0
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == stamp(at(hours=-9))
    # The T3 rollback (the Script is LIVE), then T2 is redone while the Sheet is live. The drain
    # itself saw the Script live, so it ends the unbroken run of `unavailable` on its own.
    assert drain_at(world, -7).code == 0
    record = world.get(cli.PREFLIGHT)
    assert record["sheets"] == "live" and "fence_probe_at" not in record
    assert fence_at(world, -6).code == 0
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == stamp(at(hours=-6))
    sign_capture(world, -5, -4)
    run = activate_now(world)
    assert run.code == 0, run.err
    assert [c["status"] for c in run.out["checks"]] == ["pass"] * len(cli.PRECONDITIONS)
    assert world.state().states["core"] == "active"


def test_the_documented_rollback_voids_the_old_drain_and_the_captures_of_the_live_window(
    any_world,
):
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    assert fence_at(world, -9).code == 0
    # T3 rollback: ASSAF_OS_MODE back to LIVE, then ONE preflight while the Script is live. Its
    # exit 1 (`sheets_not_fenced`) is the expected answer; it records that the fence was lifted.
    live = script_live_again(world, -8)
    assert live.code == 1 and codes(live) == ["sheets_not_fenced", "sheets_pending_proposals"]
    record = world.get(cli.PREFLIGHT)
    assert record["sheets"] == "live" and "fence_probe_at" not in record
    assert record["fence_broken_at"] == stamp(at(hours=-8))
    assert script_live_again(world, -7.5).code == 1
    assert world.get(cli.PREFLIGHT)["fence_broken_at"] == stamp(at(hours=-8)), "kept, not moved"
    # The aborted attempt is NOT redone properly: capture #1 is taken while the Sheet is live and
    # the freeze is lifted, and the Script is fenced again with the old drain.
    sign_capture(world, -6.5, -5)
    assert fence_at(world, -6).code == 1
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == stamp(at(hours=-6))
    assert world.get(cli.PREFLIGHT)["fence_broken_at"] == stamp(at(hours=-8))
    run = activate_now(world)
    assert run.code == 1 and codes(run) == ["capture_before_fence", "sheets_pending_proposals"]
    assert failing(run) == {
        "capture_before_fence": "capture_not_after_fence",
        "sheets_pending_proposals": "drain_before_fence_break",
    }
    assert world.state().states["core"] == "shadow"
    assert not (world.dir / cli.ACTIVATION).exists()
    # The retry the runbook prescribes clears both: the old manifest.json is void, T2 again, the
    # fence again, new captures.
    world.drop(cli.MANIFEST)
    for name in (cli.BACKUP, cli.DRILL, cli.FRONTEND, cli.JOURNAL):
        # Placed for the refused activate and dated NOW, which this test's clock has gone back
        # from: a real retry places fresh files, so the old ones go with the old manifest.
        world.drop(name)
    assert drain_at(world, -4).code == 0
    assert fence_at(world, -3.5).code == 0
    sign_capture(world, -3, -2.5)
    assert activate_now(world).code == 0
    assert world.state().states["core"] == "active"


def test_a_drain_that_dies_after_seeing_the_sheet_live_leaves_the_old_drain_void(any_world):
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    assert fence_at(world, -9).code == 0
    world.probes.broken = {"snapshot"}
    died = drain_at(world, -7)
    assert codes(died) == ["internal_error"]
    world.probes.broken = set()
    assert world.get(cli.DRAIN)["recorded_at"] == stamp(at(hours=-10)), "the old drain is kept"
    assert fence_at(world, -6).code == 1
    sign_capture(world, -5, -4)
    run = activate_now(world)
    assert codes(run) == ["sheets_pending_proposals"]
    assert failing(run) == {"sheets_pending_proposals": "drain_before_fence_break"}


def test_the_drain_records_that_it_saw_the_sheet_live(any_ready):
    ready = any_ready
    assert ready.get(cli.PREFLIGHT)["fence_probe_at"] == stamp(at(hours=-2))
    ready.probes.fence = ("live", "")
    assert ready.run("preflight", "--drain", "--final-accept").code == 0
    record = ready.get(cli.PREFLIGHT)
    assert (record["kind"], record["epoch"], record["release_sha"], record["sheets"]) == (
        "preflight", 1, SHA, "live")
    assert record["last_probe_at"] == stamp(NOW) and "fence_probe_at" not in record
    assert record["fence_broken_at"] == stamp(NOW)
    ready.probes.fence = ("unavailable", "")
    run = ready.run("activate")  # no fence has been recorded since the drain
    assert run.code == 1 and codes(run) == ["capture_before_fence", "sheets_pending_proposals"]
    assert failing(run) == {
        "capture_before_fence": "no_fence_probe", "sheets_pending_proposals": "fence_not_recorded",
    }


def test_a_live_preflight_before_any_fence_does_not_void_the_drain(any_world):
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    # A sanity preflight while the Script is still live: no fence was established, none is broken.
    assert script_live_again(world, -9.5).code == 1
    assert "fence_broken_at" not in world.get(cli.PREFLIGHT)
    assert fence_at(world, -9).code == 0
    sign_capture(world, -8, -7)
    assert activate_now(world).code == 0, "the freeze held since the drain: it need not be redone"


# ------------------------------------------------ a drain belongs to one freeze (V2-31b finding 2)
#
# The T1/T2 rollback only unfreezes. Nothing in the ledger changes, and no fence was ever recorded,
# so `fence_broken_at` is absent. Proposals made from the OS screen over the next days have no
# Telegram card, so no other check sees them. The old drain must not satisfy the retry.


def superseded(world: World) -> list[str]:
    return sorted(path.name for path in world.dir.glob("drain-superseded-*.json"))


def test_freeze_plan_drain_sets_the_old_drain_aside_so_each_freeze_starts_a_new_one(any_world):
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    old = (world.dir / cli.DRAIN).read_bytes()
    world.clock[0] = at(hours=-9)
    plan = world.run("freeze-plan")
    assert plan.code == 0 and "MIA_ASSAF_OS_FREEZE=true\n" in plan.out
    assert "drain.json" in plan.out, "the plan tells the operator the drain is void"
    assert not (world.dir / cli.DRAIN).exists()
    kept = superseded(world)
    assert len(kept) == 1 and (world.dir / kept[0]).read_bytes() == old, "set aside, not deleted"
    assert kept[0] == "drain-superseded-" + cli.EvidenceStore.aside_stamp(at(hours=-9)) + ".json"
    again = world.run("freeze-plan")  # nothing left to set aside
    assert again.code == 0 and superseded(world) == kept
    # Only the drain phase starts a freeze: the other phases leave a drain alone.
    world.put(cli.DRAIN, {"v": 1})
    assert world.run("freeze-plan", "--phase", "revert").code == 0
    assert (world.dir / cli.DRAIN).exists() and superseded(world) == kept


def test_freeze_plan_drain_works_before_any_evidence_directory_exists(world):
    plan = world.run("freeze-plan")
    assert plan.code == 0 and not world.dir.exists()


def test_freeze_plan_drain_refuses_when_the_old_drain_cannot_be_set_aside(world, monkeypatch):
    world.run("init")
    world.put(cli.DRAIN, {"v": 1})

    def refuse(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(cli.os, "replace", refuse)
    run = world.run("freeze-plan")
    assert run.code == 1 and codes(run) == ["evidence_unwritable"]
    assert (world.dir / cli.DRAIN).exists(), "a freeze must not start over a drain it cannot void"


def test_an_aborted_window_cannot_lend_its_drain_to_the_next_freeze(any_world):
    """The reviewer's scenario: drain, abort, unfreeze, new OS proposals, re-freeze, then T3."""
    world = started(any_world)
    assert drain_at(world, -100).code == 0  # window 1: T1, T2 at epoch 1, then aborted before T3
    # Days later: the owner made OS-screen proposals (no card, no drain), then T1 of window 2.
    world.clock[0] = at(hours=-3)
    assert world.run("freeze-plan").code == 0
    # The operator goes straight to T3 without a T2: the Script is fenced and probed.
    run = fence_at(world, -2)
    assert run.code == 1 and codes(run) == ["sheets_pending_proposals"]
    assert hard(run) == {"sheets_pending_proposals": "no_drain_evidence"}
    sign_capture(world, -1.5, -1)
    refused = activate_now(world)
    assert refused.code == 1 and codes(refused) == ["sheets_pending_proposals"]
    assert failing(refused) == {"sheets_pending_proposals": "no_drain_evidence"}
    assert world.state().states["core"] == "shadow"
    assert not (world.dir / cli.ACTIVATION).exists()


def test_an_old_drain_is_stale_even_when_nobody_ran_freeze_plan(any_world):
    """The same scenario, with the freeze keys set by hand: the age bound refuses it."""
    world = started(any_world)
    assert drain_at(world, -100).code == 0
    run = fence_at(world, -2)  # no fence was ever recorded before, so there is no fence_broken_at
    assert "fence_broken_at" not in world.get(cli.PREFLIGHT)
    assert run.code == 1 and codes(run) == ["sheets_pending_proposals"]
    assert hard(run) == {"sheets_pending_proposals": "drain_stale"}
    sign_capture(world, -1.5, -1)
    refused = activate_now(world)
    assert refused.code == 1 and codes(refused) == ["sheets_pending_proposals"]
    assert failing(refused) == {"sheets_pending_proposals": "drain_stale"}
    assert world.state().states["core"] == "shadow"
    # The retry the runbook prescribes: T1 (the old drain is set aside), a new T2, then T3, with
    # new captures (the evidence of the refused attempt goes, as in the T3 retry above).
    world.drop(cli.MANIFEST)
    for name in (cli.BACKUP, cli.DRILL, cli.FRONTEND, cli.JOURNAL):
        world.drop(name)
    assert world.run("freeze-plan").code == 0
    assert drain_at(world, -1.2).code == 0
    assert fence_at(world, -1).code == 0
    sign_capture(world, -0.9, -0.8)
    assert activate_now(world).code == 0


@pytest.mark.parametrize("extra_seconds,fresh", [(0, True), (1, False)])
def test_the_drain_must_be_taken_within_the_named_bound_of_the_fence_probe(
    any_world, extra_seconds, fresh
):
    assert cli.DRAIN_MAX_AGE <= timedelta(hours=6), "T3 follows T2 immediately: a small bound"
    world = started(any_world)
    assert drain_at(world, -20).code == 0
    world.probes.fence = ("unavailable", "")
    world.clock[0] = at(hours=-20) + cli.DRAIN_MAX_AGE + timedelta(seconds=extra_seconds)
    run = world.run("preflight")
    if fresh:
        assert run.code == 0
    else:
        assert run.code == 1 and hard(run) == {"sheets_pending_proposals": "drain_stale"}


def test_an_unknown_probe_is_no_sighting_of_the_live_sheet_and_no_end_of_the_run(any_world):
    # V2-31b finding 3: an `unknown` probe (a timeout, a transport error) carries no more
    # information than no probe at all, and a gap between two `unavailable` probes is already
    # treated as continuous. It must not restart the clock and void the captures.
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    assert fence_at(world, -9.5).code == 0
    first = stamp(at(hours=-9.5))
    world.probes.fence = ("unknown", "transport")
    world.clock[0] = at(hours=-9)
    assert world.run("preflight").code == 1  # `unknown` is still not a pass of sheets_not_fenced
    record = world.get(cli.PREFLIGHT)
    assert record["sheets"] == "unknown" and record["last_probe_at"] == stamp(at(hours=-9))
    assert record["fence_probe_at"] == first, "carried through, not restarted"
    assert "fence_broken_at" not in record, "a transport error does not void the drain"
    assert fence_at(world, -8.5).code == 0
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == first
    assert world.run("preflight").code == 0  # idempotent


def test_an_unknown_probe_between_two_fenced_probes_keeps_the_captures_valid(any_world):
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    assert fence_at(world, -9.5).code == 0  # T3
    sign_capture(world, -9.2, -8)  # T4/T5: captured after the fence, signed later
    world.probes.fence = ("unknown", "transport")
    world.clock[0] = at(hours=-1)  # T6 preflight: the signed schema probe times out once
    assert world.run("preflight").code == 1
    assert fence_at(world, -0.5).code == 0  # and passes on the next attempt
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == stamp(at(hours=-9.5))
    run = activate_now(world)
    assert run.code == 0, run.err
    assert [c["status"] for c in run.out["checks"]] == ["pass"] * len(cli.PRECONDITIONS)
    assert world.state().states["core"] == "active"


def test_activate_after_an_unknown_preflight_uses_the_carried_fence_time(any_world):
    # The last record on disk is the `unknown` one; activate probes the Script itself.
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    assert fence_at(world, -9.5).code == 0
    sign_capture(world, -9.2, -8)
    world.probes.fence = ("unknown", "transport")
    world.clock[0] = at(hours=-1)
    world.run("preflight")
    assert world.get(cli.PREFLIGHT)["sheets"] == "unknown"
    assert activate_now(world).code == 0


def test_an_unknown_probe_with_no_fence_before_it_has_nothing_to_carry(any_world):
    world = started(any_world)
    world.probes.fence = ("unknown", "transport")
    world.clock[0] = at(hours=-9)
    world.run("preflight")
    assert "fence_probe_at" not in world.get(cli.PREFLIGHT)
    assert fence_at(world, -8).code == 1  # no drain yet; the clock starts here
    assert world.get(cli.PREFLIGHT)["fence_probe_at"] == stamp(at(hours=-8))


def test_the_live_sheet_after_a_fence_and_an_unknown_probe_is_still_a_break(any_world):
    world = started(any_world)
    assert drain_at(world, -10).code == 0
    assert fence_at(world, -9.5).code == 0
    world.probes.fence = ("unknown", "transport")
    world.clock[0] = at(hours=-9)
    world.run("preflight")
    live = script_live_again(world, -8.5)  # the T3 rollback: the Script is LIVE
    assert live.code == 1
    record = world.get(cli.PREFLIGHT)
    assert "fence_probe_at" not in record
    assert record["fence_broken_at"] == stamp(at(hours=-8.5)), "the unknown probe hides no break"
    assert hard(live)["sheets_pending_proposals"] == "drain_before_fence_break"


def test_a_refused_drain_does_not_touch_the_fence_record(ready):
    before = (ready.dir / cli.PREFLIGHT).read_bytes()
    assert ready.run("preflight", "--drain").code == 1  # no --final-accept
    ready.probes.fence = ("live", "")
    ready.settings.assaf_os_freeze = False
    assert codes(ready.run("preflight", "--drain", "--final-accept")) == ["freeze_off"]
    ready.settings.assaf_os_freeze = True
    for sheets in (("unavailable", ""), ("unknown", "transport")):
        ready.probes.fence = sheets
        assert codes(ready.run("preflight", "--drain", "--final-accept")) == [
            "drain_requires_live_sheet"]
    assert (ready.dir / cli.PREFLIGHT).read_bytes() == before


def test_the_whole_cutover_in_order_with_cli_written_evidence(any_world):
    world = any_world
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
    world.put(cli.JOURNAL, journal_document(measured=at(hours=-1)))
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


def keys_of(run) -> list[str]:
    return [line for line in run.out.splitlines() if not line.startswith("#")]


def apply_switch(world, epoch=1):
    """What `mia settings` does after the operator sets the switch keys in mia-env."""
    world.settings.assaf_os_backend = "postgres"
    world.settings.assaf_os_ledger_enabled = True
    world.settings.assaf_os_authority_epoch = epoch


def test_freeze_plan_prints_the_exact_keys_and_changes_nothing(ready):
    kept_drain = (ready.dir / cli.DRAIN).read_bytes()
    drain = ready.run("freeze-plan")
    assert drain.code == 0 and "MIA_ASSAF_OS_FREEZE=true\n" in drain.out
    assert "sudo mia settings" in drain.out
    # `--phase drain` starts a freeze, so it sets the earlier freeze's drain.json aside (V2-31b,
    # tested on its own). This test is about the ledger and the plans, and runs on to `activate`.
    (ready.dir / cli.DRAIN).write_bytes(kept_drain)
    before = ready.state()
    early = ready.run("freeze-plan", "--phase", "switch")
    assert early.code == 1 and codes(early) == ["authority_state_invalid"]
    assert ready.run("activate").code == 0
    switch = ready.run("freeze-plan", "--phase", "switch")
    # The freeze STAYS on through the switch and the mirror: no write is admitted until `open`.
    assert keys_of(switch) == [
        "MIA_ASSAF_OS_BACKEND=postgres", "MIA_ASSAF_OS_LEDGER_ENABLED=true",
        "MIA_ASSAF_OS_AUTHORITY_EPOCH=1",
    ]
    assert "MIA_ASSAF_OS_FREEZE stays true" in switch.out
    apply_switch(ready)
    mirror = ready.run("freeze-plan", "--phase", "mirror")
    assert keys_of(mirror) == ["MIA_ASSAF_OS_MIRROR_ENABLED=true"]
    assert "MIA_OS_MIRROR_SPREADSHEET_ID" in mirror.out and "NEW mirror workbook" in mirror.out
    opened = ready.run("freeze-plan", "--phase", "open")
    assert keys_of(opened) == ["MIA_ASSAF_OS_FREEZE=false"]
    assert "first non-import write" in opened.out
    assert before.states["core"] == "shadow" and ready.state().epoch == 1
    assert ready.state().states["core"] == "active", "printing a plan changes no state"
    # The way back is printed only once `rollback` has fenced the ledger (see the next test).
    assert ready.run("rollback").code == 0
    fenced = ready.state()
    revert = ready.run("freeze-plan", "--phase", "revert")
    assert keys_of(revert) == [
        "MIA_ASSAF_OS_BACKEND=sheets", "MIA_ASSAF_OS_LEDGER_ENABLED=false",
        "MIA_ASSAF_OS_AUTHORITY_EPOCH=0", "MIA_ASSAF_OS_MIRROR_ENABLED=false",
    ]
    assert ready.state() == fenced, "printing a plan changes no state"
    for text in (drain.out, switch.out, mirror.out, opened.out, revert.out):
        assert SECRET not in text and PEPPER not in text


def test_the_way_back_is_printed_only_after_the_rollback_fenced_the_ledger(ready):
    assert ready.run("activate").code == 0
    apply_switch(ready)
    early = ready.run("freeze-plan", "--phase", "revert")
    # While core is active the Sheets fence still refuses every write (`authority_moved`): the
    # plan would make the settings say `sheets` over a ledger that still owns the data.
    assert early.code == 1 and codes(early) == ["authority_state_invalid"]
    assert "MIA_ASSAF_OS_BACKEND" not in json.dumps(early.out), "no key is printed on a refusal"
    assert ready.state().states["core"] == "active"
    assert ready.run("rollback").code == 0
    assert ready.run("freeze-plan", "--phase", "revert").code == 0
    assert ready.run("freeze-plan", "--phase", "revert").code == 0  # idempotent
    # A ledger that is being restored is nobody's way back to Sheets.
    ready.env.transition(WORKSPACE, "core", "restoring", expected_epoch=1, actor_owner_id=OWNER_ID)
    assert codes(ready.run("freeze-plan", "--phase", "revert")) == ["authority_state_invalid"]


def test_each_later_phase_refuses_until_the_earlier_one_is_applied(ready):
    assert ready.run("activate").code == 0
    for phase in ("mirror", "open"):
        run = ready.run("freeze-plan", "--phase", phase)
        assert run.code == 1 and codes(run) == ["switch_not_applied"], phase
    # A switch applied for the wrong epoch, or only half of it, is not applied.
    for settings in (
        {"assaf_os_backend": "postgres", "assaf_os_ledger_enabled": True,
         "assaf_os_authority_epoch": 2},
        {"assaf_os_backend": "postgres", "assaf_os_ledger_enabled": False,
         "assaf_os_authority_epoch": 1},
        {"assaf_os_backend": "sheets", "assaf_os_ledger_enabled": True,
         "assaf_os_authority_epoch": 1},
    ):
        for name, value in settings.items():
            setattr(ready.settings, name, value)
        run = ready.run("freeze-plan", "--phase", "open")
        assert codes(run) == ["switch_not_applied"], settings
    apply_switch(ready)
    assert ready.run("freeze-plan", "--phase", "open").code == 0


def test_switch_and_mirror_refuse_when_the_freeze_is_already_off(ready):
    assert ready.run("activate").code == 0
    apply_switch(ready)
    ready.settings.assaf_os_freeze = False
    for phase in ("switch", "mirror"):
        run = ready.run("freeze-plan", "--phase", phase)
        assert run.code == 1 and codes(run) == ["freeze_off"], phase
    # Opening is what turns the freeze off, and a repeat of it is harmless.
    assert ready.run("freeze-plan", "--phase", "open").code == 0


def test_mirror_open_and_switch_need_an_active_core(ready):
    for phase in ("switch", "mirror", "open"):
        run = ready.run("freeze-plan", "--phase", phase)
        assert run.code == 1 and codes(run) == ["authority_state_invalid"], phase


def test_freeze_plan_after_a_re_arm_names_the_new_epoch(ready):
    ready.run("activate")
    ready.run("rollback")
    ready.run("init", "--rearm")
    assert ready.run("freeze-plan", "--phase", "revert").code == 0
    ready.env.transition(WORKSPACE, "core", "active", expected_epoch=2, actor_owner_id=OWNER_ID)
    switch = ready.run("freeze-plan", "--phase", "switch")
    assert "MIA_ASSAF_OS_AUTHORITY_EPOCH=2" in switch.out


# --------------------------------------------------------------------------- status and hygiene


def test_status_shows_the_boundary_and_the_mirror_the_cutover_decisions_turn_on(ready):
    ready.run("activate")
    assert ready.run("status").out["non_import_receipts"] == 0
    commit_direct(ready.env, ctx(), [task("TAS_w1")])
    assert ready.run("status").out["non_import_receipts"] == 1
    for state in ("verified", "unverified", "empty"):
        ready.probes.mirror_state = state
        assert ready.run("status").out["mirror"] == state


def test_status_reports_an_unreadable_observation_as_null_without_its_message(ready, caplog):
    ready.probes.broken = {"receipts", "mirror"}
    with caplog.at_level(logging.DEBUG):
        run = ready.run("status")
    assert run.code == 0
    assert run.out["non_import_receipts"] is None and run.out["mirror"] == "unknown"
    assert DRIVER_TEXT not in json.dumps(run.out) + run.err + caplog.text


def test_status_reports_authority_settings_and_evidence_without_secrets(ready, caplog):
    ready.settings.assaf_os_authority_epoch = 1
    with caplog.at_level(logging.DEBUG):
        run = ready.run("status")
    assert run.code == 0
    assert run.out["settings"] == {
        "backend": "sheets", "ledger_enabled": False, "authority_epoch": 1,
        "epoch_matches": True, "freeze": True, "mirror_enabled": False,
    }
    assert run.out["non_import_receipts"] == 0 and run.out["mirror"] == "empty"
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
