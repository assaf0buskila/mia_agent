"""E21: close loopback `trust` in the VM's PostgreSQL (V2_PG_CONTRACTS.md section 22).

The official postgres image runs initdb with no --auth flags, so pg_hba.conf starts with
`host all all 127.0.0.1/32 trust` (and ::1) above the scram line the image appends. The database and
the app share the host network, so any process on the VM could be the superuser `mia` with no
password. Two fixes are proven here:

  * new clusters: POSTGRES_INITDB_ARGS in compose.yaml and bootstrap.sh;
  * the existing cluster: ops/gcp/vm/pg-hba-harden.sh with --check, --apply and --rollback.

Three layers. Static facts about the files and the awk program run everywhere. The behaviour proofs
use a DISPOSABLE CLUSTER: a throwaway `initdb` directory with its own port inside the opt-in test
container (the one the restore drill uses), reached through a fake `docker` that maps
`docker compose ... exec -T -u postgres postgres X` to `docker exec` into that cluster. They skip
unless MIA_RESTORE_DRILL_TEST_POSTGRES_URL is set, like the drill. The VM's real cluster is never
touched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
import zlib
from pathlib import Path

import pytest

from tests.unit.test_ci_os_ledger_postgres import ROOT, _bash

SCRIPT_PATH = ROOT / "ops/gcp/vm/pg-hba-harden.sh"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8")
COMPOSE = (ROOT / "ops/gcp/vm/compose.yaml").read_text(encoding="utf-8")
BOOTSTRAP = (ROOT / "ops/gcp/vm/bootstrap.sh").read_text(encoding="utf-8")
URL_VARIABLE = "MIA_RESTORE_DRILL_TEST_POSTGRES_URL"
CONTAINER = os.getenv("MIA_RESTORE_DRILL_TEST_PG_CONTAINER", "assaf-os-v2-10-restore-pg")
FIXED_ROLES = ("mia", "mia_app", "assaf_os_session", "assaf_os_ledger", "assaf_os_retention")

# What an `initdb` with no --auth flag writes, then the line the image appends after it.
DEFAULT_HBA = """\
# PostgreSQL Client Authentication Configuration File
# TYPE  DATABASE        USER            ADDRESS                 METHOD

# "local" is for Unix domain socket connections only
local   all             all                                     trust
# IPv4 local connections:
host    all             all             127.0.0.1/32            trust
# IPv6 local connections:
host    all             all             ::1/128                 trust
# Allow replication connections from localhost, by a user with the
# replication privilege.
local   replication     all                                     trust
host    replication     all             127.0.0.1/32            trust
host    replication     all             ::1/128                 trust

