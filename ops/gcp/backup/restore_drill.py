"""Mia's restore drill: prove a real off-host backup restores, without touching production (V2-10).

Run it by hand on Assaf's PC, never on the VM and never from a timer. It:
  1. downloads a backup pair (or reads a folder), checks the key fingerprint and both sha256s;
  2. decrypts with the key file Assaf supplies now (the offline copy; nothing is read from the VM);
  3. restores into a database called mia_drill_<UTC timestamp> on a local scratch PostgreSQL server
     and nowhere else: the name is validated, the server must be this machine or a container, and
     the drill refuses a server that has a database called mia;
  4. runs sanity queries and grant posture checks, and re-owns/re-grants schema assaf_os when the
     dump has it (ledger_posture), failing unless the posture is clean;
  5. drops the scratch database and deletes every decrypted byte, then prints a JSON report that
     records the duration against the 2 hour RTO.

It never starts a service, never contacts the VM, and never writes to the production bucket.
Standard library only. The report holds counts, names and booleans: no key, no dump content.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import backup_lib
import ledger_posture
from backup_lib import BackupError

SCRATCH_NAME = re.compile(r"mia_drill_\d{8}_\d{6}")
CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_MAX_AGE_MINUTES = 45

_SQL_COMMENT = re.compile(r"(?m)^--.*$")
_ACL_STATEMENT = re.compile(
    r"\b(?:GRANT|ALTER\s+DEFAULT\s+PRIVILEGES)\b.*?;", re.IGNORECASE | re.DOTALL
)
_TO_PUBLIC = re.compile(r"\bTO\b[^;]*\bPUBLIC\b", re.IGNORECASE)
_DEFAULT_PUBLIC_GRANT = "grant usage on schema public to public"
_MANIFEST_OBJECT = re.compile(r"(?:^|/)(mia-\d{8}T\d{6}Z)\.manifest\.json$")


class DrillRefused(Exception):
    """The drill will not run against this target. Nothing has been changed."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --- where it may restore -------------------------------------------------------------------


def scratch_name(now: datetime) -> str:
    return "mia_drill_" + backup_lib.utc(now).strftime("%Y%m%d_%H%M%S")


def validate_scratch_name(name: str) -> str:
    """The only database the drill may create, restore into or drop."""
    if not isinstance(name, str) or not SCRATCH_NAME.fullmatch(name):
        raise DrillRefused("scratch_name_invalid", "the drill only touches mia_drill_<timestamp>")
    return name


@dataclass(frozen=True)
class Target:
    """A scratch PostgreSQL server: a local docker container, or a loopback address."""

    container: str = ""
    host: str = ""
    port: int = 0
    user: str = "postgres"
    password: str = ""
    maintenance_db: str = "postgres"

    def describe(self) -> str:
        return f"container:{self.container}" if self.container else f"{self.host}:{self.port}"

    def _tool(self, name: str, options: list[str]) -> tuple[list[str], dict[str, str]]:
        if self.container:
            return ["docker", "exec", "-i", self.container, name, *options, "-U", self.user], {}
        argv = [name, *options, "-h", self.host, "-p", str(self.port), "-U", self.user]
        return argv, ({"PGPASSWORD": self.password} if self.password else {})

    def psql(self, database: str, sql: str) -> tuple[list[str], dict[str, str]]:
        argv, env = self._tool("psql", [])
        return [*argv, "-d", database, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At", "-c", sql], env

    def pg_restore(self, *options: str, database: str = "") -> tuple[list[str], dict[str, str]]:
        argv, env = self._tool("pg_restore", list(options))
        return ([*argv, "-d", database] if database else argv), env


def build_target(
    *, container: str | None = None, url: str | None = None, user: str = "postgres"
) -> Target:
    """Exactly one scratch server, and never one that is not on this machine."""
    if bool(container) == bool(url):
        raise DrillRefused("scratch_target_required", "give exactly one scratch server")
    if container:
        if not CONTAINER_NAME.fullmatch(container):
            raise DrillRefused("scratch_target_invalid", "not a container name")
        return Target(container=container, user=user)
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as error:
        raise DrillRefused("scratch_target_invalid", "bad port") from error
    if (
        parts.scheme not in ("postgresql", "postgres")
        or (parts.hostname or "") not in LOOPBACK_HOSTS
    ):
        raise DrillRefused(
            "scratch_target_not_local", "the scratch server must be localhost, 127.0.0.1 or [::1]"
        )
    database = unquote(parts.path.lstrip("/")) or "postgres"
    if database == backup_lib.DATABASE:
        raise DrillRefused("scratch_target_invalid", "the maintenance database is not mia")
    return Target(
        host=parts.hostname,
        port=port or 5432,
        user=unquote(parts.username or user),
        password=unquote(parts.password or ""),
        maintenance_db=database,
    )


# --- configuration and report ---------------------------------------------------------------


@dataclass
class DrillConfig:
    key_file: Path
    target: Target
    from_dir: Path | None = None
    bucket: str = ""
    latest: bool = False
    prefix: str = backup_lib.PREFIX_15MIN
    project: str = ""
    work_root: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "mia-restore-drill"
    )
    openssl: str = "openssl"
    gcloud: str = "gcloud"
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES
    ledger_grants_file: Path | None = None


