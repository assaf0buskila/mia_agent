"""The os-retention job (V2-26; V2_PG_CONTRACTS.md §12.2, §18.2, §22 E6, E8).

Two steps, each attempted whatever the other does, because personal statement detail must not
outlive its window even when part of the machinery is down (I11):

1. ``expire_due`` as the runtime role: every overdue pending statement batch is finished
   ``expired`` and its detail purged, under the expiry admission (the kill switch,
   ``backend=sheets``, a fenced ``statements`` domain and a disabled ledger never stop it).
2. The fixed retention deletes as the retention role, on its own DSN: the batch lines whose detail
   the runtime already purged, the I11 backstop (lines of a proposal that expired more than a day
   ago, whatever ``purged_at`` says) and the assistant audit rows older than 180 days. The job
   executes exactly ``role_policy.RETENTION_DELETE_SQL`` and no other SQL (§22 E6, E8).

The privileged DSN is read only from ``MIA_OS_LEDGER_RETENTION_DATABASE_URL`` (the job's own env
file); there is no default. Nothing is printed or logged but codes and counts, and the exit status
is non-zero when either step failed.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import sqlalchemy_database_url
from app.services.os_ledger import api
from app.services.os_ledger.role_policy import RETENTION_DELETE_SQL
from app.services.os_ledger.statements import LedgerStatementService, build_statement_service

RETENTION_DSN_ENV = "MIA_OS_LEDGER_RETENTION_DATABASE_URL"
AUTHORIZE_MODULE = "app.services.os_ledger.authorize"  # V2-19; imported at call time only
STEPS = ("purge", "backstop", "audit")
_LOG = logging.getLogger("mia.os_retention")


def _worker_context(settings: Settings) -> api.TrustedOwnerContext:
    """The ``retention`` WORKER context, from V2-19's ``for_worker``; fail closed without it."""
    try:
        return importlib.import_module(AUTHORIZE_MODULE).for_worker("retention", settings)
    except (ImportError, AttributeError):
        raise api.LedgerError("ledger_unavailable", reason="authorize_missing") from None


def _code(exc: BaseException) -> str:
    if isinstance(exc, api.LedgerError):
        return exc.reason or exc.code
    return type(exc).__name__.lower()[:48]


def run(settings: Settings, *,
        service: LedgerStatementService | None = None,
        context: Callable[[Settings], api.TrustedOwnerContext] | None = None,
        retention_engine: Engine | None = None) -> dict[str, Any]:
    """Both steps; returns ``{expired, purge, backstop, audit, errors}`` (counts and codes)."""
    summary: dict[str, Any] = {"expired": 0, "purge": 0, "backstop": 0, "audit": 0, "errors": []}
    try:
        ctx = (context or _worker_context)(settings)
        summary["expired"] = (service or build_statement_service(settings)).expire_due(ctx)
    except Exception as exc:  # the deletes below must still run
        summary["errors"].append("expire_due:" + _code(exc))
    owned = retention_engine is None
    engine = retention_engine
    try:
        if engine is None:
            dsn = os.getenv(RETENTION_DSN_ENV, "")
            if not dsn:
                summary["errors"].append("retention_dsn_missing")
                return summary
            engine = create_engine(sqlalchemy_database_url(dsn))
        params = {"workspace_id": settings.assaf_os_workspace_id}
        for step in STEPS:
            try:
                with engine.begin() as connection:
                    summary[step] = int(connection.execute(
                        text(RETENTION_DELETE_SQL[step]), params).rowcount)
            except Exception as exc:
                summary["errors"].append(step + ":" + _code(exc))
    finally:
        if owned and engine is not None:
            engine.dispose()
    return summary


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    summary = run(get_settings())
    _LOG.info("os_retention op=run outcome=%s expired=%d purge=%d backstop=%d audit=%d errors=%d",
              "failed" if summary["errors"] else "ok", summary["expired"], summary["purge"],
              summary["backstop"], summary["audit"], len(summary["errors"]))
    print(json.dumps(summary))
    if summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
