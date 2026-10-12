"""Opt-in local OwnerClient -> BFF -> FastAPI -> PostgreSQL -> TEST Sheets proof.

This test intentionally skips unless ``MIA_BUSINESS_LEDGER_E2E_ENABLED=1``. The
operator must also provide a disposable loopback PostgreSQL URL in
``MIA_OWNER_OS_TEST_POSTGRES_URL`` and the private, gitignored
``.cache/business-ledger-test/operator-config.json`` described by
``_load_operator_config``. Never point either input at production.

The Node half persists only opaque/request metadata before every provider write.
It never retries an ambiguous write; recovery is receipt-only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.migrate import _split_statements
from app.db.models import CrmContactRow
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

APP_ORIGIN = "https://os.example.test"
APPROVED_WORKBOOK_ID = "1T9Is3iJ_RlI8KndpTXYCfuTkVsMbncw3_0bAVze0RE4"
APPROVED_WORKSPACE_ID = "assafweb-staging"
APPROVED_SCRIPT_ID = "1gvkcnDHB3d21lYipOdg84zm2omvoLbmY7EoiQssMVvUCvPhaG9rJ2VV6"
SYNTHETIC_TELEGRAM_TOKEN = "synthetic-telegram-local-test-only"
EXPECTED_CONFIG_KEYS = {
    "mode",
    "spreadsheet_id",
    "script_id",
    "workspace_id",
    "script_url",
    "hmac_secret",
    "owner_id",
}
SAFE_PROVIDER_STATUSES = {"invalid", "conflict", "unavailable", "not_found"}
SAFE_PROVIDER_REASONS = {
    "accepted_deal_missing",
    "audit_capacity_exceeded",
    "audit_corrupt",
    "coverage_limit_exceeded",
    "deal_capacity_exceeded",
    "deal_row_tampered",
    "duplicate_deal_id",
    "duplicate_receipt_key",
    "idempotency_key_reused",
    "invalid_amount_minor",
    "invalid_currency",
    "invalid_deal_id",
    "invalid_deal_shape",
    "invalid_expected_revision",
    "invalid_headers_audit",
    "invalid_headers_deals",
    "invalid_idempotency_key",
    "invalid_notes",
    "invalid_operation",
    "invalid_payload_shape",
    "invalid_payment_plan",
    "invalid_request",
    "invalid_request_id",
    "invalid_save_shape",
    "invalid_settings",
    "invalid_signature",
    "invalid_snapshot_shape",
    "invalid_start_date",
    "invalid_status",
    "invalid_title",
    "invalid_transport",
    "invalid_workspace",
    "ledger_busy",
    "project_links_not_supported",
    "provider_result_unknown",
    "receipt_not_found",
    "revision_mismatch",
    "settings_mode_not_test",
    "settings_workspace_mismatch",
    "snapshot_capacity_exceeded",
    "stale_request",
    "unauthorized_owner",
    "unavailable",
    "workbook_not_test",
}
SAFE_CLIENT_STAGES = {
    "startup",
    "auth_challenge",
    "auth_login",
    "session_client",
    "session_raw",
    "contacts_read",
    "business_initial_read",
    "pending_receipt_recovery",
    "create_deal",
    "replay_create",
    "edit_deal",
    "stale_conflict",
    "logout",
    "sheet_readonly_finance",
    "sheet_readonly_render",
    "sheet_readonly_logout",
    "slice_c_deal",
    "slice_c_initial_finance",
    "slice_c_original_schedule",
    "slice_c_schedule",
    "slice_c_schedule_replay",
    "slice_c_payment",
    "slice_c_project",
    "slice_c_expense_lost_response",
    "slice_c_stale",
    "slice_c_overpay",
    "slice_c_duplicate",
    "slice_c_final_read",
}
SAFE_BACKEND_ROUTES = {
    "/v1/owner/auth/challenge": "auth_challenge",
    "/v1/owner/auth/login": "auth_login",
    "/v1/owner/auth/session": "auth_session",
}
SAFE_BACKEND_STATUSES = {200, 400, 401, 403, 413, 415, 422, 429, 500, 502, 503, 504}
SAFE_CLIENT_CODES = {
    "conflict",
    "forbidden",
    "integration_unavailable",
    "invalid_response",
    "operation_unavailable",
    "rate_limited",
    "record_not_found",
    "source_unavailable",
    "timeout",
    "unauthorized",
    "unknown",
    "validation_error",
}
SAFE_CLIENT_HTTP_STATUSES = {400, 401, 403, 404, 409, 413, 415, 422, 429, 500, 502, 503, 504}
SAFE_DEAL_FIELDS = {
    "id",
    "title",
    "amount_minor",
    "currency",
    "payment_plan",
    "start_date",
    "status",
    "project_id",
    "notes",
}


def _fail(reason: str) -> None:
    pytest.fail(f"business ledger E2E configuration rejected: {reason}", pytrace=False)


def _load_operator_config(repo: Path) -> dict[str, object]:
    path = repo / ".cache" / "business-ledger-test" / "operator-config.json"
    if not path.is_file() or path.is_symlink():
        _fail("private operator config is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("private operator config is invalid")
    if not isinstance(value, dict) or set(value) != EXPECTED_CONFIG_KEYS:
        _fail("private operator config keys do not match the bounded schema")
    if value.get("mode") != "TEST":
        _fail("private operator mode is not TEST")
    if value.get("spreadsheet_id") != APPROVED_WORKBOOK_ID:
        _fail("workbook is not the approved TEST resource")
    if value.get("workspace_id") != APPROVED_WORKSPACE_ID:
        _fail("workspace is not the approved TEST workspace")
    if value.get("script_id") != APPROVED_SCRIPT_ID:
        _fail("bound script is not the approved TEST resource")
    owner_id = value.get("owner_id")
    secret = value.get("hmac_secret")
    script_url = value.get("script_url")
    if not isinstance(owner_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", owner_id):
        _fail("owner identity is not canonical")
    if not isinstance(secret, str) or len(secret) < 32:
        _fail("ledger signing secret is unavailable")
    if not isinstance(script_url, str) or not re.fullmatch(
        r"https://script\.google\.com/macros/s/[A-Za-z0-9_-]{20,256}/exec", script_url
    ):
        _fail("Apps Script URL is not an approved TEST endpoint shape")
    frontend_raw = os.getenv("MIA_OWNER_OS_FRONTEND_ROOT", "")
    if not frontend_raw:
        _fail("MIA_OWNER_OS_FRONTEND_ROOT is required")
    frontend = Path(frontend_raw).resolve()
    expected_frontend = (
        repo.parent / "owner-os-business-ledger-mobile" / "prototypes" / "assafweb-os-mobile"
    ).resolve()
    if frontend != expected_frontend or not all(
        (frontend / name).is_file()
        for name in ("bff.mjs", "client.mjs", "contracts.mjs", "views.mjs")
    ):
        _fail("current mobile ledger frontend root is unavailable")
    value["frontend_root"] = frontend
    return value


def _telegram_proof(owner_id: str) -> str:
    data = {"id": owner_id, "first_name": "Synthetic Ledger Owner", "auth_date": int(time.time())}
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    data["hash"] = hmac.new(
        hashlib.sha256(SYNTHETIC_TELEGRAM_TOKEN.encode()).digest(),
        check.encode(),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps(data, separators=(",", ":"))


def _sanitized_provider_diagnostic(
    raw: bytes, request_body: bytes, secret: str
) -> dict[str, str] | None:
    """Extract authenticated fixed labels only; never return request or identity data."""

    try:
        wrapper = json.loads(raw)
        request_wrapper = json.loads(request_body)
        if not isinstance(wrapper, dict) or not isinstance(request_wrapper, dict):
            return None
        request = json.loads(request_wrapper["payload"])
        response_payload = wrapper["payload"]
        response_signature = wrapper["signature"]
        if (
            not isinstance(request, dict)
            or not isinstance(response_payload, str)
            or not isinstance(response_signature, str)
        ):
            return None
        expected = hmac.new(secret.encode(), response_payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(response_signature, expected):
            return None
        response = json.loads(wrapper["payload"])
        if not isinstance(response, dict):
            return None
        identity_keys = ("request_id", "workspace_id", "owner_id")
        if any(response.get(key) != request.get(key) for key in identity_keys):
            return None
        status = response["status"]
        reason = response["result"]["reason_code"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if status == "ok":
        return None
    return {
        "status": status
        if isinstance(status, str) and status in SAFE_PROVIDER_STATUSES
        else "redacted",
        "reason_code": reason
        if isinstance(reason, str) and reason in SAFE_PROVIDER_REASONS
        else "redacted",
    }


def _sanitized_client_diagnostic(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"stage": "unknown", "code": "unexpected", "status": None, "fields": []}
    if not isinstance(value, dict) or set(value) != {"stage", "code", "status", "fields"}:
        return {"stage": "unknown", "code": "unexpected", "status": None, "fields": []}
    stage = value["stage"]
    code = value["code"]
    status = value["status"]
    fields = value["fields"]
    if not isinstance(stage, str) or stage not in SAFE_CLIENT_STAGES:
        stage = "unknown"
    if not isinstance(code, str) or code not in SAFE_CLIENT_CODES:
        code = "unexpected"
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or status not in SAFE_CLIENT_HTTP_STATUSES
    ):
        status = None
    if not isinstance(fields, list) or not all(
        isinstance(field, str) and field in SAFE_DEAL_FIELDS for field in fields
    ):
        fields = []
    return {"stage": stage, "code": code, "status": status, "fields": fields}


class _ObservedAuthApp:
    """Record only fixed auth route labels and HTTP outcomes for the local harness."""

    def __init__(self, app, diagnostics: list[dict[str, object]]):
        self.app = app
        self.diagnostics = diagnostics

    async def __call__(self, scope, receive, send):
        route = SAFE_BACKEND_ROUTES.get(scope.get("path")) if scope.get("type") == "http" else None
        if route is None:
            return await self.app(scope, receive, send)
        diagnostic: dict[str, object] = {
            "route": route,
            "outcome": "started",
            "status": None,
        }
        self.diagnostics.append(diagnostic)

        async def observed_send(message):
            if message.get("type") == "http.response.start":
                status = message.get("status")
                diagnostic["outcome"] = "response"
                diagnostic["status"] = (
                    status
                    if isinstance(status, int)
                    and not isinstance(status, bool)
                    and status in SAFE_BACKEND_STATUSES
                    else None
                )
            await send(message)

        try:
            return await self.app(scope, receive, observed_send)
        except Exception:
            diagnostic["outcome"] = "exception"
            diagnostic["status"] = None
            raise


class _SafeRuntime(dict):
    """Keep subprocess credentials out of pytest fixture representations."""

    def __repr__(self) -> str:
        return "<local synthetic ledger runtime>"


def _drop_disposable_schema(admin, schema: str, roles: tuple[str, ...]) -> None:
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


@pytest.fixture(scope="module")
def live_ledger_runtime():
    if os.getenv("MIA_BUSINESS_LEDGER_E2E_ENABLED") != "1":
        pytest.skip("set MIA_BUSINESS_LEDGER_E2E_ENABLED=1 for the approved TEST ledger E2E")
    if os.getenv("MIA_ENV") != "test":
        _fail("MIA_ENV must be exactly test")

    repo = Path(__file__).resolve().parents[2]
    operator = _load_operator_config(repo)
    database_url = os.getenv("MIA_OWNER_OS_TEST_POSTGRES_URL", "")
    if not database_url:
        _fail("disposable PostgreSQL URL is required")
    parsed = make_url(database_url)
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host != "127.0.0.1"
        or parsed.port != 15463
        or parsed.database != "owner_os_test"
        or bool(parsed.query)
    ):
        _fail("PostgreSQL must be the named disposable loopback owner_os_test database")

    admin = create_engine(database_url)
    suffix = uuid4().hex[:16]
    schema = f"ledger_{suffix}"
    auth_role, read_role = f"ledger_auth_{suffix}", f"ledger_read_{suffix}"
    roles = (auth_role, read_role)
    engine = None
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

        engine = create_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
        Base.metadata.create_all(engine)
        migrations = (
            repo / "migrations" / "20261011_owner_reminders.sql",
            repo / "migrations" / "20261011_owner_os_sessions.sql",
        )
        with engine.begin() as connection:
            # Match the established owner-OS fixtures and prove both migrations idempotent.
            for migration in migrations:
                for _ in range(2):
                    for sql in _split_statements(migration.read_text(encoding="utf-8")):
                        connection.execute(text(sql))
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
            now = datetime.now(UTC).isoformat()
            connection.execute(
                CrmContactRow.__table__.insert().values(
                    id="ledger_e2e_contact",
                    revision=1,
                    fields_json=json.dumps(
                        {
                            "name": "לקוח PostgreSQL סינתטי",
                            "email": "ledger-e2e@example.test",
                            "next_step": "בדיקת תצוגה בלבד",
                            "date": "2026-10-11",
                            "status": "lead",
                            "source": "other",
                        },
                        ensure_ascii=False,
                    ),
                    source_ref="synthetic-ledger-e2e",
                    created_at=now,
                    updated_at=now,
                )
            )
    except Exception:
        if engine is not None:
            engine.dispose()
        _drop_disposable_schema(admin, schema, roles)
        admin.dispose()
        raise

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
        owner_os_cursor_secret="synthetic-cursor-key-" * 3,
        owner_os_session_scopes=(
            "owner-os:read:contacts,owner-os:read:activities,owner-os:read:reminders,"
            "business-ledger:read,business-ledger:write"
        ),
        telegram_bot_token=SYNTHETIC_TELEGRAM_TOKEN,
        telegram_owner_user_ids=operator["owner_id"],
        business_ledger_test_enabled=True,
        business_ledger_workspace_id=APPROVED_WORKSPACE_ID,
        business_ledger_script_url=operator["script_url"],
        business_ledger_hmac_secret=operator["hmac_secret"],
        cors_origins=APP_ORIGIN,
        database_url="sqlite:///:memory:",
        reminders_enabled=False,
        reminder_delivery_enabled=False,
        crm_delivery_enabled=False,
    )

    from app.main import app
    from app.services import business_ledger as ledger_port

    provider_diagnostics: list[dict[str, object]] = []
    backend_diagnostics: list[dict[str, object]] = []
    original_post = ledger_port._post

    def observed_post(url: str, body: bytes) -> bytes:
        started = time.monotonic()
        try:
            raw = original_post(url, body)
        except Exception:
            provider_diagnostics.append(
                {
                    "transport_failure": True,
                    "elapsed_ms": min(round((time.monotonic() - started) * 1000), 60_000),
                }
            )
            raise
        record: dict[str, object] = {
            "transport_failure": False,
            "elapsed_ms": min(round((time.monotonic() - started) * 1000), 60_000),
        }
        diagnostic = _sanitized_provider_diagnostic(raw, body, str(operator["hmac_secret"]))
        if diagnostic is not None:
            record.update(diagnostic)
        provider_diagnostics.append(record)
        return raw

    ledger_port._post = observed_post

    app.dependency_overrides[get_settings] = lambda: settings
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    import uvicorn

    observed_app = _ObservedAuthApp(app, backend_diagnostics)
    server = uvicorn.Server(
        uvicorn.Config(observed_app, lifespan="off", access_log=False, log_level="warning")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.02)
        if not server.started:
            _fail("local FastAPI did not start")
        yield _SafeRuntime(
            {
                "backend": f"http://127.0.0.1:{port}",
                "frontend": operator["frontend_root"],
                "proof": _telegram_proof(str(operator["owner_id"])),
                "engine": engine,
                "repo": repo,
                "provider_diagnostics": provider_diagnostics,
                "backend_diagnostics": backend_diagnostics,
            }
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        app.dependency_overrides.clear()
        ledger_port._post = original_post
        from app.api.owner_sessions import _session_factory

        _session_factory(role_urls["auth"])[0].dispose()
        engine.dispose()
        _drop_disposable_schema(admin, schema, roles)
        admin.dispose()


def _run_node_case(live_ledger_runtime, case: str, expected_stdout: str) -> None:
    runtime = live_ledger_runtime
    script = Path(__file__).resolve().parents[1] / "integration" / "business_ledger_bff_e2e.mjs"
    pending_path = runtime["repo"] / ".cache" / "business-ledger-test" / "pending-e2e.json"
    result = subprocess.run(
        [
            "node",
            str(script),
            runtime["backend"],
            str(runtime["frontend"]),
            str(pending_path),
            case,
        ],
        input=json.dumps({"proof": runtime["proof"]}),
        text=True,
        capture_output=True,
        # This finite campaign makes multiple sequential real Sheets calls,
        # whose individual cold finance read was measured at 9.6 seconds.
        timeout=300 if case == "slice-c" else 90,
        encoding="utf-8",
    )
    if result.returncode != 0:
        diagnostic = {
            "client": _sanitized_client_diagnostic(result.stderr.strip()),
            "backend": runtime["backend_diagnostics"][-1]
            if runtime["backend_diagnostics"]
            else None,
            "provider": runtime["provider_diagnostics"][-1]
            if runtime["provider_diagnostics"]
            else None,
        }
        print("ledger_e2e_diagnostic=" + json.dumps(diagnostic), file=sys.stderr)
    assert result.returncode == 0, "ledger E2E failed; inspect sanitized Node assertion output"
    assert result.stdout.strip() == expected_stdout
    assert result.stderr == ""
    with runtime["engine"].connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM crm_contacts")) == 1
        assert connection.scalar(text("SELECT count(*) FROM owner_reminders")) == 0
        assert connection.scalar(text("SELECT count(*) FROM approvals")) == 0
        assert connection.scalar(text("SELECT count(*) FROM idempotency_records")) == 0


def _sheet_readonly_failure(stage: str) -> None:
    safe_stage = stage if stage in {"setup", "launcher", "cleanup"} else "setup"
    try:
        pytest.fail(
            f"sheet-readonly TEST failed: stage={safe_stage} code=unexpected",
            pytrace=False,
        )
    except BaseException as redacted:
        raise redacted from None


def _close_sheet_runtime(iterator) -> None:
    if iterator is None:
        return
    try:
        iterator.close()
    except BaseException:
        return


def _run_sheet_readonly_boundary(runtime_factory, runner) -> None:
    iterator = None
    try:
        iterator = runtime_factory()
        runtime = next(iterator)
    except pytest.skip.Exception:
        _close_sheet_runtime(iterator)
        raise
    except (KeyboardInterrupt, SystemExit):
        _close_sheet_runtime(iterator)
        raise
    except BaseException:
        _close_sheet_runtime(iterator)
        _sheet_readonly_failure("setup")

    try:
        runner(runtime)
    except pytest.skip.Exception:
        _close_sheet_runtime(iterator)
        raise
    except (KeyboardInterrupt, SystemExit):
        _close_sheet_runtime(iterator)
        raise
    except BaseException:
        _close_sheet_runtime(iterator)
        _sheet_readonly_failure("launcher")

    try:
        next(iterator)
    except StopIteration:
        return
    except pytest.skip.Exception:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        _sheet_readonly_failure("cleanup")
    _close_sheet_runtime(iterator)
    _sheet_readonly_failure("cleanup")


@pytest.mark.skipif(
    os.getenv("MIA_BUSINESS_LEDGER_E2E_CASE") != "ab-write-original",
    reason="the original A/B writer is retained but must never be selected for closeout",
)
def test_owner_client_bff_postgres_and_real_test_ledger(live_ledger_runtime):
    _run_node_case(
        live_ledger_runtime,
        "ab-write-original",
        "PASS OwnerClient -> current mobile BFF -> FastAPI -> disposable PostgreSQL -> "
        "real Google TEST ledger; create/edit/replay/receipt/conflict and live DTO views",
    )


@pytest.mark.skipif(
    os.getenv("MIA_BUSINESS_LEDGER_E2E_CASE") != "ab-readonly",
    reason="select the bounded read-only A/B closeout explicitly",
)
def test_owner_client_bff_readonly_ab_closeout(live_ledger_runtime):
    _run_node_case(
        live_ledger_runtime,
        "ab-readonly",
        "PASS READ-ONLY A/B closeout; accepted deals/totals/views and disabled "
        "writes/logout revocation verified",
    )


@pytest.mark.skipif(
    os.getenv("MIA_BUSINESS_LEDGER_E2E_CASE") != "slice-c",
    reason="select the bounded Slice C finance campaign explicitly",
)
def test_owner_client_bff_slice_c_finance(live_ledger_runtime):
    _run_node_case(
        live_ledger_runtime,
        "slice-c",
        "PASS Slice C OwnerClient/BFF/Mia/Google TEST finance; durable writes, receipts, "
        "conflicts, totals and live DTO views",
    )


@pytest.mark.skipif(
    os.getenv("MIA_BUSINESS_LEDGER_E2E_CASE") != "sheet-readonly",
    reason="select the bounded read-only Sheet finance acceptance explicitly",
)
def test_owner_client_bff_sheet_readonly_finance_acceptance():
    _run_sheet_readonly_boundary(
        lambda: live_ledger_runtime.__wrapped__(),
        lambda runtime: _run_node_case(
            runtime,
            "sheet-readonly",
            "PASS READ-ONLY Sheet/mobile/BFF/Mia/Google TEST finance acceptance; project, "
            "totals, views and logout verified",
        ),
    )


def test_provider_diagnostic_contains_only_allowlisted_protocol_labels():
    secret = "synthetic-diagnostic-secret-value"
    request = {
        "request_id": "00000000-0000-4000-8000-000000000001",
        "workspace_id": "synthetic-workspace",
        "owner_id": "12345678",
    }
    request_body = json.dumps({"payload": json.dumps(request), "signature": "unused"}).encode()

    def response(reason: str, *, signature: str | None = None) -> bytes:
        payload = json.dumps({**request, "status": "invalid", "result": {"reason_code": reason}})
        actual_signature = (
            signature or hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        )
        return json.dumps({"payload": payload, "signature": actual_signature}).encode()

    assert _sanitized_provider_diagnostic(response("invalid_title"), request_body, secret) == {
        "status": "invalid",
        "reason_code": "invalid_title",
    }
    assert _sanitized_provider_diagnostic(response("deadbeef123456"), request_body, secret) == {
        "status": "invalid",
        "reason_code": "redacted",
    }
    assert (
        _sanitized_provider_diagnostic(
            response("invalid_title", signature="0" * 64), request_body, secret
        )
        is None
    )
    malformed_payload = json.dumps(
        {
            "payload": "[]",
            "signature": hmac.new(secret.encode(), b"[]", hashlib.sha256).hexdigest(),
        }
    ).encode()
    assert _sanitized_provider_diagnostic(malformed_payload, request_body, secret) is None
    assert _sanitized_provider_diagnostic(
        response([]),
        request_body,
        secret,  # type: ignore[arg-type]
    ) == {"status": "invalid", "reason_code": "redacted"}
    assert _sanitized_client_diagnostic(
        '{"stage":"create_deal","code":"validation_error","status":422,"fields":[]}'
    ) == {
        "stage": "create_deal",
        "code": "validation_error",
        "status": 422,
        "fields": [],
    }
    assert _sanitized_client_diagnostic(
        '{"stage":"deadbeef","code":"synthetic_secret_123456","status":422,"fields":["deadbeef"]}'
    ) == {
        "stage": "unknown",
        "code": "unexpected",
        "status": 422,
        "fields": [],
    }
    assert _sanitized_client_diagnostic('{"stage":[],"code":{},"status":[],"fields":{}}') == {
        "stage": "unknown",
        "code": "unexpected",
        "status": None,
        "fields": [],
    }


def test_auth_diagnostic_vocabulary_and_fixture_repr_are_fixed():
    assert SAFE_BACKEND_ROUTES == {
        "/v1/owner/auth/challenge": "auth_challenge",
        "/v1/owner/auth/login": "auth_login",
        "/v1/owner/auth/session": "auth_session",
    }
    assert 418 not in SAFE_BACKEND_STATUSES
    assert repr(_SafeRuntime({"proof": "synthetic_secret_123456"})) == (
        "<local synthetic ledger runtime>"
    )


def test_node_harness_rejects_missing_selector_before_initialization(tmp_path):
    script = Path(__file__).resolve().parents[1] / "integration" / "business_ledger_bff_e2e.mjs"
    result = subprocess.run(
        ["node", str(script), "http://127.0.0.1:1", str(tmp_path), str(tmp_path / "pending.json")],
        input="",
        text=True,
        capture_output=True,
        timeout=5,
        encoding="utf-8",
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "stage": "startup",
        "code": "validation_error",
        "status": 422,
        "fields": [],
    }
    assert not (tmp_path / "pending.json").exists()


def test_sheet_readonly_case_has_no_write_or_marker_call_sites():
    script = (
        Path(__file__).resolve().parents[1] / "integration" / "business_ledger_bff_e2e.mjs"
    ).read_text(encoding="utf-8")
    start = script.index("async function runSheetReadonly()")
    end = script.index("async function sendDealForCampaign", start)
    body = script[start:end]
    assert "client.readFinance()" in body
    assert "projectForm(project)" in body
    assert "businessPosts, 0" in body
    for forbidden in (
        "client.saveDeal",
        "client.saveFinance",
        "pending.save",
        "pending.clear",
        "saveSliceCCampaign",
        "sendWithDurableMarker",
        "sendFinanceWithDurableMarker",
    ):
        assert forbidden not in body


def test_sheet_readonly_boundary_redacts_setup_launcher_and_cleanup_with_showlocals(
    tmp_path,
):
    sentinel = "sheet-private-sentinel-918273"
    test_file = tmp_path / "test_sheet_boundary_privacy.py"
    test_file.write_text(
        """
