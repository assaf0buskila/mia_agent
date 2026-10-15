"""V2-04: per-operation Apps Script budgets (contract section 3).

Every budget covers the whole Script call, including its one 302 follow, and replaces the fixed
15 s. The budgets are asserted from the argument `_post` receives and from the timeout each
simulated socket open is given, on a fake monotonic clock, so nothing here sleeps.

| operation                                   | budget                                        |
| schema, proposal GET, receipt               | 15 s                                          |
| snapshot, propose                           | 25 s                                          |
| approve/reject, decision binding cached     | 25 s                                          |
| approve/reject, cold                        | pre-read <= 10 s, then min(25, 30 - elapsed)  |
"""

from __future__ import annotations

import inspect
import io
import json
import logging
import re
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest
from app.core.owner_timing import RequestTiming
from app.services import assaf_os as port

from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

APPROVE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GOOGLE = "https://script.googleusercontent.com/macros/echo?user_content_key=synthetic"
LOG_LINE = re.compile(
    r"owner_stage stage=(os_auth|os_read|os_commit|os_verify|os_mirror) "
    r"metric=(auth|db|script|ledger|verify|none) outcome=(ok|error|cancelled) "
    r"latency_ms=\d+ reason=(none|[a-z][a-z0-9_]{1,47})"
)
BUDGETS = {
    "schema": 15.0,
    "proposal": 15.0,
    "receipt": 15.0,
    "snapshot": 25.0,
    "propose": 25.0,
    "approve": 25.0,
    "reject": 25.0,
}


@pytest.fixture(autouse=True)
def clear_caches():
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()
    yield
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()


# ---------------------------------------------------------------------------- fakes


class Clock:
    """A fake monotonic clock; patching it also drives decide()'s elapsed-time accounting."""

    def __init__(self, monkeypatch, start=1_000.0):
        self.now = start
        monkeypatch.setattr(port.time, "monotonic", lambda: self.now)

    def advance(self, seconds):
        self.now += seconds


class Reply:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, data: bytes):
        self.data = data

    def read(self, limit):
        return self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _result(config, request):
    operation = request["operation"]
    if operation == "schema":
        return assaf._schema(config)
    if operation == "snapshot":
        return assaf._snapshot(config)
    if operation in {"propose", "proposal"}:
        return assaf._pending(config, "OS")
    if operation == "receipt":  # a receipt lookup carries only the request id
        request = {"data": {**request["data"], "proposal_id": assaf.PROPOSAL}}
    return assaf._decision_result(config, request)


def _by_operation(default=0.1, **overrides):
    return lambda operation: overrides.get(operation, default)


class Opener:
    """A fake urllib opener: each POST takes `latency(operation)` fake seconds or times out.

    A latency of None never answers, so the open consumes exactly the timeout it was given.
    """

    def __init__(self, clock, config, latency=_by_operation()):
        self.clock, self.config, self.latency = clock, config, latency
        self.timeouts: list[float] = []
        self.operations: list[str] = []

    def open(self, request, timeout):
        call = json.loads(json.loads(request.data)["payload"])
        self.timeouts.append(timeout)
        self.operations.append(call["operation"])
        latency = self.latency(call["operation"])
        if latency is None or latency > timeout:
            self.clock.advance(timeout)
            raise TimeoutError("timed out")
        self.clock.advance(latency)
        return Reply(assaf.signed(request.data, _result(self.config, call), self.config))


def _opener(monkeypatch, config, latency=_by_operation()):
    clock = Clock(monkeypatch)
    opener = Opener(clock, config, latency)
    monkeypatch.setattr(port, "build_opener", lambda *_handlers: opener)
    return clock, opener


def _adapter(config, source="OS"):
    return port.AssafOsPort(config, auth.OWNER_ID, actor_source=source)


def _warm(config):
    port._remember_decision_binding(config, auth.OWNER_ID, assaf._pending(config, "OS"))


def _propose(adapter):
    return adapter.propose(
        port.ProposeRequest(
            request_id=assaf.REQUEST,
            entity="tasks",
            record_id=None,
            expected_revision=0,
            input={"title": "Follow up", "status": "לביצוע"},
        )
    )


def _decide(adapter, operation):
    request_id = APPROVE_ID if operation == "approve" else REJECT_ID
    return adapter.decide(operation, assaf.PROPOSAL, port.DecisionRequest(request_id=request_id))


