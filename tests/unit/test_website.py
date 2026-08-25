import json

from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.attribution import sanitize_attribution
from app.domain.events import Channel
from app.domain.sales import NextAction, select_next_action
from app.graph.orchestrator import build_graph
from app.graph.state import empty_state
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select


def test_store_reuses_website_session() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        c1, l1 = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_abc")
        c2, l2 = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_abc")
        db.commit()
        assert c1 == c2
        assert l1 == l2
        sales = store.get_sales(l1)
        assert select_next_action(sales) == NextAction.UNDERSTAND_WORKFLOW
    finally:
        db.close()


def test_graph_returns_website_opening_question() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_graph")
        db.commit()
        result = build_graph(store).invoke(
            empty_state(run_id="run_1", thread_id="web_graph", channel="website", lead_id=lead_id)
        )
        assert result["next_action"] == "understand_workflow"
        assert "יום רגיל בעסק" in result["reply"]
    finally:
        db.close()


def test_website_config_points_at_assafweb() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/website/config")
        assert response.status_code == 200
        body = response.json()
        assert body["website_url"] == "https://www.assafweb.com"
        assert body["widget"] == "ask_mia"
        assert body["demo"] is False
        assert "יום רגיל בעסק" in body["opening"]


def test_website_widget_js_served() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/website/widget.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/javascript")
        assert response.headers["cache-control"] == "no-cache"
        body = response.text
        assert "/v1/website/config" in body
        assert "/v1/website/sessions" in body
        assert "/handoff" in body
        assert "/end" in body
        assert "sendBeacon" in body
        assert "שאלו את מיה" in body
        assert "cfg.demo" in body
        assert "form_started" in body
        assert "שאלו את מיה (דמו)" in body


def test_website_widget_preview_loads_script() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/website/preview")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-cache"
        body = response.text
        assert 'src="/v1/website/widget.js"' in body
        assert "innerHTML" not in body
        assert "auto-open" not in body
        assert "שאלו את מיה" not in body
        assert "שאלו את מיה" not in body


def test_website_widget_js_safe_and_no_auto_open() -> None:
    with TestClient(app) as client:
        body = client.get("/v1/website/widget.js").text
    assert "innerHTML" not in body
    assert "eval(" not in body
    assert "document.write" not in body
    assert "aria-expanded" in body
    assert "addEventListener('click'" in body
    assert "panel.hidden = true" in body
    assert "hostname === 'wa.me'" in body
    assert "/events" in body
    assert "page_viewed" in body
    assert "data-mia-section" in body
    assert "data-mia-cta" in body
    assert "data-mia-form" in body
    assert "section_viewed" in body
    assert "cta_click" in body
    assert "form_started" in body
    assert "form_abandoned" in body
    assert "IntersectionObserver" in body
    assert "eventQueue" in body
    assert "data.token" not in body
    assert "innerText" not in body
    assert "data-mia-form-bound" not in body
    assert "#ask-mia-root{" in body
    assert "font:16px/1.5" in body
    assert "min-height:44px" in body
    assert "color:#061b35" in body
    assert "#ask-mia-input{" in body
    assert "font-size:16px" in body
    assert "background:#fff" in body
    assert ".ask-mia-msg{" in body
    assert ".ask-mia-row{" in body
    assert "ask-mia-bubble-avatar" in body
    assert "showLoading" in body
    assert ".ask-mia-user{background:#2f5f93;color:#fff" in body
    assert "lastMiaText" in body
    assert "role === 'mia' && text === lastMiaText()" in body
    assert "#ask-mia-send{background:#2f5f93;color:#fff}" in body
    assert "flex-direction:column-reverse" in body
    assert "askMia.sessionId" in body
    assert "offer_whatsapp" in body
    assert "#ask-mia-wa.offer{" in body
    assert "#ask-mia-header{" in body
    assert "ask-mia-launch-mark" in body
    assert "#ask-mia-launch-label" in body
    assert "clip:rect(0,0,0,0)" not in body
    assert ".whatsapp-fab{display:none!important}" in body
    assert "bottom:max(1.1rem" in body
    assert "launchLabel.textContent" in body
    assert "launcher.textContent = 'שאלו את מיה (דמו)'" not in body
    assert "launcher.textContent = 'שאלו את מיה (דמו)'" not in body


