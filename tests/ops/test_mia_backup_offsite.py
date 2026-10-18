"""V2-10 VM side: a consistent dump, encrypted before it leaves the disk, uploaded and verified.

Everything external is a fake (docker, gcloud). openssl is real, so the encryption is genuine.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import backup_lib
import backup_offsite
import pytest

from tests.ops import fakes
from tests.ops.fakes import BUCKET, KEY, PLAINTEXT, TOC, FakeRunner, FakeVm

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 15, 12, 5, 3, tzinfo=UTC)
SHIPPED_ITERATIONS = backup_lib.PBKDF2_ITERATIONS  # read before any test lowers it
DUMP = "15min/mia-20261015T120503Z.dump.enc"
MANIFEST = "15min/mia-20261015T120503Z.manifest.json"


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    fakes.fast_kdf(monkeypatch)


@pytest.fixture
def env(tmp_path):
    return {
        "MIA_BACKUP_BUCKET": BUCKET,
        "MIA_BACKUP_WORK_DIR": str(tmp_path / "work"),
        # never the machine's real lock: a developer may have a `mia` running
        "MIA_STATE_LOCK": str(tmp_path / "mia-state.lock"),
    }


def run_main(env, vm, now=NOW, argv=()):
    runner = FakeRunner(vm.handler)
    code = backup_offsite.main(list(argv), runner=runner, environ=env, now=now)
    return code, runner


def work_dir(env) -> Path:
    return Path(env["MIA_BACKUP_WORK_DIR"])


def state(env) -> dict:
    return json.loads((work_dir(env) / "state.json").read_text(encoding="utf-8"))


def leftovers(env) -> list[str]:
    runs = work_dir(env) / "runs"
    return sorted(p.name for p in runs.iterdir()) if runs.exists() else []


def decrypt(tmp_path: Path, ciphertext: bytes, key: str, iterations: int) -> bytes:
    source, key_file, target = tmp_path / "in.enc", tmp_path / "key", tmp_path / "out.bin"
    source.write_bytes(ciphertext)
    key_file.write_text(key + "\n", encoding="utf-8")
    argv = backup_lib.decrypt_argv("openssl", key_file, source, target, iterations)
    assert backup_lib.Runner().run(argv).returncode == 0
    return target.read_bytes()


def test_dump_is_encrypted_verified_uploaded_and_recoverable(env, tmp_path, capsys):
    vm = FakeVm()
    code, runner = run_main(env, vm)
    assert code == 0
    assert vm.names("15min/") == [DUMP, MANIFEST]

    ciphertext = vm.objects[DUMP]["data"]
    assert PLAINTEXT[:40] not in ciphertext and ciphertext != PLAINTEXT
    manifest = backup_lib.load_manifest(vm.objects[MANIFEST]["data"])
    assert manifest["ciphertext_sha256"] == fakes.sha256_bytes(ciphertext)
    assert manifest["ciphertext_bytes"] == len(ciphertext)
    assert manifest["plaintext_sha256"] == fakes.sha256_bytes(PLAINTEXT)
    assert manifest["key_id"] == backup_lib.key_id(KEY)
    assert manifest["database"] == "mia" and manifest["toc_tables"] == 3
    assert (
        manifest["server_version"] == "16.4"
        and manifest["created_at_utc"] == "2026-10-15T12:05:03Z"
    )
    # sha256 is recorded on the object itself at upload, not only in the sidecar.
    assert vm.objects[DUMP]["metadata"]["ciphertext_sha256"] == manifest["ciphertext_sha256"]
    # The offline copy of the key plus the documented openssl line recovers the exact dump.
    assert decrypt(tmp_path, ciphertext, KEY, manifest["iterations"]) == PLAINTEXT

    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    assert fakes.no_secret_in(runner, KEY)
    assert leftovers(env) == []
    assert state(env)["last_success_utc"] == "2026-10-15T12:05:03Z"
    assert state(env)["last_object"] == DUMP


def test_manifest_is_uploaded_only_after_its_dump(env):
    vm = FakeVm()
    _, runner = run_main(env, vm)
    uploads = [c.argv[4] for c in runner.commands("storage cp")]
    assert uploads[:2] == [f"gs://{BUCKET}/{DUMP}", f"gs://{BUCKET}/{MANIFEST}"]


def test_dump_is_one_pg_dump_snapshot_taken_through_compose(env):
    _, runner = run_main(env, FakeVm())
    # Match the argument itself: pytest's temp paths contain this test's name.
    [dump] = [call for call in runner.calls if "pg_dump" in call.argv]
    assert dump.argv[:8] == [
        "docker",
        "compose",
        "-p",
        "mia",
        "-f",
        "/opt/mia/compose.yaml",
        "--env-file",
        "/opt/mia/tag.env",
    ]
    assert dump.argv[-10:] == [
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "-U",
        "mia",
        "-d",
        "mia",
        "--format=custom",
        # FORK-BACKUP: personal statement line detail stays out of every dump (V2-10)
        "--exclude-table-data=assaf_os.os_statement_batch_lines",
    ]
    # The plaintext is written under the work dir, never next to `mia backup`'s dumps.
    assert dump.stdout_path.is_relative_to(work_dir(env))


@pytest.mark.parametrize("value", ["", "not-a-key", "ab" * 31, "ab" * 33, "zz" * 32, "AB" * 32])
def test_a_malformed_key_fails_closed_before_any_dump_or_upload(env, value):
    vm = FakeVm(secret_value=value)
    code, runner = run_main(env, vm)
    assert code == 1 and vm.objects == {}
    assert not runner.commands("pg_dump") and not runner.commands("storage cp")
    assert state(env)["last_failure_code"] == "key_invalid"
    assert leftovers(env) == []


def test_an_unreachable_key_fails_closed_before_any_dump_or_upload(env):
    vm = FakeVm(secret_rc=1)
    code, runner = run_main(env, vm)
    assert code == 1 and vm.objects == {}
    assert not runner.commands("pg_dump") and not runner.commands("storage cp")
    assert state(env)["last_failure_code"] == "key_unavailable"


def test_key_is_read_from_secret_manager_and_trailing_newline_is_tolerated(env):
    vm = FakeVm(secret_value=KEY + "\n")
    code, runner = run_main(env, vm)
    assert code == 0
    [fetch] = runner.commands("secrets versions access")
    assert fetch.argv[1:6] == ["secrets", "versions", "access", "latest", "--secret=mia-backup-key"]


def test_failed_dump_uploads_nothing_and_leaves_no_files(env):
    vm = FakeVm(dump_rc=1)
    code, _ = run_main(env, vm)
    assert code == 1 and vm.objects == {}
    assert state(env)["last_failure_code"] == "dump_failed"
    assert leftovers(env) == []


@pytest.mark.parametrize(
    ("fault", "reason", "tool_text"),
    [
        ({"secret_rc": 1}, "key_unavailable", "PERMISSION_DENIED"),
        ({"dump_rc": 1}, "dump_failed", "pg_dump: error"),
        ({"cp_fail_containing": ("15min/",)}, "upload_failed", "ERROR: upload failed"),
    ],
)
def test_a_failure_logs_its_reason_code_only_never_tool_or_provider_text(
    env, capsys, fault, reason, tool_text
):
    """V2-10 review r1 / AGENTS.md: log reason codes and booleans, never provider payloads."""
    code, _ = run_main(env, FakeVm(**fault))
    err = capsys.readouterr().err
    assert code == 1
    assert f"failed code={reason}" in err
    assert tool_text not in err and "detail" not in err
    assert state(env)["last_failure_code"] == reason


@pytest.mark.parametrize(
    ("toc", "rc", "reason"),
    [
        ("", 1, "dump_unreadable"),
        ("not a pg_restore listing", 0, "dump_unreadable"),
        (TOC.replace("dbname: mia", "dbname: other"), 0, "dump_wrong_database"),
        ("\n".join(line for line in TOC.splitlines() if " TABLE " not in line), 0, "dump_empty"),
    ],
)
def test_a_dump_pg_restore_cannot_list_is_never_uploaded(env, toc, rc, reason):
    vm = FakeVm(toc=toc, toc_rc=rc)
    code, runner = run_main(env, vm)
    assert code == 1 and vm.objects == {}
    assert runner.commands("pg_restore --list")
    assert state(env)["last_failure_code"] == reason
    assert leftovers(env) == []


@pytest.mark.parametrize("fault", [{"describe_size_delta": 1}, {"describe_md5": "AAAA"}])
def test_an_upload_that_cannot_be_verified_fails_and_does_not_advance_state(env, fault):
    run_main(env, FakeVm())
    before = state(env)["last_success_utc"]
    code, _ = run_main(env, FakeVm(**fault), now=NOW + timedelta(minutes=15))
    assert code == 1
    assert state(env)["last_success_utc"] == before
    assert state(env)["last_failure_code"] == "upload_unverified"


def test_a_failed_upload_is_a_failure_and_leaves_no_plaintext(env):
    vm = FakeVm(cp_fail_containing=("15min/",))
    code, _ = run_main(env, vm)
    assert code == 1 and vm.objects == {}
    assert state(env)["last_failure_code"] == "upload_failed"
    assert leftovers(env) == []


def test_success_clears_the_failure_that_came_before_it(env):
    run_main(env, FakeVm(dump_rc=1))
    assert "last_failure_code" in state(env)
    code, _ = run_main(env, FakeVm(), now=NOW + timedelta(minutes=15))
    assert code == 0 and "last_failure_code" not in state(env)


def test_first_success_of_each_utc_day_is_also_promoted_to_daily(env):
    vm = FakeVm()
    run_main(env, vm, now=datetime(2026, 10, 15, 0, 5, 3, tzinfo=UTC))
    assert vm.names("daily/") == [
        "daily/mia-20261015T000503Z.dump.enc",
        "daily/mia-20261015T000503Z.manifest.json",
    ]
    run_main(env, vm, now=datetime(2026, 10, 15, 0, 20, 3, tzinfo=UTC))
    assert len(vm.names("daily/")) == 2 and len(vm.names("15min/")) == 4
    run_main(env, vm, now=datetime(2026, 10, 16, 0, 5, 3, tzinfo=UTC))
    assert len(vm.names("daily/")) == 4
    # The promoted copy is byte-identical to the 15-minute object, and carries its sha256.
    assert (
        vm.objects["daily/mia-20261015T000503Z.dump.enc"]["data"]
        == vm.objects["15min/mia-20261015T000503Z.dump.enc"]["data"]
    )


def test_a_missed_midnight_slot_still_gets_a_daily_copy_later_that_day(env):
    vm = FakeVm()
    run_main(env, vm, now=datetime(2026, 10, 15, 6, 35, 3, tzinfo=UTC))
    assert vm.names("daily/") == [
        "daily/mia-20261015T063503Z.dump.enc",
        "daily/mia-20261015T063503Z.manifest.json",
    ]


def test_a_failed_daily_copy_is_reported_and_retried_by_the_next_run(env):
    vm = FakeVm(cp_fail_containing=("daily/",))
    first = datetime(2026, 10, 15, 0, 5, 3, tzinfo=UTC)
    code, _ = run_main(env, vm, now=first)
    assert code == 1
    assert len(vm.names("15min/")) == 2 and vm.names("daily/") == []
    assert state(env)["last_failure_code"] == "daily_copy_failed"
    assert state(env)["last_success_utc"] == "2026-10-15T00:05:03Z"
    assert "last_daily" not in state(env)

    vm.cp_fail_containing = ()
    code, _ = run_main(env, vm, now=first + timedelta(minutes=15))
    assert code == 0 and len(vm.names("daily/")) == 2
    assert state(env)["last_daily"] == "2026-10-15"


def test_stale_run_directories_are_pruned_and_nothing_else_is_touched(tmp_path):
    runs = tmp_path / "work" / "runs"
    runs.mkdir(parents=True)
    old = runs / "run-20261014T000000Z-111"
    fresh = runs / "run-20261015T120000Z-222"
    foreign = runs / "keep-me"
    for folder in (old, fresh, foreign):
        folder.mkdir()
        (folder / "payload.bin").write_bytes(b"x")
    stray_file = runs / "run-20261014T000000Z-333"
    stray_file.write_bytes(b"a file with a run name is still not a run directory")
    elsewhere = tmp_path / "backups"
    elsewhere.mkdir()
    (elsewhere / "pre-restore-1.dump").write_bytes(b"someone else's dump")

    now = NOW.timestamp()
    os.utime(old, (now - 3 * 3600, now - 3 * 3600))
    os.utime(foreign, (now - 3 * 3600, now - 3 * 3600))
    os.utime(fresh, (now - 600, now - 600))

    assert backup_offsite.prune_stale_runs(runs, now, max_age_seconds=7200) == [old.name]
    assert not old.exists()
    assert fresh.exists() and foreign.exists() and stray_file.exists()
    assert (elsewhere / "pre-restore-1.dump").read_bytes() == b"someone else's dump"


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only; the VM runs Linux")
def test_an_overlapping_run_is_skipped_not_stacked(env, capsys):
    import fcntl

    work_dir(env).mkdir(parents=True)
    with open(work_dir(env) / "lock", "w", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, runner = run_main(env, FakeVm())
    assert code == 0 and runner.calls == []
    assert "already running" in capsys.readouterr().err


def test_a_slot_is_skipped_with_a_reason_code_while_mia_holds_the_state_lock(
    env, capsys, monkeypatch
):
    """V2-10 review r1: `mia restore` and deploy hold the state lock; a dump then is partial.

    The timer fires on its own schedule, so the run asks for the same lock before it touches the
    database. Nothing is dumped or uploaded and nothing is recorded as a failure: the next slot,
    15 minutes later, takes the backup, and `--status` goes stale after three missed slots.
    """
    asked: list[Path] = []

    def busy(path):
        asked.append(path)
        return True

    monkeypatch.setattr(backup_offsite, "state_lock_busy", busy)
    code, runner = run_main(env, FakeVm())
    assert code == 0 and runner.calls == []
    err = capsys.readouterr().err
    assert "skipped code=state_lock_busy" in err
    assert asked == [Path(env["MIA_STATE_LOCK"])]
    assert not (work_dir(env) / "state.json").exists() and leftovers(env) == []


def test_the_state_lock_is_the_one_mia_takes_and_defaults_to_its_path():
    script = (ROOT / "ops/gcp/vm/mia").read_text(encoding="utf-8")
    assert "exec 9>/var/lock/mia-state.lock" in script
    assert backup_offsite.STATE_LOCK == Path("/var/lock/mia-state.lock")
    assert backup_offsite.Config(bucket="b").state_lock == backup_offsite.STATE_LOCK


def test_a_state_lock_nobody_holds_or_that_does_not_exist_never_stops_the_backup(env, tmp_path):
    lock = Path(env["MIA_STATE_LOCK"])
    assert not lock.exists()
    assert backup_offsite.state_lock_busy(lock) is False
    assert not lock.exists()  # asking never creates it
    code, _ = run_main(env, FakeVm())
    assert code == 0 and state(env)["last_object"] == DUMP
    lock.write_text("", encoding="utf-8")
    assert backup_offsite.state_lock_busy(lock) is False
    code, _ = run_main(env, FakeVm(), now=NOW + timedelta(minutes=15))
    assert code == 0


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only; the VM runs Linux")
def test_a_real_held_state_lock_is_seen_and_released_for_the_next_slot(env, capsys):
    import fcntl

    lock = Path(env["MIA_STATE_LOCK"])
    with open(lock, "w", encoding="utf-8") as held:  # what `exec 9>/var/lock/mia-state.lock` does
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert backup_offsite.state_lock_busy(lock) is True
        code, runner = run_main(env, FakeVm())
        assert code == 0 and runner.calls == []
        assert "skipped code=state_lock_busy" in capsys.readouterr().err
    assert backup_offsite.state_lock_busy(lock) is False  # the check itself leaves no lock behind
    code, _ = run_main(env, FakeVm(), now=NOW + timedelta(minutes=15))
    assert code == 0


@pytest.mark.parametrize("bucket", ["", "UPPER", "a", "has space", "gs://bucket", "bucket/sub"])
def test_the_bucket_must_be_a_plain_bucket_name(tmp_path, bucket):
    vm = FakeVm()
    runner = FakeRunner(vm.handler)
    environ = {"MIA_BACKUP_BUCKET": bucket, "MIA_BACKUP_WORK_DIR": str(tmp_path / "work")}
    assert backup_offsite.main([], runner=runner, environ=environ, now=NOW) == 2
    assert runner.calls == []


def test_status_reports_the_age_of_the_last_good_backup(env, capsys):
    run_main(env, FakeVm())
    capsys.readouterr()
    code, _ = run_main(env, FakeVm(), now=NOW + timedelta(minutes=10), argv=["--status"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["healthy"] is True and report["age_minutes"] == 10

    code, _ = run_main(env, FakeVm(), now=NOW + timedelta(minutes=50), argv=["--status"])
    assert code == 1 and json.loads(capsys.readouterr().out)["healthy"] is False


def test_status_with_no_backup_yet_is_unhealthy(env, capsys):
    code, runner = run_main(env, FakeVm(), argv=["--status"])
    assert code == 1 and runner.calls == []
    assert json.loads(capsys.readouterr().out)["reason"] == "no_backup_recorded"


def test_shipped_encryption_parameters_are_strong():
    # The other tests lower the iteration count for speed; this is the value that ships.
    assert SHIPPED_ITERATIONS >= 600_000
    assert backup_lib.CIPHER == "aes-256-cbc"


def test_status_works_on_the_vm_without_any_settings_loaded(env, capsys):
    # `sudo python3 backup_offsite.py --status` is typed by hand, outside systemd's EnvironmentFile.
    run_main(env, FakeVm())
    capsys.readouterr()
    only_work_dir = {"MIA_BACKUP_WORK_DIR": env["MIA_BACKUP_WORK_DIR"]}
    runner = FakeRunner()
    code = backup_offsite.main(
        ["--status"], runner=runner, environ=only_work_dir, now=NOW + timedelta(minutes=5)
    )
    assert code == 0 and runner.calls == []
    assert json.loads(capsys.readouterr().out)["healthy"] is True
