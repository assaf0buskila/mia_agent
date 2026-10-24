"""V2-31: the privileged Assaf OS jobs on the VM (V2_PG_CONTRACTS.md section 3.1 and 16, E8).

``mia os-authority``, ``mia os-provision`` and ``mia os-job`` are case arms of the VM
control script; their services are profile-off entries of the compose file. The arms run here in
bash with Compose, ``install``, ``chown`` and ``flock`` stubbed and the host paths redirected, so
nothing touches docker, ``/etc/mia`` or a lock.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.unit.test_ci_os_ledger_postgres import _bash, _run_step

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "ops/gcp/vm/mia").read_text(encoding="utf-8")
COMPOSE_TEXT = (ROOT / "ops/gcp/vm/compose.yaml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)["services"]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
SHA = "a" * 40
JOBS = ("os-authority", "os-import", "os-verifier", "os-retention")
KEY_MIGRATION = "MIA_OS_LEDGER_MIGRATION_DATABASE_URL"
KEY_RETENTION = "MIA_OS_LEDGER_RETENTION_DATABASE_URL"


def arm(name: str) -> str:
    return SCRIPT.split(f"\n  {name})\n", 1)[1].split("\n    ;;", 1)[0]


def function(name: str) -> str:
    body = SCRIPT.split(f"\n{name}() {{\n", 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{\n{body}\n}}\n"


# ------------------------------------------------------------------------------- the compose


def test_the_four_jobs_are_profile_off_and_never_an_app_dependency():
    for job in JOBS:
        service = COMPOSE[job]
        assert service["profiles"] == [job], job
        assert service["restart"] == "no" and service["network_mode"] == "host"
        assert service["image"] == "mia:${MIA_TAG:?MIA_TAG is required}"
        assert "ports" not in service and "depends_on" not in service
    for name, service in COMPOSE.items():
        depends = service.get("depends_on", {})
        assert not set(JOBS) & set(depends), name
    assert list(COMPOSE)[-1] == "app", "the V2-14 pin slices the file after `app:`"
    assert "os-ledger" not in COMPOSE_TEXT.split("\n  app:", 1)[1]


def test_each_job_reads_only_its_own_credentials():
    files = {name: COMPOSE[name].get("env_file", []) for name in COMPOSE}
    assert files["os-authority"] == [
        "/etc/mia/settings.env", "/etc/mia/os-ledger-migrate.env", "/etc/mia/runtime.env"]
    assert files["os-import"] == ["/etc/mia/settings.env", "/etc/mia/runtime.env"]
    assert files["os-verifier"] == ["/etc/mia/settings.env", "/etc/mia/runtime.env"]
    assert files["os-retention"] == [
        "/etc/mia/settings.env", "/etc/mia/runtime.env", "/etc/mia/os-retention.env"]
    # The privileged DSN is in exactly two services; the retention DSN in exactly one.
    assert [n for n, f in files.items() if "/etc/mia/os-ledger-migrate.env" in f] == [
        "os-ledger-migrate", "os-authority"]
    assert [n for n, f in files.items() if "/etc/mia/os-retention.env" in f] == ["os-retention"]
    # The runtime file comes last where it matters, so it wins over a job-only file.
    assert files["os-authority"].index("/etc/mia/runtime.env") > files["os-authority"].index(
        "/etc/mia/os-ledger-migrate.env")
    for name in ("app", "migrate"):
        assert not any("os-" in path for path in files[name]), name


def test_the_entrypoints_are_the_console_scripts_pyproject_declares():
    scripts = PYPROJECT["project"]["scripts"]
    for job in ("os-authority", "os-verifier", "os-retention"):
        assert COMPOSE[job]["entrypoint"] == [job]
        module, _, attribute = scripts[job].partition(":")
        assert (module, attribute) == (f"app.workers.{job.replace('-', '_')}", "main")
    # The wheel ships `app` only, so the importer runs from the deployed tag's scripts/.
    assert "os-import" not in scripts
    assert PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["app"]
    assert COMPOSE["os-import"]["entrypoint"] == ["python", "/app/scripts/os_import.py"]
    assert any(v.endswith(":/app/scripts:ro") for v in COMPOSE["os-import"]["volumes"])


def test_every_declared_job_module_that_exists_exposes_main():
    # os_verifier (V2-18) and os_retention (V2-26) merge before V2-31; until they do the entry is
    # declared and its module is simply absent, which is checked once it is there.
    for job in ("os-authority", "os-verifier", "os-retention"):
        path = ROOT / "app/workers" / f"{job.replace('-', '_')}.py"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            assert re.search(r"^def main\(", text, re.M), path.name
    assert (ROOT / "app/workers/os_authority.py").exists()


def test_the_authority_evidence_mount_is_the_directory_the_cli_writes():
    from app.workers import os_authority as cli

    volumes = COMPOSE["os-authority"]["volumes"]
    assert volumes == [f"{cli.DEFAULT_EVIDENCE_DIR}:{cli.DEFAULT_EVIDENCE_DIR}"]
    assert cli.DEFAULT_EVIDENCE_DIR == "/var/lib/mia/os-authority"
    assert "install -d -m 700 -o 10001 -g 10001 /var/lib/mia/os-authority" in arm("os-authority")


def test_the_import_mounts_are_read_only():
    volumes = COMPOSE["os-import"]["volumes"]
    assert len(volumes) == 2 and all(v.endswith(":ro") for v in volumes)


def test_the_job_names_are_the_script_allow_list():
    match = re.search(r"^os_jobs=\(([^)]*)\)", SCRIPT, re.M)
    assert match and tuple(match.group(1).split()) == JOBS
    assert "jobs=(mia-due-scan mia-ingest-knowledge)" in SCRIPT, "no OS job is a timer job"
    assert "os-" not in re.search(r"^jobs=\(([^)]*)\)", SCRIPT, re.M).group(1)
    assert "install_timers" in SCRIPT and "os-" not in function("install_timers").replace(
        "os-ledger", "")


def test_the_retention_variable_is_owned_by_the_vm_and_dropped_from_mia_env():
    # E8: "already covered by the MIA_OS_LEDGER_\w+ owned-regex from V2-10".
    match = re.search(r'owned = re\.compile\(r"([^"]+)"\)', SCRIPT)
    assert match
    owned = re.compile(match.group(1))
    for name in (KEY_RETENTION, KEY_MIGRATION, "MIA_OWNER_OS_LEDGER_DATABASE_URL",
                 "MIA_DATABASE_URL"):
        assert owned.match(f"{name}=x"), name
    for name in ("MIA_ASSAF_OS_FREEZE", "MIA_ASSAF_OS_BACKEND", "MIA_OS_STATEMENT_PEPPER",
                 "MIA_ASSAF_OS_HMAC_SECRET"):
        assert not owned.match(f"{name}=x"), name


# ---------------------------------------------------------------------------- the shell pieces


def test_the_script_is_valid_bash():
    done = subprocess.run(
        [_bash(), "-n", str(ROOT / "ops/gcp/vm/mia")], capture_output=True, text=True,
        encoding="utf-8", check=False, timeout=60,
    )
    assert done.returncode == 0, done.stderr


def run_single_key(tmp_path, content, key=KEY_RETENTION, *, link=False):
    path = tmp_path / "job.env"
    if content is not None:
        path.write_text(content, encoding="utf-8", newline="\n")
    target = tmp_path / "link.env"
    if link:
        target.symlink_to(path)
    prelude = function("single_key_env") + "\n"
    check = f'single_key_env "{(target if link else path).as_posix()}" {key}\n'
    proc = _run_step(prelude + check, tmp_path)
    return proc.returncode == 0


@pytest.mark.parametrize("content,ok", [
    (f"{KEY_RETENTION}=postgresql://u:p@127.0.0.1:5432/mia\n", True),
    (f"# note\n\n{KEY_RETENTION}='postgresql://u:p@127.0.0.1/mia'\n", True),
    (f'{KEY_RETENTION}="postgresql://u:p@127.0.0.1/mia"\n', True),
    (f"{KEY_RETENTION}=x", True),
    ("", False),
    ("# only a comment\n", False),
    (f"{KEY_RETENTION}=x\nMIA_DATABASE_URL=secret\n", False),
    (f"{KEY_RETENTION}=x\n{KEY_RETENTION}=y\n", False),
    (f"export {KEY_RETENTION}=x\n", False),
    (f"{KEY_RETENTION}='x\nMIA_DATABASE_URL=secret'\n", False),
    (f"{KEY_RETENTION}=x MIA_BUILD_SHA={'b' * 40}\n", False),
    (f"{KEY_RETENTION}X=x\n", False),
    (f" {KEY_RETENTION}=x\n", False),
    ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL=x\n", False),
    (None, False),
])
def test_a_job_env_file_must_hold_exactly_its_one_key(tmp_path, content, ok):
    assert run_single_key(tmp_path, content) is ok


@pytest.mark.skipif(os.name != "posix", reason="symbolic links need POSIX")
def test_a_symlinked_job_env_file_is_refused(tmp_path):
    assert run_single_key(tmp_path, f"{KEY_RETENTION}=x\n", link=True) is False


def stubs(tmp_path) -> str:
    return (
        'set -euo pipefail\n'
        'die() { echo "mia: $*" >&2; exit 1; }\n'
        'compose() { echo "compose $*"; }\n'
        'install() { echo "install $*"; }\n'
        'chown() { echo "chown $*"; }\n'
        'flock() { return 0; }\n'
        f'jobs_paused="{(tmp_path / "jobs-paused").as_posix()}"\n'
        'os_jobs=(os-authority os-import os-verifier os-retention)\n'
        + function("single_key_env") + "\n"
    )


def redirected(text: str, tmp_path: Path) -> str:
    base = tmp_path.as_posix()
    return (text.replace("/etc/mia/", f"{base}/etc-mia/")
            .replace("/opt/mia/tag.env", f"{base}/tag.env")
            .replace("/opt/mia/src/", f"{base}/src/")
            .replace("/var/lock/", f"{base}/lock-")
            .replace("-v /etc/mia:", f"-v {base}/etc-mia:"))


def host(tmp_path, *, migration="ok", retention=None, tag="v1"):
    (tmp_path / "etc-mia").mkdir(exist_ok=True)
    (tmp_path / "tag.env").write_text(f"MIA_TAG={tag}\n", encoding="utf-8", newline="\n")
    if migration == "ok":
        (tmp_path / "etc-mia/os-ledger-migrate.env").write_text(
            f"{KEY_MIGRATION}=postgresql://privileged:secret@127.0.0.1/mia\n", encoding="utf-8",
            newline="\n")
    elif migration is not None:
        (tmp_path / "etc-mia/os-ledger-migrate.env").write_text(
            migration, encoding="utf-8", newline="\n")
    if retention is not None:
        (tmp_path / "etc-mia/os-retention.env").write_text(
            retention, encoding="utf-8", newline="\n")
    scripts = tmp_path / "src" / tag / "ops" / "assaf-os"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in ("provision_retention.py", "provision_ledger.py"):
        (scripts / name).write_text("# stub\n", encoding="utf-8", newline="\n")


def run_arm(tmp_path, name, argv, **host_options):
    host(tmp_path, **host_options)
    quoted = " ".join(f"'{a}'" for a in argv)
    program = stubs(tmp_path) + f"set -- {quoted}\n" + redirected(arm(name), tmp_path) + "\n"
    proc = _run_step(program, tmp_path)
    return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------- mia os-authority


@pytest.mark.parametrize("argv,tail", [
    (["init", "--approved-sha", SHA], "os-authority init --approved-sha " + SHA),
    (["status", "--approved-sha", SHA], "os-authority status --approved-sha " + SHA),
    (["activate", "--approved-sha", SHA], "os-authority activate --approved-sha " + SHA),
    (["fence", "--approved-sha", SHA], "os-authority fence --approved-sha " + SHA),
    (["restore-begin", "--expected-epoch", "1", "--approved-sha", SHA],
     "os-authority restore-begin --expected-epoch 1 --approved-sha " + SHA),
    (["restore-finish", "--expected-epoch", "1", "--approved-sha", SHA],
     "os-authority restore-finish --expected-epoch 1 --approved-sha " + SHA),
    (["rollback", "--approved-sha", SHA], "os-authority rollback --approved-sha " + SHA),
    (["init", "--rearm", "--approved-sha", SHA],
     "os-authority init --rearm --approved-sha " + SHA),
    (["preflight", "--approved-sha", SHA], "os-authority preflight --approved-sha " + SHA),
    (["preflight", "--drain", "--final-accept", "--approved-sha", SHA],
     "os-authority preflight --drain --final-accept --approved-sha " + SHA),
    (["freeze-plan", "--phase", "switch", "--approved-sha", SHA],
     "os-authority freeze-plan --phase switch --approved-sha " + SHA),
    (["freeze-plan", "--phase", "drain", "--approved-sha", SHA],
     "os-authority freeze-plan --phase drain --approved-sha " + SHA),
    (["freeze-plan", "--phase", "mirror", "--approved-sha", SHA],
     "os-authority freeze-plan --phase mirror --approved-sha " + SHA),
    (["freeze-plan", "--phase", "open", "--approved-sha", SHA],
     "os-authority freeze-plan --phase open --approved-sha " + SHA),
    (["freeze-plan", "--phase", "revert", "--approved-sha", SHA],
     "os-authority freeze-plan --phase revert --approved-sha " + SHA),
])
def test_os_authority_passes_exactly_the_command_flags_and_sha_to_the_job(tmp_path, argv, tail):
    code, output = run_arm(tmp_path, "os-authority", argv)
    assert code == 0, output
    assert f"compose --profile os-authority run --rm --no-deps -T {tail}" in output
    assert "install -d -m 700 -o 10001 -g 10001" in output
    assert "secret" not in output


@pytest.mark.parametrize("argv", [
    [],
    ["bogus", "--approved-sha", SHA],
    ["init"],
    ["init", "--approved-sha"],
    ["init", "--approved-sha", "a" * 39],
    ["init", "--approved-sha", "A" * 40],
    ["init", "--approved-sha", "g" * 40],
    ["init", "--approved-sha", SHA + "0"],
    ["init", "--approved-sha", SHA, "--unknown"],
    ["init", "--approved-sha", SHA, "--phase", "bogus"],
    ["init", "--approved-sha", SHA, "--phase"],
    ["init", "--approved-sha", SHA, "extra"],
    ["init", "--approved-sha", SHA, "--approved-sha", "short"],
    ["--approved-sha", SHA, "init"],
])
def test_os_authority_refuses_before_compose_runs(tmp_path, argv):
    code, output = run_arm(tmp_path, "os-authority", argv)
    assert code == 1 and "compose" not in output, output


@pytest.mark.parametrize("migration", [
    None,
    "",
    f"{KEY_MIGRATION}=x\nMIA_DATABASE_URL=secret\n",
    f"{KEY_MIGRATION}=x\nMIA_BUILD_SHA={'b' * 40}\n",
    f"{KEY_MIGRATION}='x\nMIA_DATABASE_URL=secret'\n",
])
def test_os_authority_refuses_a_privileged_file_with_another_key(tmp_path, migration):
    code, output = run_arm(
        tmp_path, "os-authority", ["init", "--approved-sha", SHA], migration=migration)
    assert code == 1 and "compose" not in output and "secret" not in output, output


def test_os_authority_refuses_an_undeployed_host(tmp_path):
    host(tmp_path)
    (tmp_path / "tag.env").unlink()
    program = (stubs(tmp_path) + f"set -- init --approved-sha {SHA}\n"
               + redirected(arm("os-authority"), tmp_path))
    proc = _run_step(program, tmp_path)
    assert proc.returncode == 1 and "not deployed yet" in proc.stderr


def test_os_authority_takes_the_state_lock_like_the_other_owner_gated_commands():
    lock_arm = SCRIPT.split("\ncase \"$cmd\" in\n", 1)[1].split("\nesac\n", 1)[0]
    for name in ("deploy", "restore", "os-ledger", "os-authority", "os-provision"):
        assert re.search(rf"[|\s]{name}[|)]", lock_arm), name


@pytest.mark.parametrize("marker_present", [False, True])
def test_restore_finish_checks_the_host_marker_and_overrides_inherited_assertion(
    tmp_path, marker_present,
):
    host(tmp_path)
    marker = tmp_path / "restore-incomplete"
    if marker_present:
        marker.touch()
    program = (
        stubs(tmp_path)
        + 'compose() { echo "complete=$MIA_OS_RESTORE_COMPLETE compose $*"; }\n'
        + 'MIA_OS_RESTORE_COMPLETE=true\n'
        + f'MIA_RESTORE_INCOMPLETE="{marker.as_posix()}"\n'
        + f'set -- restore-finish --expected-epoch 1 --approved-sha {SHA}\n'
        + redirected(arm("os-authority"), tmp_path)
    )
    result = _run_step(program, tmp_path)
    if marker_present:
        assert result.returncode == 1 and "restore_incomplete" in result.stderr
        assert "compose" not in result.stdout
    else:
        assert result.returncode == 0 and "complete=true compose" in result.stdout
    assert COMPOSE["os-authority"]["environment"]["MIA_OS_RESTORE_COMPLETE"] == (
        "${MIA_OS_RESTORE_COMPLETE:-false}")


def test_restore_begin_never_forwards_an_inherited_completion_assertion(tmp_path):
    host(tmp_path)
    program = (
        stubs(tmp_path)
        + 'compose() { echo "complete=$MIA_OS_RESTORE_COMPLETE compose $*"; }\n'
        + 'MIA_OS_RESTORE_COMPLETE=true\n'
        + f'set -- restore-begin --expected-epoch 1 --approved-sha {SHA}\n'
        + redirected(arm("os-authority"), tmp_path)
    )
    result = _run_step(program, tmp_path)
    assert result.returncode == 0 and "complete=false compose" in result.stdout


# --------------------------------------------------------------------------- mia os-provision


@pytest.mark.parametrize("which,script,flags", [
    ("retention", "provision_retention.py", "--target mia-vm --apply"),
    ("runtime", "provision_ledger.py", "--target mia-vm --mode provision --apply"),
])
def test_os_provision_runs_the_deployed_script_with_the_operators_sha(
    tmp_path, which, script, flags
):
    code, output = run_arm(tmp_path, "os-provision", [which, "--approved-sha", SHA])
    assert code == 0, output
    assert "compose --profile os-ledger run --rm --no-deps -T --user root" in output
    assert "/src/v1/ops/assaf-os:/app/ops/assaf-os:ro" in output
    assert "/etc-mia:/etc/mia" in output.replace("-v " + tmp_path.as_posix(), "")
    assert "--entrypoint sh os-ledger-migrate" in output
    assert f"/app/ops/assaf-os/{script} {flags} --approved-sha {SHA}" in output
    assert "secret" not in output


@pytest.mark.parametrize("argv", [
    [],
    ["retention"],
    ["retention", "--approved-sha"],
    ["bogus", "--approved-sha", SHA],
    ["both", "--approved-sha", SHA],
    ["--approved-sha", SHA, "retention"],
    ["retention", "--approved-sha", "short"],
    ["runtime", "--approved-sha", "A" * 40],
    ["retention", "--approved-sha", SHA, "--apply"],
    ["retention", "--sha", SHA],
])
def test_os_provision_refuses_before_compose_runs(tmp_path, argv):
    code, output = run_arm(tmp_path, "os-provision", argv)
    assert code == 1 and "compose" not in output, output


@pytest.mark.parametrize("which", ["retention", "runtime"])
def test_os_provision_refuses_a_bad_tag_and_a_missing_or_linked_script(tmp_path, which):
    for tag in ("../x", "-x", "a b"):
        code, output = run_arm(tmp_path, "os-provision", [which, "--approved-sha", SHA], tag=tag)
        assert code == 1 and "compose" not in output, tag
    host(tmp_path, tag="v2")
    (tmp_path / "src/v2/ops/assaf-os/provision_retention.py").unlink()
    program = (stubs(tmp_path) + f"set -- {which} --approved-sha {SHA}\n"
               + redirected(arm("os-provision"), tmp_path))
    proc = _run_step(program, tmp_path)
    assert proc.returncode == 1 and "provision_retention.py missing" in proc.stderr


def test_os_provision_mounts_etc_mia_writable_nowhere_else():
    # The restore reapply mounts /etc/mia read-only; only this arm lets the script write the file.
    assert SCRIPT.count("-v /etc/mia:/etc/mia ") == 1
    assert "-v /etc/mia:/etc/mia:ro" in SCRIPT


# ------------------------------------------------------------------------------- mia os-job


def test_os_job_runs_each_allowed_job_under_its_own_profile(tmp_path):
    for name, extra in (("os-import", []), ("os-verifier", []), ("os-retention", ["--dry-run"])):
        code, output = run_arm(
            tmp_path, "os-job", [name, *extra],
            retention=f"{KEY_RETENTION}=postgresql://u:p@127.0.0.1:5432/mia\n")
        assert code == 0, (name, output)
        expected = f"compose --profile {name} run --rm --no-deps -T {name}"
        assert any(line.strip() == " ".join([expected, *extra]) for line in output.splitlines()), (
            name, output)


@pytest.mark.parametrize("name", ["os-authority", "os-ledger-migrate", "mia-due-scan", "app",
                                  "OS-IMPORT", "os-import;id", "", "os-"])
def test_os_job_refuses_anything_outside_the_allow_list(tmp_path, name):
    code, output = run_arm(tmp_path, "os-job", [name])
    assert code == 1 and "compose" not in output, (name, output)


def test_os_job_needs_a_name(tmp_path):
    host(tmp_path)
    program = stubs(tmp_path) + "set --\n" + redirected(arm("os-job"), tmp_path)
    proc = _run_step(program, tmp_path)
    assert proc.returncode != 0 and "compose" not in proc.stdout


@pytest.mark.parametrize("retention", [
    None, "", "MIA_X=1\n", f"{KEY_RETENTION}=x\nMIA_DATABASE_URL=secret\n",
])
def test_os_retention_needs_its_one_key_file(tmp_path, retention):
    code, output = run_arm(tmp_path, "os-job", ["os-retention"], retention=retention)
    assert code == 1 and "compose" not in output and "secret" not in output, output


def test_scheduled_os_jobs_obey_the_pause_but_the_import_does_not(tmp_path):
    retention = f"{KEY_RETENTION}=postgresql://u:p@127.0.0.1:5432/mia\n"
    (tmp_path / "jobs-paused").write_text("", encoding="utf-8")
    for name in ("os-verifier", "os-retention"):
        code, output = run_arm(tmp_path, "os-job", [name], retention=retention)
        assert code == 0 and "jobs are paused; skipping" in output and "compose" not in output
    code, output = run_arm(tmp_path, "os-job", ["os-import"])
    assert code == 0 and "compose --profile os-import run" in output
    assert "install -d -m 750 -o root -g 10001 /var/lib/mia/os-import" in output


def test_os_import_takes_the_state_lock_and_the_others_a_job_lock():
    text = arm("os-job")
    import_part = text.split("      os-import)\n", 1)[1]
    assert "exec 9>" in import_part and "mia-state.lock" in import_part
    scheduled = text.split("      os-verifier|os-retention)\n", 1)[1]
    scheduled = scheduled.split("      os-import)", 1)[0]
    assert 'mia-job-$os_job.lock' in scheduled and "flock -n 8" in scheduled


def test_the_phases_the_wrapper_accepts_are_the_phases_the_cli_has():
    from app.workers import os_authority as cli

    match = re.search(r'\[\[ "\$2" =~ \^\(([a-z|]+)\)\$ \]\]', arm("os-authority"))
    assert match and tuple(match.group(1).split("|")) == cli.PHASES
