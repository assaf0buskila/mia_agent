"""V2-10: `mia restore` re-applies the roles before the services start (V2_PG_CONTRACTS.md 18.1).

`mia restore` runs `pg_restore --no-owner --no-privileges` as mia. Every object is then owned by
mia and every grant is gone, so mia_app could not read its own tables and the assaf_os ownership
and grants would be lost. The restore arm therefore runs a post-step between pg_restore and
start_services, under the state lock it already holds:

  1. app_role_demote.py --reapply        (V2-14a; a direct call, the wrapper's flock is busy)
  2. provision_ledger.py --mode reapply  (V2-15; in the deployed image, only if assaf_os exists)
  3. V2-14's real _validate_main_role_catalog on the real main DSN (only when it is mia_app)
  4. owner_os_revocations.revoked_before = restore time, in the owner-auth database
  5. a REAL SELECT and INSERT (rolled back) as the main DSN role

Any failure prints one fixed reason code and stops with the services DOWN.

Three layers: static facts about the script, the post-step functions run under bash with a stubbed
compose (always run), and the drill on a real PostgreSQL (skipped only when
MIA_RESTORE_DRILL_TEST_POSTGRES_URL is unset). The drill runs the real restore arm, the real
app_role_demote.py, the real V2-14 migration and the real V2-15 provision_ledger.py, in both its
provision and its reapply mode, and asserts the real validators: V2-14's _validate_main_role_catalog
and V2-15's validate_ledger_catalog and validate_ledger_role. The only substitution is the pin table
of provision_ledger.py (tests/ops/restore_ledger_driver.py). It needs V2-15 (#170) on the branch and
fails loudly, rather than skipping, when ops/assaf-os/provision_ledger.py is missing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import backup_lib
import pytest

from tests.unit.test_ci_os_ledger_postgres import ROOT, _bash

MIA = ROOT / "ops/gcp/vm/mia"
SCRIPT = MIA.read_text(encoding="utf-8")
LEDGER_SCRIPT = ROOT / "ops/assaf-os/provision_ledger.py"
LEDGER_DRIVER = ROOT / "tests/ops/restore_ledger_driver.py"
DRIVER = ROOT / "tests/ops/restore_demote_driver.py"
DEMOTE_PY = ROOT / "ops/gcp/vm/app_role_demote.py"
EXCLUDE = "--exclude-table-data=assaf_os.os_statement_batch_lines"
URL_VARIABLE = "MIA_RESTORE_DRILL_TEST_POSTGRES_URL"
CONTAINER = os.getenv("MIA_RESTORE_DRILL_TEST_PG_CONTAINER", "assaf-os-v2-10-restore-pg")
PASSWORD = "ab" * 32  # the main DSN password: it must never reach an argv, a log or an output

STEPS = (
    "restore_demote_reapply",
    "restore_ledger_reapply",
    "restore_main_role_validate",
    "restore_revoke_owner_sessions",
    "restore_access_probe",
)
# stage -> (stub knob, reason code)
FAILURES = {
    "demote": "app_role_reapply_failed",
    "ledger": "ledger_reapply_failed",
    "validator": "main_role_validation_failed",
    "revoke": "owner_session_revoke_failed",
    "probe": "app_role_access_probe_failed",
}


def _prelude() -> str:
    """Everything `mia` defines before it dispatches a command: variables and functions."""
    return SCRIPT.split('\ncmd="${1:-}"; shift || true\n', 1)[0]


def _arm(name: str) -> str:
    return SCRIPT.split(f"\n  {name})\n", 1)[1].split("\n    ;;", 1)[0]


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", SCRIPT, re.MULTILINE | re.DOTALL)
    assert match, f"mia defines no function {name}"
    return match.group(0)


def _comment_above(name: str) -> str:
    """The comment block directly above `name() {`."""
    head = SCRIPT.split(f"\n{name}() {{\n", 1)[0].splitlines()
    block: list[str] = []
    for line in reversed(head):
        if not line.startswith("#"):
            break
        block.append(line)
    return "\n".join(reversed(block))


def _code(text: str) -> str:
    """Executable lines only, so a comment may mention what the code must not do."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _posix(path: Path) -> str:
    return path.as_posix()


def _msys(path: Path) -> str:
    """A path that is safe inside a colon-separated PATH under Git Bash (C:/x -> /c/x)."""
    text = path.as_posix()
    return f"/{text[0].lower()}{text[2:]}" if re.match(r"^[A-Za-z]:/", text) else text


# --- static facts ---------------------------------------------------------------------------


# Names the VM owns: the main and ledger DSNs, the ledger family, the posture.
VM_OWNED = (
    "MIA_DATABASE_URL",
    "POSTGRES_PASSWORD",
    "PGHOST",
    "MIA_OWNER_OS_LEDGER_DATABASE_URL",
    "MIA_OS_LEDGER_MIGRATION_DATABASE_URL",
    "MIA_OS_LEDGER_DATABASE_URL",
    "MIA_APP_ROLE_POSTURE",
)
# Owner OS settings that provision_auth.py wrote into mia-env and that must keep coming from it
# (E10): the enable flags, the Google keys, the session secret and DSN, the origin.
FROM_MIA_ENV = (
    "MIA_OWNER_OS_ENABLED",
    "MIA_OWNER_OS_ACTIONS_ENABLED",
    "MIA_OWNER_OS_GOOGLE_ENABLED",
    "MIA_OWNER_OS_GOOGLE_CLIENT_ID",
    "MIA_OWNER_OS_GOOGLE_SUBJECT",
    "MIA_OWNER_OS_GOOGLE_OWNER_ID",
    "MIA_OWNER_OS_SESSION_SECRET",
    "MIA_OWNER_OS_SESSION_DATABASE_URL",
    "MIA_OWNER_OS_ORIGIN",
    "MIA_OWNER_OS_WORKSPACE_ID",
    "MIA_OWNER_OS_SESSION_SCOPES",
    "MIA_OPENAI_API_KEY",
)


def test_the_owned_pattern_is_exactly_the_ledger_family_and_the_posture():
    match = re.search(r'owned = re\.compile\(r"(.+?)"\)', SCRIPT)
    assert match, "fetch_settings has no owned pattern"
    assert match.group(1) == (
        r"^(MIA_DATABASE_URL|POSTGRES_\w+|PG\w+"
        r"|MIA_OWNER_OS_LEDGER_\w+|MIA_OS_LEDGER_\w+|MIA_APP_ROLE_POSTURE)\s*="
    )
    owned = re.compile(match.group(1))
    for name in VM_OWNED:
        assert owned.match(f"{name}=x"), name
        assert owned.match(f"{name} = x"), name
    near_misses = ("MIA_OWNERSHIP", "MIA_OS_LEDGERS", "MIA_APP_ROLE_POSTURES", "MIA_OWNER_OS_LEDGE")
    for name in (*FROM_MIA_ENV, *near_misses):
        assert not owned.match(f"{name}=x"), name


def test_fetch_settings_strips_the_vm_owned_keys_and_keeps_every_other_owner_os_key(tmp_path):
    """The real filter loop of `mia settings`, run on a fake Secret Manager payload."""
    import base64
    import json
    from unittest import mock

    source = re.search(r"fetch_settings\(\) \{\n  python3 - <<'PY'\n(.*?)\nPY\n", SCRIPT, re.DOTALL)
    assert source, "fetch_settings has no inline python"
    code = source.group(1).replace("/etc/mia/settings.env", _posix(tmp_path / "settings.env"))
    secret = "\n".join([
        *(f"{name}=value-{number}" for number, name in enumerate((*VM_OWNED, *FROM_MIA_ENV))),
        "export MIA_OWNER_OS_LEDGER_RETENTION_DATABASE_URL=export-prefixed",
        "export MIA_OWNER_OS_ORIGIN=https://exported.example",
    ])

    class Reply:
        def __init__(self, body: bytes):
            self.body = body

        def read(self):
            return self.body

    def urlopen(request, timeout=0):
        url = request.full_url
        if "secretmanager" in url:
            payload = {"payload": {"data": base64.b64encode(secret.encode()).decode()}}
            return Reply(json.dumps(payload).encode())
        if url.endswith("service-accounts/default/token"):
            return Reply(json.dumps({"access_token": "t"}).encode())
        return Reply(b"project-x")

    with mock.patch("urllib.request.urlopen", urlopen):
        exec(compile(code, "mia-fetch-settings", "exec"), {"__name__": "__main__"})
    kept = {
        line.split("=", 1)[0]
        for line in (tmp_path / "settings.env").read_text(encoding="utf-8").splitlines()
    }
    assert kept == {*FROM_MIA_ENV}  # MIA_OWNER_OS_ORIGIN survives with and without `export `
    for name in VM_OWNED:
        assert name not in kept, name
    assert "MIA_OWNER_OS_LEDGER_RETENTION_DATABASE_URL" not in kept


def test_every_dump_leaves_the_statement_batch_line_detail_out():
    pg_dumps = [
        line for line in SCRIPT.splitlines() if "pg_dump" in line and "compose exec" in line
    ]
    assert len(pg_dumps) == 2  # the pre-restore safety dump and `mia backup`
    assert all('"${dump_exclude[@]}"' in line for line in pg_dumps)
    assert f"dump_exclude=({EXCLUDE})" in SCRIPT


