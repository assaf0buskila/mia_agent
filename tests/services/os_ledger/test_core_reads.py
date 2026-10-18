"""The V2-16 readers' cursors and read-only snapshots (§8; V2-18 owns the optimised readers).

A cursor is opaque and HMAC-signed per workspace over {listing, entity, epoch, version, offset}:
forged, malformed or foreign is ``invalid_cursor``; another epoch or version is
``cursor_invalidated`` (both pinned by conformance on PG). Reads never write.
"""

import os

import pytest
from app.services.os_ledger import api
from app.services.os_ledger.core import _Cursors

from tests.fakes.os_ledger_builders import commit_direct, ctx, propose_task, rid, task
from tests.services.os_ledger.conftest import PG_URL_ENV, _prepare, build_pg_env, pg_enabled

SECRET = "s" * 32


def invalid(call):
    with pytest.raises(api.LedgerError, match="^invalid_cursor$"):
        call()


def test_a_cursor_round_trips_only_for_its_workspace_listing_and_entity():
    codec = _Cursors(SECRET)
    token = codec.encode("ws", "page", "tasks", 1, 7, 50)
    assert codec.decode(token, "ws", "page", "tasks") == (1, 7, 50)
    invalid(lambda: codec.decode(token, "other", "page", "tasks"))
    invalid(lambda: codec.decode(token, "ws", "deleted", "tasks"))
    invalid(lambda: codec.decode(token, "ws", "page", "clients"))


@pytest.mark.parametrize("mutate", [
    lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),  # the tag
    lambda t: t.replace("c1.", "c2.", 1),
    lambda t: "c1." + t.split(".")[1][:-2] + "AA." + t.split(".")[2],  # the payload
    lambda t: t + "=",
    lambda t: None,
    lambda t: 12,
])
def test_a_tampered_or_malformed_cursor_is_invalid(mutate):
    codec = _Cursors(SECRET)
    token = codec.encode("ws", "page", "tasks", 1, 7, 50)
    invalid(lambda: codec.decode(mutate(token), "ws", "page", "tasks"))


def test_the_key_is_the_configured_secret_or_a_process_key():
    token = _Cursors(SECRET).encode("ws", "page", "tasks", 1, 7, 2)
    assert _Cursors(SECRET).decode(token, "ws", "page", "tasks") == (1, 7, 2)
    invalid(lambda: _Cursors("t" * 32).decode(token, "ws", "page", "tasks"))
    short = _Cursors("too-short").encode("ws", "page", "tasks", 1, 7, 2)
    assert _Cursors("").decode(short, "ws", "page", "tasks") == (1, 7, 2)  # the process key


@pytest.fixture
def pg(request):
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    return _prepare(build_pg_env(os.environ[PG_URL_ENV]))


def test_every_reader_writes_no_row(pg):
    commit_direct(pg, ctx(), [task("TAS_a"), task("TAS_b")])
    proposal = propose_task(pg)
    ledger = pg.transaction.__self__
    with ledger._read(ctx()) as tx:  # every reader runs in this kind of snapshot
        assert tx._exec("SHOW transaction_read_only").scalar() == "on"
        with pytest.raises(api.LedgerError) as caught:  # PostgreSQL itself refuses a write
            tx._exec("UPDATE assaf_os.os_workspaces SET version = version")
        assert caught.value.reason == "db_error_25006"  # read_only_sql_transaction
    for read in (lambda: pg.head(ctx()), lambda: pg.page(ctx(), "tasks", after=None, limit=1),
                 lambda: pg.deleted(ctx(), "tasks", after=None, limit=5),
                 lambda: pg.pending(ctx(), after=None, limit=5),
                 lambda: pg.labels(ctx(), [api.RecordRef("tasks", "TAS_a")]),
                 lambda: pg.monthly_summary(ctx()),
                 lambda: pg.receipt(ctx(), rid()),
                 lambda: pg.creation_request(ctx(), proposal.request_id)):
        before = pg.rows("os_workspaces")
        read()
        assert pg.rows("os_workspaces") == before
