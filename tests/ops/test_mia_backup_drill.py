"""V2-10 restore drill: download, verify, decrypt, restore into a scratch database, check, drop.

Safety is the point: the drill may only ever touch a database named mia_drill_<timestamp> on a
local scratch server that has no database called mia, and it never starts a service.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import backup_offsite
import ledger_posture
import pytest
import restore_drill

from tests.ops import fakes
from tests.ops.fakes import BUCKET, KEY, OTHER_KEY, PLAINTEXT, FakeRunner, FakeScratch, FakeVm

NOW = datetime(2026, 10, 15, 12, 5, 3, tzinfo=UTC)
DRILL_AT = NOW + timedelta(minutes=3)
SCRATCH = "mia_drill_20261015_120803"
STEM = "mia-20261015T120503Z"


class FakeClock:
    def __init__(self, step: float = 1.0):
        self.now, self.step = 1000.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    fakes.fast_kdf(monkeypatch)


@pytest.fixture
def pair(tmp_path):
    """A real backup pair, produced by the VM-side code, downloaded into a folder."""
    vm = FakeVm()
    environ = {"MIA_BACKUP_BUCKET": BUCKET, "MIA_BACKUP_WORK_DIR": str(tmp_path / "vm-work")}

    def make(now: datetime) -> None:
        code = backup_offsite.main([], runner=FakeRunner(vm.handler), environ=environ, now=now)
        assert code == 0

    make(NOW)
    folder = tmp_path / "download"
    folder.mkdir()
    for name, stored in vm.objects.items():
        (folder / Path(name).name).write_bytes(stored["data"])
    key_file = tmp_path / "offline-key.txt"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    return SimpleNamespace(dir=folder, key_file=key_file, vm=vm, make=make)


def drill(pair, tmp_path, scratch=None, *, target=None, clock=None, now=DRILL_AT, **overrides):
    scratch = scratch or FakeScratch()
    runner = FakeRunner(scratch.handler)
    options = {"from_dir": pair.dir, "work_root": tmp_path / "drill-work", **overrides}
    config = restore_drill.DrillConfig(
        key_file=options.pop("key_file", pair.key_file),
        target=target or restore_drill.build_target(container="drill-pg"),
        **options,
    )
    report = restore_drill.run_drill(config, runner, clock=clock or FakeClock(), now=now)
    return report, runner, scratch


def checks(report) -> dict[str, bool]:
    return {check["name"]: check["ok"] for check in report["checks"]}


def test_drill_restores_into_a_scratch_database_checks_it_and_drops_it(pair, tmp_path):
    report, runner, scratch = drill(pair, tmp_path)
    assert report["passed"] is True, report
    assert all(checks(report).values())
    assert {
        "key_matches_backup",
        "ciphertext_sha256",
        "plaintext_sha256",
        "dump_listable",
        "dump_grants",
        "restore",
        "public_tables",
        "schema_migrations",
        "relation_grants",
    } <= set(checks(report))

    assert report["scratch_database"] == SCRATCH and report["scratch_dropped"] is True
    assert scratch.restored_into == [SCRATCH]
    assert scratch.restored_bytes == [PLAINTEXT]  # the exact dump the VM took came back
    [restore] = runner.commands("pg_restore --no-owner")
    assert {"--no-owner", "--no-privileges", "--exit-on-error"} <= set(restore.argv)
    assert scratch.statements("CREATE DATABASE") == [f"CREATE DATABASE {SCRATCH}"]
    assert scratch.statements("DROP DATABASE") == [
        f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)"
    ]
    assert report["rto_seconds"] == 7200 and report["rto_met"] is True
    assert report["durations_seconds"]["total"] > 0 and "restore" in report["durations_seconds"]
    assert KEY not in json.dumps(report)
    # The decrypted production data does not outlive the drill.
    assert list((tmp_path / "drill-work").iterdir()) == []


@pytest.mark.parametrize(
    "name",
    [
        "mia",
        "production",
        "",
        "mia_drill",
        "mia_drill_x",
        "Mia_drill_20261015_120803",
        "mia_drill_20261015_120803 ",
        f"{SCRATCH}; DROP DATABASE mia",
        f'{SCRATCH}" OR "1',
        "other_mia_drill_20261015_120803",
    ],
)
def test_only_a_timestamped_drill_name_is_ever_accepted(name):
    with pytest.raises(restore_drill.DrillRefused):
        restore_drill.validate_scratch_name(name)


def test_the_valid_drill_name_is_accepted():
    assert restore_drill.validate_scratch_name(SCRATCH) == SCRATCH
    assert restore_drill.scratch_name(DRILL_AT) == SCRATCH


@pytest.mark.parametrize("bad", ["mia", "postgres", "mia_drill_"])
def test_the_running_drill_refuses_a_bad_name_before_touching_the_server(
    pair, tmp_path, monkeypatch, bad
):
    monkeypatch.setattr(restore_drill, "scratch_name", lambda now: bad)
    scratch = FakeScratch()
    with pytest.raises(restore_drill.DrillRefused):
        drill(pair, tmp_path, scratch)
    assert scratch.sql == [] and scratch.restored_into == []


def test_a_scratch_server_that_has_a_database_called_mia_is_refused(pair, tmp_path):
    scratch = FakeScratch(mia_database_present=True)
    with pytest.raises(restore_drill.DrillRefused) as raised:
        drill(pair, tmp_path, scratch)
    assert raised.value.code == "scratch_server_has_mia"
    assert scratch.statements("CREATE DATABASE") == [] and scratch.restored_into == []
    assert list((tmp_path / "drill-work").iterdir()) == []


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u@10.1.2.3:5432/postgres",
        "postgresql://u@mia.assafweb.com/postgres",
        "postgresql://u@34.165.183.186:5432/postgres",
        "postgresql://u@0.0.0.0/postgres",
        "not a url",
    ],
)
def test_a_scratch_server_must_be_on_this_machine(url):
    with pytest.raises(restore_drill.DrillRefused):
        restore_drill.build_target(url=url)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_scratch_servers_are_accepted(host):
    assert restore_drill.build_target(url=f"postgresql://postgres@{host}:55432/postgres")


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"container": "a", "url": "postgresql://u@127.0.0.1/postgres"},
        {"container": "x; rm -rf"},
        {"container": "--privileged"},
    ],
)
def test_exactly_one_well_formed_scratch_target_is_required(arguments):
    with pytest.raises(restore_drill.DrillRefused):
        restore_drill.build_target(**arguments)


def test_the_drill_never_starts_a_service_or_runs_outside_the_scratch_target(pair, tmp_path):
    _, runner, _ = drill(pair, tmp_path)
    for call in runner.calls:
        assert "compose" not in call.text and "systemctl" not in call.text
        assert call.argv[0] in ("openssl", "docker")
        if call.argv[0] == "docker":
            assert call.argv[1:4] == ["exec", "-i", "drill-pg"]

    url = restore_drill.build_target(url="postgresql://postgres:s3cret@127.0.0.1:55432/postgres")
    _, runner, _ = drill(pair, tmp_path, target=url)
    assert fakes.stems(runner) <= {"openssl", "psql", "pg_restore"}
    assert all("s3cret" not in call.text for call in runner.calls)  # password: environment only
    assert any(call.env and call.env.get("PGPASSWORD") == "s3cret" for call in runner.calls)
    psql = runner.commands("psql")[0]
    assert psql.argv[psql.argv.index("-h") + 1] == "127.0.0.1"
    assert psql.argv[psql.argv.index("-p") + 1] == "55432"


def test_a_tampered_ciphertext_stops_the_drill_before_anything_is_decrypted(pair, tmp_path):
    path = pair.dir / f"{STEM}.dump.enc"
    damaged = bytearray(path.read_bytes())
    damaged[40] ^= 0xFF
    path.write_bytes(bytes(damaged))
    report, runner, scratch = drill(pair, tmp_path)
    assert report["passed"] is False and checks(report)["ciphertext_sha256"] is False
    assert not runner.commands("enc -d") and scratch.sql == [] and scratch.restored_into == []
    assert report["scratch_database"] is None


def test_a_tampered_manifest_stops_the_drill(pair, tmp_path):
    path = pair.dir / f"{STEM}.manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["ciphertext_sha256"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")
    report, runner, scratch = drill(pair, tmp_path)
    assert report["passed"] is False and checks(report)["ciphertext_sha256"] is False
    assert not runner.commands("enc -d") and scratch.sql == []


def test_an_unreadable_manifest_is_a_failed_check_not_a_crash(pair, tmp_path):
    (pair.dir / f"{STEM}.manifest.json").write_text("{not json", encoding="utf-8")
    report, _, scratch = drill(pair, tmp_path)
    assert report["passed"] is False and checks(report)["manifest_valid"] is False
    assert scratch.sql == []


def test_the_wrong_key_is_caught_by_its_fingerprint_before_decrypting(pair, tmp_path):
    pair.key_file.write_text(OTHER_KEY + "\n", encoding="utf-8")
    report, runner, scratch = drill(pair, tmp_path)
    assert report["passed"] is False and checks(report)["key_matches_backup"] is False
    assert not runner.commands("enc -d") and scratch.sql == []


@pytest.mark.parametrize("text", ["", "hunter2", "ab" * 31])
def test_a_malformed_key_file_fails_before_anything_is_read(pair, tmp_path, text):
    pair.key_file.write_text(text, encoding="utf-8")
    report, runner, scratch = drill(pair, tmp_path)
    assert report["passed"] is False and checks(report)["key_valid"] is False
    assert len(report["checks"]) == 1 and runner.calls == [] and scratch.sql == []


def test_a_decrypted_dump_that_does_not_match_the_recorded_hash_is_not_restored(pair, tmp_path):
    path = pair.dir / f"{STEM}.manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["plaintext_sha256"] = "1" * 64
    path.write_text(json.dumps(document), encoding="utf-8")
    report, _, scratch = drill(pair, tmp_path)
    assert report["passed"] is False
    assert checks(report)["ciphertext_sha256"] and checks(report)["plaintext_sha256"] is False
    assert scratch.restored_into == [] and scratch.statements("CREATE DATABASE") == []


def test_a_dump_pg_restore_cannot_list_is_not_restored(pair, tmp_path):
    report, _, scratch = drill(pair, tmp_path, FakeScratch(toc=""))
    assert report["passed"] is False and checks(report)["dump_listable"] is False
    assert scratch.restored_into == []


def test_a_failed_restore_is_reported_and_the_scratch_database_is_still_dropped(pair, tmp_path):
    report, _, scratch = drill(pair, tmp_path, FakeScratch(restore_rc=1))
    assert report["passed"] is False and checks(report)["restore"] is False
    assert report["scratch_dropped"] is True
    assert scratch.statements("DROP DATABASE") == [
        f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)"
    ]


ROW_VALUE = "PRIVATE-ROW-VALUE"
LEAKY = (
    'pg_restore: error: COPY failed for table "leads": ERROR: duplicate key value\n'
    f"DETAIL:  Key (id)=({ROW_VALUE}) already exists."
)


@pytest.mark.parametrize(
    ("make_scratch", "check", "detail"),
    [
        (lambda: FakeScratch(restore_rc=3, restore_stderr=LEAKY.encode()), "restore",
         "pg_restore exit 3"),
        (lambda: FakeScratch(fail_statements=("CREATE DATABASE",), fail_stderr=LEAKY.encode()),
         "scratch_created", "psql exit 1"),
    ],
)
def test_the_report_holds_an_exit_code_never_the_text_a_tool_printed(
    pair, tmp_path, make_scratch, check, detail
):
    """V2-10 review r1: the README has the report recorded in HANDOFF.md, a committed file.

    A failed restore's constraint error carries a DETAIL line with the key's value, and a failed
    COPY prints the row. The report keeps the tool and its exit code and nothing else.
    """
    report, _, _ = drill(pair, tmp_path, make_scratch())
    entry = next(item for item in report["checks"] if item["name"] == check)
    assert entry["ok"] is False and entry["detail"] == detail
    assert ROW_VALUE not in json.dumps(report)


@pytest.mark.parametrize("module", [restore_drill, ledger_posture])
def test_no_report_entry_is_built_from_a_tools_stderr(module):
    """The two behaviour cases above name the paths they cover; this one covers all of them."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    entries = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("check", "add") or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id not in ("report", "outcome"):
            continue
        entries += 1
        used = {inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)}
        assert not used & {"tail", "stderr"}, f"{module.__name__}:{node.lineno}"
    assert entries > 5  # the walk found the report entries, so the assertion above looked at them