def test_website_api_session_and_message() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        assert reply.status_code == 200
        body = reply.json()
        assert body["next_action"] == "understand_workflow"
        assert "יום רגיל בעסק" in body["message"]
        empty_end = client.post(f"/v1/website/sessions/{session_id}/end")
        # session already has a visitor message from the turn above
        assert empty_end.status_code == 200
        first_end = empty_end.json()
        assert first_end["accepted"] is True
        assert first_end["finalized"] is True
        second_end = client.post(f"/v1/website/sessions/{session_id}/end")
        assert second_end.json()["finalized"] is False
    db = get_session_factory()()
    try:
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id
                )
            )
        )
        in_rows = [row for row in rows if row.event_type == "message_in"]
        created_rows = [row for row in rows if row.event_type == "lead_created"]
        behavior_rows = [row for row in rows if row.event_type == "behavior"]
        qual_rows = [row for row in rows if row.event_type == "qualification_updated"]
        meet_rows = [row for row in rows if row.event_type == "meeting_offered"]
        handoff_rows = [row for row in rows if row.event_type == "handoff"]
        tool_rows = [row for row in rows if row.event_type == "tool_result"]
        assert len(in_rows) == 1
        assert len(created_rows) == 1
        assert len(behavior_rows) == 2
        assert len(tool_rows) == 2
        sheets_tools = [
            row for row in tool_rows
            if json.loads(row.payload_json)["tool"] == "sheets_mirror"
        ]
        assert len(sheets_tools) == 2
        cal_tool_rows = [
            row for row in tool_rows
            if json.loads(row.payload_json)["tool"] == "calendar_find_free_slots"
        ]
        assert len(cal_tool_rows) == 0
        assert len(qual_rows) == 1
        assert len(meet_rows) == 0
        assert len(handoff_rows) == 0
        behavior_kinds = {json.loads(r.payload_json)["kind"] for r in behavior_rows}
        assert behavior_kinds == {"mia_opened", "conversation_started"}
        assert in_rows[0].actor_role == "prospect"
        assert in_rows[0].lead_id == body["lead_id"]
        payload = json.loads(in_rows[0].payload_json)
        assert payload == {"text": "hi"}
        assert json.loads(in_rows[0].source_json) == {"provider": "website"}
        out_rows = [row for row in rows if row.event_type == "message_out"]
        assert len(out_rows) == 1
        assert out_rows[0].actor_role == "mia"
        assert json.loads(out_rows[0].payload_json)["text"] == body["message"]
    finally:
        db.close()


def test_website_end_skips_session_without_visitor_message() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        ended = client.post(f"/v1/website/sessions/{session_id}/end")
        assert ended.status_code == 200
        assert ended.json()["finalized"] is False


def test_website_inquiries_answer_moves_past_opening() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "היי"},
        )
        follow = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "פניות"},
        )
        assert follow.status_code == 200
        body = follow.json()
        assert body["next_action"] == "deepen_pain"
        assert "יום רגיל בעסק" not in body["message"]


def test_website_kill_switch_persists_sheets_tool_denied(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        assert reply.status_code == 200
    db = get_session_factory()()
    try:
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "tool_result",
                )
            )
        )
        assert len(rows) == 2
        for row in rows:
            payload = json.loads(row.payload_json)
            assert payload == {"tool": "sheets_mirror", "status": "denied", "result_count": 0}
        out_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "message_out",
                )
            )
        )
        assert len(out_rows) == 1
    finally:
        db.close()


