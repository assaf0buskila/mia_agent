"""Dude-clone contract: Telegram talk, Contacts/Activity writes, site identify-then-sell."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.graph.owner_agent import SYSTEM_PROMPT
from app.integrations.base import RecordingMessagePort
from app.integrations.sheets import FakeSheetsPort
from app.surfaces.crm import (
    CONTACTS_HEADERS,
    LOCKED_SPREADSHEET_ID,
    FakeContactsCrm,
)
from app.surfaces.owner import run_owner_loop
from app.tools.registries.owner_tools import get_tool

OWNER_ID = "550077"


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


async def test_owner_loop_honors_kill_switch_without_operations() -> None:
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
    result = await run_owner_loop(
        item={"id": "tg.dude.kill", "from": OWNER_ID, "text": "היי", "chat_id": OWNER_ID},
        store=store,
        port=port,
        settings=settings,
        owner_ids={OWNER_ID},
    )
    assert result.sent is False
    assert not port.sent
    assert (
        store.get_webhook(provider="telegram", provider_event_id="tg.dude.kill").status
        == "processed"
    )
    db.close()


def test_empty_env_still_resolves_locked_spreadsheet() -> None:
    settings = Settings(_env_file=None, sheets_spreadsheet_id="")
    assert settings.resolved_sheets_spreadsheet_id() == LOCKED_SPREADSHEET_ID
    assert LOCKED_SPREADSHEET_ID in settings.allowed_sheets_spreadsheet_ids()
    override = Settings(_env_file=None, sheets_spreadsheet_id="custom-sheet-id")
    assert override.resolved_sheets_spreadsheet_id() == "custom-sheet-id"
    assert LOCKED_SPREADSHEET_ID in override.allowed_sheets_spreadsheet_ids()


def test_owner_prompt_keeps_v2_crm_and_approval_boundaries() -> None:
    assert "database is the CRM source of truth" in SYSTEM_PROMPT
    assert "All external writes require an exact immutable approval" in SYSTEM_PROMPT
    assert "explicitly asks to remember" in SYSTEM_PROMPT
    assert "Google Sheet URL" not in SYSTEM_PROMPT
    assert get_tool("crm_search") is not None
    assert get_tool("crm_upsert") is not None
    assert get_tool("gmail_send") is None


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
    assert prefer_crm_tabs(["Contacts", "Activity"]) == ["Contacts", "Activity"]
    assert prefer_crm_tabs(["01 Leads", "KPI", "Contacts"]) == ["Contacts", "KPI"]


def test_live_workbook_writers_target_contacts_and_activity_only() -> None:
    """Live sheet has those two tabs only. Leftover mirrors must not recreate archive tabs."""
    import httpx
    from app.integrations.sheets import (
        CONTACTS_ACTIVITY_TAB,
        CONTACTS_TAB,
        CRM_WORKSPACE_TABS,
        ComposioSheetsPort,
    )

    assert [name for name, _headers in CRM_WORKSPACE_TABS] == ["Contacts", "Activity"]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        calls.append(f"{request.url} {body}")
        return httpx.Response(200, json={"successful": True, "data": {}})

    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="house-entity",
        spreadsheet_id=LOCKED_SPREADSHEET_ID,
        allowed_spreadsheet_ids=frozenset({LOCKED_SPREADSHEET_ID}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    port.write_locked_contact(
        ["דנה", "0501234567", "", "", "", "", "", "אתר", "", "", "", "", "", ""],
        key_column="טלפון",
    )
    port.append_locked_activity(["2026-09-02", "מיה", "telegram", "רשמה", "נרשם"])
    blob = " ".join(calls)
    assert CONTACTS_TAB in blob
    assert CONTACTS_ACTIVITY_TAB in blob
    assert "01 Leads" not in blob
    assert "10 Mia Activity" not in blob
    assert "06 Lead Sources" not in blob
    assert blob.count("GOOGLESHEETS") == 2


def test_owner_prompt_forbids_invented_results() -> None:
    assert "never invent identities, metrics, dates, or results" in SYSTEM_PROMPT
    flags = Settings(_env_file=None)
    assert flags.gmail_send is False
    assert flags.meta_write is False


def test_public_surfaces_do_not_ship_assaf_private_or_my_studio() -> None:
    root = Path(__file__).resolve().parents[2]
    # Documentation is optional in this checkout. The public boundary is the
    # shipped static web surface, plus any Markdown files that still exist.
    public_files = [path for path in root.glob("*.md") if path.is_file()]
    public_files.extend(path for path in (root / "app/web").glob("*.js") if path.is_file())
    living = [path.read_text(encoding="utf-8").lower() for path in public_files]
    widget = (root / "app/web/ask_mia.js").read_text(encoding="utf-8").lower()
    assert public_files and all(path.stat().st_size > 0 for path in public_files)
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    ecs_example = (root / "deploy/ecs-task-definition.example.json").read_text(encoding="utf-8")
    from app.integrations.transcribe import _ASSAFWEB_STT_KEYWORDS

    for blob in (*living, widget):
        assert "mystudio" not in blob
        assert "mystudio.pics" not in blob
        assert "972523393768" not in blob
    assert "MYstudio" not in _ASSAFWEB_STT_KEYWORDS
    assert "972523393768" not in env_example
    assert "972523393768" not in ecs_example
    assert "gmail.com" not in widget
    assert "unread" not in widget
    assert "calendar" not in widget
    assert "lead_" not in widget
    assert "01 leads" not in widget
    website = (root / "app/api/website.py").read_text(encoding="utf-8")
    assert "get_calendar_port" not in website
    assert "get_calendar_booking_port" not in website
    assert "CalendarPort" not in website