def test_the_restore_arm_runs_the_post_step_between_pg_restore_and_start_services():
    arm = _code(_arm("restore"))
    restore = arm.index("pg_restore -U mia -d mia --no-owner --no-privileges --exit-on-error")
    post = arm.index("post_restore_reapply")
    start = arm.index("start_services")
    assert restore < post < start
    # still under the state lock `mia restore` takes, and no second flock on it
    lock_arm = SCRIPT.split('case "$cmd" in\n', 1)[1].split("exec 9>", 1)[0]
    assert "restore" in lock_arm
    assert "flock" not in arm and "flock" not in _code(_function("post_restore_reapply"))


def test_the_helper_is_called_directly_never_through_the_locking_wrapper():
    code = _code(SCRIPT)
    assert 'python3 "$app_role_dir/app_role_demote.py" --reapply' in code
    assert "app-role-demote.sh" not in code
    assert "app_role_dir=/opt/mia/app-role-demote" in code


def test_the_post_step_runs_its_five_steps_in_the_contract_order():
    body = _code(_function("post_restore_reapply"))
    positions = [body.index(step) for step in STEPS]
    assert positions == sorted(positions)


def test_the_ledger_reapply_runs_the_deployed_script_in_the_deployed_image():
    body = _code(_function("restore_ledger_reapply"))
    for fragment in (
        "compose --profile os-ledger run --rm --no-deps -T --user root",
        # the script is the one deployed with the image's source, mounted at its repo path
        '-v "$script:/app/ops/assaf-os/provision_ledger.py:ro"',
        # E10: a restore never writes a VM-owned file, so /etc/mia is read-only in the container
        "-v /etc/mia:/etc/mia:ro",
        "--entrypoint sh os-ledger-migrate",
        "provision_ledger.py --target mia-vm --mode reapply --apply --approved-sha "
        '"$MIA_BUILD_SHA"',
        "/opt/mia/src/$tag/ops/assaf-os/provision_ledger.py",
    ):
        assert fragment in body, fragment
    assert "ledger_reapply_unavailable" in body and "ledger_reapply_failed" in body


def test_the_ledger_env_check_is_the_same_program_the_os_ledger_command_uses():
    awk = re.compile(r"awk '\n(.*?)\n\s*' ", re.DOTALL)
    command = awk.search(_arm("os-ledger"))
    reapply = awk.search(_function("restore_ledger_reapply"))
    assert command and reapply
    assert command.group(1).strip() == reapply.group(1).strip()


def test_secrets_never_travel_in_an_argument():
    body = _code(_function("restore_access_probe"))
    assert 'printf \'%s\\n\' "$main_password" |' in body  # stdin only
    assert "PGPASSWORD=" not in body
    assert body.count("$main_password") == 1
    assert "set +x" in _code(_function("post_restore_reapply"))


def test_the_reason_codes_are_the_fixed_set_the_runbook_documents():
    emitted = set(re.findall(r"restore_fail (\w+)", _code(SCRIPT)))
    assert emitted == {
        "restore_catalog_unreadable",
        "app_role_reapply_unavailable",
        "app_role_reapply_failed",
        "ledger_reapply_unavailable",
        "ledger_reapply_failed",
        "main_dsn_invalid",
        "main_role_validation_failed",
        "owner_session_revoke_failed",
        "app_role_access_probe_failed",
        "restore_evidence_invalid",
    }
    runbook = (ROOT / "ops/gcp/backup/README.md").read_text(encoding="utf-8")
    for code in emitted:
        assert f"`{code}`" in runbook, code


def test_the_offsite_backup_dump_leaves_the_batch_line_detail_out_too():
    assert backup_lib.DUMP_EXCLUDE_TABLE_DATA == "assaf_os.os_statement_batch_lines"
    assert f"--exclude-table-data={backup_lib.DUMP_EXCLUDE_TABLE_DATA}" == EXCLUDE


