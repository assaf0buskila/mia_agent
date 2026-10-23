"""The os-import job's write modes: STAGE and PROMOTE (V2-31b, the V2-30 obligation).

``scripts/os_import.py`` was a dry run. With ``--stage`` it calls ``import_workbook.stage`` and with
``--promote DIGEST`` it calls ``promote.promote`` with the owner's approved manifest digest. Each
mode builds its context with the real ``authorize.for_worker("import", settings)`` and calls
``authorize.require(ctx, "import_promote")`` before it touches the ledger. Runs on the FakeLedger
and, when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set, on the V2-16 PostgreSQL ledger (the ``pg``
case of the ``ledger`` fixture). The workbooks are synthetic; no cell value, name, amount, id or
path may reach the output.
"""

import re
from contextlib import contextmanager

import pytest
from app.core.config import MiaEnv, Settings
from app.services.os_ledger import api, authorize

from tests.fakes.os_ledger_builders import fresh_workspace
from tests.fakes.os_ledger_env import OWNER_ID
from tests.fixtures.workbooks.staging import IMPORT_WS, snapshot
from tests.fixtures.workbooks.synthetic import (
    synthetic_records,
    synthetic_tables,
    write_book,
)

DIGEST = re.compile(r"digest=([0-9a-f]{64})\n")
RECORD_TABLES = ("os_clients", "os_works", "os_payments", "os_time", "os_expenses", "os_tasks",
                 "os_leads")
CLIENT_NAME_COLUMN = 1  # the synthetic clients tab: person_id, name, ...


@pytest.fixture
def shadow(ledger):
    fresh_workspace(ledger, IMPORT_WS)
    return ledger


@pytest.fixture
def cli():
    from scripts import os_import

    return os_import


def job_settings(*, owners=OWNER_ID, epoch=1, workspace=IMPORT_WS):
    """The real ``Settings`` the job reads: one owner, the workspace and the epoch."""
    return Settings(
        _env_file=None, env=MiaEnv.TEST, telegram_owner_user_ids=owners, kill_switch=False,
        assaf_os_workspace_id=workspace, assaf_os_authority_epoch=epoch,
        assaf_os_backend="postgres", assaf_os_ledger_enabled=True,
    )


class PgShaped:
    """The ledger under test, with the one difference the FakeLedger has from PostgreSQL erased.

    The fake reports a caller-visible invariant as ``AssertionError("fake_invariant:<name>")``;
    the PostgreSQL ledger reports it as ``ledger_integrity_failed`` with the invariant's name as
    the reason (tx.py). The ``pg`` case passes straight through.
    """

    def __init__(self, env, log=None):
        self.env = env
        self.calls = []
        self.log = [] if log is None else log  # shared with a test that wants the call order

    @contextmanager
    def transaction(self, ctx, **kwargs):
        self.calls.append(kwargs.get("kind"))
        self.log.append(("transaction", kwargs.get("kind")))
        try:
            with self.env.transaction(ctx, **kwargs) as tx:
                yield tx
        except AssertionError as exc:
            name = str(exc).removeprefix("fake_invariant:")
            if self.env.kind != "fake" or name == str(exc):
                raise
            raise api.LedgerError("ledger_integrity_failed", reason=name) from None


class CollidesOnce(PgShaped):
    """The first import transaction fails with ``error`` before it writes; later ones are real."""

    def __init__(self, env, error, log=None):
        super().__init__(env, log)
        self.error = error

    @contextmanager
    def transaction(self, ctx, **kwargs):
        if not self.calls:
            self.calls.append(kwargs.get("kind"))
            self.log.append(("transaction", kwargs.get("kind")))
            raise self.error
        with super().transaction(ctx, **kwargs) as tx:
            yield tx


class Broken:
    """A ledger that fails the transaction with one chosen error (nothing is ever written)."""

    def __init__(self, error):
        self.error = error

    @contextmanager
    def transaction(self, ctx, **kwargs):
        raise self.error
        yield


