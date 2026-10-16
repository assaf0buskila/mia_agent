"""Shared pieces of Mia's off-host database backup (V2-10).

Standard library only: the VM runs these with its system python3, outside the app image, and the
restore drill runs them from a checkout on Assaf's PC.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DATABASE = "mia"
SECRET_NAME = "mia-backup-key"
REGION = "me-west1"

# Object layout in the bucket. lifecycle.json mirrors these prefixes and ages.
PREFIX_15MIN = "15min/"
PREFIX_DAILY = "daily/"
RETENTION_DAYS = {PREFIX_15MIN: 7, PREFIX_DAILY: 90}
INTERVAL_MINUTES = 15
RTO_SECONDS = 2 * 60 * 60

DUMP_SUFFIX = ".dump.enc"
MANIFEST_SUFFIX = ".manifest.json"
MANIFEST_FORMAT = 1

# The documented recovery line is exactly:
#   openssl enc -d -aes-256-cbc -pbkdf2 -iter <iterations> -md sha256 -pass file:<key> ...
CIPHER = "aes-256-cbc"
KDF = "pbkdf2-hmac-sha256"
PBKDF2_ITERATIONS = 600_000

# A Cloud Storage bucket name as this tooling accepts it: no gs:// prefix, no path, no capitals.
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]")
# A Google Cloud project id: 6-30 characters, lowercase letters, digits and hyphens.
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")

_KEY = re.compile(r"[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"[0-9a-f]{16}")
_STEM = re.compile(r"mia-(\d{8}T\d{6}Z)")
_DUMP_OBJECT = re.compile(r"mia-\d{8}T\d{6}Z\.dump\.enc")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_STEM_FORMAT = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    """A failure with a stable reason code. The detail never contains a key or dump content."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --- key ------------------------------------------------------------------------------------


def validate_key(text: str) -> str:
    candidate = text.strip()
    if not _KEY.fullmatch(candidate):
        raise BackupError("key_invalid", "expected 64 lowercase hex characters")
    return candidate


def generate_key() -> str:
    return secrets.token_hex(32)


def key_id(key: str) -> str:
    """A short fingerprint that names a key without revealing it."""
    digest = hashlib.sha256(b"mia-backup-key-id:" + validate_key(key).encode("ascii"))
    return digest.hexdigest()[:16]


# --- files and names ------------------------------------------------------------------------


def sha256_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def object_stem(when: datetime) -> str:
    return "mia-" + utc(when).strftime(_STEM_FORMAT)


def parse_stem_time(name: str) -> datetime:
    found = _STEM.search(name)
    if not found:
        raise BackupError("manifest_invalid", "no backup timestamp in the name")
    return datetime.strptime(found.group(1), _STEM_FORMAT).replace(tzinfo=UTC)


def format_time(when: datetime) -> str:
    return utc(when).strftime(_TIME_FORMAT)


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read: keys and anything derived from them."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def tail(data: bytes, limit: int = 200) -> str:
    """The end of a tool's stderr, for a log line. Never call this on stdout."""
    return data.decode("utf-8", "replace").strip().replace("\n", " | ")[-limit:]


# --- the dump listing -----------------------------------------------------------------------

_TOC_ENTRIES = re.compile(r"^;\s+TOC Entries:\s*(\d+)\s*$", re.MULTILINE)
_TOC_DBNAME = re.compile(r"^;\s+dbname:\s*(.+?)\s*$", re.MULTILINE)
_TOC_SERVER = re.compile(r"^;\s+Dumped from database version:\s*(\S+)", re.MULTILINE)
_TOC_PGDUMP = re.compile(r"^;\s+Dumped by pg_dump version:\s*(\S+)", re.MULTILINE)
_TOC_TABLE = re.compile(r"^\d+; \d+ \d+ TABLE (\S+) (\S+) (\S+)$")


@dataclass(frozen=True)
class TocSummary:
    entries: int
    dbname: str
    server_version: str
    pg_dump_version: str
    tables: tuple[tuple[str, str], ...]

    @property
    def public_tables(self) -> int:
        return sum(1 for schema, _ in self.tables if schema == "public")


def toc_summary(text: str) -> TocSummary:
    """Read the header and table entries of `pg_restore --list`. A dump without them is unusable."""
    entries, dbname = _TOC_ENTRIES.search(text), _TOC_DBNAME.search(text)
    if not entries or not dbname:
        raise BackupError("dump_unreadable", "no archive header in the pg_restore listing")
    server, pg_dump = _TOC_SERVER.search(text), _TOC_PGDUMP.search(text)
    tables = []
    for line in text.splitlines():
        found = _TOC_TABLE.match(line.rstrip())
        if found:
            tables.append((found.group(1), found.group(2)))
    return TocSummary(
        entries=int(entries.group(1)),
        dbname=dbname.group(1),
        server_version=server.group(1) if server else "",
        pg_dump_version=pg_dump.group(1) if pg_dump else "",
        tables=tuple(tables),
    )


# --- manifest -------------------------------------------------------------------------------