INVOKE = {
    "schema": lambda adapter: adapter.schema(),
    "snapshot": lambda adapter: adapter.snapshot(),
    "proposal": lambda adapter: adapter.proposal(assaf.PROPOSAL),
    "receipt": lambda adapter: adapter.receipt(assaf.REQUEST),
    "propose": _propose,
    "approve": lambda adapter: _decide(adapter, "approve"),
    "reject": lambda adapter: _decide(adapter, "reject"),
}
# Public code on a timeout: reads are source_unavailable, writes stay unknown.
PUBLIC_CODE = {
    "schema": "source_unavailable",
    "snapshot": "source_unavailable",
    "proposal": "source_unavailable",
    "receipt": "source_unavailable",
    "propose": "unknown",
    "approve": "unknown",
    "reject": "unknown",
}
STAGE = {name: "os_commit" if PUBLIC_CODE[name] == "unknown" else "os_read" for name in BUDGETS}


def _prepare(name, config):
    if name in {"approve", "reject"}:
        _warm(config)


# ------------------------------------------------------------- the argument _post receives


def test_every_port_operation_has_a_budget_and_the_table_matches_the_contract():
    assert set(port._SCRIPT_BUDGET_SECONDS) == set(port.OPERATIONS)
    assert port._SCRIPT_BUDGET_SECONDS == BUDGETS
    assert (port._DECIDE_PREREAD_SECONDS, port._DECIDE_TOTAL_SECONDS) == (10.0, 30.0)


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_post_receives_the_budget_of_its_operation(name, monkeypatch):
    config = assaf.settings()
    calls: list[tuple[str, float]] = []

    def post(_url, body, budget):
        request = json.loads(json.loads(body)["payload"])
        calls.append((request["operation"], budget))
        return assaf.signed(body, _result(config, request), config)

    monkeypatch.setattr(port, "_post", post)
    _prepare(name, config)
    INVOKE[name](_adapter(config))
    assert calls == [(name, BUDGETS[name])]


def test_the_telegram_decision_has_no_pre_read_and_the_cached_budget(monkeypatch):
    config = assaf.settings()
    calls: list[tuple[str, float]] = []

    def post(_url, body, budget):
        request = json.loads(json.loads(body)["payload"])
        calls.append((request["operation"], budget))
        return assaf.signed(body, _result(config, request), config)

    monkeypatch.setattr(port, "_post", post)
    adapter = _adapter(config, "TELEGRAM_APPROVAL")
    adapter.decide("approve", assaf.PROPOSAL, port.DecisionRequest(request_id=assaf.REQUEST))
    adapter.decide("reject", assaf.PROPOSAL, port.DecisionRequest(request_id=assaf.REQUEST))
    assert calls == [("approve", 25.0), ("reject", 25.0)]


@pytest.mark.parametrize("operation", ["approve", "reject"])
@pytest.mark.parametrize("pre_read", [0.0, 1.0, 4.99, 5.0, 9.9, 10.0])
def test_a_cold_decision_pre_reads_for_up_to_10s_then_gets_the_rest_of_30s(
    operation, pre_read, monkeypatch
):
    config = assaf.settings()
    clock, calls = Clock(monkeypatch), []

    def post(_url, body, budget):
        request = json.loads(json.loads(body)["payload"])
        calls.append((request["operation"], budget))
        if request["operation"] == "proposal":
            clock.advance(pre_read)
        return assaf.signed(body, _result(config, request), config)

    monkeypatch.setattr(port, "_post", post)
    _decide(_adapter(config), operation)
    assert [name for name, _ in calls] == ["proposal", operation]
    assert calls[0][1] == 10.0
    assert calls[1][1] == pytest.approx(min(25.0, 30.0 - pre_read))


def test_a_decision_with_no_time_left_after_the_pre_read_posts_nothing(monkeypatch):
    config = assaf.settings()
    clock, calls = Clock(monkeypatch), []

    def post(_url, body, budget):
        request = json.loads(json.loads(body)["payload"])
        calls.append(request["operation"])
        clock.advance(30.0)  # a pre-read that somehow overran its whole allowance
        return assaf.signed(body, _result(config, request), config)

    monkeypatch.setattr(port, "_post", post)
    with pytest.raises(port.AssafOsError) as refused:
        _decide(_adapter(config), "approve")
    assert refused.value.code == "source_unavailable" and refused.value.status == 503
    assert calls == ["proposal"], "no write is dispatched without a budget"


