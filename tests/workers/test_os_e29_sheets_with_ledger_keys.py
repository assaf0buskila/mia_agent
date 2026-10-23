"""Contract §22 E29: ``MIA_ASSAF_OS_LEDGER_ENABLED=true`` and ``MIA_ASSAF_OS_AUTHORITY_EPOCH=E``
are set at T1, with ``MIA_ASSAF_OS_BACKEND`` still ``sheets``. This file proves they are inert for
everything but the import.

The import (G24 stage at T4, G12 promote at T5) runs as the ledger worker before the switch, so the
two keys must already be on. Every claim below is run with the keys on (``ON``, epoch 1) and with
them off (``OFF``, ``false``/``0``), under ``backend=sheets`` and ``core=shadow``, and compared:

  (a) the backend factory still returns the Sheets backend (the port) and never takes its postgres
      path;
  (b) a v1 propose is refused ``os_frozen`` under the freeze and a v1 decide still drains (I8): the
      drivers of ``tests/workers/test_os_freeze_paths.py`` (not edited), run with the keys injected;
  (c) the ledger core admits ONLY ``kind=import`` while ``core=shadow`` (FakeLedger, and PostgreSQL
      when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set): every other kind is refused and writes
      nothing;
  (d) Mia's overview, the Telegram cards and the mirror behave exactly as with the keys off;
  (e) a census: the readers of the two settings in ``app/`` are exactly the ones reviewed here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.api.os_v2 import reads
from app.domain.approvals import DECISION_APPROVED, DECISION_REJECTED
from app.services import assaf_os as port
from app.services import os_backend
from app.services.os_backend import get_os_backend
from app.services.os_ledger import api
from app.tools.owner.assaf_os import _assaf_os_snapshot
from app.workers import os_mirror
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.fakes.os_ledger_builders import (
    commit_direct,
    commit_import,
    counts,
    ctx,
    propose_task,
    rid,
    task,
)
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE, build_fake_env
from tests.services.os_ledger.conftest import PG_URL_ENV, build_pg_env, pg_enabled
from tests.unit import test_website_lead_approval as leads

# The drivers are the freeze census's own; this file only injects the two keys into the settings
# they build, so that the same real entry points run with the keys on.
from tests.workers import test_os_freeze_paths as freeze_paths

ROOT = Path(__file__).resolve().parents[2]
# The builders as they are on import, before any test patches them.
BUILD_ASSAF_SETTINGS = freeze_paths.assaf.settings
BUILD_LEADS_SETTINGS = freeze_paths.leads._settings


@dataclass(frozen=True)
class Keys:
    label: str
    enabled: bool
    epoch: int


OFF = Keys("off", False, 0)
ON = Keys("on", True, 1)  # core is `shadow` at epoch 1, as at T1 to T5


@pytest.fixture(autouse=True)
def clean_caches():
    port._clear_assaf_os_decision_binding_cache()
    yield
    port._clear_assaf_os_decision_binding_cache()


def inject(monkeypatch, keys: Keys) -> None:
    """Make every settings object the freeze drivers build carry ``keys``."""
    def assaf_settings():
        value = BUILD_ASSAF_SETTINGS()
        value.assaf_os_ledger_enabled, value.assaf_os_authority_epoch = keys.enabled, keys.epoch
        return value

    def leads_settings(**changes):
        return BUILD_LEADS_SETTINGS(**{
            "assaf_os_ledger_enabled": keys.enabled, "assaf_os_authority_epoch": keys.epoch,
            **changes,
        })

    monkeypatch.setattr(freeze_paths.assaf, "settings", assaf_settings)
    monkeypatch.setattr(freeze_paths.leads, "_settings", leads_settings)


def fresh_sessions():
    """A new owner-session database per run: a second login in one database is not a fresh start."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        freeze_paths.auth._schema(db)
    return factory, engine


