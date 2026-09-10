"""Real PostgreSQL session serialization, without live provider requests."""

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from uuid import uuid4

import httpx
import pytest
from app.api import website as website_api
from app.api.deps import get_db
from app.core.config import Settings
from app.db.base import Base
from app.db.site_v2 import SiteV2MessageRow, SiteV2SessionRow
from app.db.store import LeadStore
from app.integrations.llm_client import LlmResponse, ToolCall
from app.main import app
from app.surfaces.site_v2 import begin_site_message, create_site_session, run_site_v2_turn
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker


def _fixture_validation(kwargs):
    if not any(
        tool.get("function", {}).get("name") == "validate_site_narrative"
        for tool in kwargs.get("tools") or []
    ):
        return None
    proposed = json.loads(kwargs["messages"][-1]["content"])["proposed_reply"]
    assert proposed == "Test reply"
    return LlmResponse(
        "", (ToolCall("validation", "validate_site_narrative", {
            "decision": "safe", "reviewed_text": "Test reply", "evidence": "",
        }, "{}"),), "stop", "", 0, 0, {},
    )


@pytest.fixture
def pg_sessions():
    url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("MIA_TEST_POSTGRES_URL required for PostgreSQL serialization proof")
    schema = "mia_site_v2_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema} -clock_timeout=2000"},
    )
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("same_message", [False, True])
def test_concurrent_messages_preserve_history_and_deduplicate(
    pg_sessions,
    monkeypatch,
    same_message,
):
    calls = []
    calls_lock = Lock()

    class Provider:
        def enabled(self):
            return True

        def complete(self, **kwargs):
            validation = _fixture_validation(kwargs)
            if validation is not None:
                return validation
            with calls_lock:
                calls.append(kwargs["messages"])
            return LlmResponse(
                "Test reply",
                (),
                "stop",
                "",
                0,
                0,
                {"role": "assistant", "content": "Test reply"},
            )

    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda settings: Provider())
    monkeypatch.setattr("app.surfaces.site_v2._knowledge", lambda *args: ())
    with pg_sessions() as db:
        credential, _ = create_site_session(db, session_id="site-pg", page={})
        db.commit()
    barrier = Barrier(2)
    settings = Settings(_env_file=None, website_v2_enabled=True, crm_v2_enabled=True)

    def request(index):
        message_id = "same" if same_message else f"turn-{index}"
        body = "hello" if same_message else f"hello-{index}"
        with pg_sessions() as db:
            barrier.wait(timeout=10)
            _, replay = begin_site_message(
                db,
                session_id="site-pg",
                credential=credential,
                client_message_id=message_id,
                payload={"text": body},
            )
            if replay is not None:
                return "replay"
            run_site_v2_turn(
                db,
                settings=settings,
                session_id="site-pg",
                credential=credential,
                client_message_id=message_id,
                text=body,
            )
            db.commit()
            return "completed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, (1, 2)))
    expected_turns = 1 if same_message else 2
    assert len(calls) == expected_turns
    assert sorted(results) == (["completed", "replay"] if same_message else ["completed"] * 2)
    with pg_sessions() as db:
        row = db.get(SiteV2SessionRow, "site-pg")
        assert row.active_message_id == ""
        turns = json.loads(row.state_json)["turns"]
        assert len(turns) == expected_turns * 2
        completed = db.scalars(select(SiteV2MessageRow)).all()
        assert len(completed) == expected_turns
        assert all(turn.status == "completed" for turn in completed)
    if not same_message:
        visitor_texts = {message["content"] for message in calls[-1] if message["role"] == "user"}
        assert {"hello-1", "hello-2"} <= visitor_texts


@pytest.mark.asyncio
async def test_async_http_concurrency_does_not_block_event_loop_on_session_lock(
    pg_sessions, monkeypatch
) -> None:
    class SlowProvider:
        def enabled(self):
            return True

        def complete(self, **_kwargs):
            validation = _fixture_validation(_kwargs)
            if validation is not None:
                return validation
            time.sleep(0.3)
            return LlmResponse(
                "Test reply",
                (),
                "stop",
                "",
                0,
                0,
                {"role": "assistant", "content": "Test reply"},
            )

    def override_db():
        with pg_sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: SlowProvider())
    monkeypatch.setattr("app.surfaces.site_v2._knowledge", lambda *args: ())
    monkeypatch.setattr(website_api, "get_session_factory", lambda: pg_sessions)
    app.dependency_overrides[get_db] = override_db
    try:
        with pg_sessions() as db:
            LeadStore(db).open_website_session("site-http-pg")
            credential, _ = create_site_session(db, session_id="site-http-pg", page={})
            db.commit()
        headers = {
            "X-Mia-Session-Credential": credential,
            "Origin": "http://127.0.0.1:8000",
        }
        transport = httpx.ASGITransport(app=app)
        started = time.perf_counter()
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            responses = await asyncio.gather(
                client.post(
                    "/v1/website/sessions/site-http-pg/messages",
                    json={"text": "first", "client_message_id": "http-1"},
                    headers=headers,
                ),
                client.post(
                    "/v1/website/sessions/site-http-pg/messages",
                    json={"text": "second", "client_message_id": "http-2"},
                    headers=headers,
                ),
            )
        elapsed = time.perf_counter() - started
        assert [response.status_code for response in responses] == [200, 200]
        assert elapsed < 1.5
    finally:
        app.dependency_overrides.pop(get_db, None)
