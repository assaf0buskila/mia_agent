"""V2-10: `mia restore` re-applies the roles before the services start (V2_PG_CONTRACTS.md 18.1).

`mia restore` runs `pg_restore --no-owner --no-privileges` as mia. Every object is then owned by
mia and every grant is gone, so mia_app could not read its own tables and the assaf_os ownership
and grants would be lost. The restore arm therefore runs a post-step between pg_restore and
start_services, under the state lock it already holds:

  1. app_role_demote.py --reapply        (V2-14a; a direct call, the wrapper's flock is busy)
  2. provision_ledger.py --mode reapply  (V2-15 r2; in the deployed image, only if assaf_os exists)
  3. V2-14's real _validate_main_role_catalog on the real main DSN (only when it is mia_app)
  4. owner_os_revocations.revoked_before = restore time, in the owner-auth database
  5. a REAL SELECT and INSERT (rolled back) as the main DSN role

Any failure prints one fixed reason code and stops with the services DOWN.

Three layers: static facts about the script, the post-step functions run under bash with a stubbed
compose (always run), and the drill on a real PostgreSQL that runs the real restore arm, the real
app_role_demote.py and a clearly marked stand-in for V2-15 r2's reapply (skipped only when
MIA_RESTORE_DRILL_TEST_POSTGRES_URL is unset).
"""

from __future__ import annotations

import importlib.util
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
STANDIN = ROOT / "tests/ops/standin_provision_ledger.py"
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
    }
    runbook = (ROOT / "ops/gcp/backup/README.md").read_text(encoding="utf-8")
    for code in emitted:
        assert f"`{code}`" in runbook, code


def test_the_offsite_backup_dump_leaves_the_batch_line_detail_out_too():
    assert backup_lib.DUMP_EXCLUDE_TABLE_DATA == "assaf_os.os_statement_batch_lines"
    assert f"--exclude-table-data={backup_lib.DUMP_EXCLUDE_TABLE_DATA}" == EXCLUDE


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


def test_the_probe_is_a_select_and_an_insert_that_is_rolled_back():
    body = _function("restore_access_probe")
    for fragment in ("SELECT 1 FROM public.", "INSERT INTO public.", "DEFAULT VALUES", "ROLLBACK;"):
        assert fragment in body, fragment
    # a refused row (class 23) proves the privilege check passed; a privilege error must not hide
    assert "integrity_constraint_violation" in body
    assert "insufficient_privilege" not in body


# --- the drill on a real PostgreSQL -------------------------------------------------------------

SHIM = r"""
export V210_ARGLOG="$STUB_LOG"
flat() { printf '%s' "$*" | tr '\n' ' ' >> "$V210_ARGLOG"; printf '\n' >> "$V210_ARGLOG"; }
python3() { "$V210_PYTHON" "$V210_DRIVER" "$@"; }
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

PYTHON_WRAPPER = """#!/bin/sh
case "$1" in
  /app/ops/assaf-os/provision_ledger.py) shift; exec "$V210_PYTHON" "$V210_LEDGER_SCRIPT" "$@" ;;
  *) exec "$V210_PYTHON" "$@" ;;