def run(cli, env, argv, *, settings=None, ledger=None):
    deps = cli.Deps(settings=settings or job_settings(), ledger=ledger or PgShaped(env))
    return cli.main(argv, deps_factory=lambda: deps)


def book(tmp_path, tables=None, name="capture.xlsx"):
    return str(write_book(tmp_path / name, tables or synthetic_tables()))


def args(path, *extra, epoch=1, workspace=IMPORT_WS):
    return [path, "--workspace", workspace, "--epoch", str(epoch), *extra]


def dry_digest(cli, path, capsys, *extra):
    assert cli.main(args(path, *extra)) == 0
    return DIGEST.search(capsys.readouterr().out)[1]


def batches(env):
    return env.rows("os_import_batches")


def leaks(text, tmp_path):
    """Every string the importer must never print: values, names, ids, paths, the workspace."""
    secrets = {IMPORT_WS, str(tmp_path), "Synthetic only", "Synthetic work", "capture.xlsx"}
    for records in synthetic_records().values():
        for record in records:
            secrets.update(v for v in record.values() if isinstance(v, str) and len(v) > 3)
    return sorted(s for s in secrets if s in text)


# ---- stage, then promote, end to end ------------------------------------------------------------


def test_stage_then_promote_end_to_end(shadow, cli, tmp_path, capsys):
    path = book(tmp_path)
    digest = dry_digest(cli, path, capsys)
    before = snapshot(shadow)

    assert run(cli, shadow, args(path, "--stage")) == 0
    staged = capsys.readouterr()
    assert staged.out == (
        "os_import op=stage outcome=ok reason=persisted rows=6 quarantined=0 issues=0\n")
    assert staged.err == ""
    (batch,) = batches(shadow)
    assert batch["status"] == "validated" and batch["authority_epoch"] == 1
    after_stage = snapshot(shadow)
    # Staging writes the import tables only: no record, proposal, receipt, event or outbox row.
    assert after_stage["os_import_batches"] == before["os_import_batches"] + 1
    assert after_stage["os_import_rows"] == before["os_import_rows"] + 6
    for table in (*RECORD_TABLES, "os_proposals", "os_receipts", "os_events", "os_outbox"):
        assert shadow.rows(table) == [], table

    assert run(cli, shadow, args(path, "--promote", digest)) == 0
    promoted = capsys.readouterr()
    created = sum(len(shadow.rows(table)) for table in RECORD_TABLES)
    assert created == 6
    assert promoted.out == (
        f"os_import op=promote outcome=ok reason=committed created={created} updated=0 "
        "unchanged=0 cancelled=0\n")
    assert promoted.err == ""
    assert [b["status"] for b in batches(shadow)] == ["imported"]
    assert len(shadow.rows("os_receipts")) == 1 and len(shadow.rows("os_proposals")) == 1
    assert shadow.version(IMPORT_WS) > after_stage["version"]

    # Promote is idempotent within the epoch: the second call replays and writes nothing.
    again = snapshot(shadow)
    assert run(cli, shadow, args(path, "--promote", digest)) == 0
    replay = capsys.readouterr()
    assert replay.out == (
        "os_import op=promote outcome=ok reason=replayed created=0 updated=0 unchanged=0 "
        "cancelled=0\n")
    assert snapshot(shadow) == again


def test_promote_carries_the_signed_cancellations(shadow, cli, tmp_path, capsys):
    import json

    path = book(tmp_path)
    cancel = tmp_path / "cancel.json"
    cancel.write_text(json.dumps([{"entity": "tasks", "record_id": "TAS_NOT_THERE"}]),
                      encoding="utf-8")
    digest = dry_digest(cli, path, capsys, "--cancellations", str(cancel))
    assert run(cli, shadow, args(path, "--stage")) == 0
    capsys.readouterr()
    before = snapshot(shadow)
    # The signed manifest lists an id the ledger does not hold: a stale cancellation is refused,
    # which proves the file reached ``promote`` and that it wrote nothing.
    assert run(cli, shadow, args(path, "--promote", digest, "--cancellations", str(cancel))) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == (
        "os_import op=promote outcome=refused reason=validation_error "
        "detail=cancellation_not_live\n")
    assert snapshot(shadow) == before


