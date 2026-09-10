"""Current CRM owner writes are durable proposals, not direct Sheet mutations.

The former tests exercised the deleted Contacts adapter and its event claim. v2 keeps
the durable CRM service as the record of truth and routes owner initiated activity
through an exact, expiring approval bound to the imported contact revision.
"""

from __future__ import annotations

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.integrations.sheets import FakeSheetsPort
from app.services.crm_v2 import CONTACT_FIELDS, CrmService
from app.tools.registries.owner_tools import ToolContext, execute_tool

OWNER = "12345"
ARGS = {
    "name": "דנה",
    "phone": "0501234567",
    "want": "ניהול תורים",
    "email": "",
    "date": "",
    "business": "",
    "source": "telegram",
    "language": "",
    "status": "",
    "summary": "",
    "next_step": "",
}


def _ctx(db, sheets, *, source_ref: str, owner_text: str) -> ToolContext:
    return ToolContext(
        principal=Principal.owner(source="telegram", actor_id=OWNER),
        store=LeadStore(db),
        brain=BrainStore(db),
        settings=Settings(_env_file=None, sheets_spreadsheet_id=""),
        embedding_port=FakeEmbeddingPort(),
        sheets=sheets,
        owner_text=owner_text,
        source_ref=source_ref,
    )


def _seed_current_contact(db, sheets: FakeSheetsPort) -> str:
    created = CrmService(db).capture(
        {"name": "דנה", "phone": "0501234567", "want": "ניהול תורים"},
        source_ref="seed:crm-owner-test",
    )
    assert created.contact is not None
    contact = created.contact
    sheets.locked_contacts.append(
        [contact.fields.get(name, "") for name in CONTACT_FIELDS] + [contact.id]
    )
    db.commit()
    return contact.id


def test_legacy_direct_crm_upsert_requires_current_explicit_owner_intent() -> None:
    init_db()
    db = get_session_factory()()
    try:
        out = execute_tool(
            "crm_upsert",
            dict(ARGS),
            _ctx(
                db,
                FakeSheetsPort(),
                source_ref="tg.crm.guard",
                owner_text="תראי לי את אנשי הקשר",
            ),
        )
        assert out.ok is False
        assert "explicit affirmative request" in out.error
    finally:
        db.close()


def test_crm_activity_is_an_exact_approval_and_does_not_write_before_decision() -> None:
    init_db()
    db = get_session_factory()()
    try:
        sheets = FakeSheetsPort()
        contact_id = _seed_current_contact(db, sheets)
        out = execute_tool(
            "crm_record_activity",
            {
                "contact_id": contact_id,
                "kind": "follow_up",
                "summary": "הלקוחה ביקשה לחזור אליה מחר",
            },
            _ctx(
                db,
                sheets,
                source_ref="tg.crm.activity.1",
                owner_text="רשמי פעילות לאיש הקשר",
            ),
        )
        assert out.ok is True
        assert out.approval_id
        assert "Nothing was written" in out.text
        assert sheets.locked_activity == []
        row = LeadStore(db).get_approval_by_approval_id(out.approval_id)
        assert row is not None
        assert '"contact_id"' in row.proposed_parameters
    finally:
        db.close()


def test_current_crm_contact_capture_is_idempotent_in_the_durable_database() -> None:
    init_db()
    db = get_session_factory()()
    try:
        service = CrmService(db)
        first = service.capture(
            {"name": "דנה", "phone": "0501234567"},
            source_ref="tg.crm.capture.1",
        )
        db.commit()
        second = service.capture(
            {"name": "דנה", "phone": "0501234567"},
            source_ref="tg.crm.capture.1",
        )
        db.commit()
        assert first.contact is not None and second.contact is not None
        assert second.contact.id == first.contact.id
    finally:
        db.close()