import os
import subprocess
from tests.unit.test_business_ledger_e2e import _run_sheet_readonly_boundary

def setup_failure():
    private_value = os.environ["SHEET_PRIVACY_SENTINEL"]
    raise RuntimeError(private_value)
    yield None

def launcher_runtime():
    private_value = os.environ["SHEET_PRIVACY_SENTINEL"]
    try:
        yield object()
    finally:
        _ = private_value

def cleanup_failure():
    private_value = os.environ["SHEET_PRIVACY_SENTINEL"]
    yield object()
    raise RuntimeError(private_value)

def test_setup():
    _run_sheet_readonly_boundary(setup_failure, lambda _runtime: None)

def test_launcher():
    def explode(_runtime):
        private_value = os.environ["SHEET_PRIVACY_SENTINEL"]
        raise subprocess.TimeoutExpired(
            cmd=["node", private_value],
            timeout=1,
            output=private_value,
            stderr=private_value,
        )
    _run_sheet_readonly_boundary(launcher_runtime, explode)

def test_cleanup():
    _run_sheet_readonly_boundary(cleanup_failure, lambda _runtime: None)
""".strip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["SHEET_PRIVACY_SENTINEL"] = sentinel
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "--showlocals", "-q"],
        text=True,
        capture_output=True,
        timeout=30,
        encoding="utf-8",
        env=environment,
    )
    rendered = result.stdout + result.stderr
    assert result.returncode == 1
    assert rendered.count("sheet-readonly TEST failed:") == 3
    assert "stage=setup code=unexpected" in rendered
    assert "stage=launcher code=unexpected" in rendered
    assert "stage=cleanup code=unexpected" in rendered
    assert sentinel not in rendered


@pytest.mark.parametrize(
    ("control_type", "control_message"),
    ((KeyboardInterrupt, "original-keyboard-interrupt"), (SystemExit, "original-system-exit")),
)
def test_sheet_readonly_boundary_closes_runtime_and_preserves_original_control(
    control_type, control_message
):
    events = []

    def runtime():
        try:
            yield object()
        finally:
            events.append("closed")
            raise SystemExit("cleanup-control-must-not-replace-original")

    def interrupt(_runtime):
        raise control_type(control_message)

    with pytest.raises(control_type) as captured:
        _run_sheet_readonly_boundary(runtime, interrupt)

    assert captured.value.args == (control_message,)
    assert events == ["closed"]


def test_selected_sheet_readonly_disabled_guard_remains_skipped(tmp_path):
    environment = os.environ.copy()
    environment["MIA_BUSINESS_LEDGER_E2E_CASE"] = "sheet-readonly"
    environment.pop("MIA_BUSINESS_LEDGER_E2E_ENABLED", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__)}::test_owner_client_bff_sheet_readonly_finance_acceptance",
            "-q",
            "-ra",
            f"--basetemp={tmp_path / 'disabled-guard'}",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        encoding="utf-8",
        env=environment,
    )
    rendered = result.stdout + result.stderr
    assert result.returncode == 0
    assert re.search(r"^s\s+\[100%\]", rendered)
    assert "sheet-readonly TEST failed:" not in rendered


def test_slice_c_persists_exact_inflight_step_across_marker_crash_windows():
    script = (
        Path(__file__).resolve().parents[1] / "integration" / "business_ledger_bff_e2e.mjs"
    ).read_text(encoding="utf-8")
    initial = script.index("beginCampaignStep(campaign, 'schedule');")
    replay = script.index("beginCampaignStep(campaign, 'schedule_replay');")
    assert initial < script.index("sendFinanceWithDurableMarker('schedule', schedule);", initial)
    assert replay < script.index("sendFinanceWithDurableMarker('schedule', schedule);", replay)
    recovery = script[
        script.index("async function recoverPendingBeforeWrites()") : script.index(
            "function exactDeal"
        )
    ]
    assert "const step = campaign.inflight;" in recovery
    assert "campaign.completed.includes('schedule') ? 'schedule_replay'" not in recovery
    completion = script[
        script.index("function completeCampaignStep") : script.index(
            "async function recoverPendingBeforeWrites"
        )
    ]
    persisted = completion.index("saveSliceCCampaign(campaign);")
    marker_clear = completion.index("finishMarkedWrite(marker);")
    inflight_clear = completion.index("campaign.inflight = null;")
    assert persisted < marker_clear < inflight_clear