def test_a_quarantined_workbook_stages_as_a_quarantined_batch_and_exits_2(
    shadow, cli, tmp_path, capsys
):
    from app.prototypes import assaf_os_export_provenance as oracle

    fields = [field["key"] for field in oracle.NATIVE_FIELDS["clients"]]
    tables = synthetic_tables()
    tables["clients"][1][fields.index("name")] = "Unaccepted edit"
    path = book(tmp_path, tables)
    assert run(cli, shadow, args(path, "--stage")) == 2
    staged = capsys.readouterr()
    assert staged.err == ""
    assert staged.out.splitlines()[0] == (
        "os_import op=stage outcome=quarantined reason=persisted rows=6 quarantined=1 issues=1")
    assert "os_import op=stage_reason outcome=quarantined reason=accepted_input_changed rows=1" in (
        staged.out.splitlines())
    assert leaks(staged.out, tmp_path) == [] and "Unaccepted edit" not in staged.out
    (batch,) = batches(shadow)
    assert batch["status"] == "quarantined" and batch["completed_at"] is not None
    # A quarantined batch can never be promoted.
    before = snapshot(shadow)
    # The quarantine refusal is pure: it comes before the digest is compared or the ledger read.
    assert run(cli, shadow, args(path, "--promote", "0" * 64)) == 1
    refusal = capsys.readouterr()
    assert refusal.err == (
        "os_import op=promote outcome=refused reason=import_quarantine_unresolved count=2\n")
    assert snapshot(shadow) == before


# ---- a re-stage at the same epoch is already staged ---------------------------------------------


def test_a_same_epoch_restage_reports_already_staged_and_writes_nothing(
    shadow, cli, tmp_path, capsys
):
    path = book(tmp_path)
    assert run(cli, shadow, args(path, "--stage")) == 0
    capsys.readouterr()
    before = snapshot(shadow)
    assert run(cli, shadow, args(path, "--stage")) == 0
    again = capsys.readouterr()
    assert again.out == (
        "os_import op=stage outcome=ok reason=already_staged rows=6 quarantined=0 issues=0 "
        "batch=validated_or_imported\n")
    assert again.err == ""
    assert snapshot(shadow) == before
    assert [b["status"] for b in batches(shadow)] == ["validated"]


def test_a_restage_after_the_promote_does_not_claim_the_batch_is_validated(
    shadow, cli, tmp_path, capsys
):
    path = book(tmp_path)
    digest = dry_digest(cli, path, capsys)
    assert run(cli, shadow, args(path, "--stage")) == 0
    assert run(cli, shadow, args(path, "--promote", digest)) == 0
    capsys.readouterr()
    assert [b["status"] for b in batches(shadow)] == ["imported"]
    before = snapshot(shadow)
    ledger = PgShaped(shadow)
    assert run(cli, shadow, args(path, "--stage"), ledger=ledger) == 0
    again = capsys.readouterr()
    # The ledger has no batch reader (LedgerTx), so the CLI proves the settled row is there and
    # names what the workbook and the row together allow: never a "validated" it cannot know.
    assert again.out == (
        "os_import op=stage outcome=ok reason=already_staged rows=6 quarantined=0 issues=0 "
        "batch=validated_or_imported\n")
    assert "batch=validated " not in again.out and again.err == ""
    assert ledger.calls == ["import", "import"], "the stage, then the read-only row probe"
    assert snapshot(shadow) == before
    assert [b["status"] for b in batches(shadow)] == ["imported"]