def test_website_session_create_persists_sheets_mirror_tool_run() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        row = store.get_tool_run(f"{session_id}:tool:sheets_mirror")
        assert row is not None
        assert row.status == "ok"
        assert row.result_count > 0
        tool_event = db.scalar(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == f"{session_id}:tool:sheets_mirror"
            )
        )
        assert tool_event is not None
        payload = json.loads(tool_event.payload_json)
        assert payload["tool"] == "sheets_mirror"
        assert payload["status"] == "ok"
        assert payload["result_count"] > 0
        assert "latency_ms" not in payload
    finally:
        db.close()


def test_website_session_create_sheets_mirror_latency(monkeypatch) -> None:
    monkeypatch.setattr("app.api.website.elapsed_ms", lambda _started: 12)
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        row = store.get_tool_run(f"{session_id}:tool:sheets_mirror")
        assert row is not None
        assert row.latency_ms == 12
        tool_event = db.scalar(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == f"{session_id}:tool:sheets_mirror"
            )
        )
        assert tool_event is not None
        payload = json.loads(tool_event.payload_json)
        assert "latency_ms" not in payload
    finally:
        db.close()


def test_website_session_create_kill_switch_sheets_mirror_denied(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    monkeypatch.setattr("app.api.website.elapsed_ms", lambda _started: 12)
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        row = store.get_tool_run(f"{session_id}:tool:sheets_mirror")
        assert row is not None
        assert row.status == "denied"
        assert row.result_count == 0
        assert row.latency_ms == 12
        tool_event = db.scalar(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == f"{session_id}:tool:sheets_mirror"
            )
        )
        assert tool_event is not None
        payload = json.loads(tool_event.payload_json)
        assert payload == {"tool": "sheets_mirror", "status": "denied", "result_count": 0}
        assert "latency_ms" not in payload
    finally:
        db.close()


def test_website_sheets_mirror_tool_run_latency(monkeypatch) -> None:
    monkeypatch.setattr("app.api.website.elapsed_ms", lambda _started: 12)
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        assert reply.status_code == 200
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        in_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "message_in",
                )
            )
        )
        assert len(in_rows) == 1
        provider_event_id = in_rows[0].provider_event_id
        sales_tool = db.scalar(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id
                == f"{provider_event_id}:tool:sheets_mirror"
            )
        )
        assert sales_tool is not None
        payload = json.loads(sales_tool.payload_json)
        assert payload["tool"] == "sheets_mirror"
        assert payload["status"] == "ok"
        assert payload["result_count"] > 0
        assert "latency_ms" not in payload
        row = store.get_tool_run(f"{provider_event_id}:tool:sheets_mirror")
        assert row is not None
        assert row.latency_ms == 12
    finally:
        db.close()


def test_website_kill_switch_sheets_mirror_latency(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    monkeypatch.setattr("app.api.website.elapsed_ms", lambda _started: 12)
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        assert reply.status_code == 200
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        in_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "message_in",
                )
            )
        )
        assert len(in_rows) == 1
        provider_event_id = in_rows[0].provider_event_id
        sales_tool = db.scalar(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id
                == f"{provider_event_id}:tool:sheets_mirror"
            )
        )
        assert sales_tool is not None
        payload = json.loads(sales_tool.payload_json)
        assert payload == {"tool": "sheets_mirror", "status": "denied", "result_count": 0}
        assert "latency_ms" not in payload
        row = store.get_tool_run(f"{provider_event_id}:tool:sheets_mirror")
        assert row is not None
        assert row.latency_ms == 12
    finally:
        db.close()


def test_sanitize_attribution_allowlists_and_strips_urls() -> None:
    result = sanitize_attribution({
        "utm_source": " meta ",
        "utm_medium": "cpc",
        "utm_campaign": "yuma",
        "utm_content": "user@evil.com",
        "landing_page": "https://www.assafweb.com/x?email=a@b.com",
        "referrer": "javascript:alert(1)",
        "utm_hack": "ignored",
    })
    assert result["utm_source"] == "meta"
    assert result["utm_medium"] == "cpc"
    assert result["utm_campaign"] == "yuma"
    assert "utm_content" not in result
    assert result["landing_page"] == "https://www.assafweb.com/x"
    assert "@" not in json.dumps(result)
    assert "?" not in result["landing_page"]
    assert "referrer" not in result
    assert "utm_hack" not in result
    assert sanitize_attribution({"landing_page": "https://www.assafweb.com/x\nphish"}) == {}
    assert sanitize_attribution({"referrer": "//evil.example/phish"}) == {}