@pytest.mark.parametrize(
    ("fault", "failing"),
    [
        ({"public_tables": 1}, "public_tables"),
        ({"public_tables": 0}, "public_tables"),
        ({"migrations": "0|"}, "schema_migrations"),
        ({"migrations": "garbage"}, "schema_migrations"),
        ({"public_acl_entries": 2}, "relation_grants"),
    ],
)
def test_sanity_and_grant_queries_fail_the_drill(pair, tmp_path, fault, failing):
    report, _, scratch = drill(pair, tmp_path, FakeScratch(**fault))
    assert report["passed"] is False and checks(report)[failing] is False
    assert report["scratch_dropped"] is True


@pytest.mark.parametrize(
    "statement",
    [
        "GRANT SELECT ON TABLE public.leads TO PUBLIC;",
        "GRANT ALL ON SEQUENCE public.leads_id_seq TO PUBLIC;",
        "GRANT EXECUTE ON FUNCTION public.f() TO PUBLIC;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE mia IN SCHEMA public\n"
        "    GRANT SELECT ON TABLES TO PUBLIC;",
    ],
)
def test_a_dump_that_grants_anything_to_public_fails_the_posture_check(pair, tmp_path, statement):
    schema = fakes.SCHEMA_SQL + statement + "\n"
    report, _, scratch = drill(pair, tmp_path, FakeScratch(schema_sql=schema))
    assert report["passed"] is False and checks(report)["dump_grants"] is False
    assert checks(report)["restore"] is True  # the restore itself is still proven


