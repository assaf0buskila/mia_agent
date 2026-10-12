"""Signed transport and real owner-session boundary tests for Assaf OS."""

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from app.api.assaf_os import router
from app.api.owner_os_boundary import OwnerOSBoundary, owner_http_error, owner_validation_error
from app.api.owner_sessions import _business_binding, _configured_scopes
from app.services import assaf_os as port
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
REQUEST = "12345678-1234-4234-8234-123456789012"
PROPOSAL = "PR_12345678901234567890"


def settings():
    value = auth._settings()
    value.assaf_os_enabled = True
    value.assaf_os_script_url = "https://script.google.com/macros/s/" + "x" * 24 + "/exec"
    value.assaf_os_workbook_id = "workbook_12345678901234567890"
    value.assaf_os_workspace_id = auth.WORKSPACE
    value.assaf_os_hmac_secret = "assaf-os-test-secret-at-least-32-bytes"
    value.assaf_os_mode = "TEST"
    value.owner_os_session_scopes += ",assaf-os:read,assaf-os:propose,assaf-os:approve"
    return value


def _schema(config):
    return {
        "api_version": "assaf-os.v1",
        "source": "google_sheets",
        "mode": config.assaf_os_mode,
        "workbook_id": config.assaf_os_workbook_id,
        "workspace_id": config.assaf_os_workspace_id,
        "schema_version": "assaf-os-unified.v1",
        "money_unit": "minor",
        "tabs": [],
        "enums": {},
    }


def _snapshot(config):
    return {
        "api_version": "assaf-os.v1",
        "source": "google_sheets",
        "mode": config.assaf_os_mode,
        "workbook_id": config.assaf_os_workbook_id,
        "workspace_id": config.assaf_os_workspace_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "coverage": "unknown",
        "entity_coverage": {key: "unknown" for key in port.RECORD_ENTITIES},
        "records": {key: [] for key in port.RECORD_ENTITIES},
        "summary": {"coverage": "unknown", "monthly": []},
        "issues": [],
        "pending_proposals": [],
    }


def _pending(config, source="MIA"):
    return {
        "api_version": "assaf-os.v1",
        "source": "google_sheets",
        "actor_source": source,
        "mode": config.assaf_os_mode,
        "workbook_id": config.assaf_os_workbook_id,
        "workspace_id": config.assaf_os_workspace_id,
        "status": "pending",
        "proposal_id": PROPOSAL,
        "request_id": REQUEST,
        "approval_request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "rejection_request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "expires_at": "2026-10-12T09:10:00.000Z",
        "entity": "tasks",
        "record_id": None,
        "expected_revision": 0,
        "input": {"title": "Follow up", "status": "לביצוע"},
        "before": None,
        "after": {"task_id": "T_123", "title": "Follow up"},
    }


