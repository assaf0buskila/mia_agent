"""Fixtures for the ledger conformance suite (V2-16f).

``ledger`` is parametrized over ``["fake"]``, plus ``"pg"`` only when BOTH hold (§3.4):
  1. ``app.services.os_ledger.core`` exists (V2-16 has landed), and
  2. ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set.
A pg case is never skipped. The PG-less ``checks`` job generates none. The sentinel
(``test_pg_sentinel.py``) fails when both conditions hold and fewer than ``MIN_PG_CASES`` pg
cases were collected; ``pytest_collection_modifyitems`` below counts them.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE, LedgerEnv, build_fake_env

PG_URL_ENV = "MIA_OS_LEDGER_TEST_POSTGRES_URL"
_COUNTS_KEY = pytest.StashKey[dict]()


def core_module_present() -> bool:
    try:
        return importlib.util.find_spec("app.services.os_ledger.core") is not None
    except (ImportError, ValueError):
        return False


def pg_enabled() -> bool:
    return core_module_present() and bool(os.environ.get(PG_URL_ENV))


LEDGER_CASES = ["fake", *(["pg"] if pg_enabled() else [])]


def pytest_collection_modifyitems(config, items):
    counts = {"fake": 0, "pg": 0}
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is None or "conformance" not in item.path.parts:
            continue
        case = callspec.params.get("ledger")
        if case in counts:
            counts[case] += 1
    config.stash[_COUNTS_KEY] = counts


@pytest.fixture
def case_counts(request):
    return request.config.stash[_COUNTS_KEY]


def _prepare(env: LedgerEnv) -> LedgerEnv:
    """The standard workspace: epoch 1, ``core`` active, ``statements`` shadow."""
    env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    return env


def build_pg_env(url: str) -> LedgerEnv:
    """V2-16 FILLS THIS IN. The conformance suite must then pass on PG unchanged.

    Required, in this order (contract §6, §3.4):
      1. apply the V2-14/14b migrations to the disposable database at ``url``;
      2. create the three ledger roles and apply ``role_policy.ledger_grant_statements()``
         directly. NEVER call ``provision_ledger`` (its targets are pinned to 127.0.0.1:56215);
      3. create a demoted test main role so the §5.5 guard passes, and connect as the runtime role;
      4. build ``core.build_ledger(settings)`` where ``settings`` reads the ``Knobs``;
      5. return a ``LedgerEnv`` whose ``rows``/``version``/``now``/``advance`` run on the
         privileged DSN (``advance`` shifts the clock the ledger uses for ``tx.now``).
    ``role_policy`` lands with V2-15 and is absent on this base, so it is imported lazily here.
    """
    from app.services.os_ledger import role_policy  # noqa: F401  (lazy: absent before V2-15)

    raise NotImplementedError("V2-16 implements the pg LedgerEnv (see this docstring)")


@pytest.fixture(params=LEDGER_CASES)
def ledger(request) -> LedgerEnv:
    if request.param == "fake":
        return _prepare(build_fake_env())
    if request.param == "pg":
        return _prepare(build_pg_env(os.environ[PG_URL_ENV]))
    raise AssertionError("unknown ledger case")
