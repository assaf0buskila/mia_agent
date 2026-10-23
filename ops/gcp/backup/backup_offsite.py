"""Mia's encrypted off-host database backup. systemd runs it every 15 minutes on the VM (V2-10).

One run:
  1. read the encryption key from Secret Manager. A missing or malformed key stops the run
     here: nothing is dumped and nothing is uploaded, so plaintext can never leave the VM.
  2. pg_dump of database mia (one consistent snapshot) into a private work directory.
  3. pg_restore --list must read the dump and find the mia tables.
  4. encrypt with openssl (key from a 0600 file, never a command line), then prove the
     ciphertext decrypts to the same bytes. Delete the plaintext.
  5. upload ciphertext, then manifest, to gs://<bucket>/15min/ and verify both objects.
  6. the first success of each UTC day is uploaded again under daily/.

A slot is skipped (reason code `state_lock_busy`, exit 0) while `mia restore`, `mia deploy` or
another state-changing `mia` command holds /var/lock/mia-state.lock: a dump taken in the middle of
a restore is a partial database, and it would be uploaded as the newest good backup. The run holds
that lock SHARED from before the key fetch until the dump is on disk (V2-10b), so a restore cannot
start in between; the exclusive `flock -n 9` of a `mia` command reports busy meanwhile.

A slot is also skipped (reason code `restore_incomplete`, exit 0) while /etc/mia/restore-incomplete
exists: `mia restore` writes it before it drops the database and removes it only after every
post-restore step succeeded, so an aborted restore is never dumped as the newest good backup.

Standard library only. It logs reason codes and never a key, dump content or provider payload.
`--status` prints the age of the last good backup and exits 1 when it is stale.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import backup_lib
from backup_lib import BackupError

BUCKET_NAME = backup_lib.BUCKET_NAME
STATE_LOCK = Path("/var/lock/mia-state.lock")  # the lock `mia` takes for deploy, restore and more
RESTORE_INCOMPLETE = Path("/etc/mia/restore-incomplete")  # `mia restore` writes it (V2-10b)
RUN_DIR = re.compile(r"run-\d{8}T\d{6}Z-\d+")
FAILURE_KEYS = ("last_failure_utc", "last_failure_code")


@dataclass(frozen=True)
class Config:
    bucket: str
    project: str = ""
    secret: str = backup_lib.SECRET_NAME
    work_dir: Path = Path("/var/lib/mia/backup-offsite")
    state_lock: Path = STATE_LOCK
    restore_incomplete: Path = RESTORE_INCOMPLETE
    compose_file: str = "/opt/mia/compose.yaml"
    tag_env: str = "/opt/mia/tag.env"
    database: str = backup_lib.DATABASE
    db_user: str = "mia"
    docker: str = "docker"
    gcloud: str = "gcloud"
    openssl: str = "openssl"
    stale_run_seconds: int = 2 * 60 * 60
    # Three missed 15-minute slots: past this the newest backup is too old to call healthy.
    max_age_minutes: int = 45

    @classmethod
    def from_env(cls, environ) -> Config:
        bucket = environ.get("MIA_BACKUP_BUCKET", "")
        if not BUCKET_NAME.fullmatch(bucket):
            raise BackupError("config_invalid", "MIA_BACKUP_BUCKET is not a bucket name")
        return cls(
            bucket=bucket,
            project=environ.get("MIA_BACKUP_PROJECT", ""),
            secret=environ.get("MIA_BACKUP_SECRET", backup_lib.SECRET_NAME),
            work_dir=Path(environ.get("MIA_BACKUP_WORK_DIR", "/var/lib/mia/backup-offsite")),
            state_lock=Path(environ.get("MIA_STATE_LOCK", str(STATE_LOCK))),
            restore_incomplete=Path(
                environ.get("MIA_RESTORE_INCOMPLETE", str(RESTORE_INCOMPLETE))
            ),
        )


class SlotSkipped(Exception):
    """This slot must not dump. Not a failure: main logs the code and exits 0."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def log(message: str) -> None:
    print(f"mia-backup-offsite: {message}", file=sys.stderr, flush=True)


