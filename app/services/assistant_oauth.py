"""E39 OAuth front: immutable consent, two factors, one atomic code exchange.

The public side never manufactures an owner context. It uses the existing guarded ledger
connection and workspace admission SQL; owner and Telegram paths use V2-19 constructors.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from sqlalchemy import text

from app.services import assistant_keys as keys
from app.services.assistant_issue import session_binding
from app.services.os_ledger import api, authorize
from app.services.os_ledger.guard import ADMISSION_SQL, AUTHORITY_SQL, check_identity
from app.services.os_ledger.statement_wire import binding, iso
from app.services.os_ledger.statements import db_revoke_watermark

CLIENT_ID = "https://chatgpt.com/oauth/client.json"
REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
SCOPE = "assaf-os:assistant"
REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(seconds=60)
TOKEN_TTL = timedelta(days=30)
ACTIVE = frozenset({"pending_owner", "approval_requested", "approved", "code_issued"})
_ID = re.compile(r"OR_[0-9a-f]{32}")
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
AUTHORIZE_PARAMS = frozenset({"response_type", "client_id", "redirect_uri", "code_challenge",
                              "code_challenge_method", "state", "resource", "scope"})
TOKEN_PARAMS = frozenset({"grant_type", "code", "redirect_uri", "client_id", "code_verifier",
                          "resource", "scope"})


class OAuthError(Exception):
    """Only a fixed OAuth code, never input values or provider/SQL text."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def enabled(settings):
    if (settings.kill_switch or settings.assaf_os_assistant_enabled is not True
            or settings.assaf_os_backend != "postgres"):
        raise api.LedgerError("ledger_unavailable", reason="oauth_off")