class Report:
    def __init__(self, started_at: datetime, target: Target):
        self.data: dict = {
            "started_utc": backup_lib.format_time(started_at),
            "scratch_server": target.describe(),
            "scratch_database": None,
            "scratch_dropped": False,
            "checks": [],
            "backup": {},
            "ledger": {},
            "durations_seconds": {},
            "rto_seconds": backup_lib.RTO_SECONDS,
            "rto_met": False,
            "passed": False,
        }

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        entry = {"name": name, "ok": bool(ok)}
        if detail:
            entry["detail"] = detail
        self.data["checks"].append(entry)
        return bool(ok)

    def ok(self) -> bool:
        return all(check["ok"] for check in self.data["checks"])


def public_grant_findings(schema_sql: str) -> list[str]:
    """Statements in a dump's schema that give anything to PUBLIC, except the default USAGE."""
    findings = []
    for found in _ACL_STATEMENT.finditer(_SQL_COMMENT.sub("", schema_sql)):
        statement = " ".join(found.group(0).split()).rstrip(";").strip()
        if _TO_PUBLIC.search(statement) and statement.lower() != _DEFAULT_PUBLIC_GRANT:
            findings.append(statement[:120])
    return findings


# --- finding the backup ---------------------------------------------------------------------


def with_project(config: DrillConfig, flags: list[str]) -> list[str]:
    return [*flags, f"--project={config.project}"] if config.project else flags


def locate_latest(config: DrillConfig, runner, folder: Path) -> str:
    """Name of the newest manifest in the bucket; both objects are downloaded into folder."""
    pattern = f"gs://{config.bucket}/{config.prefix}mia-*.manifest.json"
    listing = runner.run(
        with_project(config, [config.gcloud, "storage", "ls", pattern]), timeout=120
    )
    if listing.returncode != 0:
        raise BackupError(
            "locate_failed", f"gcloud exit {listing.returncode}: {backup_lib.tail(listing.stderr)}"
        )
    prefix = f"gs://{config.bucket}/"
    names = sorted(
        line.strip().removeprefix(prefix)
        for line in listing.stdout.decode("utf-8", "replace").splitlines()
        if _MANIFEST_OBJECT.search(line.strip())
    )
    if not names:
        raise BackupError("locate_failed", "no backup manifest in the bucket")
    manifest = names[-1]
    dump = manifest.removesuffix(backup_lib.MANIFEST_SUFFIX) + backup_lib.DUMP_SUFFIX
    folder.mkdir(parents=True, exist_ok=True)
    copy = runner.run(
        [
            config.gcloud,
            "storage",
            "cp",
            *with_project(config, []),
            prefix + manifest,
            prefix + dump,
            str(folder),
        ],
        timeout=1800,
    )
    if copy.returncode != 0:
        raise BackupError(
            "download_failed", f"gcloud exit {copy.returncode}: {backup_lib.tail(copy.stderr)}"
        )
    return manifest