# --- state ----------------------------------------------------------------------------------


def load_state(config: Config) -> dict:
    try:
        document = json.loads((config.work_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def save_state(config: Config, state: dict) -> None:
    config.work_dir.mkdir(parents=True, exist_ok=True)
    temporary = config.work_dir / "state.json.tmp"
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, config.work_dir / "state.json")


def record_failure(config: Config, now: datetime, code: str) -> None:
    state = load_state(config)
    state["last_failure_utc"] = backup_lib.format_time(now)
    state["last_failure_code"] = code
    save_state(config, state)


# --- housekeeping ---------------------------------------------------------------------------


def prune_stale_runs(runs_dir: Path, now_ts: float, max_age_seconds: float) -> list[str]:
    """Remove run directories a crashed run left behind. Touches nothing that is not ours."""
    removed: list[str] = []
    if not runs_dir.is_dir():
        return removed
    for entry in sorted(runs_dir.iterdir()):
        if not (entry.is_dir() and RUN_DIR.fullmatch(entry.name)):
            continue
        if now_ts - entry.stat().st_mtime > max_age_seconds:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def acquire_lock(work_dir: Path):
    """An exclusive lock for the whole run, or None when another run holds it."""
    try:
        import fcntl
    except ImportError:  # not Linux: development machines only, the VM always has fcntl
        return open(os.devnull, "rb")
    handle = open(work_dir / "lock", "a+", encoding="utf-8")  # held until the run ends
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def state_lock_busy(path: Path) -> bool:
    """True when a state-changing `mia` command holds the lock, so this slot must not dump.

    An early look only (V2-10b): it asks for the lock SHARED and drops it at once, and the file is
    never created. It is not the protection: between this look and the dump a `mia` command could
    still take the lock, which is what shared_state_lock closes. If there is no lock file, or it can
    not be opened, nobody can be holding it and the run goes on: a missed backup costs more than a
    dump taken beside a command that left no lock to see.
    """
    try:
        import fcntl
    except ImportError:  # not Linux: development machines only, the VM always has fcntl
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def shared_state_lock(path: Path):
    """Hold the state lock SHARED for the body, or raise SlotSkipped("state_lock_busy").

    `mia` takes the same file exclusively (`exec 9>...; flock -n 9`), so while this is held a
    restore, deploy or other state-changing command reports busy, and a restore that already holds
    the lock makes this raise: the check and the use are one atomic step (V2-10 review nit 6).

    The file is created when missing (/var/lock is a tmpfs, gone after a reboot) so that a `mia`
    command opens the same inode. If it still can not be opened the run goes on without the
    protection, as state_lock_busy does, and says so with a reason code. The lock is released
    explicitly and the descriptor closed, also on SystemExit (the SIGTERM handler's).
    """
    try:
        import fcntl
    except ImportError:  # not Linux: development machines only, the VM always has fcntl
        yield
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CREAT, 0o600)
    except OSError:
        log("state lock not held code=state_lock_unavailable")
        yield
        return
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            raise SlotSkipped("state_lock_busy") from error
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def restore_marker_present(path: Path) -> bool:
    """True unless the restore-incomplete marker is definitely absent.

    Fail closed: only "no such file" (or a parent that is not a directory) lets a dump through; any
    other error means the marker could not be ruled out, so the slot is skipped.
    """
    try:
        os.lstat(path)
    except OSError as error:
        return error.errno not in (errno.ENOENT, errno.ENOTDIR)
    return True


# --- the steps ------------------------------------------------------------------------------


def compose_exec(config: Config) -> list[str]:
    """Run a command in the postgres container, the same way `mia backup` does."""
    return [
        config.docker,
        "compose",
        "-p",
        "mia",
        "-f",
        config.compose_file,
        "--env-file",
        config.tag_env,
        "exec",
        "-T",
        "postgres",
    ]