def _real_ledger_script():
    spec = importlib.util.spec_from_file_location("v210_provision_ledger", LEDGER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_drill_runs_the_deployed_ledger_script_and_no_stand_in_remains():
    """V2-10 review r1, must-fix: the V2-15 stand-in is deleted; the real script is the one run."""
    assert LEDGER_SCRIPT.is_file(), "V2-15 (ops/assaf-os/provision_ledger.py) is a prerequisite"
    assert not (ROOT / ("tests/ops/" + "stand" + "in_provision_ledger.py")).exists()
    here = Path(__file__).read_text(encoding="utf-8")
    assert 'shutil.copy(LEDGER_SCRIPT, src / "provision_ledger.py")' in here
    for path in (ROOT / "tests/ops").glob("*.py"):
        assert "stand" + "in_provision_ledger" not in path.read_text(encoding="utf-8"), path.name


def test_mia_calls_the_real_script_with_flags_it_accepts_and_the_pin_the_driver_swaps():
    script = _real_ledger_script()
    driver_spec = importlib.util.spec_from_file_location("v210_ledger_driver", LEDGER_DRIVER)
    driver = importlib.util.module_from_spec(driver_spec)
    driver_spec.loader.exec_module(driver)
    # the pin `mia restore` relies on is the script's own, field for field
    assert tuple(script._TARGETS["mia-vm"]) == driver.PRODUCTION_PIN
    call = re.search(r"python /app/ops/assaf-os/provision_ledger\.py ([^\"']+)", SCRIPT)
    assert call, "mia restore no longer calls provision_ledger.py"
    assert call.group(1).split()[:5] == ["--target", "mia-vm", "--mode", "reapply", "--apply"]
    # the dry-run of the same target and mode is the exact plan reapply executes, with no database
    plan = script.provision_statements("mia-vm", "reapply")
    for statement in (
        "ALTER SCHEMA assaf_os OWNER TO assaf_os_owner;",
        "REVOKE TEMPORARY ON DATABASE mia FROM PUBLIC;",
        "GRANT CONNECT ON DATABASE mia TO assaf_os_ledger;",
    ):
        assert statement in plan, statement
    # the plan ends with the whole reviewed grant set, in order: V2-15's, then V2-14b's retention
    # grants. Pinning plan[-1] to one role broke when V2-14b appended its own grants.
    from app.services.os_ledger.role_policy import ledger_grant_statements

    grants = ledger_grant_statements()
    assert len(grants) > 1
    assert tuple(plan[-len(grants):]) == grants
    assert "GRANT USAGE ON SCHEMA assaf_os TO assaf_os_retention;" in grants  # V2-14b is in it


def test_the_restore_arm_pauses_the_jobs_before_it_drops_the_database():
    """V2-10 review r1: no job timer may touch a dropped, half-restored or half-repaired database.

    `mia job` takes no state lock, so the pause is a marker, created before DROP DATABASE and left
    in place for the operator to remove after readiness (`mia jobs resume`), as the runbook says.
    """
    arm = _code(_arm("restore"))
    marker = arm.index('install -m 600 /dev/null "$jobs_paused"')
    assert marker < arm.index("DROP DATABASE") < arm.index("pg_restore -U mia -d mia")
    assert "jobs resume" in _arm("restore")  # the operator is told how to undo it


def test_the_comments_make_no_claim_the_restore_steps_cannot_keep():
    """V2-10 review r1: the loopback pg_hba line is `trust`, so a password is never checked there.

    The postgres image's initdb writes `host all all 127.0.0.1/32 trust` above the line
    POSTGRES_HOST_AUTH_METHOD appends, and compose runs postgres and the app on the host network.
    Steps 3 and 5 therefore prove the role's posture and privileges, not its password. The README
    must not say a wrong password fails, and the script must not say it prints nothing it prints.
    """
    assert "Nothing here prints SQL" not in SCRIPT
    assert "Nothing here prints DSNs, passwords or row data" in SCRIPT
    runbook = (ROOT / "ops/gcp/backup/README.md").read_text(encoding="utf-8")
    assert "or the DSN does not authenticate" not in runbook
    assert "loopback authentication is not proven" in runbook.lower()
    for name in ("restore_main_role_validate", "restore_access_probe"):
        assert "`trust`" in _comment_above(name), name


# --- the post-step functions under bash with a stubbed compose ---------------------------------

STUB = r"""
log() { printf '%s' "$*" | tr '\n' ' ' >> "$STUB_LOG"; printf '\n' >> "$STUB_LOG"; }
compose() {
  log "compose $*"
  case "$*" in
    *"rolname='mia_app'"*) printf '%s\n' "$STUB_ROLE" ;;
    *"to_regnamespace('assaf_os')"*) printf '%s\n' "$STUB_LEDGER" ;;
    *"datname='assaf_os_auth'"*) printf '%s\n' "$STUB_AUTHDB" ;;
    *"relname IN ('owner_os_"*) printf '%s\n' "$STUB_TABLES" ;;
    *"read -r PGPASSWORD"*) cat > /dev/null; [ "$STUB_FAIL" != probe ] ;;
    *" -1"*) cat > /dev/null; [ "$STUB_FAIL" != revoke ] ;;
    *"os-ledger-migrate"*) [ "$STUB_FAIL" != ledger ] ;;
    *" app "*) [ "$STUB_FAIL" != validator ] ;;
    *) : ;;
  esac
}
python3() {
  log "python3 $*"
  [ "$STUB_FAIL" != demote ] || { echo "reason=app_role_check_failed ok=false"; return 1; }
}
start_services() { log START_SERVICES; }
"""


class Rig:
    """A fake VM directory tree plus the knobs the stubbed compose answers from."""

    def __init__(self, tmp: Path, **knobs):
        self.tmp = tmp
        self.log = tmp / "stub.log"
        self.knobs = {
            "role": "1",
            "ledger": "t",
            "authdb": "1",
            "tables": "3",
            "fail": "",
            "dsn_role": "mia_app",
            "demote_files": True,
            "ledger_script": True,
            "migrate_env": True,
            "tag": "testtag",
            **knobs,
        }
        k = self.knobs
        (tmp / "demote/sql").mkdir(parents=True)
        if k["demote_files"]:
            (tmp / "demote/app_role_demote.py").write_text("# stub\n")
            (tmp / "demote/sql/app-role-demote.sql").write_text("-- stub\n")
        script = tmp / f"src/{k['tag']}/ops/assaf-os"
        script.mkdir(parents=True)
        if k["ledger_script"]:
            (script / "provision_ledger.py").write_text("# stub\n")
        (tmp / "tag.env").write_text(f"MIA_TAG={k['tag']}\n")
        dsn = k.get("dsn") or f"postgresql://{k['dsn_role']}:{PASSWORD}@127.0.0.1:5432/mia"
        (tmp / "runtime.env").write_text(f"MIA_DATABASE_URL={dsn}\n")
        if k["migrate_env"]:
            (tmp / "migrate.env").write_text(
                "MIA_OS_LEDGER_MIGRATION_DATABASE_URL=postgresql://mia:x@127.0.0.1:5432/mia\n"
            )

    def paths(self) -> dict[str, str]:
        return vm_paths(self.tmp)


def vm_paths(t: Path) -> dict[str, str]:
    """The VM's absolute paths `mia` uses, mapped into a scratch tree."""
    return {
        "/opt/mia/app-role-demote": _posix(t / "demote"),
        "/opt/mia/src": _posix(t / "src"),
        "/opt/mia/tag.env": _posix(t / "tag.env"),
        "/etc/mia/runtime.env": _posix(t / "runtime.env"),
        "/etc/mia/os-ledger-migrate.env": _posix(t / "migrate.env"),
        "/var/lib/mia/backups": _posix(t / "backups"),
        "/var/lib/mia/os-authority": "./os-authority",
    }


def _localize(text: str, mapping: dict[str, str]) -> str:
    for literal, local in mapping.items():
        text = text.replace(literal, local)
    return text


def _run_bash(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    path = cwd / "harness.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run(
        [_bash(), "-e", "harness.sh"],
        cwd=cwd,
        env={**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*", **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=300,
    )


def run_post_step(tmp_path: Path, **knobs) -> SimpleNamespace:
    rig = Rig(tmp_path, **knobs)
    k = rig.knobs
    script = "\n".join([
        _localize(_prelude(), rig.paths()),
        STUB,
        "post_restore_reapply",
        "start_services",
    ])
    done = _run_bash(script, tmp_path, {
        "STUB_LOG": _posix(rig.log),
        "STUB_ROLE": k["role"],
        "STUB_LEDGER": k["ledger"],
        "STUB_AUTHDB": k["authdb"],
        "STUB_TABLES": k["tables"],
        "STUB_FAIL": k["fail"],
        "MIA_HTTPS_MARKER": _posix(tmp_path / "no-https"),
        "MIA_JOBS_PAUSED": _posix(tmp_path / "no-jobs-marker"),
    })
    log = rig.log.read_text() if rig.log.exists() else ""
    return SimpleNamespace(
        rc=done.returncode,
        out=done.stdout,
        err=done.stderr,
        log=log,
        lines=log.splitlines(),
        started="START_SERVICES" in log,
    )


def _index(lines: list[str], *needles: str) -> int:
    for number, line in enumerate(lines):
        if all(needle in line for needle in needles):
            return number
    raise AssertionError(f"no logged call with {needles}")


def test_all_five_steps_run_in_order_then_the_services_start(tmp_path):
    result = run_post_step(tmp_path)
    assert result.rc == 0, result.err
    lines = result.lines
    order = [
        _index(lines, "python3", "app_role_demote.py --reapply"),
        _index(lines, "os-ledger-migrate", "provision_ledger.py", "--mode reapply"),
        _index(lines, "--entrypoint python app -c"),
        _index(lines, " -1"),
        _index(lines, "read -r PGPASSWORD"),
        _index(lines, "START_SERVICES"),
    ]
    assert order == sorted(order) and len(set(order)) == len(order)
    assert "post-restore complete" in result.out


def test_no_secret_reaches_a_command_line_a_log_or_an_output(tmp_path):
    result = run_post_step(tmp_path)
    assert result.rc == 0, result.err
    assert PASSWORD not in result.log + result.out + result.err


@pytest.mark.parametrize("stage", FAILURES)
def test_a_failing_step_stops_the_restore_with_the_services_down(tmp_path, stage):
    result = run_post_step(tmp_path, fail=stage)
    assert result.rc == 1
    assert f"reason={FAILURES[stage]}" in result.err
    assert not result.started
    # nothing after the failed step ran
    later = {
        "demote": ["provision_ledger.py", "--entrypoint python app", "read -r PGPASSWORD"],
        "ledger": ["--entrypoint python app", " -1", "read -r PGPASSWORD"],
        "validator": [" -1", "read -r PGPASSWORD"],
        "revoke": ["read -r PGPASSWORD"],
        "probe": [],
    }[stage]
    for needle in later:
        assert needle not in result.log, needle
    assert PASSWORD not in result.log + result.out + result.err


def test_before_the_demote_exists_every_role_step_is_skipped_but_the_probe_still_runs(tmp_path):
    result = run_post_step(tmp_path, role="", ledger="f", authdb="", dsn_role="mia")
    assert result.rc == 0, result.err
    assert "python3" not in result.log and "os-ledger-migrate" not in result.log
    assert "--entrypoint python app" not in result.log and " -1" not in result.log
    for skipped in (
        "app_role_reapply skipped reason=app_role_not_demoted",
        "ledger_reapply skipped reason=ledger_schema_absent",
        "main_role_validation skipped reason=main_dsn_not_demoted",
        "owner_session_revoke skipped reason=owner_auth_database_absent",
    ):
        assert skipped in result.out, skipped
    assert "read -r PGPASSWORD" in result.log and result.started


def test_a_missing_owner_auth_table_set_is_skipped_but_a_partial_one_aborts(tmp_path):
    absent = run_post_step(tmp_path / "a", tables="0")
    assert absent.rc == 0 and "reason=owner_auth_tables_absent" in absent.out
    assert " -1" not in absent.log
    partial = run_post_step(tmp_path / "b", tables="2")
    assert partial.rc == 1 and "reason=owner_session_revoke_failed" in partial.err
    assert not partial.started


def test_the_helper_files_missing_after_the_demote_stops_the_restore(tmp_path):
    result = run_post_step(tmp_path, demote_files=False)
    assert result.rc == 1 and "reason=app_role_reapply_unavailable" in result.err
    assert "python3" not in result.log and not result.started


@pytest.mark.parametrize(
    "knobs",
    [
        {"ledger_script": False},
        {"migrate_env": False},
        {"tag": "..", "ledger_script": False},
    ],
)
def test_a_ledger_schema_without_a_usable_script_stops_the_restore(tmp_path, knobs):
    result = run_post_step(tmp_path, **knobs)
    assert result.rc == 1 and "reason=ledger_reapply_unavailable" in result.err
    assert "os-ledger-migrate" not in result.log and not result.started


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://mia_app:short@127.0.0.1:5432/mia",
        f"postgresql://mia_app:{PASSWORD}@10.0.0.9:5432/mia",
        f"postgresql://mia_app:{PASSWORD}@127.0.0.1:5432/other",
        f"postgresql://mia_app:{PASSWORD}@127.0.0.1:5432/mia?sslmode=disable",
        f"postgresql://mia_app:{PASSWORD}@127.0.0.1:5432/mia ",
    ],
)
def test_a_main_dsn_that_is_not_the_canonical_loopback_one_stops_the_restore(tmp_path, dsn):
    result = run_post_step(tmp_path, dsn=dsn)
    assert result.rc == 1 and "reason=main_dsn_invalid" in result.err
    assert not result.started and PASSWORD not in result.log + result.out + result.err


# --- V2-10b: nothing starts the services on a half-restored database ---------------------------
#
# A restore that stops after DROP DATABASE leaves the services down only until another command
# (`mia deploy`, `settings`, `https enable`, `go-live`) calls start_services. The marker closes
# that: written before DROP DATABASE, removed only when every post-restore step succeeded.

RESTORE_MARKER = "/etc/mia/restore-incomplete"