def locate_in_dir(folder: Path) -> str:
    manifests = sorted(p.name for p in folder.glob("mia-*" + backup_lib.MANIFEST_SUFFIX))
    if not manifests:
        raise BackupError("locate_failed", "no backup manifest in the folder")
    return manifests[-1]


# --- the drill ------------------------------------------------------------------------------


def number(result: backup_lib.Result) -> int | None:
    try:
        return int(result.stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return None


def run_drill(
    config: DrillConfig, runner, *, clock=time.monotonic, now: datetime | None = None
) -> dict:
    now = backup_lib.utc(now) if now is not None else datetime.now(UTC)
    scratch = validate_scratch_name(scratch_name(now))  # before anything else is touched
    report = Report(now, config.target)
    started = clock()

    @contextmanager
    def timed(phase: str):
        begin = clock()
        try:
            yield
        finally:
            seconds = report.data["durations_seconds"]
            seconds[phase] = round(seconds.get(phase, 0.0) + (clock() - begin), 3)

    def finish() -> dict:
        total = clock() - started
        report.data["durations_seconds"]["total"] = round(total, 3)
        report.data["rto_met"] = total <= backup_lib.RTO_SECONDS
        report.data["passed"] = report.ok() and report.data["rto_met"]
        return report.data

    try:
        key = backup_lib.validate_key(config.key_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, BackupError) as error:
        report.check("key_valid", False, getattr(error, "code", "key_file_unreadable"))
        return finish()
    report.check("key_valid", True)

    config.work_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="drill-", dir=config.work_root))
    try:
        _drill(config, runner, report, timed, run_dir, key, scratch, now)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return finish()


