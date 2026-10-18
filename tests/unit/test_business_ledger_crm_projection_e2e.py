r"""Opt-in Slice D proof against disposable PostgreSQL and the approved TEST workbook.

The ordinary suite is offline and skips the actual proof. The parent operator may run it with::

    $env:MIA_ENV='test'
    $env:MIA_BUSINESS_LEDGER_E2E_ENABLED='1'
    $env:MIA_BUSINESS_LEDGER_E2E_CASE='slice-d'
    $env:MIA_OWNER_OS_TEST_POSTGRES_URL=
      'postgresql+psycopg://postgres:<synthetic>@127.0.0.1:15463/owner_os_test'
    .\.venv\Scripts\python.exe -m pytest \
      tests/unit/test_business_ledger_crm_projection_e2e.py -k actual_slice_d -q

Only after all opt-in guards pass does the harness load the existing private TEST operator
configuration. It never reads ``.env``. An ambiguous projection write is never repeated: the
durable marker permits only the signed ``crm_projection_receipt`` read operation.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

APP_ORIGIN = "https://os.example.test"
APPROVED_WORKSPACE_ID = "assafweb-staging"
SYNTHETIC_TELEGRAM_TOKEN = "synthetic-telegram-local-test-only"
SYNTHETIC_NAME = "פונה סינתטי — בדיקת אתר"
SYNTHETIC_EMAIL = "slice-d@example.test"
CAMPAIGN_ID = "slice-d-canonical-intake-projection-v1"
SOURCE_ID = str(uuid5(NAMESPACE_URL, "assafweb:slice-d:form-intake-v1"))
CAMPAIGN_REQUEST_ID = str(uuid5(NAMESPACE_URL, "assafweb:slice-d:projection-v1"))
EXPECTED_TOTALS = {
    "deal_value_minor": 44567,
    "scheduled_minor": 44567,
    "received_minor": 1200,
    "outstanding_minor": 43367,
    "expense_minor": 500,
    "net_cash_minor": 700,
}
EXPECTED_COUNTS = {"deals": 3, "charges": 4, "payments": 1, "expenses": 1, "projects": 1}
MARKER_KEYS = {
    "version",
    "phase",
    "campaign_id",
    "request_id",
    "source_id",
    "snapshot_hash",
    "projection_revision",
    "payload",
}
PHASES = {"prepared", "inflight", "completed", "locked"}
LIVE_STAGES = {
    "configuration",
    "receipt_recovery",
    "postgres_setup",
    "owner_login",
    "canonical_intake_capture",
    "canonical_intake_replay",
    "canonical_intake_conflict",
    "canonical_intake_counts",
    "canonical_read",
    "preflight",
    "initial_finance",
    "projection_write",
    "receipt_read",
    "final_finance",
    "cleanup",
}


class SliceDRejected(RuntimeError):
    """A fixed local harness guard rejected the candidate before a provider write."""


def _selected() -> bool:
    return (
        os.getenv("MIA_ENV") == "test"
        and os.getenv("MIA_BUSINESS_LEDGER_E2E_ENABLED") == "1"
        and os.getenv("MIA_BUSINESS_LEDGER_E2E_CASE") == "slice-d"
    )


def _reject(reason: str) -> None:
    pytest.fail(f"Slice D TEST harness configuration rejected: {reason}", pytrace=False)


def _live_failure_boundary(progress: dict[str, str], call) -> None:
    try:
        call()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        stage = progress.get("stage")
        safe_stage = stage if stage in LIVE_STAGES else "configuration"
        try:
            pytest.fail(
                f"Slice D TEST failed: stage={safe_stage} code=unexpected",
                pytrace=False,
            )
        except BaseException as redacted:
            raise redacted from None


def _marker_path(repo: Path) -> Path:
    return repo / ".cache" / "business-ledger-test" / "pending-crm-projection-e2e.json"


def _validate_marker(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != MARKER_KEYS:
        raise SliceDRejected("invalid_marker_shape")
    if (
        value["version"] != 1
        or value["phase"] not in PHASES
        or value["campaign_id"] != CAMPAIGN_ID
        or value["request_id"] != CAMPAIGN_REQUEST_ID
        or value["source_id"] != SOURCE_ID
        or not isinstance(value["snapshot_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["snapshot_hash"]) is None
        or not isinstance(value["projection_revision"], int)
        or isinstance(value["projection_revision"], bool)
        or value["projection_revision"] != 1
        or (value["payload"] is not None and not isinstance(value["payload"], dict))
    ):
        raise SliceDRejected("invalid_marker_values")
    if value["phase"] in {"prepared", "inflight"}:
        payload = value["payload"]
        if (
            not isinstance(payload, dict)
            or payload.get("snapshot_hash") != value["snapshot_hash"]
            or payload.get("projection_revision") != value["projection_revision"]
            or payload.get("mode") != "TEST"
            or payload.get("synthetic") is not True
            or payload.get("workspace_id") != APPROVED_WORKSPACE_ID
        ):
            raise SliceDRejected("invalid_marker_payload")
    elif value["payload"] is not None:
        raise SliceDRejected("terminal_marker_contains_payload")
    return value


def _load_marker(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise SliceDRejected("unsafe_marker")
    try:
        return _validate_marker(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SliceDRejected("invalid_marker") from exc


def _save_marker(path: Path, marker: dict[str, object]) -> None:
    _validate_marker(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _terminal_marker(marker: dict[str, object], phase: str) -> dict[str, object]:
    terminal = dict(marker, phase=phase, payload=None)
    _validate_marker(terminal)
    return terminal


def _assert_finance(snapshot: dict[str, object]) -> None:
    assert snapshot["mode"] == "TEST"
    assert snapshot["source"] == "google_sheets"
    assert snapshot["coverage"] == "financial_complete"
    assert snapshot["totals"] == EXPECTED_TOTALS
    assert snapshot["counts"] == EXPECTED_COUNTS


def _trusted_projection(
    *, owner_id: str, expected_owner: str, envelope: dict[str, object] | None, now: datetime
) -> dict[str, object]:
    if owner_id != expected_owner or re.fullmatch(r"[1-9][0-9]{0,19}", owner_id) is None:
        raise SliceDRejected("unauthorized_owner")
    if envelope is None:
        raise SliceDRejected("source_unavailable")
    from app.services.business_ledger_crm_projection import (
        CrmProjectionInvalid,
        build_test_crm_projection,
    )

    trusted = copy.deepcopy(envelope)
    trusted["synthetic"] = True
    try:
        return build_test_crm_projection(
            trusted,
            workspace_id=APPROVED_WORKSPACE_ID,
            now=now,
        )
    except CrmProjectionInvalid as exc:
        raise SliceDRejected(exc.code) from None


def _recover_projection_receipt(ledger, marker: dict[str, object], path: Path) -> dict:
    from app.services.business_ledger import LedgerError

    try:
        receipt = ledger._call(
            "crm_projection_receipt",
            {
                "snapshot_hash": marker["snapshot_hash"],
                "projection_revision": marker["projection_revision"],
            },
        )
        assert receipt == {
            "status": "committed",
            "snapshot_hash": marker["snapshot_hash"],
            "projection_revision": marker["projection_revision"],
        }
    except (LedgerError, AssertionError):
        _save_marker(path, _terminal_marker(marker, "locked"))
        raise SliceDRejected("receipt_recovery_locked") from None
    _save_marker(path, _terminal_marker(marker, "completed"))
    return receipt


def _telegram_proof(owner_id: str) -> str:
    # Local HMAC fixture only; this is not real Telegram or device acceptance.
    data: dict[str, object] = {
        "id": owner_id,
        "first_name": "Synthetic Slice D Owner",
        "auth_date": int(time.time()),
    }
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    data["hash"] = hmac.new(
        hashlib.sha256(SYNTHETIC_TELEGRAM_TOKEN.encode()).digest(),
        check.encode(),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps(data, separators=(",", ":"))


def _login(client, owner_id: str) -> None:
    challenge = client.get("/v1/owner/auth/challenge")
    assert challenge.status_code == 200
    response = client.post(
        "/v1/owner/auth/login",
        json={"login_proof": _telegram_proof(owner_id)},
        headers={"X-CSRF-Token": challenge.json()["csrf_token"]},
    )
    assert response.status_code == 200


def _database_url() -> str:
    from sqlalchemy.engine import make_url

    raw = os.getenv("MIA_OWNER_OS_TEST_POSTGRES_URL", "")
    if not raw:
        _reject("disposable PostgreSQL URL is required")
    parsed = make_url(raw)
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.username != "postgres"
        or not parsed.password
        or parsed.host != "127.0.0.1"
        or parsed.port != 15463
        or parsed.database != "owner_os_test"
        or bool(parsed.query)
    ):
        _reject("PostgreSQL must be the named disposable loopback owner_os_test database")
    return raw


@contextmanager
def _disposable_runtime(database_url: str, config: dict[str, str]):
    from app.core.config import Settings, get_settings
    from app.db.base import Base
    from app.db.migrate import _apply_file, _split_statements
    from app.main import app
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    suffix = uuid5(NAMESPACE_URL, f"{CAMPAIGN_ID}:{time.time_ns()}").hex[:16]
    schema = f"slice_d_{suffix}"
    auth_role, read_role = f"slice_d_auth_{suffix}", f"slice_d_read_{suffix}"
    roles = (auth_role, read_role)
    admin = create_engine(database_url)
    writer = None
    client = None
    parsed = make_url(database_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(
                text("REVOKE TEMPORARY, CREATE ON DATABASE owner_os_test FROM PUBLIC")
            )
            for role in roles:
                connection.execute(
                    text(f"CREATE ROLE {role} LOGIN NOINHERIT PASSWORD 'synthetic_local'")
                )
                connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {role}'))
        writer = create_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
        Base.metadata.create_all(writer)
        migration = (
            Path(__file__).resolve().parents[2] / "migrations" / "20261011_owner_os_sessions.sql"
        )
        with writer.begin() as connection:
            for _ in range(2):
                for statement in _split_statements(migration.read_text(encoding="utf-8")):
                    connection.execute(text(statement))
            # ADD CONSTRAINT has no IF NOT EXISTS: re-runs go through the runner's savepoints.
            for _ in range(2):
                _apply_file(connection, migration.parent / "20261015_owner_os_session_chains.sql")
            auth_grants = {
                "owner_os_login_challenges": "SELECT, INSERT, UPDATE",
                "owner_os_login_proofs": "INSERT",
                "owner_os_sessions": "SELECT, INSERT, UPDATE",
                "owner_os_revocations": "SELECT, INSERT, UPDATE",
                "owner_os_auth_attempts": "SELECT, INSERT",
            }
            for table, rights in auth_grants.items():
                connection.execute(text(f"GRANT {rights} ON {table} TO {auth_role}"))
            connection.execute(
                text(f"GRANT USAGE, SELECT ON owner_os_auth_attempts_id_seq TO {auth_role}")
            )
            for table in (
                "crm_contacts",
                "crm_activities",
                "owner_reminders",
                "idempotency_records",
                "approvals",
            ):
                connection.execute(text(f"GRANT SELECT ON {table} TO {read_role}"))
        role_urls = {
            "auth": parsed.set(
                username=auth_role,
                password="synthetic_local",
                query={"options": f"-csearch_path={schema}"},
            ).render_as_string(hide_password=False),
            "read": parsed.set(
                username=read_role,
                password="synthetic_local",
                query={"options": f"-csearch_path={schema}"},
            ).render_as_string(hide_password=False),
        }
        settings = Settings(
            _env_file=None,
            env="test",
            kill_switch=False,
            owner_os_enabled=True,
            owner_os_actions_enabled=False,
            owner_os_origin=APP_ORIGIN,
            owner_os_workspace_id=APPROVED_WORKSPACE_ID,
            owner_os_session_database_url=role_urls["auth"],
            owner_os_read_database_url=role_urls["read"],
            owner_os_action_database_url="",
            owner_os_cursor_secret="synthetic-slice-d-cursor-key-" * 2,
            owner_os_session_scopes="owner-os:read:contacts,owner-os:read:activities",
            telegram_bot_token=SYNTHETIC_TELEGRAM_TOKEN,
            telegram_owner_user_ids=config["owner_id"],
            business_ledger_test_enabled=True,
            business_ledger_workspace_id=APPROVED_WORKSPACE_ID,
            business_ledger_script_url=config["script_url"],
            business_ledger_hmac_secret=config["hmac_secret"],
            cors_origins=APP_ORIGIN,
            database_url="sqlite:///:memory:",
            reminders_enabled=False,
            reminder_delivery_enabled=False,
            crm_delivery_enabled=False,
        )
        app.dependency_overrides[get_settings] = lambda: settings
        client = TestClient(app, base_url=APP_ORIGIN, headers={"Origin": APP_ORIGIN})
        yield client, settings, sessionmaker(writer, autoflush=False, expire_on_commit=False)
    finally:
        if client is not None:
            client.close()
        app.dependency_overrides.clear()
        from app.api.owner_sessions import _session_factory

        if "role_urls" in locals():
            _session_factory(role_urls["auth"])[0].dispose()
        if writer is not None:
            writer.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            for role in roles:
                exists = connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                    {"role": role},
                )
                if exists:
                    connection.execute(text(f"DROP OWNED BY {role}"))
                    connection.execute(text(f"DROP ROLE {role}"))
        admin.dispose()


def _run_actual_slice_d(progress: dict[str, str]) -> None:
    progress["stage"] = "configuration"
    repo = Path(__file__).resolve().parents[2]
    from tests.unit.test_business_ledger_actions_e2e import _load_operator_config

    config = _load_operator_config(repo)
    database_url = _database_url()
    marker_path = _marker_path(repo)

    from app.core.config import MiaEnv, Settings
    from app.services.business_ledger import BusinessLedger, LedgerError

    ledger_settings = Settings(
        _env_file=None,
        env=MiaEnv.TEST,
        owner_os_enabled=True,
        owner_os_workspace_id=APPROVED_WORKSPACE_ID,
        telegram_owner_user_ids=config["owner_id"],
        business_ledger_test_enabled=True,
        business_ledger_workspace_id=APPROVED_WORKSPACE_ID,
        business_ledger_script_url=config["script_url"],
        business_ledger_hmac_secret=config["hmac_secret"],
        database_url="sqlite:///:memory:",
    )
    ledger = BusinessLedger(ledger_settings, config["owner_id"])
    marker = _load_marker(marker_path)
    if marker is not None:
        if marker["phase"] == "locked":
            raise SliceDRejected("campaign_locked")
        progress["stage"] = "initial_finance"
        _assert_finance(ledger.finance_snapshot())
        progress["stage"] = "receipt_recovery"
        _recover_projection_receipt(ledger, marker, marker_path)
        progress["stage"] = "final_finance"
        _assert_finance(ledger.finance_snapshot())
        return

    progress["stage"] = "initial_finance"
    before = ledger.finance_snapshot()
    _assert_finance(before)
    progress["stage"] = "postgres_setup"
    with _disposable_runtime(database_url, config) as (client, _settings, session_factory):
        progress["stage"] = "canonical_intake_capture"
        from app.db.models import (
            CrmActivityRow,
            CrmContactRow,
            CrmFormIntakeReceiptRow,
            CrmOutboxRow,
        )
        from app.services.form_intake import FormIntakePayloadConflict, FormIntakeService
        from sqlalchemy import func, select

        fields = {
            "name": SYNTHETIC_NAME,
            "email": SYNTHETIC_EMAIL,
            "source": "AssafWeb form",
            "status": "new",
        }
        payload_hash = hashlib.sha256(
            json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        with session_factory() as session:
            service = FormIntakeService(session)
            first = service.capture(
                source_id=SOURCE_ID,
                payload_sha256=payload_hash,
                submitted_at=datetime.now(UTC).isoformat(),
                fields=fields,
                summary="",
                recipient_ids=(),
            )
            session.commit()
            progress["stage"] = "canonical_intake_replay"
            replay = service.capture(
                source_id=SOURCE_ID,
                payload_sha256=payload_hash,
                submitted_at=datetime.now(UTC).isoformat(),
                fields=fields,
                summary="",
                recipient_ids=(),
            )
            assert first.status == "captured" and replay.status == "replayed"
            assert (first.contact_id, first.activity_id) == (replay.contact_id, replay.activity_id)
            progress["stage"] = "canonical_intake_conflict"
            with pytest.raises(FormIntakePayloadConflict):
                service.capture(
                    source_id=SOURCE_ID,
                    payload_sha256="0" * 64,
                    submitted_at=datetime.now(UTC).isoformat(),
                    fields=fields,
                    summary="",
                    recipient_ids=(),
                )
            session.rollback()
            progress["stage"] = "canonical_intake_counts"
            assert session.scalar(select(func.count()).select_from(CrmContactRow)) == 1
            assert session.scalar(select(func.count()).select_from(CrmActivityRow)) == 1
            assert session.scalar(select(func.count()).select_from(CrmFormIntakeReceiptRow)) == 1
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(CrmOutboxRow)
                    .where(CrmOutboxRow.destination == "telegram")
                )
                == 0
            )

        progress["stage"] = "owner_login"
        _login(client, config["owner_id"])
        progress["stage"] = "canonical_read"
        response = client.get("/v1/owner/os/contacts?limit=50")
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["api_version"] == "owner-os.v1"
        assert envelope["coverage"] == "complete"
        assert envelope["pagination"] == {
            "next_cursor": None,
            "has_more": False,
            "page_complete": True,
        }
        assert len(envelope["data"]["items"]) == 1
        contact = envelope["data"]["items"][0]
        assert contact["contact_id"] == first.contact_id
        assert contact["name"] == SYNTHETIC_NAME
        assert contact["phone"] is None and contact["email"] == SYNTHETIC_EMAIL
        activity_response = client.get(
            f"/v1/owner/os/contacts/{first.contact_id}/activities?limit=50"
        )
        assert activity_response.status_code == 200
        activity_envelope = activity_response.json()
        assert activity_envelope["coverage"] == "complete"
        assert activity_envelope["pagination"] == {
            "next_cursor": None,
            "has_more": False,
            "page_complete": True,
        }
        assert [item["activity_id"] for item in activity_envelope["data"]["items"]] == [
            first.activity_id
        ]

        progress["stage"] = "preflight"
        projection = _trusted_projection(
            owner_id=config["owner_id"],
            expected_owner=config["owner_id"],
            envelope=envelope,
            now=datetime.now(UTC),
        )
        assert projection["contacts"][0]["contact_id"] == first.contact_id
        assert projection["contacts"][0]["revision"] == contact["revision"]
        assert SYNTHETIC_EMAIL not in json.dumps(projection, ensure_ascii=False)
        marker = {
            "version": 1,
            "phase": "prepared",
            "campaign_id": CAMPAIGN_ID,
            "request_id": CAMPAIGN_REQUEST_ID,
            "source_id": SOURCE_ID,
            "snapshot_hash": projection["snapshot_hash"],
            "projection_revision": projection["projection_revision"],
            "payload": projection,
        }
        _save_marker(marker_path, marker)
        marker["phase"] = "inflight"
        _save_marker(marker_path, marker)
        progress["stage"] = "projection_write"
        try:
            result = ledger._call("save_crm_projection", projection)
            assert result == {
                "status": "committed",
                "snapshot_hash": projection["snapshot_hash"],
                "projection_revision": 1,
            }
        except LedgerError:
            progress["stage"] = "receipt_read"
            _recover_projection_receipt(ledger, marker, marker_path)
        else:
            progress["stage"] = "receipt_read"
            _recover_projection_receipt(ledger, marker, marker_path)

    progress["stage"] = "final_finance"
    after = ledger.finance_snapshot()
    _assert_finance(after)
    assert after["totals"] == before["totals"] and after["counts"] == before["counts"]


def test_slice_d_default_off_before_private_config_or_provider(monkeypatch) -> None:
    monkeypatch.delenv("MIA_BUSINESS_LEDGER_E2E_ENABLED", raising=False)
    monkeypatch.delenv("MIA_BUSINESS_LEDGER_E2E_CASE", raising=False)
    assert not _selected()


def test_slice_d_marker_is_bounded_and_receipt_recovery_never_reposts(tmp_path) -> None:
    payload = {
        "mode": "TEST",
        "synthetic": True,
        "workspace_id": APPROVED_WORKSPACE_ID,
        "snapshot_hash": "a" * 64,
        "projection_revision": 1,
    }
    marker = {
        "version": 1,
        "phase": "inflight",
        "campaign_id": CAMPAIGN_ID,
        "request_id": CAMPAIGN_REQUEST_ID,
        "source_id": SOURCE_ID,
        "snapshot_hash": "a" * 64,
        "projection_revision": 1,
        "payload": payload,
    }
    path = tmp_path / "pending-crm-projection-e2e.json"
    _save_marker(path, marker)

    class ReceiptOnly:
        def __init__(self):
            self.calls = []

        def _call(self, operation, data):
            self.calls.append((operation, data))
            return {"status": "committed", **data}

    port = ReceiptOnly()
    _recover_projection_receipt(port, _load_marker(path), path)
    assert [call[0] for call in port.calls] == ["crm_projection_receipt"]
    assert _load_marker(path)["phase"] == "completed"
    serialized = path.read_text(encoding="utf-8")
    assert '"payload":null' in serialized
    assert "hmac_secret" not in serialized and "owner_id" not in serialized

    class MissingReceipt(ReceiptOnly):
        def _call(self, operation, data):
            from app.services.business_ledger import LedgerError

            self.calls.append((operation, data))
            raise LedgerError("record_not_found", 404)

    locked_path = tmp_path / "pending-locked.json"
    _save_marker(locked_path, marker)
    missing = MissingReceipt()
    with pytest.raises(SliceDRejected, match="receipt_recovery_locked"):
        _recover_projection_receipt(missing, marker, locked_path)
    assert [call[0] for call in missing.calls] == ["crm_projection_receipt"]
    assert _load_marker(locked_path)["phase"] == "locked"


def test_slice_d_live_failure_output_is_fixed_and_redacted() -> None:
    secret = "must-never-appear"
    with pytest.raises(pytest.fail.Exception) as failure:
        _live_failure_boundary(
            {"stage": "projection_write"},
            lambda: (_ for _ in ()).throw(RuntimeError(secret)),
        )
    message = str(failure.value)
    assert message == "Slice D TEST failed: stage=projection_write code=unexpected"
    assert secret not in message
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True
    assert secret not in str(failure.getrepr(style="short"))


def _offline_envelope(now: datetime) -> dict[str, object]:
    observed = now.isoformat().replace("+00:00", "Z")
    pagination = {"next_cursor": None, "has_more": False, "page_complete": True}
    return {
        "api_version": "owner-os.v1",
        "source": "mia_postgresql",
        "owner_scope": "self",
        "request_id": "request_synthetic",
        "snapshot_id": "snapshot_synthetic",
        "observed_at": observed,
        "coverage": "complete",
        "coverage_scope": "stored_pg_contacts",
        "freshness": "fresh",
        "reason_codes": [],
        "source_statuses": [],
        "capabilities": [],
        "data": {
            "section_state": "available",
            "items": [
                {
                    "contact_id": "crm_synthetic",
                    "revision": 1,
                    "name": SYNTHETIC_NAME,
                    "business": None,
                    "phone": None,
                    "email": SYNTHETIC_EMAIL,
                    "updated_at": observed,
                    "source": {
                        "source_id": "crm_contacts",
                        "recorded_label": "AssafWeb form",
                        "evidence_key": None,
                        "attribution_state": "unverified",
                    },
                    "status": {
                        "recorded_value": "new",
                        "qualification": None,
                        "pipeline_stage": None,
                    },
                    "next_action": {
                        "text": None,
                        "due_date": None,
                        "due_at": None,
                        "precision": "none",
                        "timezone": "Asia/Jerusalem",
                        "state": "not_collected",
                    },
                    "record_quality": {
                        "stored_read": "verified",
                        "business_classification": "not_collected",
                        "source_coverage": "recorded",
                        "missing_fields": [],
                        "reason_codes": [],
                    },
                }
            ],
            "coverage": "complete",
            "coverage_scope": "stored_pg_contacts",
            "freshness": "fresh",
            "reason_codes": [],
            "pagination": pagination,
        },
        "pagination": pagination,
    }


def test_slice_d_preflight_rejects_wrong_owner_unavailable_incomplete_and_stale() -> None:
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    valid = _offline_envelope(now)
    cases = [
        {"owner_id": "999", "expected_owner": "123", "envelope": valid},
        {"owner_id": "123", "expected_owner": "123", "envelope": None},
        {"owner_id": "123", "expected_owner": "123", "envelope": dict(valid, coverage="partial")},
        {
            "owner_id": "123",
            "expected_owner": "123",
            "envelope": dict(
                valid, observed_at=(now - timedelta(seconds=301)).isoformat().replace("+00:00", "Z")
            ),
        },
    ]

    class MockPort:
        def __init__(self):
            self.calls = []

        def _call(self, operation, data):
            self.calls.append((operation, data))
            return {"status": "committed"}

    port = MockPort()
    for case in cases:
        with pytest.raises(SliceDRejected):
            candidate = _trusted_projection(now=now, **case)
            port._call("save_crm_projection", candidate)
    assert port.calls == []
    accepted = _trusted_projection(owner_id="123", expected_owner="123", envelope=valid, now=now)
    assert accepted["contacts"][0]["contact_id"] == "crm_synthetic"
    assert SYNTHETIC_EMAIL not in json.dumps(accepted, ensure_ascii=False)
    port._call("save_crm_projection", accepted)
    assert [call[0] for call in port.calls] == ["save_crm_projection"]


def test_actual_slice_d() -> None:
    if (
        os.getenv("MIA_BUSINESS_LEDGER_E2E_ENABLED") != "1"
        or os.getenv("MIA_BUSINESS_LEDGER_E2E_CASE") != "slice-d"
    ):
        pytest.skip("set the two explicit Slice D E2E guards for the approved TEST proof")
    if os.getenv("MIA_ENV") != "test":
        _reject("MIA_ENV must be exactly test")
    progress = {"stage": "configuration"}
    _live_failure_boundary(progress, lambda: _run_actual_slice_d(progress))
