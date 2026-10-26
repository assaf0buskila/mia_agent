"""D1/D2: client uploads never reserve DB capacity; refusals retain IP accounting."""

import asyncio
import json
import threading
from urllib.parse import urlencode

import pytest
from app.api.assistant import mcp, oauth
from app.api.assistant import os as rest
from app.services import assistant_keys as keys
from starlette.requests import Request

from tests.services.os_ledger.assistant.support import IP_A
from tests.services.os_ledger.assistant.test_oauth_flow import VERIFIER, Flow
from tests.services.os_ledger.statements.support import batch, encode, pg_url, three_lines


def request(surface, key, receive):
    path = {"rest": rest.ROOT + "/statement-batches", "mcp": "/v1/assistant/mcp",
            "oauth": "/oauth/token"}[surface]
    headers = [] if surface == "oauth" else [(b"authorization", ("Bearer " + key).encode())]
    headers.append((b"content-type", b"application/x-www-form-urlencoded"
                    if surface == "oauth" else b"application/json"))
    return Request({"type": "http", "method": "POST", "path": path,
                    "client": (IP_A, 50000), "headers": headers}, receive=receive)


def endpoint(surface):
    return {"rest": rest.submit_batch, "mcp": mcp.mcp, "oauth": oauth.token}[surface]


def setup(aw, monkeypatch):
    flow = Flow(aw)
    monkeypatch.setattr(rest, "gateway_for", lambda _: aw.gateway)
    monkeypatch.setattr(oauth, "service_for", lambda _: flow.service)
    monkeypatch.setattr(keys, "_bearer_slots", threading.BoundedSemaphore(3))
    monkeypatch.setattr(keys, "_BEARER_WAIT_SECONDS", 0.05)
    return flow


def capacity():
    acquired = 0
    try:
        while acquired < 3 and keys._bearer_slots.acquire(blocking=False):
            acquired += 1
        assert acquired == 3
        assert not keys._bearer_slots.acquire(blocking=False)
    finally:
        for _ in range(acquired):
            keys._bearer_slots.release()


async def normal(surface, aw, key, flow):
    if surface == "oauth":
        _, code = flow.code()
        raw = urlencode({"grant_type": "authorization_code", "code": code,
                         "client_id": flow.oauth.CLIENT_ID,
                         "redirect_uri": flow.oauth.REDIRECT_URI, "code_verifier": VERIFIER,
                         "resource": flow.oauth.resource(aw.config)}).encode()
    elif surface == "rest":
        raw = encode(batch(three_lines()))
    else:
        raw = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return await endpoint(surface)(request(surface, key, receive), aw.config)


def refused(response, surface):
    assert response.status_code == (400 if surface == "oauth" else 422)
    body = json.loads(response.body)
    if surface == "oauth":
        assert body == {"error": "invalid_request"}
    else:
        assert body["error"]["code"] == "validation_error"
    assert "no-store" in response.headers["cache-control"]


if pg_url():
    @pytest.mark.parametrize("surface", ["rest", "mcp", "oauth"])
    async def test_three_stalled_uploads_leave_capacity_for_normal_requests(
            pg_aw, monkeypatch, surface):
        aw = pg_aw
        flow = setup(aw, monkeypatch)
        key = aw.issue()["secret"]
        for module in (rest, oauth):
            monkeypatch.setattr(module, "BODY_TIMEOUT_SECONDS", 0.8, raising=False)
        entered = [asyncio.Event() for _ in range(3)]

        async def receive(index):
            entered[index].set()
            await asyncio.Event().wait()

        tasks = [asyncio.create_task(endpoint(surface)(
            request(surface, key, lambda i=i: receive(i)), aw.config)) for i in range(3)]
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)), 2)
            # Before any deadline, all slots are free and another request finishes immediately.
            capacity()
            response = await asyncio.wait_for(normal(surface, aw, key, flow), 0.4)
            assert response.status_code == 200, response.body
            assert all(not task.done() for task in tasks)
            if surface == "oauth":
                # An unauthenticated upload cannot starve the other transport either.
                response = await asyncio.wait_for(normal("mcp", aw, key, flow), 0.4)
                assert response.status_code == 200, response.body
            for response in await asyncio.wait_for(asyncio.gather(*tasks), 2):
                refused(response, surface)
            rows = aw.audit()
            assert len(rows) == (5 if surface == "oauth" else 4)
            assert len([r for r in rows if r["reason"] == "body_timeout"]) == 3
            assert all(r["client_ip_hmac"] == aw.ip_hmac(IP_A) for r in rows)
            assert aw.credential_rows()[0]["use_count"] == (4 if surface == "rest" else 0)
            capacity()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.parametrize("surface", ["rest", "mcp", "oauth"])
    @pytest.mark.parametrize("outcome", ["timeout", "disconnect"])
    async def test_upload_failure_is_counted_once_at_the_size_refusal_point(
            pg_aw, monkeypatch, surface, outcome):
        aw = pg_aw
        setup(aw, monkeypatch)
        issued = aw.issue()
        before = aw.credential(issued["credential_id"])
        for module in (rest, oauth):
            monkeypatch.setattr(module, "BODY_TIMEOUT_SECONDS", 0.01, raising=False)

        async def receive():
            if outcome == "disconnect":
                return {"type": "http.disconnect"}
            await asyncio.Event().wait()

        response = await asyncio.wait_for(endpoint(surface)(
            request(surface, issued["secret"], receive), aw.config), 1)
        refused(response, surface)
        [row] = aw.audit()
        assert row["reason"] == "body_timeout"
        assert row["operation"] == {"rest": "submit", "mcp": "mcp_control",
                                    "oauth": "oauth_token"}[surface]
        assert row["client_ip_hmac"] == aw.ip_hmac(IP_A)
        if surface != "rest":
            assert aw.credential(issued["credential_id"]) == before
        else:
            assert aw.credential(issued["credential_id"])["use_count"] == 1
        aw.seed_audit(59, ip_hmac=aw.ip_hmac(IP_A), operation="context")
        response = await asyncio.wait_for(endpoint(surface)(
            request(surface, issued["secret"], receive), aw.config), 1)
        assert response.status_code == 429
        assert len(aw.audit()) == 60
        capacity()