def test_the_default_public_schema_usage_grant_is_not_a_finding(pair, tmp_path):
    assert "GRANT USAGE ON SCHEMA public TO PUBLIC" in fakes.SCHEMA_SQL
    report, _, _ = drill(pair, tmp_path)
    assert checks(report)["dump_grants"] is True


def test_a_restore_slower_than_the_two_hour_rto_fails_the_drill(pair, tmp_path):
    report, _, _ = drill(pair, tmp_path, clock=FakeClock(step=1000.0))
    assert report["rto_met"] is False and report["passed"] is False
    assert report["durations_seconds"]["total"] > 7200


def test_latest_picks_the_newest_backup_in_the_bucket_and_downloads_it(pair, tmp_path):
    pair.make(NOW + timedelta(minutes=15))
    newest = "15min/mia-20261015T122003Z"
    scratch = FakeScratch(bucket_objects={n: o["data"] for n, o in pair.vm.objects.items()})
    now = datetime(2026, 10, 15, 12, 23, 3, tzinfo=UTC)
    report, runner, _ = drill(
        pair, tmp_path, scratch, from_dir=None, bucket=BUCKET, latest=True, prefix="15min/", now=now
    )
    assert report["passed"] is True, report
    assert report["backup"]["manifest"] == f"{newest}.manifest.json"
    assert report["backup"]["age_minutes"] == 3 and checks(report)["backup_fresh"] is True
    [listing] = runner.commands("storage ls")
    assert listing.argv[3] == f"gs://{BUCKET}/15min/mia-*.manifest.json"
    [download] = runner.commands("storage cp")
    assert f"gs://{BUCKET}/{newest}.dump.enc" in download.argv
    assert f"gs://{BUCKET}/{newest}.manifest.json" in download.argv


