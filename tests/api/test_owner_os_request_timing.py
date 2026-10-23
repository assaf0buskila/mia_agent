"""OS-RUNTIME F5: server-side click-to-verified timing on the owner OS routes.

A request id joins the server logs to the phone, the Server-Timing header gains a whole-request
``total`` and a ``ledger`` emitter, and every private response is summarised by one log line.
All of it carries random ids, fixed codes and integer milliseconds only: never a body, a record, a
path id, a proposal id, a name, a phone or a text.
"""

from __future__ import annotations

import logging
import re

import pytest
from app.core import owner_timing
from app.core.owner_timing import RequestTiming

from tests.api import test_owner_os_timing as base
from tests.api.os_v2.v2_19_support import OwnerApp
from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
ORIGIN = base.ORIGIN
ORDER = ("auth", "db", "script", "ledger", "verify", "total")
_RID = re.compile(r"[0-9a-f]{32}")
_PART = re.compile(r"(auth|db|script|ledger|verify|total);dur=(\d+)")
_SUMMARY = re.compile(
    r"owner_os_request op=(?P<op>[a-z_]+) status=(?P<status>\d{3}) "
    r"total_ms=(?P<ms>\d+) rid=(?P<rid>[0-9a-f]{32})"
)
_STAGE_RID = re.compile(r" rid=(?P<rid>[0-9a-f]{32}|none)$")


@pytest.fixture(autouse=True)
def clear_decision_binding_cache():
    assaf.port._clear_assaf_os_decision_binding_cache()
    yield
    assaf.port._clear_assaf_os_decision_binding_cache()


def timing_of(response) -> dict[str, int]:
    values = response.headers.get_list("server-timing")
    assert len(values) == 1, values
    parsed: dict[str, int] = {}
    for part in values[0].split(", "):
        match = _PART.fullmatch(part)
        assert match is not None, values[0]
        assert match.group(1) not in parsed
        parsed[match.group(1)] = int(match.group(2))
    assert list(parsed) == [name for name in ORDER if name in parsed], "fixed order"
    return parsed


def lines_of(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "mia.owner_timing"]


def summaries(caplog) -> list[re.Match]:
    return [m for m in map(_SUMMARY.fullmatch, lines_of(caplog)) if m]


@pytest.fixture
def ledger_owner(monkeypatch):
    from app.services.os_ledger import api

    from tests.api.os_v2.v2_19_support import ledger_world

    env = ledger_world()
    monkeypatch.setattr(api, "build_ledger", lambda _settings: env.fake)
    with OwnerApp() as owner:
        yield owner



# ------------------------------------------------------------------------- the request id


def test_the_request_id_scope_is_a_contextvar_that_resets():
    assert owner_timing.current_request_id() == "none"
    with owner_timing.request_id_scope("a" * 32):
        assert owner_timing.current_request_id() == "a" * 32
        with owner_timing.request_id_scope("b" * 32):
            assert owner_timing.current_request_id() == "b" * 32
        assert owner_timing.current_request_id() == "a" * 32
    assert owner_timing.current_request_id() == "none"


def test_total_is_a_metric_only_the_boundary_may_set():
    assert owner_timing.SERVER_TIMING_METRICS == ORDER
    with pytest.raises(ValueError):
        owner_timing.owner_phase("total", "os_read")
    with pytest.raises(ValueError):
        with owner_timing.owner_stage("os_read", metric="total"):
            pass
    timing = RequestTiming()
    timing.add("total", 12)
    timing.authenticated = True
    assert timing.header(200) == "total;dur=12"
    assert timing.header(401) == "" and timing.header(403) == ""


def test_the_os_stage_line_ends_with_the_rid(caplog):
    with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
        with owner_timing.owner_stage("os_read", metric="script"):
            pass
        with owner_timing.request_id_scope("c" * 32):
            with owner_timing.owner_stage("os_commit"):
                pass
    first, second = lines_of(caplog)
    assert first.endswith("reason=none rid=none"), first
    assert second == (
        "owner_stage stage=os_commit metric=none outcome=ok "
        f"latency_ms={re.search(r'latency_ms=(\d+)', second).group(1)} reason=none rid={'c' * 32}"
    )