def with_project(config: Config, argv: list[str]) -> list[str]:
    return [*argv, f"--project={config.project}"] if config.project else argv


def fetch_key(config: Config, runner) -> str:
    argv = with_project(
        config,
        [config.gcloud, "secrets", "versions", "access", "latest", f"--secret={config.secret}"],
    )
    done = runner.run(argv, timeout=60)
    if done.returncode != 0:
        raise BackupError(
            "key_unavailable", f"gcloud exit {done.returncode}: {backup_lib.tail(done.stderr)}"
        )
    return backup_lib.validate_key(done.stdout.decode("utf-8", "replace"))


def take_dump(config: Config, runner, target: Path) -> None:
    argv = [
        *compose_exec(config),
        "pg_dump",
        "-U",
        config.db_user,
        "-d",
        config.database,
        "--format=custom",
        f"--exclude-table-data={backup_lib.DUMP_EXCLUDE_TABLE_DATA}",
    ]
    done = runner.run(argv, stdout_path=target, timeout=900)
    if done.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        raise BackupError(
            "dump_failed", f"pg_dump exit {done.returncode}: {backup_lib.tail(done.stderr)}"
        )


def read_dump(config: Config, runner, dump: Path) -> backup_lib.TocSummary:
    argv = [*compose_exec(config), "pg_restore", "--list"]
    done = runner.run(argv, stdin_path=dump, timeout=300)
    if done.returncode != 0:
        raise BackupError(
            "dump_unreadable", f"pg_restore exit {done.returncode}: {backup_lib.tail(done.stderr)}"
        )
    toc = backup_lib.toc_summary(done.stdout.decode("utf-8", "replace"))
    if toc.dbname != config.database:
        raise BackupError("dump_wrong_database", "the archive is not of the mia database")
    if toc.public_tables == 0:
        raise BackupError("dump_empty", "the archive has no public tables")
    return toc


def seal(config: Config, runner, key_file: Path, dump: Path, sealed: Path, check: Path) -> None:
    """Encrypt the dump, then prove the ciphertext decrypts back to the same bytes."""
    done = runner.run(backup_lib.encrypt_argv(config.openssl, key_file, dump, sealed), timeout=900)
    if done.returncode != 0:
        raise BackupError(
            "encrypt_failed", f"openssl exit {done.returncode}: {backup_lib.tail(done.stderr)}"
        )
    opened = runner.run(
        backup_lib.decrypt_argv(
            config.openssl, key_file, sealed, check, backup_lib.PBKDF2_ITERATIONS
        ),
        timeout=900,
    )
    try:
        matches = opened.returncode == 0 and (
            backup_lib.sha256_file(check)[0] == backup_lib.sha256_file(dump)[0]
        )
    finally:
        check.unlink(missing_ok=True)
    if not matches:
        raise BackupError("encrypt_unverified", "the ciphertext does not decrypt to the dump")


def upload(config: Config, runner, source: Path, name: str, sha256: str = "") -> None:
    argv = [config.gcloud, "storage", "cp", str(source), f"gs://{config.bucket}/{name}"]
    if sha256:
        argv.append(f"--custom-metadata=ciphertext_sha256={sha256}")
    done = runner.run(with_project(config, argv), timeout=600)
    if done.returncode != 0:
        raise BackupError(
            "upload_failed", f"gcloud exit {done.returncode}: {backup_lib.tail(done.stderr)}"
        )