def drive(driver, monkeypatch, tmp_path, caplog, keys: Keys, *args, authority="shadow"):
    """One real entry point of the freeze census, with ``keys`` on its settings."""
    inject(monkeypatch, keys)
    port._clear_assaf_os_decision_binding_cache()
    directory = tmp_path / keys.label
    directory.mkdir(exist_ok=True)
    factory, engine = fresh_sessions()
    try:
        env = freeze_paths.Env(monkeypatch, factory, directory, caplog, authority)
        return driver(env, *args)
    finally:
        engine.dispose()


# ------------------------------------------------------------------ (a) the factory


def test_a_the_factory_returns_the_sheets_backend_and_never_takes_the_postgres_path(monkeypatch):
    def postgres_path(*_args, **_kwargs):
        raise AssertionError("backend=sheets must not reach the postgres factory path")

    monkeypatch.setattr(os_backend, "_postgres_backend", postgres_path)
    seen = {}
    for keys in (OFF, ON):
        settings = leads._settings(
            assaf_os_backend="sheets", assaf_os_freeze=True,
            assaf_os_ledger_enabled=keys.enabled, assaf_os_authority_epoch=keys.epoch,
        )
        assert (settings.assaf_os_ledger_enabled, settings.assaf_os_authority_epoch) == (
            keys.enabled, keys.epoch), "the loader keeps both keys"
        backends = [
            get_os_backend(settings, leads.OWNER, actor=actor)
            for actor in ("MIA", "OS", "TELEGRAM_APPROVAL")
        ]
        seen[keys.label] = [
            (type(backend), backend.kind, type(backend._port)) for backend in backends
        ]
    assert seen["on"] == seen["off"]
    assert all(
        backend_type is os_backend.SheetsOsBackend and kind == "sheets"
        and port_type is port.AssafOsPort
        for backend_type, kind, port_type in seen["on"]
    )


def test_a_the_postgres_path_still_needs_the_backend_to_say_postgres(monkeypatch):
    # The keys alone never select the ledger: only `assaf_os_backend == "postgres"` does.
    calls = []
    monkeypatch.setattr(os_backend, "_postgres_backend", lambda *a, **k: calls.append(a) or "pg")
    settings = leads._settings(
        assaf_os_backend="sheets", assaf_os_ledger_enabled=True, assaf_os_authority_epoch=1)
    assert get_os_backend(settings, leads.OWNER, actor="MIA").kind == "sheets" and not calls
    settings.assaf_os_backend = "postgres"
    assert get_os_backend(settings, leads.OWNER, actor="MIA") == "pg" and len(calls) == 1


# ------------------------------------------------------------------ (b) v1 propose and decide


@pytest.fixture
def census(monkeypatch, tmp_path, caplog):
    def run(driver, keys, *args, **kw):
        return drive(driver, monkeypatch, tmp_path, caplog, keys, *args, **kw)

    return run


@pytest.mark.parametrize(
    "site", sorted(freeze_paths.INTENT_DRIVERS), ids=lambda s: f"{s[0]}::{s[1]}")
def test_b_a_v1_intent_is_refused_os_frozen_with_the_keys_on_exactly_as_with_them_off(
    census, site
):
    driver = freeze_paths.INTENT_DRIVERS[site]
    off, on = census(driver, OFF, True), census(driver, ON, True)
    assert off.refused == "os_frozen" and on.refused == "os_frozen", (site, off, on)
    assert on == off, "the same refusal and the same calls to the Script"
    assert "propose" not in on.calls, "the Script was never asked"
    assert set(on.calls) <= {"snapshot", "schema", "proposal", "receipt"}


@pytest.mark.parametrize(
    "site", sorted(freeze_paths.INTENT_DRIVERS), ids=lambda s: f"{s[0]}::{s[1]}")
def test_b_the_same_v1_intent_reaches_the_script_before_the_freeze_with_the_keys_on(census, site):
    # A refusal can only come from the freeze: with it off, the keys on change nothing.
    driver = freeze_paths.INTENT_DRIVERS[site]
    off, on = census(driver, OFF, False), census(driver, ON, False)
    assert on == off and on.refused != "os_frozen" and "propose" in on.calls, (site, on)


