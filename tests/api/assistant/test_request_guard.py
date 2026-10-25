"""E37a: a bearer request reserves one slot before its authentication reads."""

import asyncio
import json
import threading
import time

import pytest
from app.api.assistant import mcp
from app.api.assistant import os as rest
from app.services import assistant_keys as keys
from app.services.os_ledger import api
from starlette.requests import Request

CASES = (
    ("GET", "/v1/assistant/os/context", None),
    ("POST", "/v1/assistant/os/statement-batches", {}),
    ("GET", "/v1/assistant/os/statement-batches/missing", None),
    ("POST", "/v1/assistant/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}),
    ("POST", "/v1/assistant/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "get_statement_context"}}),
)


@pytest.mark.parametrize("method,path,body", CASES)
def test_saturated_request_refuses_before_authentication_db_reads(env, monkeypatch,
                                                               method, path, body):
    aw, client = env
    key = aw.issue()["secret"]
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(keys, "_bearer_slots", slots)
    assert slots.acquire(blocking=False)
    aw.store.calls.clear()
    watermarks = []
    monkeypatch.setattr(aw.gateway, "_watermark", lambda owner: watermarks.append(owner))
    started = time.monotonic()
    try:
        response = client.request(method, path, json=body,
                                  headers={"Authorization": "Bearer " + key})
    finally:
        slots.release()
    assert response.status_code == 503, response.text
    expected = {"code": "ledger_unavailable", "retryable": True}
    if method == "POST":
        expected["write_outcome"] = "not_started"
    assert response.json()["error"] == expected
    assert 1.8 <= time.monotonic() - started < 3
    assert not aw.store.calls and not watermarks and aw.audit() == []


class CountingSlots:
    def __init__(self):
        self.slots = threading.BoundedSemaphore(1)
        self.acquisitions = 0

    def acquire(self, **kwargs):
        self.acquisitions += 1
        return self.slots.acquire(**kwargs)

    def release(self):
        return self.slots.release()


@pytest.mark.parametrize("method,path,body", CASES)
@pytest.mark.parametrize("exit_path", ["normal", "unknown_key", "ip_limit", "key_limit",
                                      "auth_error"])
def test_one_slot_covers_auth_and_work_and_is_released(env, monkeypatch, method, path, body,
                                                     exit_path):
    aw, client = env
    key = aw.issue()["secret"]
    slots = CountingSlots()
    monkeypatch.setattr(keys, "_bearer_slots", slots)
    if exit_path == "unknown_key":
        key = keys.generate_secret()
    elif exit_path == "ip_limit":
        monkeypatch.setattr(aw.store, "ip_calls", lambda *args: keys.IP_LIMIT)
    elif exit_path == "key_limit":
        monkeypatch.setattr(keys, "KEY_LIMIT", 0)
    elif exit_path == "auth_error":
        def fail(*args):
            raise api.LedgerError("ledger_unavailable")
        monkeypatch.setattr(aw.store, "find", fail)
    response = client.request(method, path, json=body,
                              headers={"Authorization": "Bearer " + key})
    assert slots.acquisitions == 1
    assert slots.slots.acquire(blocking=False)
    slots.slots.release()
    if exit_path == "unknown_key":
        assert response.status_code == 401
    elif exit_path == "ip_limit":
        assert response.status_code == 429
    elif exit_path == "auth_error":
        assert response.status_code == 503
    else:
        assert response.status_code in {200, 404, 422, 429}, response.text


def request_for(path, key):
    return Request({"type": "http", "method": "POST", "path": path,
                    "client": ("203.0.113.10", 50000),
                    "headers": [(b"authorization", ("Bearer " + key).encode()),
                                (b"content-type", b"application/json")]})


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_cancelled_request_releases_its_acquired_slot(env, monkeypatch, surface):
    aw, _client = env
    key = aw.issue()["secret"]
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(keys, "_bearer_slots", slots)
    entered_body = asyncio.Event()

    async def read_body(_request):
        entered_body.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(rest, "_read_body", read_body)
    endpoint, path = ((rest.submit_batch, rest.ROOT + "/statement-batches") if surface == "rest"
                      else (mcp.mcp, "/v1/assistant/mcp"))
    task = asyncio.create_task(endpoint(request_for(path, key), aw.config))
    try:
        await asyncio.wait_for(entered_body.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.__traceback__ is not None
        assert slots.acquire(blocking=False), "cancelled acquired request leaked its slot"
        slots.release()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_cancellation_while_waiting_never_acquires_a_late_slot(env, monkeypatch, surface):
    aw, _client = env
    key = aw.issue()["secret"]
    attempted = threading.Event()

    class ObservedSlots(CountingSlots):
        def acquire(self, **kwargs):
            attempted.set()
            return super().acquire(**kwargs)

    slots = ObservedSlots()
    assert slots.slots.acquire(blocking=False)
    monkeypatch.setattr(keys, "_bearer_slots", slots)
    aw.store.calls.clear()
    endpoint, path = ((rest.submit_batch, rest.ROOT + "/statement-batches") if surface == "rest"
                      else (mcp.mcp, "/v1/assistant/mcp"))
    task = asyncio.create_task(endpoint(request_for(path, key), aw.config))
    try:
        assert await asyncio.to_thread(attempted.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.__traceback__ is not None
    finally:
        slots.slots.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.1)
    assert slots.slots.acquire(blocking=False), "cancelled waiter acquired after its exit"
    slots.slots.release()
    assert not aw.store.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize("stage", ["authenticate", "run"])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancelled_request_holds_slot_until_db_worker_finishes(env, monkeypatch,
                                                                  surface, stage, repeat_cancel):
    aw, _client = env
    key = aw.issue()["secret"]
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(keys, "_bearer_slots", slots)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = getattr(aw.gateway, stage)

    def blocked(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(3)
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(aw.gateway, stage, blocked)

    async def read_body(_request):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "get_statement_context"}}).encode()
        return raw, len(raw), False

    monkeypatch.setattr(rest, "_read_body", read_body)
    endpoint, path = ((rest.get_context, rest.ROOT + "/context") if surface == "rest"
                      else (mcp.mcp, "/v1/assistant/mcp"))
    task = asyncio.create_task(endpoint(request_for(path, key), aw.config))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        if repeat_cancel:
            task.cancel()
            await asyncio.sleep(0.02)
        available = slots.acquire(blocking=False)
        if available:
            slots.release()
        assert not available, "cancelled request freed its slot while DB worker was running"
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.__traceback__ is not None
        assert finished.is_set()
        assert slots.acquire(blocking=False)
        slots.release()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 3)


@pytest.mark.asyncio
async def test_cancelled_failing_db_worker_never_logs_its_exception_payload(caplog):
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    observed = []
    entered = threading.Event()
    release = threading.Event()

    def fail():
        entered.set()
        assert release.wait(3)
        raise ValueError("private-provider-payload")

    loop.set_exception_handler(lambda _loop, context: observed.append(context))
    task = asyncio.create_task(rest._bearer_threadpool(fail))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.__traceback__ is not None
        await asyncio.sleep(0)
        assert observed == []
        assert "private-provider-payload" not in caplog.text
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        loop.set_exception_handler(previous)