def verify_upload(config: Config, runner, source: Path, name: str, sha256: str = "") -> None:
    """Read the object's metadata back and compare it with the local file."""
    argv = [
        config.gcloud,
        "storage",
        "objects",
        "describe",
        f"gs://{config.bucket}/{name}",
        "--format=json",
    ]
    done = runner.run(with_project(config, argv), timeout=120)
    if done.returncode != 0:
        raise BackupError("upload_unverified", f"describe exit {done.returncode}")
    try:
        remote = json.loads(done.stdout)
        remote_size = int(remote["size"])
    except (ValueError, KeyError, TypeError) as error:
        raise BackupError("upload_unverified", "unreadable object metadata") from error
    if remote_size != source.stat().st_size:
        raise BackupError("upload_unverified", "stored size differs from the local file")
    stored_md5 = remote.get("md5_hash") or remote.get("md5Hash")
    if stored_md5:
        digest = hashlib.md5(usedforsecurity=False)  # GCS reports md5; this is not a security use
        with open(source, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if stored_md5 not in (base64.b64encode(digest.digest()).decode(), digest.hexdigest()):
            raise BackupError("upload_unverified", "stored md5 differs from the local file")
    if sha256:
        custom = remote.get("custom_fields") or remote.get("metadata") or {}
        recorded = custom.get("ciphertext_sha256") if isinstance(custom, dict) else None
        if recorded is not None and recorded != sha256:
            raise BackupError("upload_unverified", "recorded sha256 differs from the local file")


def run_backup(config: Config, runner, now: datetime) -> dict:
    # The state lock is held SHARED from before the key fetch until the dump is on disk, so a
    # `mia restore` cannot start in between (it takes the lock exclusively and would report busy),
    # and the marker is read under it: the restore writes the marker while it holds the lock, so
    # either the marker is already there or no restore can begin until the dump is taken.
    with contextlib.ExitStack() as state_hold:
        state_hold.enter_context(shared_state_lock(config.state_lock))
        if restore_marker_present(config.restore_incomplete):
            raise SlotSkipped("restore_incomplete")
        key = fetch_key(config, runner)
        run_dir = config.work_dir / "runs" / f"run-{now:%Y%m%dT%H%M%SZ}-{os.getpid()}"
        run_dir.mkdir(parents=True, mode=0o700)
        try:
            backup_lib.write_private(run_dir / "key", key + "\n")
            take_dump(config, runner, run_dir / "dump.bin")
            state_hold.close()  # the dump is on disk: a restore may take the lock now
            return finish_backup(config, runner, now, key, run_dir)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)


def finish_backup(config: Config, runner, now: datetime, key: str, run_dir: Path) -> dict:
    """Everything after the dump: check, seal, upload, verify, record. The caller removes run_dir.

    The dump is on disk and the state lock is already released when this runs.
    """
    stem = backup_lib.object_stem(now)
    key_file, dump = run_dir / "key", run_dir / "dump.bin"
    sealed, manifest_file = run_dir / "dump.enc", run_dir / "manifest.json"

    toc = read_dump(config, runner, dump)
    plain_sha, plain_bytes = backup_lib.sha256_file(dump)
    seal(config, runner, key_file, dump, sealed, run_dir / "check.bin")
    dump.unlink()
    key_file.unlink()

    cipher_sha, cipher_bytes = backup_lib.sha256_file(sealed)
    dump_object = stem + backup_lib.DUMP_SUFFIX
    manifest_object = stem + backup_lib.MANIFEST_SUFFIX
    manifest = backup_lib.build_manifest(
        database=config.database,
        created_at=now,
        host=socket.gethostname(),
        dump_object=dump_object,
        ciphertext_sha256=cipher_sha,
        ciphertext_bytes=cipher_bytes,
        plaintext_sha256=plain_sha,
        plaintext_bytes=plain_bytes,
        key=key,
        toc=toc,
    )
    manifest_file.write_bytes(backup_lib.dump_manifest(manifest))

    # The manifest goes last: an object with no manifest is an unfinished backup.
    prefix = backup_lib.PREFIX_15MIN
    upload(config, runner, sealed, prefix + dump_object, cipher_sha)
    upload(config, runner, manifest_file, prefix + manifest_object)
    verify_upload(config, runner, sealed, prefix + dump_object, cipher_sha)
    verify_upload(config, runner, manifest_file, prefix + manifest_object)

    state = load_state(config)
    for field in FAILURE_KEYS:
        state.pop(field, None)
    state.update(
        last_success_utc=backup_lib.format_time(now),
        last_object=prefix + dump_object,
        key_id=backup_lib.key_id(key),
    )
    save_state(config, state)

    today = now.strftime("%Y-%m-%d")
    promoted = state.get("last_daily") != today
    if promoted:
        try:
            prefix = backup_lib.PREFIX_DAILY
            upload(config, runner, sealed, prefix + dump_object, cipher_sha)
            upload(config, runner, manifest_file, prefix + manifest_object)
            verify_upload(config, runner, sealed, prefix + dump_object, cipher_sha)
            verify_upload(config, runner, manifest_file, prefix + manifest_object)
        except BackupError as error:
            raise BackupError("daily_copy_failed", error.code) from error
        state["last_daily"] = today
        save_state(config, state)
    return {"object": dump_object, "bytes": cipher_bytes, "daily": promoted}


