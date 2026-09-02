"""Dude-clone contract: Telegram talk, Contacts/Activity writes, site identify-then-sell."""

from __future__ import annotations

import pytest
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.integrations.base import RecordingMessagePort
from app.integrations.sheets import FakeSheetsPort
from app.main import app
from app.surfaces.crm import (
    CONTACTS_HEADERS,
    LOCKED_SPREADSHEET_ID,
    ContactRecord,
    CrmDenied,
    FakeContactsCrm,
    log_contact,
)
from app.surfaces.identity import extract_fields
from app.surfaces.owner import run_owner_loop, talk_as_dude
from app.surfaces.site import reset_site_book, run_site_turn, site_book, site_opening
from fastapi.testclient import TestClient

OWNER_ID = "550077"


@pytest.fixture(autouse=True)
def _reset_book() -> None:
    reset_site_book()
    yield
    reset_site_book()


def test_contacts_headers_are_a1_n1_with_date_after_email() -> None:
    assert CONTACTS_HEADERS == (
        "שם",
        "טלפון",
        "אימייל",
        "תאריך",
        "עסק",
        "מקור",
        "שפה",
        "מה רוצים",
        "סטטוס",
        "סיכום שיחה",
        "הבא",
        "נוצר",
        "עודכן",
        "פינג לאסף",
    )
    assert len(CONTACTS_HEADERS) == 14


def test_crm_refuses_row_without_phone_or_email() -> None:
    crm = FakeContactsCrm()
    with pytest.raises(CrmDenied, match="phone or email"):
        log_contact(
            crm,
            ContactRecord(name="דנה", want="אתר"),
            who="מיה",
            channel="website",
            action="שיחת אתר",
            result="נרשם",
        )
    assert crm.contacts == {}
    assert crm.activity == []
    assert "01 Leads" not in crm.written_tabs()


def test_crm_writes_phone_only_or_email_only_and_never_leads_tab() -> None:
    crm = FakeContactsCrm()
    phone_row = log_contact(
        crm,
        ContactRecord(name="דנה", phone="0501234567", want="אתר"),
        who="מיה",
        channel="website",
        action="שיחת אתר",
        result="נרשם",
    )
    email_row = log_contact(
        crm,
        ContactRecord(name="רון", email="ron@example.com", want="אוטומציה"),
        who="אסף",
        channel="telegram",
        action="עדכון איש קשר",
        result="נרשם",
    )
    assert phone_row.phone == "0501234567"
    assert email_row.email == "ron@example.com"
    assert set(crm.written_tabs()) == {"Contacts", "Activity"}
    assert "01 Leads" not in crm.written_tabs()
    blob = " ".join(" ".join(cells) for cells in crm.cells_written)
    assert "lead_" not in blob.lower()
    assert crm.spreadsheet_id == LOCKED_SPREADSHEET_ID


def test_crm_refuses_lead_id_in_cells() -> None:
    crm = FakeContactsCrm()
    with pytest.raises(CrmDenied, match="lead ids"):
        log_contact(
            crm,
            ContactRecord(name="x", phone="0501234567", summary="lead_abc123def456"),
            who="מיה",
            channel="website",
            action="שיחת אתר",
            result="נרשם",
        )


def test_identity_never_mints_lead_id() -> None:
    fields = extract_fields("קוראים לי דנה 0501234567 dana@example.com 2026-09-02")
    assert fields.phone
    assert fields.email
    assert fields.date == "2026-09-02"
    assert "lead_" not in fields.name
    assert "lead_" not in fields.to_contact(source="website").cells()


async def test_telegram_owner_loop_still_sends() -> None:
    init_db()
    db = get_session_factory()()
    port = RecordingMessagePort()
    crm = FakeContactsCrm()
    settings = Settings()
    store = LeadStore(db)
    store.claim_webhook(
        provider="telegram",
        provider_event_id="tg.dude.1",
        channel="telegram",
        envelope_kind="text",
    )
    try:
        result = await run_owner_loop(
            item={"id": "tg.dude.1", "from": OWNER_ID, "text": "מה קורה?", "chat_id": OWNER_ID},
            store=store,
            port=port,
            settings=settings,
            crm=crm,
            owner_ids={OWNER_ID},
        )
        db.commit()
    finally:
        db.close()
    assert result.processed is True
    assert result.sent is True
    assert port.sent
    assert port.sent[0].text
    assert result.last_reply == port.sent[0].text
    assert crm.written_tabs() == ()


def test_telegram_logs_contact_when_phone_present() -> None:
    crm = FakeContactsCrm()
    reply, wrote = talk_as_dude(text="תרשמי את דנה 0501234567", crm=crm)
    assert wrote is True
    assert "Contacts" in reply
    assert "Contacts" in crm.written_tabs()
    assert "Activity" in crm.written_tabs()
    assert "01 Leads" not in crm.written_tabs()


def test_telegram_refuses_contact_log_without_phone_or_email() -> None:
    crm = FakeContactsCrm()
    reply, wrote = talk_as_dude(text="תרשמי איש קשר בלי פרטים", crm=crm)
    assert wrote is False
    assert "טלפון או אימייל" in reply
    assert crm.written_tabs() == ()


