"""Opaque, server-side owner web sessions.

Only hashes of browser credentials are stored.  The API boundary owns Telegram
proof verification and the response cookie; this module owns the transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

MAX_SESSION_TTL_SECONDS = 30 * 60
SESSION_TOKEN_BYTES = 32
CHALLENGE_TOKEN_BYTES = 32


class OwnerSessionDenied(PermissionError):
    """An opaque credential was absent, expired, revoked, or already consumed."""


class OwnerSessionReplay(OwnerSessionDenied):
    """A login proof or pre-authentication challenge was already consumed."""


class OwnerSessionRateLimited(OwnerSessionDenied):
    """The durable global auth-attempt budget was exhausted."""


@dataclass(frozen=True)
class OwnerSessionRecord:
    owner_id: str
    scopes: frozenset[str]
    session_hash: str
    expires_at: datetime
    workspace_id: str
    login_proof_hash: str


@dataclass(frozen=True)
class IssuedChallenge:
    cookie_token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedSession:
    cookie_token: str
    record: OwnerSessionRecord


def credential_hash(value: str, *, purpose: str) -> str:
    """Domain-separate hashes so equal opaque values cannot correlate across tables."""
    return hashlib.sha256(f"owner-os:{purpose}:".encode() + value.encode()).hexdigest()


def challenge_hash(cookie_token: str, csrf_token: str) -> str:
    return credential_hash(f"{cookie_token}:{csrf_token}", purpose="challenge")


def login_proof_hash(signature_hex: str) -> str:
    """Hash Telegram's signed digest, independent of JSON ordering/serialization."""
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise OwnerSessionDenied("invalid proof signature") from exc
    if len(signature) != hashlib.sha256().digest_size:
        raise OwnerSessionDenied("invalid proof signature")
    return hashlib.sha256(b"owner-os:telegram-proof:" + signature).hexdigest()


def session_csrf_token(cookie_token: str, *, secret: str) -> str:
    """Derive a multi-instance CSRF value without persisting its raw form."""
    key = hashlib.sha256(b"owner-os:csrf-key:" + secret.encode()).digest()
    return hmac.new(key, cookie_token.encode(), hashlib.sha256).hexdigest()


def valid_session_csrf(cookie_token: str, supplied: str, *, secret: str) -> bool:
    if not cookie_token or not supplied or len(supplied) > 128:
        return False
    return hmac.compare_digest(session_csrf_token(cookie_token, secret=secret), supplied)


def _utc(moment: datetime | str | None = None) -> datetime:
    current = moment or datetime.now(UTC)
    if isinstance(current, str):
        current = datetime.fromisoformat(current)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _scopes_text(scopes: frozenset[str]) -> str:
    return ",".join(sorted(scopes))


def _parse_scopes(value: str) -> frozenset[str]:
    return frozenset(item for item in value.split(",") if item)


