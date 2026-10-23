"""The importer belongs to the import job: a WORKER minted for another job is refused (V2-30b).

``_require_worker`` used to check only the channel, so a retention or mirror worker context could
stage and promote. It now also pins the job scope, exactly as
``authorize.for_worker("import", ...)`` mints it. Runs on the FakeLedger and, with
``MIA_OS_LEDGER_TEST_POSTGRES_URL`` set, on PostgreSQL.
"""

import pytest
from app.core.config import MiaEnv, Settings
from app.services.os_ledger import api, authorize
from app.services.os_ledger.import_workbook import (
    manifest_digest,
    persist_staged_batch,
    prepare,
    stage,
)
from app.services.os_ledger.promote import promote

from tests.fakes.contexts import for_worker, make_ctx
from tests.fakes.os_ledger_builders import fresh_workspace
from tests.fakes.os_ledger_env import OWNER_ID
from tests.fixtures.workbooks.staging import IMPORT_WS, snapshot, workbook, worker
from tests.fixtures.workbooks.synthetic import synthetic_tables

OTHER_JOBS = ("mirror", "verifier", "authority", "retention")


@pytest.fixture
def shadow(ledger):
    fresh_workspace(ledger, IMPORT_WS)
    return ledger


def refused_job(call):
    with pytest.raises(api.LedgerError) as caught:
        call()
    assert (caught.value.code, caught.value.reason) == ("scope_denied", "worker_job_denied")
    assert caught.value.write_outcome == "not_started"


def minted(job):
    settings = Settings(
        _env_file=None, env=MiaEnv.TEST, telegram_owner_user_ids=OWNER_ID, kill_switch=False,
        assaf_os_workspace_id=IMPORT_WS, assaf_os_authority_epoch=1,
        assaf_os_backend="postgres", assaf_os_ledger_enabled=True,
    )
    return authorize.for_worker(job, settings)


def test_the_context_the_production_constructor_mints_for_the_import_job_is_accepted(
    shadow, tmp_path
):
    ctx = minted("import")
    assert ctx.channel == "WORKER" and ctx.scopes == frozenset({"import"})
    book = workbook(tmp_path, synthetic_tables())
    result = stage(shadow, ctx, book)
    assert result.status == "validated"
    out = promote(shadow, ctx, result.batch, result.manifest_digest)
    assert out.created == 6 and out.replayed is False


@pytest.mark.parametrize("job", OTHER_JOBS)
def test_a_worker_minted_for_another_job_cannot_stage_persist_or_promote(shadow, tmp_path, job):
    book = workbook(tmp_path, synthetic_tables())
    staged = stage(shadow, worker(), book)  # the import worker is allowed
    other = minted(job)
    assert other.channel == "WORKER" and other.scopes == frozenset({job})
    digest = manifest_digest(staged.batch)
    before = snapshot(shadow)
    refused_job(lambda: stage(shadow, other, book))
    refused_job(lambda: persist_staged_batch(shadow, other, staged.batch))
    refused_job(lambda: promote(shadow, other, staged.batch, digest))
    assert snapshot(shadow) == before and shadow.rows("os_clients") == []
    # The refusal is about the job, not the batch: the import worker still promotes it.
    assert promote(shadow, worker(), staged.batch, digest).created == 6


@pytest.mark.parametrize("scopes", [
    frozenset(), frozenset({"retention"}), frozenset({"import", "retention"}),
    frozenset({"assaf-os:direct"}),
])
def test_only_exactly_the_import_scope_passes(shadow, tmp_path, scopes):
    book = workbook(tmp_path, synthetic_tables())
    ctx = make_ctx("WORKER", workspace=IMPORT_WS, scopes=scopes)
    refused_job(lambda: stage(shadow, ctx, book))
    batch = prepare(book, workspace_id=IMPORT_WS, authority_epoch=1)
    refused_job(lambda: persist_staged_batch(shadow, ctx, batch))
    refused_job(lambda: promote(shadow, ctx, batch, manifest_digest(batch)))
    assert shadow.rows("os_import_batches") == []


def test_the_channel_check_still_comes_first(shadow, tmp_path):
    book = workbook(tmp_path, synthetic_tables())
    for channel in ("OS", "MIA", "TELEGRAM_APPROVAL"):
        ctx = make_ctx(channel, workspace=IMPORT_WS, scopes=frozenset({"import"}))
        with pytest.raises(api.LedgerError) as caught:
            stage(shadow, ctx, book)
        assert (caught.value.code, caught.value.reason) == (
            "channel_operation_denied", "import_requires_worker")
    # A tests-only context for another purpose is a WORKER too, so it meets the scope check.
    refused_job(lambda: stage(shadow, for_worker("retention", workspace=IMPORT_WS), book))