def test_website_session_with_utms_persists_attribution() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/v1/website/sessions",
            params={
                "utm_source": "meta",
                "utm_campaign": "yuma",
                "landing_page": "https://www.assafweb.com/he?foo=1",
                "utm_hack": "1",
            },
        )
        assert created.status_code == 200
        body = created.json()
        session_id = body["session_id"]
        lead_id = body["lead_id"]
    db = get_session_factory()()
    try:
        attr_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "attribution",
                )
            )
        )
        assert len(attr_rows) == 1
        row = attr_rows[0]
        assert row.lead_id == lead_id
        assert row.provider_event_id == f"{lead_id}:attribution"
        assert row.event_id == f"evt_{lead_id}:attribution"
        payload = json.loads(row.payload_json)
        assert payload == {
            "utm_source": "meta",
            "utm_campaign": "yuma",
            "landing_page": "https://www.assafweb.com/he",
        }
        assert "foo" not in payload
        assert "@" not in json.dumps(payload)
        assert "utm_hack" not in payload
        created_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "lead_created",
                )
            )
        )
        assert len(created_rows) == 1
    finally:
        db.close()


def test_website_session_without_utms_has_no_attribution() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
    db = get_session_factory()()
    try:
        attr_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "attribution",
                )
            )
        )
        assert len(attr_rows) == 0
        created_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "lead_created",
                )
            )
        )
        assert len(created_rows) == 1
    finally:
        db.close()


def _behavior_rows(db, session_id: str) -> list[CanonicalEventRow]:
    return list(
        db.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.conversation_id == session_id,
                CanonicalEventRow.event_type == "behavior",
            )
        )
    )


def test_website_session_create_persists_mia_opened() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
    db = get_session_factory()()
    try:
        rows = _behavior_rows(db, session_id)
        opened = [r for r in rows if json.loads(r.payload_json)["kind"] == "mia_opened"]
        assert len(opened) == 1
        assert json.loads(opened[0].payload_json) == {"kind": "mia_opened"}
        assert opened[0].provider_event_id == f"{session_id}:mia_opened"
    finally:
        db.close()


def test_website_first_message_persists_conversation_started_idempotent() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "again"},
        )
    db = get_session_factory()()
    try:
        rows = _behavior_rows(db, session_id)
        started = [r for r in rows if json.loads(r.payload_json)["kind"] == "conversation_started"]
        assert len(started) == 1
        assert started[0].provider_event_id == f"{session_id}:conversation_started"
    finally:
        db.close()


def test_website_handoff_persists_whatsapp_handoff() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        response = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert response.status_code == 200
    db = get_session_factory()()
    try:
        rows = _behavior_rows(db, session_id)
        handoff = [r for r in rows if json.loads(r.payload_json)["kind"] == "whatsapp_handoff"]
        assert len(handoff) == 1
        assert handoff[0].provider_event_id == f"{session_id}:whatsapp_handoff"
    finally:
        db.close()


def test_website_post_page_viewed_accepted_and_idempotent() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        path = "https://www.assafweb.com/he/pricing?x=1"
        first = client.post(
            f"/v1/website/sessions/{session_id}/events",
            json={"kind": "page_viewed", "path": path},
        )
        assert first.status_code == 200
        assert first.json() == {"accepted": True, "kind": "page_viewed"}
        second = client.post(
            f"/v1/website/sessions/{session_id}/events",
            json={"kind": "page_viewed", "path": path},
        )
        assert second.status_code == 200
        assert second.json() == {"accepted": True, "kind": "page_viewed"}
    db = get_session_factory()()
    try:
        rows = [
            r for r in _behavior_rows(db, session_id)
            if json.loads(r.payload_json)["kind"] == "page_viewed"
        ]
        assert len(rows) == 1
        payload = json.loads(rows[0].payload_json)
        assert payload["path"] == "https://www.assafweb.com/he/pricing"
        assert "?" not in payload["path"]
    finally:
        db.close()


