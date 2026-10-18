"""Fakes for the V2-10 backup tests. openssl is real; docker, gcloud, psql, pg_restore are not."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import backup_lib

KEY = "ab" * 32
OTHER_KEY = "cd" * 32
BUCKET = "mia-test-backups"
PLAINTEXT = b"PGDMP\x01\x0e\x00 synthetic mia dump: lead rows and approval rows\n" * 40

TOC = """;
; Archive created at 2026-10-15 12:05:03 UTC
;     dbname: mia
;     TOC Entries: 7
;     Compression: gzip
;     Dump Version: 1.15-0
;     Format: CUSTOM
;     Integer: 4 bytes
;     Offset: 8 bytes
;     Dumped from database version: 16.4 (Debian 16.4-1.pgdg120+1)
;     Dumped by pg_dump version: 16.4 (Debian 16.4-1.pgdg120+1)
;
;
; Selected TOC Entries:
;
3; 2615 2200 SCHEMA - public pg_database_owner
218; 1259 16385 TABLE public approvals mia
219; 1259 16390 TABLE public leads mia
220; 1259 16395 TABLE public schema_migrations mia
3401; 0 16385 TABLE DATA public approvals mia
3402; 0 16390 TABLE DATA public leads mia
3403; 0 16395 TABLE DATA public schema_migrations mia
"""

SCHEMA_SQL = """--
-- PostgreSQL database dump
--
CREATE TABLE public.leads (id integer NOT NULL);
REVOKE ALL ON TABLE public.leads FROM PUBLIC;
REVOKE ALL ON TABLE public.leads FROM mia;
GRANT ALL ON TABLE public.leads TO mia;
GRANT USAGE ON SCHEMA public TO PUBLIC;
"""


@dataclass
class Call:
    argv: list[str]
    stdin_path: Path | None = None
    stdout_path: Path | None = None
    input_bytes: bytes | None = None
    env: dict | None = None

    @property
    def text(self) -> str:
        return " ".join(self.argv)

    @property
    def tool(self) -> str:
        return Path(self.argv[0]).stem


class FakeRunner(backup_lib.Runner):
    """Records every command. openssl runs for real so encryption is genuinely exercised."""

    def __init__(self, handler=None, real=("openssl",)):
        self.calls: list[Call] = []
        self.handler = handler
        self.real = set(real)
        self._real = backup_lib.Runner()

    def run(
        self, argv, *, stdin_path=None, stdout_path=None, input_bytes=None, env=None, timeout=None
    ):
        argv = [str(part) for part in argv]
        self.calls.append(Call(argv, stdin_path, stdout_path, input_bytes, env))
        if Path(argv[0]).stem in self.real:
            return self._real.run(
                argv,
                stdin_path=stdin_path,
                stdout_path=stdout_path,
                input_bytes=input_bytes,
                env=env,
                timeout=timeout,
            )
        result = self.handler(self.calls[-1]) if self.handler else None
        return result if result is not None else backup_lib.Result(0, b"", b"")

    def commands(self, needle: str) -> list[Call]:
        return [call for call in self.calls if needle in call.text]


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


@dataclass
class FakeVm:
    """The VM's docker and gcloud, plus the bucket they write to."""

    secret_value: str = KEY
    secret_rc: int = 0
    dump_rc: int = 0
    dump_bytes: bytes = PLAINTEXT
    toc: str = TOC
    toc_rc: int = 0
    cp_fail_containing: tuple[str, ...] = ()
    describe_size_delta: int = 0
    describe_md5: str | None = None
    objects: dict[str, dict] = field(default_factory=dict)

    def handler(self, call: Call):
        text = call.text
        if "secrets versions access" in text:
            if self.secret_rc:
                return backup_lib.Result(self.secret_rc, b"", b"ERROR: PERMISSION_DENIED")
            return backup_lib.Result(0, self.secret_value.encode(), b"")
        if "pg_dump" in call.argv:
            if self.dump_rc:
                return backup_lib.Result(self.dump_rc, b"", b"pg_dump: error")
            call.stdout_path.write_bytes(self.dump_bytes)
            return backup_lib.Result(0, b"", b"")
        if "pg_restore" in call.argv and "--list" in call.argv:
            assert call.stdin_path is not None and call.stdin_path.exists()
            return backup_lib.Result(self.toc_rc, self.toc.encode(), b"")
        if call.argv[1:3] == ["storage", "cp"]:
            source, destination = call.argv[3], call.argv[4]
            if any(part in destination for part in self.cp_fail_containing):
                return backup_lib.Result(1, b"", b"ERROR: upload failed")
            metadata = {}
            for part in call.argv:
                if part.startswith("--custom-metadata="):
                    for pair in part.split("=", 1)[1].split(","):
                        key, _, value = pair.partition("=")
                        metadata[key] = value
            name = destination.removeprefix(f"gs://{BUCKET}/")
            self.objects[name] = {"data": Path(source).read_bytes(), "metadata": metadata}
            return backup_lib.Result(0, b"", b"")
        if call.argv[1:4] == ["storage", "objects", "describe"]:
            name = call.argv[4].removeprefix(f"gs://{BUCKET}/")
            if name not in self.objects:
                return backup_lib.Result(1, b"", b"ERROR: not found")
            stored = self.objects[name]
            document = {
                "name": name,
                "size": len(stored["data"]) + self.describe_size_delta,
                "md5_hash": self.describe_md5 or md5_b64(stored["data"]),
                "metadata": stored["metadata"],
            }
            return backup_lib.Result(0, json.dumps(document).encode(), b"")
        return None

    def names(self, prefix: str) -> list[str]:
        return sorted(name for name in self.objects if name.startswith(prefix))