class OwnerSessionStore:
    """Transaction-scoped store. Callers must commit before issuing credentials."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _now(self, supplied: datetime | None) -> datetime:
        if supplied is not None:
            return _utc(supplied)
        return _utc(self.session.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one())

    def _lock_owner(self, owner_id: str) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(text("SET LOCAL statement_timeout = '8s'"))
            self.session.execute(text("SET LOCAL lock_timeout = '8s'"))
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"owner-os:{owner_id}"},
            )

    def _revoked_before(self, owner_id: str) -> datetime:
        row = self.session.execute(
            text(
                """
                SELECT revoked_before
                FROM owner_os_revocations
                WHERE owner_id = :owner_id
                """
            ),
            {"owner_id": owner_id},
        ).first()
        return _utc(row[0]) if row is not None else datetime(1970, 1, 1, tzinfo=UTC)

    def record_auth_attempt(
        self,
        kind: str,
        *,
        limit: int,
        window_seconds: int,
        now: datetime | None = None,
    ) -> None:
        """Apply a small durable global budget for this single-owner auth surface."""
        if kind not in {"challenge", "login"}:
            raise ValueError("invalid auth attempt kind")
        moment = self._now(now)
        since = moment - timedelta(seconds=window_seconds)
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"owner-os:auth-rate:{kind}"},
            )
        count = self.session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM owner_os_auth_attempts
                WHERE kind = :kind
                  AND occurred_at >= :since
                """
            ),
            {"kind": kind, "since": since},
        ).scalar_one()
        if int(count) >= limit:
            raise OwnerSessionRateLimited("auth rate limited")
        self.session.execute(
            text(
                """
                INSERT INTO owner_os_auth_attempts (kind, occurred_at)
                VALUES (:kind, :occurred_at)
                """
            ),
            {"kind": kind, "occurred_at": moment},
        )

    def issue_challenge(
        self,
        *,
        ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> IssuedChallenge:
        moment = self._now(now)
        ttl = max(1, min(ttl_seconds, 600))
        cookie_token = secrets.token_urlsafe(CHALLENGE_TOKEN_BYTES)
        csrf_token = secrets.token_urlsafe(CHALLENGE_TOKEN_BYTES)
        expires_at = moment + timedelta(seconds=ttl)
        self.session.execute(
            text(
                """
                INSERT INTO owner_os_login_challenges
                    (challenge_hash, expires_at, consumed_at, created_at)
                VALUES (:challenge_hash, :expires_at, NULL, :created_at)
                """
            ),
            {
                "challenge_hash": challenge_hash(cookie_token, csrf_token),
                "expires_at": expires_at,
                "created_at": moment,
            },
        )
        return IssuedChallenge(
            cookie_token=cookie_token,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )

    def validate_pending_challenge(
        self,
        *,
        challenge_cookie: str,
        challenge_csrf: str,
        now: datetime | None = None,
    ) -> None:
        """Read-only preflight before expensive external identity verification.

        This deliberately does not consume the challenge. ``create_session`` performs
        the authoritative consume in the same transaction as proof/session persistence.
        """
        if (
            not challenge_cookie
            or not challenge_csrf
            or len(challenge_cookie) > 256
            or len(challenge_csrf) > 256
        ):
            raise OwnerSessionReplay("challenge rejected")
        moment = self._now(now)
        pending = self.session.execute(
            text(
                """
                SELECT challenge_hash
                FROM owner_os_login_challenges
                WHERE challenge_hash = :challenge_hash
                  AND consumed_at IS NULL
                  AND expires_at >= :now
                """
            ),
            {
                "challenge_hash": challenge_hash(challenge_cookie, challenge_csrf),
                "now": moment,
            },
        ).first()
        if pending is None:
            raise OwnerSessionReplay("challenge rejected")

    def create_session(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        scopes: frozenset[str],
        proof_hash: str,
        proof_authenticated_at: datetime,
        challenge_cookie: str,
        challenge_csrf: str,
        previous_cookie: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> IssuedSession:
        """Consume challenge + proof, revoke prior cookie, and insert atomically."""
        if not owner_id.isascii() or not owner_id.isdigit() or not workspace_id or not scopes:
            raise OwnerSessionDenied("invalid owner session")
        moment = self._now(now)
        ttl = max(1, min(ttl_seconds, MAX_SESSION_TTL_SECONDS))
        self._lock_owner(owner_id)
        revoked_before = self._revoked_before(owner_id)
        if _utc(proof_authenticated_at) <= revoked_before:
            raise OwnerSessionReplay("proof predates revocation")
        consumed = self.session.execute(
            text(
                """
                UPDATE owner_os_login_challenges
                SET consumed_at = :now
                WHERE challenge_hash = :challenge_hash
                  AND consumed_at IS NULL
                  AND expires_at >= :now
                  AND created_at > :revoked_before
                RETURNING challenge_hash
                """
            ),
            {
                "challenge_hash": challenge_hash(challenge_cookie, challenge_csrf),
                "now": moment,
                "revoked_before": revoked_before,
            },
        ).first()
        if consumed is None:
            raise OwnerSessionReplay("challenge rejected")

        try:
            self.session.execute(
                text(
                    """
                    INSERT INTO owner_os_login_proofs (proof_hash, owner_id, consumed_at)
                    VALUES (:proof_hash, :owner_id, :consumed_at)
                    """
                ),
                {
                    "proof_hash": proof_hash,
                    "owner_id": owner_id,
                    "consumed_at": moment,
                },
            )
        except IntegrityError as exc:
            raise OwnerSessionReplay("proof replayed") from exc

        if previous_cookie:
            self.session.execute(
                text(
                    """
                    UPDATE owner_os_sessions
                    SET revoked_at = :now
                    WHERE session_hash = :session_hash
                      AND owner_id = :owner_id
                      AND revoked_at IS NULL
                    """
                ),
                {
                    "now": moment,
                    "session_hash": credential_hash(previous_cookie, purpose="session"),
                    "owner_id": owner_id,
                },
            )

        cookie_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        hashed = credential_hash(cookie_token, purpose="session")
        expires_at = moment + timedelta(seconds=ttl)
        self.session.execute(
            text(
                """
                INSERT INTO owner_os_sessions
                    (session_hash, owner_id, workspace_id, login_proof_hash, scopes,
                     expires_at, revoked_at, created_at)
                VALUES
                    (:session_hash, :owner_id, :workspace_id, :login_proof_hash, :scopes,
                     :expires_at, NULL, :created_at)
                """
            ),
            {
                "session_hash": hashed,
                "owner_id": owner_id,
                "workspace_id": workspace_id,
                "login_proof_hash": proof_hash,
                "scopes": _scopes_text(scopes),
                "expires_at": expires_at,
                "created_at": moment,
            },
        )
        return IssuedSession(
            cookie_token=cookie_token,
            record=OwnerSessionRecord(
                owner_id=owner_id,
                scopes=scopes,
                session_hash=hashed,
                expires_at=expires_at,
                workspace_id=workspace_id,
                login_proof_hash=proof_hash,
            ),
        )

    def authenticate(
        self,
        cookie_token: str,
        *,
        now: datetime | None = None,
    ) -> OwnerSessionRecord:
        if not cookie_token or len(cookie_token) > 256:
            raise OwnerSessionDenied("session required")
        moment = self._now(now)
        hashed = credential_hash(cookie_token, purpose="session")
        row = self.session.execute(
            text(
                """
                SELECT owner_id, workspace_id, login_proof_hash, scopes, expires_at
                FROM owner_os_sessions
                WHERE session_hash = :session_hash
                  AND revoked_at IS NULL
                  AND expires_at > :now
                """
            ),
            {"session_hash": hashed, "now": moment},
        ).mappings().first()
        if row is None:
            raise OwnerSessionDenied("session rejected")
        expires_at = _utc(row["expires_at"])
        return OwnerSessionRecord(
            owner_id=str(row["owner_id"]),
            scopes=_parse_scopes(str(row["scopes"])),
            session_hash=hashed,
            expires_at=expires_at,
            workspace_id=str(row["workspace_id"]),
            login_proof_hash=str(row["login_proof_hash"]),
        )

    def revoke_current(
        self,
        *,
        session_hash: str,
        owner_id: str,
        now: datetime | None = None,
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE owner_os_sessions
                SET revoked_at = :now
                WHERE session_hash = :session_hash
                  AND owner_id = :owner_id
                  AND revoked_at IS NULL
                """
            ),
            {"now": self._now(now), "session_hash": session_hash, "owner_id": owner_id},
        )

    def revoke_all_for_authenticated_owner(
        self,
        *,
        principal: OwnerSessionRecord,
        now: datetime | None = None,
    ) -> None:
        """The target comes only from the authenticated session, never request data."""
        moment = self._now(now)
        self._lock_owner(principal.owner_id)
        if self.session.get_bind().dialect.name == "postgresql":
            monotonic_epoch = (
                "GREATEST(owner_os_revocations.revoked_before, excluded.revoked_before)"
            )
        else:
            # SQLite's scalar max() is the test-equivalent of PostgreSQL GREATEST().
            monotonic_epoch = (
                "max(owner_os_revocations.revoked_before, excluded.revoked_before)"
            )
        self.session.execute(
            text(
                f"""
                INSERT INTO owner_os_revocations (owner_id, revoked_before)
                VALUES (:owner_id, :revoked_before)
                ON CONFLICT (owner_id) DO UPDATE
                SET revoked_before = {monotonic_epoch}
                """
            ),
            {"owner_id": principal.owner_id, "revoked_before": moment},
        )
        self.session.execute(
            text(
                """
                UPDATE owner_os_sessions
                SET revoked_at = :now
                WHERE owner_id = :owner_id
                  AND revoked_at IS NULL
                """
            ),
            {"now": moment, "owner_id": principal.owner_id},
        )
