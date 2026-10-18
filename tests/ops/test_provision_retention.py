"""V2-31 (V2_PG_CONTRACTS.md section 22 E8): provisioning the os-retention credential.

The unit cases run everywhere. The database cases run when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is
set (the V2-16f rule: no case exists otherwise, so none is skipped), on the disposable database the
ledger fixture migrates. They prove: the password staged in the job-only env file is the one the
server holds as a SCRAM verifier; a retry rotates nothing; a database whose PUBLIC CONNECT is
revoked, an unsafe role or a failing catalog validator is refused before anything is staged; and
nothing the script prints or logs carries a DSN or a password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import os
import re
import stat
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.services.os_ledger.conftest import PG_URL_ENV, build_pg_env, pg_enabled

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "assaf-os" / "provision_retention.py"
SHA = "c" * 40
ROLE = "assaf_os_retention"
DSN_ENV = "MIA_OS_LEDGER_RETENTION_DATABASE_URL"


def load():
    spec = importlib.util.spec_from_file_location("provision_retention", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provision = load()


def target(tmp_path, **over):
    values = {
        "host": "127.0.0.1", "port": 5432, "database": "mia", "migration_role": "mia",
        "main_role": "mia_app", "retention_env": str(tmp_path / "os-retention.env"),
    }
    values.update(over)
    return provision.RetentionTarget(**values)


# ------------------------------------------------------------------------------- the plan


def test_the_dry_run_prints_the_plan_and_no_secret(capsys):
    provision.main(["--target", "mia-vm"])
    out = capsys.readouterr().out
    verifier = "<SCRAM-SHA-256 verifier computed by libpq>"
    assert f"ALTER ROLE assaf_os_retention PASSWORD '{verifier}';" in out
    assert "/etc/mia/os-retention.env" in out and DSN_ENV in out
    assert "PUBLIC must hold CONNECT" in out and "catalog validator runs first" in out
    assert not re.search(r"[0-9a-f]{64}", out) and "postgresql://" not in out


def test_only_the_pinned_vm_target_exists():
    assert list(provision._TARGETS) == ["mia-vm"]
    pin = provision._TARGETS["mia-vm"]
    assert (pin.host, pin.port, pin.database, pin.migration_role, pin.main_role) == (
        "127.0.0.1", 5432, "mia", "mia", "mia_app")
    assert pin.retention_env == "/etc/mia/os-retention.env"
    with pytest.raises(ValueError, match="^retention_target_unpinned$"):
        provision.plan("somewhere-else")
    with pytest.raises(SystemExit) as refused:
        provision.main(["--target", "somewhere-else"])
    assert refused.value.code == 2


@pytest.mark.parametrize("approved", [None, "d" * 40, "short"])
def test_apply_needs_the_exact_approved_sha(monkeypatch, capsys, approved):
    monkeypatch.setenv("MIA_BUILD_SHA", SHA)
    argv = ["--target", "mia-vm", "--apply", *(["--approved-sha", approved] if approved else [])]
    with pytest.raises(SystemExit) as refused:
        provision.main(argv)
    assert refused.value.code == 1
    assert capsys.readouterr().err == "retention_go_sha_required\n"


def test_apply_refuses_a_build_that_is_not_a_full_sha(monkeypatch, capsys):
    monkeypatch.setenv("MIA_BUILD_SHA", "main")
    with pytest.raises(SystemExit) as refused:
        provision.main(["--target", "mia-vm", "--apply", "--approved-sha", "main"])
    assert refused.value.code == 1
    assert capsys.readouterr().err == "retention_go_sha_required\n"


def test_a_failure_prints_one_fixed_code_never_a_driver_message(monkeypatch, capsys):
    monkeypatch.setenv("MIA_BUILD_SHA", SHA)

    def boom(_target):
        raise RuntimeError("connection to postgresql://assaf_os_retention:SECRET@host failed")

    monkeypatch.setattr(provision, "apply", boom)
    with pytest.raises(SystemExit) as refused:
        provision.main(["--target", "mia-vm", "--apply", "--approved-sha", SHA])
    assert refused.value.code == 1
    assert capsys.readouterr().err == "retention_provision_refused\n"
    monkeypatch.setattr(
        provision, "apply", lambda _t: (_ for _ in ()).throw(ValueError("postgresql://x:SECRET@h"))
    )
    with pytest.raises(SystemExit):
        provision.main(["--target", "mia-vm", "--apply", "--approved-sha", SHA])
    assert capsys.readouterr().err == "retention_provision_refused\n"


# ----------------------------------------------------------------------- the staged credential


def test_a_new_credential_is_one_line_in_a_private_file_and_a_retry_reuses_it(tmp_path):
    pin = target(tmp_path)
    assert provision.retention_credential(pin, create=False) is None
    assert not Path(pin.retention_env).exists(), "create=False never writes"
    password = provision.retention_credential(pin)
    assert re.fullmatch(r"[0-9a-f]{64}", password)
    content = Path(pin.retention_env).read_text(encoding="utf-8")
    assert content == f"{DSN_ENV}=postgresql://{ROLE}:{password}@127.0.0.1:5432/mia\n"
    assert "\r" not in content
    if os.name == "posix":
        assert stat.S_IMODE(Path(pin.retention_env).stat().st_mode) == 0o600
    assert provision.retention_credential(pin) == password
    assert provision.retention_credential(pin, create=False) == password
    assert Path(pin.retention_env).read_text(encoding="utf-8") == content, "a retry rotates nothing"


def test_the_credential_never_lands_in_the_runtime_file_or_the_ledger_key(tmp_path):
    pin = target(tmp_path)
    provision.retention_credential(pin)
    assert "MIA_OWNER_OS_LEDGER_DATABASE_URL" not in Path(pin.retention_env).read_text("utf-8")
    assert not (tmp_path / "runtime.env").exists()


@pytest.mark.parametrize("content", [
    "",
    "MIA_OS_LEDGER_RETENTION_DATABASE_URL=postgresql://assaf_os_retention:x@127.0.0.1:5432/mia\n",
    f"{DSN_ENV}=postgresql://assaf_os_ledger:{'a' * 64}@127.0.0.1:5432/mia\n",
    f"{DSN_ENV}=postgresql://{ROLE}:{'a' * 64}@10.0.0.1:5432/mia\n",
    f"{DSN_ENV}=postgresql://{ROLE}:{'a' * 64}@127.0.0.1:5432/other\n",
    f"{DSN_ENV}=postgresql://{ROLE}:{'A' * 64}@127.0.0.1:5432/mia\n",
    f"{DSN_ENV}=postgresql://{ROLE}:{'a' * 64}@127.0.0.1:5432/mia\nMIA_X=1\n",
    f"{DSN_ENV}=postgresql://{ROLE}:{'a' * 64}@127.0.0.1:5432/mia\n"
    f"{DSN_ENV}=postgresql://{ROLE}:{'b' * 64}@127.0.0.1:5432/mia\n",
    f"# note\n{DSN_ENV}=postgresql://{ROLE}:{'a' * 64}@127.0.0.1:5432/mia\n",
])
def test_a_staged_file_that_is_not_exactly_the_canonical_line_is_refused(tmp_path, content):
    pin = target(tmp_path)
    path = Path(pin.retention_env)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="^retention_env_invalid$"):
        provision.retention_credential(pin)
    with pytest.raises(ValueError, match="^retention_env_invalid$"):
        provision.retention_credential(pin, create=False)
    assert path.read_text(encoding="utf-8") == content, "a refused file is left exactly as found"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and links")
def test_a_group_readable_or_linked_file_is_refused(tmp_path):
    pin = target(tmp_path)
    password = provision.retention_credential(pin)
    Path(pin.retention_env).chmod(0o640)
    with pytest.raises(ValueError, match="^retention_env_invalid$"):
        provision.retention_credential(pin)
    Path(pin.retention_env).chmod(0o600)
    assert provision.retention_credential(pin) == password
    link = tmp_path / "linked.env"
    link.symlink_to(pin.retention_env)
    with pytest.raises(ValueError, match="^retention_env_invalid$"):
        provision.retention_credential(target(tmp_path, retention_env=str(link)))


# --------------------------------------------------------------------------------- database

if pg_enabled():

    def scram_matches(verifier: str, password: str) -> bool:
        match = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)", verifier)
        assert match, "the server holds a SCRAM-SHA-256 verifier"
        iterations, salt, stored, _server = match.groups()
        salted = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt), int(iterations)
        )
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        return base64.b64decode(stored) == hashlib.sha256(client_key).digest()

    @pytest.fixture
    def pg(tmp_path, monkeypatch):
        url = os.environ[PG_URL_ENV]
        build_pg_env(url)
        parsed = make_url(url)
        pin = target(
            tmp_path, port=parsed.port, database=parsed.database, migration_role="postgres",
            main_role="v216_main",
        )
        monkeypatch.setitem(provision._TARGETS, "local-test", pin)
        monkeypatch.setenv("MIA_BUILD_SHA", SHA)
        monkeypatch.setenv(
            "MIA_OS_LEDGER_MIGRATION_DATABASE_URL",
            f"postgresql+psycopg://postgres@127.0.0.1:{parsed.port}/{parsed.database}",
        )
        monkeypatch.setenv(
            "MIA_DATABASE_URL",
            f"postgresql+psycopg://v216_main@127.0.0.1:{parsed.port}/{parsed.database}",
        )
        admin = create_engine(url)
        with admin.begin() as c:
            c.execute(text(f"ALTER ROLE {ROLE} PASSWORD NULL"))
        yield pin, admin
        with admin.begin() as c:
            c.execute(text(f"ALTER ROLE {ROLE} PASSWORD NULL"))
            c.execute(text(f"GRANT CONNECT ON DATABASE {pin.database} TO PUBLIC"))
        admin.dispose()

    def verifier(admin) -> str | None:
        with admin.connect() as c:
            return c.scalar(text("SELECT rolpassword FROM pg_authid WHERE rolname = :r"),
                            {"r": ROLE})

    def run(capsys, argv=("--target", "local-test", "--apply", "--approved-sha", SHA)):
        try:
            provision.main(list(argv))
        except SystemExit as stop:
            captured = capsys.readouterr()
            return stop.code, captured.out, captured.err
        captured = capsys.readouterr()
        return 0, captured.out, captured.err

    def test_apply_stages_the_password_and_sets_the_matching_scram_verifier(pg, capsys):
        pin, admin = pg
        assert verifier(admin) is None, "the role has no password before"
        code, out, err = run(capsys)
        assert (code, err) == (0, ""), (out, err)
        password = provision.retention_credential(pin, create=False)
        assert re.fullmatch(r"[0-9a-f]{64}", password)
        held = verifier(admin)
        assert held.startswith("SCRAM-SHA-256$") and scram_matches(held, password)
        assert password not in out + err and "postgresql://" not in out + err
        assert "password set from a SCRAM verifier" in out

    def test_a_retry_rotates_nothing_and_still_matches(pg, capsys):
        pin, admin = pg
        assert run(capsys)[0] == 0
        before = Path(pin.retention_env).read_text(encoding="utf-8")
        first = verifier(admin)
        assert run(capsys)[0] == 0
        assert Path(pin.retention_env).read_text(encoding="utf-8") == before
        password = provision.retention_credential(pin, create=False)
        assert scram_matches(verifier(admin), password) and scram_matches(first, password)

    def test_removing_the_file_rotates_the_credential(pg, capsys):
        pin, admin = pg
        assert run(capsys)[0] == 0
        old = provision.retention_credential(pin, create=False)
        Path(pin.retention_env).unlink()
        assert run(capsys)[0] == 0
        new = provision.retention_credential(pin, create=False)
        assert new != old
        assert scram_matches(verifier(admin), new) and not scram_matches(verifier(admin), old)

    def test_the_provisioned_role_still_passes_the_catalog_validator(pg, capsys):
        from app.services.os_ledger import role_policy

        pin, admin = pg
        assert run(capsys)[0] == 0
        with admin.connect() as c:
            role_policy.validate_ledger_catalog(
                c, main_role="v216_main", migration_role="postgres"
            )

    def test_a_revoked_public_connect_is_refused_before_anything_is_staged(pg, capsys):
        pin, admin = pg
        with admin.begin() as c:
            c.execute(text(f"REVOKE CONNECT ON DATABASE {pin.database} FROM PUBLIC"))
        code, out, err = run(capsys)
        assert (code, err) == (1, "retention_connect_blocked\n")
        assert not Path(pin.retention_env).exists() and verifier(admin) is None

    def test_an_unsafe_role_is_refused_before_anything_is_staged(pg, capsys):
        pin, admin = pg
        with admin.begin() as c:
            c.execute(text(f"ALTER ROLE {ROLE} CREATEDB"))
        try:
            code, out, err = run(capsys)
        finally:
            with admin.begin() as c:
                c.execute(text(f"ALTER ROLE {ROLE} NOCREATEDB"))
        assert (code, err) == (1, "retention_role_unsafe\n")
        assert not Path(pin.retention_env).exists() and verifier(admin) is None

    def test_a_failing_catalog_validator_is_refused_before_anything_is_staged(pg, capsys):
        pin, admin = pg
        with admin.begin() as c:
            c.execute(text(f"GRANT SELECT ON assaf_os.os_events TO {ROLE}"))
        try:
            code, out, err = run(capsys)
        finally:
            with admin.begin() as c:
                c.execute(text(f"REVOKE SELECT ON assaf_os.os_events FROM {ROLE}"))
        assert (code, err) == (1, "retention_catalog_refused\n")
        assert not Path(pin.retention_env).exists() and verifier(admin) is None

    def test_a_main_dsn_for_the_wrong_role_is_an_unpinned_target(pg, monkeypatch, capsys):
        pin, admin = pg
        monkeypatch.setenv(
            "MIA_DATABASE_URL",
            f"postgresql+psycopg://postgres@127.0.0.1:{pin.port}/{pin.database}",
        )
        code, out, err = run(capsys)
        assert (code, err) == (1, "retention_target_unpinned\n")
        assert not Path(pin.retention_env).exists() and verifier(admin) is None

    def test_a_corrupt_staged_file_stops_the_run_and_is_left_alone(pg, capsys):
        pin, admin = pg
        Path(pin.retention_env).write_text("garbage\n", encoding="utf-8", newline="\n")
        Path(pin.retention_env).chmod(0o600)
        code, out, err = run(capsys)
        assert (code, err) == (1, "retention_env_invalid\n")
        assert Path(pin.retention_env).read_text(encoding="utf-8") == "garbage\n"
        assert verifier(admin) is None, "the transaction rolled back"

    def test_a_failure_after_staging_keeps_the_credential_for_the_retry(pg, monkeypatch, capsys):
        pin, admin = pg

        def fail(_target, _password):
            raise ValueError("retention_login_failed")

        monkeypatch.setattr(provision, "_verify_login", fail)
        code, out, err = run(capsys)
        assert (code, err) == (1, "retention_login_failed\n")
        staged = provision.retention_credential(pin, create=False)
        assert staged and scram_matches(verifier(admin), staged), "committed, then verified"
        monkeypatch.undo()  # the real login check, and the fixture's pins, are back
        assert run(capsys, ("--target", "mia-vm"))[0] == 0  # the dry run needs no database