ARM_STUB = r"""
chown() { [ "$STUB_FAIL" != evidence ]; }
python3() { "$V210_PYTHON" "$@"; }
mark() { if [ -e "$MIA_RESTORE_INCOMPLETE" ]; then echo present; else echo absent; fi; }
log() { printf '%s\n' "$*" >> "$STUB_LOG"; }
compose() {
  case "$*" in
    *"information_schema.tables"*) echo 5 ;;
    *"DROP DATABASE"*) log "DROP marker=$(mark)" ;;
    *"pg_restore"*) log "PG_RESTORE marker=$(mark)"; cat > /dev/null ;;
    *"pg_dump"*) log "SAFETY_DUMP marker=$(mark)"; echo dump ;;
    *) : ;;
  esac
}
wait_healthy() { :; }
post_restore_reapply() {
  log "POST marker=$(mark)"
  if [ "$STUB_FAIL" = post ]; then
    echo "mia: restore stopped, the services stay down: reason=stub" >&2
    exit 1
  fi
}
start_services() { log "START marker=$(mark)"; }
"""


def run_restore_arm(tmp_path: Path, *, fail: str = "", dump: bool = True) -> SimpleNamespace:
    """The real restore arm of `mia`, with compose, the post-step and start_services stubbed."""
    paths = vm_paths(tmp_path)
    (tmp_path / "backups").mkdir()
    (tmp_path / "tag.env").write_text("MIA_TAG=testtag\n")
    dump_file = tmp_path / "mia.dump"
    if dump:
        dump_file.write_bytes(b"PGDMP")
    marker, log = tmp_path / "restore-incomplete", tmp_path / "arm.log"
    script = "\n".join([
        _localize(_prelude(), paths),
        ARM_STUB,
        f"set -- {_posix(dump_file)}",
        _localize(_arm("restore"), paths),
    ])
    done = _run_bash(script, tmp_path, {
        "STUB_LOG": _posix(log),
        "STUB_FAIL": fail,
        "V210_PYTHON": _posix(Path(sys.executable)),
        "MIA_RESTORE_INCOMPLETE": _posix(marker),
        "MIA_JOBS_PAUSED": _posix(tmp_path / "jobs-paused"),
        "MIA_HTTPS_MARKER": _posix(tmp_path / "no-https"),
    })
    return SimpleNamespace(
        rc=done.returncode,
        err=done.stderr,
        lines=log.read_text().splitlines() if log.exists() else [],
        marker=marker.exists(),
    )


def run_start_services(tmp_path: Path, *, marker: bool) -> SimpleNamespace:
    path, log = tmp_path / "restore-incomplete", tmp_path / "svc.log"
    if marker:
        path.write_text("")
    script = "\n".join([
        _prelude(),
        r"""compose() { printf 'compose %s\n' "$*" >> "$STUB_LOG"; }
wait_healthy() { printf 'wait %s\n' "$*" >> "$STUB_LOG"; }
start_services""",
    ])
    done = _run_bash(script, tmp_path, {
        "STUB_LOG": _posix(log),
        "MIA_RESTORE_INCOMPLETE": _posix(path),
        "MIA_HTTPS_MARKER": _posix(tmp_path / "no-https"),
        "MIA_JOBS_PAUSED": _posix(tmp_path / "no-jobs-marker"),
    })
    return SimpleNamespace(
        rc=done.returncode, err=done.stderr,
        lines=log.read_text().splitlines() if log.exists() else [],
    )


def test_the_restore_arm_writes_the_marker_before_it_drops_and_removes_it_after_the_repair():
    code = _code(_arm("restore"))
    attempt = code.index("record_restore_attempt")
    mark = code.index('install -m 600 /dev/null "$restore_incomplete"')
    drop = code.index("DROP DATABASE")
    restore = code.index("pg_restore -U mia -d mia")
    post = code.index("post_restore_reapply")
    clear = code.index('rm -f "$restore_incomplete"')
    start = code.index("start_services")
    assert attempt < mark < drop < restore < post < clear < start
    assert code.count("$restore_incomplete") == 2  # written once, removed once
    assert f'restore_incomplete="${{MIA_RESTORE_INCOMPLETE:-{RESTORE_MARKER}}}"' in SCRIPT


def test_start_services_refuses_while_the_marker_exists_before_it_touches_compose():
    body = _code(_function("start_services"))
    refusal = body.index('die "restore_incomplete: run mia restore again"')
    assert '[ ! -e "$restore_incomplete" ]' in body and refusal < body.index("compose ")
    # the four other commands that bring the services up all go through that one function
    for name in ("deploy", "settings", "https", "go-live", "restore"):
        assert "start_services" in _arm(name), name


def test_the_state_lock_refusals_name_the_retry_because_a_backup_dump_holds_the_lock_too():
    refusals = re.findall(r'flock -n 9 \|\| die "([^"]*)"', SCRIPT)
    assert len(refusals) == 2  # the shared case, and `os-import`
    retry = "another Mia operation (or a backup dump) is running; try again in a minute"
    assert set(refusals) == {retry}
    assert "another Mia operation is already running" not in SCRIPT
    # the per-job locks (fd 8) keep their own wording
    assert SCRIPT.count('flock -n 8 || { echo "mia: $name is already running"; exit 0; }') == 1


def test_a_restore_marks_the_database_incomplete_across_the_drop_and_clears_it_only_at_the_end(
    tmp_path,
):
    result = run_restore_arm(tmp_path)
    assert result.rc == 0, result.err
    # a failure before the drop (the safety dump) leaves the database intact, so no marker yet
    assert result.lines == [
        "SAFETY_DUMP marker=absent",
        "DROP marker=present",
        "PG_RESTORE marker=present",
        "POST marker=present",
        "START marker=absent",
    ]
    assert not result.marker


def test_a_post_restore_failure_leaves_the_marker_and_never_starts_the_services(tmp_path):
    result = run_restore_arm(tmp_path, fail="post")
    assert result.rc == 1 and "reason=stub" in result.err
    assert result.lines[-1] == "POST marker=present"
    assert not any(line.startswith("START") for line in result.lines)
    assert result.marker  # the next deploy, settings, https or go-live now refuses


def test_a_restore_refused_before_it_touches_anything_leaves_no_marker(tmp_path):
    result = run_restore_arm(tmp_path, dump=False)
    assert result.rc == 1 and "missing or empty dump" in result.err
    assert result.lines == [] and not result.marker


def test_attempt_write_failure_aborts_before_marker_and_drop(tmp_path):
    result = run_restore_arm(tmp_path, fail="evidence")
    assert result.rc == 1 and "reason=restore_evidence_invalid" in result.err
    assert result.lines == ["SAFETY_DUMP marker=absent"]
    assert not result.marker


def test_wrapper_writes_unique_dated_attempt_before_marker_and_drop(tmp_path):
    documents = []
    for index in range(2):
        directory = tmp_path / str(index)
        directory.mkdir()
        result = run_restore_arm(directory)
        assert result.rc == 0, result.err
        documents.append(json.loads((directory / "os-authority/restore-attempt.json").read_text()))
    for doc in documents:
        assert re.fullmatch(r"[a-f0-9]{32}", doc["attempt_id"])
        assert datetime.fromisoformat(doc["recorded_at"].replace("Z", "+00:00")).tzinfo
        assert doc["pre_restore_fence_record_present"] is False
    assert documents[0]["attempt_id"] != documents[1]["attempt_id"]


def test_start_services_refuses_with_the_reason_while_the_marker_exists(tmp_path):
    result = run_start_services(tmp_path, marker=True)
    assert result.rc == 1
    assert "mia: restore_incomplete: run mia restore again" in result.err
    assert result.lines == []  # compose was never reached


def test_start_services_starts_the_services_once_the_marker_is_gone(tmp_path):
    result = run_start_services(tmp_path, marker=False)
    assert result.rc == 0, result.err
    assert result.lines[0] == "compose up -d --remove-orphans postgres migrate app"
    assert "wait app" in result.lines


def test_the_probe_is_a_select_and_an_insert_that_is_rolled_back():
    body = _function("restore_access_probe")
    for fragment in ("SELECT 1 FROM public.", "INSERT INTO public.", "DEFAULT VALUES", "ROLLBACK;"):
        assert fragment in body, fragment
    # a refused row (class 23) proves the privilege check passed; a privilege error must not hide
    assert "integrity_constraint_violation" in body
    assert "insufficient_privilege" not in body


# --- the drill on a real PostgreSQL -------------------------------------------------------------

SHIM = r"""
chown() { :; }
export V210_ARGLOG="$STUB_LOG"
flat() { printf '%s' "$*" | tr '\n' ' ' >> "$V210_ARGLOG"; printf '\n' >> "$V210_ARGLOG"; }
python3() {
  if [ "$1" = - ]; then "$V210_PYTHON" "$@"
  else "$V210_PYTHON" "$V210_DRIVER" "$@"; fi
}
docker() {
  case "$1" in
    inspect) echo healthy; return 0 ;;
    compose) ;;
    *) command docker "$@"; return ;;
  esac
  shift
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -p|-f|--env-file|--profile) shift 2 ;;
      *) break ;;
    esac
  done
  local sub="$1"; shift
  flat "compose $sub $*"
  case "$sub" in
    exec) shift 2; command docker exec -i "$V210_PG_CONTAINER" "$@" ;;
    ps) echo "cid-${2:-x}" ;;
    run)
      local entry="" user=""
      export V210_ETC_MIA_READONLY=0
      while [ "$#" -gt 0 ]; do
        case "$1" in
          --rm|--no-deps|-T) shift ;;
          --user) user="$2"; shift 2 ;;
          --entrypoint) entry="$2"; shift 2 ;;
          -v)
            case "$2" in
              *:/app/ops/assaf-os/provision_ledger.py:ro)
                export V210_LEDGER_SCRIPT="${2%:/app/ops/assaf-os/provision_ledger.py:ro}" ;;
              /etc/mia:/etc/mia:ro) export V210_ETC_MIA_READONLY=1 ;;
            esac
            shift 2 ;;
          *) break ;;
        esac
      done
      local service="$1"; shift
      case "$service" in
        os-ledger-migrate) [ "$user" = root ] || return 97 ;;
      esac
      PATH="$V210_BIN:$PATH" "$entry" "$@" ;;
    *) : ;;
  esac
}
"""

