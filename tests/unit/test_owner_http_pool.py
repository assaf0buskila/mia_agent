"""Local transport/lifecycle evidence. No business providers or credentials."""

import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest
from app.brain.embeddings import EmbeddingError, OpenAIEmbeddingPort, build_embedding_port
from app.core.config import Settings
from app.domain.owner.brain import build_agent_client
from app.integrations import owner_http
from app.integrations.llm_client import LlmClient


@pytest.fixture(autouse=True)
def isolated_owner_pool():
    owner_http.close_owner_http_client()
    yield
    owner_http.close_owner_http_client()


def test_one_pool_under_simultaneous_first_use_and_recreated_after_close():
    with ThreadPoolExecutor(max_workers=8) as executor:
        clients = list(executor.map(lambda _: owner_http.get_owner_http_client(), range(16)))
    assert all(client is clients[0] for client in clients)
    owner_http.close_owner_http_client()
    assert clients[0].is_closed
    owner_http.close_owner_http_client()
    assert owner_http.get_owner_http_client() is not clients[0]


def test_real_keepalive_connection_reuse_and_close():
    peers = []
    finished = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            peers.append(self.client_address)
            body = b'{"choices":[{"message":{"content":"ok"}}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

        def finish(self):
            super().finish()
            finished.append(self.client_address)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pool = owner_http.get_owner_http_client()
        for key in ("first-key", "second-key", "third-key"):
            client = LlmClient(
                api_key=key, model="synthetic",
                url=f"http://127.0.0.1:{server.server_port}/chat", client=pool,
            )
            assert client.complete(messages=[{"role": "user", "content": key}]).text == "ok"
        assert len(peers) == 3
        assert len(set(peers)) == 1, "the same TCP connection must serve all three turns"
        owner_http.close_owner_http_client()
        assert pool.is_closed
    finally:
        owner_http.close_owner_http_client()
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert peers[0] in finished, "closing the pool must close the keepalive socket"


def test_auth_cookies_payloads_and_fallback_state_do_not_cross_turns(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        headers = {"Set-Cookie": "private=first-turn; Path=/"}
        if request.url.path.endswith("embeddings"):
            return httpx.Response(200, headers=headers,
                json={"data": [{"index": 0, "embedding": [1., 0.]}]})
        if body["model"] == "blocked":
            return httpx.Response(404, headers=headers)
        return httpx.Response(200, headers=headers, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]})

    original = httpx.Client
    created = []

    def factory(**kwargs):
        created.append(1)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(owner_http.httpx, "Client", factory)
    settings = Settings(_env_file=None, openai_api_key="first-key", gemini_api_key="g-key",
        owner_agent_model="blocked", owner_agent_fallback_model="ok",
        owner_agent_gemini_model="gemini", embedding_model="embedding", embedding_dim=2)
    first = build_agent_client(settings)
    assert first.complete(messages=[{"role": "user", "content": "first-turn"}]).text == "ok"
    assert first.errors
    settings.openai_api_key = "second-key"
    second = build_agent_client(settings)
    assert second is not first
    assert second.errors == [] and second.last_model == ""
    assert second._active_index == 0
    assert second.complete(messages=[{"role": "user", "content": "second-turn"}]).text == "ok"
    port = build_embedding_port(settings, client=owner_http.get_owner_http_client())
    assert port.embed(["embedding-only"]) == [[1., 0.]]
    assert created == [1]
    assert all("cookie" not in request.headers for request in requests)
    assert not list(owner_http.get_owner_http_client().cookies.jar)
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer first-key", "Bearer first-key", "Bearer second-key", "Bearer second-key",
        "Bearer second-key",
    ]
    assert all("first-turn" not in request.content.decode() for request in requests[2:])
    assert all(c._client is owner_http.get_owner_http_client() for c in second._clients)
    assert second._clients[-1]._api_key == "g-key"


@pytest.mark.asyncio
async def test_fastapi_lifespan_closes_owner_pool(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr("app.workers.crm_runtime.start_crm_runtime", lambda *a: None)
    monkeypatch.setattr("app.workers.reminders.start_reminder_runtime", lambda *a: None)
    monkeypatch.setattr(main.os_mirror, "start_if_enabled", lambda *a: None)
    async with main.lifespan(main.app):
        pool = owner_http.get_owner_http_client()
        assert not pool.is_closed
    assert pool.is_closed


def test_website_pool_remains_separate():
    from app.integrations.site_http import get_site_http_client

    assert owner_http.get_owner_http_client() is not get_site_http_client()


def test_unprepared_injected_real_client_falls_back_before_network():
    with httpx.Client() as http:
        port = OpenAIEmbeddingPort(api_key="synthetic", model="m", dim=2, client=http)
        with pytest.raises(EmbeddingError, match="total deadline support"):
            port.embed_with_timeout(["query"], timeout=.2)


@pytest.mark.parametrize("pooled", [True, False])
@pytest.mark.parametrize("slow_headers", [True, False])
def test_absolute_embedding_deadline_stops_trickling_response_and_resets(pooled, slow_headers):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"data":[{"index":0,"embedding":[1,0]}]}'
            headers = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            try:
                if self.path == "/slow" and slow_headers:
                    for part in (headers[:8], headers[8:16], headers[16:24], headers[24:]):
                        sleep(.15)
                        self.wfile.write(part)
                else:
                    self.wfile.write(headers)
                if self.path == "/slow" and not slow_headers:
                    for part in (body[:8], body[8:16], body[16:24], body[24:]):
                        sleep(.15)
                        self.wfile.write(part)
                else:
                    self.wfile.write(body)
            except OSError:
                pass  # Expected: the deadline closes the timed-out connection.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pool = owner_http.get_owner_http_client() if pooled else None
        root = f"http://127.0.0.1:{server.server_port}"
        fast = OpenAIEmbeddingPort(api_key="synthetic", model="m", dim=2,
            url=root + "/fast", client=pool)
        assert fast.embed(["warm pooled connection"]) == [[1., 0.]]
        slow = OpenAIEmbeddingPort(api_key="synthetic", model="m", dim=2,
            url=root + "/slow", client=pool)
        started = monotonic()
        with pytest.raises(EmbeddingError):
            slow.embed_with_timeout(["query"], timeout=.22)
        assert monotonic() - started < .45
        # A failed request cannot leave its expired ContextVar in this thread, or
        # poison the shared connection pool for the next owner turn.
        assert fast.embed(["next turn"]) == [[1., 0.]]
    finally:
        owner_http.close_owner_http_client()
        server.shutdown()
        server.server_close()
        thread.join(2)
