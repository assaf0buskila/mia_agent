"""Opaque, server-side owner web sessions.

Only hashes of browser credentials are stored.  The API boundary owns Telegram
proof verification and the response cookie; this module owns the transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# V2-08 bounded sliding sessions. The settings carry the same bounds.
MIN_IDLE_SECONDS = 300
MAX_IDLE_SECONDS = 12 * 60 * 60
MIN_ABSOLUTE_SECONDS = 60 * 60
MAX_ABSOLUTE_SECONDS = 14 * 24 * 60 * 60
# A rotated token stays valid this long for requests already in flight.
GRACE_SECONDS = 60
# A session becomes due for renewal this long after it was issued.
RENEW_AGE_SECONDS = 900
SESSION_TOKEN_BYTES = 32
CHALLENGE_TOKEN_BYTES = 32
CHAIN_ID_BYTES = 16

_logger = logging.getLogger(__name__)
_SESSION_COLUMNS = (
    "owner_id, workspace_id, login_proof_hash, scopes, expires_at, revoked_at, created_at, "
    "chain_id, chain_started_at, authenticated_at, first_used_at, rotated_at, grace_until, "
    "successor_hash"
)
_PREFIXED_SESSION_COLUMNS = ", ".join(
    "s." + column.strip() for column in _SESSION_COLUMNS.split(",")
)


class OwnerSessionDenied(PermissionError):
    """An opaque credential was absent, expired, revoked, or already consumed."""


class OwnerSessionReplay(OwnerSessionDenied):
    """A login proof or pre-authentication challenge was already consumed."""


class OwnerSessionRateLimited(OwnerSessionDenied):
    """The durable global auth-attempt budget was exhausted."""


class OwnerSessionChainReuse(OwnerSessionDenied):
    """A replaced token was presented. Its whole chain is already revoked and committed."""


@dataclass(frozen=True)
class OwnerSessionRecord:
    owner_id: str
    scopes: frozenset[str]
    session_hash: str
    expires_at: datetime
    workspace_id: str
    login_proof_hash: str
    created_at: datetime
    # min(created_at + RENEW_AGE, expires_at), or the check time on a rotated row.
    renew_after: datetime
    # NULL on a legacy (pre-V2-08) row, which can never be renewed.
    chain_id: str | None
    chain_started_at: datetime | None
    authenticated_at: datetime | None
    rotated_at: datetime | None


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


def _optional_utc(value: Any) -> datetime | None:
    return None if value is None else _utc(value)


def _renew_after(
    *,
    created_at: datetime,
    expires_at: datetime,
    rotated_at: datetime | None,
    moment: datetime,
) -> datetime:
    if rotated_at is not None:
        # A rotated predecessor (lost response) is due at once.
        return moment
    return min(created_at + timedelta(seconds=RENEW_AGE_SECONDS), expires_at)


def _record(
    row: Mapping[str, Any],
    *,
    session_hash: str,
    moment: datetime,
) -> OwnerSessionRecord:
    created_at = _utc(row["created_at"])
    expires_at = _utc(row["expires_at"])
    rotated_at = _optional_utc(row["rotated_at"])
    chain_id = row["chain_id"]
    return OwnerSessionRecord(
        owner_id=str(row["owner_id"]),
        scopes=_parse_scopes(str(row["scopes"])),
        session_hash=session_hash,
        expires_at=expires_at,
        workspace_id=str(row["workspace_id"]),
        login_proof_hash=str(row["login_proof_hash"]),
        created_at=created_at,
        renew_after=_renew_after(
            created_at=created_at, expires_at=expires_at, rotated_at=rotated_at, moment=moment
        ),
        chain_id=None if chain_id is None else str(chain_id),
        chain_started_at=_optional_utc(row["chain_started_at"]),
        authenticated_at=_optional_utc(row["authenticated_at"]),
        rotated_at=rotated_at,
    )


def _predates_revocation(row: Mapping[str, Any]) -> bool:
    """True when the owner's revoked_before is at or after the row's own timestamps.

    Every row is checked by created_at (a legacy row has nothing else). A chain row is also
    checked by chain_started_at and authenticated_at, so no successor outlives an epoch that
    its chain or proof predates.
    """
    epoch = _optional_utc(row["revoked_before"])
    if epoch is None:
        return False
    moments = [row["created_at"], row["chain_started_at"], row["authenticated_at"]]
    return any(_utc(value) <= epoch for value in moments if value is not None)


def _valid_windows(idle_seconds: int, absolute_seconds: int) -> bool:
    return (
        MIN_IDLE_SECONDS <= idle_seconds <= MAX_IDLE_SECONDS
        and MIN_ABSOLUTE_SECONDS <= absolute_seconds <= MAX_ABSOLUTE_SECONDS
        and absolute_seconds >= idle_seconds
    )


class OwnerSessionStore:
    """Transaction-scoped store. Callers must commit before issuing credentials.

    One exception: a presented token its browser had already replaced revokes the whole chain
    and commits that revocation itself, so no caller's rollback can undo it.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        # Set by authenticate() when it wrote first_used_at. require_owner_session commits it.
        self.first_use_recorded = False

    def _is_postgres(self) -> bool:
        return self.session.get_bind().dialect.name == "postgresql"

    def _now(self, supplied: datetime | None) -> datetime:
        if supplied is not None:
            return _utc(supplied)
        return _utc(self.session.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one())

    def _clock(self, supplied: datetime | None) -> datetime:
        """The wall clock after the owner lock, not the transaction's start time."""
        if supplied is not None or not self._is_postgres():
            return self._now(supplied)
        return _utc(self.session.execute(text("SELECT clock_timestamp()")).scalar_one())

    def _session_row(self, session_hash: str, *, for_update: bool = False):
        lock = " FOR UPDATE" if for_update and self._is_postgres() else ""
        return self.session.execute(
            text(
                f"SELECT {_SESSION_COLUMNS} FROM owner_os_sessions "
                f"WHERE session_hash = :session_hash{lock}"
            ),
            {"session_hash": session_hash},
        ).mappings().first()

    def _revoke_chain(
        self,
        *,
        session_hash: str,
        chain_id: str | None,
        owner_id: str,
        moment: datetime,
    ) -> None:
        """Revoke every row of the chain, or only the row itself when it is legacy."""
        if chain_id is None:
            condition, values = "session_hash = :key", {"key": session_hash}
        else:
            condition, values = "chain_id = :key", {"key": chain_id}
        self.session.execute(
            text(
                f"""
                UPDATE owner_os_sessions
                SET revoked_at = :now
                WHERE owner_id = :owner_id
                  AND revoked_at IS NULL
                  AND {condition}
                """
            ),
            {"now": moment, "owner_id": owner_id, **values},
        )

    def _deny_chain_reuse(
        self,
        *,
        session_hash: str,
        chain_id: str | None,
        owner_id: str,
        moment: datetime,
    ) -> OwnerSessionChainReuse:
        """Revoke and commit the whole chain, then return the denial for the caller to raise.

        The owner lock serializes this with renew(), so a successor inserted concurrently is
        committed (and therefore revoked here) before this UPDATE reads the chain.
        """
        self._lock_owner(owner_id)
        self._revoke_chain(
            session_hash=session_hash, chain_id=chain_id, owner_id=owner_id, moment=moment
        )
        self.session.commit()
        _logger.warning("owner_auth reason=session_chain_reuse")
        return OwnerSessionChainReuse("session chain reuse")

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
        """Consume challenge + proof, revoke the prior cookie's chain, and start a new chain.

        ``ttl_seconds`` is the idle window: the first row of a chain expires at now + idle.
        """
        if not owner_id.isascii() or not owner_id.isdigit() or not workspace_id or not scopes:
            raise OwnerSessionDenied("invalid owner session")
        moment = self._now(now)
        ttl = max(1, min(ttl_seconds, MAX_IDLE_SECONDS))
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
            self._revoke_chain_of(
                credential_hash(previous_cookie, purpose="session"),
                owner_id=owner_id,
                moment=moment,
            )

        cookie_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        hashed = credential_hash(cookie_token, purpose="session")
        expires_at = moment + timedelta(seconds=ttl)
        chain_id = secrets.token_hex(CHAIN_ID_BYTES)
        authenticated_at = _utc(proof_authenticated_at)
        self.session.execute(
            text(
                """
                INSERT INTO owner_os_sessions
                    (session_hash, owner_id, workspace_id, login_proof_hash, scopes,
                     expires_at, revoked_at, created_at, chain_id, chain_started_at,
                     authenticated_at)
                VALUES
                    (:session_hash, :owner_id, :workspace_id, :login_proof_hash, :scopes,
                     :expires_at, NULL, :created_at, :chain_id, :chain_started_at,
                     :authenticated_at)
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
                "chain_id": chain_id,
                "chain_started_at": moment,
                "authenticated_at": authenticated_at,
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
                created_at=moment,
                renew_after=_renew_after(
                    created_at=moment, expires_at=expires_at, rotated_at=None, moment=moment
                ),
                chain_id=chain_id,
                chain_started_at=moment,
                authenticated_at=authenticated_at,
                rotated_at=None,
            ),
        )

    def _revoke_chain_of(self, session_hash: str, *, owner_id: str, moment: datetime) -> None:
        """Revoke the whole chain of one presented row (or just the row when it is legacy)."""
        found = self.session.execute(
            text(
                """
                SELECT chain_id
                FROM owner_os_sessions
                WHERE session_hash = :session_hash
                  AND owner_id = :owner_id
                """
            ),
            {"session_hash": session_hash, "owner_id": owner_id},
        ).first()
        if found is None:
            return
        self._revoke_chain(
            session_hash=session_hash, chain_id=found[0], owner_id=owner_id, moment=moment
        )

    def authenticate(
        self,
        cookie_token: str,
        *,
        now: datetime | None = None,
    ) -> OwnerSessionRecord:
        """Contract rules, on every route: select by hash with no state filter, then
        1. a missing or revoked row is denied,
        1a. (erratum 2) a row created at or before the owner's revoked_before is denied, and a
            chain row also when chain_started_at or authenticated_at is at or before it,
        2. a rotated row at or past its grace revokes (and commits) its whole chain, denied,
        3. a row at or past expires_at is denied,
        4. otherwise allowed, and first_used_at is set once (the caller commits it).
        """
        self.first_use_recorded = False
        if not cookie_token or len(cookie_token) > 256:
            raise OwnerSessionDenied("session required")
        moment = self._now(now)
        hashed = credential_hash(cookie_token, purpose="session")
        row = self.session.execute(
            text(
                f"SELECT {_PREFIXED_SESSION_COLUMNS}, r.revoked_before "
                "FROM owner_os_sessions s "
                "LEFT JOIN owner_os_revocations r ON r.owner_id = s.owner_id "
                "WHERE s.session_hash = :session_hash"
            ),
            {"session_hash": hashed},
        ).mappings().first()
        if row is None or row["revoked_at"] is not None:
            raise OwnerSessionDenied("session rejected")
        if _predates_revocation(row):
            # Revoke-all, or a restore that moved the epoch: no write, just refuse.
            raise OwnerSessionDenied("session rejected")
        if row["rotated_at"] is not None:
            grace_until = _optional_utc(row["grace_until"])
            if grace_until is None or moment >= grace_until:
                raise self._deny_chain_reuse(
                    session_hash=hashed,
                    chain_id=row["chain_id"],
                    owner_id=str(row["owner_id"]),
                    moment=moment,
                )
        if moment >= _utc(row["expires_at"]):
            raise OwnerSessionDenied("session rejected")
        if row["first_used_at"] is None:
            self.session.execute(
                text(
                    """
                    UPDATE owner_os_sessions
                    SET first_used_at = :now
                    WHERE session_hash = :session_hash
                      AND first_used_at IS NULL
                    """
                ),
                {"now": moment, "session_hash": hashed},
            )
            self.first_use_recorded = True
        return _record(row, session_hash=hashed, moment=moment)

    def revoke_current_chain(
        self,
        *,
        session_hash: str,
        owner_id: str,
        now: datetime | None = None,
    ) -> None:
        """Logout: revoke the presented row's whole chain, a predecessor in grace included."""
        moment = self._now(now)
        # Serialize with renew(): a successor it commits first is revoked here as well.
        self._lock_owner(owner_id)
        self._revoke_chain_of(session_hash, owner_id=owner_id, moment=moment)

    def renew(
        self,
        cookie_token: str,
        *,
        idle_seconds: int,
        absolute_seconds: int,
        authorize: Callable[[OwnerSessionRecord], object],
        now: datetime | None = None,
    ) -> IssuedSession:
        """Rotate the presented session into a successor of the same chain.

        ``authorize`` re-checks the numeric owner allow-list and the workspace for the locked
        row and raises to deny. The caller commits before it sends the new cookie.
        """
        if not cookie_token or len(cookie_token) > 256:
            raise OwnerSessionDenied("session required")
        if not _valid_windows(idle_seconds, absolute_seconds):
            raise OwnerSessionDenied("invalid session window")
        hashed = credential_hash(cookie_token, purpose="session")
        owner = self.session.execute(
            text("SELECT owner_id FROM owner_os_sessions WHERE session_hash = :session_hash"),
            {"session_hash": hashed},
        ).first()
        if owner is None:
            raise OwnerSessionDenied("session rejected")
        self._lock_owner(str(owner[0]))
        moment = self._clock(now)
        row = self._session_row(hashed, for_update=True)
        if row is None or str(row["owner_id"]) != str(owner[0]) or row["revoked_at"] is not None:
            raise OwnerSessionDenied("session rejected")
        if row["chain_id"] is None:
            raise OwnerSessionDenied("legacy session cannot be renewed")
        owner_id = str(row["owner_id"])
        chain_id = str(row["chain_id"])
        rotated_at = _optional_utc(row["rotated_at"])
        grace_until = _optional_utc(row["grace_until"])
        # Reuse detection comes first: a rotated row's expires_at is at most its grace_until,
        # so the expiry check below would otherwise hide a token presented after its grace.
        if rotated_at is not None and (grace_until is None or moment >= grace_until):
            raise self._deny_chain_reuse(
                session_hash=hashed, chain_id=chain_id, owner_id=owner_id, moment=moment
            )
        chain_started_at = _utc(row["chain_started_at"])
        authenticated_at = _utc(row["authenticated_at"])
        chain_cap = chain_started_at + timedelta(seconds=absolute_seconds)
        revoked_before = self._revoked_before(owner_id)
        if (
            moment >= _utc(row["expires_at"])
            or moment >= chain_cap
            or chain_started_at <= revoked_before
            or authenticated_at <= revoked_before
        ):
            raise OwnerSessionDenied("session rejected")
        authorize(_record(row, session_hash=hashed, moment=moment))

        if rotated_at is not None:
            self._retire_unused_successor(
                row, session_hash=hashed, chain_id=chain_id, owner_id=owner_id, moment=moment
            )

        new_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        successor = credential_hash(new_token, purpose="session")
        expires_at = min(moment + timedelta(seconds=idle_seconds), chain_cap)
        # INSERT ... SELECT copies identity, proof and the scopes text byte for byte, so a
        # renewal can never re-derive or widen what the login granted.
        inserted = self.session.execute(
            text(
                """
                INSERT INTO owner_os_sessions
                    (session_hash, owner_id, workspace_id, login_proof_hash, scopes,
                     expires_at, revoked_at, created_at, chain_id, chain_started_at,
                     authenticated_at)
                SELECT :successor, owner_id, workspace_id, login_proof_hash, scopes,
                       :expires_at, NULL, :now, chain_id, chain_started_at, authenticated_at
                FROM owner_os_sessions
                WHERE session_hash = :presented
                """
            ),
            {
                "successor": successor,
                "expires_at": expires_at,
                "now": moment,
                "presented": hashed,
            },
        )
        if inserted.rowcount != 1:
            raise OwnerSessionDenied("session rejected")
        # The row is locked, so these equal COALESCE(rotated_at, now),
        # COALESCE(grace_until, now + GRACE) and LEAST(expires_at, grace_until).
        kept_rotated_at = rotated_at or moment
        kept_grace_until = grace_until or moment + timedelta(seconds=GRACE_SECONDS)
        self.session.execute(
            text(
                """
                UPDATE owner_os_sessions
                SET rotated_at = :rotated_at,
                    grace_until = :grace_until,
                    expires_at = :expires_at,
                    successor_hash = :successor
                WHERE session_hash = :presented
                """
            ),
            {
                "rotated_at": kept_rotated_at,
                "grace_until": kept_grace_until,
                "expires_at": min(_utc(row["expires_at"]), kept_grace_until),
                "successor": successor,
                "presented": hashed,
            },
        )
        return IssuedSession(
            cookie_token=new_token,
            record=OwnerSessionRecord(
                owner_id=owner_id,
                scopes=_parse_scopes(str(row["scopes"])),
                session_hash=successor,
                expires_at=expires_at,
                workspace_id=str(row["workspace_id"]),
                login_proof_hash=str(row["login_proof_hash"]),
                created_at=moment,
                renew_after=_renew_after(
                    created_at=moment, expires_at=expires_at, rotated_at=None, moment=moment
                ),
                chain_id=chain_id,
                chain_started_at=chain_started_at,
                authenticated_at=authenticated_at,
                rotated_at=None,
            ),
        )

    def _retire_unused_successor(
        self,
        row: Mapping[str, Any],
        *,
        session_hash: str,
        chain_id: str,
        owner_id: str,
        moment: datetime,
    ) -> None:
        """A rotated row inside its grace: a lost response may retry, a replaced token may not."""
        successor_hash = row["successor_hash"]
        successor = (
            None
            if successor_hash is None
            else self._session_row(str(successor_hash), for_update=True)
        )
        if (
            successor is None
            or successor["chain_id"] != chain_id
            or successor["first_used_at"] is not None
            or successor["rotated_at"] is not None
        ):
            # The browser had already replaced this token (or the chain is inconsistent).
            raise self._deny_chain_reuse(
                session_hash=session_hash, chain_id=chain_id, owner_id=owner_id, moment=moment
            )
        self.session.execute(
            text(
                """
                UPDATE owner_os_sessions
                SET rotated_at = :now,
                    grace_until = :now,
                    expires_at = :expires_at
                WHERE session_hash = :successor
                """
            ),
            {
                "now": moment,
                "expires_at": min(_utc(successor["expires_at"]), moment),
                "successor": str(successor_hash),
            },
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