# The deployed script, run by the driver that swaps only its pin (the port and the env file path).
PYTHON_WRAPPER = """#!/bin/sh
case "$1" in
  /app/ops/assaf-os/provision_ledger.py)
    shift; exec "$V210_PYTHON" "$V210_LEDGER_DRIVER" "$V210_LEDGER_SCRIPT" "$@" ;;
  *) exec "$V210_PYTHON" "$@" ;;
esac
"""
BUILD_SHA = "c" * 40


class Drill:
    def __init__(self, tmp: Path, url):
        self.tmp = tmp
        self.url = url
        self.port = url.port
        self.runtime_env = tmp / "runtime.env"  # the drill's /etc/mia/runtime.env
        self.write_log = tmp / "etc-mia-writes.log"  # a write into the read-only mount lands here
        self.ledger_line = ""  # the staged MIA_OWNER_OS_LEDGER_DATABASE_URL line, once provisioned

    # -- plumbing

    def psql(self, database: str, sql: str, *, user: str = "postgres", check: bool = True):
        done = subprocess.run(
            ["docker", "exec", "-i", CONTAINER, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
             "-U", user, "-d", database, "-f", "-"],
            input=sql, capture_output=True, text=True, encoding="utf-8", check=False, timeout=120,
        )
        if check and done.returncode:
            raise AssertionError(f"psql failed on {database}: {done.stderr}\n{sql[:400]}")
        return done

    def tool(self, name: str, *args: str, stdin: Path | None = None, stdout: Path | None = None):
        command = ["docker", "exec", "-i", CONTAINER, name, "-U", "mia", *args]
        with (stdin.open("rb") if stdin else open(os.devnull, "rb")) as source:
            if stdout:
                with stdout.open("wb") as sink:
                    done = subprocess.run(command, stdin=source, stdout=sink,
                                          stderr=subprocess.PIPE, check=False, timeout=180)
            else:
                done = subprocess.run(command, stdin=source, capture_output=True, check=False,
                                      timeout=180)
        assert done.returncode == 0, done.stderr.decode(errors="replace")
        return done.stdout.decode(errors="replace") if not stdout else ""

    def engine(self, user: str, database: str = "mia"):
        from sqlalchemy import create_engine

        return create_engine(
            self.url.set(username=user, database=database, drivername="postgresql+psycopg")
        )

    def scalar(self, database: str, sql: str, *, user: str = "postgres"):
        return self.psql(database, sql, user=user).stdout.strip()

    # -- the database under test

    def build(self) -> None:
        """A production-like `mia`: V2-14a demoted, V2-14's real ledger, V2-15's real provision."""
        self.reset()
        self.psql("postgres", "CREATE DATABASE mia OWNER mia; CREATE DATABASE assaf_os_auth "
                  "OWNER mia;")
        self.psql("mia", """
            CREATE TABLE public.drill_accounts (id serial PRIMARY KEY, name text NOT NULL);
            CREATE TABLE public.drill_notes (id bigserial PRIMARY KEY, body text);
            CREATE VIEW public.drill_account_names AS SELECT name FROM public.drill_accounts;
            INSERT INTO public.drill_accounts(name) VALUES ('kept-one'), ('kept-two');
            INSERT INTO public.drill_notes(body) VALUES ('note');
        """, user="mia")
        self.stage_main_role()
        migration = (ROOT / "migrations/20261011_owner_os_sessions.sql").read_text(encoding="utf-8")
        self.psql("assaf_os_auth", migration, user="mia")
        self.psql("assaf_os_auth", """
            INSERT INTO owner_os_sessions(session_hash, owner_id, workspace_id, login_proof_hash,
                scopes, expires_at, revoked_at, created_at)
            VALUES ('s1', '1001', 'assaf-os', 'p1', 'assaf-os:read', now() + interval '1 hour',
                    NULL, now() - interval '1 hour'),
                   ('s2', '1002', 'assaf-os', 'p2', 'assaf-os:read', now() + interval '1 hour',
                    NULL, now() - interval '2 hours');
            INSERT INTO owner_os_login_proofs(proof_hash, owner_id, consumed_at)
            VALUES ('p1', '1001', now() - interval '1 hour'),
                   ('p2', '1002', now() - interval '2 hours'),
                   ('p3', '1003', now() - interval '3 hours');
            INSERT INTO owner_os_revocations(owner_id, revoked_before)
            VALUES ('1001', now() - interval '30 days');
        """, user="mia")
        self.migrate_ledger()
        self.psql("mia", "INSERT INTO assaf_os.os_workspaces(workspace_id, owner_id, epoch) "
                  "VALUES ('assaf-os', '1001', 1);", user="mia")
        self.provision_ledger_roles()

    def stage_main_role(self, *, check: bool = True):
        """V2-14a's reviewed SQL, exactly as `stage` would run it (the demoted role mia_app)."""
        spec = importlib.util.spec_from_file_location("v210_demote", DEMOTE_PY)
        demote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demote)
        return self.psql("mia", demote.transaction(f"ALTER ROLE mia_app PASSWORD '{PASSWORD}';\n"),
                         user="mia", check=check)

    def migrate_ledger(self) -> None:
        """V2-14's real migration, as the superuser `mia`: assaf_os, its tables, functions, trigger
        and the owner role, with the demoted main role checked inside the transaction."""
        from app.db.os_ledger_migrate import apply_ledger_migrations

        engine = self.engine("mia")
        try:
            apply_ledger_migrations(engine, main_role="mia_app")
        finally:
            engine.dispose()

    def provision_ledger_roles(self) -> None:
        """V2-15's real provisioning: the runtime and retention roles, the exact grant map, and
        the runtime DSN staged in the drill's runtime.env (the VM-owned file), then validated."""
        self.write_runtime_env(f"MIA_DATABASE_URL=postgresql://mia_app:{PASSWORD}@127.0.0.1:5432/mia\n")
        self.ledger("provision", check=True)
        staged = [
            line for line in self.runtime_env.read_text(encoding="utf-8").splitlines()
            if line.startswith("MIA_OWNER_OS_LEDGER_DATABASE_URL=")
        ]
        assert len(staged) == 1, "provision must stage exactly one ledger DSN"
        self.ledger_line = staged[0] + "\n"

    def recreate_database(self) -> None:
        """The empty `mia` a new VM has: exactly the two statements the README gives as the way out
        of a restore that ran first, plus the app's tables the deploy's own `migrate` made."""
        self.psql("postgres", "DROP DATABASE IF EXISTS mia WITH (FORCE); "
                  "CREATE DATABASE mia OWNER mia;")
        self.psql("mia", "CREATE TABLE public.new_vm_accounts (id serial PRIMARY KEY, name text);",
                  user="mia")

    def documented_order(self) -> None:
        """The README's recovery order, before any restore: V2-14a's stage, V2-14's migration,
        V2-15's provision."""
        self.stage_main_role()
        self.migrate_ledger()
        self.provision_ledger_roles()

    def build_new_vm(self, *, prepared: bool) -> None:
        """A freshly bootstrapped VM: the superuser `mia` and an empty `mia` database. Roles are
        cluster-global, so the old VM's roles are gone too (`reset` drops them)."""
        self.reset()
        self.recreate_database()
        if prepared:
            self.documented_order()

    def write_runtime_env(self, text: str) -> None:
        self.runtime_env.write_text(text, encoding="utf-8", newline="\n")
        self.runtime_env.chmod(0o600)  # the script insists on a private file on POSIX

    def ledger(self, mode: str, *, check: bool = False, **extra: str):
        """The deployed provision_ledger.py through the pin-swapping driver, as the VM runs it."""
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "MIA_BUILD_SHA": BUILD_SHA,
            "V210_LEDGER_PORT": str(self.port),
            "V210_RUNTIME_ENV": str(self.runtime_env),
            "V210_WRITE_LOG": str(self.write_log),
            "MIA_OS_LEDGER_MIGRATION_DATABASE_URL":
                f"postgresql://mia@127.0.0.1:{self.port}/mia",
            "MIA_DATABASE_URL": f"postgresql://mia_app:{PASSWORD}@127.0.0.1:{self.port}/mia",
            **extra,
        }
        done = subprocess.run(
            [sys.executable, str(LEDGER_DRIVER), str(LEDGER_SCRIPT), "--target", "mia-vm",
             "--mode", mode, "--apply", "--approved-sha", BUILD_SHA],
            env=env, capture_output=True, text=True, encoding="utf-8", check=False, timeout=180,
        )
        if check:
            assert done.returncode == 0, done.stderr + done.stdout
        return done

    def reset(self) -> None:
        self.psql("postgres", """
            SELECT pg_terminate_backend(pid) FROM pg_stat_activity
             WHERE datname IN ('mia', 'assaf_os_auth') AND pid <> pg_backend_pid();
            DROP DATABASE IF EXISTS mia WITH (FORCE);
            DROP DATABASE IF EXISTS assaf_os_auth WITH (FORCE);
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mia') THEN
                CREATE ROLE mia SUPERUSER LOGIN;
              END IF;
            END $$;
            DROP ROLE IF EXISTS mia_app;
            DROP ROLE IF EXISTS assaf_os_ledger;
            DROP ROLE IF EXISTS assaf_os_retention;
            DROP ROLE IF EXISTS assaf_os_owner;
        """)
        self.ledger_line = ""
        self.write_log.unlink(missing_ok=True)

    def build_plain(self) -> None:
        """Production today: no demote, no ledger, no owner auth. A restore must still work."""
        self.reset()
        self.psql("postgres", "CREATE DATABASE mia OWNER mia;")
        self.psql("mia", """
            CREATE TABLE public.old_accounts (id serial PRIMARY KEY, name text NOT NULL);
            INSERT INTO public.old_accounts(name) VALUES ('old');
        """, user="mia")

    batch_proposal = "PRP_DRILL_BATCH"

    def add_private_batch_line(self) -> str:
        """A real pending statement batch and one private line of it, in V2-14b's real tables.

        Inserted as the migration identity `mia`. Returns the private text that must never reach
        a dump.
        """
        private = "PRIVATE-LINE-DETAIL"
        self.psql("mia", f"""
            INSERT INTO assaf_os.os_proposals(workspace_id, proposal_id, request_id, request_digest,
                envelope_digest, owner_id, epoch, approval_id, rejection_id, entity, record_id,
                expected_revision, mutations, dependency_digest, source, kind, status, created_at,
                expires_at)
            VALUES ('assaf-os', '{self.batch_proposal}', gen_random_uuid(), repeat('a', 64),
                repeat('b', 64), '1001', 1, gen_random_uuid(), gen_random_uuid(), 'statements',
                'STM_' || repeat('c', 32), 0, '[]'::jsonb, repeat('d', 64), 'ASSISTANT_PASTE',
                'statement_batch', 'pending', now(), now() + interval '1 day');
            INSERT INTO assaf_os.os_statement_batch_lines(workspace_id, proposal_id, line_key,
                occurrence_index, detail)
            VALUES ('assaf-os', '{self.batch_proposal}', repeat('e', 64), 0,
                '{{"merchant": "{private}"}}');
        """, user="mia")
        return private

    def add_unreviewed_ledger_table(self) -> None:
        """An object V2-15's exact validator does not know: no reapply makes this posture clean."""
        self.psql("mia", "CREATE TABLE assaf_os.os_unreviewed (id int); "
                  "ALTER TABLE assaf_os.os_unreviewed OWNER TO assaf_os_owner;", user="mia")

    def dump(self, name: str) -> Path:
        target = self.tmp / name
        self.tool("pg_dump", "-d", "mia", "--format=custom", EXCLUDE, stdout=target)
        return target

    # -- the real restore arm

    def restore(self, dump: Path, *, skip_post_step: bool = False, dsn_role: str = "mia_app",
                runtime_extra: str = "", stage_ledger: bool = True):
        t = self.tmp
        (t / "demote").mkdir(exist_ok=True)
        (t / "bin").mkdir(exist_ok=True)
        wrapper = t / "bin/python"
        wrapper.write_text(PYTHON_WRAPPER, newline="\n")
        wrapper.chmod(0o755)
        src = t / "src/testtag/ops/assaf-os"
        src.mkdir(parents=True, exist_ok=True)
        shutil.copy(LEDGER_SCRIPT, src / "provision_ledger.py")  # byte for byte the deployed file
        (t / "tag.env").write_text("MIA_TAG=testtag\n")
        self.write_runtime_env(
            f"MIA_DATABASE_URL=postgresql://{dsn_role}:{PASSWORD}@127.0.0.1:5432/mia\n"
            + (self.ledger_line if stage_ledger else "")
            + runtime_extra
        )
        (t / "migrate.env").write_text(
            "MIA_OS_LEDGER_MIGRATION_DATABASE_URL=postgresql://mia:x@127.0.0.1:5432/mia\n"
        )
        (t / "backups").mkdir(exist_ok=True)
        # the helper files exactly as `install` places them: py + sql/ side by side
        installed = t / "demote"
        shutil.copy(DEMOTE_PY, installed / "app_role_demote.py")
        (installed / "sql").mkdir(exist_ok=True)
        shutil.copy(ROOT / "ops/gcp/vm/sql/app-role-demote.sql",
                    installed / "sql/app-role-demote.sql")
        text = _localize(_prelude() + "\n", vm_paths(t))
        arm = _localize(_arm("restore"), vm_paths(t))
        if skip_post_step:
            arm = arm.replace("post_restore_reapply", ":")
        script = "\n".join([
            "set -euo pipefail", SHIM, text, f"set -- {_posix(dump)}", arm, "echo HARNESS_FINISHED",
        ])
        log = t / "calls.log"
        log.write_text("")
        done = _run_bash(script, t, {
            "STUB_LOG": _posix(log),
            "V210_PG_CONTAINER": CONTAINER,
            "V210_PYTHON": _posix(Path(sys.executable)),
            "V210_DRIVER": _posix(DRIVER),
            "V210_LEDGER_DRIVER": _posix(LEDGER_DRIVER),
            "V210_LEDGER_PORT": str(self.port),
            "V210_RUNTIME_ENV": _posix(self.runtime_env),
            "V210_WRITE_LOG": _posix(self.write_log),
            "V210_BIN": _msys(t / "bin"),
            "MIA_BUILD_SHA": BUILD_SHA,
            "PYTHONPATH": str(ROOT),
            "MIA_OS_LEDGER_MIGRATION_DATABASE_URL":
                f"postgresql://mia@127.0.0.1:{self.port}/mia",
            "MIA_DATABASE_URL": f"postgresql://mia_app:{PASSWORD}@127.0.0.1:{self.port}/mia",
            "MIA_HTTPS_MARKER": _posix(t / "no-https"),
            "MIA_JOBS_PAUSED": _posix(t / "no-jobs-marker"),
            # V2-10b: the restore arm writes this before DROP DATABASE, never in the real /etc/mia
            "MIA_RESTORE_INCOMPLETE": _posix(t / "restore-incomplete"),
        })
        calls = log.read_text().splitlines()
        return SimpleNamespace(
            rc=done.returncode, out=done.stdout, err=done.stderr, calls=calls,
            finished="HARNESS_FINISHED" in done.stdout,
            started=any("up -d --remove-orphans postgres migrate app" in c for c in calls),
            env_after=self.runtime_env.read_text(encoding="utf-8"),
            jobs_paused=(t / "no-jobs-marker").exists(),
            incomplete=(t / "restore-incomplete").exists(),
        )


