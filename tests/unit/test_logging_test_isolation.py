"""A test that configures process logging must not hand its capture stream to the next test.

``configure_logging()`` (every worker ``main()`` and the first ``import app.main`` call it) binds
the root handler to whatever ``sys.stderr`` is at that moment. Under ``capsys`` that is a capture
that closes at teardown. ``tests/db/test_assaf_os_ledger_migration.py`` runs ``migrate.main()``
under ``capsys``; every later INFO record then failed with "I/O operation on closed file", and
logging's own "--- Logging error ---" report landed in a later test's stderr (42 importer CLI
cases in a whole-suite run with the PostgreSQL URLs set). ``tests/conftest.py`` restores the root
logger after each test.

The two tests run in file order: the first leaves the hazard behind, the second must not see it.
"""

import logging
import sys

from app.core.logging import RedactingFilter, configure_logging


def _configured_handlers() -> list[logging.Handler]:
    return [
        handler for handler in logging.getLogger().handlers
        if any(isinstance(f, RedactingFilter) for f in handler.filters)
    ]


def test_a_worker_main_configures_logging_inside_capsys(capsys):
    configure_logging()
    assert [handler.stream for handler in _configured_handlers()] == [sys.stderr]


def test_the_next_test_does_not_inherit_that_handler(capsys):
    root = logging.getLogger()
    assert not any(getattr(getattr(h, "stream", None), "closed", False) for h in root.handlers)
    logging.getLogger("mia.os_import").info("os_import op=stage outcome=ok reason=persisted")
    logging.getLogger("mia.os_import").warning("os_import op=stage outcome=refused")
    assert capsys.readouterr().err == ""