@pytest.mark.parametrize(
    "key", sorted(freeze_paths.DECISION_DRIVERS), ids=freeze_paths.decision_id)
def test_b_a_v1_decision_still_drains_under_the_freeze_with_the_keys_on(census, key):
    driver = freeze_paths.DECISION_DRIVERS[key]
    off, on = census(driver, OFF, True), census(driver, ON, True)
    assert on.refused is None and key[1] in on.calls, (key, on)
    assert on == off, "approve and reject reach the Script exactly as with the keys off"


@pytest.mark.parametrize(
    "key", sorted(freeze_paths.DECISION_DRIVERS), ids=freeze_paths.decision_id)
def test_b_the_sheets_fence_still_refuses_a_decision_once_core_is_active_with_the_keys_on(
    census, key
):
    # The other end of the cutover is unchanged too: after activate the Script may not settle.
    driver = freeze_paths.DECISION_DRIVERS[key]
    off = census(driver, OFF, True, authority="active")
    on = census(driver, ON, True, authority="active")
    assert on.refused is not None and key[1] not in on.calls and on == off


# ------------------------------------------------------------------ (c) the ledger core


CASES = ["fake", *(["pg"] if pg_enabled() else [])]


@pytest.fixture(params=CASES)
def ledger(request):
    env = build_fake_env() if request.param == "fake" else build_pg_env(os.environ[PG_URL_ENV])
    env.ensure_workspace(WORKSPACE, OWNER_ID, 1)  # core = shadow, epoch 1
    assert env.state(WORKSPACE).states["core"] == "shadow"
    return env


def set_keys(env, keys: Keys, *, backend: str, freeze: bool) -> None:
    env.knobs.enabled, env.knobs.settings_epoch = keys.enabled, keys.epoch
    env.knobs.backend, env.knobs.freeze, env.knobs.kill_switch = backend, freeze, False


DISABLED = "ledger_unavailable:ledger_disabled"  # the core's step 2: the ledger switch is off


def refused(call) -> str:
    """``code`` or ``code:reason`` of the refusal; the call must not be admitted."""
    try:
        call()
    except api.LedgerError as error:
        return error.code + (f":{error.reason}" if error.reason else "")
    raise AssertionError("the ledger admitted a write it must refuse")


def enter(env, kind: str):
    def call():
        with env.transaction(ctx(), domain="core", kind=kind):
            pass

    return call


NON_IMPORT_KINDS = ("proposal", "owner_direct", "owner_undo", "statement_batch")


def attempts(env) -> dict[str, str]:
    """Every way to write that is not an import, as the refusal code each one gets."""
    codes = {kind: refused(enter(env, kind)) for kind in NON_IMPORT_KINDS}
    codes["propose"] = refused(lambda: propose_task(env))
    codes["decide"] = refused(lambda: env.decide(ctx(), "PRO_none", rid(), "approve"))
    codes["reject"] = refused(lambda: env.decide(ctx(), "PRO_none", rid(), "reject"))
    return codes


@pytest.mark.parametrize("freeze", [True, False], ids=["frozen", "unfrozen"])
def test_c_under_sheets_the_core_admits_only_an_import_while_it_is_shadow(ledger, freeze):
    env = ledger
    results = {}
    for keys in (OFF, ON):
        set_keys(env, keys, backend="sheets", freeze=freeze)
        before = counts(env)
        results[keys.label] = attempts(env)
        assert counts(env) == before, f"keys {keys.label}: a refused write left a row"
    # With the keys off nothing is admitted either: the same operations are refused, by the switch.
    assert set(results["off"]) == set(results["on"])
    for operation, code in results["off"].items():
        assert code == ("os_frozen" if freeze and operation == "propose" else DISABLED), operation
    # With them on, the BACKEND is what refuses (and for a proposal under the freeze, the freeze).
    for operation, code in results["on"].items():
        expected = "os_frozen" if freeze and operation == "propose" else "backend_not_postgres"
        assert code == expected, (operation, code)
    assert env.state(WORKSPACE).states["core"] == "shadow"