# --- status and entry point -----------------------------------------------------------------


def report_status(config: Config, now: datetime) -> int:
    state = load_state(config)
    last = state.get("last_success_utc")
    if not last:
        report = {"healthy": False, "reason": "no_backup_recorded"}
    else:
        taken = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        age = int((now - taken).total_seconds() // 60)
        report = {
            "healthy": age <= config.max_age_minutes,
            "age_minutes": age,
            "last_success_utc": last,
            "last_object": state.get("last_object", ""),
        }
        if state.get("last_failure_code"):
            report["last_failure_code"] = state["last_failure_code"]
    print(json.dumps(report, sort_keys=True))
    return 0 if report["healthy"] else 1


def main(argv=None, *, runner=None, environ=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Encrypted off-host backup of database mia.")
    parser.add_argument(
        "--status",
        action="store_true",
        help="print the age of the last good backup; exit 1 when stale",
    )
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    runner = runner if runner is not None else backup_lib.Runner()
    now = backup_lib.utc(now) if now is not None else datetime.now(UTC)
    if args.status:
        # Typed by hand, so the settings file systemd loads for the service may be absent.
        work_dir = Path(environ.get("MIA_BACKUP_WORK_DIR", str(Config.work_dir)))
        return report_status(Config(bucket="", work_dir=work_dir), now)
    try:
        config = Config.from_env(environ)
    except BackupError as error:
        log(f"not configured code={error.code}")
        return 2

    config.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_lock_busy(config.state_lock):
        log("skipped code=state_lock_busy")
        return 0
    lock = acquire_lock(config.work_dir)
    if lock is None:
        log("already running; skipping this slot")
        return 0
    try:
        prune_stale_runs(config.work_dir / "runs", now.timestamp(), config.stale_run_seconds)
        (config.work_dir / "runs").mkdir(exist_ok=True, mode=0o700)
        result = run_backup(config, runner, now)
    except SlotSkipped as skip:
        log(f"skipped code={skip.code}")
        return 0
    except BackupError as error:
        # The code only. error.detail can carry gcloud, pg_dump or openssl stderr, which is
        # provider text and must never reach journald (AGENTS.md: reason codes and booleans).
        log(f"failed code={error.code}")
        record_failure(config, now, error.code)
        return 1
    finally:
        lock.close()
    log(f"ok object={result['object']} bytes={result['bytes']} daily={result['daily']}")
    return 0


def install_sigterm_handler() -> None:
    """systemd sends SIGTERM at TimeoutStartSec. Turn it into SystemExit so `finally` blocks run.

    run_backup then removes its run directory, with the plaintext dump and the key file in it.

    Called by the entry point only, never at import: a test or a caller that imports this module
    keeps its own signal handling.
    """

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)


if __name__ == "__main__":
    install_sigterm_handler()
    sys.exit(main())