def _drill(
    config, runner, report: Report, timed, run_dir: Path, key: str, scratch: str, now: datetime
) -> None:
    target = config.target

    def psql(database: str, sql: str) -> backup_lib.Result:
        argv, env = target.psql(database, sql)
        return runner.run(argv, env=env or None, timeout=600)

    def pg_restore(*options: str, database: str = "", stdin: Path | None = None):
        argv, env = target.pg_restore(*options, database=database)
        return runner.run(argv, stdin_path=stdin, env=env or None, timeout=7200)

    # 1. find, verify and decrypt the backup
    with timed("download"):
        try:
            if config.latest:
                folder = run_dir / "download"
                manifest_name = locate_latest(config, runner, folder)
            elif config.from_dir is not None:
                folder = config.from_dir
                manifest_name = locate_in_dir(folder)
            else:
                raise BackupError("locate_failed", "no backup source given")
        except (BackupError, OSError) as error:
            report.check("locate_backup", False, getattr(error, "code", "folder_unreadable"))
            return
    report.check("locate_backup", True)

    with timed("verify"):
        try:
            manifest = backup_lib.load_manifest(
                (folder / manifest_name.rsplit("/", 1)[-1]).read_bytes()
            )
        except (OSError, BackupError) as error:
            report.check("manifest_valid", False, getattr(error, "code", "manifest_unreadable"))
            return
        report.check("manifest_valid", True)
        created = datetime.strptime(manifest["created_at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        age = int((now - created).total_seconds() // 60)
        report.data["backup"] = {
            "manifest": manifest_name,
            "created_at_utc": manifest["created_at_utc"],
            "age_minutes": age,
            "key_id": manifest["key_id"],
            "ciphertext_bytes": manifest["ciphertext_bytes"],
            "server_version": manifest["server_version"],
        }
        if config.latest:
            report.check(
                "backup_fresh",
                age <= config.max_age_minutes,
                f"{age} min old, limit {config.max_age_minutes}",
            )
        if not report.check("key_matches_backup", backup_lib.key_id(key) == manifest["key_id"]):
            return
        sealed = folder / manifest["dump_object"]
        try:
            cipher_sha, cipher_bytes = backup_lib.sha256_file(sealed)
        except OSError:
            report.check("ciphertext_sha256", False, "dump object missing")
            return
        if not report.check(
            "ciphertext_sha256",
            cipher_sha == manifest["ciphertext_sha256"]
            and cipher_bytes == manifest["ciphertext_bytes"],
        ):
            return

    plain, key_file = run_dir / "dump.bin", run_dir / "key"
    with timed("decrypt"):
        backup_lib.write_private(key_file, key + "\n")
        try:
            opened = runner.run(
                backup_lib.decrypt_argv(
                    config.openssl, key_file, sealed, plain, manifest["iterations"]
                ),
                timeout=3600,
            )
        finally:
            key_file.unlink(missing_ok=True)
        if not report.check(
            "decrypt",
            opened.returncode == 0,
            "" if opened.returncode == 0 else backup_lib.tail(opened.stderr),
        ):
            return
        if not report.check(
            "plaintext_sha256", backup_lib.sha256_file(plain)[0] == manifest["plaintext_sha256"]
        ):
            return

    # 2. read the dump before any database exists
    with timed("inspect"):
        listing = pg_restore("--list", stdin=plain)
        try:
            if listing.returncode != 0:
                raise BackupError("dump_unreadable", backup_lib.tail(listing.stderr))
            toc = backup_lib.toc_summary(listing.stdout.decode("utf-8", "replace"))
            listed = toc.public_tables > 0
        except BackupError as error:
            report.check("dump_listable", False, error.code)
            return
        if not report.check("dump_listable", listed, f"{toc.public_tables} public tables"):
            return
        # `-f -` is required: pg_restore exits 1 without a database or an output file.
        schema = pg_restore("--schema-only", "-f", "-", stdin=plain)
        findings = public_grant_findings(schema.stdout.decode("utf-8", "replace"))
        report.check(
            "dump_grants",
            schema.returncode == 0 and not findings,
            f"{len(findings)} grants to PUBLIC"
            if findings
            else ("" if schema.returncode == 0 else backup_lib.tail(schema.stderr)),
        )

    # 3. the scratch server must not be production
    probe = psql(
        target.maintenance_db,
        f"SELECT count(*) FROM pg_database WHERE datname = '{backup_lib.DATABASE}'",
    )
    mia_databases = number(probe)
    if probe.returncode != 0 or mia_databases is None:
        raise DrillRefused("scratch_server_unreachable", backup_lib.tail(probe.stderr))
    if mia_databases != 0:
        raise DrillRefused(
            "scratch_server_has_mia",
            f"the scratch server has a database called {backup_lib.DATABASE}",
        )

    # 4. restore, check, drop
    created_db = psql(target.maintenance_db, f"CREATE DATABASE {scratch}")
    if not report.check(
        "scratch_created",
        created_db.returncode == 0,
        "" if created_db.returncode == 0 else backup_lib.tail(created_db.stderr),
    ):
        return
    report.data["scratch_database"] = scratch
    try:
        with timed("restore"):
            restored = pg_restore(
                "--no-owner", "--no-privileges", "--exit-on-error", database=scratch, stdin=plain
            )
            report.check(
                "restore",
                restored.returncode == 0,
                "" if restored.returncode == 0 else backup_lib.tail(restored.stderr),
            )
        if restored.returncode == 0:
            with timed("checks"):
                sanity_checks(report, psql, scratch, toc, config)
    finally:
        dropped = psql(target.maintenance_db, f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        report.data["scratch_dropped"] = dropped.returncode == 0
        report.check(
            "scratch_dropped",
            dropped.returncode == 0,
            "" if dropped.returncode == 0 else "drop the scratch database by hand",
        )


def sanity_checks(report: Report, psql, scratch: str, toc, config: DrillConfig) -> None:
    tables = number(
        psql(
            scratch,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
        )
    )
    report.check(
        "public_tables",
        tables is not None and tables > 0 and tables == toc.public_tables,
        f"restored {tables}, dump lists {toc.public_tables}",
    )

    migrations = psql(
        scratch, "SELECT count(*) || '|' || coalesce(max(filename), '') FROM schema_migrations"
    )
    count, _, last = migrations.stdout.decode("utf-8", "replace").strip().partition("|")
    report.check(
        "schema_migrations",
        migrations.returncode == 0 and count.isdigit() and int(count) > 0 and bool(last),
        last or "no migrations recorded",
    )

    granted = number(
        psql(
            scratch,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "LATERAL aclexplode(c.relacl) a WHERE n.nspname = 'public' "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'S') AND a.grantee = 0",
        )
    )
    report.check("relation_grants", granted == 0, f"{granted} PUBLIC grants on public relations")

    grants_text = None
    if config.ledger_grants_file is not None:
        try:
            grants_text = config.ledger_grants_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            grants_text = None
    outcome = ledger_posture.evaluate(
        lambda sql: psql(scratch, sql), grants_text=grants_text, create_roles=True
    )
    report.data["checks"].extend(outcome.checks)
    report.data["ledger"] = outcome.as_report()


# --- command line ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore the newest Mia backup into a scratch database and check it."
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        required=True,
        help="the offline copy of the backup key (64 hex characters)",
    )
    server = parser.add_mutually_exclusive_group(required=True)
    server.add_argument("--scratch-container", help="a local docker container running postgres:16")
    server.add_argument(
        "--scratch-url", help="postgresql://user[:password]@127.0.0.1:port/postgres"
    )
    parser.add_argument("--scratch-user", default="postgres", help="role for --scratch-container")
    parser.add_argument("--from-dir", type=Path, help="a folder holding a downloaded backup pair")
    parser.add_argument("--latest", action="store_true", help="download the newest backup")
    parser.add_argument("--bucket", default="", help="the backup bucket, with --latest")
    parser.add_argument(
        "--prefix",
        default=backup_lib.PREFIX_15MIN,
        choices=(backup_lib.PREFIX_15MIN, backup_lib.PREFIX_DAILY),
    )
    parser.add_argument("--project", default="", help="Google Cloud project, with --latest")
    parser.add_argument("--max-age-minutes", type=int, default=DEFAULT_MAX_AGE_MINUTES)
    parser.add_argument(
        "--ledger-grants-file",
        type=Path,
        help="the reviewed V2-15 grant set; required once schema assaf_os exists",
    )
    parser.add_argument(
        "--work-root", type=Path, default=Path(tempfile.gettempdir()) / "mia-restore-drill"
    )
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument("--gcloud", default="gcloud")
    parser.add_argument("--report", type=Path, help="also write the JSON report to this file")
    return parser


def main(argv=None, *, runner=None, clock=time.monotonic, now: datetime | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.from_dir) == bool(args.latest) or (args.latest and not args.bucket):
        parser.error("give --from-dir, or --latest with --bucket")
    runner = runner if runner is not None else backup_lib.Runner()
    try:
        target = build_target(
            container=args.scratch_container, url=args.scratch_url, user=args.scratch_user
        )
        config = DrillConfig(
            key_file=args.key_file,
            target=target,
            from_dir=args.from_dir,
            bucket=args.bucket,
            latest=args.latest,
            prefix=args.prefix,
            project=args.project,
            work_root=args.work_root,
            openssl=args.openssl,
            gcloud=args.gcloud,
            max_age_minutes=args.max_age_minutes,
            ledger_grants_file=args.ledger_grants_file,
        )
        report = run_drill(config, runner, clock=clock, now=now)
    except DrillRefused as error:
        print(f"restore-drill: refused code={error.code} detail={error.detail}", file=sys.stderr)
        return 3
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