def test_every_private_response_carries_a_fresh_x_request_id(sessions, monkeypatch):
    config = assaf.settings()
    monkeypatch.setattr(base.port, "_post", base._transport(config))
    seen = []
    with base._browser(sessions, config) as browser:
        anonymous = browser.get("/v1/owner/os/unified", headers=ORIGIN)
        session = auth._login(browser)
        headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        responses = [
            anonymous,  # 401
            browser.get("/v1/owner/os/unified", headers=ORIGIN),  # 200
            browser.get("/v1/owner/os/unified", headers={"Origin": "https://attacker.invalid"}),
            browser.get("/v1/owner/os/unified/proposals/!", headers=ORIGIN),  # 422 envelope
            browser.post("/v1/owner/os/unified/proposals", headers=headers, json={"x": 1}),
            browser.post(  # 415 before any handler
                "/v1/owner/os/unified/proposals",
                headers={**ORIGIN, "Content-Type": "text/plain"}, content=b"{}",
            ),
            browser.post(  # 413 before any handler
                "/v1/owner/os/unified/proposals", headers=ORIGIN,
                content=b"{" + b" " * 20_000 + b"}",
            ),
            browser.get("/v1/owner/auth/session?x=1", headers=ORIGIN),  # 400 invalid_query
            browser.get("/v1/owner/os/probe-crash", headers=ORIGIN),  # 503 sanitized
        ]
        for response in responses:
            assert response.headers.get_list("x-request-id") != [], response.status_code
            seen.extend(response.headers.get_list("x-request-id"))
    assert [r.status_code for r in responses] == [401, 200, 403, 422, 422, 415, 413, 400, 503]
    assert all(_RID.fullmatch(value) for value in seen), seen
    assert len(seen) == len(set(seen)) == len(responses), "one fresh id per response"


def test_the_error_envelope_request_id_is_the_response_header(sessions, monkeypatch):
    config = assaf.settings()
    monkeypatch.setattr(base.port, "_post", base._transport(config))
    with base._browser(sessions, config) as browser:
        anonymous = browser.get("/v1/owner/os/unified", headers=ORIGIN)
        session = auth._login(browser)
        headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        errors = [
            anonymous,
            browser.get("/v1/owner/os/unified", headers={"Origin": "https://attacker.invalid"}),
            browser.get("/v1/owner/os/unified/proposals/!", headers=ORIGIN),
            browser.post("/v1/owner/os/unified/proposals", headers=headers, json={"x": 1}),
            browser.post(
                "/v1/owner/os/unified/proposals",
                headers={**ORIGIN, "Content-Type": "text/plain"}, content=b"{}",
            ),
            browser.post(
                "/v1/owner/os/unified/proposals", headers=ORIGIN,
                content=b"{" + b" " * 20_000 + b"}",
            ),
            browser.get("/v1/owner/auth/session?x=1", headers=ORIGIN),
            browser.get("/v1/owner/os/probe-crash", headers=ORIGIN),
        ]
    for response in errors:
        assert response.status_code >= 400
        assert response.json()["request_id"] == response.headers["x-request-id"], (
            response.status_code
        )
        assert _RID.fullmatch(response.json()["request_id"])


def test_the_error_envelope_outside_a_request_still_gets_a_fresh_id():
    from app.api.owner_os_boundary import error_response

    first = error_response(503, "source_unavailable")
    second = error_response(503, "source_unavailable")
    ids = [__import__("json").loads(r.body)["request_id"] for r in (first, second)]
    assert all(_RID.fullmatch(value) for value in ids) and ids[0] != ids[1]


