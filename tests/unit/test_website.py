import json
from urllib.parse import urlparse

from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.attribution import sanitize_attribution
from app.domain.events import Channel
from app.domain.handoff import click_to_chat_url
from app.domain.sales import NextAction, select_next_action
from app.graph.orchestrator import build_graph
from app.graph.state import empty_state
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.test_handoff import CLICK_CHAT


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
        assert body["whatsapp_url"] is None
        assert "טלפון או באימייל" in body["opening"]


def test_website_config_exposes_only_a_config_derived_whatsapp_url(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_CLICK_TO_CHAT", CLICK_CHAT)
    with TestClient(app) as client:
        body = client.get("/v1/website/config").json()
    # WhatsApp is offered after phone or email, never on config.
    assert body["whatsapp_url"] is None


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
        assert body["lead_id"] == ""
        assert body["next_action"] in {"ask_need", "ask_contact"}
        assert body["whatsapp_url"] is None
        empty_end = client.post(f"/v1/website/sessions/{session_id}/end")
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
        visitor_in = [
            row for row in in_rows if json.loads(row.payload_json).get("text") == "hi"
        ]
        assert len(visitor_in) == 1
        assert created_rows == []
        visitor_in = visitor_in[0]
        assert visitor_in.actor_role == "prospect"
        assert visitor_in.lead_id in {None, ""}
        assert not str(visitor_in.lead_id or "").startswith("lead_")
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
        assert body["lead_id"] == ""
        assert body["next_action"] in {"ask_need", "ask_contact", "handoff"}
        assert "lead_" not in body["message"]


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
        assert reply.json()["message"]
    db = get_session_factory()()
    try:
        out_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "message_out",
                )
            )
        )
        assert out_rows
    finally:
        db.close()


def test_website_session_create_does_not_write_01_leads() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert created.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.get_tool_run(f"{session_id}:tool:sheets_mirror") is None
        tool_event = db.scalar(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == f"{session_id}:tool:sheets_mirror"
            )
        )
        assert tool_event is None
        created_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "lead_created",
                )
            )
        )
        assert created_rows == []
    finally:
        db.close()


def test_website_session_create_kill_switch_still_opens(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        assert reply.status_code == 200
        assert reply.json()["lead_id"] == ""


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
        assert body["lead_id"] == ""
        assert not session_id.startswith("lead_")
    db = get_session_factory()()
    try:
        created_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "lead_created",
                )
            )
        )
        assert created_rows == []
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
        assert created_rows == []
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


def test_website_session_create_persists_open_event_without_lead_id() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert created.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id
                )
            )
        )
        assert rows
        assert all(not str(row.lead_id or "").startswith("lead_") for row in rows)
    finally:
        db.close()


def test_website_first_message_persists_visitor_text() -> None:
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
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "message_in",
                )
            )
        )
        texts = [json.loads(row.payload_json).get("text") for row in rows]
        assert "hi" in texts
        assert "again" in texts
    finally:
        db.close()


def test_website_handoff_persists_whatsapp_handoff() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        missing = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert missing.status_code == 409
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "צריכים אתר", "phone": "0501234567"},
        )
        response = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert response.status_code == 200
        assert response.json()["notification_status"] in {"delivered", "failed"}
    db = get_session_factory()()
    try:
        rows = _behavior_rows(db, session_id)
        handoff = [r for r in rows if json.loads(r.payload_json)["kind"] == "whatsapp_handoff"]
        assert len(handoff) == 1
        assert handoff[0].provider_event_id == f"{session_id}:whatsapp_handoff"
        assert not str(handoff[0].lead_id or "").startswith("lead_")
    finally:
        db.close()


def test_website_message_pings_assaf_after_contact(monkeypatch) -> None:
    from app.api.deps import get_telegram_port
    from app.integrations.base import RecordingMessagePort

    port = RecordingMessagePort()
    app.dependency_overrides[get_telegram_port] = lambda: port
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "111")
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            need = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "צריכים אתר"},
            )
            assert need.status_code == 200
            assert port.sent == []
            captured = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "0501234567", "phone": "0501234567", "name": "דנה"},
            )
            assert captured.status_code == 200
            assert captured.json()["next_action"] == "handoff"
            handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
            assert handoff.status_code == 200
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
    assert port.sent
    assert "שיחה מהאתר" in port.sent[0].text
    assert "0501234567" in port.sent[0].text


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


def test_website_kill_switch_does_not_stop_chat(monkeypatch) -> None:
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
        assert reply.json()["message"]
        assert reply.json()["lead_id"] == ""


def test_website_identify_then_sell_asks_for_contact() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        msg1 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        assert msg1.status_code == 200
        assert msg1.json()["lead_id"] == ""
        assert msg1.json()["next_action"] == "ask_contact"
        assert msg1.json()["whatsapp_url"] is None
        assert "מחיר" not in msg1.json()["message"]


def test_exact_direct_assaf_then_yalla_sequence_still_needs_contact(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_CLICK_TO_CHAT", CLICK_CHAT)
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        direct = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "אפשר להגיע לאסף?"},
        )
        confirm = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "יאללה 0501234567", "phone": "0501234567"},
        )
    assert direct.status_code == 200
    assert direct.json()["whatsapp_url"] is None
    assert confirm.status_code == 200
    assert confirm.json()["next_action"] == "handoff"
    assert confirm.json()["whatsapp_url"] == click_to_chat_url(CLICK_CHAT)


def test_website_conversation_asks_contact_before_whatsapp() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        hi = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "היי"},
        )
        assert hi.json()["next_action"] in {"ask_need", "ask_contact"}
        assert hi.json()["whatsapp_url"] is None
        shoes = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "אני מוכר נעליים יש לי עיסוק רק במלאי"},
        )
        assert shoes.status_code == 200
        assert shoes.json()["next_action"] == "ask_contact"
        assert shoes.json()["whatsapp_url"] is None


def test_website_price_question_does_not_invent_a_number() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        body = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "כמה עולה?"},
        ).json()
        assert body["next_action"] == "no_price"
        assert "מחיר" in body["message"]
        assert body["whatsapp_url"] is None


def test_offer_whatsapp_reply_includes_click_to_chat_url_after_contact(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_CLICK_TO_CHAT", CLICK_CHAT)
    expected = click_to_chat_url(CLICK_CHAT)
    parsed = urlparse(expected)
    assert parsed.scheme == "https"
    assert parsed.hostname == "wa.me"
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        more = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "צריכים אתר", "phone": "0501234567", "name": "דנה"},
        )
        assert more.status_code == 200
        assert more.json()["next_action"] == "handoff"
        assert more.json()["whatsapp_url"] == expected