host all all all scram-sha-256
"""


def _posix(path: Path) -> str:
    return path.as_posix()


def _msys(path: Path) -> str:
    text = path.as_posix()
    return f"/{text[0].lower()}{text[2:]}" if re.match(r"^[A-Za-z]:/", text) else text


def awk_program() -> str:
    pattern = r"read -r -d '' awk_program <<'AWK' \|\| true\n(.*?)\nAWK\n"
    match = re.search(pattern, SCRIPT, re.DOTALL)
    assert match, "pg-hba-harden.sh has no awk program"
    return match.group(1)


def rewrite_script() -> str:
    pattern = r"read -r -d '' rewrite_script <<'SH' \|\| true\n(.*?)\nSH\n"
    match = re.search(pattern, SCRIPT, re.DOTALL)
    assert match, "pg-hba-harden.sh has no rewrite script"
    return match.group(1)


def run_awk(tmp_path: Path, text: str) -> str:
    source = tmp_path / "pg_hba.conf"
    source.write_text(text, encoding="utf-8", newline="\n")
    done = subprocess.run(
        [_bash(), "-c", 'awk "$0" "$1"', awk_program(), _posix(source)],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=60,
        env={**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"},
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


# --- static facts -----------------------------------------------------------------------------


def initdb_args_in_compose() -> str:
    match = re.search(r'^\s+POSTGRES_INITDB_ARGS:\s*"([^"]*)"\s*$', COMPOSE, re.MULTILINE)
    assert match, "compose.yaml sets no POSTGRES_INITDB_ARGS"
    return match.group(1)


def test_new_clusters_get_scram_on_every_tcp_line_from_initdb():
    """E21: compose.yaml and bootstrap.sh set the same POSTGRES_INITDB_ARGS, and nothing else."""
    value = initdb_args_in_compose()
    assert "--auth-host=scram-sha-256" in value.split()
    assert f"POSTGRES_INITDB_ARGS={value}" in BOOTSTRAP
    assert "trust" not in value
    # the postgres service's environment holds this one key, so the ownership extension stays narrow
    service = COMPOSE.split("\n  postgres:\n", 1)[1].split("\n  migrate:\n", 1)[0]
    environment = service.split("    environment:\n", 1)[1].split("\n    volumes:", 1)[0]
    keys = re.findall(r"^\s{6}([A-Z_]+):", environment, re.MULTILINE)
    assert keys == ["POSTGRES_INITDB_ARGS"]
    # bootstrap's postgres.env gains that key and no other
    env_line = re.search(r"printf '(POSTGRES_DB=mia.*?)\\n' \"\$db_password\"", BOOTSTRAP)
    assert env_line, "bootstrap.sh no longer writes postgres.env in the expected form"
    assert re.findall(r"^([A-Z_]+)=", env_line.group(1).replace("\\n", "\n"), re.MULTILINE) == [
        "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_INITDB_ARGS",
    ]


def test_the_unix_socket_stays_trust_on_new_clusters_because_every_vm_tool_uses_it():
    """Deliberate deviation from E21's literal text: no --auth-local.

    `mia backup|restore`, app_role_demote.py, backup_offsite.py, provision_auth.py and the restore
    drill reach PostgreSQL as `mia` through `docker compose exec ... psql` over the container's own
    unix socket, with no password. `--auth-local=scram-sha-256` would break every one of them,
    including the restore a new VM exists to run. The socket is not reachable from the host network.
    """
    assert "--auth-local" not in initdb_args_in_compose()
    for path in (
        ROOT / "ops/gcp/vm/mia",
        ROOT / "ops/gcp/vm/app_role_demote.py",
        ROOT / "ops/gcp/backup/backup_offsite.py",
    ):
        text = path.read_text(encoding="utf-8")
        assert re.search(r'"-U",\s*"mia"|-U mia|"-U",\s*config\.db_user', text), path.name
        assert re.search(r"""exec["',\s]*(-T["',\s]*)?postgres""", text), path.name


def test_the_script_is_lf_syntactically_valid_and_takes_exactly_one_mode():
    assert b"\r" not in SCRIPT_PATH.read_bytes()
    done = subprocess.run([_bash(), "-n", _posix(SCRIPT_PATH)], capture_output=True, text=True,
                          check=False, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "--check | --apply | --rollback" in SCRIPT
    assert re.search(r"\[ \"\$#\" -eq 1 \] \|\| usage", SCRIPT)
    assert SCRIPT.count("--check)") + SCRIPT.count("--apply)") + SCRIPT.count("--rollback)") >= 3


def test_the_script_checks_the_roles_the_contract_names_before_it_changes_anything():
    match = re.search(r"fixed_roles=\(([^)]*)\)", SCRIPT)
    assert match and tuple(match.group(1).split()) == FIXED_ROLES
    # the refusal comes before the backup and the rewrite in the apply arm
    apply_arm = SCRIPT.split("\n  --apply)\n", 1)[1]
    assert apply_arm.index("hba_role_without_scram") < apply_arm.index("pre-e21-")
    assert apply_arm.index("hba_dsn_without_password") < apply_arm.index("pre-e21-")
    assert apply_arm.index("hba_dsn_unverified") < apply_arm.index("pre-e21-")
    assert "SCRAM-SHA-256" in SCRIPT and "rolcanlogin" in SCRIPT


def test_nothing_secret_is_ever_an_argument_or_printed():
    code = "\n".join(line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#"))
    assert code.startswith("set +x") or "\nset +x\n" in code
    assert "PGPASSWORD=" not in code
    assert "printf '%s\\n' \"$password\" | pg sh -c" in code  # stdin only
    assert 'IFS= read -r PGPASSWORD; export PGPASSWORD' in code
    for variable in ("password", "dsn_passwords"):
        for line in code.splitlines():
            if f"${variable}" in line or f"${{{variable}[" in line:
                assert "echo" not in line, line
    # nothing prints the contents of pg_hba.conf or a DSN
    assert "cat \"$hba_file\"" not in code and "echo \"$value\"" not in code


def test_apply_and_rollback_take_the_shared_state_lock_but_check_does_not():
    assert 'if [ "$mode" != --check ]; then\n  exec 9>"$state_lock"\n  flock -n 9' in SCRIPT
    assert 'state_lock="${MIA_HBA_STATE_LOCK:-/var/lock/mia-state.lock}"' in SCRIPT
    assert "hba_operation_busy" in SCRIPT


def test_the_script_never_deploys_restarts_or_fetches_anything():
    code = "\n".join(line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#"))
    for word in ("docker compose up", "restart", "compose stop", "gcloud", "curl", "systemctl",
                 "mia deploy", "fetch_settings", "DROP ", "pg_dump"):
        assert word not in code, word


def test_the_reason_codes_are_documented_in_the_runbook():
    runbook = (ROOT / "docs/runbooks/APP_ROLE_DEMOTE.md").read_text(encoding="utf-8")
    for code in sorted(set(re.findall(r"refuse (hba_[a-z_]+)", SCRIPT))):
        assert f"`{code}`" in runbook, code


def test_the_awk_program_and_the_rewrite_script_have_no_backslash_to_mangle_on_the_way_in():
    """They travel as arguments through `docker compose exec`: no escape may depend on a shell."""
    assert "\\" not in awk_program()
    assert "\\" not in rewrite_script()


# --- the awk program on sample files -------------------------------------------------------------


def test_every_tcp_trust_line_becomes_scram_and_nothing_else_changes(tmp_path):
    out = run_awk(tmp_path, DEFAULT_HBA)
    before, after = DEFAULT_HBA.splitlines(), out.splitlines()
    assert len(before) == len(after)
    changed = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
    assert len(changed) == 4  # host all x2, host replication x2
    for old, new in changed:
        assert old.startswith("host ") and old.rstrip().endswith("trust")
        # same columns, same layout: only the method word differs
        assert new == old[: old.rindex("trust")] + "scram-sha-256"
    # the unix socket lines, the comments and the image's own scram line are untouched
    assert "local   all             all                                     trust" in after
    assert "local   replication     all                                     trust" in after
    assert "host all all all scram-sha-256" in after
    assert all("trust" not in line for line in after if line.startswith("host"))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("host all all 127.0.0.1/32 trust", "host all all 127.0.0.1/32 scram-sha-256"),
        ("host    all   all   ::1/128    trust   # loopback",
         "host    all   all   ::1/128    scram-sha-256   # loopback"),
        ("hostssl all all 10.0.0.0/8 trust", "hostssl all all 10.0.0.0/8 scram-sha-256"),
        ("hostnossl all all all trust", "hostnossl all all all scram-sha-256"),
        ("host all all 127.0.0.1 255.255.255.255 trust",
         "host all all 127.0.0.1 255.255.255.255 scram-sha-256"),
        ("host all all ::1 ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff trust",
         "host all all ::1 ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff scram-sha-256"),
        ("host all all samehost trust", "host all all samehost scram-sha-256"),
        ("  host all all localhost trust  ", "  host all all localhost scram-sha-256  "),
        ("host all all all trust map=x", "host all all all scram-sha-256 map=x"),
        # untouched: other methods, the unix socket, comments, and `trust` as a name not a method
        ("host all all 127.0.0.1/32 md5", "host all all 127.0.0.1/32 md5"),
        ("host all all 127.0.0.1/32 scram-sha-256", "host all all 127.0.0.1/32 scram-sha-256"),
        ("host all all 127.0.0.1/32 reject", "host all all 127.0.0.1/32 reject"),
        ("host all all 127.0.0.1/32 peer", "host all all 127.0.0.1/32 peer"),
        ("local all all trust", "local all all trust"),
        ("local all all peer", "local all all peer"),
        ("# host all all 127.0.0.1/32 trust", "# host all all 127.0.0.1/32 trust"),
        ("host trust trust 127.0.0.1/32 md5", "host trust trust 127.0.0.1/32 md5"),
        ("host all all 127.0.0.1/32 md5 # was trust", "host all all 127.0.0.1/32 md5 # was trust"),
        ("", ""),
    ],
)
def test_the_awk_program_handles_each_line_form(tmp_path, line, expected):
    assert run_awk(tmp_path, line + "\n") == expected + "\n"


def test_the_awk_program_is_idempotent(tmp_path):
    once = run_awk(tmp_path, DEFAULT_HBA)
    assert run_awk(tmp_path, once) == once


# --- the disposable cluster ---------------------------------------------------------------------


class Cluster:
    """A throwaway `initdb` directory inside the opt-in test container, on its own port."""

    def __init__(self, tmp_path: Path, initdb_args: str = ""):
        suffix = uuid.uuid4().hex[:10]
        self.dir = f"/tmp/hbat_{suffix}"
        self.sock = f"{self.dir}/sock"
        self.hba = f"{self.dir}/data/pg_hba.conf"
        self.port = 55300 + zlib.crc32(suffix.encode()) % 600
        self.password = "mia" + uuid.uuid4().hex  # the superuser password, as the DSN holds it
        self.app_password = "app" + uuid.uuid4().hex
        self.tmp = tmp_path
        self.etc = tmp_path / "etc"
        self.etc.mkdir()
        self.real_docker = shutil.which("docker")
        assert self.real_docker, "docker is required for the disposable cluster tests"
        self.arglog = tmp_path / "docker-calls.log"
        self.arglog.write_text("")
        self.initdb_args = initdb_args
        self._write_shims()

    # -- plumbing

    def docker(self, *args: str, user: str = "postgres", env: dict | None = None,
               stdin: str | None = None, check: bool = True):
        flags = [item for key, value in (env or {}).items() for item in ("-e", f"{key}={value}")]
        done = subprocess.run(
            ["docker", "exec", "-i", "-u", user, *flags, CONTAINER, *args],
            input=stdin, capture_output=True, text=True, encoding="utf-8", check=False, timeout=120,
        )
        if check and done.returncode:
            raise AssertionError(f"docker exec {args[:3]} failed: {done.stderr}")
        return done

    def psql(self, sql: str, database: str = "postgres", check: bool = True):
        return self.docker(
            "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "mia", "-d", database, "-c", sql,
            env={"PGHOST": self.sock, "PGPORT": str(self.port)}, check=check,
        )

    def scalar(self, sql: str, database: str = "postgres") -> str:
        return self.psql(sql, database).stdout.strip()

    def tcp(self, role: str, password: str, database: str = "mia") -> bool:
        """Can this role log in over 127.0.0.1 with this password?"""
        done = self.docker(
            "psql", "-X", "-qAt", "-h", "127.0.0.1", "-p", str(self.port), "-U", role, "-d",
            database, "-c", "SELECT 1", env={"PGPASSWORD": password}, check=False,
        )
        return done.returncode == 0 and done.stdout.strip() == "1"

    def socket_admin(self) -> bool:
        """The path every VM tool uses: the unix socket, no password."""
        done = self.docker(
            "psql", "-X", "-qAt", "-U", "mia", "-d", "postgres", "-c", "SELECT 1",
            env={"PGHOST": self.sock, "PGPORT": str(self.port)}, check=False,
        )
        return done.returncode == 0 and done.stdout.strip() == "1"

    def hba_text(self) -> str:
        return self.docker("cat", self.hba).stdout.replace("\r\n", "\n")

    def backups(self) -> list[str]:
        done = self.docker("sh", "-c", f"ls -1 {self.hba}.pre-e21-* 2>/dev/null || true")
        return [line for line in done.stdout.splitlines() if line.strip()]

    def backup_text(self, path: str) -> str:
        return self.docker("cat", path).stdout.replace("\r\n", "\n")

    def start(self) -> None:
        self.docker(
            "sh", "-c",
            "set -e; rm -rf \"$1\"; mkdir -p \"$1/sock\"; printf '%s' \"$2\" > \"$1/pw\"; "
            "initdb -D \"$1/data\" -U mia --pwfile=\"$1/pw\" $4 >/dev/null 2>&1; "
            "printf 'host all all all scram-sha-256\\n' >> \"$1/data/pg_hba.conf\"; "
            "pg_ctl -D \"$1/data\" -o \"-p $3 -c listen_addresses=127.0.0.1 "
            "-c unix_socket_directories=$1/sock\" -w -s -l \"$1/log\" start",
            "sh", self.dir, self.password, str(self.port), self.initdb_args,
        )
        self.psql("CREATE DATABASE mia")
        self.psql(f"CREATE ROLE mia_app LOGIN PASSWORD '{self.app_password}'")

    def stop(self) -> None:
        self.docker("sh", "-c",
                    f"pg_ctl -D {self.dir}/data -m immediate -w -s stop >/dev/null 2>&1; "
                    f"rm -rf {self.dir}", check=False)

    # -- the VM's files and the script

    def write_env(self, name: str, text: str) -> None:
        (self.etc / name).write_text(text, encoding="utf-8", newline="\n")

    def write_dsns(self, *, mia_password: str | None = None) -> None:
        password = self.password if mia_password is None else mia_password
        self.write_env(
            "runtime.env",
            f"MIA_DATABASE_URL=postgresql://mia:{password}@127.0.0.1:{self.port}/mia\n",
        )

    def _write_shims(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$HBA_ARGLOG"
[ "$1" = compose ] || exec "$HBA_REAL_DOCKER" "$@"
shift
while [ "$#" -gt 0 ]; do case "$1" in -p|-f|--env-file) shift 2 ;; *) break ;; esac; done
[ "$1" = exec ] || exit 97
shift
[ "$1" = -T ] && shift
user=root
[ "$1" = -u ] && { user="$2"; shift 2; }
[ "$1" = postgres ] || exit 98
shift
exec "$HBA_REAL_DOCKER" exec -i -u "$user" -e PGHOST="$HBA_SOCK" -e PGPORT="$HBA_PORT" \
  "$HBA_CONTAINER" "$@"
""", encoding="utf-8", newline="\n")
        if shutil.which("flock") is None:  # Git Bash has none; the VM's util-linux does
            (bin_dir / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8",
                                           newline="\n")
        self.bin_dir = bin_dir

    def run(self, mode: str, *, euid: str = "0", lock: Path | None = None, port: int | None = None,
            etc: Path | None = None):
        environment = {
            **os.environ,
            "MSYS_NO_PATHCONV": "1",
            "MSYS2_ARG_CONV_EXCL": "*",
            "HBA_BIN": _msys(self.bin_dir),
            "HBA_REAL_DOCKER": _posix(Path(self.real_docker)),
            "HBA_ARGLOG": _posix(self.arglog),
            "HBA_SOCK": self.sock,
            "HBA_PORT": str(port or self.port),
            "HBA_CONTAINER": CONTAINER,
            "MIA_HBA_EUID": euid,
            "MIA_HBA_ETC_DIR": _posix(etc or self.etc),
            "MIA_HBA_STATE_LOCK": _posix(lock or (self.tmp / "mia-state.lock")),
            "MIA_HBA_COMPOSE_FILE": "/opt/mia/compose.yaml",
            "MIA_HBA_TAG_ENV": "/opt/mia/tag.env",
        }
        done = subprocess.run(
            [_bash(), "-c", 'PATH="$HBA_BIN:$PATH"; exec bash "$0" "$@"',
             _posix(SCRIPT_PATH), mode],
            env=environment, capture_output=True, text=True, encoding="utf-8", check=False,
            timeout=300,
        )
        values = dict(
            line.split("=", 1) for line in done.stdout.splitlines() if "=" in line and " " not in
            line.split("=", 1)[0]
        )
        return done, values


@pytest.fixture
def cluster(tmp_path):
    if not os.getenv(URL_VARIABLE):
        pytest.skip(f"{URL_VARIABLE} unset (opt-in: the disposable server's container)")
    rig = Cluster(tmp_path)
    rig.start()
    rig.write_dsns()
    try:
        yield rig
    finally:
        rig.stop()


def test_a_default_cluster_lets_anyone_in_over_tcp_and_check_says_so_without_changing_it(cluster):
    """The finding, reproduced: a wrong password logs in as the superuser over 127.0.0.1."""
    assert cluster.tcp("mia", "not-the-password")
    before = cluster.hba_text()
    done, values = cluster.run("--check")
    assert done.returncode == 1, done.stdout + done.stderr
    assert values["hardened"] == "false" and values["apply_ready"] == "true"
    assert values["trust_tcp_lines"] == "4" and values["trust_local_lines"] == "2"
    assert values["hba_rule_errors"] == "0" and values["roles_without_scram"] == "none"
    assert cluster.hba_text() == before and cluster.backups() == []


def test_apply_closes_tcp_trust_keeps_the_socket_and_backs_up_first(cluster):
    before = cluster.hba_text()
    assert cluster.tcp("mia", "not-the-password")
    done, values = cluster.run("--apply")
    assert done.returncode == 0, done.stdout + done.stderr
    assert values["applied"] == "true" and values["hardened"] == "true"
    assert values["logins_verified"] == "1" and values["trust_local_lines"] == "2"

    # the file: only the four TCP trust lines changed; the unix socket lines are still trust
    after = cluster.hba_text()
    changed = [(b, a) for b, a in zip(before.splitlines(), after.splitlines(), strict=True)
               if b != a]
    assert len(changed) == 4 and len(before.splitlines()) == len(after.splitlines())
    assert all(a == b[: b.rindex("trust")] + "scram-sha-256" for b, a in changed)
    assert re.search(r"^local\s+all\s+all\s+trust$", after, re.MULTILINE)

    # the backup is byte for byte the file before, next to it
    [backup] = cluster.backups()
    assert re.fullmatch(re.escape(cluster.hba) + r"\.pre-e21-\d{8}T\d{6}Z", backup)
    assert cluster.backup_text(backup) == before
    assert values["backup"] == backup

    # the proof that matters: TCP now checks the password, and the admin path still works
    assert not cluster.tcp("mia", "not-the-password")
    assert cluster.tcp("mia", cluster.password)
    assert cluster.tcp("mia_app", cluster.app_password)
    assert not cluster.tcp("mia_app", cluster.password)
    assert cluster.socket_admin()

    # no secret reached an argument of any docker call, nor the output
    calls = cluster.arglog.read_text(encoding="utf-8")
    for secret in (cluster.password, cluster.app_password):
        assert secret not in calls + done.stdout + done.stderr


def test_apply_is_idempotent_and_the_second_run_makes_no_backup(cluster):
    assert cluster.run("--apply")[0].returncode == 0
    [backup] = cluster.backups()
    hardened = cluster.hba_text()
    done, values = cluster.run("--apply")
    assert done.returncode == 0 and values["applied"] == "false"
    assert "hba_already_hardened" in done.stdout
    assert cluster.backups() == [backup] and cluster.hba_text() == hardened
    done, values = cluster.run("--check")
    assert done.returncode == 0 and values["hardened"] == "true"


@pytest.mark.parametrize(
    ("role", "setup", "missing"),
    [
        ("assaf_os_session", "SET password_encryption = 'md5'; "
         "CREATE ROLE assaf_os_session LOGIN PASSWORD 'old-hash'", "assaf_os_session"),
        ("assaf_os_ledger", "CREATE ROLE assaf_os_ledger LOGIN", "assaf_os_ledger"),
        ("assaf_os_retention", "CREATE ROLE assaf_os_retention LOGIN", "assaf_os_retention"),
        ("mia_app", "ALTER ROLE mia_app PASSWORD NULL", "mia_app"),
        ("mia", "ALTER ROLE mia PASSWORD NULL", "mia"),
    ],
)
def test_apply_refuses_when_a_live_dsn_role_has_no_scram_password(cluster, role, setup, missing):
    cluster.psql(setup)
    before = cluster.hba_text()
    done, values = cluster.run("--apply")
    assert done.returncode == 2, done.stdout + done.stderr
    assert "reason=hba_role_without_scram" in done.stdout
    assert missing in values["roles_without_scram"].split(",")
    # nothing changed: the file, the backups, the reload, and trust is still open
    assert cluster.hba_text() == before and cluster.backups() == []
    assert cluster.tcp("mia", "not-the-password")
    check, check_values = cluster.run("--check")
    assert check_values["apply_ready"] == "false"


def test_a_role_that_cannot_log_in_or_does_not_exist_is_not_a_dsn_role(cluster):
    """A NOLOGIN role (the owner, or a retention role made before V2-14b) holds no DSN, and the
    roles V2-15 has not created yet do not exist. Since V2-14b, V2-15 makes assaf_os_retention LOGIN
    with no password (E8), so once provisioned it blocks apply until V2-31 gives it a verifier:
    the case parametrised above pins that, and the runbook runs step 1a before the provision."""
    cluster.psql("CREATE ROLE assaf_os_retention NOLOGIN; CREATE ROLE assaf_os_owner NOLOGIN")
    done, values = cluster.run("--apply")
    assert done.returncode == 0, done.stdout + done.stderr
    assert values["hardened"] == "true"


def test_a_role_found_only_in_a_dsn_file_is_checked_too(cluster):
    cluster.psql("SET password_encryption = 'md5'; CREATE ROLE extra_reader LOGIN PASSWORD 'old'")
    cluster.write_env(
        "settings.env",
        f"MIA_OWNER_OS_SESSION_DATABASE_URL='postgresql://extra_reader:old@127.0.0.1:"
        f"{cluster.port}/mia'\n",
    )
    before = cluster.hba_text()
    done, values = cluster.run("--apply")
    assert done.returncode == 2 and "reason=hba_role_without_scram" in done.stdout
    assert "extra_reader" in values["roles_without_scram"].split(",")
    assert cluster.hba_text() == before


def test_every_readable_dsn_form_is_logged_in_with_after_the_change(cluster):
    """Quoted values and a `+psycopg` driver suffix are real forms in the VM's DSN files."""
    port, password, app = cluster.port, cluster.password, cluster.app_password
    cluster.write_env(
        "runtime.env",
        f"MIA_DATABASE_URL=postgresql://mia_app:{app}@127.0.0.1:{port}/mia\n"
        f"MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://mia:{password}@127.0.0.1:{port}/mia\n",
    )
    cluster.write_env(
        "os-ledger-migrate.env",
        f"MIA_OS_LEDGER_MIGRATION_DATABASE_URL='postgresql+psycopg://mia:{password}"
        f"@127.0.0.1:{port}/mia'\n",
    )
    cluster.write_env(
        "settings.env",
        f'MIA_OWNER_OS_SESSION_DATABASE_URL="postgresql://mia_app:{app}@127.0.0.1:{port}/mia"\n'
        "MIA_OWNER_OS_ORIGIN=https://example.test\n",
    )
    done, values = cluster.run("--apply")
    assert done.returncode == 0, done.stdout + done.stderr
    assert values["logins_verified"] == "4" and values["dsn_unverified"] == "none"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://mia_app:{app}@db.example:{port}/mia",
        "postgresql://mia_app:{app}@localhost:{port}/mia",
        "postgresql://mia_app:{app}@127.0.0.1:{port}/mia?sslmode=disable",
    ],
)
def test_a_dsn_form_the_script_cannot_verify_is_refused_before_any_change(cluster, dsn):
    """V2-10 review r1: a form whose login is never tried must stop the change, not just be listed.

    It used to be reported as `dsn_unverified` while apply went on: after the reload its login was
    never checked and its role was not in the scram check, so `mia os-ledger migrate` or
    `mia restore` step 2 could fail afterwards, with no rollback. It now stops the change the way a
    DSN with no password does, and the operator rewrites the DSN in the canonical form.
    """
    key = "MIA_OWNER_OS_SESSION_DATABASE_URL"
    app, port = cluster.app_password, cluster.port
    cluster.write_env("settings.env", f"{key}=" + dsn.format(app=app, port=port) + "\n")
    before = cluster.hba_text()
    done, values = cluster.run("--apply")
    assert done.returncode == 2, done.stdout + done.stderr
    assert "reason=hba_dsn_unverified" in done.stdout
    assert values["dsn_unverified"] == key
    assert cluster.hba_text() == before and cluster.backups() == []
    assert cluster.tcp("mia", "not-the-password")  # nothing was reloaded: trust is still open
    assert app not in done.stdout + done.stderr

    check, check_values = cluster.run("--check")
    assert check_values["dsn_unverified"] == key and check_values["apply_ready"] == "false"

    # written in the canonical form, the same apply goes through and logs in with it
    cluster.write_env("settings.env", f"{key}=postgresql://mia_app:{app}@127.0.0.1:{port}/mia\n")
    done, values = cluster.run("--apply")
    assert done.returncode == 0, done.stdout + done.stderr
    assert values["dsn_unverified"] == "none" and values["logins_verified"] == "2"