@pytest.fixture
def drill(tmp_path):
    raw = os.getenv(URL_VARIABLE, "")
    if not raw:
        pytest.skip(f"{URL_VARIABLE} unset (opt-in: a disposable server on 127.0.0.1:56218)")
    from sqlalchemy.engine import make_url

    url = make_url(raw)
    assert url.host == "127.0.0.1" and url.port == 56218
    assert url.database == "restore_drill" and url.username == "postgres"
    rig = Drill(tmp_path, url)
    rig.build()
    try:
        yield rig
    finally:
        rig.reset()


def test_drill_without_the_post_step_a_restore_locks_mia_app_out(drill):
    """The control: pg_restore --no-owner --no-privileges alone leaves a full outage."""
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    dump = drill.dump("source.dump")
    result = drill.restore(dump, skip_post_step=True)
    assert result.rc == 0 and result.finished, result.err
    assert drill.scalar("mia", "SELECT pg_get_userbyid(relowner) FROM pg_class "
                        "WHERE oid='public.drill_accounts'::regclass") == "mia"
    with drill.engine("mia_app").connect() as conn, pytest.raises(ProgrammingError) as denied:
        conn.execute(text("SELECT count(*) FROM public.drill_accounts"))
    assert "permission denied" in str(denied.value)


def _ledger_engine(rig: Drill):
    return rig.engine("assaf_os_ledger")


def _assert_both_real_validators(rig: Drill) -> None:
    """V2-14's posture validator and V2-15's exact validator, on the restored database."""
    from app.db.os_ledger_migrate import _validate_main_role_catalog
    from app.services.os_ledger.role_policy import validate_ledger_catalog

    with rig.engine("postgres").begin() as conn:
        _validate_main_role_catalog(conn, "mia_app")
        validate_ledger_catalog(conn, main_role="mia_app", migration_role="mia")


