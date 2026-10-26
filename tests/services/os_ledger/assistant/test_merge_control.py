"""V2-38 controls obey V2-37's shared IP accounting and bearer pool admission."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.api.assistant import os as rest
from app.core.config import get_settings
from app.main import app
from app.services import assistant_keys as keys
from app.services.os_ledger.api import LedgerError
from fastapi.testclient import TestClient

from tests.services.os_ledger.assistant.support import IP_A


@pytest.fixture
def client(aw, monkeypatch):
    monkeypatch.setattr(rest, "gateway_for", lambda settings: aw.gateway)
    app.dependency_overrides[get_settings] = lambda: aw.config
    try:
        with TestClient(app, base_url="https://api.assafweb.test", client=(IP_A, 50000)) as http:
            yield http
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_tools_list_rest_and_invalid_lookup_share_the_last_ip_row(aw, client, monkeypatch):
    issued = aw.issue()
    aw.seed_audit(59, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    entered, release, control_started, invalid_started = (threading.Event() for _ in range(4))
    control_finished = threading.Event()
    original_control = aw.store._inner.control
    original_invalid = aw.store._inner.invalid_lookup

    def work(ctx):
        entered.set()
        assert release.wait(5)
        return {"held": True}

    def control(*args, **kwargs):
        control_started.set()
        try:
            return original_control(*args, **kwargs)
        finally:
            control_finished.set()

    def invalid(**kwargs):
        invalid_started.set()
        return original_invalid(**kwargs)

    monkeypatch.setattr(aw.statements, "context_for_assistant", work)
    monkeypatch.setattr(rest, "context_body", lambda result: result)
    monkeypatch.setattr(aw.store._inner, "control", control)
    monkeypatch.setattr(aw.store._inner, "invalid_lookup", invalid)
    headers = {"Authorization": f"Bearer {issued['secret']}"}
    with ThreadPoolExecutor(max_workers=3) as pool:
        valid = pool.submit(client.get, "/v1/assistant/os/context", headers=headers)
        try:
            assert entered.wait(3)
            bad = pool.submit(client.get, "/v1/assistant/os/context", headers={
                "Authorization": "Bearer aosp_" + "z" * 43})
            assert invalid_started.wait(3)  # lookup finished while the address still has 59 rows
            control_call = pool.submit(client.post, "/v1/assistant/mcp", headers=headers,
                                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert control_started.wait(3)
            # A control must wait behind the valid call's IP lock, just as an invalid lookup does.
            assert not control_finished.wait(0.2)
        finally:
            release.set()
        responses = [future.result(timeout=5) for future in (valid, control_call, bad)]
    assert [response.status_code for response in responses] == [200, 429, 429]
    for response in responses[1:]:
        assert response.json()["error"]["code"] == "rate_limited"
    assert len(aw.audit()) == 60
    assert aw.credential(issued["credential_id"])["use_count"] == 1


def test_already_authenticated_control_recounts_and_refuses_without_a_row(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    aw.seed_audit(60, ip_hmac=caller.ip_hmac, operation="context", ok=True, reason="ok")
    before = aw.credential(issued["credential_id"])
    with pytest.raises(LedgerError) as caught:
        aw.gateway.control(caller, "tools_list")
    assert (caught.value.code, caught.value.reason, caught.value.status) == (
        "rate_limited", "ip_limit", 429)
    assert len(aw.audit()) == 60
    assert aw.credential(issued["credential_id"]) == before


def test_control_inside_three_saturated_bearer_slots_is_a_retryable_503(
        aw, client, monkeypatch, caplog):
    callers = [aw.authenticate(aw.issue(label=str(i))["secret"], ip=f"203.0.113.{i}")
               for i in range(3)]
    issued = aw.issue(label="control")
    entered = threading.Barrier(4)
    release = threading.Event()
    control_touched = []
    original = aw.store._inner.control

    def control(*args, **kwargs):
        control_touched.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(aw.store._inner, "control", control)

    def work(facts):
        entered.wait(5)
        assert release.wait(6)

    def held_request(caller):
        async def request():
            async with keys.async_bearer_request():
                return aw.gateway.run(caller, "context", work)
        return asyncio.run(request())

    with ThreadPoolExecutor(max_workers=3) as pool:
        held = [pool.submit(held_request, caller) for caller in callers]
        try:
            entered.wait(5)
            start = time.monotonic()
            response = client.post("/v1/assistant/mcp", headers={
                "Authorization": f"Bearer {issued['secret']}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 503, response.text
            assert 1.8 <= time.monotonic() - start < 3
            error = response.json()["error"]
            assert (error["code"], error["retryable"], error["write_outcome"]) == (
                "ledger_unavailable", True, "not_started")
            assert "bearer_busy" in caplog.text
            assert not control_touched and aw.audit() == []
        finally:
            release.set()
        for future in held:
            future.result(timeout=5)


@pytest.mark.parametrize("failure", [None, "database", "invalid_method", "ip_limit"])
def test_control_releases_the_shared_guard_on_every_exit(aw, client, monkeypatch, failure):
    from tests.api.assistant.test_request_guard import CountingSlots

    issued = aw.issue()
    slots = CountingSlots()
    monkeypatch.setattr(keys, "_bearer_slots", slots)
    original = aw.gateway.control
    observations = []

    def observed(caller, method):
        acquired = slots.slots.acquire(blocking=False)
        observations.append((slots.acquisitions, not acquired))
        if acquired:
            slots.slots.release()
        if failure == "ip_limit":
            # Authenticate passed: make control itself recount the now-full IP window.
            aw.seed_audit(60, ip_hmac=caller.ip_hmac, operation="context", ok=True, reason="ok")
        return original(caller, "raw payload" if failure == "invalid_method" else method)

    monkeypatch.setattr(aw.gateway, "control", observed)
    if failure == "database":
        def broken(*args, **kwargs):
            raise LedgerError("ledger_unavailable")
        monkeypatch.setattr(aw.store._inner, "control", broken)
    response = client.post("/v1/assistant/mcp", headers={
        "Authorization": "Bearer " + issued["secret"]},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == {None: 200, "database": 503, "invalid_method": 503,
                                    "ip_limit": 429}[failure], response.text
    assert observations == [(1, True)], "MCP control must run inside its single guard"
    assert slots.acquisitions == 1
    assert slots.slots.acquire(blocking=False)
    slots.slots.release()