def test_website_post_javascript_path_rejected() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/events",
            json={"kind": "page_viewed", "path": "javascript:alert(1)"},
        )
        assert response.status_code == 200
        assert response.json() == {"accepted": False, "kind": "page_viewed"}
    db = get_session_factory()()
    try:
        page_rows = [
            r for r in _behavior_rows(db, session_id)
            if json.loads(r.payload_json).get("kind") == "page_viewed"
        ]
        assert len(page_rows) == 0
    finally:
        db.close()


def test_website_post_mia_opened_from_client_returns_422() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/events",
            json={"kind": "mia_opened"},
        )
        assert response.status_code == 422


def test_website_post_behavior_unknown_session_404() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/website/sessions/web_missing/events",
            json={"kind": "page_viewed", "path": "/x"},
        )
        assert response.status_code == 404


def test_website_post_cta_click_invalid_accepted_false() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        for cta in ("", "user@evil.com"):
            response = client.post(
                f"/v1/website/sessions/{session_id}/events",
                json={"kind": "cta_click", "cta": cta},
            )
            assert response.status_code == 200
            assert response.json() == {"accepted": False, "kind": "cta_click"}
    db = get_session_factory()()
    try:
        cta_rows = [
            r for r in _behavior_rows(db, session_id)
            if json.loads(r.payload_json).get("kind") == "cta_click"
        ]
        assert len(cta_rows) == 0
    finally:
        db.close()


def test_website_kill_switch_still_persists_message_out(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        assert reply.status_code == 200
        body = reply.json()
        assert body["message"]
    db = get_session_factory()()
    try:
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id
                )
            )
        )
        out_rows = [row for row in rows if row.event_type == "message_out"]
        assert len(out_rows) == 1
        assert json.loads(out_rows[0].payload_json)["text"] == body["message"]
    finally:
        db.close()


def test_website_funnel_reflect_hypothesis_qualify_meeting() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        msg1 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        assert msg1.status_code == 200
        # Pain is clear, the manual step behind it is not. Ask, do not hand off.
        assert msg1.json()["next_action"] == "deepen_pain"
        assert "WhatsApp" not in msg1.json()["message"]
        msg2 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "we call everyone back by hand from a list"},
        )
        assert msg2.status_code == 200
        assert msg2.json()["next_action"] == "reflect"
        msg3 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "about two hours every day"},
        )
        assert msg3.status_code == 200
        # ADR-028: the continuation gate now offers the booked meeting first, the
        # website's default exit, instead of WhatsApp.
        assert msg3.json()["next_action"] == "offer_meeting"
        assert "WhatsApp" not in msg3.json()["message"]
        # The meeting offer was not taken (no acceptance token in the next message),
        # so the very next continuation-ready turn proves WhatsApp is still the
        # reachable fallback (ADR-028), exactly as it always was after the gate.
        msg4 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "I decide this quarter"},
        )
        assert msg4.status_code == 200
        assert msg4.json()["next_action"] == "offer_whatsapp"
        assert "WhatsApp" in msg4.json()["message"]
        # WhatsApp has now been offered, so the gate is closed and the ladder
        # resumes at the next unmet rung, same as pre-ADR-028.
        msg5 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "I'd like to understand the process a bit more first"},
        )
        assert msg5.status_code == 200
        assert msg5.json()["next_action"] == "offer_hypothesis"
        msg6 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "let's book a meeting"},
        )
        assert msg6.status_code == 200
        assert msg6.json()["next_action"] == "offer_meeting"