def test_the_v2_ledger_envelope_uses_the_same_request_id(ledger_owner):
    response = ledger_owner.client.get(
        "/v1/owner/os/unified/pending?bogus=1", headers=ledger_owner.login()
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert _RID.fullmatch(response.headers["x-request-id"])


# --------------------------------------------------------------------------- Server-Timing total


def test_total_is_the_last_server_timing_name_and_covers_the_script_call(sessions, monkeypatch):
    config = assaf.settings()
    monkeypatch.setattr(base.port, "_post", base._transport(config, delay=0.05))
    with base._browser(sessions, config) as browser:
        auth._login(browser)
        response = browser.get("/v1/owner/os/unified", headers=ORIGIN)
    assert response.status_code == 200
    timing = timing_of(response)
    print(
        f"OS-RUNTIME F5 server-timing={response.headers['server-timing']!r} "
        f"x-request-id={response.headers['x-request-id']}"
    )
    assert list(timing)[-1] == "total", timing
    assert timing["total"] >= timing["script"] >= 40
    assert timing["total"] >= timing["auth"] + timing["script"] + timing["verify"] - 5


def test_no_total_and_no_server_timing_on_401_and_403(sessions, monkeypatch, caplog):
    config = assaf.settings()
    monkeypatch.setattr(base.port, "_post", base._transport(config))
    with base._browser(sessions, config) as browser:
        with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
            no_cookie = browser.get("/v1/owner/os/unified", headers=ORIGIN)
            auth._login(browser)
            wrong_origin = browser.get(
                "/v1/owner/os/unified", headers={"Origin": "https://attacker.invalid"}
            )
            csrf = browser.post("/v1/owner/os/unified/proposals", headers=ORIGIN, json={})
    assert [no_cookie.status_code, wrong_origin.status_code, csrf.status_code] == [401, 403, 403]
    for response in (no_cookie, wrong_origin, csrf):
        assert "server-timing" not in response.headers
        assert "total" not in " ".join(response.headers.values())
        assert _RID.fullmatch(response.headers["x-request-id"])
    # The refusals are still summarised in the server log, by status, never in a header.
    statuses = sorted(
        int(m["status"]) for m in summaries(caplog) if m["op"] in {"snapshot", "propose"}
    )
    assert statuses == [401, 403, 403]


def test_a_crash_after_authentication_keeps_total(sessions):
    config = assaf.settings()
    with base._browser(sessions, config) as browser:
        auth._login(browser)
        response = browser.get("/v1/owner/os/probe-crash", headers=ORIGIN)
    assert response.status_code == 503
    assert list(timing_of(response))[-1] == "total"


# ------------------------------------------------------------------- summary line and stage rid


def test_one_summary_line_per_request_shares_its_rid_with_the_stage_lines(
    sessions, monkeypatch, caplog
):
    config = assaf.settings()
    monkeypatch.setattr(base.port, "_post", base._transport(config, delay=0.02))
    with base._browser(sessions, config) as browser:
        session = auth._login(browser)
        headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
            caplog.clear()
            snapshot = browser.get("/v1/owner/os/unified", headers=ORIGIN)
            snapshot_lines = list(lines_of(caplog))
            caplog.clear()
            proposed = browser.post(
                "/v1/owner/os/unified/proposals", headers=headers, json=base._propose_body()
            )
            propose_lines = list(lines_of(caplog))
    assert snapshot.status_code == proposed.status_code == 200
    for response, observed, op in (
        (snapshot, snapshot_lines, "snapshot"),
        (proposed, propose_lines, "propose"),
    ):
        rid = response.headers["x-request-id"]
        summary = [m for m in map(_SUMMARY.fullmatch, observed) if m]
        print(f"OS-RUNTIME F5 summary_line={summary[0].group(0)!r}" if summary else "no summary")
        assert len(summary) == 1, observed
        assert summary[0]["op"] == op and summary[0]["status"] == "200"
        assert summary[0]["rid"] == rid
        assert int(summary[0]["ms"]) == timing_of(response)["total"]
        stages = [line for line in observed if line.startswith("owner_stage ")]
        assert stages and all(base._LOG_LINE.fullmatch(line) for line in stages), stages
        assert {_STAGE_RID.search(line)["rid"] for line in stages} == {rid}


def test_the_summary_line_is_not_logged_for_other_prefixes(sessions, caplog):
    config = assaf.settings()
    with base._browser(sessions, config) as browser:
        auth._login(browser)
        with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
            caplog.clear()
            browser.get("/public-probe", headers=ORIGIN)
    assert summaries(caplog) == []


@pytest.mark.parametrize(
    ("method", "path", "op"),
    [
        ("GET", "/v1/owner/os/unified", "snapshot"),
        ("GET", "/v1/owner/os/unified/schema", "schema"),
        ("GET", "/v1/owner/os/unified/cached", "cached"),
        ("POST", "/v1/owner/os/unified/proposals", "propose"),
        ("GET", "/v1/owner/os/unified/proposals/PR_12345678901234567890", "proposal"),
        ("POST", "/v1/owner/os/unified/proposals/PR_12345678901234567890/approve", "approve"),
        ("POST", "/v1/owner/os/unified/proposals/PR_12345678901234567890/reject", "reject"),
        ("GET", "/v1/owner/os/unified/receipts/12345678-1234-4234-8234-123456789012", "receipt"),
        ("GET", "/v1/owner/os/unified/pending", "pending"),
        ("GET", "/v1/owner/os/unified/proposal-requests/12345678-1234-4234-8234-123456789012",
         "proposal_request"),
        ("GET", "/v1/owner/auth/session", "auth"),
        ("POST", "/v1/owner/auth/login", "auth"),
        ("GET", "/v1/owner/auth", "auth"),
        ("GET", "/v1/owner/os/contacts", "other"),
        ("GET", "/v1/owner/os/unified/unknown", "other"),
        ("DELETE", "/v1/owner/os/unified/proposals", "other"),
        ("GET", "/v1/owner/os/unified/proposals/a/b/approve", "other"),
        ("GET", "/v1/owner/os/unified/proposals/PR_1/approve", "other"),
        ("POST", "/v1/owner/os/unified", "other"),
    ],
)
def test_the_op_table_is_fixed_and_never_the_raw_path(method, path, op):
    from app.api.owner_os_boundary import request_op

    assert request_op(method, path) == op


def test_no_id_payload_or_name_reaches_any_timing_line(sessions, monkeypatch, caplog):
    config = assaf.settings()
    monkeypatch.setattr(base.port, "_post", base._transport(config))
    with caplog.at_level(logging.DEBUG):
        with base._browser(sessions, config) as browser:
            session = auth._login(browser)
            headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
            approval = assaf._pending(config)["approval_request_id"]
            browser.post(
                "/v1/owner/os/unified/proposals", headers=headers, json=base._propose_body()
            )
            browser.get(f"/v1/owner/os/unified/proposals/{assaf.PROPOSAL}", headers=ORIGIN)
            browser.post(
                f"/v1/owner/os/unified/proposals/{assaf.PROPOSAL}/approve",
                headers=headers, json={"request_id": approval},
            )
            browser.get(f"/v1/owner/os/unified/receipts/{assaf.REQUEST}", headers=ORIGIN)
    text = "\n".join(lines_of(caplog))
    assert summaries(caplog), "no summary line was logged"
    for forbidden in (assaf.PROPOSAL, assaf.REQUEST, approval, base.SENTINEL, "Follow up"):
        assert forbidden not in text, forbidden
    assert all(
        _SUMMARY.fullmatch(line) or base._LOG_LINE.fullmatch(line) for line in lines_of(caplog)
    ), lines_of(caplog)


# --------------------------------------------------------------------------------- ledger metric


def test_the_ledger_metric_appears_on_the_postgres_backend_routes(ledger_owner, caplog):
    owner = ledger_owner
    root = "/v1/owner/os/unified"
    with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
        snapshot = owner.client.get(root, headers=owner.login())
        schema = owner.client.get(root + "/schema", headers=owner.login())
        pending = owner.client.get(root + "/pending", headers=owner.login())
    for response in (snapshot, schema, pending):
        assert response.status_code == 200, response.text
        timing = timing_of(response)
        assert "ledger" in timing and list(timing)[-1] == "total", timing
    metrics = {
        line.split("metric=")[1].split()[0]
        for line in lines_of(caplog)
        if "stage=os_read" in line
    }
    assert "ledger" in metrics


def test_a_ledger_write_is_timed_under_the_commit_stage(ledger_owner, caplog):
    owner = ledger_owner
    body = {
        "request_id": "12345678-1234-4234-8234-123456789012", "entity": "tasks",
        "record_id": None, "expected_revision": 0,
        "input": {"title": "Follow up", "status": "לביצוע"},
    }
    headers = owner.login()
    with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
        response = owner.client.post("/v1/owner/os/unified/proposals", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert "ledger" in timing_of(response)
    assert any("stage=os_commit metric=ledger" in line for line in lines_of(caplog))


def test_the_ledger_phase_is_silent_outside_a_request(caplog):
    from tests.contract.shapes.v2_20 import build_world, seed

    world = build_world()
    seed(world)
    with caplog.at_level(logging.DEBUG, logger="mia.owner_timing"):
        world.backend.schema()
    assert lines_of(caplog) == []
    timing = RequestTiming()
    with timing.activate():
        owner_timing.mark_owner_authenticated()
        world.backend.schema()
    assert "ledger;dur=" in timing.header(200)


def test_a_v2_read_error_keeps_the_ledger_phase_and_the_error_translation(ledger_owner):
    response = ledger_owner.client.get(
        "/v1/owner/os/unified/pending?limit=0", headers=ledger_owner.login()
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query"
    assert response.json()["request_id"] == response.headers["x-request-id"]
