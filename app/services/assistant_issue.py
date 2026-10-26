"""E37: Telegram approves an immutable request; only its OS session can mint once.

The main DB card decision commits before this ledger CAS. There are no provider claims:
a failed ledger transaction is retried by the next tap. Collection locks the request before
generating a key, and commits the hash and the collected status together.
"""

from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.services import assistant_keys as keys
from app.services.os_ledger import api, authorize
from app.services.os_ledger.statement_wire import binding, iso
from app.services.os_ledger.statements import db_revoke_watermark

TTL = timedelta(minutes=10)
_ID = re.compile(r"IR_[0-9a-f]{32}")


def session_binding(settings: Any, chain_id: str) -> str:
    raw = getattr(settings, "os_step_up_secret", "")
    if type(raw) is not str or len(raw.encode()) < 32:
        raise api.LedgerError("ledger_unavailable", reason="step_up_secret_unset")
    if type(chain_id) is not str or not chain_id:
        raise api.LedgerError("owner_auth_required", reason="session_missing")
    return hmac.new(raw.encode(), chain_id.encode(), "sha256").hexdigest()


@dataclass(frozen=True, repr=False)
class IssueRequest:
    workspace_id: str
    issue_request_id: str
    owner_id: str
    epoch: int
    label: str
    kind: str
    expires_in_days: int
    session_binding: str
    status: str
    created_at: datetime
    approval_deadline: datetime
    approved_at: datetime | None = None
    collect_deadline: datetime | None = None
    collected_at: datetime | None = None
    credential_id: str | None = None


class IssueTables:
    """The request row on the ledger connection, or the transaction's rollback scratch."""

    def __init__(self, tx):
        self.tx = tx

    def memory(self):
        return self.tx.scratch.setdefault("assistant_issue_requests", {})

    def get(self, id: str) -> IssueRequest | None:
        if self.tx.connection is None:
            return self.memory().get(id)
        row = (
            self.tx.connection.execute(
                text(
                    "SELECT * FROM assaf_os.os_assistant_issue_requests "
                    "WHERE workspace_id = :ws AND issue_request_id = :id FOR UPDATE"
                ),
                {"ws": self.tx.ctx.workspace, "id": id},
            )
            .mappings()
            .first()
        )
        return IssueRequest(**row) if row is not None else None

    def insert(self, row: IssueRequest):
        if self.tx.connection is None:
            self.memory()[row.issue_request_id] = row
            return
        self.tx.connection.execute(
            text(
                "INSERT INTO assaf_os.os_assistant_issue_requests "
                "(workspace_id, issue_request_id, owner_id, epoch, label, kind, expires_in_days, "
                "session_binding, status, created_at, approval_deadline) VALUES "
                "(:workspace_id, :issue_request_id, :owner_id, :epoch, :label, :kind, "
                ":expires_in_days, :session_binding, :status, :created_at, :approval_deadline)"
            ),
            vars(row),
        )

    def update(self, row: IssueRequest, **values):
        assert set(values) <= {"status", "approved_at", "collected_at", "credential_id"}
        if self.tx.connection is None:
            if "approved_at" in values:
                values["collect_deadline"] = values["approved_at"] + TTL
            self.memory()[row.issue_request_id] = replace(row, **values)
            return
        changed = self.tx.connection.execute(
            text(
                "UPDATE assaf_os.os_assistant_issue_requests SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + " WHERE workspace_id = :ws AND issue_request_id = :id AND status = :previous"
            ),
            {**values, "ws": row.workspace_id, "id": row.issue_request_id, "previous": row.status},
        )
        if changed.rowcount != 1:
            raise api.LedgerError("ledger_unavailable", reason="issue_cas_failed")

    def cancel_uncollected(self):
        if self.tx.connection is None:
            for id, row in list(self.memory().items()):
                if row.workspace_id == self.tx.ctx.workspace and row.status in {
                    "pending_approval", "approved"
                }:
                    self.update(row, status="cancelled")
            return
        self.tx.connection.execute(text(
            "UPDATE assaf_os.os_assistant_issue_requests SET status = 'cancelled' "
            "WHERE workspace_id = :ws AND status IN ('pending_approval', 'approved')"
        ), {"ws": self.tx.ctx.workspace})