def test_site_refuses_crm_and_whatsapp_without_phone_or_email() -> None:
    crm = FakeContactsCrm()
    settings = Settings().model_copy(update={"whatsapp_click_to_chat": "972501111111"})
    book = site_book()
    book.open("web_siteform1")
    turn = run_site_turn(
        session_id="web_siteform1",
        text="צריכים אתר לעסק",
        settings=settings,
        crm=crm,
        book=book,
    )
    assert turn.crm_wrote is False
    assert turn.whatsapp_url is None
    assert turn.next_action == "ask_contact"
    assert crm.written_tabs() == ()


def test_site_writes_crm_after_phone_or_email_and_offers_whatsapp() -> None:
    crm = FakeContactsCrm()
    settings = Settings().model_copy(update={"whatsapp_click_to_chat": "972501111111"})
    book = site_book()
    book.open("web_siteform2")
    run_site_turn(
        session_id="web_siteform2",
        text="צריכים אתר",
        settings=settings,
        crm=crm,
        book=book,
    )
    turn = run_site_turn(
        session_id="web_siteform2",
        text="0501234567",
        settings=settings,
        crm=crm,
        name="דנה",
        phone="0501234567",
        date="2026-09-02",
        book=book,
    )
    assert turn.crm_wrote is True
    assert turn.whatsapp_url is not None
    assert turn.whatsapp_url.startswith("https://wa.me/972501111111")
    assert turn.next_action == "handoff"
    assert "01 Leads" not in crm.written_tabs()
    blob = " ".join(" ".join(cells) for cells in crm.cells_written)
    assert "lead_" not in blob.lower()
    assert any("תאריך" not in cell for cell in crm.cells_written[0])
    assert "2026-09-02" in crm.cells_written[0]


def test_site_does_not_invent_prices() -> None:
    crm = FakeContactsCrm()
    settings = Settings()
    book = site_book()
    book.open("web_price1")
    turn = run_site_turn(
        session_id="web_price1",
        text="כמה עולה?",
        settings=settings,
        crm=crm,
        book=book,
    )
    assert turn.next_action == "no_price"
    assert "מחיר" in turn.reply
    assert crm.written_tabs() == ()


def test_website_http_session_has_empty_lead_id_and_no_leads_write() -> None:
    sheets = FakeSheetsPort()
    from app.api.deps import get_sheets_port

    app.dependency_overrides[get_sheets_port] = lambda: sheets
    try:
        with TestClient(app) as client:
            created = client.post("/v1/website/sessions")
            assert created.status_code == 200
            body = created.json()
            assert body["lead_id"] == ""
            assert not body["session_id"].startswith("lead_")
            assert body["session_id"].startswith("web_")
            reply = client.post(
                f"/v1/website/sessions/{body['session_id']}/messages",
                json={"text": "hi"},
            )
            assert reply.status_code == 200
            message = reply.json()
            assert message["lead_id"] == ""
            assert not str(message["lead_id"]).startswith("lead_")
            assert message["whatsapp_url"] is None
            assert message["next_action"] in {"ask_need", "ask_contact"}
            config = client.get("/v1/website/config").json()
            assert config["opening"] == site_opening()
            assert config["whatsapp_url"] is None
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)
    assert not any(op[0] == "lead" for op in sheets.owner_operations)
    assert "01 Leads" not in str(sheets.owner_operations)
    assert sheets.rows == {}


def test_website_handoff_requires_phone_or_email() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        denied = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert denied.status_code == 409
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "צריכים אתר", "phone": "0501234567", "name": "דנה"},
        )
        allowed = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert allowed.status_code == 200
        assert allowed.json()["token"]
        assert "lead_" not in allowed.json()["token"]


def test_website_form_fields_are_required_for_crm_path() -> None:
    crm_probe = FakeContactsCrm()
    settings = Settings()
    book = site_book()
    book.open("web_formreq")
    without = run_site_turn(
        session_id="web_formreq",
        text="רק שם דנה",
        settings=settings,
        crm=crm_probe,
        name="דנה",
        book=book,
    )
    assert without.crm_wrote is False
    with_email = run_site_turn(
        session_id="web_formreq",
        text="dana@example.com",
        settings=settings,
        crm=crm_probe,
        email="dana@example.com",
        book=book,
    )
    assert with_email.crm_wrote is True


def test_kill_switch_does_not_block_website(monkeypatch) -> None:
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


async def test_owner_loop_ignores_kill_switch() -> None:
    init_db()
    db = get_session_factory()()
    port = RecordingMessagePort()
    settings = Settings().model_copy(update={"kill_switch": True})
    store = LeadStore(db)
    store.claim_webhook(
        provider="telegram",
        provider_event_id="tg.dude.kill",
        channel="telegram",
        envelope_kind="text",
    )
    try:
        result = await run_owner_loop(
            item={"id": "tg.dude.kill", "from": OWNER_ID, "text": "היי", "chat_id": OWNER_ID},
            store=store,
            port=port,
            settings=settings,
            crm=FakeContactsCrm(),
            owner_ids={OWNER_ID},
        )
    finally:
        db.close()
    assert result.sent is True
    assert port.sent


def test_live_sheets_upsert_lead_is_noop() -> None:
    from app.integrations.sheets import ComposioSheetsPort, LeadMirrorRow

    port = ComposioSheetsPort(api_key="", user_id="", spreadsheet_id="x")
    port.upsert_lead(
        LeadMirrorRow(
            lead_id="lead_should_not_write",
            channel="website",
            stage="open",
            fit="good",
            pain_level=1,
            next_action="ask",
        )
    )