def test_a_dsn_with_no_password_is_refused_before_any_change(cluster):
    cluster.write_env(
        "runtime.env", f"MIA_DATABASE_URL=postgresql://mia@127.0.0.1:{cluster.port}/mia\n"
    )
    before = cluster.hba_text()
    done, values = cluster.run("--apply")
    assert done.returncode == 2 and "reason=hba_dsn_without_password" in done.stdout
    assert values["dsn_without_password"] == "MIA_DATABASE_URL"
    assert cluster.hba_text() == before and cluster.backups() == []


def test_a_dsn_whose_password_is_wrong_rolls_the_change_back_and_never_leaves_the_app_out(cluster):
    cluster.write_dsns(mia_password="not-what-the-role-has")
    before = cluster.hba_text()
    done, values = cluster.run("--apply")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "reason=hba_login_failed_rolled_back" in done.stdout
    assert values["key"] == "MIA_DATABASE_URL"
    # the original file is back and loaded: trust is open again, as before the attempt
    assert cluster.hba_text() == before
    assert cluster.tcp("mia", "anything-at-all")
    assert "not-what-the-role-has" not in done.stdout + done.stderr
    assert "not-what-the-role-has" not in cluster.arglog.read_text(encoding="utf-8")


def test_trust_that_a_rewrite_cannot_reach_is_restored_not_half_applied(cluster):
    """An included file keeps a TCP trust rule alive: the result is judged by the server's rules."""
    extra = f"{cluster.dir}/extra.conf"
    cluster.docker("sh", "-c", f"printf 'host all all 127.0.0.1/32 trust\\n' > {extra}")
    cluster.docker("sh", "-c", f"printf 'include {cluster.dir}/extra.conf\\n' >> {cluster.hba}")
    cluster.psql("SELECT pg_reload_conf()")
    before = cluster.hba_text()
    done, _ = cluster.run("--apply")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "reason=hba_rewrite_invalid_restored" in done.stdout
    assert cluster.hba_text() == before
    assert cluster.tcp("mia", "anything-at-all")


