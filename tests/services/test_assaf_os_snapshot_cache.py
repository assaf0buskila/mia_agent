"""V2-42: the server-side cache of the last validated Assaf OS snapshot (contract section 1).

The cache holds one row per owner and workspace. A row is written only from a snapshot the port
has already validated, read only after every binding, digest, shape and age check passes, and a
failed check is always a miss, never an error. The cache is never a source of truth.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.core.config import Settings
from app.db import models as _models  # noqa: F401
from app.db import reminders as _reminders  # noqa: F401
from app.db import site_v2 as _site_v2  # noqa: F401
from app.db.base import Base
from app.db.migrate import POSTGRES_ONLY, applied_schema_version, apply_migrations
from app.db.session import make_engine
from app.services import assaf_os_snapshot_cache as cache
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.services import snapshot_cache_support as support
from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

OWNER = auth.OWNER_ID
OTHER_OWNER = "87654321"
SENTINEL = "ZZ-SENTINEL-4d1e9b"
LOGGER = "mia.os_snapshot_cache"
LINE = re.compile(
    r"os_snapshot_cache op=(read|write) outcome=(hit|miss|stored|skipped|failed) "
    r"reason=(hit|no_row|binding_mismatch|digest_mismatch|invalid_shape|too_old|future_as_of"
    r"|stored|not_newer|too_large|db_unavailable)"
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config() -> Settings:
    value = assaf.settings()
    value.telegram_owner_user_ids = f"{OWNER},{OTHER_OWNER}"
    return value


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cache.db'}")
    support.apply_migration(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cache, "get_session_factory", lambda: factory)
    yield factory
    engine.dispose()


@pytest.fixture
def now(monkeypatch) -> datetime:
    """Pin the module clock to the real one at test start, so the exact validator still agrees."""
    moment = datetime.now(UTC)
    monkeypatch.setattr(cache, "_now", lambda: moment)
    return moment


def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def _snapshot(config, when: datetime | None = None, **kwargs):
    return support.snapshot_at(assaf._snapshot(config), when or datetime.now(UTC), **kwargs)


def _remember(config, snapshot, owner: str = OWNER) -> None:
    assert cache.remember_snapshot(config, owner, snapshot) is None


def _row(db, config, owner: str = OWNER):
    return support.read_row(db, owner, config.assaf_os_workspace_id)


# ---------------------------------------------------------------------------- the migration


def test_migration_creates_the_contract_table_and_is_rerunnable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    support.apply_migration(engine, times=2)
    columns = {column["name"]: column for column in inspect(engine).get_columns(support.TABLE)}
    assert tuple(columns) == support.COLUMNS
    assert all(column["nullable"] is False for column in columns.values())
    assert inspect(engine).get_pk_constraint(support.TABLE)["constrained_columns"] == [
        "owner_id",
        "workspace_id",
    ]
    engine.dispose()


def test_migration_check_constraints_enforce_the_contract_bounds(db, config):
    base = _snapshot(config)
    for column, bad in (
        ("mode", "STAGING"),
        ("byte_length", 1),
        ("byte_length", support.MAX_CACHED_BYTES + 1),
        ("version", 0),
    ):
        with pytest.raises(IntegrityError):
            support.seed(db, config, OWNER, base, **{column: bad})
    assert support.row_count(db) == 0
    for column, good in (("mode", "LIVE"), ("byte_length", 2), ("byte_length", 261_120)):
        support.seed(db, config, OWNER, base, **{column: good})
    assert support.row_count(db) == 1


def test_migration_is_portable_ungranted_and_listed_after_the_others(tmp_path):
    assert support.MIGRATION_NAME not in POSTGRES_ONLY
    body = support.MIGRATION.read_bytes()
    assert b"\r" not in body and body.endswith(b"\n")
    sql = body.decode("utf-8").upper()
    assert "GRANT" not in sql and "REVOKE" not in sql and "AUTOINCREMENT" not in sql
    names = sorted(path.name for path in (ROOT / "migrations").glob("*.sql"))
    assert support.MIGRATION_NAME in names
    engine = make_engine(f"sqlite:///{tmp_path / 'full.db'}")
    Base.metadata.create_all(bind=engine)
    summary = apply_migrations(engine)
    assert summary.failed == ""
    assert support.MIGRATION_NAME in summary.applied
    assert applied_schema_version(engine) >= support.MIGRATION_NAME
    again = apply_migrations(engine)
    assert again.failed == "" and again.applied == []
    engine.dispose()


def test_only_the_migration_and_the_cache_module_name_the_table():
    named: list[str] = []
    callers: list[str] = []
    for folder in ("app", "migrations", "ops", "scripts", "integrations"):
        for path in sorted((ROOT / folder).rglob("*")):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".sql",
                ".sh",
                ".yaml",
                ".yml",
                ".toml",
                ".json",
                ".md",
                ".txt",
            }:
                continue
            text_body = path.read_text(encoding="utf-8", errors="replace")
            if support.TABLE in text_body:
                named.append(path.relative_to(ROOT).as_posix())
            if folder == "app" and "remember_snapshot" in text_body:
                callers.append(path.relative_to(ROOT).as_posix())
    assert sorted(named) == sorted(
        [f"migrations/{support.MIGRATION_NAME}", "app/services/assaf_os_snapshot_cache.py"]
    )
    # The one writer is the module and its one caller is the snapshot route.
    assert sorted(callers) == ["app/api/assaf_os.py", "app/services/assaf_os_snapshot_cache.py"]


# ----------------------------------------------------------------- write, then read


def test_a_remembered_snapshot_is_read_back_inside_the_contract_envelope(db, config, now):
    snapshot = _snapshot(config, now - timedelta(seconds=137, milliseconds=900), zulu=True)
    _remember(config, snapshot)

    hit = cache.read_cached_snapshot(config, OWNER)

    assert list(hit) == [
        "api_version",
        "source",
        "mode",
        "workbook_id",
        "workspace_id",
        "cached",
        "as_of",
        "age_seconds",
        "cache_version",
        "snapshot",
    ]
    assert hit["api_version"] == "assaf-os.v1" and hit["source"] == "google_sheets"
    assert hit["mode"] == config.assaf_os_mode
    assert hit["workbook_id"] == config.assaf_os_workbook_id
    assert hit["workspace_id"] == config.assaf_os_workspace_id
    assert hit["cached"] is True
    assert hit["as_of"] == snapshot["observed_at"], "verbatim, including the Z suffix"
    assert hit["age_seconds"] == 137, "floor of server time minus as_of"
    assert hit["cache_version"] == 1
    assert hit["snapshot"] == snapshot


def test_the_stored_row_follows_the_contract(db, config, now):
    snapshot = _snapshot(config, now - timedelta(seconds=20))
    snapshot["observed_at"] = (
        (now - timedelta(seconds=20)).astimezone(timezone(timedelta(hours=2))).isoformat()
    )
    assert snapshot["observed_at"].endswith("+02:00")
    snapshot["issues"] = ["שלום"]
    _remember(config, snapshot)

    row = _row(db, config)
    raw = support.canonical(snapshot)
    encoded = raw.encode("utf-8")
    assert row["snapshot_json"] == raw
    assert row["snapshot_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert row["byte_length"] == len(encoded)
    assert row["binding_hash"] == support.config_binding(config, OWNER)
    assert row["binding_hash"] == cache.binding_hash(config, OWNER)
    assert re.fullmatch(r"[0-9a-f]{64}", row["binding_hash"])
    assert row["owner_id"] == OWNER and row["workspace_id"] == config.assaf_os_workspace_id
    assert row["workbook_id"] == config.assaf_os_workbook_id
    assert (row["mode"], row["api_version"], row["schema_version"]) == (
        config.assaf_os_mode,
        "assaf-os.v1",
        "assaf-os-unified.v1",
    )
    assert support.as_utc(row["as_of"]) == support.parse_time(snapshot["observed_at"])
    assert abs((support.as_utc(row["stored_at"]) - now).total_seconds()) < 1
    assert row["version"] == 1
    # The hit still returns observed_at exactly as the Script sent it.
    assert cache.read_cached_snapshot(config, OWNER)["as_of"] == snapshot["observed_at"]


def test_the_binding_hash_is_the_contract_hmac_over_compact_json(config):
    assert cache.binding_hash(config, OWNER) == support.config_binding(config, OWNER)
    rotated = support.config_binding(
        config, OWNER, secret="a-different-secret-of-32-bytes-or-more"
    )
    assert cache.binding_hash(config, OWNER) != rotated
    assert cache.MAX_CACHED_BYTES == 261_120 == support.MAX_CACHED_BYTES
    assert cache.MAX_AGE_SECONDS == support.SEVEN_DAYS
    assert cache.MAX_FUTURE_SECONDS == 300


def test_a_newer_snapshot_replaces_the_row_and_bumps_the_version(db, config, now):
    first = _snapshot(config, now - timedelta(seconds=60), marker="first")
    second = _snapshot(config, now - timedelta(seconds=30), marker="second")
    _remember(config, first)
    _remember(config, second)
    row = _row(db, config)
    assert row["version"] == 2 and support.canonical(second) == row["snapshot_json"]
    assert support.row_count(db) == 1, "one row per owner and workspace, overwritten in place"
    assert cache.read_cached_snapshot(config, OWNER)["cache_version"] == 2


@pytest.mark.parametrize("delta", [-30, 0])
def test_an_older_or_equal_snapshot_is_not_newer_and_changes_nothing(
    db, config, now, caplog, delta
):
    newest = _snapshot(config, now - timedelta(seconds=30), marker="newest")
    _remember(config, newest)
    before = _row(db, config)
    late = _snapshot(config, now - timedelta(seconds=30 - delta), marker="late")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _remember(config, late)
    assert _row(db, config) == before, "a slower, older snapshot never overwrites a newer one"
    assert _lines(caplog) == ["os_snapshot_cache op=write outcome=skipped reason=not_newer"]


def test_a_changed_binding_replaces_the_row_even_with_an_older_snapshot(db, config, now):
    _remember(config, _snapshot(config, now - timedelta(seconds=10), marker="before-rotation"))
    config.assaf_os_hmac_secret = "rotated-test-secret-with-at-least-32-bytes"
    older = _snapshot(config, now - timedelta(seconds=90), marker="after-rotation")
    _remember(config, older)
    row = _row(db, config)
    assert row["binding_hash"] == support.config_binding(config, OWNER)
    assert row["snapshot_json"] == support.canonical(older) and row["version"] == 2
    assert cache.read_cached_snapshot(config, OWNER)["cached"] is True


def test_rows_are_isolated_by_owner_and_by_workspace(db, config, now):
    mine = _snapshot(config, now - timedelta(seconds=5), marker="mine")
    theirs = _snapshot(config, now - timedelta(seconds=6), marker="theirs")
    _remember(config, mine, OWNER)
    _remember(config, theirs, OTHER_OWNER)
    assert cache.read_cached_snapshot(config, OWNER)["snapshot"]["issues"] == ["mine"]
    assert cache.read_cached_snapshot(config, OTHER_OWNER)["snapshot"]["issues"] == ["theirs"]
    assert support.row_count(db) == 2
    config.telegram_owner_user_ids += ",5555555"
    assert cache.read_cached_snapshot(config, "5555555") == {"cached": False}
    config.assaf_os_workspace_id = "another-workspace"
    assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}


# ----------------------------------------------------- each binding component, changed alone


@pytest.mark.parametrize(
    "component",
    [
        "api_version",
        "schema_version",
        "owner_id",
        "workspace_id",
        "workbook_id",
        "mode",
        "script_url",
        "secret",
    ],
)
def test_a_stored_hash_over_any_one_different_component_is_a_binding_mismatch(
    db, config, now, caplog, component
):
    snapshot = _snapshot(config, now - timedelta(seconds=5))
    changed = {
        "api_version": "assaf-os.v0",
        "schema_version": "assaf-os-unified.v0",
        "owner_id": OTHER_OWNER,
        "workspace_id": "somewhere-else",
        "workbook_id": "workbook_99999999999999999999",
        "mode": "LIVE",
        "script_url": "https://script.google.com/macros/s/" + "z" * 24 + "/exec",
        "secret": "an-old-secret-that-is-at-least-32-bytes",
    }[component]
    if component in {"api_version", "schema_version"}:
        stored = support.binding_hash(
            config.assaf_os_hmac_secret,
            owner_id=OWNER,
            workspace_id=config.assaf_os_workspace_id,
            workbook_id=config.assaf_os_workbook_id,
            mode=config.assaf_os_mode,
            script_url=config.assaf_os_script_url,
            **{component: changed},
        )
    elif component == "owner_id":
        stored = support.config_binding(config, changed)
    else:
        stored = support.config_binding(config, OWNER, **{component: changed})
    assert stored != support.config_binding(config, OWNER)
    support.seed(db, config, OWNER, snapshot, binding_hash=stored)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}
    assert _lines(caplog) == ["os_snapshot_cache op=read outcome=miss reason=binding_mismatch"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("workbook", "binding_mismatch"),
        ("mode", "binding_mismatch"),
        ("script_url", "binding_mismatch"),
        ("secret", "binding_mismatch"),
        ("workspace", "no_row"),
    ],
)
def test_a_configuration_change_after_the_write_is_a_miss(db, config, now, caplog, change, reason):
    _remember(config, _snapshot(config, now - timedelta(seconds=5)))
    assert cache.read_cached_snapshot(config, OWNER)["cached"] is True
    if change == "workbook":
        config.assaf_os_workbook_id = "workbook_98765432109876543210"
    elif change == "mode":
        config.assaf_os_mode = "LIVE"
    elif change == "script_url":
        config.assaf_os_script_url = "https://script.google.com/macros/s/" + "q" * 24 + "/exec"
    elif change == "secret":
        config.assaf_os_hmac_secret = "rotated-test-secret-with-at-least-32-bytes"
    else:
        config.assaf_os_workspace_id = "another-workspace"
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}
    assert _lines(caplog) == [f"os_snapshot_cache op=read outcome=miss reason={reason}"]


@pytest.mark.parametrize("column", ["workbook_id", "mode", "api_version", "schema_version"])
def test_a_row_whose_plain_binding_column_differs_is_a_binding_mismatch(db, config, now, column):
    other = {
        "workbook_id": "workbook_98765432109876543210",
        "mode": "LIVE",
        "api_version": "assaf-os.v0",
        "schema_version": "assaf-os-unified.v0",
    }[column]
    snapshot = _snapshot(config, now - timedelta(seconds=5))
    support.seed(db, config, OWNER, snapshot, **{column: other})
    assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}


# -------------------------------------------------------------------------------- tampering


def _miss_reason(config, caplog) -> str:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}
    (line,) = _lines(caplog)
    match = LINE.fullmatch(line)
    assert match and match.group(1) == "read" and match.group(2) in {"miss", "failed"}, line
    return match.group(3)


def test_a_payload_edited_without_its_digest_is_a_digest_mismatch(db, config, now, caplog):
    original = _snapshot(config, now - timedelta(seconds=5), marker="original")
    swapped = _snapshot(config, now - timedelta(seconds=5), marker="swapped")
    support.seed(db, config, OWNER, original, snapshot_json=support.canonical(swapped))
    assert _miss_reason(config, caplog) == "digest_mismatch"


@pytest.mark.parametrize("column", ["snapshot_sha256", "byte_length"])
def test_a_wrong_digest_or_length_is_a_digest_mismatch(db, config, now, caplog, column):
    snapshot = _snapshot(config, now - timedelta(seconds=5))
    wrong = "0" * 64 if column == "snapshot_sha256" else 7
    support.seed(db, config, OWNER, snapshot, **{column: wrong})
    assert _miss_reason(config, caplog) == "digest_mismatch"


@pytest.mark.parametrize("payload", ["{not json", "[]", "null", '"text"', '{"a": NaN}', "{}"])
def test_a_payload_with_a_matching_digest_that_is_not_a_snapshot_is_invalid_shape(
    db, config, now, caplog, payload
):
    encoded = payload.encode("utf-8")
    support.seed(
        db,
        config,
        OWNER,
        _snapshot(config, now - timedelta(seconds=5)),
        snapshot_json=payload,
        snapshot_sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=max(2, len(encoded)),
    )
    assert _miss_reason(config, caplog) == "invalid_shape"


def _mutations(config):
    def extra_key(value):
        value["extra"] = 1

    def unknown_field(value):
        value["records"]["tasks"].append({"title": "x", "not_a_field": 1})

    def float_money(value):
        value["records"]["payments"].append({"actual_amount_ils": 1.5})

    def bad_coverage(value):
        value["coverage"] = "excellent"

    def missing_summary(value):
        del value["summary"]

    def other_workbook(value):
        value["workbook_id"] = "workbook_98765432109876543210"

    def other_workspace(value):
        value["workspace_id"] = "another-workspace"

    def other_mode(value):
        value["mode"] = "LIVE"

    def other_source(value):
        value["source"] = "elsewhere"

    return [
        extra_key,
        unknown_field,
        float_money,
        bad_coverage,
        missing_summary,
        other_workbook,
        other_workspace,
        other_mode,
        other_source,
    ]


@pytest.mark.parametrize("index", range(9))
def test_a_consistently_signed_row_that_fails_the_exact_validator_is_invalid_shape(
    db, config, now, caplog, index
):
    snapshot = _snapshot(config, now - timedelta(seconds=5))
    _mutations(config)[index](snapshot)
    support.seed(db, config, OWNER, snapshot, as_of=now - timedelta(seconds=5))
    assert _miss_reason(config, caplog) == "invalid_shape"


def test_observed_at_must_equal_as_of_and_carry_a_timezone(db, config, now, caplog):
    snapshot = _snapshot(config, now - timedelta(seconds=5))
    support.seed(db, config, OWNER, snapshot, as_of=now - timedelta(seconds=6))
    assert _miss_reason(config, caplog) == "invalid_shape"

    caplog.clear()
    naive = _snapshot(config, now - timedelta(seconds=5))
    naive["observed_at"] = naive["observed_at"].replace("+00:00", "")
    support.seed(db, config, OWNER, naive, as_of=now - timedelta(seconds=5))
    assert _miss_reason(config, caplog) == "invalid_shape"


def test_the_validator_probe_never_replaces_the_stored_snapshot(db, config, now):
    stored = _snapshot(config, now - timedelta(days=2), marker="old but valid")
    support.seed(db, config, OWNER, stored)
    hit = cache.read_cached_snapshot(config, OWNER)
    assert hit["snapshot"]["observed_at"] == stored["observed_at"]
    assert hit["snapshot"] == stored


# ------------------------------------------------------------------------------------- age


def test_age_is_the_floor_of_server_time_minus_as_of(db, config, now):
    for seconds, expected in ((0.0, 0), (0.999, 0), (59.5, 59), (137.9, 137), (3600.0, 3600)):
        support.seed(db, config, OWNER, _snapshot(config, now - timedelta(seconds=seconds)))
        assert cache.read_cached_snapshot(config, OWNER)["age_seconds"] == expected


def test_a_snapshot_up_to_seven_days_old_is_served_and_older_is_not(db, config, now, caplog):
    edge = _snapshot(config, now - timedelta(days=7))
    support.seed(db, config, OWNER, edge)
    hit = cache.read_cached_snapshot(config, OWNER)
    assert hit["cached"] is True and hit["age_seconds"] == support.SEVEN_DAYS

    expired = _snapshot(config, now - timedelta(days=7, seconds=1))
    support.seed(db, config, OWNER, expired)
    assert _miss_reason(config, caplog) == "too_old"


def test_a_slightly_future_as_of_is_clamped_and_a_distant_one_is_refused(db, config, now, caplog):
    support.seed(db, config, OWNER, _snapshot(config, now + timedelta(seconds=299)))
    near = cache.read_cached_snapshot(config, OWNER)
    assert near["cached"] is True and near["age_seconds"] == 0

    support.seed(db, config, OWNER, _snapshot(config, now + timedelta(seconds=301)))
    assert _miss_reason(config, caplog) == "future_as_of"


# ------------------------------------------------------------------------- the size bound


def test_a_snapshot_of_exactly_the_maximum_size_is_stored(db, config, now):
    snapshot = support.padded_to(
        _snapshot(config, now - timedelta(seconds=5)), support.MAX_CACHED_BYTES
    )
    _remember(config, snapshot)
    row = _row(db, config)
    assert row["byte_length"] == 261_120
    assert cache.read_cached_snapshot(config, OWNER)["snapshot"] == snapshot


def test_one_byte_over_the_maximum_is_skipped_and_the_old_row_stays(db, config, now, caplog):
    kept = _snapshot(config, now - timedelta(seconds=40), marker="kept")
    _remember(config, kept)
    before = _row(db, config)
    big = support.padded_to(
        _snapshot(config, now - timedelta(seconds=5)), support.MAX_CACHED_BYTES + 1
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _remember(config, big)
    assert _lines(caplog) == ["os_snapshot_cache op=write outcome=skipped reason=too_large"]
    assert _row(db, config) == before


def test_the_bound_counts_utf8_bytes_not_characters(db, config, now, caplog):
    base = _snapshot(config, now - timedelta(seconds=5))
    over = support.padded_to(base, support.MAX_CACHED_BYTES + 2, char="א")
    assert len(support.canonical(over)) < support.MAX_CACHED_BYTES, "fewer characters than bytes"
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _remember(config, over)
    assert _lines(caplog) == ["os_snapshot_cache op=write outcome=skipped reason=too_large"]
    assert support.row_count(db) == 0
    fits = support.padded_to(base, support.MAX_CACHED_BYTES, char="א")
    _remember(config, fits)
    assert support.row_count(db) == 1


# -------------------------------------------------------------------- write-side refusals


def _write_outcome(config, caplog, snapshot) -> str:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _remember(config, snapshot)
    (line,) = _lines(caplog)
    match = LINE.fullmatch(line)
    assert match and match.group(1) == "write", line
    return f"{match.group(2)}:{match.group(3)}"


def test_the_writer_rechecks_the_binding_and_the_shape_before_storing(db, config, now, caplog):
    good = _snapshot(config, now - timedelta(seconds=5))
    foreign = copy.deepcopy(good)
    foreign["workbook_id"] = "workbook_98765432109876543210"
    assert _write_outcome(config, caplog, foreign) == "skipped:binding_mismatch"
    wrong_mode = copy.deepcopy(good)
    wrong_mode["mode"] = "LIVE"
    assert _write_outcome(config, caplog, wrong_mode) == "skipped:binding_mismatch"
    malformed = copy.deepcopy(good)
    malformed["records"]["tasks"].append({"nope": 1})
    assert _write_outcome(config, caplog, malformed) == "skipped:invalid_shape"
    naive = copy.deepcopy(good)
    naive["observed_at"] = naive["observed_at"].replace("+00:00", "")
    assert _write_outcome(config, caplog, naive) == "skipped:invalid_shape"
    nan = copy.deepcopy(good)
    nan["issues"] = [float("nan")]
    assert _write_outcome(config, caplog, nan) == "skipped:invalid_shape"
    future = _snapshot(config, now + timedelta(seconds=400))
    assert _write_outcome(config, caplog, future) == "skipped:future_as_of"
    assert support.row_count(db) == 0
    assert _write_outcome(config, caplog, good) == "stored:stored"


def test_a_disabled_feature_or_unknown_owner_is_never_written(db, config, now, caplog):
    snapshot = _snapshot(config, now - timedelta(seconds=5))
    assert _write_outcome(config, caplog, snapshot) == "stored:stored"
    config.assaf_os_enabled = False
    assert _write_outcome(config, caplog, _snapshot(config, now)) == "skipped:binding_mismatch"
    assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}
    config.assaf_os_enabled = True
    assert _write_outcome(config, caplog, _snapshot(config, now)) == "stored:stored"
    assert _write_outcome(config, caplog, snapshot) == "skipped:not_newer"
    config.telegram_owner_user_ids = "999999"
    assert _write_outcome(config, caplog, _snapshot(config, now)) == "skipped:binding_mismatch"


def test_remember_does_not_mutate_the_validated_result(db, config, now):
    snapshot = _snapshot(config, now - timedelta(seconds=5), marker="stays")
    frozen = copy.deepcopy(snapshot)
    _remember(config, snapshot)
    cache.read_cached_snapshot(config, OWNER)
    assert snapshot == frozen


# ---------------------------------------------------------------- failures are never raised


class _Boom(Exception):
    pass


def test_a_missing_table_is_a_miss_and_a_failed_write_never_raises(
    tmp_path, monkeypatch, config, caplog
):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cache, "get_session_factory", lambda: factory)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}
        _remember(config, _snapshot(config))
    assert _lines(caplog) == [
        "os_snapshot_cache op=read outcome=failed reason=db_unavailable",
        "os_snapshot_cache op=write outcome=failed reason=db_unavailable",
    ]
    engine.dispose()


def test_an_unreachable_database_is_a_miss_and_never_an_exception(monkeypatch, config, caplog):
    def unreachable():
        raise _Boom(SENTINEL)

    monkeypatch.setattr(cache, "get_session_factory", unreachable)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}
        _remember(config, _snapshot(config))
    assert SENTINEL not in caplog.text
    assert _lines(caplog) == [
        "os_snapshot_cache op=read outcome=failed reason=db_unavailable",
        "os_snapshot_cache op=write outcome=failed reason=db_unavailable",
    ]


@pytest.mark.parametrize("bad", [None, [], "text", 7, {}, {"observed_at": 1}])
def test_remember_never_raises_on_a_value_that_is_not_a_snapshot(db, config, caplog, bad):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _remember(config, bad)
    assert [LINE.fullmatch(line) is not None for line in _lines(caplog)] == [True]
    assert support.row_count(db) == 0


# ------------------------------------------------------------------------- the log contract


def test_every_operation_logs_exactly_one_line_with_codes_only(db, config, now, caplog):
    snapshot = _snapshot(config, now - timedelta(seconds=5), marker=SENTINEL)
    oversized = support.padded_to(_snapshot(config, now), support.MAX_CACHED_BYTES + 1)
    steps = [
        lambda: cache.read_cached_snapshot(config, OWNER),
        lambda: _remember(config, snapshot),
        lambda: cache.read_cached_snapshot(config, OWNER),
        lambda: _remember(config, snapshot),
        lambda: _remember(config, oversized),
        lambda: cache.read_cached_snapshot(config, OTHER_OWNER),
    ]
    seen: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for step in steps:
            caplog.clear()
            step()
            lines = _lines(caplog)
            assert len(lines) == 1, lines
            seen.append(lines[0])
    assert seen == [
        "os_snapshot_cache op=read outcome=miss reason=no_row",
        "os_snapshot_cache op=write outcome=stored reason=stored",
        "os_snapshot_cache op=read outcome=hit reason=hit",
        "os_snapshot_cache op=write outcome=skipped reason=not_newer",
        "os_snapshot_cache op=write outcome=skipped reason=too_large",
        "os_snapshot_cache op=read outcome=miss reason=no_row",
    ]
    row = _row(db, config)
    assert all(LINE.fullmatch(line) for line in seen)
    joined = " ".join(seen)
    for secret in (
        SENTINEL,
        OWNER,
        OTHER_OWNER,
        row["binding_hash"],
        row["snapshot_sha256"],
        str(row["byte_length"]),
        config.assaf_os_workbook_id,
        config.assaf_os_hmac_secret,
        config.assaf_os_script_url,
        "age_seconds",
    ):
        assert secret not in joined, secret
    assert SENTINEL not in caplog.text