def test_c_the_import_is_refused_with_the_keys_off_and_admitted_with_them_on(ledger):
    env = ledger
    set_keys(env, OFF, backend="sheets", freeze=True)
    before = counts(env)
    assert refused(lambda: commit_import(env, ctx(), [task("TAS_i1")], "d" * 64)) == DISABLED
    assert counts(env) == before
    set_keys(env, ON, backend="sheets", freeze=True)  # T1 keys: the freeze is on too
    proposal, receipt = commit_import(env, ctx(), [task("TAS_i1")], "d" * 64)
    assert (proposal.kind, receipt.kind, receipt.status) == ("import", "import", "committed")
    after = counts(env)
    assert after["os_receipts"] == before["os_receipts"] + 1
    assert after["os_proposals"] == before["os_proposals"] + 1
    # ...and it did not make the ledger writable for anything else.
    assert all(code == "backend_not_postgres" for operation, code in attempts(env).items()
               if operation != "propose")
    assert env.state(WORKSPACE).states["core"] == "shadow"


@pytest.mark.parametrize("freeze", [True, False], ids=["frozen", "unfrozen"])
def test_c_even_a_postgres_backend_cannot_write_before_core_is_active(ledger, freeze):
    # The second wall: if the backend ever said postgres early, `core=shadow` still admits only
    # imports (authority_not_active), with the keys on.
    env = ledger
    set_keys(env, ON, backend="postgres", freeze=freeze)
    before = counts(env)
    codes = attempts(env)
    assert counts(env) == before
    for operation, code in codes.items():
        expected = "os_frozen" if freeze and operation == "propose" else "authority_not_active"
        assert code == expected, (operation, code)
    commit_import(env, ctx(), [task("TAS_i2")], "c" * 64)  # an import is still admitted


def test_c_an_activated_core_is_what_makes_it_writable_not_the_keys(ledger):
    # The reverse check: the very same keys and a postgres backend DO admit a commit once core is
    # active, so the refusals above come from `shadow`, not from a test that cannot write.
    env = ledger
    set_keys(env, ON, backend="postgres", freeze=False)
    commit_import(env, ctx(), [task("TAS_i3")], "b" * 64)
    env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    _proposal, receipt = commit_direct(env, ctx(), [task("TAS_w1")])
    assert receipt.kind == "owner_direct" and receipt.status == "committed"


# ------------------------------------------------------------------ (d) Mia, Telegram, mirror


def test_d_mias_overview_is_the_same_with_the_keys_on(monkeypatch, tmp_path):
    results = {}
    for keys in (OFF, ON):
        config = freeze_paths.leads._settings(
            assaf_os_freeze=True, assaf_os_ledger_enabled=keys.enabled,
            assaf_os_authority_epoch=keys.epoch,
        )
        calls: list[str] = []
        monkeypatch.setattr(port, "_post", freeze_paths.transport(config, calls))
        freeze_paths.open_authority(monkeypatch)
        directory = tmp_path / keys.label
        directory.mkdir()
        store, session, engine = freeze_paths.new_store(directory)
        try:
            result = _assaf_os_snapshot(freeze_paths.tool_context(store, config), {})
        finally:
            session.close()
            engine.dispose()
        body = json.loads(result.text)
        body.pop("observed_at")  # the Sheet read's own clock
        results[keys.label] = (result.ok, result.error, result.evidence, result.max_chars, body,
                               calls)
    assert results["on"][0] is True and results["on"][5] == ["snapshot"]
    assert results["on"] == results["off"], "the same overview from the same Sheet read"
    assert results["on"][2] == "assaf_os_signed_snapshot" and results["on"][4]["source"] == (
        "google_sheets")


