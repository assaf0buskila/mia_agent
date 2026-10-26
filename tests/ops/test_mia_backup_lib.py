"""V2-10 shared pieces: key rules, manifest, dump listing, the command runner, and shipped files."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import backup_lib
import pytest

from tests.ops import fakes
from tests.ops.fakes import KEY, OTHER_KEY, TOC

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "ops" / "gcp" / "backup"
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def error_code(call) -> str:
    with pytest.raises(backup_lib.BackupError) as raised:
        call()
    return raised.value.code


def test_a_key_is_exactly_64_lowercase_hex_characters_and_whitespace_is_stripped():
    assert backup_lib.validate_key(KEY) == KEY
    assert backup_lib.validate_key(f"  {KEY}\r\n") == KEY
    for bad in [
        "",
        "ab" * 31,
        "ab" * 33,
        "AB" * 32,
        "zz" * 32,
        f"{KEY} {KEY}",
        "ab" * 16 + "\n" * 2,
    ]:
        assert error_code(lambda bad=bad: backup_lib.validate_key(bad)) == "key_invalid"


def test_generated_keys_are_valid_and_distinct():
    first, second = backup_lib.generate_key(), backup_lib.generate_key()
    assert backup_lib.validate_key(first) == first and first != second


def test_the_key_fingerprint_identifies_a_key_without_revealing_it():
    ident = backup_lib.key_id(KEY)
    assert len(ident) == 16 and ident == backup_lib.key_id(KEY + "\n")
    assert ident != backup_lib.key_id(OTHER_KEY)
    assert ident not in KEY and KEY not in ident


def test_dump_listing_is_parsed_for_database_version_and_public_tables():
    toc = backup_lib.toc_summary(TOC)
    assert toc.entries == 7 and toc.dbname == "mia"
    assert toc.server_version == "16.4" and toc.pg_dump_version == "16.4"
    assert toc.tables == (
        ("public", "approvals"),
        ("public", "leads"),
        ("public", "schema_migrations"),
    )
    assert toc.public_tables == 3


def test_table_data_entries_are_not_counted_as_tables():
    toc = backup_lib.toc_summary(TOC)
    assert ("public", "DATA") not in toc.tables and len(toc.tables) == 3


@pytest.mark.parametrize("text", ["", "hello", ";\n; Selected TOC Entries:\n;\n"])
def test_a_listing_without_a_header_is_unreadable(text):
    assert error_code(lambda: backup_lib.toc_summary(text)) == "dump_unreadable"


def test_object_names_sort_by_time_and_round_trip():
    early = datetime(2026, 10, 15, 9, 5, 3, tzinfo=UTC)
    late = datetime(2026, 10, 15, 10, 20, 3, tzinfo=UTC)
    assert backup_lib.object_stem(early) == "mia-20261015T090503Z"
    assert backup_lib.object_stem(early) < backup_lib.object_stem(late)
    assert backup_lib.parse_stem_time("mia-20261015T090503Z") == early
    assert backup_lib.parse_stem_time("15min/mia-20261015T090503Z.manifest.json") == early
    assert error_code(lambda: backup_lib.parse_stem_time("nonsense")) == "manifest_invalid"


def manifest_fields() -> dict:
    return {
        "database": "mia",
        "created_at": datetime(2026, 10, 15, 12, 5, 3, tzinfo=UTC),
        "host": "mia",
        "dump_object": "mia-20261015T120503Z.dump.enc",
        "ciphertext_sha256": "a" * 64,
        "ciphertext_bytes": 10,
        "plaintext_sha256": "b" * 64,
        "plaintext_bytes": 8,
        "key": KEY,
        "toc": backup_lib.toc_summary(TOC),
    }


def test_manifest_round_trips_and_is_canonical():
    raw = backup_lib.dump_manifest(backup_lib.build_manifest(**manifest_fields()))
    assert raw.endswith(b"\n") and b"\r" not in raw
    document = backup_lib.load_manifest(raw)
    assert document["format"] == 1 and document["key_id"] == backup_lib.key_id(KEY)
    assert document["cipher"] == "aes-256-cbc" and document["toc_tables"] == 3
    assert KEY not in raw.decode()
    assert backup_lib.dump_manifest(document) == raw


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("ciphertext_sha256"),
        lambda d: d.update(format=2),
        lambda d: d.update(ciphertext_sha256="xyz"),
        lambda d: d.update(plaintext_sha256="A" * 64),
        lambda d: d.update(ciphertext_bytes=-1),
        lambda d: d.update(iterations="many"),
        lambda d: d.update(created_at_utc="yesterday"),
        lambda d: d.update(dump_object="../../etc/passwd"),
    ],
)
def test_a_manifest_that_is_not_ours_is_rejected(mutate):
    document = backup_lib.build_manifest(**manifest_fields())
    mutate(document)
    assert (
        error_code(lambda: backup_lib.load_manifest(json.dumps(document).encode()))
        == "manifest_invalid"
    )


@pytest.mark.parametrize("raw", [b"", b"[]", b"{not json", b"null"])
def test_garbage_is_not_a_manifest(raw):
    assert error_code(lambda: backup_lib.load_manifest(raw)) == "manifest_invalid"


def test_sha256_file_reports_digest_and_size(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"mia")
    assert backup_lib.sha256_file(path) == (fakes.sha256_bytes(b"mia"), 3)


@needs_openssl
def test_openssl_round_trip_and_wrong_key(tmp_path, monkeypatch):
    fakes.fast_kdf(monkeypatch)
    source = tmp_path / "plain.bin"
    source.write_bytes(fakes.PLAINTEXT)
    keys = {}
    for name, value in {"right": KEY, "wrong": OTHER_KEY}.items():
        keys[name] = tmp_path / f"{name}.key"
        keys[name].write_text(value + "\n", encoding="utf-8")
    runner = backup_lib.Runner()
    sealed = tmp_path / "plain.enc"
    assert (
        runner.run(backup_lib.encrypt_argv("openssl", keys["right"], source, sealed)).returncode
        == 0
    )
    assert sealed.read_bytes().startswith(b"Salted__")  # the format the runbook's one-liner reads
    opened = tmp_path / "opened.bin"
    done = runner.run(
        backup_lib.decrypt_argv(
            "openssl", keys["right"], sealed, opened, backup_lib.PBKDF2_ITERATIONS
        )
    )
    assert done.returncode == 0 and opened.read_bytes() == fakes.PLAINTEXT
    # The key is handed over as a file, never on a command line.
    assert KEY not in " ".join(backup_lib.encrypt_argv("openssl", keys["right"], source, sealed))
    wrong = tmp_path / "wrong.bin"
    bad = runner.run(
        backup_lib.decrypt_argv(
            "openssl", keys["wrong"], sealed, wrong, backup_lib.PBKDF2_ITERATIONS
        )
    )
    assert bad.returncode != 0 or wrong.read_bytes() != fakes.PLAINTEXT


def test_runner_captures_output_and_feeds_stdin_and_files(tmp_path):
    runner = backup_lib.Runner()
    echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
    done = runner.run(echo, input_bytes=b"mia")
    assert (done.returncode, done.stdout, done.stderr) == (0, b"MIA", b"")

    source, target = tmp_path / "in.txt", tmp_path / "out.txt"
    source.write_bytes(b"from a file")
    done = runner.run(echo, stdin_path=source, stdout_path=target)
    assert done.returncode == 0 and target.read_bytes() == b"FROM A FILE" and done.stdout == b""


def test_runner_reports_failure_instead_of_raising(tmp_path):
    runner = backup_lib.Runner()
    assert runner.run(["definitely-not-a-real-binary-xyz"]).returncode == 127
    failing = runner.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    )
    assert failing.returncode == 3 and failing.stderr == b"boom"
    slow = runner.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert slow.returncode == 124


@pytest.mark.parametrize(
    "available, executable, lookups, returncode",
    [
        (
            {"gcloud.cmd": "C:/Google Cloud SDK/bin/gcloud.cmd"},
            "C:/Google Cloud SDK/bin/gcloud.cmd",
            ["gcloud", "gcloud.cmd"],
            0,
        ),
        (
            {"gcloud": "/snap/bin/gcloud", "gcloud.cmd": "/other/gcloud.cmd"},
            "/snap/bin/gcloud",
            ["gcloud"],
            0,
        ),
        ({}, "gcloud", ["gcloud", "gcloud.cmd"], 127),
    ],
    ids=["windows-cmd", "linux-gcloud-preferred", "missing"],
)
def test_runner_resolves_gcloud_once_without_changing_arguments_or_stdin(
    monkeypatch, available, executable, lookups, returncode
):
    searched, calls = [], []

    def which(name):
        searched.append(name)
        return available.get(name)

    def run(argv, **options):
        calls.append((argv, options))
        if not available:
            raise FileNotFoundError
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    runner = backup_lib.Runner()
    argv = ["gcloud", "secrets", "versions", "add", "mia-backup-key", "--data-file=-"]
    for _ in range(2):
        done = runner.run(argv, input_bytes=KEY.encode(), timeout=60)
        assert done.returncode == returncode
        if returncode == 127:
            assert done == backup_lib.Result(127, b"", b"command or file not found: gcloud")
        else:
            assert done == backup_lib.Result(0, b"ok", b"")
    assert searched == lookups
    assert argv[0] == "gcloud"  # the caller's list is not mutated
    assert calls == [
        (
            [executable, *argv[1:]],
            {
                "stderr": subprocess.PIPE,
                "check": False,
                "timeout": 60,
                "input": KEY.encode(),
                "stdout": subprocess.PIPE,
            },
        )
    ] * 2


@pytest.mark.parametrize("executable", ["/custom/gcloud", "C:/SDK/gcloud.cmd", "docker"])
def test_runner_preserves_explicit_executables_and_other_tools(monkeypatch, executable):
    def unexpected_lookup(name):
        pytest.fail(f"unexpected executable lookup: {name}")

    def run(argv, **options):
        assert argv == [executable, "--version"]
        assert not options.get("shell", False)
        return subprocess.CompletedProcess(argv, 0, b"version", b"")

    monkeypatch.setattr(shutil, "which", unexpected_lookup)
    monkeypatch.setattr(subprocess, "run", run)
    assert backup_lib.Runner().run([executable, "--version"]).stdout == b"version"


def test_lifecycle_file_matches_the_owner_decision():
    rules = json.loads((BACKUP / "lifecycle.json").read_text(encoding="utf-8"))["rule"]
    found = {tuple(r["condition"]["matchesPrefix"]): r["condition"]["age"] for r in rules}
    assert found == {("15min/",): 7, ("daily/",): 90}
    assert all(r["action"] == {"type": "Delete"} for r in rules)
    assert backup_lib.RETENTION_DAYS == {"15min/": 7, "daily/": 90}
    assert backup_lib.PREFIX_15MIN == "15min/" and backup_lib.PREFIX_DAILY == "daily/"


def unit_lines(name: str) -> list[str]:
    text = (BACKUP / name).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_the_timer_fires_every_fifteen_minutes_off_the_other_jobs_and_catches_up():
    lines = unit_lines("mia-backup-offsite.timer")
    assert "OnCalendar=*:5/15" in lines  # :05 :20 :35 :50, clear of the :00/:15 due scan
    assert "Persistent=true" in lines
    assert "Unit=mia-backup-offsite.service" in lines
    assert "WantedBy=timers.target" in lines


def test_the_service_runs_the_installed_script_once_with_the_tools_it_needs():
    lines = unit_lines("mia-backup-offsite.service")
    assert "Type=oneshot" in lines
    assert "ExecStart=/usr/bin/python3 /opt/mia/backup/backup_offsite.py" in lines
    assert "EnvironmentFile=/etc/mia/backup-offsite.env" in lines
    assert "Requires=docker.service" in lines and "UMask=0077" in lines
    path = next(line for line in lines if line.startswith("Environment=PATH="))
    assert "/snap/bin" in path  # systemd's default PATH does not include the snap gcloud
    assert any(
        line.startswith("Environment=CLOUDSDK_CONFIG=/var/lib/mia/backup-offsite") for line in lines
    )
    assert not any("restore" in line for line in lines)


def test_everything_that_runs_on_the_vm_is_lf_only_and_stdlib_only():
    files = [p for p in BACKUP.iterdir() if p.is_file()]
    assert files
    for path in files:
        assert b"\r" not in path.read_bytes(), f"{path.name} has CRLF"
    local = {p.stem for p in BACKUP.glob("*.py")}
    for path in BACKUP.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                assert name in sys.stdlib_module_names or name in local, (
                    f"{path.name} imports {name}"
                )


def test_gitattributes_keeps_the_backup_files_lf():
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "ops/gcp/backup/* text eol=lf" in rules