def issuer(settings, *, allow_http=False):
    value = settings.public_base_url
    try:
        parsed = urlsplit(value)
        schemes = {"https", "http"} if allow_http else {"https"}
        if (parsed.scheme.lower() not in schemes or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or "?" in value or "#" in value
                or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
            raise ValueError
        parsed.port  # validate its syntax without restricting legitimate HTTPS origins
    except (ValueError, TypeError):
        raise api.LedgerError("ledger_unavailable", reason="oauth_issuer_unset") from None
    return parsed.scheme.lower() + "://" + parsed.netloc.lower()


def resource(settings):
    return issuer(settings) + "/v1/assistant/mcp"


def same_resource(value, settings):
    if type(value) is not str or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        expected = urlsplit(resource(settings))
        return (parsed.scheme.lower() == expected.scheme
                and parsed.netloc.lower() == expected.netloc
                and parsed.path == expected.path and "?" not in value and "#" not in value)
    except ValueError:
        return False


def validate_authorize(settings, params):
    params = {k: v for k, v in params.items() if k in AUTHORIZE_PARAMS}
    required = {"response_type", "client_id", "redirect_uri", "code_challenge",
                "code_challenge_method", "state", "resource"}
    if (not required <= params.keys()
            or any(type(v) is not str for v in params.values())
            or params["response_type"] != "code" or params["client_id"] != CLIENT_ID
            or params["redirect_uri"] != REDIRECT_URI
            or params["code_challenge_method"] != "S256"
            or not _CHALLENGE.fullmatch(params["code_challenge"])
            or not re.fullmatch(r"[\x20-\x7e]{1,512}", params["state"])
            or params.get("scope", SCOPE) != SCOPE
            or not same_resource(params["resource"], settings)):
        raise OAuthError("invalid_request")
    return {**params, "scope": SCOPE, "resource": resource(settings)}


@dataclass(frozen=True, repr=False)
class OAuthRequest:
    workspace_id: str
    oauth_request_id: str
    owner_id: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scope: str
    resource: str
    state: str
    epoch: int
    status: str
    created_at: object
    deadline: object
    client_ip_hmac: str
    chain_binding: str | None = None
    approved_at: object = None
    code_hash: str | None = None
    code_expires_at: object = None
    code_used_at: object = None
    credential_id: str | None = None


class OAuthTables:
    def __init__(self, tx):
        self.tx = tx

    def memory(self):
        return self.tx.scratch.setdefault("assistant_oauth_requests", {})

    def get(self, id=None, *, code_hash=None):
        if self.tx.connection is None:
            if code_hash is not None:
                return next((r for r in self.memory().values() if r.code_hash == code_hash), None)
            return self.memory().get(id)
        predicate = "code_hash = :value" if code_hash is not None else "oauth_request_id = :value"
        row = self.tx.connection.execute(text(
            "SELECT * FROM assaf_os.os_assistant_oauth_requests WHERE workspace_id = :ws AND "
            + predicate + " FOR UPDATE"),
            {"ws": self.tx.ctx.workspace, "value": code_hash if code_hash is not None else id}
        ).mappings().first()
        return None if row is None else OAuthRequest(**row)

    def insert(self, row):
        if self.tx.connection is None:
            self.memory()[row.oauth_request_id] = row
            return
        fields = tuple(vars(row))
        self.tx.connection.execute(text(
            "INSERT INTO assaf_os.os_assistant_oauth_requests (" + ", ".join(fields)
            + ") VALUES (" + ", ".join(":" + f for f in fields) + ")"), vars(row))

    def update(self, row, **values):
        assert values and set(values) <= {"status", "chain_binding", "approved_at", "code_hash",
                                          "code_expires_at", "code_used_at", "credential_id"}
        if self.tx.connection is None:
            self.memory()[row.oauth_request_id] = replace(row, **values)
            return
        changed = self.tx.connection.execute(text(
            "UPDATE assaf_os.os_assistant_oauth_requests SET "
            + ", ".join(f"{k} = :{k}" for k in values)
            + " WHERE workspace_id = :ws AND oauth_request_id = :id AND status = :previous"),
            {**values, "ws": row.workspace_id, "id": row.oauth_request_id, "previous": row.status})
        if changed.rowcount != 1:
            raise api.LedgerError("ledger_unavailable", reason="oauth_cas_failed")

    def live(self):
        """The caller holds the workspace lock through expiry, recount and admission."""
        if self.tx.connection is None:
            for row in list(self.memory().values()):
                if row.status in ACTIVE and (
                        row.code_expires_at if row.status == "code_issued" else row.deadline
                ) <= self.tx.now:
                    self.update(row, status="expired")
            return sorted((r for r in self.memory().values() if r.status in ACTIVE),
                          key=lambda r: (r.created_at, r.oauth_request_id))
        self.tx.connection.execute(text(
            "UPDATE assaf_os.os_assistant_oauth_requests SET status = 'expired' "
            "WHERE workspace_id = :ws AND status IN "
            "('pending_owner', 'approval_requested', 'approved', 'code_issued') AND "
            "CASE WHEN status = 'code_issued' THEN code_expires_at ELSE deadline END <= :now"),
            {"ws": self.tx.ctx.workspace, "now": self.tx.now})
        return [OAuthRequest(**r) for r in self.tx.connection.execute(text(
            "SELECT * FROM assaf_os.os_assistant_oauth_requests WHERE workspace_id = :ws "
            "AND status IN ('pending_owner', 'approval_requested', 'approved', 'code_issued') "
            "ORDER BY created_at, oauth_request_id"),
            {"ws": self.tx.ctx.workspace}).mappings()]

    def cancel_uncollected(self):
        if self.tx.connection is None:
            for row in list(self.memory().values()):
                if row.workspace_id == self.tx.ctx.workspace and row.status in ACTIVE:
                    self.update(row, status="cancelled")
            return
        self.tx.connection.execute(text(
            "UPDATE assaf_os.os_assistant_oauth_requests SET status = 'cancelled' "
            "WHERE workspace_id = :ws AND status IN "
            "('pending_owner', 'approval_requested', 'approved', 'code_issued')"),
            {"ws": self.tx.ctx.workspace})


class AssistantOAuthService(keys.AssistantCredentialService):
    def __init__(self, *args, revoke_watermark=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._watermark = revoke_watermark or db_revoke_watermark
        self._store = keys.PgAssistantStore(self._settings)

    @contextmanager
    def _public_tx(self, *, ip_hmac=None):
        """Same workspace lock/epoch/fence as the ledger; no public owner context is minted."""
        settings = self._settings
        enabled(settings)
        if settings.assaf_os_ledger_enabled is not True:
            raise api.LedgerError("ledger_unavailable", reason="ledger_disabled")
        workspace = settings.assaf_os_workspace_id
        epoch = settings.assaf_os_authority_epoch
        if (type(workspace) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", workspace) is None
                or type(epoch) is not int or epoch < 1):
            raise api.LedgerError("ledger_unavailable", reason="oauth_workspace_unavailable")
        with self._store._begin() as conn:
            # Eviction adds an IP-counted audit row: preserve R2a's IP -> workspace order.
            if ip_hmac is not None:
                self._store._lock(conn, f"ip:{ip_hmac}")
            row = conn.execute(ADMISSION_SQL, {"ws": workspace}).one()
            check_identity(row.current_role, row.session_role)
            if row.owner_id is None or row.owner_id not in settings.telegram_owner_user_id_set():
                raise api.LedgerError("ledger_unavailable", reason="oauth_workspace_unavailable")
            authority = {r.domain: (r.state, int(r.epoch)) for r in conn.execute(
                AUTHORITY_SQL, {"ws": workspace})}
            if not (row.epoch == epoch and all(
                    authority.get(d) == ("active", epoch) for d in ("core", "statements"))):
                raise api.LedgerError("authority_changed")
            clock = getattr(self._ledger, "clock", None)
            now = clock() if callable(clock) else row.db_now
            ctx = SimpleNamespace(workspace=workspace, numeric_owner=int(row.owner_id),
                                  authority_epoch=epoch)
            tx = self._ledger._tx(conn, ctx, row, kind="statement_batch", purpose="write")
            tx.now = now
            yield tx
            if tx.poisoned is not None:
                raise tx.poisoned

    def account(self, ip, operation, *, reason="oauth_attempt"):
        enabled(self._settings)
        self._store.oauth_attempt(ip_hmac=keys.client_ip_hmac(self._settings, ip),
                                  operation=operation, reason=reason)

    def authorize(self, params, *, ip):
        enabled(self._settings)
        p = validate_authorize(self._settings, params)
        ip_hmac = keys.client_ip_hmac(self._settings, ip)
        row = None
        with self._public_tx(ip_hmac=ip_hmac) as tx:
            tables = OAuthTables(tx)
            unbound = [r for r in tables.live() if r.status == "pending_owner"]
            if sum(r.client_ip_hmac == ip_hmac for r in unbound) < 2:
                if len(unbound) >= 5:
                    if tx.connection is None:
                        tx.scratch.setdefault("assistant_oauth_audit", []).append("evicted_unbound")
                    else:
                        if self._store._count(
                                tx.connection, "client_ip_hmac = :ip AND occurred_at >= :since",
                                ip=ip_hmac, since=tx.now - timedelta(
                                    seconds=keys.IP_WINDOW_SECONDS)) >= keys.IP_LIMIT:
                            raise keys._limited("ip_limit")
                        self._store._insert(
                            tx.connection, credential_id=None, moment=tx.now,
                            operation="oauth_authorize", reason="evicted_unbound", ok=True,
                            facts=keys.Facts(), ip_hmac=ip_hmac)
                    tables.update(unbound[0], status="cancelled")
                row = OAuthRequest(
                    tx.ctx.workspace, "OR_" + secrets.token_hex(16), str(tx.ctx.numeric_owner),
                    p["client_id"], p["redirect_uri"], p["code_challenge"], p["scope"],
                    p["resource"], p["state"], tx.ctx.authority_epoch, "pending_owner",
                    tx.now, tx.now + REQUEST_TTL, client_ip_hmac=ip_hmac)
                tables.insert(row)
        if row is None:
            raise OAuthError("oauth_pending_limit")
        return row

    def _owner(self, ctx, operation):
        self._admit(ctx, operation)
        if self._settings.kill_switch:
            raise api.LedgerError("kill_switch")

    def _same(self, tx, id, chain_id):
        row = OAuthTables(tx).get(id) if type(id) is str and _ID.fullmatch(id) else None
        bound = session_binding(self._settings, chain_id)
        if row is None or (row.chain_binding is not None and not hmac.compare_digest(
                row.chain_binding, bound)):
            raise api.LedgerError("oauth_request_unknown")
        self._binding(row, tx.ctx)
        return row

    @staticmethod
    def _binding(row, ctx):
        if (row.workspace_id, row.owner_id, row.epoch) != (
                ctx.workspace, str(ctx.numeric_owner), ctx.authority_epoch):
            raise api.LedgerError("authority_changed", reason="oauth_binding_changed")

    def _not_revoked(self, row):
        mark = self._watermark(row.owner_id)
        if mark is not None and mark >= row.created_at:
            raise api.LedgerError("authority_changed", reason="owner_revoked_all")

    @staticmethod
    def _status(row, now):
        if (row.status in {"pending_owner", "approval_requested", "approved"}
                and now >= row.deadline):
            return "expired"
        if row.status == "code_issued" and now >= row.code_expires_at:
            return "expired"
        return row.status

    def _transition(self, row, now, allowed):
        status = self._status(row, now)
        if status not in allowed:
            code = {"expired": "oauth_expired", "cancelled": "oauth_cancelled",
                    "rejected": "oauth_rejected", "code_issued": "oauth_completed",
                    "exchanged": "oauth_completed"}.get(status, "oauth_not_approved")
            raise api.LedgerError(code)
        self._not_revoked(row)

    def _body(self, ctx, row, now):
        return {**binding(ctx), "oauth_request_id": row.oauth_request_id,
                "status": self._status(row, now), "client": "ChatGPT", "scope": SCOPE,
                "expires_in_days": 30, "expires_at": iso(row.deadline),
                "created_at": iso(row.created_at), "deadline": iso(row.deadline)}

    def status(self, ctx, id, *, chain_id):
        self._owner(ctx, "read")
        with self._tx(ctx) as tx:
            row = self._same(tx, id, chain_id)
            return self._body(ctx, row, tx.now)

    def request_approval(self, ctx, id, *, chain_id):
        self._owner(ctx, "manage_credentials")
        with self._tx(ctx) as tx:
            row = self._same(tx, id, chain_id)
            self._transition(row, tx.now, {"pending_owner", "approval_requested"})
            if row.status == "pending_owner":
                if sum(r.status != "pending_owner" for r in OAuthTables(tx).live()) >= 5:
                    raise api.LedgerError("oauth_not_approved", reason="oauth_bound_limit")
                OAuthTables(tx).update(row, status="approval_requested",
                                       chain_binding=session_binding(self._settings, chain_id))
                row = OAuthTables(tx).get(id)
            return self._body(ctx, row, tx.now)

    @staticmethod
    def envelope(row):
        return {"kind": "assistant_key.oauth_grant", "parameters": {
            "oauth_request_id": row.oauth_request_id, "client": "ChatGPT", "scope": SCOPE,
            "expires_in_days": 30,
            "created_at": iso(row.created_at)},
            "target": {
                "workspace_id": row.workspace_id, "authority_epoch": row.epoch,
                "chain_binding": row.chain_binding}, "version": 1}

    def proposal(self, ctx, id, *, chain_id):
        self._owner(ctx, "manage_credentials")
        with self._tx(ctx) as tx:
            row = self._same(tx, id, chain_id)
            self._transition(row, tx.now, {"approval_requested"})
            return self.envelope(row)

    def decide_grant(self, approval, *, decision):
        from app.services.owner_actions import read_owner_action

        if self._settings.kill_switch:
            raise api.LedgerError("kill_switch")
        if (self._settings.assaf_os_assistant_enabled is not True
                or self._settings.assaf_os_backend != "postgres"):
            raise api.LedgerError("operation_unavailable", reason="assistant_off")
        envelope = read_owner_action(approval)
        if (envelope is None or envelope["kind"] != "assistant_key.oauth_grant"
                or decision not in {"approve", "reject"} or approval.decision != (
                    "approved" if decision == "approve" else "rejected")):
            raise api.LedgerError("owner_approval_required")
        ctx = authorize.from_telegram_decision(approval, self._settings)
        authorize.require(ctx, "decide")
        with self._tx(ctx) as tx:
            row = OAuthTables(tx).get(envelope["parameters"].get("oauth_request_id"))
            if (row is None or row.chain_binding is None or self.envelope(row) != envelope
                    or row.owner_id != approval.actor_id):
                raise api.LedgerError("target_changed")
            self._binding(row, ctx)
            self._not_revoked(row)
            desired = "approved" if decision == "approve" else "rejected"
            if row.status == desired or desired == "approved" and row.status in {
                    "code_issued", "exchanged"}:
                return "already_handled"
            self._transition(row, tx.now, {"approval_requested"})
            values = {"status": desired}
            if desired == "approved":
                values["approved_at"] = tx.now
            OAuthTables(tx).update(row, **values)
        return desired

    def complete(self, ctx, id, *, chain_id):
        self._owner(ctx, "manage_credentials")
        with self._tx(ctx) as tx:
            row = self._same(tx, id, chain_id)
            self._transition(row, tx.now, {"approved"})
            if row.chain_binding is None or row.approved_at is None:
                raise api.LedgerError("oauth_not_approved")
            code = secrets.token_urlsafe(32)
            OAuthTables(tx).update(row, status="code_issued", code_hash=digest(code),
                                   code_expires_at=tx.now + CODE_TTL)
            url = row.redirect_uri + "?" + urlencode({
                "code": code, "state": row.state, "iss": issuer(self._settings)})
        return {"redirect_url": url}

    def token(self, params):
        enabled(self._settings)
        params = {k: v for k, v in params.items() if k in TOKEN_PARAMS}
        required = {"grant_type", "code", "redirect_uri", "client_id", "code_verifier", "resource"}
        if (not required <= params.keys()
                or any(type(v) is not str for v in params.values())
                or any(not params[k] for k in required)):
            raise OAuthError("invalid_request")
        if params["grant_type"] != "authorization_code":
            raise OAuthError("unsupported_grant_type")
        if params["client_id"] != CLIENT_ID:
            raise OAuthError("invalid_client")
        if params.get("scope", SCOPE) != SCOPE:
            raise OAuthError("invalid_scope")
        if not _CHALLENGE.fullmatch(params["code"]):
            raise OAuthError("invalid_grant")
        result = None
        with self._public_tx() as tx:
            tables = OAuthTables(tx)
            row = tables.get(code_hash=digest(params["code"]))
            if row is not None and row.code_used_at is not None:
                # Commit revocation before invalid_grant; raising inside would roll it back.
                if row.credential_id is not None:
                    self._revoke_in(tx, keys.credential_tables(tx), row.credential_id)
            elif row is not None:
                valid_binding = True
                try:
                    self._binding(row, tx.ctx)
                    self._not_revoked(row)
                except api.LedgerError as exc:
                    if exc.code != "authority_changed":
                        raise
                    valid_binding = False
                verifier = params["code_verifier"]
                challenge = (base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii"))
                             .digest()).decode().rstrip("=") if _VERIFIER.fullmatch(verifier)
                             else "")
                if (valid_binding and row.status == "code_issued"
                        and row.code_expires_at is not None
                        and tx.now < row.code_expires_at and row.chain_binding is not None
                        and row.approved_at is not None
                        and params["redirect_uri"] == row.redirect_uri
                        and params["client_id"] == row.client_id
                        and same_resource(params["resource"], self._settings)
                        and row.resource == resource(self._settings)
                        and hmac.compare_digest(challenge, row.code_challenge)):
                    value = self._new_secret()
                    minted = keys.StoredCredential(
                        self._new_id(), row.owner_id, row.epoch, "ChatGPT connector", "gpt", tx.now,
                        tx.now + TOKEN_TTL, None, workspace_id=row.workspace_id,
                        secret_hash=keys.secret_hash(value), audience=row.resource)
                    keys.credential_tables(tx).insert(minted)
                    tables.update(row, status="exchanged", code_used_at=tx.now,
                                  credential_id=minted.credential_id)
                    result = {"access_token": value, "token_type": "Bearer",
                              "expires_in": int(TOKEN_TTL.total_seconds()), "scope": SCOPE}
        if result is None:
            raise OAuthError("invalid_grant")
        return result