def signed(body, result, config, *, mutate=None, valid_signature=True):
    request = json.loads(json.loads(body)["payload"])
    inner = {key: value for key, value in request.items() if key != "data"}
    inner.update(mode=config.assaf_os_mode, ok=True, data=result)
    if mutate:
        mutate(inner)
    payload = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    signature = hmac.new(
        config.assaf_os_hmac_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    if not valid_signature:
        signature = "0" * 64
    return json.dumps({"payload": payload, "signature": signature}).encode()


def client(sessions, config):
    app = auth._app(sessions, config)
    app.include_router(router)
    app.add_middleware(OwnerOSBoundary)
    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    return TestClient(app, base_url=auth.ORIGIN)


def test_default_off_scopes_and_mia_cannot_decide():
    config = settings()
    assert {"assaf-os:read", "assaf-os:propose", "assaf-os:approve"} <= _configured_scopes(config)
    config.assaf_os_enabled = False
    assert not {"assaf-os:read", "assaf-os:propose", "assaf-os:approve"} & _configured_scopes(
        config
    )
    with pytest.raises(port.AssafOsError) as denied:
        port.AssafOsPort(settings(), auth.OWNER_ID, actor_source="MIA").decide(
            "approve", PROPOSAL, port.DecisionRequest(request_id=REQUEST)
        )
    assert denied.value.code == "forbidden"


def test_assaf_only_scopes_receive_stable_dedicated_recovery_binding():
    config = settings()
    config.owner_os_session_scopes = "assaf-os:read,assaf-os:propose,assaf-os:approve"
    first = _business_binding(config, auth.OWNER_ID)
    assert first and first == _business_binding(config, auth.OWNER_ID)
    assert first != _business_binding(config, "99999999")
    config.assaf_os_workspace_id = "different-workspace"
    assert first != _business_binding(config, auth.OWNER_ID)


def test_actual_schema_and_nullable_monthly_summary_shape_are_accepted(monkeypatch):
    config = settings()
    schema = _schema(config)
    schema["tabs"] = [
        {
            "key": "summary",
            "fields": [{"key": key} for key in sorted(port.SUMMARY_FIELDS)],
        }
    ]
    snapshot = _snapshot(config)
    snapshot["coverage"] = "partial"
    snapshot["entity_coverage"]["payments"] = "partial"
    snapshot["summary"] = {
        "coverage": "partial",
        "monthly": [
            {
                "month": "2026-10",
                "currency": "ILS",
                "paid_income_ils": None,
                "paid_expenses_ils": 12345,
                "net_cash_ils": None,
                "recorded_minutes": 90,
                "effective_hourly_ils": None,
                "coverage": "partial",
                "reason_codes": ["payments_coverage_partial"],
            }
        ],
    }
    responses = iter((schema, snapshot))
    monkeypatch.setattr(
        port, "_post", lambda _url, body: signed(body, next(responses), config)
    )
    adapter = port.AssafOsPort(config, auth.OWNER_ID, actor_source="OS")
    assert "summary_scope" in {f["key"] for f in adapter.schema()["tabs"][0]["fields"]}
    assert adapter.snapshot()["summary"]["monthly"][0]["paid_income_ils"] is None


def test_receipt_lookup_uses_fresh_transport_id_and_preserves_logical_id(monkeypatch):
    config = settings()
    captured = {}

    def respond(_url, body):
        request = json.loads(json.loads(body)["payload"])
        captured.update(request)
        result = {
            **{key: value for key, value in _schema(config).items() if key in {
                "api_version", "source", "mode", "workbook_id", "workspace_id"
            }},
            "actor_source": "MIA",
            "status": "unknown",
            "request_id": REQUEST,
            "proposal_id": None,
            "entity": None,
            "record_id": None,
            "revision": None,
            "committed_at": None,
            "reason_codes": ["receipt_not_found"],
        }
        return signed(body, result, config)

    monkeypatch.setattr(port, "_post", respond)
    result = port.AssafOsPort(config, auth.OWNER_ID, actor_source="MIA").receipt(REQUEST)
    assert captured["request_id"] != REQUEST
    assert captured["data"] == {"request_id": REQUEST}
    assert result["status"] == "unknown" and result["request_id"] == REQUEST


def test_actual_committed_receipt_with_reason_codes_is_accepted(monkeypatch):
    config = settings()

    def respond(_url, body):
        request = json.loads(json.loads(body)["payload"])
        result = {
            "api_version": "assaf-os.v1",
            "source": "google_sheets",
            "actor_source": "MIA",
            "mode": config.assaf_os_mode,
            "workbook_id": config.assaf_os_workbook_id,
            "workspace_id": config.assaf_os_workspace_id,
            "status": "committed",
            "request_id": REQUEST,
            "proposal_id": PROPOSAL,
            "entity": "tasks",
            "record_id": "T_123",
            "revision": 1,
            "committed_at": "2026-10-12T09:02:00.000Z",
            "reason_codes": [],
        }
        assert request["data"]["request_id"] == REQUEST
        return signed(body, result, config)

    monkeypatch.setattr(port, "_post", respond)
    receipt = port.AssafOsPort(config, auth.OWNER_ID, actor_source="MIA").receipt(REQUEST)
    assert receipt["status"] == "committed" and receipt["reason_codes"] == []


def test_browser_decision_binds_stable_request_id_to_proposal_before_post(monkeypatch):
    config = settings()
    calls = []

    def respond(_url, body):
        request = json.loads(json.loads(body)["payload"])
        calls.append(request)
        assert request["operation"] == "proposal"
        return signed(body, _pending(config, "OS"), config)

    monkeypatch.setattr(port, "_post", respond)
    adapter = port.AssafOsPort(config, auth.OWNER_ID, actor_source="OS")
    with pytest.raises(port.AssafOsError) as mismatch:
        adapter.decide(
            "approve",
            PROPOSAL,
            port.DecisionRequest(request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        )
    assert mismatch.value.code == "decision_request_mismatch"
    assert [call["operation"] for call in calls] == ["proposal"]


def test_kill_switch_blocks_mutation_before_transport(monkeypatch):
    config = settings()
    config.kill_switch = True
    monkeypatch.setattr(port, "_post", lambda *_: pytest.fail("transport touched"))
    adapter = port.AssafOsPort(config, auth.OWNER_ID, actor_source="OS")
    with pytest.raises(port.AssafOsError) as blocked:
        adapter.propose(
            port.ProposeRequest(
                request_id=REQUEST,
                entity="tasks",
                record_id=None,
                expected_revision=0,
                input={"title": "Follow up"},
            )
        )
    assert (blocked.value.code, blocked.value.status) == ("kill_switch", 403)


@pytest.mark.parametrize(
    "field,value",
    [("workbook_id", "other_workbook_1234567890"), ("mode", "LIVE"), ("actor_source", "OS")],
)
def test_signed_response_binding_tamper_is_rejected(monkeypatch, field, value):
    config = settings()
    monkeypatch.setattr(
        port,
        "_post",
        lambda _url, body: signed(
            body, _pending(config), config, mutate=lambda inner: inner.update({field: value})
        ),
    )
    with pytest.raises(port.AssafOsError) as failure:
        port.AssafOsPort(config, auth.OWNER_ID, actor_source="MIA").propose(
            port.ProposeRequest(
                request_id=REQUEST,
                entity="tasks",
                record_id=None,
                expected_revision=0,
                input={"title": "Follow up", "status": "לביצוע"},
            )
        )
    assert failure.value.code == "unknown"


def test_bad_signature_and_lost_propose_response_are_unknown(monkeypatch):
    config = settings()
    monkeypatch.setattr(
        port,
        "_post",
        lambda _url, body: signed(body, _pending(config), config, valid_signature=False),
    )
    adapter = port.AssafOsPort(config, auth.OWNER_ID, actor_source="MIA")
    change = port.ProposeRequest(
        request_id=REQUEST,
        entity="tasks",
        record_id=None,
        expected_revision=0,
        input={"title": "Follow up", "status": "לביצוע"},
    )
    with pytest.raises(port.AssafOsError) as failure:
        adapter.propose(change)
    assert failure.value.code == "unknown"
    monkeypatch.setattr(
        port, "_post", lambda *_: (_ for _ in ()).throw(port.AssafOsError("source_unavailable"))
    )
    with pytest.raises(port.AssafOsError) as lost:
        adapter.propose(change)
    assert lost.value.code == "unknown"


def test_real_session_scope_origin_csrf_and_removed_owner(sessions, monkeypatch):
    config, calls = settings(), []

    def respond(_url, body):
        request = json.loads(json.loads(body)["payload"])
        calls.append(request)
        result = _schema(config) if request["operation"] == "schema" else _pending(config, "OS")
        return signed(body, result, config)

    monkeypatch.setattr(port, "_post", respond)
    with client(sessions, config) as browser:
        assert (
            browser.get("/v1/owner/os/unified/schema", headers={"Origin": auth.ORIGIN}).status_code
            == 401
        )
        session = auth._login(browser)
        assert (
            browser.get("/v1/owner/os/unified/schema", headers={"Origin": auth.ORIGIN}).status_code
            == 200
        )
        body = {
            "request_id": REQUEST,
            "entity": "tasks",
            "record_id": None,
            "expected_revision": 0,
            "input": {"title": "Follow up", "status": "לביצוע"},
        }
        count = len(calls)
        assert (
            browser.post(
                "/v1/owner/os/unified/proposals", headers={"Origin": auth.ORIGIN}, json=body
            ).status_code
            == 403
        )
        assert (
            browser.post(
                "/v1/owner/os/unified/proposals",
                headers={
                    "Origin": "https://attacker.invalid",
                    "X-CSRF-Token": session["csrf_token"],
                },
                json=body,
            ).status_code
            == 403
        )
        assert len(calls) == count
        good = browser.post(
            "/v1/owner/os/unified/proposals",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": session["csrf_token"]},
            json=body,
        )
        assert good.status_code == 200 and calls[-1]["actor_source"] == "OS"
        config.kill_switch = True
        call_count = len(calls)
        blocked = browser.post(
            "/v1/owner/os/unified/proposals",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": session["csrf_token"]},
            json=body,
        )
        assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "kill_switch"
        assert len(calls) == call_count
        config.kill_switch = False
        config.telegram_owner_user_ids = "99999999"
        assert (
            browser.get("/v1/owner/os/unified/schema", headers={"Origin": auth.ORIGIN}).status_code
            == 401
        )