def test_a_binding_mismatch_still_refuses_before_any_decide_post(monkeypatch):
    config = assaf.settings()
    _clock, opener = _opener(monkeypatch, config)
    with pytest.raises(port.AssafOsError) as mismatch:
        _adapter(config).decide(
            "approve", assaf.PROPOSAL, port.DecisionRequest(request_id=assaf.REQUEST)
        )
    assert mismatch.value.code == "decision_request_mismatch" and mismatch.value.status == 409
    assert opener.operations == ["proposal"]


# ------------------------------------------------------- the simulated socket-level budget


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_each_operation_times_out_at_exactly_its_budget(name, monkeypatch):
    config = assaf.settings()
    clock, opener = _opener(monkeypatch, config, _by_operation(default=None))
    start = clock.now
    _prepare(name, config)
    with pytest.raises(port.AssafOsError) as timed_out:
        INVOKE[name](_adapter(config))
    assert timed_out.value.code == PUBLIC_CODE[name] and timed_out.value.status == 503
    assert opener.timeouts == [BUDGETS[name]]
    assert clock.now - start == pytest.approx(BUDGETS[name])


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_an_answer_just_inside_the_budget_succeeds_and_just_outside_does_not(name, monkeypatch):
    config = assaf.settings()
    _clock, opener = _opener(monkeypatch, config, _by_operation(**{name: BUDGETS[name] - 0.1}))
    _prepare(name, config)
    assert INVOKE[name](_adapter(config))
    assert opener.timeouts == [BUDGETS[name]]

    port._clear_assaf_os_schema_cache()
    _opener(monkeypatch, config, _by_operation(**{name: BUDGETS[name] + 0.1}))
    _prepare(name, config)
    with pytest.raises(port.AssafOsError) as slow:
        INVOKE[name](_adapter(config))
    assert slow.value.code == PUBLIC_CODE[name]


def test_a_20_second_snapshot_succeeds(monkeypatch):
    config = assaf.settings()
    clock, opener = _opener(monkeypatch, config, _by_operation(snapshot=20.0))
    start = clock.now
    snapshot = _adapter(config).snapshot()
    assert snapshot["workspace_id"] == config.assaf_os_workspace_id
    assert opener.timeouts == [25.0] and clock.now - start == pytest.approx(20.0)


@pytest.mark.parametrize("name", ["schema", "proposal", "receipt"])
def test_the_same_20_seconds_is_too_slow_for_a_15_second_read(name, monkeypatch):
    config = assaf.settings()
    _clock, opener = _opener(monkeypatch, config, _by_operation(**{name: 20.0}))
    with pytest.raises(port.AssafOsError) as slow:
        INVOKE[name](_adapter(config))
    assert slow.value.code == "source_unavailable" and opener.timeouts == [15.0]


def test_a_20_second_propose_and_a_20_second_warm_approve_succeed(monkeypatch):
    config = assaf.settings()
    _clock, opener = _opener(
        monkeypatch, config, _by_operation(propose=20.0, approve=20.0, reject=20.0)
    )
    adapter = _adapter(config)
    assert _propose(adapter)["status"] == "pending"
    assert _decide(adapter, "approve")["status"] == "committed"
    assert opener.operations == ["propose", "approve"], "propose primed the binding"
    assert opener.timeouts == [25.0, 25.0]


@pytest.mark.parametrize("operation", ["approve", "reject"])
def test_cold_worst_case_9_9s_pre_read_then_a_hung_decide_ends_by_30s(operation, monkeypatch):
    config = assaf.settings()
    clock, opener = _opener(monkeypatch, config, _by_operation(proposal=9.9, **{operation: None}))
    start = clock.now
    with pytest.raises(port.AssafOsError) as hung:
        _decide(_adapter(config), operation)
    assert hung.value.code == "unknown" and hung.value.status == 503
    assert opener.operations == ["proposal", operation]
    assert opener.timeouts == [pytest.approx(10.0), pytest.approx(20.1)]
    assert clock.now - start == pytest.approx(30.0)
    assert clock.now - start <= 30.0 + 1e-9 < 35.0