@pytest.mark.parametrize("decision", [DECISION_APPROVED, DECISION_REJECTED])
def test_d_a_telegram_card_settles_the_same_with_the_keys_on(census, decision):
    off = census(freeze_paths.drive_telegram, OFF, True, decision)
    on = census(freeze_paths.drive_telegram, ON, True, decision)
    assert on.refused is None and on == off, "approve and reject still reach the Script"
    assert ("approve" if decision == DECISION_APPROVED else "reject") in on.calls


def test_d_a_lead_card_is_still_refused_os_frozen_with_the_keys_on(census):
    off = census(freeze_paths.drive_leads, OFF, True)
    on = census(freeze_paths.drive_leads, ON, True)
    assert on.refused == "os_frozen" and on == off


def test_d_the_mirror_does_not_start_and_pauses_for_the_same_reason(monkeypatch):
    spawned = []
    monkeypatch.setattr(os_mirror, "_spawn", spawned.append)
    reasons = {}
    for keys in (OFF, ON):
        settings = leads._settings(
            assaf_os_freeze=True, assaf_os_ledger_enabled=keys.enabled,
            assaf_os_authority_epoch=keys.epoch,
        )
        os_mirror.start_if_enabled(settings)
        reasons[keys.label] = os_mirror.pause_reason(settings)
    assert spawned == [], "the publisher starts only with MIA_ASSAF_OS_MIRROR_ENABLED (T7)"
    assert reasons == {"off": "disabled", "on": "disabled"}


def test_d_the_v2_routes_stay_off_under_sheets_before_any_context_is_minted(monkeypatch):
    # `authorize._binding` reads the epoch for every minted context; the only v2 route that mints
    # one in this tree sits behind a guard that answers 404 off the postgres backend.
    minted = []
    monkeypatch.setattr(reads.authorize, "from_owner_read", lambda *a, **k: minted.append(a))
    for keys in (OFF, ON):
        settings = leads._settings(
            assaf_os_backend="sheets", assaf_os_ledger_enabled=keys.enabled,
            assaf_os_authority_epoch=keys.epoch,
        )
        with pytest.raises(api.LedgerError) as error:
            reads.route_guard(SimpleNamespace(headers={}), settings)
        assert (error.value.code, error.value.reason) == (
            "operation_unavailable", "backend_not_postgres")
    assert minted == []


# ------------------------------------------------------------------ (e) the census of readers


SETTING_NAMES = ("assaf_os_ledger_enabled", "assaf_os_authority_epoch")

# Every file in app/ that names either setting, and why it is safe under backend=sheets. A new file
# here is a new reader: it must be reviewed against §22 E29 before it is added.
READERS = {
    "app/core/config.py": "the fields and their coercion",
    "app/services/os_backend.py": "the postgres factory path only (backend == postgres)",
    "app/services/os_ledger/authorize.py": "the epoch of a minted context (gated callers only)",
    "app/services/os_ledger/core.py": "the ledger core: admits only kind=import at core=shadow",
    "app/workers/os_authority.py": "the cutover CLI (status, freeze-plan guards)",
}


def test_e_the_readers_of_the_two_settings_are_the_reviewed_ones():
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = [name for name in SETTING_NAMES if re.search(rf"\b{name}\b", text)]
        if hits:
            found[path.relative_to(ROOT).as_posix()] = hits
    assert set(found) == set(READERS), sorted(set(found) ^ set(READERS))


def test_e_no_script_reads_the_two_environment_keys():
    names = ("MIA_ASSAF_OS_LEDGER_ENABLED", "MIA_ASSAF_OS_AUTHORITY_EPOCH")
    users = []
    for folder in ("scripts", "ops", "deploy"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in {".py", ".sh", ".ps1", ".yaml", ".yml", ".json"} and path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                if any(name in text for name in names):
                    users.append(path.relative_to(ROOT).as_posix())
    assert users == [], users
