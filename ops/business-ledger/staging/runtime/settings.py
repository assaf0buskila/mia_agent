"""Finite Business TEST extension of the reviewed private staging settings."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from staging_runtime import base_settings as base

# The canonical bootstrap imports these names from staging_runtime.settings.
ADMIN_PASSWORD_FILE = base.ADMIN_PASSWORD_FILE
AUTH_PASSWORD_FILE = base.AUTH_PASSWORD_FILE
AUTH_ROLE = base.AUTH_ROLE
BACKEND_SHA = base.BACKEND_SHA
DATABASE_NAME = base.DATABASE_NAME
READ_PASSWORD_FILE = base.READ_PASSWORD_FILE
READ_ROLE = base.READ_ROLE
database_url = base.database_url
read_secret = base.read_secret
reject_ambient_credentials = base.reject_ambient_credentials

BUSINESS_SECRET_FILE = Path("/run/secrets/business_ledger_hmac_secret")
TARGETS = json.loads(Path(__file__).with_name("business_targets.json").read_text(encoding="utf-8"))
BUSINESS_SCOPES = "business-ledger:read,business-ledger:write"

StageConfigurationError = base.StageConfigurationError
StageRuntimeConfig = base.StageRuntimeConfig


def _exact_business_opt_in(env) -> bool:
    return env.get("OS_STAGE_BUSINESS_TEST_ENABLED", "") == "true"


def load_stage_config(environment=None, *, secret_loader=base.read_secret):
    env = __import__("os").environ if environment is None else environment
    stage = base.load_stage_config(env, secret_loader=secret_loader)
    if not _exact_business_opt_in(env):
        raise StageConfigurationError("stage_business_exact_test_opt_in_required")
    if env.get("OS_STAGE_BUSINESS_WORKSPACE_ID") != TARGETS["workspace_id"]:
        raise StageConfigurationError("stage_business_workspace_mismatch")
    if env.get("OS_STAGE_BUSINESS_WORKBOOK_ID") != TARGETS["workbook_id"]:
        raise StageConfigurationError("stage_business_workbook_mismatch")
    service_url = env.get("OS_STAGE_BUSINESS_SERVICE_URL", "")
    target = urlsplit(service_url)
    if service_url != TARGETS["service_url"] or target.hostname != "script.google.com":
        raise StageConfigurationError("stage_business_service_mismatch")
    hmac_secret = secret_loader(BUSINESS_SECRET_FILE)
    if len(hmac_secret) < 32:
        raise StageConfigurationError("stage_business_hmac_invalid")
    scopes = f"{base.READ_SCOPES},{BUSINESS_SCOPES}"
    settings = stage.settings.model_copy(
        update={
            "owner_os_workspace_id": TARGETS["workspace_id"],
            "owner_os_session_scopes": scopes,
            "business_ledger_test_enabled": True,
            "business_ledger_workspace_id": TARGETS["workspace_id"],
            "business_ledger_script_url": service_url,
            "business_ledger_hmac_secret": hmac_secret,
            "owner_os_actions_enabled": False,
            "reminders_enabled": False,
            "reminder_delivery_enabled": False,
            "crm_delivery_enabled": False,
            "automation_mode": "off",
        }
    )
    return base.StageRuntimeConfig(
        settings, stage.backend_sha, stage.origin, stage.owner_ids, stage.bot_id
    )