def test_rollback_puts_the_newest_backup_back_and_is_idempotent(cluster):
    before = cluster.hba_text()
    assert cluster.run("--apply")[0].returncode == 0
    assert not cluster.tcp("mia", "not-the-password")
    done, values = cluster.run("--rollback")
    assert done.returncode == 0, done.stdout + done.stderr
    assert values["rolled_back"] == "true" and values["hardened"] == "false"
    assert cluster.hba_text() == before
    assert cluster.tcp("mia", "not-the-password")  # reloaded: trust is back, as it was
    done, values = cluster.run("--rollback")
    assert done.returncode == 0 and values["rolled_back"] == "false"
    assert "hba_already_restored" in done.stdout


def test_rollback_with_no_backup_changes_nothing(cluster):
    before = cluster.hba_text()
    done, _ = cluster.run("--rollback")
    assert done.returncode == 1 and "reason=hba_no_backup" in done.stdout
    assert cluster.hba_text() == before


def test_rollback_takes_the_newest_of_several_backups(cluster):
    first = cluster.hba_text()
    assert cluster.run("--apply")[0].returncode == 0
    assert cluster.run("--rollback")[0].returncode == 0
    # a hand edit between two windows: the second apply backs up THIS file, and rollback restores it
    cluster.docker("sh", "-c", f"printf '# edited between windows\\n' >> {cluster.hba}")
    edited = cluster.hba_text()
    assert edited != first
    import time

    time.sleep(1.1)  # backup names carry the second
    assert cluster.run("--apply")[0].returncode == 0
    assert len(cluster.backups()) == 2
    assert cluster.run("--rollback")[0].returncode == 0
    assert cluster.hba_text() == edited