def test_a_stale_latest_backup_fails_the_drill_even_though_it_restores(pair, tmp_path):
    scratch = FakeScratch(bucket_objects={n: o["data"] for n, o in pair.vm.objects.items()})
    later = NOW + timedelta(hours=3)
    report, _, _ = drill(
        pair,
        tmp_path,
        scratch,
        from_dir=None,
        bucket=BUCKET,
        latest=True,
        prefix="15min/",
        now=later,
    )
    assert checks(report)["backup_fresh"] is False and checks(report)["restore"] is True
    assert report["passed"] is False and report["backup"]["age_minutes"] == 180


def test_latest_with_an_empty_bucket_fails_cleanly(pair, tmp_path):
    report, _, scratch = drill(
        pair, tmp_path, FakeScratch(), from_dir=None, bucket=BUCKET, latest=True, prefix="15min/"
    )
    assert report["passed"] is False and checks(report)["locate_backup"] is False
    assert "restore" not in checks(report) and scratch.sql == []


def run_main(pair, tmp_path, scratch, *extra):
    argv = [
        "--key-file",
        str(pair.key_file),
        "--scratch-container",
        "drill-pg",
        "--from-dir",
        str(pair.dir),
        "--work-root",
        str(tmp_path / "work"),
        *extra,
    ]
    runner = FakeRunner(scratch.handler)
    return restore_drill.main(argv, runner=runner, clock=FakeClock(), now=DRILL_AT), runner


