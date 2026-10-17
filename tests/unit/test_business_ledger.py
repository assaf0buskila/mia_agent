"""Ledger boundary checks use real existing session/CSRF code, synthetic records only."""

import hashlib
import hmac
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.api.business_ledger import router
from app.api.owner_os_boundary import OwnerOSBoundary, owner_http_error, owner_validation_error
from app.api.owner_sessions import _business_binding, _configured_scopes
from app.core.config import MiaEnv
from app.services import business_ledger as port
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
KEY = "12345678-1234-4234-8234-123456789012"
ID = "12345678-1234-4234-8234-123456789013"
RECEIPT = "12345678-1234-4234-8234-123456789014"


def settings():
    value = auth._settings()
    value.business_ledger_test_enabled = True
    value.business_ledger_workspace_id = auth.WORKSPACE
    value.business_ledger_script_url = "https://script.google.com/macros/s/" + "x" * 24 + "/exec"
    value.business_ledger_hmac_secret = "synthetic-ledger-secret" * 2
    value.owner_os_session_scopes += ",business-ledger:read,business-ledger:write"
    return value


def change():
    return {
        "request_id": KEY,
        "expected_revision": 0,
        "deal": {
            "id": ID,
            "title": "עסקת TEST בלבד",
            "amount_minor": 12345,
            "currency": "ILS",
            "payment_plan": "full",
            "start_date": "2026-10-11",
            "status": "active",
            "project_id": None,
            "notes": "",
        },
    }


def receipt():
    return {
        "status": "committed",
        "request_id": KEY,
        "id": ID,
        "revision": 1,
        "receipt_id": RECEIPT,
        "committed_at": datetime.now(UTC).isoformat(),
        "source": "google_sheets",
        "mode": "TEST",
    }


def snapshot():
    return {
        "api_version": "business-ledger.v1",
        "mode": "TEST",
        "source": "google_sheets",
        "observed_at": datetime.now(UTC).isoformat(),
        "coverage": "deals_only",
        "deals": [{**change()["deal"], "revision": 1, "updated_at": datetime.now(UTC).isoformat()}],
        "totals": {
            "deal_value_minor": 12345,
            "received_minor": None,
            "outstanding_minor": None,
            "expense_minor": None,
        },
        "reason_codes": ["financial_coverage_incomplete"],
    }


def signed_reply(body, *, result=None, status="ok", overrides=None):
    request = json.loads(json.loads(body)["payload"])
    inner = {key: request[key] for key in ("request_id", "workspace_id", "owner_id")}
    inner.update(status=status, result=result or receipt())
    inner.update(overrides or {})
    payload = json.dumps(inner, ensure_ascii=False)
    signature = hmac.new(
        settings().business_ledger_hmac_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return json.dumps({"payload": payload, "signature": signature}).encode()


def client(sessions, config=None):
    app = auth._app(sessions, config or settings())
    app.include_router(router)
    app.add_middleware(OwnerOSBoundary)
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException

    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    return TestClient(app, base_url=auth.ORIGIN)


def test_scopes_default_off_independent_of_crm_writes():
    config = settings()
    assert "business-ledger:write" in _configured_scopes(config)
    assert "quick_leads.create" not in _configured_scopes(config)
    config.business_ledger_test_enabled = False
    assert "business-ledger:read" not in _configured_scopes(config)
    config.business_ledger_test_enabled = True
    config.env = MiaEnv.PROD
    assert "business-ledger:write" not in _configured_scopes(config)
    with pytest.raises(port.LedgerError):
        port.BusinessLedger(config, auth.OWNER_ID)


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount_minor", 1.01),
        ("amount_minor", True),
        ("amount_minor", 0),
        ("amount_minor", -1),
        ("amount_minor", 10**12 + 1),
        ("currency", "USD"),
        ("title", "=IMPORTXML(secret)"),
        ("notes", "@formula"),
        ("start_date", "2026-02-30"),
        ("id", "not-a-uuid"),
    ],
)
def test_invalid_finance_never_reaches_google(monkeypatch, field, value):
    monkeypatch.setattr(port, "_post", lambda *_: pytest.fail("unauthorized provider call"))
    data = change()
    data["deal"][field] = value
    with pytest.raises(ValidationError):
        port.DealWrite.model_validate(data)


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_id": "99999999"},
        {"workspace_id": "other"},
        {"request_id": ID},
    ],
)
def test_response_bound_to_request_owner_workspace(monkeypatch, overrides):
    monkeypatch.setattr(port, "_post", lambda _, body: signed_reply(body, overrides=overrides))
    with pytest.raises(port.LedgerError, match="unknown"):
        port.BusinessLedger(settings(), auth.OWNER_ID).save(port.DealWrite.model_validate(change()))


def test_forged_signature_and_false_zero_rejected(monkeypatch):
    monkeypatch.setattr(port, "_post", lambda *_: b'{"payload":"{}","signature":"fake"}')
    with pytest.raises(port.LedgerError, match="source_unavailable"):
        port.BusinessLedger(settings(), auth.OWNER_ID).snapshot()
    data = snapshot()
    data["totals"]["received_minor"] = 0
    monkeypatch.setattr(port, "_post", lambda _, body: signed_reply(body, result=data))
    with pytest.raises(port.LedgerError, match="source_unavailable"):
        port.BusinessLedger(settings(), auth.OWNER_ID).snapshot()