def test_a_cluster_initialised_with_the_e21_args_is_hardened_from_the_start(tmp_path):
    if not os.getenv(URL_VARIABLE):
        pytest.skip(f"{URL_VARIABLE} unset")
    rig = Cluster(tmp_path, initdb_args=initdb_args_in_compose())
    rig.start()
    rig.write_dsns()
    try:
        done, values = rig.run("--check")
        assert done.returncode == 0, done.stdout + done.stderr
        assert values["hardened"] == "true" and values["trust_tcp_lines"] == "0"
        assert not rig.tcp("mia", "not-the-password")
        assert rig.tcp("mia", rig.password)
        assert rig.socket_admin()  # every VM tool still reaches PostgreSQL over the socket
        done, values = rig.run("--apply")
        assert done.returncode == 0 and values["applied"] == "false"
        assert rig.backups() == []
    finally:
        rig.stop()


def test_a_default_initdb_is_what_made_the_finding(tmp_path):
    """The control for the test above: the same initdb with no --auth flag has TCP trust."""
    if not os.getenv(URL_VARIABLE):
        pytest.skip(f"{URL_VARIABLE} unset")
    rig = Cluster(tmp_path)
    rig.start()
    try:
        hba = rig.hba_text()
        assert re.search(r"^host\s+all\s+all\s+127\.0\.0\.1/32\s+trust$", hba, re.MULTILINE)
        assert re.search(r"^host\s+all\s+all\s+::1/128\s+trust$", hba, re.MULTILINE)
    finally:
        rig.stop()


