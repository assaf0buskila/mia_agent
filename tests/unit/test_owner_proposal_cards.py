"""Snapshot-style coverage for `app.domain.owner.proposal_cards`.

Owner tools used to hand back thin acknowledgement prose and the turn glued its
approve/reject buttons directly onto that sentence -- the owner could tap approve
without ever seeing what he was approving. These tests pin the replacement: one
Hebrew, escaped, secret-free card per proposal kind, built straight from the exact
envelope `read_owner_action` already validated. `render_owner_proposal_card` takes
that envelope directly, so these are plain unit tests with no database involved.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.domain.approvals import ACTION_GMAIL_SEND, ACTION_WEBSITE_EDIT
from app.domain.owner.proposal_cards import (
    render_owner_approval_card,
    render_owner_proposal_card,
)

_SECRET_CONNECTION_ID = "conn_top_secret_12345"
_SECRET_ACCOUNT_HASH = "a" * 64
_SECRET_STATE_HASH = "b" * 64
_SECRET_INPUT_SCHEMA = {"type": "object", "properties": {"leak": {"type": "string"}}}


def _connection_blob() -> dict:
    return {
        "connected_account_id": _SECRET_CONNECTION_ID,
        "toolkit": "GMAIL",
    }


def test_gmail_create_draft_card_shows_full_body_and_escapes_html() -> None:
    envelope = {
        "kind": "gmail.create_draft",
        "version": 1,
        "parameters": {
            "to": "dana@example.com",
            "subject": "Q3 <update> & recap",
            "body": "Hi Dana,\n<script>alert(1)</script>\nSee you soon.",
        },
        "target": {"recipient": "dana@example.com", "provider_binding": {}},
    }
    card = render_owner_proposal_card(envelope)
    assert "לאישור" in card
    assert "עדיין לא בוצע" in card
    assert "dana@example.com" in card
    assert "<script>" not in card
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in card
    assert "&lt;update&gt; &amp; recap" in card
    assert "לא יישלח מייל" in card


def test_composio_gmail_send_draft_card_shows_identity_never_secrets() -> None:
    envelope = {
        "kind": "composio.write",
        "version": 1,
        "parameters": {
            "slug": "GMAIL_SEND_DRAFT",
            "toolkit": "GMAIL",
            "arguments": {"draft_id": "draft-abc-123"},
        },
        "target": {
            "slug": "GMAIL_SEND_DRAFT",
            "toolkit": "GMAIL",
            "input_schema": _SECRET_INPUT_SCHEMA,
            "risk": "R3",
            "account_hash": _SECRET_ACCOUNT_HASH,
            "connection": _connection_blob(),
            "resource": {
                "reader_slug": "GMAIL_GET_DRAFT",
                "identity": {"draft_id": "draft-abc-123"},
                "state_hash": _SECRET_STATE_HASH,
            },
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "לאישור" in card
    assert "המייל יישלח" in card
    assert "draft-abc-123" in card
    assert _SECRET_CONNECTION_ID not in card
    assert _SECRET_ACCOUNT_HASH not in card
    assert _SECRET_STATE_HASH not in card
    assert "leak" not in card


def test_composio_linkedin_card_shows_full_post_text_and_visibility() -> None:
    long_post = "פוסט חדש על מיה. " * 40  # well past the generic bounding limit
    envelope = {
        "kind": "composio.write",
        "version": 1,
        "parameters": {
            "slug": "LINKEDIN_CREATE_LINKED_IN_POST",
            "toolkit": "LINKEDIN",
            "arguments": {"commentary": long_post, "visibility": "PUBLIC"},
        },
        "target": {
            "slug": "LINKEDIN_CREATE_LINKED_IN_POST",
            "toolkit": "LINKEDIN",
            "input_schema": _SECRET_INPUT_SCHEMA,
            "risk": "R3",
            "account_hash": _SECRET_ACCOUNT_HASH,
            "connection": _connection_blob(),
        },
    }
    card = render_owner_proposal_card(envelope)
    assert long_post.strip() in card
    assert "PUBLIC" in card
    assert "…" not in card  # never truncated for LinkedIn
    assert _SECRET_CONNECTION_ID not in card
    assert _SECRET_ACCOUNT_HASH not in card


def test_composio_other_tool_bounds_arguments_and_hides_secrets() -> None:
    huge_value = "x" * 1000
    envelope = {
        "kind": "composio.write",
        "version": 1,
        "parameters": {
            "slug": "NOTION_CREATE_PAGE",
            "toolkit": "NOTION",
            "arguments": {"title": "New page", "body": huge_value},
        },
        "target": {
            "slug": "NOTION_CREATE_PAGE",
            "toolkit": "NOTION",
            "input_schema": _SECRET_INPUT_SCHEMA,
            "risk": "R3",
            "account_hash": _SECRET_ACCOUNT_HASH,
            "connection": _connection_blob(),
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "NOTION_CREATE_PAGE" in card
    assert "New page" in card
    assert huge_value not in card  # bounded, not shown in full
    assert "…" in card
    assert _SECRET_CONNECTION_ID not in card
    assert _SECRET_ACCOUNT_HASH not in card
    assert "leak" not in card


def test_calendar_create_card_shows_local_time_duration_and_no_invite_note() -> None:
    envelope = {
        "kind": "calendar.create",
        "version": 1,
        "parameters": {
            "title": "פגישת ייעוץ",
            "start": "2026-09-20T10:00:00+03:00",
            "end": "2026-09-20T10:45:00+03:00",
            "timezone": "Asia/Jerusalem",
            "location": "זום",
        },
        "target": {"free": True, "start": "", "end": "", "provider_binding": {}},
    }
    card = render_owner_proposal_card(envelope)
    assert "פגישת ייעוץ" in card
    assert "זום" in card
    assert "45 דקות" in card
    assert "לא נשלחות הזמנות" in card
    assert "10:00" in card
    assert "20 בספטמבר" in card


def test_calendar_reschedule_card_shows_old_and_new_local_time() -> None:
    envelope = {
        "kind": "calendar.reschedule",
        "version": 1,
        "parameters": {
            "event_id": "evt_1",
            "start": "2026-09-21T12:00:00+03:00",
            "end": "2026-09-21T12:30:00+03:00",
            "timezone": "Asia/Jerusalem",
        },
        "target": {
            "event_id": "evt_1",
            "start": "2026-09-20T09:00:00+03:00",
            "end": "2026-09-20T09:30:00+03:00",
            "destination_free": True,
            "provider_binding": {},
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "לאישור: העברת אירוע ביומן" in card
    # Old and new dates are different Hebrew days -- both must be visible.
    assert "20" in card
    assert "21" in card


def test_crm_upsert_new_contact_lists_fields_without_arrows() -> None:
    envelope = {
        "kind": "crm.upsert",
        "version": 1,
        "parameters": {
            "fields": {"name": "דנה כהן", "phone": "0501234567", "email": ""},
            "contact_id": "",
            "expected_revision": 0,
        },
        "target": {
            "contact_id": "",
            "revision": 0,
            "snapshot_hash": "h1",
            "fields": {"phone": "0501234567"},
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "איש קשר חדש" in card
    assert "דנה כהן" in card
    assert "0501234567" in card
    assert "→" not in card


def test_crm_upsert_existing_contact_shows_before_after_only_for_changes() -> None:
    envelope = {
        "kind": "crm.upsert",
        "version": 1,
        "parameters": {
            "fields": {
                "name": "דנה לוי",
                "phone": "0501234567",
                "email": "dana@new.com",
            },
            "contact_id": "c_1",
            "expected_revision": 3,
        },
        "target": {
            "contact_id": "c_1",
            "revision": 3,
            "snapshot_hash": "h2",
            "fields": {"name": "דנה כהן", "phone": "0501234567", "email": ""},
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "עדכון איש קשר" in card
    assert "c_1" in card
    # name changed: both values shown with an arrow.
    assert "דנה כהן" in card and "דנה לוי" in card and "→" in card
    # phone unchanged: never mentioned at all.
    assert "0501234567" not in card
    # email newly filled in: shown without an arrow (nothing to diff against).
    assert "dana@new.com" in card


def test_crm_activity_card_shows_contact_kind_and_full_summary() -> None:
    envelope = {
        "kind": "crm.activity",
        "version": 1,
        "parameters": {
            "contact_id": "c_2",
            "kind": "call",
            "summary": "דיברנו על החבילה השנתית ותיאמנו פולואפ.",
        },
        "target": {
            "contact_id": "c_2",
            "revision": 1,
            "snapshot_hash": "h3",
            "fields": {"name": "רון פרידמן"},
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "רון פרידמן" in card
    assert "call" in card
    assert "דיברנו על החבילה השנתית ותיאמנו פולואפ." in card


def test_crm_resolve_conflict_card_shows_chosen_value() -> None:
    envelope = {
        "kind": "crm.resolve_conflict",
        "version": 1,
        "parameters": {
            "conflict_id": "conf_1",
            "resolution": "sheet",
            "value": None,
            "contact_id": "c_3",
            "expected_revision": 2,
        },
        "target": {
            "conflict": {
                "id": "conf_1",
                "contact_id": "c_3",
                "issue_type": "field_conflict",
                "field_name": "business",
                "base_value": "",
                "database_value": "עסק א",
                "sheet_value": "עסק ב",
                "status": "open",
            },
            "contact": {
                "contact_id": "c_3",
                "revision": 2,
                "snapshot_hash": "h4",
                "fields": {"name": "לקוח קיים"},
            },
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "לקוח קיים" in card
    assert "עסק א" in card
    assert "עסק ב" in card
    assert "מהגיליון" in card


def test_sheets_update_card_shows_range_and_bounds_long_cells() -> None:
    huge_cell = "y" * 1000
    envelope = {
        "kind": "sheets.update",
        "version": 1,
        "parameters": {
            "spreadsheet_id": "sheet_123",
            "range": "Contacts!A2:B2",
            "values": [["דנה", huge_cell]],
        },
        "target": {
            "spreadsheet_id": "sheet_123",
            "range": "Contacts!A2:B2",
            "values": [["", ""]],
            "provider_binding": {},
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "Contacts!A2:B2" in card
    assert "דנה" in card
    assert huge_cell not in card
    assert "…" in card


def test_unknown_kind_renders_generic_card_without_raw_json() -> None:
    envelope = {
        "kind": "some.future.kind",
        "version": 1,
        "parameters": {"secret_looking_field": "should never leak", "x": 1},
        "target": {"connection": _connection_blob()},
    }
    card = render_owner_proposal_card(envelope)
    assert "לאישור" in card
    assert "עדיין לא בוצע" in card
    assert "should never leak" not in card
    assert _SECRET_CONNECTION_ID not in card
    assert "{" not in card
    assert "}" not in card


def test_malformed_envelope_never_crashes_and_never_leaks() -> None:
    card = render_owner_proposal_card(
        {"kind": "crm.upsert", "parameters": "not-a-dict", "target": None}
    )
    assert "לאישור" in card
    assert "עדיין לא בוצע" in card


def test_render_owner_approval_card_handles_missing_row() -> None:
    card = render_owner_approval_card(None)
    assert "לאישור" in card
    assert "עדיין לא בוצע" in card


def test_render_owner_approval_card_renders_a_legacy_row_without_full_envelope() -> None:
    """A pre-v2 approval row stores only an identity blob, never a full envelope."""
    row = SimpleNamespace(
        action=ACTION_GMAIL_SEND,
        resource_id="draft_legacy_1",
        resource_type="gmail",
        lead_id=None,
        risk="R3",
        proposed_parameters='{"action":"gmail_send","resource_id":"draft_legacy_1"}',
    )
    card = render_owner_approval_card(row)
    assert "לאישור" in card
    assert "שליחת מייל" in card
    assert "draft_legacy_1" in card
    assert "עדיין לא בוצע" in card


def test_render_owner_approval_card_renders_legacy_website_edit_before_after() -> None:
    row = SimpleNamespace(
        action=ACTION_WEBSITE_EDIT,
        resource_id="assafweb-home",
        resource_type="website",
        lead_id=None,
        risk="R3",
        proposed_parameters='{"before": "מחיר ישן", "after": "מחיר <new>"}',
    )
    card = render_owner_approval_card(row)
    assert "שינוי באתר" in card
    assert "מחיר ישן" in card
    assert "מחיר &lt;new&gt;" in card


def test_crm_upsert_never_shows_system_owned_created_updated_fields() -> None:
    """`created`/`updated` are computed from row timestamps, never from a proposal --
    mutation coverage for the `SYSTEM_OWNED_FIELDS` skip in `_card_crm_upsert`: if
    that `continue` were removed, the injected 2099 values below would leak onto
    the card under a raw "created"/"updated" label (neither word appears anywhere
    else in this card's Hebrew template).
    """
    envelope = {
        "kind": "crm.upsert",
        "version": 1,
        "parameters": {
            "fields": {
                "name": "עדי",
                "created": "2099-12-31T00:00:00Z",
                "updated": "2099-12-31T00:00:00Z",
            },
            "contact_id": "c_9",
            "expected_revision": 1,
        },
        "target": {
            "contact_id": "c_9",
            "revision": 1,
            "snapshot_hash": "hx",
            "fields": {"name": "", "created": "2000-01-01", "updated": "2000-01-01"},
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "עדי" in card
    assert "2099-12-31" not in card
    assert "2000-01-01" not in card
    assert "created" not in card
    assert "updated" not in card