def test_cold_worst_case_never_exceeds_30s_for_any_pre_read_duration(monkeypatch):
    config = assaf.settings()
    for tenths in range(0, 101):
        pre_read = tenths / 10
        port._clear_assaf_os_decision_binding_cache()
        clock, opener = _opener(monkeypatch, config, _by_operation(proposal=pre_read, approve=None))
        start = clock.now
        with pytest.raises(port.AssafOsError):
            _decide(_adapter(config), "approve")
        assert clock.now - start <= 30.0 + 1e-9, pre_read
        assert opener.timeouts[0] == 10.0
        assert opener.timeouts[1] == pytest.approx(min(25.0, 30.0 - pre_read))


def test_a_pre_read_that_hangs_ends_at_10s_and_never_posts_the_decision(monkeypatch):
    config = assaf.settings()
    clock, opener = _opener(monkeypatch, config, _by_operation(proposal=None))
    start = clock.now
    with pytest.raises(port.AssafOsError) as hung:
        _decide(_adapter(config), "approve")
    assert hung.value.code == "source_unavailable" and hung.value.status == 503
    assert opener.operations == ["proposal"] and opener.timeouts == [10.0]
    assert clock.now - start == pytest.approx(10.0)
    assert not port._DECISION_BINDINGS, "a failed pre-read leaves nothing cached"


def test_the_proposal_route_read_keeps_its_own_15_second_budget(monkeypatch):
    config = assaf.settings()
    _clock, opener = _opener(monkeypatch, config)
    _adapter(config).proposal(assaf.PROPOSAL)
    assert opener.timeouts == [15.0]


# ------------------------------------------------------------------- _post and the redirect


def _redirect(location=GOOGLE):
    headers = Message()
    headers["Location"] = location
    return HTTPError(
        "https://script.google.com/macros/s/synthetic/exec", 302, "Found", headers, io.BytesIO(b"")
    )


class Scripted:
    """An opener that plays a fixed list of (latency, outcome) steps."""

    def __init__(self, clock, steps):
        self.clock, self.steps = clock, list(steps)
        self.timeouts: list[float] = []
        self.methods: list[str] = []

    def open(self, request, timeout):
        self.timeouts.append(timeout)
        self.methods.append(request.get_method())
        latency, outcome = self.steps.pop(0)
        if latency is None or latency > timeout:
            self.clock.advance(timeout)
            raise TimeoutError("timed out")
        self.clock.advance(latency)
        if isinstance(outcome, BaseException):
            raise outcome
        return Reply(outcome)


def _script(monkeypatch, steps):
    clock = Clock(monkeypatch)
    opener = Scripted(clock, steps)
    monkeypatch.setattr(port, "build_opener", lambda *_handlers: opener)
    return clock, opener


URL = "https://script.google.com/macros/s/" + "x" * 24 + "/exec"


def test_post_shares_one_budget_between_the_post_and_its_302_follow(monkeypatch):
    clock, opener = _script(monkeypatch, [(20.0, _redirect()), (4.0, b'{"ok":1}')])
    start = clock.now
    assert port._post(URL, b"{}", 25.0) == b'{"ok":1}'
    assert opener.methods == ["POST", "GET"]
    assert opener.timeouts == [pytest.approx(25.0), pytest.approx(5.0)]
    assert clock.now - start == pytest.approx(24.0)


def test_post_times_out_when_the_follow_overruns_the_shared_budget(monkeypatch):
    clock, opener = _script(monkeypatch, [(20.0, _redirect()), (5.5, b"{}")])
    start = clock.now
    with pytest.raises(port.AssafOsError) as slow:
        port._post(URL, b"{}", 25.0)
    assert slow.value.code == "source_unavailable"
    assert opener.timeouts == [pytest.approx(25.0), pytest.approx(5.0)]
    assert clock.now - start == pytest.approx(25.0)


def test_post_does_not_follow_when_the_redirect_arrives_with_no_budget_left(monkeypatch):
    clock, opener = _script(monkeypatch, [(25.0, _redirect()), (1.0, b"{}")])
    with pytest.raises(port.AssafOsError) as slow:
        port._post(URL, b"{}", 25.0)
    assert slow.value.code == "source_unavailable" and opener.methods == ["POST"]
    assert clock.now == 1_025.0