def test_non_root_and_bad_usage_are_refused_before_any_docker_call(cluster):
    done, _ = cluster.run("--apply", euid="1000")
    assert done.returncode == 1 and "reason=hba_root_required" in done.stdout
    for bad in ("", "--apply --check", "apply", "--status"):
        done, _ = cluster.run(bad)
        assert done.returncode == 2, bad
    assert cluster.arglog.read_text(encoding="utf-8") == ""


def test_an_unreachable_database_is_exit_2_and_nothing_is_changed(cluster):
    before = cluster.hba_text()
    done, _ = cluster.run("--apply", port=1)
    assert done.returncode == 2 and "reason=hba_database_unreachable" in done.stdout
    assert cluster.hba_text() == before


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("flock") is None,
                    reason="needs util-linux flock; the VM has it")
def test_apply_and_rollback_refuse_while_another_mia_operation_holds_the_lock(cluster, tmp_path):
    import fcntl

    lock = tmp_path / "held.lock"
    before = cluster.hba_text()
    with open(lock, "w", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for mode in ("--apply", "--rollback"):
            done, _ = cluster.run(mode, lock=lock)
            assert done.returncode == 1 and "reason=hba_operation_busy" in done.stdout
        check, _ = cluster.run("--check", lock=lock)  # read-only: not blocked
        assert check.returncode == 1 and "hardened=false" in check.stdout
    assert cluster.hba_text() == before
