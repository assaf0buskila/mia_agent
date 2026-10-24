"""Fixtures for the V2-37 assistant tests.

``aw`` runs every test on the FakeLedger (with the in-memory store) and, when
``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set, on the PostgreSQL ledger too (with the real
``PgAssistantStore``). A PG case is generated, never skipped, exactly like the statement suite.
A test that needs a fake-only hook takes ``fake_aw`` and has no PG case.
"""

from __future__ import annotations

import pytest

from tests.services.os_ledger.assistant.support import AWorld
from tests.services.os_ledger.statements.support import pg_url

CASES = ["fake", *(["pg"] if pg_url() else [])]
# test_pg_rows.py is PostgreSQL only: collected when the URL is set, absent (not skipped) otherwise.
collect_ignore = [] if pg_url() else ["test_pg_rows.py"]


@pytest.fixture(params=CASES)
def aw(request) -> AWorld:
    return AWorld(request.param, request=request)


@pytest.fixture(params=CASES)
def new_aw(request):
    """A factory for a world with other settings (the assistant flag off, a missing key, ...)."""

    def make(**settings) -> AWorld:
        return AWorld(request.param, request=request, **settings)

    return make


@pytest.fixture
def pg_aw(request) -> AWorld:
    return AWorld("pg", request=request)


@pytest.fixture
def fake_aw() -> AWorld:
    return AWorld("fake")