esac
"""


class Drill:
    def __init__(self, tmp: Path, url):
        self.tmp = tmp
        self.url = url
        self.port = url.port

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
        """A production-like `mia`: V2-14a demote applied AND a stand-in assaf_os ledger."""
        self.reset()
        self.psql("postgres", "CREATE DATABASE mia OWNER mia; CREATE DATABASE assaf_os_auth "
                  "OWNER mia;")
        spec = importlib.util.spec_from_file_location("v210_demote", DEMOTE_PY)
        demote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demote)
        self.psql("mia", """
            CREATE TABLE public.drill_accounts (id serial PRIMARY KEY, name text NOT NULL);
            CREATE TABLE public.drill_notes (id bigserial PRIMARY KEY, body text);
            CREATE VIEW public.drill_account_names AS SELECT name FROM public.drill_accounts;
            CREATE FUNCTION public.drill_count() RETURNS bigint LANGUAGE sql
              AS 'SELECT count(*) FROM public.drill_accounts';
            INSERT INTO public.drill_accounts(name) VALUES ('kept-one'), ('kept-two');
            INSERT INTO public.drill_notes(body) VALUES ('note');
            CREATE ROLE assaf_os_owner NOLOGIN;
            CREATE ROLE assaf_os_ledger LOGIN;
            CREATE SCHEMA assaf_os AUTHORIZATION assaf_os_owner;
            CREATE TABLE assaf_os.os_workspaces (id serial PRIMARY KEY, name text);
            CREATE TABLE assaf_os.os_statement_batch_lines (id serial PRIMARY KEY, detail text);
            CREATE FUNCTION assaf_os.os_version() RETURNS int LANGUAGE sql AS 'SELECT 1';
            CREATE FUNCTION assaf_os.os_internal() RETURNS int LANGUAGE sql AS 'SELECT 2';
            ALTER TABLE assaf_os.os_workspaces OWNER TO assaf_os_owner;
            ALTER TABLE assaf_os.os_statement_batch_lines OWNER TO assaf_os_owner;
            ALTER FUNCTION assaf_os.os_version() OWNER TO assaf_os_owner;
            ALTER FUNCTION assaf_os.os_internal() OWNER TO assaf_os_owner;
            REVOKE ALL ON SCHEMA assaf_os FROM PUBLIC;
            REVOKE ALL ON ALL TABLES IN SCHEMA assaf_os FROM PUBLIC;
            REVOKE ALL ON ALL ROUTINES IN SCHEMA assaf_os FROM PUBLIC;
            INSERT INTO assaf_os.os_workspaces(name) VALUES ('assaf-os');
            INSERT INTO assaf_os.os_statement_batch_lines(detail) VALUES ('PRIVATE-LINE-DETAIL');
        """, user="mia")
        # V2-14a's reviewed SQL, exactly as `stage` would run it
        self.psql("mia", demote.transaction(f"ALTER ROLE mia_app PASSWORD '{PASSWORD}';\n"),
                  user="mia")
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
        # the ledger posture, by the same stand-in the restore will call
        self.stand_in(check=True)

    def stand_in(self, *, check: bool = False, **extra: str):
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "MIA_BUILD_SHA": "c" * 40,
            "V210_STANDIN_PORT": str(self.port),
            "MIA_OS_LEDGER_MIGRATION_DATABASE_URL":
                f"postgresql://mia@127.0.0.1:{self.port}/mia",
            "MIA_DATABASE_URL": f"postgresql://mia_app@127.0.0.1:{self.port}/mia",
            **extra,
        }
        done = subprocess.run(
            [sys.executable, str(STANDIN), "--target", "mia-vm", "--mode", "reapply", "--apply",
             "--approved-sha", "c" * 40],
            env=env, capture_output=True, text=True, check=False, timeout=120,
        )
        if check:
            assert done.returncode == 0, done.stderr
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

    def build_plain(self) -> None:
        """Production today: no demote, no ledger, no owner auth. A restore must still work."""
        self.reset()
        self.psql("postgres", "CREATE DATABASE mia OWNER mia;")
        self.psql("mia", """
            CREATE TABLE public.old_accounts (id serial PRIMARY KEY, name text NOT NULL);
            INSERT INTO public.old_accounts(name) VALUES ('old');
        """, user="mia")

    def dump(self, name: str) -> Path:
        target = self.tmp / name
        self.tool("pg_dump", "-d", "mia", "--format=custom", EXCLUDE, stdout=target)
        return target

    # -- the real restore arm

    def restore(self, dump: Path, *, fail: str = "", skip_post_step: bool = False,
                dsn_role: str = "mia_app", runtime_extra: str = "", rw_mount: bool = False):
        t = self.tmp
        (t / "demote").mkdir(exist_ok=True)
        (t / "bin").mkdir(exist_ok=True)
        wrapper = t / "bin/python"
        wrapper.write_text(PYTHON_WRAPPER, newline="\n")
        wrapper.chmod(0o755)
        src = t / "src/testtag/ops/assaf-os"
        src.mkdir(parents=True, exist_ok=True)
        shutil.copy(STANDIN, src / "provision_ledger.py")
        (t / "tag.env").write_text("MIA_TAG=testtag\n")
        (t / "runtime.env").write_text(
            f"MIA_DATABASE_URL=postgresql://{dsn_role}:{PASSWORD}@127.0.0.1:5432/mia\n"
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
        if rw_mount:  # only to show that the read-only flag is what stops the restore
            assert "-v /etc/mia:/etc/mia:ro" in text
            text = text.replace("-v /etc/mia:/etc/mia:ro", "-v /etc/mia:/etc/mia")
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
            "V210_BIN": _msys(t / "bin"),
            "V210_STANDIN_PORT": str(self.port),
            "V210_STANDIN_FAIL": "1" if fail == "ledger" else "",
            "MIA_BUILD_SHA": "c" * 40,
            "PYTHONPATH": str(ROOT),
            "MIA_OS_LEDGER_MIGRATION_DATABASE_URL":
                f"postgresql://mia@127.0.0.1:{self.port}/mia",
            "MIA_DATABASE_URL": f"postgresql://mia_app:{PASSWORD}@127.0.0.1:{self.port}/mia",
            "MIA_HTTPS_MARKER": _posix(t / "no-https"),
            "MIA_JOBS_PAUSED": _posix(t / "no-jobs-marker"),
        })
        calls = log.read_text().splitlines()
        return SimpleNamespace(
            rc=done.returncode, out=done.stdout, err=done.stderr, calls=calls,
            finished="HARNESS_FINISHED" in done.stdout,
            started=any("up -d --remove-orphans postgres migrate app" in c for c in calls),
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


def test_drill_a_restore_gives_both_postures_back_and_only_then_starts_the_services(drill):
    from app.db.os_ledger_migrate import _validate_main_role_catalog
    from sqlalchemy import text

    before = datetime.now(UTC) - timedelta(seconds=2)
    dump = drill.dump("source.dump")
    result = drill.restore(dump)
    assert result.rc == 0 and result.finished, result.err + result.out

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

    # V2-14's real posture validator and the ledger validation both pass on the restored database
    with drill.engine("postgres").begin() as conn:
        _validate_main_role_catalog(conn, "mia_app")
        spec = importlib.util.spec_from_file_location("standin", STANDIN)
        standin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(standin)
        standin.validate(conn, main_role="mia_app")
        assert conn.scalar(text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='assaf_os'"
        )) == "assaf_os_owner"

    # mia_app can really SELECT and INSERT on its tables, and the data came back
    app = drill.engine("mia_app")
    try:
        with app.begin() as conn:
            assert conn.scalar(text("SELECT count(*) FROM public.drill_accounts")) == 2
            conn.execute(text("INSERT INTO public.drill_accounts(name) VALUES ('after-restore')"))
            conn.execute(text("INSERT INTO public.drill_notes(body) VALUES ('after-restore')"))
            assert conn.scalar(text("SELECT public.drill_count()")) == 3
    finally:
        app.dispose()

    # every owner session issued before the restore is revoked, in the owner-auth database
    rows = drill.scalar("assaf_os_auth", "SELECT owner_id || '|' || (revoked_before >= "
                        f"'{before.isoformat()}')::text FROM owner_os_revocations ORDER BY 1")
    assert rows.splitlines() == ["1001|true", "1002|true", "1003|true"]
    assert drill.scalar("assaf_os_auth", "SELECT count(*) FROM owner_os_sessions "
                        "WHERE revoked_at IS NULL") == "0"


def test_drill_the_safety_dump_keeps_the_data_but_not_the_batch_line_detail(drill):
    dump = drill.dump("source.dump")
    result = drill.restore(dump)
    assert result.rc == 0 and result.finished, result.err
    [safety] = list((drill.tmp / "backups").glob("pre-restore-*.dump"))
    listing = drill.tool("pg_restore", "--list", stdin=safety)
    assert "TABLE assaf_os os_statement_batch_lines" in listing  # the shape survives
    assert "TABLE DATA assaf_os os_statement_batch_lines" not in listing  # the detail does not
    assert "TABLE DATA public drill_accounts" in listing
    assert "PRIVATE-LINE-DETAIL" not in safety.read_bytes().decode("latin-1")
    # the offsite dump of the same database obeys the same rule
    source = drill.tool("pg_restore", "--list", stdin=dump)
    assert "TABLE DATA assaf_os os_statement_batch_lines" not in source


def test_drill_a_failing_ledger_reapply_leaves_the_services_down(drill):
    dump = drill.dump("source.dump")
    result = drill.restore(dump, fail="ledger")
    assert result.rc == 1 and not result.finished
    assert "reason=ledger_reapply_failed" in result.err
    assert not result.started
    # the helper ran and stopped before the later steps
    assert not any("read -r PGPASSWORD" in c for c in result.calls)
    assert drill.scalar("assaf_os_auth", "SELECT count(*) FROM owner_os_sessions "
                        "WHERE revoked_at IS NULL") == "2"


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

    with drill.engine("postgres").begin() as conn:
        _validate_main_role_catalog(conn, "mia_app")


def test_drill_a_database_from_before_the_demote_and_the_ledger_still_restores(drill):
    """Production today has neither posture: every role step skips and the services still start."""
    drill.build_plain()
    result = drill.restore(drill.dump("old.dump"), dsn_role="mia")
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


def test_drill_a_missing_ledger_runtime_role_stops_the_restore_because_etc_mia_is_read_only(drill):
    """E10: a restore never writes a VM-owned file, so it cannot create the runtime role."""
    dump = drill.dump("source.dump")
    # roles are cluster-global: a fresh cluster has the dump's schema but not the login role
    drill.psql("mia", "DROP OWNED BY assaf_os_ledger; DROP ROLE assaf_os_ledger;", user="mia")
    role = "SELECT count(*) FROM pg_roles WHERE rolname = 'assaf_os_ledger'"
    assert drill.scalar("postgres", role) == "0"

    result = drill.restore(dump)
    assert result.rc == 1 and not result.finished
    assert "reason=ledger_reapply_failed" in result.err
    assert not result.started
    assert any("/etc/mia:/etc/mia:ro" in call for call in result.calls)
    # the refusal rolled the role creation back; nothing after the ledger step ran
    assert drill.scalar("postgres", role) == "0"
    assert not any("read -r PGPASSWORD" in call for call in result.calls)

    # the read-only flag is what stopped it: the same restore with a writable /etc/mia creates it
    retry = drill.restore(dump, rw_mount=True)
    assert retry.rc == 0 and retry.finished, retry.err + retry.out
    assert drill.scalar("postgres", role) == "1"