def test_restaging_a_quarantined_workbook_is_already_staged_but_still_not_promotable(
    shadow, cli, tmp_path, capsys
):
    from app.prototypes import assaf_os_export_provenance as oracle

    fields = [field["key"] for field in oracle.NATIVE_FIELDS["clients"]]
    tables = synthetic_tables()
    tables["clients"][1][fields.index("name")] = "Unaccepted edit"
    path = book(tmp_path, tables)
    assert run(cli, shadow, args(path, "--stage")) == 2
    capsys.readouterr()
    before = snapshot(shadow)
    # The first stage said exit 2; saying exit 0 now would turn a quarantined batch into a success.
    assert run(cli, shadow, args(path, "--stage")) == 2
    again = capsys.readouterr()
    assert again.out.splitlines()[0] == (
        "os_import op=stage outcome=quarantined reason=already_staged rows=6 quarantined=1 "
        "issues=1 batch=quarantined")
    assert snapshot(shadow) == before


def test_the_next_epoch_stages_the_same_workbook_as_a_new_batch(shadow, cli, tmp_path, capsys):
    from tests.fixtures.workbooks.staging import next_epoch

    path = book(tmp_path)
    assert run(cli, shadow, args(path, "--stage")) == 0
    next_epoch(shadow, epoch=1)
    capsys.readouterr()
    assert run(cli, shadow, args(path, "--stage", epoch=2),
               settings=job_settings(epoch=2)) == 0
    out = capsys.readouterr().out
    assert "reason=persisted" in out and "already_staged" not in out
    assert sorted(b["authority_epoch"] for b in batches(shadow)) == [1, 2]


@pytest.mark.parametrize("reason", ["import_batch_pk", "integrity_23505"])
def test_both_spellings_of_the_batch_key_collision_are_already_staged_when_the_row_is_there(
    shadow, cli, tmp_path, capsys, reason
):
    path = book(tmp_path)
    assert run(cli, shadow, args(path, "--stage")) == 0
    capsys.readouterr()
    before = snapshot(shadow)
    ledger = CollidesOnce(shadow, api.LedgerError("ledger_integrity_failed", reason=reason))
    assert run(cli, shadow, args(path, "--stage"), ledger=ledger) == 0
    assert capsys.readouterr().out == (
        "os_import op=stage outcome=ok reason=already_staged rows=6 quarantined=0 issues=0 "
        "batch=validated_or_imported\n")
    assert ledger.calls == ["import", "import"] and snapshot(shadow) == before


@pytest.mark.parametrize("reason", ["import_batch_pk", "integrity_23505"])
def test_a_collision_with_no_matching_batch_row_is_re_raised_with_its_own_code(
    shadow, cli, tmp_path, capsys, reason
):
    """A unique key other than the batch's (a future one) must not read as 'already staged'."""
    path = book(tmp_path)
    before = snapshot(shadow)
    ledger = CollidesOnce(shadow, api.LedgerError("ledger_integrity_failed", reason=reason))
    assert run(cli, shadow, args(path, "--stage"), ledger=ledger) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == (
        f"os_import op=stage outcome=refused reason=ledger_integrity_failed detail={reason}\n")
    assert ledger.calls == ["import", "import"], "the stage, then the row probe that found none"
    assert snapshot(shadow) == before and batches(shadow) == []


def test_the_row_probe_failing_is_a_refusal_with_the_probes_own_code(
    shadow, cli, tmp_path, capsys
):
    path = book(tmp_path)
    collision = api.LedgerError("ledger_integrity_failed", reason="integrity_23505")
    ledger = Broken(collision)  # the stage AND the probe both fail with the collision
    assert run(cli, shadow, args(path, "--stage"), ledger=ledger) == 1
    assert capsys.readouterr().err == (
        "os_import op=stage outcome=refused reason=ledger_integrity_failed "
        "detail=integrity_23505\n")


@pytest.mark.parametrize("error,line", [
    (api.LedgerError("ledger_integrity_failed", reason="import_row_entity"),
     "reason=ledger_integrity_failed detail=import_row_entity"),
    (api.LedgerError("ledger_integrity_failed", reason="integrity_23514"),
     "reason=ledger_integrity_failed detail=integrity_23514"),
    (api.LedgerError("ledger_integrity_failed"), "reason=ledger_integrity_failed"),
    (api.LedgerError("authority_not_active"), "reason=authority_not_active"),
    (api.LedgerError("authority_changed"), "reason=authority_changed"),
    (api.LedgerError("ledger_unavailable", reason="db_error_08006"),
     "reason=ledger_unavailable detail=db_error_08006"),
    (api.LedgerError("unknown"), "reason=unknown"),
])
def test_every_other_ledger_error_keeps_its_code_and_is_nonzero(
    shadow, cli, tmp_path, capsys, error, line
):
    path = book(tmp_path)
    assert run(cli, shadow, args(path, "--stage"), ledger=Broken(error)) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == f"os_import op=stage outcome=refused {line}\n"


