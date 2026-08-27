"""Owner Telegram lead cards are labeled lines, and lead upserts hit Sheets."""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.hot_handoff import format_hot_brief
from app.domain.memory import ConversationTurn
from app.domain.owner_lead_card import format_owner_lead_card, last_message_short
from app.domain.sales import NextAction, PainLevel, SalesState
from app.domain.website_handoff_brief import (
    format_website_human_handoff_brief,
    format_website_whatsapp_brief,
)
from app.integrations.sheets import (
    DisabledSheetsPort,
    FakeSheetsPort,
    lead_mirror_row_from_state,
    maybe_mirror_lead_snapshot,
    mirror_lead,
)
from app.services.notifications import render_conversation_summary


def _sales(**overrides: object) -> SalesState:
    payload: dict[str, object] = {
        "lead_id": "lead_card12abcd",
        "workflow_known": True,
        "manual_step_known": True,
        "pain_level": PainLevel.P2,
        "whatsapp_handoff_offered": True,
        "headline": "מוכר נעליים",
    }
    payload.update(overrides)
    return SalesState.model_validate(payload)


def _turns() -> list[ConversationTurn]:
    return [
        ConversationTurn(role="prospect", text="אני מוכר נעליים ומזין מלאי ידנית"),
        ConversationTurn(role="mia", text="כמה זמן זה לוקח?"),
        ConversationTurn(role="prospect", text="שעתיים כל בוקר"),
    ]


def _assert_structured_card(text: str, *, lead_id: str) -> None:
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) >= 5, text
    assert "\n" in text
    assert lead_id in text
    assert "ליד" in text
    assert "פעולה הבאה" in text
    assert "וואטסאפ הוצע" in text
    # A wall of prose would be one long line. This is labeled rows.
    assert max(len(line) for line in lines) < 280, text


def test_owner_lead_card_is_labeled_lines_not_a_blob() -> None:
    text = format_owner_lead_card(
        title="שיחה מהאתר הסתיימה",
        lead_id="lead_card12abcd",
        stage="open",
        last_said="שעתיים כל בוקר",
        next_action="handoff",
        whatsapp_offered=True,
    )
    _assert_structured_card(text, lead_id="lead_card12abcd")
    assert "שלב" in text
    assert "מה אמרו" in text
    assert "כן" in text
    assert "<code>lead_card12abcd</code>" in text


def test_conversation_summary_is_structured_hebrew() -> None:
    text = render_conversation_summary(
        {
            "lead_id": "lead_sum12abcd",
            "name": "דנה",
            "stage": "open",
            "last_message_short": "צריכה לעדכן מלאי",
            "next_action": "offer_meeting",
            "whatsapp_offered": "כן",
            "conversation_id": "web_1",
            "budget": None,
        }
    )
    _assert_structured_card(text, lead_id="lead_sum12abcd")
    assert "שיחה מהאתר הסתיימה" in text
    assert "דנה" in text
    assert "צריכה לעדכן מלאי" in text
    assert "Budget" not in text
    assert "Name:" not in text
    assert "New website conversation" not in text
    assert "web_1" in text


def test_whatsapp_and_human_handoff_cards_share_the_structure() -> None:
    sales = _sales()
    turns = _turns()
    whatsapp = format_website_whatsapp_brief(
        lead_id=sales.lead_id, sales=sales, turns=turns, stage="open"
    )
    human = format_website_human_handoff_brief(
        lead_id=sales.lead_id, sales=sales, turns=turns, stage="open"
    )
    _assert_structured_card(whatsapp, lead_id=sales.lead_id)
    _assert_structured_card(human, lead_id=sales.lead_id)
    assert "השורה שלך:" in whatsapp
    assert "צריך אותך" in human
    assert "מיה לא תענה שם" not in human


def test_hot_brief_is_structured() -> None:
    text = format_hot_brief(
        lead_id="lead_hot12abcd",
        sales=_sales(lead_id="lead_hot12abcd"),
        want="רוצה לדבר עם אסף",
    )
    _assert_structured_card(text, lead_id="lead_hot12abcd")
    assert "ליד חם" in text


def test_lead_upsert_hits_fake_sheets_port_with_conversation_fields() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_sheet_card_1"
        )
        sales = _sales(lead_id=lead_id)
        store.save_sales(sales)
        db.commit()
        sheets = FakeSheetsPort()
        written = maybe_mirror_lead_snapshot(
            sheets=sheets,
            store=store,
            lead_id=lead_id,
            channel="website",
            next_action=NextAction.OFFER_WHATSAPP.value,
            kill_switch=False,
        )
        assert written is True
        row = sheets.rows[lead_id]
        assert row.channel == "website"
        assert row.stage == "open"
        assert row.whatsapp_offered == "כן"
        assert row.disqualified == "לא"
        assert row.discovery_summary
        assert "Timestamp" not in row.lead_id
        assert row.timestamp
        assert "@" not in row.discovery_summary
        assert "@" not in row.last_message_short
    finally:
        db.close()


def test_lead_mirror_row_records_last_message_and_skips_pii_columns() -> None:
    row = lead_mirror_row_from_state(
        lead_id="lead_msg12abcd",
        channel="website",
        stage="open",
        sales=_sales(lead_id="lead_msg12abcd"),
        next_action="quantify",
        turns=_turns(),
        now=datetime(2026, 8, 27, 11, 0, tzinfo=UTC),
    )
    assert last_message_short(_turns()) in row.last_message_short
    assert row.whatsapp_offered == "כן"
    assert row.disqualified == "לא"
    dumped = row.model_dump()
    assert "phone" not in dumped
    assert "email" not in dumped
    assert "contact" not in dumped


def test_disabled_sheets_port_does_not_crash_a_lead_upsert() -> None:
    written = mirror_lead(
        sheets=DisabledSheetsPort(),
        row=lead_mirror_row_from_state(
            lead_id="lead_off12abcd",
            channel="website",
            stage="open",
            sales=_sales(lead_id="lead_off12abcd"),
            next_action="understand_workflow",
        ),
        kill_switch=False,
    )
    assert written is True
