"""Shared V2-28 test fixtures: a FakeLedger workspace, a stand-in for V2-19's ``authorize``
module, a SQLite store with production session settings, and ledger-bound owner actions.

Nothing here touches a network, Telegram, Google or a credential. The V2-16f fake is used
unchanged; ``SpyLedger`` only records which ledger methods ran and with which channel.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.capabilities.types import Principal
from app.core.config import MiaEnv, Settings
from app.db.base import Base
from app.db.models import (
    ApprovalRow,
    CrmContactConversationRow,
    CrmContactRow,
    CrmOutboxRow,
    IdempotencyRow,
)
from app.db.session import make_engine
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED, DECISION_REJECTED
from app.services.os_ledger import api
from app.services.owner_actions import OwnerActionProposal, propose_owner_action
from sqlalchemy.orm import sessionmaker

from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger import FakeLedger

OWNER = "123456"
WORKSPACE = "test"
AUTHORIZE_MODULE = "app.services.os_ledger.authorize"
NOW = datetime.now(UTC).isoformat()


def pg_settings(**changes: Any) -> Settings:
    """Settings with the ledger backend active (V2-17 factory says ``postgres``)."""
    values: dict[str, Any] = dict(
        _env_file=None,
        env=MiaEnv.TEST,
        telegram_owner_user_ids=OWNER,
        owner_os_enabled=True,
        assaf_os_enabled=True,
        assaf_os_script_url="https://script.google.com/macros/s/" + "x" * 24 + "/exec",
        assaf_os_workbook_id="workbook_12345678901234567890",
        assaf_os_workspace_id=WORKSPACE,
        assaf_os_hmac_secret="assaf-os-test-secret-at-least-32-bytes",
        assaf_os_mode="TEST",
        assaf_os_backend="postgres",
        assaf_os_ledger_enabled=True,
        assaf_os_authority_epoch=1,
    )
    values.update(changes)
    return Settings(**values)


def make_store_factory(tmp_path) -> sessionmaker:
    """A file-backed SQLite store with the production session settings (autoflush off).

    A file database (not ``:memory:``) gives every session its own connection, so a second
    session sees only committed rows. That is what proves "the CAS commits first".
    """
    engine = make_engine(f"sqlite:///{tmp_path / 'v2-28.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class SpyLedger(FakeLedger):
    """The V2-16f FakeLedger, unchanged, plus a log of the calls this chunk cares about."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[tuple[str, str, str]] = []
        self.before_decide: Callable[[], None] | None = None
        self.before_read: Callable[[], None] | None = None

    def decide(self, ctx, proposal_id, decision_id, decision):
        self.calls.append(("decide", ctx.channel, decision_id))
        if self.before_decide is not None:
            hook, self.before_decide = self.before_decide, None
            hook()
        return super().decide(ctx, proposal_id, decision_id, decision)

    def propose(self, ctx, req):
        self.calls.append(("propose", ctx.channel, req.request_id))
        return super().propose(ctx, req)

    def _reading(self) -> None:
        if self.before_read is not None:
            hook, self.before_read = self.before_read, None
            hook()

    def receipt(self, ctx, request_id):
        self.calls.append(("receipt", ctx.channel, request_id))
        self._reading()
        return super().receipt(ctx, request_id)

    def creation_request(self, ctx, request_id):
        self.calls.append(("creation_request", ctx.channel, request_id))
        self._reading()
        return super().creation_request(ctx, request_id)

    def page(self, ctx, entity, *, after, limit):
        self.calls.append(("page", ctx.channel, entity))
        return super().page(ctx, entity, after=after, limit=limit)

    def named(self, method: str) -> list[tuple[str, str, str]]:
        return [call for call in self.calls if call[0] == method]


class FakeAuthorize(types.ModuleType):
    """Stand-in for V2-19's ``app.services.os_ledger.authorize`` (not built yet).

    It re-checks the numeric owner like the real constructors, and ``from_telegram_decision``
    refuses unless the ApprovalRow already carries a decision. With ``committed_probe`` set it also
    refuses unless a *fresh* session sees that decision, i.e. the CAS was committed first.
    """

    def __init__(self) -> None:
        super().__init__(AUTHORIZE_MODULE)
        self.calls: list[tuple[str, str]] = []
        self.committed_probe: Callable[[Any], str] | None = None

    @staticmethod
    def _owner(value: object, settings: Settings) -> int:
        owner = str(value or "")
        if not owner.isdigit() or owner not in settings.telegram_owner_user_id_set():
            raise api.LedgerError("owner_auth_required")
        return int(owner)

    def _ctx(self, channel: str, owner: int, settings: Settings):
        return make_ctx(
            channel,
            owner=owner,
            workspace=settings.assaf_os_workspace_id,
            epoch=settings.assaf_os_authority_epoch,
        )

    def from_mia_tool(self, principal, settings):
        owner = self._owner(getattr(principal, "actor_id", ""), settings)
        self.calls.append(("from_mia_tool", "MIA"))
        return self._ctx("MIA", owner, settings)

    def from_telegram_decision(self, approval, settings):
        owner = self._owner(getattr(approval, "actor_id", ""), settings)
        decision = getattr(approval, "decision", "")
        if decision not in {DECISION_APPROVED, DECISION_REJECTED}:
            raise api.LedgerError("owner_approval_required")
        if self.committed_probe is not None and self.committed_probe(approval) != decision:
            raise AssertionError("approval_cas_not_committed_before_decide")
        self.calls.append(("from_telegram_decision", decision))
        return self._ctx("TELEGRAM_APPROVAL", owner, settings)