def test_main_prints_the_report_writes_it_and_exits_zero_on_a_pass(pair, tmp_path, capsys):
    report_path = tmp_path / "evidence" / "drill.json"
    code, _ = run_main(pair, tmp_path, FakeScratch(), "--report", str(report_path))
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["passed"] is True
    assert json.loads(report_path.read_text(encoding="utf-8")) == printed


def test_main_exits_one_on_a_failed_check_and_three_on_a_refusal(pair, tmp_path, capsys):
    code, _ = run_main(pair, tmp_path, FakeScratch(restore_rc=1))
    assert code == 1 and json.loads(capsys.readouterr().out)["passed"] is False

    code, runner = run_main(pair, tmp_path, FakeScratch(mia_database_present=True))
    captured = capsys.readouterr()
    assert code == 3 and "scratch_server_has_mia" in captured.err
    assert not runner.commands("CREATE DATABASE")


def test_main_requires_exactly_one_backup_source(pair, tmp_path):
    base = ["--key-file", str(pair.key_file), "--scratch-container", "drill-pg"]
    for argv in (
        base,
        [*base, "--from-dir", "a", "--latest", "--bucket", "b"],
        [*base, "--latest"],
    ):
        with pytest.raises(SystemExit) as raised:
            restore_drill.main(argv, runner=FakeRunner(), clock=FakeClock(), now=DRILL_AT)
        assert raised.value.code == 2


def test_the_drill_is_never_scheduled():
    root = Path(__file__).resolve().parents[2] / "ops" / "gcp" / "backup"
    for unit in list(root.glob("*.timer")) + list(root.glob("*.service")):
        assert "restore_drill" not in unit.read_text(encoding="utf-8")
        assert "restore-drill" not in unit.name


# --- the ledger schema: a restore must give assaf_os its owner and its grants back ----------------

LEDGER_GRANTS = (
    "-- the reviewed grant set\n"
    "GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger;\n"
    "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA assaf_os TO assaf_os_ledger;\n"
)


@pytest.fixture
def grants_file(tmp_path):
    path = tmp_path / "ledger-grants.sql"
    path.write_text(LEDGER_GRANTS, encoding="utf-8")
    return path


def ledger_calls(runner, needle: str):
    return [call for call in runner.calls if needle in call.text]


def test_a_dump_without_the_ledger_schema_needs_no_grant_file(pair, tmp_path):
    report, _, scratch = drill(pair, tmp_path)
    assert report["passed"] is True
    assert checks(report)["ledger_schema"] is True and "ledger_grants" not in checks(report)
    assert report["ledger"]["schema_present"] is False
    assert scratch.statements("ALTER SCHEMA") == [] and scratch.statements("CREATE ROLE") == []


def test_a_restored_ledger_schema_is_reowned_regranted_and_checked(pair, tmp_path, grants_file):
    report, runner, scratch = drill(
        pair, tmp_path, FakeScratch(ledger_present=True), ledger_grants_file=grants_file
    )
    assert report["passed"] is True, report
    ledger = [
        "ledger_schema",
        "ledger_grants",
        "ledger_roles",
        "ledger_reown_regrant",
        "ledger_posture",
    ]
    assert all(checks(report)[name] for name in ledger)
    assert report["ledger"]["schema_present"] is True
    assert set(report["ledger"]["violations"]) == {
        "schema_owner",
        "object_owner",
        "public_grants",
        "other_grantees",
        "ledger_destructive",
    }

    order = [
        next(i for i, s in enumerate(scratch.sql) if needle in s)
        for needle in (
            "CREATE ROLE",
            "ALTER SCHEMA assaf_os OWNER TO assaf_os_owner",
            "AS ledger_destructive",
        )
    ]
    assert order == sorted(order)
    [apply_sql] = scratch.statements("ALTER SCHEMA assaf_os OWNER TO assaf_os_owner")
    assert "REVOKE ALL ON ALL ROUTINES IN SCHEMA assaf_os FROM PUBLIC" in apply_sql
    assert "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA assaf_os TO assaf_os_ledger" in apply_sql
    # Every ledger statement ran in the scratch database, never in mia or the maintenance database.
    for needle in ("CREATE ROLE", "ALTER SCHEMA", "AS ledger_destructive", "nspname = 'assaf_os'"):
        found = ledger_calls(runner, needle)
        assert found, needle
        assert all(call.argv[call.argv.index("-d") + 1] == SCRATCH for call in found), needle