def test_drill_a_restore_gives_both_postures_back_and_only_then_starts_the_services(drill):
    from app.db.os_ledger_migrate import _validate_main_role_catalog
    from app.services.os_ledger.role_policy import validate_ledger_catalog, validate_ledger_role
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.orm import Session

    password = "SELECT rolpassword FROM pg_authid WHERE rolname = 'assaf_os_ledger'"
    password_before = drill.scalar("postgres", password)
    assert password_before.startswith("SCRAM-SHA-256$")
    before = datetime.now(UTC) - timedelta(seconds=2)
    dump = drill.dump("source.dump")
    result = drill.restore(dump)
    assert result.rc == 0 and result.finished, result.err + result.out
    assert not result.incomplete  # removed only after every step passed, right before the start

    # the order on the wire: restore, helper, ledger, validator, revoke, probe, then start
    def first(*needles: str) -> int:
        return next(i for i, c in enumerate(result.calls) if all(n in c for n in needles))

    steps = [
        first("pg_restore", "--no-owner", "--no-privileges"),
        first("rolname='mia_app'"),
        first("run", "os-ledger-migrate", "provision_ledger.py"),
        first("run", "--entrypoint python app -c"),
        first("exec", "assaf_os_auth", " -1"),
        first("read -r PGPASSWORD"),
        first("up -d --remove-orphans postgres migrate app"),
    ]
    assert steps == sorted(steps), steps
    # nothing was skipped: this database has both postures, the demote and the owner auth
    for note in ("app_role_reapply ok", "ledger_reapply ok", "main_role_validation ok",
                 "owner_session_revoke ok", "access_probe ok role=mia_app", "complete"):
        assert f"mia: post-restore {note}" in result.out, result.out
    assert "skipped" not in result.out
    assert PASSWORD not in "\n".join(result.calls) + result.out + result.err
    assert result.jobs_paused  # the restore paused the jobs before it dropped anything

    # V2-14's real posture validator AND V2-15's real exact validator pass on the restored database
    with drill.engine("postgres").begin() as conn:
        _validate_main_role_catalog(conn, "mia_app")
        validate_ledger_catalog(conn, main_role="mia_app", migration_role="mia")
        assert conn.scalar(text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='assaf_os'"
        )) == "assaf_os_owner"
    # and the runtime preflight the ledger itself runs: as assaf_os_ledger, with the main role
    ledger, main = _ledger_engine(drill), drill.engine("mia_app")
    try:
        with Session(ledger) as db:
            validate_ledger_role(db, main, migration_role="mia")
            # the ledger data came back, the runtime role reads it and may not delete it
            assert db.scalar(text("SELECT count(*) FROM assaf_os.os_workspaces")) == 1
            with pytest.raises(ProgrammingError) as denied:
                db.execute(text("DELETE FROM assaf_os.os_workspaces"))
            assert "permission denied" in str(denied.value)
    finally:
        ledger.dispose()
        main.dispose()

    # the existing runtime role kept its password, and /etc/mia was neither written nor changed
    assert drill.scalar("postgres", password) == password_before
    assert result.env_after == (
        f"MIA_DATABASE_URL=postgresql://mia_app:{PASSWORD}@127.0.0.1:5432/mia\n"
        + drill.ledger_line
    )
    assert any("/etc/mia:/etc/mia:ro" in call for call in result.calls)
    assert not drill.write_log.exists()

    # mia_app can really SELECT and INSERT on its tables, and the data came back
    app = drill.engine("mia_app")
    try:
        with app.begin() as conn:
            assert conn.scalar(text("SELECT count(*) FROM public.drill_accounts")) == 2
            conn.execute(text("INSERT INTO public.drill_accounts(name) VALUES ('after-restore')"))
            conn.execute(text("INSERT INTO public.drill_notes(body) VALUES ('after-restore')"))
            assert conn.scalar(text("SELECT count(*) FROM public.drill_account_names")) == 3
    finally:
        app.dispose()

    # every owner session issued before the restore is revoked, in the owner-auth database
    rows = drill.scalar("assaf_os_auth", "SELECT owner_id || '|' || (revoked_before >= "
                        f"'{before.isoformat()}')::text FROM owner_os_revocations ORDER BY 1")
    assert rows.splitlines() == ["1001|true", "1002|true", "1003|true"]
    assert drill.scalar("assaf_os_auth", "SELECT count(*) FROM owner_os_sessions "
                        "WHERE revoked_at IS NULL") == "0"


def test_drill_restoring_the_same_dump_twice_is_idempotent(drill):
    """Every step is re-runnable: the second restore takes a second safety dump and passes again."""
    dump = drill.dump("source.dump")
    first = drill.restore(dump)
    assert first.rc == 0 and first.finished, first.err + first.out
    second = drill.restore(dump)
    assert second.rc == 0 and second.finished, second.err + second.out
    assert "mia: post-restore complete" in second.out and "skipped" not in second.out
    assert len(list((drill.tmp / "backups").glob("pre-restore-*.dump"))) >= 1
    assert drill.scalar("mia", "SELECT count(*) FROM public.drill_accounts") == "2"


def test_drill_the_dumps_keep_the_data_but_not_the_batch_line_detail(drill):
    """V2-14b's real `os_statement_batch_lines` (merged in #168) is in the schema this drill builds.

    A pending statement batch holds private line detail until its purge. Neither the safety dump
    that `mia restore` takes of the live database nor the off-site dump may carry that detail, while
    the rest of the data (the parent proposal, the public tables) stays in both. The restore of the
    off-site dump runs the whole post-step, so it must also pass V2-14's and V2-15's real
    validators on the real table.
    """
    private = drill.add_private_batch_line()
    data = "TABLE DATA assaf_os os_statement_batch_lines"
    shape = "TABLE assaf_os os_statement_batch_lines"

    # The control: without the exclusion the detail is in the dump, so its absence below means
    # something. A custom-format dump is compressed, so the readable proof is the restore script.
    unfiltered = drill.tmp / "unfiltered.dump"
    drill.tool("pg_dump", "-d", "mia", "--format=custom", stdout=unfiltered)
    assert data in drill.tool("pg_restore", "--list", stdin=unfiltered)
    assert private in drill.tool("pg_restore", "-f", "-", stdin=unfiltered)

    # the off-site dump of the live database (the flag backup_lib passes), and the safety dump
    offsite = drill.dump("offsite.dump")
    result = drill.restore(offsite)  # the post-step is ON: no skip_post_step
    assert result.rc == 0 and result.finished, result.err + result.out
    assert "mia: post-restore complete" in result.out and "skipped" not in result.out
    [safety] = list((drill.tmp / "backups").glob("pre-restore-*.dump"))
    for dump in (offsite, safety):
        listing = drill.tool("pg_restore", "--list", stdin=dump)
        assert shape in listing  # the shape survives
        assert data not in listing  # the detail does not
        assert "TABLE DATA assaf_os os_proposals" in listing  # the rest of the ledger does
        assert "TABLE DATA public drill_accounts" in listing
        script = drill.tool("pg_restore", "-f", "-", stdin=dump)
        assert private not in script  # not even as text in the restore script
        assert drill.batch_proposal in script

    # the restore came back with the shape, the parent row and no private detail, and it is valid
    count = "SELECT count(*) FROM assaf_os.{table}"
    assert drill.scalar("mia", count.format(table="os_statement_batch_lines")) == "0"
    assert drill.scalar("mia", count.format(table="os_proposals")
                        + f" WHERE proposal_id = '{drill.batch_proposal}'") == "1"
    _assert_both_real_validators(drill)
    owner = ("SELECT pg_get_userbyid(relowner) FROM pg_class "
             "WHERE oid = 'assaf_os.os_statement_batch_lines'::regclass")
    assert drill.scalar("mia", owner) == "assaf_os_owner"


