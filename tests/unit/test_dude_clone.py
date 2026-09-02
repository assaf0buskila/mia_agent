"""Dude-clone contract: Telegram talk, Contacts/Activity writes, site identify-then-sell."""

from __future__ import annotations

import pytest
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.graph.owner_agent import SYSTEM_PROMPT
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
from app.surfaces.owner import _asks_for_sheet_url, run_owner_loop, talk_as_dude
from app.surfaces.site import reset_site_book, run_site_turn, site_book, site_opening
from app.tools.registries.owner_tools import ToolContext, execute_tool, get_tool, tool_names
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


def test_empty_env_still_resolves_locked_spreadsheet() -> None:
    settings = Settings(_env_file=None, sheets_spreadsheet_id="")
    assert settings.resolved_sheets_spreadsheet_id() == LOCKED_SPREADSHEET_ID
    assert LOCKED_SPREADSHEET_ID in settings.allowed_sheets_spreadsheet_ids()
    override = Settings(_env_file=None, sheets_spreadsheet_id="custom-sheet-id")
    assert override.resolved_sheets_spreadsheet_id() == "custom-sheet-id"
    assert LOCKED_SPREADSHEET_ID in override.allowed_sheets_spreadsheet_ids()


def test_owner_prompt_never_asks_for_sheet_url() -> None:
    assert "Never ask him for a Google Sheet URL" in SYSTEM_PROMPT
    assert "docs.google.com/spreadsheets" not in SYSTEM_PROMPT
    assert "limited access" not in SYSTEM_PROMPT.lower()
    assert "not the source of truth" not in SYSTEM_PROMPT
    assert "crm_search" in SYSTEM_PROMPT
    assert "crm_upsert" in SYSTEM_PROMPT
    assert "No unsolicited Gmail" in SYSTEM_PROMPT
    assert "01 Leads" in SYSTEM_PROMPT
    assert get_tool("crm_search") is not None
    assert get_tool("crm_upsert") is not None
    assert get_tool("gmail_send") is None
    assert "paste" not in get_tool("crm_search").description.lower()
    assert "paste" not in get_tool("crm_upsert").description.lower()
    assert "docs.google.com" not in get_tool("crm_upsert").description.lower()


def test_owner_turn_does_not_ask_for_spreadsheet_url() -> None:
    reply, wrote = talk_as_dude(text="תראי לי את ה-CRM", crm=FakeContactsCrm())
    assert wrote is False
    assert "http" not in reply.lower()
    assert "spreadsheet" not in reply.lower()
    assert "docs.google.com" not in reply
    assert _asks_for_sheet_url(
        "I can only access a sheet if you paste the Google Sheet URL"
    )
    assert not _asks_for_sheet_url("רשמתי ב-Contacts.")


def test_crm_tools_use_locked_id_and_never_write_leads_tab() -> None:
    init_db()
    db = get_session_factory()()
    sheets = FakeSheetsPort()
    try:
        ctx = ToolContext(
            principal=Principal.owner(source="telegram", actor_id=OWNER_ID),
            store=LeadStore(db),
            brain=BrainStore(db),
            settings=Settings(_env_file=None, sheets_spreadsheet_id=""),
            embedding_port=FakeEmbeddingPort(),
            sheets=sheets,
            owner_text="תרשמי את דנה 0501234567",
            source_ref="tg.crm.1",
        )
        wrote = execute_tool(
            "crm_upsert",
            {
                "name": "דנה",
                "phone": "0501234567",
                "email": None,
                "date": "2026-09-02",
                "business": None,
                "source": "telegram",
                "language": "he",
                "want": "אתר",
                "status": None,
                "summary": "שיחה",
                "next_step": None,
            },
            ctx,
        )
        assert wrote.ok is True
        assert LOCKED_SPREADSHEET_ID in wrote.text
        assert "01 Leads" not in wrote.text
        assert "lead_" not in wrote.text.lower()
        assert all(op[1] == LOCKED_SPREADSHEET_ID for op in sheets.owner_operations)
        assert all(op[0] != "lead" for op in sheets.owner_operations)
        assert "01 Leads" not in str(sheets.owner_operations)
        blob = " ".join(" ".join(row) for row in sheets.locked_contacts)
        assert "lead_" not in blob.lower()
        found = execute_tool("crm_search", {"query": "דנה"}, ctx)
        assert found.ok is True
        assert "דנה" in found.text
        assert "lead_" not in found.text.lower()
        assert "01 Leads" not in found.text
        refused = execute_tool(
            "crm_upsert",
            {
                "name": "רק שם",
                "phone": None,
                "email": None,
                "date": None,
                "business": None,
                "source": None,
                "language": None,
                "want": None,
                "status": None,
                "summary": None,
                "next_step": None,
            },
            ctx,
        )
        assert refused.ok is True
        assert "phone or email" in refused.text.lower()
    finally:
        db.close()
    assert "crm_search" in tool_names()
    assert "crm_upsert" in tool_names()


def test_sheets_read_without_id_uses_locked_workbook() -> None:
    from app.capabilities.sheets import sheets_read

    sheets = FakeSheetsPort()
    sheets.owner_values[(LOCKED_SPREADSHEET_ID, "Contacts!A1:N20")] = [["שם", "טלפון"]]
    allowed = Settings(_env_file=None, sheets_spreadsheet_id="").allowed_sheets_spreadsheet_ids()
    out = sheets_read(
        sheets,
        {"spreadsheet_id": "", "range": None},
        allowed_spreadsheet_ids=allowed,
    )
    assert out["rows"] == [["שם", "טלפון"]]


def test_crm_range_is_contacts_and_rejects_01_leads() -> None:
    from app.capabilities.sheets import sheets_list_tabs, sheets_read
    from app.core.errors import InvalidArguments
    from app.surfaces.crm import DEFAULT_CONTACTS_READ_RANGE, prefer_crm_tabs

    assert DEFAULT_CONTACTS_READ_RANGE.startswith("Contacts!")
    assert "01 Leads" not in DEFAULT_CONTACTS_READ_RANGE
    sheets = FakeSheetsPort()
    allowed = Settings(_env_file=None, sheets_spreadsheet_id="").allowed_sheets_spreadsheet_ids()
    with pytest.raises(InvalidArguments, match="01 Leads"):
        sheets_read(
            sheets,
            {"spreadsheet_id": LOCKED_SPREADSHEET_ID, "range": "01 Leads!A1:F20"},
            allowed_spreadsheet_ids=allowed,
        )
    sheets.sheet_names[LOCKED_SPREADSHEET_ID] = [
        "01 Leads",
        "Activity",
        "Contacts",
        "10 Mia Activity",
    ]
    listed = sheets_list_tabs(
        sheets,
        {"spreadsheet_id": ""},
        allowed_spreadsheet_ids=allowed,
    )
    assert listed["tabs"] == ["Contacts", "Activity"]
    assert "01 Leads" not in listed["tabs"]
    assert prefer_crm_tabs(["01 Leads", "KPI", "Contacts"]) == ["Contacts", "KPI"]


def test_owner_prompt_forbids_invented_counts() -> None:
    assert "Never invent metrics, counts, or pipeline numbers" in SYSTEM_PROMPT
    assert "say you do not know" in SYSTEM_PROMPT
    assert "Do not say they are disconnected" in SYSTEM_PROMPT
    assert "Contacts!A1:N20" in SYSTEM_PROMPT
    assert "No Lead ID" in SYSTEM_PROMPT

