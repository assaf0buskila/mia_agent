"""V2-16s scaffold: api.py, the §3.3 settings block, the boundary caps and the v2 errors."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.api.os_v2.errors import ledger_error_handler, write_route
from app.api.owner_os_boundary import OwnerOSBoundary, body_cap
from app.core import config
from app.core.config import Settings
from app.prototypes import assaf_os_domain
from app.services.os_ledger import api
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def fresh_coercion_log(monkeypatch):
    monkeypatch.setattr(config, "_OS_CONFIG_COERCED", set())


# ---------------------------------------------------------------------------------------------
# api.py


def test_api_parity_with_the_prototype_domain():
    assert api.LEDGER_API == "os-ledger.v1"
    assert api.ENTITIES == assaf_os_domain.ENTITIES
    assert dict(api.ID_KEYS) == dict(assaf_os_domain.ID_KEYS)
    with pytest.raises(TypeError):
        api.ID_KEYS["leads"] = "x"  # type: ignore[index]


def test_dependents_mirror_every_v2_14_record_foreign_key():
    sql = (ROOT / "migrations_assaf_os" / "20261015_assaf_os_ledger.sql").read_text("utf-8")
    edges = set()
    for table, body in re.findall(r"CREATE TABLE assaf_os\.os_(\w+) \((.*?)\n\);", sql, re.S):
        for column, target in re.findall(
            r"FOREIGN KEY \(workspace_id, (\w+)\)\s+"
            r"REFERENCES assaf_os\.os_(\w+)\(workspace_id, record_id\)",
            body,
        ):
            if table in api.ENTITIES:
                edges.add((table, column, target))
    assert edges == set(api.DEPENDENTS)
    assert len(api.DEPENDENTS) == len(set(api.DEPENDENTS))


@pytest.mark.parametrize(
    ("entity", "record", "deleted", "live"),
    [
        ("payments", {"kind": "התקבל"}, False, True),
        ("payments", {"kind": "בוטל"}, False, False),
        ("payments", {"kind": "החזר"}, True, False),
        ("works", {"status": "פעילה"}, False, True),
        ("works", {"status": "בוטלה"}, False, False),
        ("time", {"status": "פעיל"}, False, True),
        ("time", {"status": "בוטל"}, False, False),
        ("tasks", {"status": "לביצוע"}, False, True),
        ("tasks", {"status": "בתהליך"}, False, True),
        ("tasks", {"status": "ממתין"}, False, True),
        ("tasks", {"status": "בוצע"}, False, False),
        ("tasks", {"status": "בוטל"}, False, False),
        ("tasks", {"status": "לביצוע"}, True, False),
        ("clients", {"status": "לא פעיל"}, False, True),
    ],
)
def test_is_live(entity, record, deleted, live):
    assert api.is_live(entity, record, deleted=deleted) is live


def test_since_token_round_trip_and_refusals():
    head = api.Head(
        observed_version=12,
        authority_epoch=1,
        states={"core": "active", "statements": "shadow"},
        pending_seq=7,
        mirror_verified_version=11,
    )
    token = api.since_token(head)
    assert token == "12.7.11"
    assert api.parse_since(token) == (12, 7, 11)
    assert api.parse_since("0.0.0") == (0, 0, 0)
    for bad in (
        "",
        "1.2",
        "1.2.3.4",
        "a.b.c",
        "1.2.-3",
        " 1.2.3",
        "1.2.3\n",
        "١.2.3",
        "1" * 20 + ".1.1",
        123,
        None,
    ):
        with pytest.raises(api.LedgerError) as caught:
            api.parse_since(bad)  # type: ignore[arg-type]
        assert caught.value.code == "invalid_query"
        assert caught.value.status == 400


def test_digest_is_canonical_and_strict():
    assert api.digest({"b": 1, "a": "שלום"}) == api.digest({"a": "שלום", "b": 1})
    assert api.digest([1, 2]) != api.digest([2, 1])
    for bad in (float("nan"), float("inf"), {"d": datetime.now(UTC)}):
        with pytest.raises((ValueError, TypeError)):
            api.digest(bad)


def test_canonical_uuid_is_strict():
    v4 = "12345678-1234-4234-8234-123456789abc"
    assert api.canonical_uuid(v4) == v4
    assert api.canonical_uuid(v4, version=4) == v4
    v5 = api.mia_request_uuid("1", "ws", "src")
    assert api.canonical_uuid(v5, version=5) == v5
    for bad in (v4.upper(), "{" + v4 + "}", v4.replace("-", ""), "", None, 5, v4 + " "):
        with pytest.raises(api.LedgerError) as caught:
            api.canonical_uuid(bad)  # type: ignore[arg-type]
        assert caught.value.code == "invalid_request_id"
        assert caught.value.status == 422
    with pytest.raises(api.LedgerError):
        api.canonical_uuid(v5, version=4)


def test_request_uuids_are_deterministic_uuid5_in_one_namespace():
    assert str(api.REQUEST_NS) == "f3ed116a-a84c-5293-89b8-5c412ef2bfd5"
    pairs = [
        (api.mia_request_uuid("7", "ws", "src"), api.mia_request_uuid("7", "ws", "src")),
        (api.paste_request_uuid("7", "b1"), api.paste_request_uuid("7", "b1")),
        (api.assistant_request_uuid("cred", "b1"), api.assistant_request_uuid("cred", "b1")),
        (api.import_request_uuid(1, "d" * 64), api.import_request_uuid(1, "d" * 64)),
    ]
    for first, second in pairs:
        assert first == second
        assert api.canonical_uuid(first, version=5) == first
    assert api.import_request_uuid(1, "d" * 64) != api.import_request_uuid(2, "d" * 64)
    assert api.paste_request_uuid("7", "b1") != api.assistant_request_uuid("7", "b1")


def test_error_table_is_complete_and_exact():
    assert len(api.ERROR_CODES) == 51  # 50 + cycle_has_statement (§22 E31)
    unknown = api.LedgerError("unknown")
    assert (unknown.status, unknown.write_outcome, unknown.retryable) == (503, "unknown", False)
    for code, spec in api.ERROR_CODES.items():
        error = api.LedgerError(code)
        assert error.status == spec.status
        assert error.write_outcome == ("unknown" if code == "unknown" else "not_started")
        assert error.details == {}
    retryable = {code for code, spec in api.ERROR_CODES.items() if spec.retryable}
    assert retryable == {"rate_limited", "ledger_unavailable", "os_frozen"}
    assert api.LedgerError("invalid_trusted_context", 403).status == 403
    with pytest.raises(ValueError):
        api.LedgerError("invalid_trusted_context", 401)
    with pytest.raises(ValueError):
        api.LedgerError("no_such_code")
    with pytest.raises(ValueError):
        api.LedgerError("ledger_unavailable", details={"reason": "workspace_missing"})
    conflict = api.LedgerError("undo_conflict", details={"reason": "record_changed", "changed": []})
    assert dict(conflict.details) == {"reason": "record_changed", "changed": []}
    logged = api.LedgerError("ledger_unavailable", reason="workspace_missing")
    assert logged.reason == "workspace_missing" and logged.details == {}
    assert api.LedgerError("ledger_unavailable", reason="Bad Text!").reason == "error"


def test_trusted_context_needs_the_capability():
    fields = dict(
        numeric_owner=42,
        workspace="ws",
        authority_epoch=1,
        channel="OS",
        scopes=frozenset({"assaf-os:read"}),
        auth_expires_at=datetime.now(UTC),
        browser_mutation_verified=True,
        credential_id=None,
    )
    ctx = api._issue(**fields)
    assert ctx.numeric_owner == 42
    assert "_capability" not in repr(ctx)
    with pytest.raises(FrozenInstanceError):
        ctx.numeric_owner = 43  # type: ignore[misc]
    for forged in (object(), None):
        with pytest.raises(api.LedgerError) as caught:
            api.TrustedOwnerContext(**fields, _capability=forged)
        assert caught.value.code == "invalid_trusted_context"


def test_core_authority_state_is_none_without_a_dsn():
    assert api.core_authority_state(object()) is None
    assert api.core_authority_state(Settings(_env_file=None)) is None


def test_lazy_entry_points_fail_closed_when_the_implementation_is_missing(monkeypatch):
    class WithDsn:
        owner_os_ledger_database_url = "postgresql://ledger@127.0.0.1:1/x"

    # §22 E13: V2-16 has landed, so simulate a missing implementation module.
    monkeypatch.setattr(
        api.importlib,
        "import_module",
        lambda *a, **k: (_ for _ in ()).throw(ImportError("x")),
    )
    with pytest.raises(api.LedgerError) as caught:
        api.core_authority_state(WithDsn())
    assert (caught.value.code, caught.value.reason) == (
        "ledger_unavailable",
        "implementation_missing",
    )
    with pytest.raises(api.LedgerError) as caught:
        api.build_ledger(WithDsn())
    assert caught.value.code == "ledger_unavailable"


def test_core_authority_state_maps_read_failures(monkeypatch):
    class WithDsn:
        owner_os_ledger_database_url = "postgresql://ledger@127.0.0.1:1/x"

    def broken(_settings):
        raise OSError("connection refused with secret text")

    monkeypatch.setattr(api, "_implementation", lambda module, name: broken)
    with pytest.raises(api.LedgerError) as caught:
        api.core_authority_state(WithDsn())
    assert (caught.value.code, caught.value.reason) == ("ledger_unavailable", "read_failed")

    monkeypatch.setattr(api, "_implementation", lambda module, name: lambda _s: "active")
    assert api.core_authority_state(WithDsn()) == "active"


# ---------------------------------------------------------------------------------------------
# §3.3 settings block

DEFAULTS = {
    "assaf_os_backend": "sheets",
    "assaf_os_ledger_enabled": False,
    "assaf_os_authority_epoch": 0,
    "assaf_os_freeze": False,
    "assaf_os_owner_direct_enabled": False,
    "owner_undo_window_seconds": 30,
    "assaf_os_statements_enabled": False,
    "os_statement_pepper": "",
    "assaf_os_mirror_enabled": False,
    "os_mirror_spreadsheet_id": "",
    "os_mirror_service_account_json": "",
    "os_mirror_lag_warn_seconds": 300,
    "os_mirror_anti_entropy_seconds": 900,
    "assaf_os_assistant_enabled": False,
    "os_assistant_ip_hmac_key": "",
    "os_step_up_secret": "",
}
BAD = {
    "assaf_os_backend": ["Postgres", "mysql", 1],
    "assaf_os_ledger_enabled": ["maybe", "2", [1]],
    "assaf_os_authority_epoch": ["-1", "abc", "1.5", True, "9" * 25],
    "assaf_os_freeze": ["sure", {}],
    "assaf_os_owner_direct_enabled": ["enable"],
    "owner_undo_window_seconds": ["9", "61", "thirty", False],
    "assaf_os_statements_enabled": ["x"],
    "os_statement_pepper": [123, None, "short", "x" * 31],
    "assaf_os_mirror_enabled": ["y e s"],
    "os_mirror_spreadsheet_id": [42, "a,b", "a b", "id/with/slash"],
    "os_mirror_service_account_json": [{"k": "v"}],
    "os_mirror_lag_warn_seconds": ["59", "3601"],
    "os_mirror_anti_entropy_seconds": ["299", "3601"],
    "assaf_os_assistant_enabled": ["nope"],
    "os_assistant_ip_hmac_key": [b"bytes", "k" * 31],
    "os_step_up_secret": [1.5, "too-short"],
}
GOOD = {
    "assaf_os_backend": ("postgres", "postgres"),
    "assaf_os_ledger_enabled": ("true", True),
    "assaf_os_authority_epoch": ("3", 3),
    "assaf_os_freeze": ("1", True),
    "assaf_os_owner_direct_enabled": ("on", True),
    "owner_undo_window_seconds": ("60", 60),
    "assaf_os_statements_enabled": ("yes", True),
    "os_statement_pepper": ("p" * 32, "p" * 32),
    "assaf_os_mirror_enabled": ("TRUE", True),
    "os_mirror_spreadsheet_id": ("sheet-1", "sheet-1"),
    "os_mirror_service_account_json": ("{}", "{}"),
    "os_mirror_lag_warn_seconds": ("60", 60),
    "os_mirror_anti_entropy_seconds": ("3600", 3600),
    "assaf_os_assistant_enabled": ("false", False),
    "os_assistant_ip_hmac_key": ("k" * 32, "k" * 32),
    "os_step_up_secret": ("s" * 32, "s" * 32),
}


def test_block_is_complete_and_default_off():
    assert set(config._OS_CONFIG_RULES) == set(DEFAULTS) == set(BAD) == set(GOOD)
    settings = Settings(_env_file=None)
    for name, default in DEFAULTS.items():
        assert getattr(settings, name) == default, name
    assert config.os_config_coerced() is False


def test_secrets_never_appear_in_repr():
    settings = Settings(
        _env_file=None,
        os_statement_pepper="PEPPER" * 6,
        os_mirror_service_account_json="SA-JSON",
        os_assistant_ip_hmac_key="IPKEY" * 7,
        os_step_up_secret="STEPUP" * 6,
    )
    text = repr(settings)
    for secret in ("PEPPER", "SA-JSON", "IPKEY", "STEPUP"):
        assert secret not in text


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_bad_value_coerces_to_the_default_and_never_fails_the_load(name, caplog):
    caplog.set_level(logging.WARNING, logger="mia.config")
    for value in BAD[name]:
        settings = Settings(_env_file=None, **{name: value})
        assert getattr(settings, name) == DEFAULTS[name], (name, value)
    assert config.os_config_coerced() is True
    lines = [r.getMessage() for r in caplog.records if r.name == "mia.config"]
    assert lines == [f"os_config op=load outcome=coerced reason=config_invalid field={name}"]


@pytest.mark.parametrize("name", sorted(GOOD))
def test_a_valid_value_is_kept(name):
    raw, parsed = GOOD[name]
    assert getattr(Settings(_env_file=None, **{name: raw}), name) == parsed
    assert config.os_config_coerced() is False


def test_env_values_are_coerced_too_and_empty_means_unset(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="mia.config")
    monkeypatch.setenv("MIA_ASSAF_OS_BACKEND", "oracle")
    monkeypatch.setenv("MIA_OWNER_UNDO_WINDOW_SECONDS", "600")
    monkeypatch.setenv("MIA_ASSAF_OS_FREEZE", "")
    monkeypatch.setenv("MIA_ASSAF_OS_AUTHORITY_EPOCH", "  ")
    monkeypatch.setenv("MIA_ASSAF_OS_LEDGER_ENABLED", "true")
    settings = Settings(_env_file=None)
    assert settings.assaf_os_backend == "sheets"
    assert settings.owner_undo_window_seconds == 30
    assert settings.assaf_os_freeze is False
    assert settings.assaf_os_authority_epoch == 0
    assert settings.assaf_os_ledger_enabled is True
    fields = sorted(r.getMessage().rsplit("=", 1)[1] for r in caplog.records)
    assert fields == ["assaf_os_backend", "owner_undo_window_seconds"]
    for record in caplog.records:
        assert "oracle" not in record.getMessage() and "600" not in record.getMessage()


def test_health_reports_coercion():
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").json()["os_config_coerced"] is False
    Settings(_env_file=None, assaf_os_backend="bogus")
    assert client.get("/health").json()["os_config_coerced"] is True


# ---------------------------------------------------------------------------------------------
# Sheets allow-list subtraction


def _allowed(**overrides):
    return Settings(
        _env_file=None,
        sheets_allowed_spreadsheet_ids="extra-1,os-book,mirror-book",
        assaf_os_workbook_id="os-book",
        **overrides,
    ).allowed_sheets_spreadsheet_ids()


def test_mirror_workbook_is_always_subtracted():
    assert "mirror-book" in _allowed()
    assert "mirror-book" not in _allowed(os_mirror_spreadsheet_id="mirror-book")
    assert "mirror-book" not in _allowed(
        os_mirror_spreadsheet_id=" mirror-book ", assaf_os_backend="sheets"
    )


def test_os_workbook_is_subtracted_only_when_frozen_or_on_postgres():
    assert "os-book" in _allowed()
    assert "os-book" in _allowed(assaf_os_backend="sheets", assaf_os_freeze=False)
    assert "os-book" not in _allowed(assaf_os_freeze=True)
    assert "os-book" not in _allowed(assaf_os_backend="postgres")
    # §22 E9: an invalid backend coerces, and a coerced value fails toward subtracting.
    assert "os-book" not in _allowed(assaf_os_backend="postgresql")
    assert {"extra-1"} <= _allowed(assaf_os_freeze=True, os_mirror_spreadsheet_id="mirror-book")


def test_default_allow_list_is_unchanged():
    from app.surfaces.crm import LOCKED_SPREADSHEET_ID

    assert Settings(_env_file=None).allowed_sheets_spreadsheet_ids() == frozenset(
        {LOCKED_SPREADSHEET_ID}
    )


# ---------------------------------------------------------------------------------------------
# Boundary: every non-GET body is buffered, capped, content-type- and duplicate-key-checked.


def _boundary_client():
    inner = FastAPI()
    seen = []

    @inner.api_route("/v1/owner/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def echo(rest: str, request: Request):
        body = await request.body()
        seen.append((request.method, rest, len(body)))
        return {"ok": True}

    return TestClient(OwnerOSBoundary(inner)), seen


def _json(size):
    filler = "x" * max(size - len('{"a":""}'), 0)
    return json.dumps({"a": filler}, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    ("path", "cap"),
    [
        ("/v1/owner/os/unified/proposals", 16_384),
        ("/v1/owner/os/unified/commits", 16_384),
        ("/v1/owner/os/unified/statement-batches", 131_072),
        ("/v1/owner/os/unified/statement-batches/b1/draft", 16_384),
        ("/v1/owner/auth/step-up", 8_192),
        ("/v1/owner/auth/session", 16_384),
    ],
)
def test_per_route_body_caps(path, cap):
    assert body_cap(path) == cap
    client, seen = _boundary_client()
    headers = {"content-type": "application/json"}
    assert client.post(path, content=_json(cap), headers=headers).status_code == 200
    over = client.post(path, content=_json(cap + 1), headers=headers)
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "validation_error"
    assert len(seen) == 1


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_every_non_get_method_is_checked(method):
    client, seen = _boundary_client()
    path = "/v1/owner/os/unified/commits/x"
    ok = client.request(
        method, path, content=b'{"a":1}', headers={"content-type": "application/json"}
    )
    assert ok.status_code == 200
    cases = [
        (_json(16_385), "application/json", 413),
        (b'{"a":1,"a":2}', "application/json", 422),
        (b"[1]", "application/json", 422),
        (b"{not json", "application/json", 422),
        (b'{"a":1}', "text/plain", 415),
    ]
    for payload, content_type, status in cases:
        response = client.request(
            method, path, content=payload, headers={"content-type": content_type}
        )
        assert response.status_code == status, (payload[:20], content_type)
        assert response.headers["cache-control"] == "private, no-store"
    assert client.request(method, path).status_code == 200  # an empty body needs no content type
    assert [entry[0] for entry in seen] == [method, method]


def test_get_is_untouched_and_public_paths_are_not_buffered():
    client, seen = _boundary_client()
    assert client.get("/v1/owner/os/unified").status_code == 200
    inner = FastAPI()

    @inner.put("/public")
    async def public(request: Request):
        return {"n": len(await request.body())}

    public_client = TestClient(OwnerOSBoundary(inner))
    big = public_client.put(
        "/public", content=b"x" * 40_000, headers={"content-type": "text/plain"}
    )
    assert big.json() == {"n": 40_000}


# ---------------------------------------------------------------------------------------------
# v2 error envelope


def _error_client():
    inner = FastAPI()
    inner.add_exception_handler(api.LedgerError, ledger_error_handler)

    @inner.api_route("/v1/owner/{rest:path}", methods=["GET", "POST"])
    async def fail(rest: str, request: Request):
        code = request.query_params["code"]
        details = json.loads(request.query_params.get("details", "{}"))
        raise api.LedgerError(code, details=details, reason="workspace_missing")

    return TestClient(inner)


def test_write_routes_carry_write_outcome_and_reads_do_not(caplog):
    caplog.set_level(logging.INFO, logger="mia.os_v2")
    client = _error_client()
    write = client.post("/v1/owner/os/unified/commits?code=ledger_unavailable")
    assert write.status_code == 503
    body = write.json()
    assert set(body) == {"api_version", "request_id", "error"}
    assert body["api_version"] == "owner-os.v1"
    assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"])
    assert body["error"] == {
        "code": "ledger_unavailable",
        "retryable": True,
        "write_outcome": "not_started",
    }
    assert write.headers["cache-control"] == "private, no-store"
    unknown = client.post("/v1/owner/os/unified/commits?code=unknown").json()["error"]
    assert unknown == {"code": "unknown", "retryable": False, "write_outcome": "unknown"}
    read = client.get("/v1/owner/os/unified?code=invalid_query")
    assert read.status_code == 400
    assert read.json()["error"] == {"code": "invalid_query", "retryable": False}
    step_up = client.post("/v1/owner/auth/step-up?code=step_up_required").json()["error"]
    assert "write_outcome" not in step_up
    messages = [r.getMessage() for r in caplog.records if r.name == "mia.os_v2"]
    assert (
        "os_v2 op=fail outcome=ledger_unavailable reason=workspace_missing write=True" in messages
    )


def test_details_render_only_when_allowed():
    client = _error_client()
    details = json.dumps(
        {"reason": "record_changed", "changed": [{"entity": "tasks", "record_id": "T1"}]}
    )
    response = client.post(
        "/v1/owner/os/unified/commits/x/undo", params={"code": "undo_conflict", "details": details}
    )
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "undo_conflict",
        "retryable": False,
        "write_outcome": "not_started",
        "details": {
            "reason": "record_changed",
            "changed": [{"entity": "tasks", "record_id": "T1"}],
        },
    }
    no_details = client.post("/v1/owner/os/unified/commits?code=has_dependents").json()["error"]
    assert "details" not in no_details


def test_write_route_rule():
    assert write_route("POST", "/v1/owner/os/unified/commits")
    assert write_route("post", "/v1/assistant/os/statement-batches")
    assert not write_route("GET", "/v1/owner/os/unified")
    assert not write_route("HEAD", "/v1/owner/os/unified")
    assert not write_route("POST", "/v1/owner/auth/step-up")
    assert write_route("POST", "/v1/owner/authx")


def test_main_registers_the_ledger_error_handler_and_the_mirror_hook(monkeypatch):
    from app import main
    from app.workers import os_mirror

    assert main.app.exception_handlers[api.LedgerError] is ledger_error_handler
    assert (
        os_mirror.start_if_enabled(Settings(_env_file=None, assaf_os_mirror_enabled=True)) is None
    )
    calls = []
    monkeypatch.setattr(os_mirror, "start_if_enabled", lambda settings: calls.append(settings))
    with TestClient(main.app):
        pass
    assert len(calls) == 1


def test_erratum_e1_rev_import_surface():
    assert api.IMPORT_STATUS_EDGES == {
        ("staged", "validated"),
        ("staged", "quarantined"),
        ("validated", "imported"),
        ("staged", "failed"),
        ("validated", "failed"),
    }
    # The vocabulary is exactly the V2-14 CHECK on os_import_batches.status.
    sql = (ROOT / "migrations_assaf_os" / "20261015_assaf_os_ledger.sql").read_text("utf-8")
    check = re.search(r"os_import_batches.*?status IN \(([^)]*)\)", sql, re.S).group(1)
    assert set(re.findall(r"'(\w+)'", check)) == set(api.ImportStatus.__args__)
    assert api.LIVE_IDS_LIMIT == 100_000
    for code, status in (
        ("invalid_transaction_kind", 409),
        ("import_status_invalid", 409),
        ("local_record_limit", 409),
        ("invalid_entity", 422),
    ):
        assert (api.ERROR_CODES[code].status, api.ERROR_CODES[code].retryable) == (status, False)
    for method in ("insert_import_batch", "insert_import_rows", "set_import_status", "live_ids"):
        assert callable(getattr(api.LedgerTx, method))
    assert [f for f in api.ImportRow.__dataclass_fields__] == [
        "row_number", "entity", "record_id", "source_digest", "status",
        "raw_record", "canonical_record", "provenance", "quarantine",
    ]


# A literal copy of the §5.2 table plus the E1-rev codes: (status, retryable, details keys).
SECTION_5_2 = {
    "invalid_query": (400, False, ()),
    "owner_auth_required": (401, False, ()),
    "assistant_auth_required": (401, False, ()),
    "kill_switch": (403, False, ()),
    "scope_denied": (403, False, ()),
    "channel_operation_denied": (403, False, ()),
    "owner_approval_required": (403, False, ()),
    "browser_mutation_verification_required": (403, False, ()),
    "step_up_required": (403, False, ()),
    "invalid_trusted_context": (403, False, ()),
    "operation_unavailable": (404, False, ()),
    "proposal_not_found": (404, False, ()),
    "commit_not_found": (404, False, ()),
    "batch_not_found": (404, False, ()),
    "idempotency_mismatch": (409, False, ()),
    "decision_request_mismatch": (409, False, ()),
    "already_decided": (409, False, ()),
    "authority_changed": (409, False, ()),
    "authority_moved": (409, False, ()),
    "cursor_invalidated": (409, False, ("observed_version",)),
    "target_changed": (409, False, ("reason",)),
    "not_undoable": (409, False, ()),
    "already_undone": (409, False, ("undo_request_id",)),
    "undo_expired": (409, False, ()),
    "undo_conflict": (409, False, ("reason", "changed")),
    "has_dependents": (409, False, ("dependents",)),
    "review_stale": (409, False, ()),
    "unassigned_lines": (409, False, ()),
    "merge_choice_required": (409, False, ()),
    "cycle_has_statement": (409, False, ()),  # §22 E31
    "draft_conflict": (409, False, ()),
    "batch_conflict": (409, False, ("conflicting_proposal_id",)),
    "lead_already_exists": (409, False, ()),
    "import_quarantine_unresolved": (409, False, ("count",)),
    "manifest_digest_mismatch": (409, False, ()),
    "import_orphan_rows": (409, False, ("counts",)),
    "validation_error": (422, False, ("reason",)),
    "invalid_request_id": (422, False, ()),
    "invalid_cursor": (422, False, ()),
    "invalid_statement_batch": (422, False, ("issues",)),
    "rate_limited": (429, True, ()),
    "ledger_unavailable": (503, True, ()),
    "backend_not_postgres": (503, False, ()),
    "authority_not_active": (503, False, ()),
    "os_frozen": (503, True, ()),
    "ledger_integrity_failed": (503, False, ()),
    "unknown": (503, False, ()),
    # Erratum E1-rev (§22).
    "invalid_transaction_kind": (409, False, ()),
    "import_status_invalid": (409, False, ()),
    "local_record_limit": (409, False, ()),
    "invalid_entity": (422, False, ()),
}


def test_error_table_equals_a_literal_copy_of_section_5_2():
    actual = {
        code: (spec.status, spec.retryable, tuple(sorted(spec.details)))
        for code, spec in api.ERROR_CODES.items()
    }
    expected = {code: (s, r, tuple(sorted(d))) for code, (s, r, d) in SECTION_5_2.items()}
    assert actual == expected


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_head_and_options_are_buffered_like_any_non_get(method):
    inner = FastAPI()

    @inner.api_route("/v1/owner/os/unified/x", methods=[method])
    async def handler(request: Request):
        return {"n": len(await request.body())}

    client = TestClient(OwnerOSBoundary(inner))
    assert client.request(method, "/v1/owner/os/unified/x").status_code == 200
    over = client.request(
        method, "/v1/owner/os/unified/x", content=_json(16_385),
        headers={"content-type": "application/json"},
    )
    assert over.status_code == 413


E1_REV_SIGNATURES = {
    "insert_import_batch": (
        "(self, *, batch_id: 'str', authority_epoch: 'int', source_digest: 'str', "
        "source_binding: 'str', provenance: 'Mapping[str, Any]') -> 'None'"
    ),
    "insert_import_rows": "(self, batch_id: 'str', rows: 'Sequence[ImportRow]') -> 'int'",
    "set_import_status": (
        "(self, batch_id: 'str', status: 'ImportStatus', *, "
        "quarantine: 'Mapping[str, Any] | None' = None) -> 'None'"
    ),
    "live_ids": "(self, entity: 'Entity') -> 'frozenset[str]'",
}


def test_erratum_e1_rev_signatures_are_pinned():
    import inspect

    for method, expected in E1_REV_SIGNATURES.items():
        assert str(inspect.signature(getattr(api.LedgerTx, method))) == expected, method


@pytest.mark.parametrize(
    ("name", "raw", "parsed"),
    [
        ("assaf_os_authority_epoch", str(2**63 - 1), 2**63 - 1),
        ("os_mirror_spreadsheet_id", " sheet-1 ", "sheet-1"),
        ("os_statement_pepper", "א" * 16, "א" * 16),  # 32 bytes in UTF-8
        ("os_step_up_secret", "", ""),
    ],
)
def test_boundary_values_are_kept_without_a_flag(name, raw, parsed):
    assert getattr(Settings(_env_file=None, **{name: raw}), name) == parsed
    assert config.os_config_coerced() is False


def test_a_non_conforming_route_name_logs_a_fixed_op(caplog):
    caplog.set_level(logging.INFO, logger="mia.os_v2")
    inner = FastAPI()
    inner.add_exception_handler(api.LedgerError, ledger_error_handler)

    async def fail():
        raise api.LedgerError("ledger_unavailable")

    inner.add_api_route("/v1/owner/os/unified/x", fail, methods=["POST"], name="Bad Name; drop")
    assert TestClient(inner).post("/v1/owner/os/unified/x").status_code == 503
    messages = [r.getMessage() for r in caplog.records if r.name == "mia.os_v2"]
    assert messages == [
        "os_v2 op=route outcome=ledger_unavailable reason=ledger_unavailable write=True"
    ]