def test_lost_response_unknown_no_automatic_retry_then_receipt(monkeypatch):
    calls = []

    def lost(_, body):
        calls.append(body)
        raise port.LedgerError("source_unavailable")

    monkeypatch.setattr(port, "_post", lost)
    service = port.BusinessLedger(settings(), auth.OWNER_ID)
    with pytest.raises(port.LedgerError, match="unknown"):
        service.save(port.DealWrite.model_validate(change()))
    assert len(calls) == 1
    monkeypatch.setattr(port, "_post", lambda _, body: signed_reply(body))
    assert service.receipt(KEY)["receipt_id"] == RECEIPT


def test_signed_provider_failure_is_unknown_for_write(monkeypatch):
    monkeypatch.setattr(port, "_post", lambda _, body: signed_reply(body, status="unavailable"))
    with pytest.raises(port.LedgerError, match="unknown"):
        port.BusinessLedger(settings(), auth.OWNER_ID).save(port.DealWrite.model_validate(change()))


def test_noncredential_recovery_binding_is_stable_owner_workspace_scoped():
    config = settings()
    binding = _business_binding(config, auth.OWNER_ID)
    assert binding and len(binding) == 64 and auth.OWNER_ID not in binding
    assert binding == _business_binding(config, auth.OWNER_ID)
    assert binding != _business_binding(config, "99999999")
    config.owner_os_workspace_id = "other"
    assert binding != _business_binding(config, auth.OWNER_ID)
    config.business_ledger_test_enabled = False
    assert _business_binding(config, auth.OWNER_ID) is None


def test_actual_shared_javascript_snapshot_satisfies_python_and_mobile_contracts():
    backend = Path(__file__).resolve().parents[2]
    mobile = (
        backend.parent / "owner-os-business-ledger-mobile" / "prototypes" / "assafweb-os-mobile"
    )
    if not (mobile / "contracts.mjs").is_file():
        pytest.skip("Cross-repository check requires the isolated mobile ledger checkout")
    script = """
      const core = require('./ops/business-ledger/core.js');
      const {readBusinessSnapshot} = await import(process.argv[1]);
      const input = JSON.parse(process.argv[2]);
      const now = new Date().toISOString();
      const accepted = {...input.deal, revision:1, updated_at:now};
      const snapshot = core.snapshot([accepted],now);
      const view = readBusinessSnapshot(snapshot);
      if(view.totals.received_minor!==null) throw Error('false_zero');
      process.stdout.write(JSON.stringify(snapshot));
    """
    result = subprocess.run(
        [
            "node",
            "--input-type=commonjs",
            "-e",
            "(async()=>{" + script + "})().catch(()=>process.exit(1));",
            (mobile / "contracts.mjs").as_uri(),
            json.dumps(change()),
        ],
        cwd=backend,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    accepted = port.LedgerSnapshot.model_validate_json(result.stdout)
    assert accepted.deals[0].id == ID
    assert accepted.totals.deal_value_minor == 12345
    assert accepted.totals.received_minor is None


def test_real_auth_origin_csrf_logout_and_no_store(sessions, monkeypatch):
    calls = []

    def respond(_, body):
        calls.append(json.loads(json.loads(body)["payload"]))
        result = snapshot() if calls[-1]["operation"] == "snapshot" else receipt()
        return signed_reply(body, result=result)

    monkeypatch.setattr(port, "_post", respond)
    with client(sessions) as browser:
        assert (
            browser.get("/v1/owner/os/business", headers={"Origin": auth.ORIGIN}).status_code == 401
        )
        session = auth._login(browser)
        read = browser.get("/v1/owner/os/business", headers={"Origin": auth.ORIGIN})
        assert read.status_code == 200, read.text
        assert read.json()["totals"]["received_minor"] is None
        assert read.headers["cache-control"] == "private, no-store"
        count = len(calls)
        for headers in ({"Origin": "https://attacker.invalid"}, {"Origin": auth.ORIGIN}):
            response = browser.post("/v1/owner/os/business/deals", headers=headers, json=change())
            assert response.status_code == 403
        assert len(calls) == count
        headers = {"Origin": auth.ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        write = browser.post("/v1/owner/os/business/deals", headers=headers, json=change())
        assert write.status_code == 200, write.text
        assert write.json()["receipt_id"] == RECEIPT
        assert browser.post("/v1/owner/auth/logout", headers=headers).status_code == 204
        assert (
            browser.get("/v1/owner/os/business", headers={"Origin": auth.ORIGIN}).status_code == 401
        )


def test_removed_owner_and_disabled_scope_cannot_call_provider(sessions, monkeypatch):
    config = settings()
    monkeypatch.setattr(port, "_post", lambda *_: pytest.fail("unauthorized provider call"))
    with client(sessions, config) as browser:
        auth._login(browser)
        config.telegram_owner_user_ids = "99999999"
        assert (
            browser.get("/v1/owner/os/business", headers={"Origin": auth.ORIGIN}).status_code == 401
        )
        config.telegram_owner_user_ids = auth.OWNER_ID
        config.business_ledger_test_enabled = False
        assert (
            browser.get("/v1/owner/os/business", headers={"Origin": auth.ORIGIN}).status_code == 403
        )
