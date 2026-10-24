"""Fixtures for the V2-26 statement service tests.

``world`` runs every test on the FakeLedger and, when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set,
on the PostgreSQL ledger as well. A PG case is generated, never skipped, exactly like the ledger
conformance suite (``tests/services/os_ledger/conftest.py``). A test that needs a fake-only hook
(fault injection) takes ``fake_world`` and has no PG case.
"""

from __future__ import annotations

import pytest

from tests.services.os_ledger.statements.support import World, pg_url

CASES = ["fake", *(["pg"] if pg_url() else [])]


@pytest.fixture(params=CASES)
def world(request) -> World:
    return World(request.param, request=request)


@pytest.fixture(params=CASES)
def new_world(request):
    """A factory for a world with other settings (a missing pin, a shadow domain, ...)."""

    def make(**settings) -> World:
        return World(request.param, request=request, **settings)

    return make


@pytest.fixture
def fake_world() -> World:
    return World("fake")