def test_an_unexpected_exception_is_an_internal_error_never_a_traceback(
    shadow, cli, tmp_path, capsys
):
    path = book(tmp_path)

    class Exploding:
        def transaction(self, ctx, **kwargs):
            raise RuntimeError(f"driver text with {tmp_path} and Synthetic only")

    assert run(cli, shadow, args(path, "--stage"), ledger=Exploding()) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == "os_import op=stage outcome=refused reason=internal_error\n"


# ---- the worker context: the real authorize module ----------------------------------------------


def test_each_mode_builds_the_import_worker_and_requires_import_promote_first(
    shadow, cli, tmp_path, capsys, monkeypatch
):
    events = []
    real_for_worker, real_require = authorize.for_worker, authorize.require

    def for_worker(job, settings):
        events.append(("for_worker", job))
        return real_for_worker(job, settings)

    def require(ctx, op):
        events.append(("require", op, ctx.channel, ctx.scopes))
        return real_require(ctx, op)

    monkeypatch.setattr(authorize, "for_worker", for_worker)
    monkeypatch.setattr(authorize, "require", require)
    path = book(tmp_path)
    digest = dry_digest(cli, path, capsys)
    ledger = PgShaped(shadow, log=events)
    assert run(cli, shadow, args(path, "--stage"), ledger=ledger) == 0
    assert events == [
        ("for_worker", "import"),
        ("require", "import_promote", "WORKER", frozenset({"import"})),
        ("transaction", "import"),
    ]
    events.clear()
    assert run(cli, shadow, args(path, "--promote", digest), ledger=ledger) == 0
    assert events == [
        ("for_worker", "import"),
        ("require", "import_promote", "WORKER", frozenset({"import"})),
        ("transaction", "import"),
    ]


@pytest.mark.parametrize("mode", ["stage", "promote"])
@pytest.mark.parametrize("job", ["retention", "mirror", "verifier", "authority"])
def test_a_context_for_the_wrong_worker_job_is_refused_and_nothing_is_written(
    shadow, cli, tmp_path, capsys, monkeypatch, mode, job
):
    path = book(tmp_path)
    digest = dry_digest(cli, path, capsys)
    real_for_worker = authorize.for_worker
    # A wiring slip that minted another job's worker: ``require`` is the gate that stops it.
    monkeypatch.setattr(
        authorize, "for_worker", lambda _job, settings: real_for_worker(job, settings))
    before = snapshot(shadow)
    ledger = PgShaped(shadow)
    mode_args = ["--stage"] if mode == "stage" else ["--promote", digest]
    assert run(cli, shadow, args(path, *mode_args), ledger=ledger) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == (
        f"os_import op={mode} outcome=refused reason=scope_denied detail=worker_job_denied\n")
    assert ledger.calls == [] and snapshot(shadow) == before


@pytest.mark.parametrize("mode", ["stage", "promote"])
def test_a_second_owner_in_the_allow_list_refuses_the_worker_before_any_write(
    shadow, cli, tmp_path, capsys, mode
):
    path = book(tmp_path)
    digest = dry_digest(cli, path, capsys)
    before = snapshot(shadow)
    ledger = PgShaped(shadow)
    mode_args = ["--stage"] if mode == "stage" else ["--promote", digest]
    settings = job_settings(owners=f"{OWNER_ID},456")
    assert run(cli, shadow, args(path, *mode_args), settings=settings, ledger=ledger) == 1
    assert capsys.readouterr().err == (
        f"os_import op={mode} outcome=refused reason=ledger_unavailable "
        "detail=worker_owner_not_single\n")
    assert ledger.calls == [] and snapshot(shadow) == before


