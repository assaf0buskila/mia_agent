"""The ``os_verifier`` job (V2-18, §8): a periodic full recompute that flags summary drift.

``main()`` runs ``app.services.os_ledger.verifier.verify`` once and exits 0 when clean, 1 on
drift (``summary_drift``, ``summary_cycle_not_empty``, ``state_invalid`` or ``workspace_missing``)
and 2 when the ledger is unavailable. It only reads; it never repairs. V2-31 registers the
entrypoint and its schedule (§3.1).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from app.services.os_ledger import api, verifier

_LOG = logging.getLogger("mia.os_ledger")


def main(argv: list[str] | None = None, *, settings: Any | None = None) -> int:
    del argv  # no options: the workspace and the DSN come from the settings
    if settings is None:
        from app.core.config import get_settings
        from app.core.logging import configure_logging

        settings = get_settings()
        configure_logging()
    try:
        report = verifier.verify(settings)
    except api.LedgerError as exc:
        _LOG.warning("os_verifier op=main outcome=refused reason=%s", exc.reason or exc.code)
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
