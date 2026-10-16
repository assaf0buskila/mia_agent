"""V2-10 activation tooling: the provisioning script and the VM installer.

Both are owner-gated and are never run by these tests; every gcloud and systemctl call is a fake.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import backup_lib
import install_on_vm
import provision_backup
import pytest

from tests.ops import fakes
from tests.ops.fakes import KEY, FakeRunner

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "ops" / "gcp" / "backup"
PROJECT = "mia-assafweb"
BUCKET = "mia-assafweb-mia-backups"
SA = "serviceAccount:mia-vm@mia-assafweb.iam.gserviceaccount.com"


@dataclass
class FakeGcloud:
    bucket: bool = False
    secret: bool = False
    version: bool = False

    def handler(self, call):
        text = call.text
        if "storage buckets describe" in text:
            return backup_lib.Result(0 if self.bucket else 1, b"", b"")
        if "storage buckets create" in text:
            self.bucket = True
        elif "secrets describe" in text:
            return backup_lib.Result(0 if self.secret else 1, b"", b"")
        elif "secrets create" in text:
            self.secret = True
        elif "secrets versions list" in text:
            return backup_lib.Result(0, b"1\n" if self.version else b"", b"")
        elif "secrets versions add" in text:
            self.version = True
        return None


def run(arguments, gcloud=None):
    gcloud = gcloud or FakeGcloud()
    runner = FakeRunner(gcloud.handler)
    return provision_backup.main(arguments, runner=runner), runner, gcloud


def verbs(runner) -> list[str]:
    return [call.text for call in runner.calls]


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "offline-key.txt"
    path.write_text(KEY + "\n", encoding="utf-8")
    return path


def test_plan_is_the_default_and_makes_no_calls_at_all(capsys):
    code, runner, _ = run(["plan", "--project", PROJECT])
    assert code == 0 and runner.calls == []
    out = capsys.readouterr().out
    assert f"gcloud storage buckets create gs://{BUCKET}" in out
    assert "roles/storage.objectCreator" in out and "nothing below has been run" in out.lower()


def test_apply_builds_everything_in_a_safe_order_on_an_empty_project(key_file, capsys):
    code, runner, gcloud = run(["apply", "--project", PROJECT, "--key-file", str(key_file)])
    assert code == 0 and gcloud.bucket and gcloud.secret and gcloud.version
    commands = verbs(runner)
    first = {
        "services": next(i for i, c in enumerate(commands) if "services enable" in c),
        "bucket": next(i for i, c in enumerate(commands) if "storage buckets create" in c),
        "lifecycle": next(i for i, c in enumerate(commands) if "--lifecycle-file" in c),
        "iam": next(i for i, c in enumerate(commands) if "add-iam-policy-binding gs://" in c),
        "secret": next(i for i, c in enumerate(commands) if "secrets create" in c),
        "key": next(i for i, c in enumerate(commands) if "secrets versions add" in c),
    }
    assert list(first.values()) == sorted(first.values())
    assert KEY not in capsys.readouterr().out
    assert fakes.no_secret_in(runner, KEY)


def test_the_bucket_is_private_regional_and_uniformly_controlled(key_file):
    _, runner, _ = run(["apply", "--project", PROJECT])
    [create] = runner.commands("storage buckets create")
    assert create.argv[4] == f"gs://{BUCKET}"
    for flag in (
        "--location=me-west1",
        "--uniform-bucket-level-access",
        "--public-access-prevention",
        f"--project={PROJECT}",
    ):
        assert flag in create.argv


def test_lifecycle_uses_the_committed_file(key_file):
    _, runner, _ = run(["apply", "--project", PROJECT])
    [lifecycle] = runner.commands("--lifecycle-file")
    flag = next(a for a in lifecycle.argv if a.startswith("--lifecycle-file="))
    assert Path(flag.split("=", 1)[1]).resolve() == (BACKUP / "lifecycle.json").resolve()


def test_the_vm_can_add_and_read_backups_but_never_delete_or_overwrite_them():
    _, runner, _ = run(["apply", "--project", PROJECT])
    bindings = [
        next(a for a in c.argv if a.startswith("--role=")).split("=", 1)[1]
        for c in runner.commands("add-iam-policy-binding gs://")
    ]
    assert sorted(bindings) == ["roles/storage.objectCreator", "roles/storage.objectViewer"]
    for call in runner.commands("add-iam-policy-binding"):
        assert f"--member={SA}" in call.argv
        assert "allUsers" not in call.text and "allAuthenticatedUsers" not in call.text
    assert not any(
        role in " ".join(verbs(runner))
        for role in ("objectAdmin", "storage.admin", "legacyBucket", "roles/owner", "roles/editor")
    )


def test_the_key_secret_is_regional_and_readable_only_by_the_vm_account():
    _, runner, _ = run(["apply", "--project", PROJECT])
    [create] = runner.commands("secrets create")
    assert create.argv[3] == "mia-backup-key"
    assert (
        "--replication-policy=user-managed" in create.argv and "--locations=me-west1" in create.argv
    )
    [grant] = runner.commands("secrets add-iam-policy-binding")
    assert (
        f"--member={SA}" in grant.argv and "--role=roles/secretmanager.secretAccessor" in grant.argv
    )


def test_the_key_goes_in_through_stdin_only(key_file):
    _, runner, _ = run(["apply", "--project", PROJECT, "--key-file", str(key_file)])
    [add] = runner.commands("secrets versions add")
    assert "--data-file=-" in add.argv
    assert add.input_bytes.decode().strip() == KEY
    assert str(key_file) not in add.text and KEY not in add.text


def test_without_a_key_file_nothing_is_uploaded_and_the_next_step_is_printed(capsys):
    code, runner, gcloud = run(["apply", "--project", PROJECT])
    assert code == 0 and not runner.commands("secrets versions add") and gcloud.version is False
    assert "generate-key" in capsys.readouterr().out


def test_an_existing_key_version_is_never_replaced(key_file):
    gcloud = FakeGcloud(bucket=True, secret=True, version=True)
    code, runner, _ = run(["apply", "--project", PROJECT, "--key-file", str(key_file)], gcloud)
    assert code == 0 and not runner.commands("secrets versions add")


def test_a_second_run_creates_nothing(key_file):
    gcloud = FakeGcloud()
    run(["apply", "--project", PROJECT, "--key-file", str(key_file)], gcloud)
    code, runner, _ = run(["apply", "--project", PROJECT, "--key-file", str(key_file)], gcloud)
    assert code == 0
    for forbidden in ("buckets create", "secrets create", "versions add"):
        assert not runner.commands(forbidden), forbidden


def test_only_storage_secrets_and_services_are_ever_touched(key_file):
    _, runner, _ = run(["apply", "--project", PROJECT, "--key-file", str(key_file)])
    assert {call.argv[1] for call in runner.calls} <= {"storage", "secrets", "services"}
    assert all(call.argv[0] == "gcloud" for call in runner.calls)
    assert not any(word in " ".join(verbs(runner)) for word in ("compute", "ssh", "delete", "disk"))


@pytest.mark.parametrize(
    "arguments",
    [
        ["apply", "--project", "x"],
        ["apply", "--project", "Mia_Project"],
        ["apply", "--project", PROJECT, "--bucket", "UPPER"],
        ["apply", "--project", PROJECT, "--bucket", "gs://bucket"],
        ["apply", "--project", PROJECT, "--service-account", "someone@example.com"],
        ["apply", "--project", PROJECT, "--key-file", "does-not-exist.txt"],
    ],
)
def test_bad_inputs_are_rejected_before_any_gcloud_call(arguments):
    code, runner, _ = run(arguments)
    assert code == 2 and runner.calls == []


def test_a_malformed_key_file_is_rejected_before_any_gcloud_call(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("hunter2", encoding="utf-8")
    code, runner, _ = run(["apply", "--project", PROJECT, "--key-file", str(bad)])
    assert code == 2 and runner.calls == []


def test_a_failing_gcloud_step_stops_the_run(key_file):
    def broken(call):
        if "buckets describe" in call.text or "buckets create" in call.text:
            return backup_lib.Result(1, b"", b"ERROR: denied")
        return None

    runner = FakeRunner(broken)
    assert provision_backup.main(["apply", "--project", PROJECT], runner=runner) == 1
    assert not runner.commands("secrets create")


def test_generate_key_writes_a_valid_private_key_and_never_prints_it(tmp_path, capsys):
    target = tmp_path / "offline" / "mia-backup.key"
    assert provision_backup.main(["generate-key", "--out", str(target)]) == 0
    key = backup_lib.validate_key(target.read_text(encoding="utf-8"))
    out = capsys.readouterr()
    assert key not in out.out + out.err and backup_lib.key_id(key) in out.out
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o077 == 0


def test_generate_key_never_overwrites_an_existing_file(tmp_path, capsys):
    target = tmp_path / "mia-backup.key"
    target.write_text("precious", encoding="utf-8")
    assert provision_backup.main(["generate-key", "--out", str(target)]) == 2
    assert target.read_text(encoding="utf-8") == "precious"


# --- the VM installer -----------------------------------------------------------------------


@pytest.fixture
def vm(tmp_path):
    root = tmp_path / "vm"
    (root / "opt/mia").mkdir(parents=True)
    (root / "opt/mia/compose.yaml").write_text("name: mia\n", encoding="utf-8")
    (root / "usr/local/bin").mkdir(parents=True)
    (root / "usr/local/bin/mia").write_text("#!/bin/bash\n", encoding="utf-8")
    return root


def install(vm, *arguments, which=None, is_root=lambda: True):
    runner = FakeRunner()
    code = install_on_vm.main(
        ["--bucket", BUCKET, *arguments],
        runner=runner,
        root=vm,
        which=which or (lambda name: f"/usr/bin/{name}"),
        is_root=is_root,
    )
    return code, runner


def test_install_copies_scripts_units_and_config_into_place(vm):
    code, runner = install(vm, "--project", PROJECT)
    assert code == 0
    # ledger_posture.py rides along so the production restore path can re-own assaf_os.
    for name in ("backup_lib.py", "backup_offsite.py", "ledger_posture.py"):
        assert (vm / "opt/mia/backup" / name).read_bytes() == (BACKUP / name).read_bytes()
    for name in ("mia-backup-offsite.service", "mia-backup-offsite.timer"):
        assert (vm / "etc/systemd/system" / name).read_bytes() == (BACKUP / name).read_bytes()
    env = (vm / "etc/mia/backup-offsite.env").read_text(encoding="utf-8").splitlines()
    assert env == [f"MIA_BACKUP_BUCKET={BUCKET}", f"MIA_BACKUP_PROJECT={PROJECT}"]
    assert (vm / "var/lib/mia/backup-offsite/gcloud").is_dir()
    if sys.platform != "win32":
        assert (vm / "etc/mia/backup-offsite.env").stat().st_mode & 0o777 == 0o600
        assert (vm / "var/lib/mia/backup-offsite").stat().st_mode & 0o777 == 0o700


def test_the_restore_drill_is_not_installed_on_the_vm(vm):
    install(vm)
    installed = {p.name for p in (vm / "opt/mia/backup").iterdir()}
    assert "restore_drill.py" not in installed and "provision_backup.py" not in installed


def test_install_reloads_systemd_but_does_not_enable_or_start_anything_by_default(vm):
    code, runner = install(vm)
    assert code == 0
    assert [c.argv for c in runner.calls] == [["systemctl", "daemon-reload"]]


def test_the_timer_is_enabled_only_when_asked_and_the_service_is_never_started_directly(vm):
    code, runner = install(vm, "--enable-timer")
    assert code == 0
    assert ["systemctl", "enable", "--now", "mia-backup-offsite.timer"] in [
        c.argv for c in runner.calls
    ]
    assert not any("mia-backup-offsite.service" in c.text for c in runner.calls)
    assert not any(name in c.text for c in runner.calls for name in ("mia-due-scan", "mia-ingest"))


def test_install_leaves_the_deployed_mia_files_alone(vm):
    before = {p: p.read_bytes() for p in (vm / "usr/local/bin/mia", vm / "opt/mia/compose.yaml")}
    install(vm)
    assert {p: p.read_bytes() for p in before} == before


def test_install_without_the_tools_it_needs_writes_nothing(vm):
    code, runner = install(vm, which=lambda name: None if name == "gcloud" else f"/usr/bin/{name}")
    assert code == 1 and runner.calls == []
    assert not (vm / "opt/mia/backup").exists() and not (vm / "etc/mia/backup-offsite.env").exists()


def test_install_before_mia_is_deployed_writes_nothing(tmp_path):
    code, runner = install(tmp_path / "bare")
    assert code == 1 and runner.calls == []


def test_install_refuses_to_run_unprivileged(vm):
    code, runner = install(vm, is_root=lambda: False)
    assert code == 1 and runner.calls == [] and not (vm / "opt/mia/backup").exists()


@pytest.mark.parametrize("bucket", ["UPPER", "gs://x", "a b", ""])
def test_install_rejects_a_bad_bucket_name(vm, bucket):
    runner = FakeRunner()
    code = install_on_vm.main(
        ["--bucket", bucket], runner=runner, root=vm, which=lambda name: name, is_root=lambda: True
    )
    assert code == 2 and runner.calls == []


def test_installing_twice_is_harmless(vm):
    install(vm)
    first = (vm / "etc/mia/backup-offsite.env").read_bytes()
    code, _ = install(vm)
    assert code == 0 and (vm / "etc/mia/backup-offsite.env").read_bytes() == first