# ---- nothing is written on any refusal ----------------------------------------------------------


def test_a_workspace_or_epoch_that_differs_from_the_context_is_refused_before_the_ledger(
    shadow, cli, tmp_path, capsys
):
    path = book(tmp_path)
    before = snapshot(shadow)
    for mode in ("--stage", "--promote"):
        extra = [mode] if mode == "--stage" else [mode, "0" * 64]
        for kwargs in ({"epoch": 2}, {"workspace": "ws-other"}):
            ledger = PgShaped(shadow)
            assert run(cli, shadow, args(path, *extra, **kwargs), ledger=ledger) == 1
            op = "stage" if mode == "--stage" else "promote"
            refusal = capsys.readouterr()
            assert refusal.out == ""
            assert refusal.err == f"os_import op={op} outcome=refused reason=binding_mismatch\n"
            assert ledger.calls == [] and snapshot(shadow) == before


def test_a_digest_the_owner_did_not_approve_promotes_nothing(shadow, cli, tmp_path, capsys):
    path = book(tmp_path)
    assert run(cli, shadow, args(path, "--stage")) == 0
    capsys.readouterr()
    before = snapshot(shadow)
    assert run(cli, shadow, args(path, "--promote", "0" * 64)) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == "os_import op=promote outcome=refused reason=manifest_digest_mismatch\n"
    assert snapshot(shadow) == before and shadow.rows("os_clients") == []
    assert [b["status"] for b in batches(shadow)] == ["validated"]


def test_promoting_a_workbook_that_was_never_staged_writes_nothing(shadow, cli, tmp_path, capsys):
    path = book(tmp_path)
    digest = dry_digest(cli, path, capsys)
    before = snapshot(shadow)
    assert run(cli, shadow, args(path, "--promote", digest)) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err.startswith("os_import op=promote outcome=refused reason=")
    assert refusal.err.count("\n") == 1 and "outcome=ok" not in refusal.err
    assert snapshot(shadow) == before and shadow.rows("os_clients") == []


@pytest.mark.parametrize("mode", ["stage", "promote"])
def test_a_ledger_that_is_not_in_shadow_refuses_authority_not_active(
    ledger, cli, tmp_path, capsys, mode
):
    fresh_workspace(ledger, IMPORT_WS, core="active")
    path = book(tmp_path)
    digest = dry_digest(cli, path, capsys)
    before = snapshot(ledger)
    mode_args = ["--stage"] if mode == "stage" else ["--promote", digest]
    assert run(cli, ledger, args(path, *mode_args)) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == f"os_import op={mode} outcome=refused reason=authority_not_active\n"
    assert snapshot(ledger) == before


@pytest.mark.parametrize("mode", ["stage", "promote"])
def test_an_unreadable_workbook_is_refused_without_touching_the_ledger(
    shadow, cli, tmp_path, capsys, mode
):
    bad = tmp_path / "not-a-book.xlsx"
    bad.write_bytes(b"not a zip file")
    ledger = PgShaped(shadow)
    before = snapshot(shadow)
    mode_args = ["--stage"] if mode == "stage" else ["--promote", "0" * 64]
    assert run(cli, shadow, args(str(bad), *mode_args), ledger=ledger) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == f"os_import op={mode} outcome=refused reason=invalid_xlsx\n"
    assert leaks(refusal.err, tmp_path) == [] and "not-a-book" not in refusal.err
    assert ledger.calls == [] and snapshot(shadow) == before


# ---- arguments, the dry run and the live wiring -------------------------------------------------


@pytest.mark.parametrize("extra", [
    ["--stage", "--promote", "0" * 64],            # one mode at a time
    ["--promote", "not-a-digest"],                 # a sha256 hex digest
    ["--promote", "A" * 64],                       # lower case only, as the dry run prints it
    ["--promote", "0" * 63],
    ["--promote"],
    ["--stage", "--manifest-out", "out.json"],     # the review artifact is a dry-run concern
    ["--promote", "0" * 64, "--manifest-out", "out.json"],
    ["--stage", "--cancellations", "c.json"],      # only promote reads the signed cancellations
])
def test_argument_mistakes_are_refused_before_any_deps_are_built(cli, tmp_path, capsys, extra):
    def deps():
        raise AssertionError("deps built for a refused command line")

    assert cli.main(args(book(tmp_path), *extra), deps_factory=deps) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == "os_import op=stage outcome=refused reason=invalid_arguments\n"


