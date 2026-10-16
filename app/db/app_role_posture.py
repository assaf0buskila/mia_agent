"""Read-only main-DSN authority probe. Never include database errors in logs.

The catalog check is V2-14's `_validate_main_role_catalog`, imported and never copied, so this
probe and the ledger migration can never disagree about what a safe main role is.
"""

import logging
import threading

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db.os_ledger_migrate import _validate_main_role_catalog

logger = logging.getLogger(__name__)

# The only reasons the validator may report. Anything else becomes a fixed code, so a driver
# message (which can carry a DSN fragment) never reaches a log line or a response.
_VALIDATOR_REASONS = frozenset(
    {
        "app_role_demote_required",
        "app_role_database_owner_unsafe",
        "app_role_membership_unsafe",
        "app_role_ledger_access_unsafe",
    }
)

_coerced_lock = threading.Lock()
_coerced_logged = False


def effective_mode(value: object) -> str:
    """Return "enforce" only for exactly "enforce"; every other value behaves as "observe".

    The setting is a plain str on purpose (V2 contract section 3.3): a bad value must never fail
    the settings load or the process start. An unknown value is logged once, without its text.
    """
    global _coerced_logged
    if value == "enforce":
        return "enforce"
    if value != "observe":
        with _coerced_lock:
            first = not _coerced_logged
            _coerced_logged = True
        if first:
            logger.warning("app_role_posture op=load outcome=coerced reason=config_invalid")
    return "observe"


def inspect_posture(engine: Engine) -> str:
    if engine.dialect.name != "postgresql":
        return "app_role_postgres_required"
    with engine.connect() as conn:
        role, impersonated = conn.execute(
            text("SELECT current_user, session_user <> current_user")
        ).one()
        try:
            _validate_main_role_catalog(conn, role)
        except ValueError as failure:
            reason = str(failure)
            return reason if reason in _VALIDATOR_REASONS else "app_role_check_failed"
        if impersonated:
            return "app_role_impersonation_unsafe"
    return "app_role_ok"


def posture_ready(engine: Engine, mode: str = "observe") -> bool:
    mode = effective_mode(mode)
    try:
        reason = inspect_posture(engine)
    except Exception:
        reason = "app_role_check_failed"
    safe = reason == "app_role_ok"
    ready = mode == "observe" or safe
    logger.info("app_role_posture reason=%s safe=%s ready=%s", reason, safe, ready)
    return ready


def app_role_ready(mode: str = "observe") -> bool:
    from app.db.session import get_engine

    mode = effective_mode(mode)
    try:
        engine = get_engine()
    except Exception:
        ready = mode == "observe"
        logger.info("app_role_posture reason=app_role_check_failed safe=False ready=%s", ready)
        return ready
    return posture_ready(engine, mode)