class LedgerWorld:
    """One active FakeLedger workspace whose knobs follow ``world.settings``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or pg_settings()
        self.ledger = SpyLedger(
            clock=lambda: datetime.now(UTC),
            settings_epoch=lambda: self.settings.assaf_os_authority_epoch,
            backend=lambda: self.settings.assaf_os_backend,
            kill_switch=lambda: self.settings.kill_switch,
            freeze=lambda: self.settings.assaf_os_freeze,
            enabled=lambda: self.settings.assaf_os_ledger_enabled,
        )
        self.ledger.ensure_workspace(WORKSPACE, OWNER, 1)
        self.ledger.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER)
        self.authorize = FakeAuthorize()

    def install(self, monkeypatch, *, authorize: bool = True) -> LedgerWorld:
        monkeypatch.setattr(api, "build_ledger", lambda settings: self.ledger)
        if authorize:
            monkeypatch.setitem(sys.modules, AUTHORIZE_MODULE, self.authorize)
        else:
            monkeypatch.delitem(sys.modules, AUTHORIZE_MODULE, raising=False)
        return self

    def ctx(self, channel: str):
        return make_ctx(channel, owner=int(OWNER), workspace=WORKSPACE, epoch=1)

    def propose_lead(self, *, source_id: str = "probe", **values: Any) -> api.ProposalRow:
        lead = {
            "name": "דנה",
            "phone": "+972502222222",
            "email": "visitor@example.com",
            "path": "לימוד",
            "status": "חדש",
            "source_note": "AssafWeb",
        }
        lead.update(values)
        return self.ledger.propose(
            self.ctx("MIA"),
            api.ProposeInput(
                request_id=api.mia_request_uuid(OWNER, WORKSPACE, source_id),
                entity="leads",
                record_id=None,
                expected_revision=0,
                input=lead,
            ),
        )

    def commit_lead(self, *, source_id: str, **values: Any) -> api.ProposalRow:
        row = self.propose_lead(source_id=source_id, **values)
        self.ledger.decide(self.ctx("OS"), row.proposal_id, row.approval_id, "approve")
        return row

    def receipts(self) -> list[dict]:
        return self.ledger.rows("os_receipts")


def committed_probe(factory: sessionmaker) -> Callable[[Any], str]:
    """Read the ApprovalRow decision through a brand-new session (committed state only)."""

    def probe(approval) -> str:
        with factory() as fresh:
            row = fresh.get(ApprovalRow, approval.id)
            return "" if row is None else row.decision

    return probe


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ledger_owner_action(
    store: LeadStore,
    settings: Settings,
    row: api.ProposalRow,
    *,
    source_ref: str = "telegram:v2-28",
    body: dict | None = None,
) -> OwnerActionProposal:
    """Create the card the way the Mia tool does under ``postgres`` (V2-20's target shape)."""
    from app.domain.owner.proposal_cards import ledger_proposal_body

    pending = body if body is not None else ledger_proposal_body(row)
    return propose_owner_action(
        store,
        principal=Principal.owner(source="telegram", actor_id=OWNER),
        source_ref=source_ref,
        kind="assaf_os.approve",
        parameters={
            "proposal_id": row.proposal_id,
            "request_id": row.approval_id,
            "rejection_request_id": row.rejection_id,
        },
        target={
            "proposal": pending,
            "proposal_digest": hashlib.sha256(_json(pending).encode()).hexdigest(),
            "approval_guard_version": 1,
            "workbook_id": settings.assaf_os_workbook_id,
            "workspace_id": settings.assaf_os_workspace_id,
            "mode": settings.assaf_os_mode,
        },
        now=datetime.now(UTC),
        ttl_seconds=600,
    )


def approve_row(
    store: LeadStore, approval_id: str, decision: str = DECISION_APPROVED
) -> ApprovalRow:
    """The Telegram CAS outcome, left uncommitted in the caller's session."""
    row = store.get_approval_by_approval_id(approval_id)
    assert row is not None
    row.decision = decision
    row.approver = OWNER
    store.session.flush()
    return row


def provider_claims(store: LeadStore) -> list[tuple[str, str]]:
    return [
        (item.key, item.status)
        for item in store.session.query(IdempotencyRow).filter(IdempotencyRow.scope == "approval")
    ]


def seed_lead_source(store: LeadStore, *, phone: str = "050-222-2222") -> dict:
    """One captured website conversation with its durable Telegram delivery payload."""
    captured = {
        "name": "דנה",
        "phone": "+972502222222",
        "email": "visitor@example.com",
        "source": "AssafWeb",
        "status": "new",
    }
    raw = {**captured, "phone": phone}
    store.session.add(
        CrmContactRow(
            id="crm-contact-1",
            revision=1,
            fields_json=json.dumps(captured, ensure_ascii=False),
            source_ref="old-source",
            conversation_id="older-conversation",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    store.session.add(
        CrmContactConversationRow(
            id="conversation-link-1",
            contact_id="crm-contact-1",
            conversation_id="site-current",
            captured_fields_json=json.dumps(captured, ensure_ascii=False),
            source_ref="website:site-current",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    payload = {
        "conversation_id": "site-current",
        "recipient_id": OWNER,
        "text": "new lead",
        "lead_source": {"source_ref": "website:site-current", "raw_fields": raw},
    }
    store.session.add(
        CrmOutboxRow(
            id="outbox-1",
            dedupe_key="telegram:site-current",
            aggregate_type="contact",
            aggregate_id="crm-contact-1",
            destination="telegram",
            payload_json=json.dumps(payload, ensure_ascii=False),
            status="pending",
            created_at=NOW,
        )
    )
    store.session.flush()
    return payload
