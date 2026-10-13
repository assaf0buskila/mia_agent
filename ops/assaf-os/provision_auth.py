"""Prepare private OS sessions on the existing MIA PostgreSQL, never a new server.

Explicit --apply creates a new technical database/role and patches only OS keys in
the existing Secret Manager secret. Both feature gates stay OFF. No .env is read,
credential is written to disk/argv, or secret/provider response is printed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.request
from pathlib import Path

PROJECT = "mia-assafweb"
VM = "mia"
ZONE = "me-west1-a"
DATABASE = "assaf_os_auth"
ROLE = "assaf_os_session"
ORIGIN = "https://assaf-os.assaf-buskila10.workers.dev"
WORKBOOK = "1T9Is3iJ_RlI8KndpTXYCfuTkVsMbncw3_0bAVze0RE4"
GOOGLE_CLIENT = "268307387746-5lkr1hp68hm8movq4v9psq11qnucots8.apps.googleusercontent.com"


def fingerprint(values: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def verify_vm() -> None:
    metadata = json.loads(command([
        "gcloud", "compute", "instances", "describe", VM,
        f"--project={PROJECT}", f"--zone={ZONE}",
        "--format=json(id,status,networkInterfaces)",
    ]))
    addresses = [item.get("natIP") for interface in metadata.get("networkInterfaces", [])
                 for item in interface.get("accessConfigs", [])]
    if (str(metadata.get("id")) != "8764131532174833241"
            or metadata.get("status") != "RUNNING"
            or addresses != ["34.165.183.186"]):
        raise RuntimeError("existing_vm_binding_changed")


def command(arguments: list[str], *, data: str | None = None) -> str:
    executable = shutil.which(arguments[0])
    if not executable:
        raise RuntimeError("cli_unavailable")
    result = subprocess.run(
        [executable, *arguments[1:]], input=data, capture_output=True, text=True,
        encoding="utf-8", timeout=180, check=False,
    )
    if result.returncode:
        # CLI stderr may contain sensitive provider responses. Emit a code only.
        raise RuntimeError("command_failed")
    return result.stdout


def settings() -> dict[str, str]:
    raw = command(["gcloud", "secrets", "versions", "access", "latest",
                   "--secret=mia-env", f"--project={PROJECT}"])
    values: dict[str, str] = {}
    for line in raw.splitlines():
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if match:
            key, value = match.groups()
            if key in values:
                raise RuntimeError("duplicate_setting")
            values[key] = value
    if not all(values.get(key) for key in
               ("MIA_ENV", "MIA_PUBLIC_BASE_URL", "MIA_TELEGRAM_BOT_TOKEN")):
        raise RuntimeError("settings_incomplete")
    return values


def postgres(sql: str, database: str = "postgres") -> str:
    if database not in {"postgres", DATABASE}:
        raise RuntimeError("database_denied")
    remote = (f"sudo docker exec -i mia-postgres-1 psql -X -U mia -d {database} "
              "--set=ON_ERROR_STOP=1 --no-align --tuples-only --quiet")
    if os.name == "nt":
        # The SDK Windows SSH wrapper injects a host-key answer into stdin, which
        # corrupts piped SQL. Use its installed SSH client and existing key/cache;
        # -batch rejects an unknown/changed host key instead of accepting one.
        sdk = shutil.which("gcloud")
        if not sdk:
            raise RuntimeError("cli_unavailable")
        plink = Path(sdk).parent / "sdk" / "plink.exe"
        key = Path.home() / ".ssh" / "google_compute_engine.ppk"
        return command([str(plink), "-batch", "-T", "-i", str(key),
                        "assaf_buskila10_gmail_com@34.165.183.186", remote], data=sql)
    return command([
        "gcloud", "compute", "ssh", VM, f"--project={PROJECT}", f"--zone={ZONE}",
        "--ssh-flag=-T", "--command=" + remote,
    ], data=sql)


def owner_subject() -> str:
    token = command(["gcloud", "auth", "print-access-token"]).strip()
    request = urllib.request.Request("https://www.googleapis.com/oauth2/v3/userinfo",
                                     headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=20) as response:
        identity = json.loads(response.read())
    if (identity.get("email") != "assaf.buskila10@gmail.com"
            or identity.get("email_verified") is not True
            or not re.fullmatch(r"[0-9]{10,64}", str(identity.get("sub", "")))):
        raise RuntimeError("google_owner_mismatch")
    return str(identity["sub"])


def provision(apply: bool = False) -> dict:
    verify_vm()
    existing = settings()
    owner_ids = [part.strip() for part in
                 existing.get("MIA_TELEGRAM_OWNER_USER_IDS", "").split(",") if part.strip()]
    if len(owner_ids) != 1 or not re.fullmatch(r"[0-9]{1,32}", owner_ids[0]):
        raise RuntimeError("owner_binding_requires_review")
    if any(existing.get(key) for key in ("MIA_OWNER_OS_SESSION_DATABASE_URL",
                                         "MIA_ASSAF_OS_HMAC_SECRET")):
        raise RuntimeError("already_configured_do_not_rotate")
    occupied = postgres(
        f"SELECT 'database' FROM pg_database WHERE datname='{DATABASE}' "
        f"UNION ALL SELECT 'role' FROM pg_roles WHERE rolname='{ROLE}';\n")
    if occupied.strip():
        raise RuntimeError("existing_auth_target_do_not_replace")
    subject = owner_subject()
    plan = {"project": PROJECT, "vm": VM, "database": DATABASE, "role": ROLE,
            "new_server": False, "features_enabled": False, "owner_verified": True}
    if not apply:
        return {"status": "preflight_ready", **plan}
    password = secrets.token_hex(48)
    patches = {
        "MIA_OWNER_OS_ENABLED": "false",
        "MIA_OWNER_OS_ACTIONS_ENABLED": "false",
        "MIA_OWNER_OS_GOOGLE_ENABLED": "false",
        "MIA_OWNER_OS_GOOGLE_CLIENT_ID": GOOGLE_CLIENT,
        "MIA_OWNER_OS_GOOGLE_SUBJECT": subject,
        "MIA_OWNER_OS_GOOGLE_OWNER_ID": owner_ids[0],
        "MIA_OWNER_OS_SESSION_SECRET": secrets.token_hex(48),
        "MIA_OWNER_OS_SESSION_DATABASE_URL":
            f"postgresql+psycopg://{ROLE}:{password}@127.0.0.1:5432/{DATABASE}",
        "MIA_OWNER_OS_ORIGIN": ORIGIN,
        "MIA_OWNER_OS_WORKSPACE_ID": "assaf-os",
        "MIA_OWNER_OS_SESSION_SCOPES": "assaf-os:read,assaf-os:propose,assaf-os:approve",
        "MIA_ASSAF_OS_ENABLED": "false",
        "MIA_ASSAF_OS_WORKBOOK_ID": WORKBOOK,
        "MIA_ASSAF_OS_WORKSPACE_ID": "assaf-os",
        "MIA_ASSAF_OS_HMAC_SECRET": secrets.token_hex(48),
        "MIA_ASSAF_OS_MODE": "LIVE",
    }
    # Preserve the untouched baseline, including website/MIA provider settings.
    merged = dict(existing)
    merged.update(patches)
    secret_text = "\n".join(key + "=" + value for key, value in merged.items()) + "\n"
    if fingerprint(settings()) != fingerprint(existing):
        raise RuntimeError("settings_baseline_changed")
    command(["gcloud", "secrets", "versions", "add", "mia-env", "--data-file=-",
             f"--project={PROJECT}"], data=secret_text)
    if fingerprint(settings()) != fingerprint(merged):
        raise RuntimeError("settings_publish_verification_failed")
    # Credentials are recoverable from Secret Manager if provisioning is interrupted.
    postgres(f"CREATE ROLE {ROLE} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
             f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}';\n"
             f"CREATE DATABASE {DATABASE} OWNER mia;\n"
             f"REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC;\n"
             f"GRANT CONNECT ON DATABASE {DATABASE} TO {ROLE};\n")
    migration = Path(__file__).resolve().parents[2] / "migrations/20261011_owner_os_sessions.sql"
    grants = {
        "owner_os_login_challenges": "SELECT, INSERT, UPDATE",
        "owner_os_login_proofs": "INSERT",
        "owner_os_sessions": "SELECT, INSERT, UPDATE",
        "owner_os_revocations": "SELECT, INSERT, UPDATE",
        "owner_os_auth_attempts": "SELECT, INSERT",
    }
    sql = "BEGIN;\nREVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
    sql += migration.read_text(encoding="utf-8") + "\n"
    sql += f"GRANT USAGE ON SCHEMA public TO {ROLE};\n"
    sql += "\n".join(f"GRANT {rights} ON {table} TO {ROLE};"
                     for table, rights in grants.items())
    sql += f"\nGRANT USAGE, SELECT ON owner_os_auth_attempts_id_seq TO {ROLE};\nCOMMIT;\n"
    postgres(sql, DATABASE)
    verified = postgres("SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name LIKE 'owner_os_%';\n",
                        DATABASE).strip()
    if verified != "5":
        raise RuntimeError("auth_schema_verification_failed")
    verify_role_posture()
    return {"status": "provisioned_disabled", "tables": 5, "role_posture": "pass", **plan}


def verify_role_posture() -> None:
    """Run the real auth verifier's SQL under the new effective role, no CRM access."""
    source = Path(__file__).resolve().parents[2] / "app/api/owner_sessions.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in module.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_validate_runtime_role")
    queries = [node.args[0].value for node in ast.walk(function)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "text" and len(node.args) == 1
               and isinstance(node.args[0], ast.Constant)
               and isinstance(node.args[0].value, str)]
    if len(queries) != 3 or any(not query.lstrip().startswith("SELECT") for query in queries):
        raise RuntimeError("posture_verifier_changed")
    sql = f"BEGIN; SET LOCAL ROLE {ROLE};\n" + ";\n".join(queries) + ";\nROLLBACK;\n"
    rows = postgres(sql, DATABASE).strip().splitlines()
    if (len(rows) != 3 or any(value != "f" for value in rows[0].split("|"))
            or any(value != "t" for row in rows[1:] for value in row.split("|"))):
        raise RuntimeError("auth_role_posture_failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    try:
        print(json.dumps(provision(arguments.apply), separators=(",", ":")))
    except Exception as error:
        # No exception text/traceback: it may include DSNs, tokens or provider text.
        allowed = {"cli_unavailable", "command_failed", "duplicate_setting", "settings_incomplete",
                   "google_owner_mismatch", "owner_binding_requires_review",
                   "already_configured_do_not_rotate", "existing_auth_target_do_not_replace",
                   "auth_schema_verification_failed", "existing_vm_binding_changed",
                   "settings_baseline_changed", "settings_publish_verification_failed",
                   "posture_verifier_changed", "auth_role_posture_failed"}
        code = (str(error) if type(error) is RuntimeError and str(error) in allowed
                else "provisioning_failed_no_secret_output")
        print(json.dumps({"status": "failed", "reason": code}, separators=(",", ":")))
        raise SystemExit(1) from None