def test_a_restored_ledger_schema_without_a_grant_file_fails_the_drill(pair, tmp_path):
    report, _, scratch = drill(pair, tmp_path, FakeScratch(ledger_present=True))
    assert report["passed"] is False
    assert checks(report)["restore"] is True and checks(report)["ledger_grants"] is False
    assert scratch.statements("ALTER SCHEMA") == [] and scratch.statements("CREATE ROLE") == []
    assert report["scratch_dropped"] is True


def test_a_grant_file_with_anything_but_grants_is_refused_and_nothing_is_applied(
    pair, tmp_path, grants_file
):
    grants_file.write_text("DROP TABLE assaf_os.os_events;\n", encoding="utf-8")
    report, _, scratch = drill(
        pair, tmp_path, FakeScratch(ledger_present=True), ledger_grants_file=grants_file
    )
    assert report["passed"] is False and checks(report)["ledger_grants"] is False
    assert scratch.statements("ALTER SCHEMA") == [] and scratch.statements("DROP TABLE") == []


def test_a_missing_grant_file_is_a_failed_check_not_a_crash(pair, tmp_path):
    report, _, scratch = drill(
        pair, tmp_path, FakeScratch(ledger_present=True), ledger_grants_file=tmp_path / "nope.sql"
    )
    assert report["passed"] is False and checks(report)["ledger_grants"] is False
    assert scratch.statements("ALTER SCHEMA") == []


def test_a_posture_violation_after_the_regrant_fails_the_drill(pair, tmp_path, grants_file):
    scratch = FakeScratch(ledger_present=True, ledger_posture="0|0|2|0|0")
    report, _, _ = drill(pair, tmp_path, scratch, ledger_grants_file=grants_file)
    assert report["passed"] is False and checks(report)["ledger_posture"] is False
    assert report["ledger"]["violations"]["public_grants"] == 2
    assert report["scratch_dropped"] is True


def test_a_failed_reown_fails_the_drill_without_running_the_posture_check(
    pair, tmp_path, grants_file
):
    scratch = FakeScratch(ledger_present=True, fail_statements=("ALTER SCHEMA",))
    report, _, scratch = drill(pair, tmp_path, scratch, ledger_grants_file=grants_file)
    assert report["passed"] is False and checks(report)["ledger_reown_regrant"] is False
    assert "ledger_posture" not in checks(report) and report["scratch_dropped"] is True


def test_main_passes_the_grant_file_through(pair, tmp_path, grants_file, capsys):
    scratch = FakeScratch(ledger_present=True)
    code, _ = run_main(pair, tmp_path, scratch, "--ledger-grants-file", str(grants_file))
    assert code == 0 and json.loads(capsys.readouterr().out)["ledger"]["schema_present"] is True
    code, _ = run_main(pair, tmp_path, FakeScratch(ledger_present=True))
    assert code == 1 and json.loads(capsys.readouterr().out)["passed"] is False


def test_the_dump_schema_is_read_to_stdout_because_pg_restore_refuses_to_run_without_a_target(
    pair, tmp_path
):
    # Found against a real PostgreSQL 16: `pg_restore --schema-only` alone exits 1
    # ("one of -d/--dbname and -f/--file must be specified"), which no fake had reported.
    _, runner, _ = drill(pair, tmp_path)
    [schema] = runner.commands("pg_restore --schema-only")
    position = schema.argv.index("--schema-only")
    assert schema.argv[position + 1 : position + 3] == ["-f", "-"]
    assert "-d" not in schema.argv  # reading the script never touches a database


def test_a_scratch_server_that_cannot_be_asked_is_refused_for_what_it_is(pair, tmp_path):
    # Not "has a database called mia": the probe itself failed, so the server is unknown.
    scratch = FakeScratch(fail_statements=("FROM pg_database",))
    with pytest.raises(restore_drill.DrillRefused) as raised:
        drill(pair, tmp_path, scratch)
    assert raised.value.code == "scratch_server_unreachable"
    assert scratch.statements("CREATE DATABASE") == [] and scratch.restored_into == []
    assert list((tmp_path / "drill-work").iterdir()) == []
