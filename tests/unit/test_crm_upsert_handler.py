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
from app.domain.approvals import DECISION_APPROVED
from app.integrations import sheets as sheets_integration
from app.integrations.sheets import FakeSheetsPort
from app.services.crm_v2 import CONTACT_FIELDS, CrmService
from app.services.owner_actions import (
    decide_owner_action,
    execute_approved_owner_action_with_adapters,
    read_owner_action,
)
from app.tools.owner.types import ToolSpec
from app.tools.registries import owner_tools as owner_tools_module
from app.tools.registries.owner_tools import ToolContext, execute_tool

OWNER = "12345"
EXPLICIT_INTENT_TEXT = "תרשמי את דנה לאנשי הקשר"
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


def test_valid_owner_crm_upsert_creates_a_pending_proposal_and_writes_nothing() -> None:
    """The C0-verified defect: `_crm_upsert` used to fall through and return None."""
    init_db()
    db = get_session_factory()()
    try:
        phone = "0509990001"
        args = {**ARGS, "phone": phone}
        out = execute_tool(
            "crm_upsert",
            args,
            _ctx(
                db,
                FakeSheetsPort(),
                source_ref="tg.crm.upsert.valid",
                owner_text=EXPLICIT_INTENT_TEXT,
            ),
        )
        assert out.ok is True
        assert out.approval_id
        assert "Nothing was written" in out.text
        row = LeadStore(db).get_approval_by_approval_id(out.approval_id)
        assert row is not None
        envelope = read_owner_action(row)
        assert envelope is not None
        assert envelope["kind"] == "crm.upsert"
        assert envelope["parameters"]["fields"]["phone"] == phone
        assert envelope["parameters"]["fields"]["name"] == "דנה"
        # Identity comes from `snapshot_identity`, never from a model-supplied row number.
        assert "row" not in envelope["parameters"]
        assert CrmService(db).lookup(query=phone) == []
    finally:
        db.close()


def test_crm_upsert_without_phone_or_email_does_not_propose() -> None:
    init_db()
    db = get_session_factory()()
    try:
        args = dict(ARGS)
        args["phone"] = ""
        args["email"] = ""
        out = execute_tool(
            "crm_upsert",
            args,
            _ctx(
                db,
                FakeSheetsPort(),
                source_ref="tg.crm.upsert.nokey",
                owner_text=EXPLICIT_INTENT_TEXT,
            ),
        )
        assert out.ok is True
        assert out.approval_id == ""
        assert "phone or email" in out.text
    finally:
        db.close()


def test_crm_upsert_with_a_lead_id_in_the_fields_is_refused() -> None:
    init_db()
    db = get_session_factory()()
    try:
        phone = "0509990002"
        args = {**ARGS, "phone": phone, "next_step": "lead_42 follow up"}
        out = execute_tool(
            "crm_upsert",
            args,
            _ctx(
                db,
                FakeSheetsPort(),
                source_ref="tg.crm.upsert.leadid",
                owner_text=EXPLICIT_INTENT_TEXT,
            ),
        )
        assert out.ok is False
        assert "lead ids are not used" in out.error
        assert out.approval_id == ""
        assert CrmService(db).lookup(query=phone) == []
    finally:
        db.close()


def test_crm_upsert_replay_with_the_same_source_ref_does_not_duplicate() -> None:
    """`propose_owner_action` binds the proposal id to source_ref; a replay reuses it."""
    init_db()
    db = get_session_factory()()
    try:
        phone = "0509990003"
        args = {**ARGS, "phone": phone}
        ctx = _ctx(
            db,
            FakeSheetsPort(),
            source_ref="tg.crm.upsert.replay",
            owner_text=EXPLICIT_INTENT_TEXT,
        )
        first = execute_tool("crm_upsert", args, ctx)
        db.commit()
        second = execute_tool("crm_upsert", args, ctx)
        db.commit()
        assert first.ok is True and second.ok is True
        assert first.approval_id and first.approval_id == second.approval_id
        assert CrmService(db).lookup(query=phone) == []
    finally:
        db.close()


def test_execute_tool_turns_a_handler_returning_none_into_a_failure_result() -> None:
    """A handler bug (falling through with no return) must not crash the owner turn."""
    init_db()
    db = get_session_factory()()
    try:
        spec = ToolSpec(
            name="_test_forgetful_handler",
            description="test-only handler that forgets to return a ToolResult",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=lambda _ctx, _args: None,
        )
        owner_tools_module._REGISTRY[spec.name] = spec
        try:
            out = execute_tool(
                spec.name,
                {},
                _ctx(db, FakeSheetsPort(), source_ref="tg.none.1", owner_text=""),
            )
        finally:
            del owner_tools_module._REGISTRY[spec.name]
        assert out.ok is False
        assert out.error
    finally:
        db.close()