def test_drill_a_posture_the_real_validator_rejects_stops_the_restore_and_is_retryable(drill):
    clean = drill.dump("clean.dump")
    drill.add_unreviewed_ledger_table()
    dirty = drill.dump("unreviewed.dump")
    result = drill.restore(dirty)
    assert result.rc == 1 and not result.finished
    assert "reason=ledger_reapply_failed" in result.err
    assert "ledger_provision_refused" in result.err  # the script's one sanitized code
    assert not result.started
    assert result.jobs_paused  # and they stay paused: nothing may touch a half-repaired database
    # V2-10b: and the database stays marked, so no other command starts the services on it
    assert result.incomplete
    # the helper ran and stopped before the later steps
    assert not any("read -r PGPASSWORD" in c for c in result.calls)
    assert drill.scalar("assaf_os_auth", "SELECT count(*) FROM owner_os_sessions "
                        "WHERE revoked_at IS NULL") == "2"
    # the real script is atomic: its re-own and its grants rolled back with the refusal
    assert drill.scalar("mia", "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                        "WHERE nspname = 'assaf_os'") == "mia"
    assert PASSWORD not in "\n".join(result.calls) + result.out + result.err

    # fix the cause (restore a dump without the unreviewed table) and run it again
    retry = drill.restore(clean)
    assert retry.rc == 0 and retry.finished, retry.err + retry.out
    assert "mia: post-restore complete" in retry.out
    assert not retry.incomplete  # the restore that succeeds is what clears it


def test_drill_the_real_probe_catches_a_table_the_validator_never_looks_at(drill):
    """The V2-14a drill's lesson: the posture check does not look at `public` access."""
    # drill_accounts sorts first, so it is the probed table. Forced row security with no policy
    # refuses every INSERT; the sabotage rides inside the dump, so it survives the restore.
    drill.psql("mia", "ALTER TABLE public.drill_accounts ENABLE ROW LEVEL SECURITY; "
               "ALTER TABLE public.drill_accounts FORCE ROW LEVEL SECURITY;", user="mia")
    dump = drill.dump("sabotaged.dump")
    result = drill.restore(dump)
    assert result.rc == 1 and not result.finished
    assert "reason=app_role_access_probe_failed" in result.err
    assert not result.started
    # every earlier step succeeded: only the real access probe objected
    from app.db.os_ledger_migrate import _validate_main_role_catalog
    from app.services.os_ledger.role_policy import validate_ledger_catalog

    with drill.engine("postgres").begin() as conn:
        _validate_main_role_catalog(conn, "mia_app")
        validate_ledger_catalog(conn, main_role="mia_app", migration_role="mia")


def test_drill_a_database_from_before_the_demote_and_the_ledger_still_restores(drill):
    """Production today has neither posture: every role step skips and the services still start."""
    drill.build_plain()
    result = drill.restore(drill.dump("old.dump"), dsn_role="mia", stage_ledger=False)
    assert result.rc == 0 and result.finished, result.err + result.out
    for skipped in (
        "app_role_reapply skipped reason=app_role_not_demoted",
        "ledger_reapply skipped reason=ledger_schema_absent",
        "main_role_validation skipped reason=main_dsn_not_demoted",
        "owner_session_revoke skipped reason=owner_auth_database_absent",
    ):
        assert f"mia: post-restore {skipped}" in result.out, result.out
    # the probe still ran, as the DSN's own role
    assert "mia: post-restore access_probe ok role=mia" in result.out
    assert result.started
    assert drill.scalar("mia", "SELECT count(*) FROM public.old_accounts") == "1"


def test_drill_the_reapply_with_no_assaf_os_is_the_scripts_own_noop(drill):
    """V2-15's `--mode reapply` on a database without the schema changes nothing and exits 0."""
    drill.build_plain()
    done = drill.ledger("reapply")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "-- assaf_os is absent: reapply is a no-op"


def test_drill_a_missing_ledger_runtime_role_is_refused_before_it_is_created_when_nothing_is_staged(
    drill,
):
    """E10: reapply never writes a VM-owned file, so it cannot invent the runtime DSN either."""
    dump = drill.dump("source.dump")
    # roles are cluster-global: a fresh cluster has the dump's schema but not the login role
    drill.psql("mia", "DROP OWNED BY assaf_os_ledger; DROP ROLE assaf_os_ledger;", user="mia")
    role = "SELECT count(*) FROM pg_roles WHERE rolname = 'assaf_os_ledger'"
    assert drill.scalar("postgres", role) == "0"

    result = drill.restore(dump, stage_ledger=False)
    assert result.rc == 1 and not result.finished
    assert "reason=ledger_reapply_failed" in result.err
    assert not result.started
    assert any("/etc/mia:/etc/mia:ro" in call for call in result.calls)
    # refused before CREATE ROLE: nothing was created, nothing was written, nothing came after
    assert drill.scalar("postgres", role) == "0"
    assert "MIA_OWNER_OS_LEDGER_DATABASE_URL" not in result.env_after
    assert not drill.write_log.exists()
    assert not any("read -r PGPASSWORD" in call for call in result.calls)


def test_drill_a_missing_ledger_runtime_role_is_recreated_from_the_staged_dsn_without_a_write(
    drill,
):
    """The other half of E10: with the DSN already staged, reapply creates the role from it."""
    dump = drill.dump("source.dump")
    drill.psql("mia", "DROP OWNED BY assaf_os_ledger; DROP ROLE assaf_os_ledger;", user="mia")
    role = "SELECT count(*) FROM pg_roles WHERE rolname = 'assaf_os_ledger'"
    assert drill.scalar("postgres", role) == "0"

    result = drill.restore(dump)  # the staged line from the original provision is in runtime.env
    assert result.rc == 0 and result.finished, result.err + result.out
    assert drill.scalar("postgres", role) == "1"
    assert drill.scalar("postgres", "SELECT rolpassword LIKE 'SCRAM-SHA-256$%' FROM pg_authid "
                        "WHERE rolname = 'assaf_os_ledger'") == "t"
    assert drill.ledger_line in result.env_after  # the staged DSN is untouched
    assert any("/etc/mia:/etc/mia:ro" in call for call in result.calls)
    assert not drill.write_log.exists()  # and the read-only mount was never even tried


def test_drill_a_new_vm_that_restores_first_stops_and_the_stage_then_refuses(drill):
    """The fresh-cluster finding, reproduced: restoring before the roles exist has no way forward.

    A new VM has no mia_app and no ledger roles. A dump that holds assaf_os stops at step 2, and
    V2-14a's stage, run afterwards on the restored database, refuses (`pg_restore --no-privileges`
    left PUBLIC EXECUTE on the assaf_os functions). The README therefore puts the roles first.
    """
    dump = drill.dump("old-vm.dump")
    drill.build_new_vm(prepared=False)
    result = drill.restore(dump, dsn_role="mia", stage_ledger=False)
    assert result.rc == 1 and not result.finished and not result.started
    assert "reason=ledger_reapply_failed" in result.err
    assert "ledger_provision_refused" in result.err
    assert result.jobs_paused
    assert not drill.write_log.exists()  # the read-only mount was never written through

    refused = drill.stage_main_role(check=False)  # V2-14a on the restored database
    assert refused.returncode != 0 and "app_role_ledger_access_unsafe" in refused.stderr
    roles = "SELECT count(*) FROM pg_roles WHERE rolname = 'mia_app'"
    assert drill.scalar("postgres", roles) == "0"


def test_drill_a_new_vm_that_restored_first_recovers_by_recreating_the_database_empty(drill):
    """The way out the README gives for a restore that ran before the roles existed.

    The failed restore created nothing cluster-wide (the ledger script refused before CREATE ROLE),
    so the new VM is intact; only its database holds the half-repaired restore. Recreate it empty,
    run the documented order, restore again.
    """
    dump = drill.dump("old-vm.dump")
    drill.build_new_vm(prepared=False)
    first = drill.restore(dump, dsn_role="mia", stage_ledger=False)
    assert first.rc == 1 and "reason=ledger_reapply_failed" in first.err

    drill.recreate_database()
    drill.documented_order()
    second = drill.restore(dump)
    assert second.rc == 0 and second.finished and second.started, second.err + second.out
    assert "mia: post-restore complete" in second.out
    _assert_both_real_validators(drill)
    assert drill.scalar("mia", "SELECT count(*) FROM public.drill_accounts") == "2"
    assert len(list((drill.tmp / "backups").glob("pre-restore-*.dump"))) == 2  # each kept its own


def test_drill_a_new_vm_restores_when_the_roles_are_made_first_in_the_documented_order(drill):
    """The README's recovery order, proven: stage, migrate and provision first, then restore."""
    dump = drill.dump("old-vm.dump")
    drill.build_new_vm(prepared=True)
    result = drill.restore(dump)
    assert result.rc == 0 and result.finished and result.started, result.err + result.out
    for note in ("app_role_reapply ok", "ledger_reapply ok", "main_role_validation ok",
                 "access_probe ok role=mia_app", "complete"):
        assert f"mia: post-restore {note}" in result.out, result.out
    # a new VM has no owner-auth database: that one step is the only one skipped
    assert "owner_session_revoke skipped reason=owner_auth_database_absent" in result.out
    assert result.out.count("skipped") == 1
    assert PASSWORD not in "\n".join(result.calls) + result.out + result.err
    assert not drill.write_log.exists()  # the restore never wrote /etc/mia
    assert any("/etc/mia:/etc/mia:ro" in call for call in result.calls)

    _assert_both_real_validators(drill)
    # the old VM's data is back, readable by the runtime roles of THIS VM, and not deletable
    assert drill.scalar("mia", "SELECT count(*) FROM public.drill_accounts") == "2"
    assert drill.scalar("mia", "SELECT count(*) FROM assaf_os.os_workspaces") == "1"
    from app.services.os_ledger.role_policy import validate_ledger_role
    from sqlalchemy.orm import Session

    ledger, main = _ledger_engine(drill), drill.engine("mia_app")
    try:
        with Session(ledger) as db:
            validate_ledger_role(db, main, migration_role="mia")
    finally:
        ledger.dispose()
        main.dispose()


def test_the_read_only_guard_really_blocks_a_write_into_the_env_files_directory(tmp_path):
    """Without this the `no write attempted` assertions above could pass on a guard that is off."""
    code = (
        "import importlib.util, pathlib, sys, tempfile\n"
        f"spec = importlib.util.spec_from_file_location('d', {str(LEDGER_DRIVER)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"target = pathlib.Path({str(tmp_path)!r})\n"
        "module.read_only(target)\n"
        "try:\n"
        "    tempfile.mkstemp(prefix='.ledger-', dir=target)\n"
        "except OSError as error:\n"
        "    print(error.errno)\n"
        "else:\n"
        "    print('written')\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env={**os.environ, "V210_WRITE_LOG": str(tmp_path / "log.txt")},
        capture_output=True, text=True, check=False, timeout=60,
    )
    import errno

    assert done.stdout.strip() == str(errno.EROFS), done.stderr
    assert "write attempted" in (tmp_path / "log.txt").read_text(encoding="utf-8")
    assert list(tmp_path.glob(".ledger-*")) == []