def build_manifest(
    *,
    database: str,
    created_at: datetime,
    host: str,
    dump_object: str,
    ciphertext_sha256: str,
    ciphertext_bytes: int,
    plaintext_sha256: str,
    plaintext_bytes: int,
    key: str,
    toc: TocSummary,
) -> dict:
    return {
        "format": MANIFEST_FORMAT,
        "database": database,
        "created_at_utc": format_time(created_at),
        "host": host,
        "dump_object": dump_object,
        "ciphertext_sha256": ciphertext_sha256,
        "ciphertext_bytes": ciphertext_bytes,
        "plaintext_sha256": plaintext_sha256,
        "plaintext_bytes": plaintext_bytes,
        "cipher": CIPHER,
        "kdf": KDF,
        "iterations": PBKDF2_ITERATIONS,
        "key_id": key_id(key),
        "toc_entries": toc.entries,
        "toc_tables": toc.public_tables,
        "server_version": toc.server_version,
        "pg_dump_version": toc.pg_dump_version,
    }


def dump_manifest(document: dict) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _is_count(value: object, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def load_manifest(raw: bytes) -> dict:
    """Parse and validate a manifest. Anything that is not ours is `manifest_invalid`."""
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise BackupError("manifest_invalid", "not JSON") from error
    if not isinstance(document, dict):
        raise BackupError("manifest_invalid", "not an object")

    def fail(field: str) -> BackupError:
        return BackupError("manifest_invalid", f"bad or missing field {field}")

    if document.get("format") != MANIFEST_FORMAT:
        raise fail("format")
    for field in ("database", "host", "server_version", "pg_dump_version"):
        if not isinstance(document.get(field), str):
            raise fail(field)
    if not document["database"]:
        raise fail("database")
    for field in ("ciphertext_sha256", "plaintext_sha256"):
        if not isinstance(document.get(field), str) or not _SHA256.fullmatch(document[field]):
            raise fail(field)
    if not isinstance(document.get("key_id"), str) or not _KEY_ID.fullmatch(document["key_id"]):
        raise fail("key_id")
    for field in ("ciphertext_bytes", "plaintext_bytes", "toc_entries", "toc_tables"):
        if not _is_count(document.get(field)):
            raise fail(field)
    if not _is_count(document.get("iterations"), minimum=1):
        raise fail("iterations")
    if document.get("cipher") != CIPHER or document.get("kdf") != KDF:
        raise fail("cipher")
    if not isinstance(document.get("dump_object"), str) or not _DUMP_OBJECT.fullmatch(
        document["dump_object"]
    ):
        raise fail("dump_object")
    try:
        datetime.strptime(str(document.get("created_at_utc")), _TIME_FORMAT)
    except ValueError as error:
        raise fail("created_at_utc") from error
    return document


# --- openssl --------------------------------------------------------------------------------


def encrypt_argv(
    openssl: str, key_file: Path, source: Path, target: Path, iterations: int | None = None
) -> list[str]:
    """The key is read from a file by openssl; it never appears on a command line."""
    rounds = PBKDF2_ITERATIONS if iterations is None else iterations
    return [
        str(openssl),
        "enc",
        f"-{CIPHER}",
        "-salt",
        "-pbkdf2",
        "-iter",
        str(rounds),
        "-md",
        "sha256",
        "-pass",
        f"file:{key_file}",
        "-in",
        str(source),
        "-out",
        str(target),
    ]


def decrypt_argv(
    openssl: str, key_file: Path, source: Path, target: Path, iterations: int
) -> list[str]:
    return [
        str(openssl),
        "enc",
        "-d",
        f"-{CIPHER}",
        "-pbkdf2",
        "-iter",
        str(iterations),
        "-md",
        "sha256",
        "-pass",
        f"file:{key_file}",
        "-in",
        str(source),
        "-out",
        str(target),
    ]


# --- running commands -----------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class Runner:
    """Runs one external command. It reports failure as a Result instead of raising."""

    def run(
        self,
        argv: list[str],
        *,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        input_bytes: bytes | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Result:
        argv = [str(part) for part in argv]
        opened = []
        try:
            options: dict = {"stderr": subprocess.PIPE, "check": False, "timeout": timeout}
            if env:
                options["env"] = {**os.environ, **env}
            if input_bytes is not None:
                options["input"] = input_bytes
            elif stdin_path is not None:
                source = open(stdin_path, "rb")
                opened.append(source)
                options["stdin"] = source
            else:
                options["stdin"] = subprocess.DEVNULL
            if stdout_path is not None:
                descriptor = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                sink = os.fdopen(descriptor, "wb")
                opened.append(sink)
                options["stdout"] = sink
            else:
                options["stdout"] = subprocess.PIPE
            done = subprocess.run(argv, **options)
            return Result(done.returncode, done.stdout or b"", done.stderr or b"")
        except FileNotFoundError:
            return Result(127, b"", f"command or file not found: {argv[0]}".encode())
        except subprocess.TimeoutExpired:
            return Result(124, b"", b"timed out")
        except OSError as error:
            return Result(126, b"", str(error).encode())
        finally:
            for handle in opened:
                handle.close()
