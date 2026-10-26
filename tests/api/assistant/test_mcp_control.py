"""S1: same proof against the real PostgreSQL store and its memory double."""

import pytest
from app.services import assistant_keys as keys
from tests.services.os_ledger.assistant.support import IP_A, IP_B


def test_control_rows_are_codes_only_and_do_not_touch_usage(control_world):
    aw = control_world
    issued = aw.issue()
    before = aw.credential(issued["credential_id"])
    methods = ("initialize", "ping", "tools_list", "notification", "invalid_request",
               "method_not_found", "invalid_params", "body_too_large", "unsupported_media")
    for method in methods:
        aw.gateway.control(aw.authenticate(issued["secret"]), method)
    rows = aw.audit()
    assert len(rows) == len(methods)
    assert {r["reason"] for r in rows} == set(methods)
    for row in rows:
        assert row == dict(credential_id=issued["credential_id"], occurred_at=aw.clock(),
                           operation="mcp_control", reason=row["reason"], ok=True,
                           line_count=0, bytes=0, proposal_id=None,
                           client_ip_hmac=aw.ip_hmac(IP_A))
    assert aw.credential(issued["credential_id"]) == before


def test_controls_do_not_consume_hour_or_day_quota(control_world):
    aw = control_world
    issued = aw.issue()
    for _ in range(35):
        aw.gateway.control(aw.authenticate(issued["secret"]), "tools_list")
    caller = aw.authenticate(issued["secret"], ip=IP_B)
    for _ in range(5):
        assert aw.gateway.run(caller, "submit", lambda facts: "ok") == "ok"
    with pytest.raises(keys.api.LedgerError, match="rate_limited"):
        aw.gateway.run(caller, "submit", lambda facts: pytest.fail("daily quota bypass"))
    for _ in range(25):
        aw.gateway.run(caller, "context", lambda facts: None)
    with pytest.raises(keys.api.LedgerError, match="rate_limited"):
        aw.gateway.run(caller, "context", lambda facts: pytest.fail("hourly quota bypass"))


def test_controls_count_toward_ip_limit_before_lookup(control_world):
    aw = control_world
    issued = aw.issue()
    for _ in range(60):
        aw.gateway.control(aw.authenticate(issued["secret"]), "ping")
    with pytest.raises(keys.api.LedgerError) as caught:
        aw.authenticate(issued["secret"])
    assert caught.value.reason == "ip_limit"
    assert len(aw.audit()) == 60
    assert aw.authenticate(issued["secret"], ip=IP_B)