@pytest.mark.parametrize(
    "location",
    [
        "http://script.googleusercontent.com/macros/echo",
        "https://evil.example/macros/echo",
        "https://script.googleusercontent.com:8443/macros/echo",
        "https://user:pw@script.googleusercontent.com/macros/echo",
    ],
)
def test_post_still_refuses_to_follow_a_redirect_off_the_pinned_host(location, monkeypatch):
    _clock, opener = _script(monkeypatch, [(0.1, _redirect(location)), (0.1, b"{}")])
    with pytest.raises(port.AssafOsError) as refused:
        port._post(URL, b"{}", 25.0)
    assert refused.value.code == "source_unavailable" and opener.methods == ["POST"]


def test_post_takes_the_budget_as_a_required_third_argument():
    parameters = inspect.signature(port._post).parameters
    assert list(parameters) == ["url", "body", "budget"]
    assert parameters["budget"].default is inspect.Parameter.empty


# -------------------------------------------------------------- the per-operation stage log


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "mia.owner_timing"]


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_a_timeout_logs_timeout_operation_as_the_only_detail(name, monkeypatch, caplog):
    config = assaf.settings()
    _opener(monkeypatch, config, _by_operation(default=None))
    _prepare(name, config)
    with caplog.at_level(logging.DEBUG, logger="mia.owner_timing"), RequestTiming().activate():
        with pytest.raises(port.AssafOsError) as timed_out:
            INVOKE[name](_adapter(config))
    assert timed_out.value.code == PUBLIC_CODE[name], "public codes are unchanged"
    lines = _lines(caplog)
    assert lines and all(LOG_LINE.fullmatch(line) for line in lines), lines
    script = [line for line in lines if "metric=script" in line]
    assert len(script) == 1
    assert f"stage={STAGE[name]} metric=script outcome=error" in script[0]
    assert script[0].endswith(f"reason=timeout_{name}"), script[0]
    for private in (config.assaf_os_hmac_secret, config.assaf_os_script_url, assaf.PROPOSAL):
        assert private not in " ".join(lines)


def test_a_cold_pre_read_timeout_is_attributed_to_the_proposal_read(monkeypatch, caplog):
    config = assaf.settings()
    _opener(monkeypatch, config, _by_operation(proposal=None))
    with caplog.at_level(logging.DEBUG, logger="mia.owner_timing"), RequestTiming().activate():
        with pytest.raises(port.AssafOsError) as hung:
            _decide(_adapter(config), "approve")
    assert hung.value.code == "source_unavailable"
    (line,) = [line for line in _lines(caplog) if "metric=script" in line]
    assert "stage=os_read" in line and line.endswith("reason=timeout_proposal")


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out"),
        URLError(TimeoutError("timed out")),
    ],
)
def test_socket_and_connect_timeouts_are_both_recognized(failure, monkeypatch, caplog):
    config = assaf.settings()
    _script(monkeypatch, [(0.0, failure)])
    with caplog.at_level(logging.DEBUG, logger="mia.owner_timing"), RequestTiming().activate():
        with pytest.raises(port.AssafOsError) as timed_out:
            _adapter(config).snapshot()
    assert timed_out.value.code == "source_unavailable"
    (line,) = [line for line in _lines(caplog) if "metric=script" in line]
    assert line.endswith("reason=timeout_snapshot")


@pytest.mark.parametrize(
    "failure",
    [
        URLError(ConnectionRefusedError("refused")),
        OSError("network is unreachable"),
        HTTPError(URL, 500, "Server Error", Message(), io.BytesIO(b"")),
        HTTPError(URL, 403, "Forbidden", Message(), io.BytesIO(b"")),
    ],
)
def test_other_transport_failures_keep_the_old_reason_and_are_not_timeouts(
    failure, monkeypatch, caplog
):
    config = assaf.settings()
    _script(monkeypatch, [(0.0, failure)])
    with caplog.at_level(logging.DEBUG, logger="mia.owner_timing"), RequestTiming().activate():
        with pytest.raises(port.AssafOsError) as down:
            _adapter(config).snapshot()
    assert down.value.code == "source_unavailable"
    (line,) = [line for line in _lines(caplog) if "metric=script" in line]
    assert line.endswith("reason=source_unavailable")
    _script(monkeypatch, [(0.0, failure)])
    with pytest.raises(port.AssafOsError) as unknown:
        _propose(_adapter(config))
    assert unknown.value.code == "unknown"


def test_outside_an_owner_request_a_timeout_logs_nothing(monkeypatch, caplog):
    config = assaf.settings()
    _opener(monkeypatch, config, _by_operation(default=None))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(port.AssafOsError):
            _adapter(config, "MIA").snapshot()
    assert _lines(caplog) == []