def fast_kdf(monkeypatch, iterations: int = 1000) -> None:
    """The shipped iteration count is asserted separately; tests use a cheap one."""
    monkeypatch.setattr(backup_lib, "PBKDF2_ITERATIONS", iterations)


def public_table_count(toc: str) -> int:
    return len([t for t in backup_lib.toc_summary(toc).tables if t[0] == "public"])


@dataclass
class FakeScratch:
    """The scratch Postgres (psql and pg_restore) and, for --latest, the bucket listing."""

    dump: bytes = PLAINTEXT
    toc: str = TOC
    schema_sql: str = SCHEMA_SQL
    mia_database_present: bool = False
    public_tables: int | None = None
    migrations: str = "5|20261011_owner_reminders.sql"
    public_acl_entries: int = 0
    restore_rc: int = 0
    restore_stderr: bytes = b"pg_restore: error"
    fail_stderr: bytes = b"ERROR: simulated failure"  # what a failing psql statement prints
    ledger_present: bool = False
    ledger_posture: str = "0|0|0|0|0"
    fail_statements: tuple[str, ...] = ()
    sql: list[str] = field(default_factory=list)
    restored_into: list[str] = field(default_factory=list)
    restored_bytes: list[bytes] = field(default_factory=list)
    bucket_objects: dict[str, bytes] = field(default_factory=dict)

    def handler(self, call: Call):
        if call.argv[1:3] == ["storage", "ls"]:
            pattern = call.argv[3]
            stem = pattern.rsplit("*", 1)[0]
            urls = sorted(
                f"gs://{BUCKET}/{n}"
                for n in self.bucket_objects
                if f"gs://{BUCKET}/{n}".startswith(stem) and n.endswith(".manifest.json")
            )
            return backup_lib.Result(0, "\n".join(urls).encode(), b"")
        if call.argv[1:3] == ["storage", "cp"]:
            destination = Path(call.argv[-1])
            for url in call.argv[3:-1]:
                if url.startswith("--"):
                    continue
                name = url.removeprefix(f"gs://{BUCKET}/")
                (destination / name.rsplit("/", 1)[-1]).write_bytes(self.bucket_objects[name])
            return backup_lib.Result(0, b"", b"")
        tool = next((a for a in call.argv if Path(a).name in ("psql", "pg_restore")), None)
        if tool == "psql":
            statement = call.argv[call.argv.index("-c") + 1]
            self.sql.append(statement)
            if any(part in statement for part in self.fail_statements):
                return backup_lib.Result(1, b"", self.fail_stderr)
            return backup_lib.Result(0, self._answer(statement).encode(), b"")
        if tool == "pg_restore":
            if "--list" in call.argv:
                return backup_lib.Result(0, self.toc.encode(), b"")
            if "--schema-only" in call.argv:
                return backup_lib.Result(0, self.schema_sql.encode(), b"")
            self.restored_into.append(call.argv[call.argv.index("-d") + 1])
            self.restored_bytes.append(call.stdin_path.read_bytes())
            failure = self.restore_stderr if self.restore_rc else b""
            return backup_lib.Result(self.restore_rc, b"", failure)
        return None

    def _answer(self, statement: str) -> str:
        if "AS ledger_destructive" in statement:  # the assaf_os posture query
            return f"{self.ledger_posture}\n"
        if "nspname = 'assaf_os'" in statement and "count(*)" in statement:
            return "1\n" if self.ledger_present else "0\n"
        if "FROM pg_database" in statement:
            return "1\n" if self.mia_database_present else "0\n"
        if "information_schema.tables" in statement:
            if self.public_tables is not None:
                return f"{self.public_tables}\n"
            return f"{public_table_count(self.toc)}\n"
        if "schema_migrations" in statement:
            return f"{self.migrations}\n"
        if "aclexplode" in statement:
            return f"{self.public_acl_entries}\n"
        return ""

    def statements(self, needle: str) -> list[str]:
        return [s for s in self.sql if needle in s]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stems(runner: FakeRunner) -> set[str]:
    return {call.tool for call in runner.calls}


def no_secret_in(runner: FakeRunner, secret: str) -> bool:
    """True when the secret never appears in any command line or environment."""
    pattern = re.compile(re.escape(secret))
    for call in runner.calls:
        if pattern.search(call.text):
            return False
        if call.env and any(pattern.search(str(v)) for v in call.env.values()):
            return False
    return True