class AssistantIssueService(keys.AssistantCredentialService):
    def __init__(self, *args, revoke_watermark=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._watermark = revoke_watermark or db_revoke_watermark

    def _revoke_all_in(self, tx):
        IssueTables(tx).cancel_uncollected()
        from app.services.assistant_oauth import OAuthTables

        OAuthTables(tx).cancel_uncollected()

    def _live(self):
        if self._settings.kill_switch:
            raise api.LedgerError("kill_switch")

    def issue(self, ctx, *, chain_id, label, kind, expires_in_days=None):
        self._admit(ctx, "manage_credentials")
        self._live()
        bound = session_binding(self._settings, chain_id)
        clean = keys._label(label)
        if type(kind) is not str or kind not in keys.KINDS:
            raise keys._invalid("invalid_kind")
        days = keys.DEFAULT_DAYS if expires_in_days is None else expires_in_days
        if type(days) is not int or not 1 <= days <= keys.MAX_DAYS:
            raise keys._invalid("invalid_expiry")
        with self._tx(ctx) as tx:
            row = IssueRequest(
                ctx.workspace,
                "IR_" + secrets.token_hex(16),
                str(ctx.numeric_owner),
                ctx.authority_epoch,
                clean,
                kind,
                days,
                bound,
                "pending_approval",
                tx.now,
                tx.now + TTL,
            )
            IssueTables(tx).insert(row)
        return self._body(ctx, row, row.status)

    def _same(self, tables, id, chain_id):
        row = tables.get(id) if type(id) is str and _ID.fullmatch(id) else None
        if row is None or not hmac.compare_digest(
            row.session_binding, session_binding(self._settings, chain_id)
        ):
            raise api.LedgerError("credential_unknown")
        return row

    def _body(self, ctx, row, status):
        deadline = row.collect_deadline if row.approved_at is not None else row.approval_deadline
        return {
            **binding(ctx),
            "issue_request_id": row.issue_request_id,
            "status": status,
            "expires_at": iso(deadline),
        }

    def _status(self, row, now):
        deadline = row.collect_deadline if row.approved_at is not None else row.approval_deadline
        return (
            "expired"
            if row.status in {"pending_approval", "approved"} and now >= deadline
            else row.status
        )

    @staticmethod
    def envelope(row):
        return {
            "kind": "assistant_key.issue",
            "parameters": {
                "issue_request_id": row.issue_request_id,
                "label": row.label,
                "kind": row.kind,
                "expires_in_days": row.expires_in_days,
            },
            "target": {
                "workspace_id": row.workspace_id,
                "authority_epoch": row.epoch,
                "session_binding": row.session_binding,
            },
            "version": 1,
        }

    def proposal(self, ctx, id, *, chain_id):
        self._admit(ctx, "manage_credentials")
        with self._tx(ctx) as tx:
            return self.envelope(self._same(IssueTables(tx), id, chain_id))

    def status(self, ctx, id, *, chain_id):
        self._admit(ctx, "read")
        with self._tx(ctx) as tx:
            row = self._same(IssueTables(tx), id, chain_id)
            return self._body(ctx, row, self._status(row, tx.now))

    def decide_issue(self, approval, *, decision):
        from app.services.owner_actions import read_owner_action

        self._live()
        envelope = read_owner_action(approval)
        if envelope is None or envelope["kind"] != "assistant_key.issue":
            raise api.LedgerError("owner_approval_required")
        if decision not in {"approve", "reject"} or approval.decision != (
            "approved" if decision == "approve" else "rejected"
        ):
            raise api.LedgerError("owner_approval_required")
        ctx = authorize.from_telegram_decision(approval, self._settings)
        authorize.require(ctx, "decide")
        if not self._settings.assaf_os_assistant_enabled:
            raise api.LedgerError("operation_unavailable", reason="assistant_off")
        with self._tx(ctx) as tx:
            tables = IssueTables(tx)
            row = tables.get(envelope["parameters"].get("issue_request_id", ""))
            if row is None or self.envelope(row) != envelope or row.owner_id != approval.actor_id:
                raise api.LedgerError("target_changed")
            if row.status == "cancelled":
                raise api.LedgerError("issue_cancelled")
            self._check_binding(row, ctx)
            self._not_revoked(row)
            desired = "approved" if decision == "approve" else "rejected"
            if row.status == desired or row.status == "collected" and desired == "approved":
                return "already_handled"
            if tx.now >= row.approval_deadline:
                raise api.LedgerError("issue_expired")
            if row.status != "pending_approval":
                raise api.LedgerError("issue_rejected")
            values = {"status": desired}
            if desired == "approved":
                values["approved_at"] = tx.now
            tables.update(row, **values)
        return desired

    @staticmethod
    def _check_binding(row, ctx):
        # Ledger admission binds the current context to the workspace. A refreshed context
        # after an epoch bump must also reject an older immutable request, on both paths.
        if (row.workspace_id, row.epoch, row.owner_id) != (
            ctx.workspace, ctx.authority_epoch, str(ctx.numeric_owner)
        ):
            raise api.LedgerError("authority_changed", reason="issue_binding_changed")

    def _not_revoked(self, row):
        mark = self._watermark(row.owner_id)
        if mark is not None and mark >= row.created_at:
            raise api.LedgerError("authority_changed", reason="owner_revoked_all")

    def collect(self, ctx, id, *, chain_id):
        self._admit(ctx, "manage_credentials")
        self._live()
        with self._tx(ctx) as tx:
            tables = IssueTables(tx)
            row = self._same(tables, id, chain_id)  # FOR UPDATE before minting
            if row.status == "cancelled":
                raise api.LedgerError("issue_cancelled")
            self._check_binding(row, ctx)
            self._not_revoked(row)
            status = self._status(row, tx.now)
            code = {
                "pending_approval": "issue_not_approved",
                "rejected": "issue_rejected",
                "expired": "issue_expired",
                "collected": "issue_collected",
                "cancelled": "issue_cancelled",
            }.get(status)
            if code:
                raise api.LedgerError(code)
            if status != "approved":
                raise api.LedgerError("issue_not_approved")
            secret = self._new_secret()
            minted = keys.StoredCredential(
                self._new_id(),
                row.owner_id,
                row.epoch,
                row.label,
                row.kind,
                tx.now,
                tx.now + timedelta(days=row.expires_in_days),
                None,
                workspace_id=ctx.workspace,
                scopes=keys.SCOPES_TEXT,
                secret_hash=keys.secret_hash(secret),
            )
            keys.credential_tables(tx).insert(minted)
            tables.update(
                row, status="collected", collected_at=tx.now, credential_id=minted.credential_id
            )
        return {**binding(ctx), **keys._item(minted), "secret": secret}