def test_website_shoe_conversation_offers_whatsapp_without_looping() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        hi = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "היי"},
        )
        assert hi.json()["next_action"] == "understand_workflow"
        assert "וואטסאפ" not in hi.json()["message"]
        shoes = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "אני מוכר נעליים יש לי עיסוק רק במלאי"},
        )
        assert shoes.status_code == 200
        assert shoes.json()["next_action"] == "deepen_pain"
        assert "יום רגיל בעסק" not in shoes.json()["message"]
        sheets = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "להכניס הכל לשיטס"},
        )
        assert sheets.status_code == 200
        # Third rung, not a repeat and not yet a handoff: the sheet entry is known,
        # how often and how long it takes is not.
        assert sheets.json()["next_action"] == "quantify"
        assert "יום רגיל בעסק" not in sheets.json()["message"]
        sizes = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "נעליים מידות דגמים"},
        )
        assert sizes.status_code == 200
        # Enough context to be useful: retailer, inventory work, manual sheet entry,
        # models and sizes. ADR-028: the continuation gate now offers the booked
        # meeting first (the website's default exit), not WhatsApp.
        assert sizes.json()["next_action"] == "offer_meeting"
        assert "וואטסאפ" not in sizes.json()["message"]
        assert "יום רגיל בעסק" not in sizes.json()["message"]
        # The meeting offer was not taken, so the next continuation-ready turn proves
        # WhatsApp is still reachable as the fallback (ADR-028).
        more = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "אשמח לשמוע פרטים נוספים על זה"},
        )
        assert more.status_code == 200
        assert more.json()["next_action"] == "offer_whatsapp"
        assert "וואטסאפ" in more.json()["message"]
        assert "יום רגיל בעסק" not in more.json()["message"]
    db = get_session_factory()()
    try:
        rows = _behavior_rows(db, session_id)
        kinds = {json.loads(r.payload_json)["kind"] for r in rows}
        assert "whatsapp_handoff_offered" in kinds
    finally:
        db.close()


def test_website_defect_a_transcript_never_repeats_a_reply() -> None:
    """The exact reported loop: four messages, four different replies, no restart."""
    transcript = (
        "אני מוכר נעליים יש לי עיסוק רק במלאי",
        "להכניס הכל לשיטס",
        "נעליים מידות דגמים",
        "כל יום בערך שעה",
        "אני מחליט לבד",
    )
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        replies: list[str] = []
        actions: list[str] = []
        for text in transcript:
            body = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": text},
            ).json()
            replies.append(body["message"])
            actions.append(body["next_action"])
        assert len(set(replies)) == len(replies)
        assert "understand_workflow" not in actions
        for reply in replies:
            assert "יום רגיל בעסק" not in reply
            assert "ספר לי קצת על העסק" not in reply
            assert reply.count("?") <= 1


def test_website_prelaunch_wants_site_offers_whatsapp() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        hi = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "היי"},
        )
        assert hi.json()["next_action"] == "understand_workflow"
        opened = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "אני האמת לא עוסק כרגע אני רוצה לפתוח עסק והייתי רוצה אולי לבנות אתר"
            },
        )
        assert opened.status_code == 200
        # ADR-028: stated buying intent still clears the continuation gate on the
        # first substantive answer, but the gate now offers the booked meeting first.
        assert opened.json()["next_action"] == "offer_meeting"
        assert "וואטסאפ" not in opened.json()["message"]
        assert "יום רגיל בעסק" not in opened.json()["message"]
        # The meeting offer was not taken, so the next continuation-ready turn proves
        # WhatsApp is still reachable as the fallback (ADR-028).
        more = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "אשמח להבין את התהליך טוב יותר"},
        )
        assert more.status_code == 200
        assert more.json()["next_action"] == "offer_whatsapp"
        assert "וואטסאפ" in more.json()["message"]
        assert "יום רגיל בעסק" not in more.json()["message"]
        bye = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "בי תודה"},
        )
        assert bye.json()["next_action"] == "stop"