def test_approving_the_crm_upsert_proposal_writes_exactly_one_contact(monkeypatch) -> None:
    init_db()
    db = get_session_factory()()
    try:
        phone = "0509990004"
        sheets = FakeSheetsPort()
        ctx = _ctx(
            db,
            sheets,
            source_ref="tg.crm.upsert.approve",
            owner_text=EXPLICIT_INTENT_TEXT,
        )
        proposed = execute_tool("crm_upsert", {**ARGS, "phone": phone}, ctx)
        assert proposed.ok is True and proposed.approval_id
        db.commit()

        store = LeadStore(db)
        decision = decide_owner_action(
            store,
            principal=ctx.principal,
            approval_id=proposed.approval_id,
            decision=DECISION_APPROVED,
        )
        assert decision.status == "decided"
        db.commit()

        monkeypatch.setattr(sheets_integration, "build_sheets_port", lambda _settings: sheets)
        settings = Settings(_env_file=None, telegram_owner_user_ids=OWNER)
        row = store.get_approval_by_approval_id(proposed.approval_id)
        assert row is not None

        outcome = execute_approved_owner_action_with_adapters(
            store,
            settings=settings,
            principal=ctx.principal,
            proposal_id=row.resource_id,
        )
        assert outcome.status == "executed"
        contacts = CrmService(db).lookup(query=phone)
        assert len(contacts) == 1
    finally:
        db.close()


def _seed_contact(db, sheets: FakeSheetsPort, fields: dict[str, str]) -> str:
    created = CrmService(db).capture(fields, source_ref=f"seed:{fields['phone']}")
    assert created.contact is not None
    contact = created.contact
    sheets.locked_contacts.append(
        [contact.fields.get(name, "") for name in CONTACT_FIELDS] + [contact.id]
    )
    db.commit()
    return contact.id


def _approve_and_execute(db, ctx: ToolContext, approval_id: str, sheets, monkeypatch):
    store = LeadStore(db)
    decision = decide_owner_action(
        store,
        principal=ctx.principal,
        approval_id=approval_id,
        decision=DECISION_APPROVED,
    )
    assert decision.status == "decided"
    db.commit()
    monkeypatch.setattr(sheets_integration, "build_sheets_port", lambda _settings: sheets)
    row = store.get_approval_by_approval_id(approval_id)
    assert row is not None
    return execute_approved_owner_action_with_adapters(
        store,
        settings=Settings(_env_file=None, telegram_owner_user_ids=OWNER),
        principal=ctx.principal,
        proposal_id=row.resource_id,
    )


def test_crm_upsert_on_an_existing_contact_keeps_its_source_and_summary(monkeypatch) -> None:
    init_db()
    db = get_session_factory()()
    try:
        phone = "0509990005"
        sheets = FakeSheetsPort()
        _seed_contact(
            db,
            sheets,
            {"name": "דנה", "phone": phone, "source": "website", "summary": "סיכום מקורי"},
        )
        ctx = _ctx(db, sheets, source_ref="tg.crm.upsert.existing", owner_text=EXPLICIT_INTENT_TEXT)
        proposed = execute_tool(
            "crm_upsert",
            {**ARGS, "phone": phone, "source": "", "summary": "", "want": "אתר חדש"},
            ctx,
        )
        assert proposed.ok is True and proposed.approval_id
        db.commit()

        outcome = _approve_and_execute(db, ctx, proposed.approval_id, sheets, monkeypatch)

        assert outcome.status == "executed"
        [contact] = CrmService(db).lookup(query=phone)
        assert contact.fields["source"] == "website"
        assert contact.fields["summary"] == "סיכום מקורי"
        assert contact.fields["want"] == "אתר חדש"
    finally:
        db.close()


def test_crm_upsert_for_a_new_contact_fills_source_and_summary_defaults(monkeypatch) -> None:
    init_db()
    db = get_session_factory()()
    try:
        phone = "0509990006"
        sheets = FakeSheetsPort()
        ctx = _ctx(db, sheets, source_ref="tg.crm.upsert.new", owner_text=EXPLICIT_INTENT_TEXT)
        proposed = execute_tool(
            "crm_upsert", {**ARGS, "phone": phone, "source": "", "summary": ""}, ctx
        )
        assert proposed.ok is True and proposed.approval_id
        db.commit()

        outcome = _approve_and_execute(db, ctx, proposed.approval_id, sheets, monkeypatch)

        assert outcome.status == "executed"
        [contact] = CrmService(db).lookup(query=phone)
        assert contact.fields["source"] == "telegram"
        assert contact.fields["summary"] == EXPLICIT_INTENT_TEXT
    finally:
        db.close()


def test_crm_upsert_is_rejected_when_the_contact_changed_before_approval(monkeypatch) -> None:
    init_db()
    db = get_session_factory()()
    try:
        phone = "0509990007"
        sheets = FakeSheetsPort()
        contact_id = _seed_contact(db, sheets, {"name": "דנה", "phone": phone})
        ctx = _ctx(db, sheets, source_ref="tg.crm.upsert.changed", owner_text=EXPLICIT_INTENT_TEXT)
        proposed = execute_tool("crm_upsert", {**ARGS, "phone": phone, "want": "אתר"}, ctx)
        assert proposed.ok is True and proposed.approval_id
        db.commit()

        service = CrmService(db)
        [current] = service.lookup(query=phone)
        service.capture(
            {"phone": phone, "business": "שונה בינתיים"},
            source_ref="seed:changed-after-proposal",
            contact_id=contact_id,
            expected_revision=current.revision,
        )
        db.commit()

        outcome = _approve_and_execute(db, ctx, proposed.approval_id, sheets, monkeypatch)

        assert outcome.status == "target_changed"
        [contact] = CrmService(db).lookup(query=phone)
        assert contact.fields.get("want", "") != "אתר"
    finally:
        db.close()