def test_the_dry_run_never_builds_deps(cli, tmp_path, capsys):
    def deps():
        raise AssertionError("the dry run touched settings or the ledger")

    assert cli.main(args(book(tmp_path)), deps_factory=deps) == 0
    out = capsys.readouterr()
    assert out.out.startswith("os_import op=stage outcome=ok reason=dry_run rows=6 ")
    assert out.err == ""


def test_the_live_wiring_loads_the_settings_and_builds_the_ledger_from_them(
    shadow, cli, tmp_path, capsys, monkeypatch
):
    import app.core.config as config

    seen = []
    settings = job_settings()
    ledger = PgShaped(shadow)
    monkeypatch.setattr(config, "get_settings", lambda: seen.append("settings") or settings)
    monkeypatch.setattr(
        api, "build_ledger", lambda given: seen.append(given) or ledger)
    assert cli.main(args(book(tmp_path), "--stage")) == 0
    assert seen == ["settings", settings]
    assert capsys.readouterr().out.startswith("os_import op=stage outcome=ok reason=persisted")


def test_a_ledger_that_cannot_be_built_is_a_refusal_not_a_traceback(
    cli, tmp_path, capsys, monkeypatch
):
    import app.core.config as config

    monkeypatch.setattr(config, "get_settings", lambda: job_settings())

    def unbuilt(_settings):
        raise api.LedgerError("ledger_unavailable", reason="ledger_dsn_missing")

    monkeypatch.setattr(api, "build_ledger", unbuilt)
    assert cli.main(args(book(tmp_path), "--stage")) == 1
    refusal = capsys.readouterr()
    assert refusal.out == ""
    assert refusal.err == (
        "os_import op=stage outcome=refused reason=ledger_unavailable detail=ledger_dsn_missing\n")


def test_the_runbooks_os_import_commands_parse_with_the_real_parser(cli):
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    flat = re.sub(r"\s+", " ", (root / "ops/assaf-os/CUTOVER_RUNBOOK.md").read_text("utf-8"))
    commands = re.findall(r"`sudo mia os-job os-import ([^`]+)`", flat)
    assert len(commands) == 2, commands  # G24 stages, G12 promotes

    def parse(command, *extra):
        tokens = [{"W": "ws-1", "E": "1", "<digest>": "0" * 64}.get(
            token, token.replace("<capture>", "capture").replace("<cancellations>", "cancel"))
            for token in command.split()]
        return cli._parser().parse_args([*tokens, *extra])

    staged = parse(commands[0])
    assert staged.stage is True and staged.promote is None
    assert (staged.workspace, staged.epoch) == ("ws-1", 1)
    assert str(staged.workbook).replace("\\", "/") == "/var/lib/mia/os-import/capture.xlsx"
    promoted = parse(commands[1], "--cancellations", "/var/lib/mia/os-import/cancel.json")
    assert promoted.stage is False and promoted.promote == "0" * 64
    assert (promoted.workspace, promoted.epoch) == ("ws-1", 1)
    cli._check_modes(staged)
    cli._check_modes(promoted)


def test_no_output_line_carries_a_value_a_name_an_id_or_a_path(shadow, cli, tmp_path, capsys):
    path = book(tmp_path)
    digest = dry_digest(cli, path, capsys)
    seen = []
    for mode_args in (["--stage"], ["--stage"], ["--promote", digest], ["--promote", digest],
                      ["--promote", "0" * 64]):
        run(cli, shadow, args(path, *mode_args))
        output = capsys.readouterr()
        seen.append(output.out + output.err)
    text = "".join(seen)
    assert text and leaks(text, tmp_path) == []
