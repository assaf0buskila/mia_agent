"""Owner-run local bootstrap, not cutover tooling. No Google calls or migrations.

check validates only; prepare probes/optionally starts PG16 and creates a detached
worktree; run rechecks every guard and launches that worktree on loopback.
Only reason codes are emitted on errors. Config values are never printed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
LOCAL = Path(__file__).resolve().parent / "local"
REQUIRED = {
    "MIA_DATABASE_URL", "MIA_ASSAF_OS_SCRIPT_URL", "REHEARSAL_DEPLOYMENT_ID",
    "MIA_ASSAF_OS_WORKBOOK_ID", "MIA_ASSAF_OS_WORKSPACE_ID", "MIA_ASSAF_OS_MODE",
    "MIA_ASSAF_OS_HMAC_SECRET", "MIA_TELEGRAM_OWNER_USER_IDS",
}
OPTIONAL = {
    "MIA_OWNER_OS_LEDGER_DATABASE_URL", "MIA_OWNER_OS_SESSION_DATABASE_URL",
    "MIA_OWNER_OS_READ_DATABASE_URL", "MIA_OWNER_OS_ACTION_DATABASE_URL",
}


class Refused(Exception):
    """A bounded reason code, never an exception from a provider."""


def command(args, reason, *, cwd=ROOT, env=None, allowed=(0,)):
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        raise Refused(reason) from None
    if result.returncode not in allowed:
        raise Refused(reason)
    return result


def read_config(path):
    if path.name != "rehearsal.env" or path.is_symlink():
        raise Refused("config_path_invalid")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key != key.strip() or key in values or "\x00" in value:
            raise Refused("config_invalid")
        values[key] = value.strip()
    return {key: value for key, value in values.items() if value}


def load_local():
    paths = [LOCAL / "rehearsal.env", LOCAL / "production-pins.json"]
    for path in paths:
        if path.is_symlink() or path.resolve().parent != LOCAL.absolute():
            raise Refused("config_path_invalid")
        if not path.is_file():
            raise Refused("local_config_missing")
        # check-ignore returns non-zero for tracked files as well as unignored files.
        command(["git", "check-ignore", "--quiet", str(path)], "local_config_not_ignored")
    try:
        return read_config(paths[0]), json.loads(paths[1].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise Refused("local_config_invalid") from None


def script_id(url):
    match = re.fullmatch(r"https://script\.google\.com/macros/s/([A-Za-z0-9_-]+)/exec", url)
    if not match:
        raise Refused("script_url_invalid")
    return match[1]


def dsn_parts(value):
    try:
        url = urlsplit(value)
        if (url.scheme != "postgresql+psycopg" or url.query or url.fragment
                or not url.username or not url.port
                or not re.fullmatch(r"/[A-Za-z0-9_]+", url.path)):
            raise Refused("dsn_invalid")
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise Refused("dsn_not_loopback")
        return {
            "host": url.hostname, "port": url.port, "dbname": url.path[1:],
            "user": unquote(url.username), "password": unquote(url.password or ""),
        }
    except ValueError:
        raise Refused("dsn_invalid") from None


def guard(config, pins):
    if not isinstance(pins, dict):
        raise Refused("pins_incomplete")
    for key in ("script_urls", "deployment_ids", "workbook_ids"):
        values = pins.get(key)
        if (not isinstance(values, list) or not values
                or any(not isinstance(v, str) or not v or v != v.strip() for v in values)):
            raise Refused("pins_incomplete")
    if set(config) - REQUIRED - OPTIONAL:
        raise Refused("config_unknown_key")
    if any(not config.get(key) for key in REQUIRED):
        raise Refused("config_incomplete")
    if config["MIA_ASSAF_OS_MODE"] != "TEST":
        raise Refused("mode_not_test")
    url = config["MIA_ASSAF_OS_SCRIPT_URL"]
    if url in pins["script_urls"]:
        raise Refused("production_script_url")
    deployment = script_id(url)
    production_ids = set(pins["deployment_ids"]) | {script_id(u) for u in pins["script_urls"]}
    if deployment in production_ids or config["REHEARSAL_DEPLOYMENT_ID"] in production_ids:
        raise Refused("production_deployment")
    if config["MIA_ASSAF_OS_WORKBOOK_ID"] in pins["workbook_ids"]:
        raise Refused("production_workbook")
    if deployment != config["REHEARSAL_DEPLOYMENT_ID"]:
        raise Refused("deployment_mismatch")
    if (not re.fullmatch(r"[A-Za-z0-9_-]{20,}", config["MIA_ASSAF_OS_WORKBOOK_ID"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
                                config["MIA_ASSAF_OS_WORKSPACE_ID"])
            or not re.fullmatch(r"[1-9][0-9]{0,19}", config["MIA_TELEGRAM_OWNER_USER_IDS"])
            or len(config["MIA_ASSAF_OS_HMAC_SECRET"]) < 32):
        raise Refused("binding_invalid")
    main = dsn_parts(config["MIA_DATABASE_URL"])
    for key in OPTIONAL:
        if config.get(key):
            other = dsn_parts(config[key])
            if any(other[k] != main[k] for k in ("host", "port", "dbname")):
                raise Refused("dsn_target_mismatch")


def runtime_environment(config, sha):
    excluded = {"DATABASE_URL", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("MIA_", "PG", "UV_")) and key.upper() not in excluded
    }
    env.update({key: value for key, value in config.items() if key.startswith("MIA_")})
    env.update({
        "MIA_ENV": "test",  # Settings must never load .env.
        "MIA_BUILD_SHA": sha,
        "MIA_ASSAF_OS_ENABLED": "true",
        "MIA_AUTOMATION_MODE": "off",
        "UV_CACHE_DIR": str(LOCAL / "uv-cache"),
    })
    return env


def check_pg(config):
    import psycopg

    # Explicit parameters and a scrubbed libpq environment prevent PGSERVICE redirection.
    for key in list(os.environ):
        if key.upper().startswith("PG"):
            del os.environ[key]
    try:
        with psycopg.connect(**dsn_parts(config["MIA_DATABASE_URL"]), connect_timeout=3) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW server_version_num")
                if not 160000 <= int(cursor.fetchone()[0]) < 170000:
                    raise Refused("pg_version_not_16")
    except psycopg.Error:
        raise Refused("pg_unavailable") from None


def start_pg(config, sha):
    parts = dsn_parts(config["MIA_DATABASE_URL"])
    if (parts["host"] != "127.0.0.1" or parts["user"] != "postgres"
            or parts["password"] or parts["dbname"] != "rehearsal_test"
            or parts["port"] != 56217):
        raise Refused("pg_start_requires_disposable_target")
    # Do not publish a 'loopback' port on a remotely selected Docker daemon.
    if os.environ.get("DOCKER_HOST"):
        raise Refused("docker_host_override")
    result = command(["docker", "context", "inspect"], "docker_context_unavailable")
    try:
        host = json.loads(result.stdout)[0]["Endpoints"]["docker"]["Host"]
        if not host.startswith(("unix://", "npipe://")):
            raise Refused("docker_not_local")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise Refused("docker_context_invalid") from None
    command([
        "docker", "run", "--detach", "--rm", "--pull=never",
        "--name", f"assaf-os-rehearsal-{sha[:12]}",
        "--label", "assaf-os.disposable=rehearsal",
        "--publish", "127.0.0.1:56217:5432", "--tmpfs", "/var/lib/postgresql/data",
        "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
        "--env", "POSTGRES_DB=rehearsal_test", "postgres:16",
    ], "pg_start_failed")
    for _ in range(20):
        try:
            check_pg(config)
            return
        except Refused as error:
            if str(error) != "pg_unavailable":
                raise
            time.sleep(0.5)
    raise Refused("pg_start_timeout")


def verify_worktree(path, sha):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise Refused("worktree_path_invalid")
    head = command(["git", "rev-parse", "HEAD"], "worktree_unavailable", cwd=path)
    if head.stdout.strip() != sha:
        raise Refused("release_sha_mismatch")
    branch = command(["git", "symbolic-ref", "-q", "HEAD"], "worktree_invalid",
                     cwd=path, allowed=(0, 1))
    if branch.returncode != 1:
        raise Refused("worktree_not_detached")
    status = command(["git", "status", "--porcelain", "--untracked-files=all"],
                     "worktree_status_failed", cwd=path)
    if status.stdout.strip():
        raise Refused("worktree_not_clean")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "prepare", "run"))
    parser.add_argument("--sha", required=True, help="Full 40-hex locally available release commit")
    parser.add_argument("--start-pg", action="store_true", help="prepare only; new disposable PG16")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
            raise Refused("sha_invalid")
        if args.start_pg and args.action != "prepare":
            raise Refused("start_pg_prepare_only")
        config, pins = load_local()
        guard(config, pins)
        if args.action == "check":
            print("reason=rehearsal_guard_passed")
            return 0
        path = LOCAL / "worktrees" / args.sha
        # Resolve the SHA before Docker or worktree mutation; never fetch implicitly.
        commit = command(["git", "rev-parse", "--verify", f"{args.sha}^{{commit}}"],
                         "release_sha_unavailable")
        if commit.stdout.strip() != args.sha:
            raise Refused("release_sha_mismatch")
        if args.start_pg:
            start_pg(config, args.sha)
        else:
            check_pg(config)
        if args.action == "prepare" and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            command(["git", "-c", "core.autocrlf=false", "worktree", "add", "--detach",
                     str(path), args.sha], "worktree_create_failed")
        verify_worktree(path, args.sha)
        if args.action == "prepare":
            launch = [sys.executable, str(Path(__file__).resolve()), "run", "--sha", args.sha]
            # Commands contain paths and the SHA only; no DSNs, pins or HMAC values.
            quoted = ("& " + " ".join("'" + v.replace("'", "''") + "'" for v in launch)
                      if os.name == "nt" else shlex.join(launch))
            print("reason=rehearsal_prepared")
            print("env_names=" + ",".join(sorted(k for k in config if k.startswith("MIA_"))))
            print("fixed_env_names=MIA_ENV,MIA_BUILD_SHA,MIA_ASSAF_OS_ENABLED,MIA_AUTOMATION_MODE")
            print("mia_command=" + quoted)
            print("mia_entrypoint=uv run --offline --frozen uvicorn app.main:app "
                  "--host 127.0.0.1 --port 58000")
            return 0
        # Run inside the detached SHA, without a shell, reload or inherited MIA keys.
        return subprocess.call(
            ["uv", "run", "--offline", "--frozen", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", "58000"],
            cwd=path, env=runtime_environment(config, args.sha),
        )
    except Refused as error:
        print(f"reason={error}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print("reason=local_io_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
