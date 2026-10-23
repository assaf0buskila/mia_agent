"""V2-02: phase timing on the owner OS and owner auth routes.

The backend emits a Server-Timing header with the fixed names auth, db, script, ledger, verify and
total (integer milliseconds, no other parameter) only for authenticated requests, and never on a
401 or 403 envelope. Duration log lines carry stage names, outcomes, reason codes and integer
milliseconds only.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta

import app.api.owner_sessions as owner_sessions_api
import pytest
from app.api.assaf_os import router as assaf_os_router
from app.api.owner_os_boundary import OwnerOSBoundary, owner_http_error, owner_validation_error
from app.api.owner_sessions import OwnerSessionPrincipal, owner_scope
from app.core import owner_timing
from app.core.owner_timing import owner_stage
from app.services import assaf_os as port
from fastapi import Depends, FastAPI, Response
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.exceptions import HTTPException

from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

sessions = auth.sessions

SENTINEL = "ZZ-SENTINEL-9f3c71"
ORIGIN = {"Origin": auth.ORIGIN}
METRICS = ("auth", "db", "script", "ledger", "verify", "total")
OS_STAGES = ("os_auth", "os_read", "os_commit", "os_verify", "os_mirror")
_PART = re.compile(r"(auth|db|script|ledger|verify|total);dur=(\d+)")
_SUMMARY_LINE = re.compile(
    r"owner_os_request op=[a-z_]+ status=\d{3} total_ms=\d+ rid=[0-9a-f]{32}"
)
_LOG_LINE = re.compile(
    r"owner_stage stage=(os_auth|os_read|os_commit|os_verify|os_mirror) "
    r"metric=(auth|db|script|ledger|verify|none) outcome=(ok|error|cancelled) "
    r"latency_ms=\d+ reason=(none|[a-z][a-z0-9_]{1,47}) rid=([0-9a-f]{32}|none)"
)


@pytest.fixture(autouse=True)
def clear_decision_binding_cache():
    port._clear_assaf_os_decision_binding_cache()
    yield
    port._clear_assaf_os_decision_binding_cache()


# --------------------------------------------------------------------------- helpers


def _pending(config):
    value = assaf._pending(config, "OS")
    value["input"] = {"title": SENTINEL, "status": value["input"]["status"]}
    value["after"] = {"task_id": "T_123", "title": SENTINEL}
    return value


def _transport(config, *, delay: float = 0.0, calls: list | None = None):
    def respond(_url, body, _budget):
        request = json.loads(json.loads(body)["payload"])
        operation = request["operation"]
        if calls is not None:
            calls.append(operation)
        time.sleep(delay)
        if operation == "schema":
            result = assaf._schema(config)
        elif operation == "snapshot":
            result = assaf._snapshot(config)
        elif operation in {"propose", "proposal"}:
            result = _pending(config)
        else:
            result = assaf._decision_result(config, request)
        return assaf.signed(body, result, config)

    return respond


def _app(sessions, config) -> FastAPI:
    app = auth._app(sessions, config)
    app.include_router(assaf_os_router)

    @app.get("/v1/owner/os/probe-header")
    def probe_header(_: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read"))):
        return Response(
            "{}",
            media_type="application/json",
            headers={"Server-Timing": f"evil;dur=1, db;desc={SENTINEL};dur=2"},
        )

    @app.get("/v1/owner/os/probe-crash")
    def probe_crash(_: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read"))):
        raise RuntimeError(SENTINEL)

    @app.get("/public-probe")
    def public_probe(_: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read"))):
        return {"ok": True}

    app.add_middleware(OwnerOSBoundary)
    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    return app


def _browser(sessions, config) -> TestClient:
    return TestClient(_app(sessions, config), base_url=auth.ORIGIN)


def _timing(response) -> dict[str, int]:
    values = response.headers.get_list("server-timing")
    assert len(values) == 1, values
    parsed: dict[str, int] = {}
    for part in values[0].split(", "):
        match = _PART.fullmatch(part)
        assert match is not None, values[0]
        assert match.group(1) not in parsed
        parsed[match.group(1)] = int(match.group(2))
    assert list(parsed) == [name for name in METRICS if name in parsed], "fixed order"
    return parsed


def _propose_body() -> dict:
    return {
        "request_id": assaf.REQUEST,
        "entity": "tasks",
        "record_id": None,
        "expected_revision": 0,
        "input": {"title": SENTINEL, "status": "לביצוע"},
    }


def _stage_lines(lines: list[str]) -> list[str]:
    """The owner_stage lines; every other line on the logger must be a request summary."""
    assert all(_LOG_LINE.fullmatch(line) or _SUMMARY_LINE.fullmatch(line) for line in lines), lines
    return [line for line in lines if _LOG_LINE.fullmatch(line)]


def _owner_lines(caplog) -> list[str]:
    return [
        record.getMessage() for record in caplog.records if record.name == "mia.owner_timing"
    ]


# ------------------------------------------------------------- owner_timing contract


def test_unknown_stage_name_raises() -> None:
    with pytest.raises(ValueError):
        with owner_stage("not_a_stage"):
            pass
    with pytest.raises(ValueError):
        with owner_stage(SENTINEL):
            pass


@pytest.mark.parametrize("stage", OS_STAGES)
def test_os_stages_are_allow_listed_and_log_codes_and_ms_only(stage, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
        with owner_stage(stage):
            pass
    (line,) = _owner_lines(caplog)
    assert _LOG_LINE.fullmatch(line), line
    assert f"stage={stage} " in line and "outcome=ok" in line and "reason=none" in line


def test_os_stage_error_logs_a_reason_code_never_the_message(caplog) -> None:
    class Coded(Exception):
        code = "source_unavailable"

    class Loose(Exception):
        code = f"Not A Code {SENTINEL}"

    with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
        for error in (Coded(SENTINEL), Loose(SENTINEL), RuntimeError(SENTINEL)):
            with pytest.raises(type(error)):
                with owner_stage("os_commit", metric="script"):
                    raise error
    lines = _owner_lines(caplog)
    assert len(lines) == 3
    assert all(_LOG_LINE.fullmatch(line) for line in lines), lines
    assert "outcome=error" in lines[0] and "reason=source_unavailable" in lines[0]
    assert "reason=error" in lines[1] and "reason=error" in lines[2]
    assert SENTINEL not in " ".join(lines)


def test_metric_names_are_fixed_and_only_os_stages_take_one() -> None:
    for stage, metric in (("os_auth", "desc"), ("os_auth", SENTINEL), ("model", "auth")):
        with pytest.raises(ValueError):
            with owner_stage(stage, metric=metric):
                pass
    with pytest.raises(ValueError):
        owner_timing.RequestTiming().add("edge", 1)
    with pytest.raises(ValueError):
        owner_timing.owner_phase("edge", "os_auth")
    with pytest.raises(ValueError):
        owner_timing.owner_phase("auth", "model")


def test_request_timing_header_rules() -> None:
    timing = owner_timing.RequestTiming()
    timing.add("script", 1234)
    timing.add("auth", 7)
    timing.add("script", 6.9)
    timing.add("db", -5)
    assert timing.header(200) == "", "unauthenticated requests never get a header"
    timing.authenticated = True
    assert timing.header(200) == "auth;dur=7, db;dur=0, script;dur=1240"
    assert timing.header(503) == timing.header(200), "authenticated error envelopes keep it"
    assert timing.header(401) == "" and timing.header(403) == ""
    assert owner_timing.RequestTiming().header(200) == ""


def test_stage_adds_to_the_active_request_only() -> None:
    timing = owner_timing.RequestTiming()
    with timing.activate():
        with owner_stage("os_read", metric="script"):
            time.sleep(0.03)
        owner_timing.mark_owner_authenticated()
    header = timing.header(200)
    assert re.fullmatch(r"script;dur=\d+", header), header
    assert int(header.split("=")[1]) >= 20
    with owner_stage("os_read", metric="script"):  # no active request: nothing to add to
        pass
    assert timing.header(200) == header
    owner_timing.mark_owner_authenticated()  # and nothing to mark
    assert timing.header(200) == header


def test_phase_outside_a_request_is_silent(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="mia.owner_timing"):
        with owner_timing.owner_phase("script", "os_read"):
            pass
    assert _owner_lines(caplog) == []


def test_port_call_outside_a_request_logs_nothing(monkeypatch, caplog) -> None:
    config = assaf.settings()
    monkeypatch.setattr(port, "_post", _transport(config))
    with caplog.at_level(logging.DEBUG):
        result = port.AssafOsPort(config, auth.OWNER_ID, actor_source="MIA").snapshot()
    assert result["workspace_id"] == config.assaf_os_workspace_id
    assert _owner_lines(caplog) == []


# ----------------------------------------------------------------- success envelopes


def test_snapshot_success_carries_allow_listed_server_timing(sessions, monkeypatch, caplog) -> None:
    config = assaf.settings()
    calls: list[str] = []
    monkeypatch.setattr(port, "_post", _transport(config, delay=0.05, calls=calls))
    with _browser(sessions, config) as browser:
        auth._login(browser)
        with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
            response = browser.get("/v1/owner/os/unified", headers=ORIGIN)
    assert response.status_code == 200 and calls == ["snapshot"]
    timing = _timing(response)
    assert {"auth", "script", "verify"} <= set(timing)
    assert set(timing) <= set(METRICS) and "ledger" not in timing
    assert timing["script"] >= 40, "the Apps Script round trip is the dominant phase"
    assert response.headers["cache-control"] == "private, no-store"
    lines = _owner_lines(caplog)
    stage_lines = _stage_lines(lines)
    assert stage_lines, lines
    stages = {_LOG_LINE.fullmatch(line).group(1) for line in stage_lines}
    assert {"os_auth", "os_read", "os_verify"} <= stages and "os_commit" not in stages


def test_writes_log_the_commit_stage(sessions, monkeypatch, caplog) -> None:
    config = assaf.settings()
    monkeypatch.setattr(port, "_post", _transport(config, delay=0.02))
    with _browser(sessions, config) as browser:
        session = auth._login(browser)
        headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
            proposed = browser.post(
                "/v1/owner/os/unified/proposals", headers=headers, json=_propose_body()
            )
            approved = browser.post(
                f"/v1/owner/os/unified/proposals/{assaf.PROPOSAL}/approve",
                headers=headers,
                json={"request_id": assaf._pending(config)["approval_request_id"]},
            )
    assert proposed.status_code == 200 and approved.status_code == 200
    assert _timing(proposed)["script"] >= 15 and _timing(approved)["script"] >= 15
    stages = {_LOG_LINE.fullmatch(line).group(1) for line in _stage_lines(_owner_lines(caplog))}
    assert {"os_commit", "os_verify"} <= stages


def test_session_status_and_successful_login_carry_auth_timing(sessions, monkeypatch) -> None:
    config = assaf.settings()
    with _browser(sessions, config) as browser:
        challenge = browser.get("/v1/owner/auth/challenge", headers=ORIGIN)
        assert challenge.status_code == 200
        assert "server-timing" not in challenge.headers, "no credential proven yet"
        csrf = challenge.json()["csrf_token"]
        forged = json.loads(auth._proof())
        forged["hash"] = "0" * 64
        denied = browser.post(
            "/v1/owner/auth/login",
            headers={**ORIGIN, "X-CSRF-Token": csrf},
            json={"login_proof": json.dumps(forged)},
        )
        assert denied.status_code == 401 and "server-timing" not in denied.headers
        login = browser.post(
            "/v1/owner/auth/login",
            headers={**ORIGIN, "X-CSRF-Token": csrf},
            json={"login_proof": auth._proof()},
        )
        assert login.status_code == 200
        assert "auth" in _timing(login)
        status = browser.get("/v1/owner/auth/session", headers=ORIGIN)
        assert status.status_code == 200 and "auth" in _timing(status)


# ------------------------------------------------- authenticated error envelopes keep it


def test_authenticated_error_envelopes_carry_the_header(sessions, monkeypatch) -> None:
    config = assaf.settings()
    monkeypatch.setattr(port, "_post", _transport(config))
    with _browser(sessions, config) as browser:
        session = auth._login(browser)
        headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}

        invalid_id = browser.get("/v1/owner/os/unified/proposals/!", headers=ORIGIN)
        assert invalid_id.status_code == 422
        assert invalid_id.json()["error"]["code"] == "invalid_proposal_id"
        assert "auth" in _timing(invalid_id)

        invalid_body = browser.post(
            "/v1/owner/os/unified/proposals", headers=headers, json={"request_id": "x"}
        )
        assert invalid_body.status_code == 422
        assert invalid_body.json()["error"]["code"] == "validation_error"
        assert "auth" in _timing(invalid_body)

        mismatch = browser.post(
            f"/v1/owner/os/unified/proposals/{assaf.PROPOSAL}/approve",
            headers=headers,
            json={"request_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "decision_request_mismatch"
        assert {"auth", "script", "verify"} <= set(_timing(mismatch))

        def unavailable(_url, _body, _budget):
            time.sleep(0.03)
            raise port.AssafOsError("source_unavailable")

        monkeypatch.setattr(port, "_post", unavailable)
        down = browser.get("/v1/owner/os/unified", headers=ORIGIN)
        assert down.status_code == 503 and down.json()["error"]["code"] == "source_unavailable"
        timing = _timing(down)
        assert timing["script"] >= 20, "a failed round trip is still measured"
        assert "verify" not in timing


def test_unexpected_exception_after_auth_is_sanitized_and_keeps_the_header(
    sessions, caplog
) -> None:
    config = assaf.settings()
    with _browser(sessions, config) as browser:
        auth._login(browser)
        with caplog.at_level(logging.DEBUG):
            response = browser.get("/v1/owner/os/probe-crash", headers=ORIGIN)
    assert response.status_code == 503
    assert response.json()["error"] == {"code": "source_unavailable", "retryable": True}
    assert "auth" in _timing(response)
    assert SENTINEL not in response.text
    assert SENTINEL not in json.dumps(dict(response.headers))


# ------------------------------------------- 401 and 403 envelopes never carry timings


def _expire(sessions, sql: str, **params) -> None:
    with sessions() as db:
        db.execute(text(sql), params)
        db.commit()


@pytest.mark.parametrize(
    "case",
    [
        "no_cookie",
        "unknown_cookie",
        "expired_session",
        "revoked_session",
        "removed_owner",
        "scope_removed",
        "wrong_origin",
        "csrf_missing",
        "csrf_wrong",
    ],
)
def test_401_and_403_envelopes_carry_no_server_timing(sessions, monkeypatch, case) -> None:
    config = assaf.settings()
    monkeypatch.setattr(port, "_post", _transport(config))
    with _browser(sessions, config) as browser:
        route, headers, method = "/v1/owner/os/unified", dict(ORIGIN), "get"
        if case not in {"no_cookie", "unknown_cookie"}:
            auth._login(browser)
            baseline = browser.get(route, headers=ORIGIN)
            assert baseline.status_code == 200
            assert "server-timing" in baseline.headers, "the same session is timed while valid"
        if case == "unknown_cookie":
            browser.cookies.set(
                owner_sessions_api.SESSION_COOKIE, "u" * 43, domain=auth.ORIGIN.split("//")[1]
            )
        if case == "expired_session":
            past = datetime.now(UTC) - timedelta(minutes=5)
            _expire(sessions, "UPDATE owner_os_sessions SET expires_at = :t", t=past)
        if case == "revoked_session":
            _expire(sessions, "UPDATE owner_os_sessions SET revoked_at = :t", t=datetime.now(UTC))
        if case == "removed_owner":
            config.telegram_owner_user_ids = "99999999"
        if case == "scope_removed":
            config.owner_os_session_scopes = auth.READ_SCOPE
        if case == "wrong_origin":
            headers = {"Origin": "https://attacker.invalid"}
        if case in {"csrf_missing", "csrf_wrong"}:
            method = "post"
            route = "/v1/owner/os/unified/proposals"
            headers = {**ORIGIN}
            if case == "csrf_wrong":
                headers["X-CSRF-Token"] = "w" * 64
        response = (
            browser.get(route, headers=headers)
            if method == "get"
            else browser.post(route, headers=headers, json=_propose_body())
        )
        if case == "no_cookie":
            assert response.status_code == 401
        if case == "unknown_cookie":
            sent = response.request.headers.get("cookie", "")
            assert f"{owner_sessions_api.SESSION_COOKIE}=" in sent, "the bogus cookie was sent"
    assert response.status_code in {401, 403}, (case, response.status_code)
    assert "server-timing" not in response.headers, case
    assert response.json()["error"]["code"] in {
        "owner_auth_required",
        "owner_scope_denied",
        "origin_denied",
        "csrf_denied",
    }


def test_denied_auth_routes_carry_no_server_timing(sessions) -> None:
    config = assaf.settings()
    with _browser(sessions, config) as browser:
        anonymous = browser.get("/v1/owner/auth/session", headers=ORIGIN)
        assert anonymous.status_code == 401 and "server-timing" not in anonymous.headers
        logout = browser.post("/v1/owner/auth/logout", headers=ORIGIN)
        assert logout.status_code in {401, 403} and "server-timing" not in logout.headers
        revoke = browser.post("/v1/owner/auth/revoke-all", headers=ORIGIN)
        assert revoke.status_code in {401, 403} and "server-timing" not in revoke.headers


def test_boundary_rejections_before_authentication_carry_no_server_timing(sessions) -> None:
    config = assaf.settings()
    with _browser(sessions, config) as browser:
        oversized = browser.post(
            "/v1/owner/os/unified/proposals", headers=ORIGIN, content=b"{" + b" " * 20_000 + b"}"
        )
        wrong_type = browser.post(
            "/v1/owner/os/unified/proposals",
            headers={**ORIGIN, "Content-Type": "text/plain"},
            content=b"{}",
        )
        malformed = browser.post(
            "/v1/owner/os/unified/proposals",
            headers={**ORIGIN, "Content-Type": "application/json"},
            content=b"{not json",
        )
        query = browser.get("/v1/owner/auth/session?x=1", headers=ORIGIN)
    assert [r.status_code for r in (oversized, wrong_type, malformed, query)] == [
        413,
        415,
        422,
        400,
    ]
    for response in (oversized, wrong_type, malformed, query):
        assert "server-timing" not in response.headers


# --------------------------------------------------------------------- header hygiene


def test_upstream_server_timing_is_replaced_by_the_allow_list(sessions) -> None:
    config = assaf.settings()
    with _browser(sessions, config) as browser:
        auth._login(browser)
        response = browser.get("/v1/owner/os/probe-header", headers=ORIGIN)
    assert response.status_code == 200
    assert set(_timing(response)) <= set(METRICS)
    assert "evil" not in response.headers["server-timing"]
    assert SENTINEL not in response.headers["server-timing"]
    assert "desc" not in response.headers["server-timing"]


def test_routes_outside_the_private_prefixes_get_no_server_timing(sessions) -> None:
    config = assaf.settings()
    with _browser(sessions, config) as browser:
        auth._login(browser)
        response = browser.get("/public-probe", headers=ORIGIN)
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert "server-timing" not in response.headers
        legacy = browser.get("/read", headers=ORIGIN)
        assert "server-timing" not in legacy.headers


def test_timings_do_not_accumulate_across_requests(sessions, monkeypatch) -> None:
    config = assaf.settings()
    slow = {"delay": 0.12}

    def respond(url, body, _budget):
        return _transport(config, delay=slow["delay"])(url, body, _budget)

    monkeypatch.setattr(port, "_post", respond)
    with _browser(sessions, config) as browser:
        auth._login(browser)
        first = _timing(browser.get("/v1/owner/os/unified", headers=ORIGIN))
        status = _timing(browser.get("/v1/owner/auth/session", headers=ORIGIN))
        slow["delay"] = 0.0
        third = _timing(browser.get("/v1/owner/os/unified", headers=ORIGIN))
    assert first["script"] >= 100
    assert "script" not in status, "the next request starts from zero"
    assert third["script"] < 100


# ---------------------------------------------------------- the session database phase


class _AuditedDb:
    """sqlite stand-in: PostgreSQL-only SET LOCAL statements are skipped."""

    def __init__(self, real):
        self._real = real

    def execute(self, statement, *args, **kwargs):
        if str(statement).startswith("SET LOCAL"):
            return None
        return self._real.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_db_phase_comes_from_the_real_session_dependency(sessions, monkeypatch, caplog) -> None:
    config = assaf.settings()
    config.owner_os_session_database_url = "postgresql://dedicated.invalid/owner_os"
    monkeypatch.setattr(
        owner_sessions_api, "_session_factory", lambda _url: (None, lambda: _AuditedDb(sessions()))
    )
    monkeypatch.setattr(owner_sessions_api, "_validate_runtime_role", lambda _db: time.sleep(0.05))
    app = auth._app(sessions, config)
    app.dependency_overrides.pop(owner_sessions_api.get_owner_session_db)
    app.include_router(assaf_os_router)
    app.add_middleware(OwnerOSBoundary)
    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    with TestClient(app, base_url=auth.ORIGIN) as browser:
        auth._login(browser)
        with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
            response = browser.get("/v1/owner/auth/session", headers=ORIGIN)
    assert response.status_code == 200
    timing = _timing(response)
    assert timing["db"] >= 40 and "auth" in timing
    assert any("metric=db" in line for line in _owner_lines(caplog))


# ------------------------------------------------------------------- privacy sentinel


def test_sentinel_never_reaches_logs_or_headers(sessions, monkeypatch, caplog) -> None:
    config = assaf.settings()
    monkeypatch.setattr(port, "_post", _transport(config))
    responses = []
    with caplog.at_level(logging.DEBUG):
        with _browser(sessions, config) as browser:
            session = auth._login(browser)
            headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
            responses.append(
                browser.post(
                    "/v1/owner/os/unified/proposals", headers=headers, json=_propose_body()
                )
            )
            responses.append(
                browser.post(
                    f"/v1/owner/os/unified/proposals/{assaf.PROPOSAL}/approve",
                    headers=headers,
                    json={"request_id": assaf._pending(config)["approval_request_id"]},
                )
            )
            responses.append(browser.get("/v1/owner/os/probe-crash", headers=ORIGIN))
            responses.append(browser.get("/v1/owner/os/probe-header", headers=ORIGIN))

            def explode(_url, _body, _budget):
                raise RuntimeError(f"transport failed for {SENTINEL}")

            monkeypatch.setattr(port, "_post", explode)
            responses.append(browser.get("/v1/owner/os/unified", headers=ORIGIN))
    assert [r.status_code for r in responses] == [200, 200, 503, 200, 503]
    assert all("server-timing" in r.headers for r in responses)
    for response in responses:
        assert SENTINEL not in json.dumps(dict(response.headers))
    assert SENTINEL not in caplog.text
    lines = _owner_lines(caplog)
    assert lines and _stage_lines(lines), lines
