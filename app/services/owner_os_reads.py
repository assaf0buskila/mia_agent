"""Authoritative, fail-closed PostgreSQL reads for the private Owner OS API.

This module deliberately does not use the ordinary application session or CRM service.
The configured role is checked before customer rows are read, every transaction is
REPEATABLE READ / READ ONLY, and the transaction is always rolled back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, create_engine, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import CrmActivityRow, CrmContactRow
from app.db.reminders import OwnerReminderRow
from app.db.session import sqlalchemy_database_url

API_VERSION = "owner-os.v1"
TIMEZONE = "Asia/Jerusalem"
MAX_PAGE_SIZE = 50
CURSOR_TTL_SECONDS = 300
STATEMENT_TIMEOUT_MS = 5_000
CONNECT_TIMEOUT_SECONDS = 5
MAX_NEXT_ACTION_SCAN_ROWS = 2_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CURSOR_HEADER = struct.Struct(">BIII6s")
_CURSOR_VERSION = 1
_CURSOR_TAG_BYTES = 12
_ALLOWED_SELECT_TABLES = (
    "crm_contacts",
    "crm_activities",
    "owner_reminders",
    "approvals",
    "idempotency_records",
)


class OwnerOsReadError(RuntimeError):
    """A safe, public-code error from the read boundary."""

    code = "source_unavailable"
    status_code = 503
    retryable = True


class OwnerOsDisabled(OwnerOsReadError):
    code = "capability_unavailable"
    status_code = 501
    retryable = False


class OwnerOsUnsafeDatabase(OwnerOsReadError):
    code = "source_unavailable"


class OwnerOsInvalidQuery(OwnerOsReadError):
    code = "invalid_query"
    status_code = 400
    retryable = False


class OwnerOsCursorInvalid(OwnerOsReadError):
    code = "cursor_invalidated"
    status_code = 409
    retryable = False


class OwnerOsNotFound(OwnerOsReadError):
    code = "record_not_found"
    status_code = 404
    retryable = False


class OwnerOsSourceDataInvalid(OwnerOsReadError):
    code = "source_unavailable"


@dataclass(frozen=True)
class Page:
    items: list[dict[str, object]]
    next_cursor: str | None
    has_more: bool
    page_complete: bool = True


@dataclass(frozen=True)
class CursorBoundary:
    row_id: str
    checksum: int


def _digest(value: str, size: int) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()[:size]


class CursorCodec:
    """Short signed keyset cursor bound to owner, route, filters, order and limit."""

    def __init__(self, secret: str) -> None:
        raw = secret.encode("utf-8")
        if len(raw) < 32:
            raise OwnerOsUnsafeDatabase("owner_os_cursor_secret is too short")
        self._secret = raw

    def encode(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        context: str,
        row_id: str,
        checksum: int,
        now: int | None = None,
    ) -> str:
        if not _SAFE_ID.fullmatch(workspace_id):
            raise OwnerOsInvalidQuery("invalid workspace binding")
        if not _SAFE_ID.fullmatch(row_id) or len(row_id.encode("ascii")) > 64:
            raise OwnerOsSourceDataInvalid("unsafe cursor boundary id")
        expires = (int(time.time()) if now is None else now) + CURSOR_TTL_SECONDS
        header = _CURSOR_HEADER.pack(
            _CURSOR_VERSION,
            expires,
            checksum & 0xFFFFFFFF,
            int.from_bytes(_digest(owner_id, 4), "big"),
            _digest(f"{workspace_id}\0{context}", 6),
        )
        payload = header + row_id.encode("ascii")
        key = hmac.new(
            self._secret, f"owner-os-cursor\0{workspace_id}".encode(), hashlib.sha256
        ).digest()
        tag = hmac.new(key, payload, hashlib.sha256).digest()[:_CURSOR_TAG_BYTES]
        token = base64.urlsafe_b64encode(payload + tag).rstrip(b"=").decode("ascii")
        if len(token) > 128 or not _SAFE_ID.fullmatch(token):
            raise OwnerOsSourceDataInvalid("cursor exceeds frontend contract")
        return token

    def decode(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        context: str,
        token: str,
        now: int | None = None,
    ) -> CursorBoundary:
        if not _SAFE_ID.fullmatch(workspace_id):
            raise OwnerOsInvalidQuery("invalid workspace binding")
        if not _SAFE_ID.fullmatch(token):
            raise OwnerOsInvalidQuery("malformed cursor")
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except (ValueError, TypeError):
            raise OwnerOsInvalidQuery("malformed cursor") from None
        minimum = _CURSOR_HEADER.size + 1 + _CURSOR_TAG_BYTES
        if len(raw) < minimum:
            raise OwnerOsInvalidQuery("malformed cursor")
        payload, supplied = raw[:-_CURSOR_TAG_BYTES], raw[-_CURSOR_TAG_BYTES:]
        key = hmac.new(
            self._secret, f"owner-os-cursor\0{workspace_id}".encode(), hashlib.sha256
        ).digest()
        expected = hmac.new(key, payload, hashlib.sha256).digest()[:_CURSOR_TAG_BYTES]
        if not hmac.compare_digest(supplied, expected):
            raise OwnerOsInvalidQuery("invalid cursor signature")
        version, expires, checksum, owner_digest, context_digest = _CURSOR_HEADER.unpack(
            payload[: _CURSOR_HEADER.size]
        )
        moment = int(time.time()) if now is None else now
        if version != _CURSOR_VERSION or expires < moment:
            raise OwnerOsCursorInvalid("expired cursor")
        if owner_digest != int.from_bytes(_digest(owner_id, 4), "big"):
            raise OwnerOsInvalidQuery("cursor owner mismatch")
        if context_digest != _digest(f"{workspace_id}\0{context}", 6):
            raise OwnerOsInvalidQuery("cursor context mismatch")
        try:
            row_id = payload[_CURSOR_HEADER.size :].decode("ascii")
        except UnicodeDecodeError:
            raise OwnerOsInvalidQuery("malformed cursor") from None
        if not _SAFE_ID.fullmatch(row_id) or len(row_id) > 64:
            raise OwnerOsInvalidQuery("malformed cursor")
        return CursorBoundary(row_id=row_id, checksum=checksum)


def _postgres_url(raw_url: str) -> str:
    scheme = raw_url.strip().split(":", 1)[0].lower()
    if scheme not in {"postgres", "postgresql"} and not scheme.startswith("postgresql+"):
        raise OwnerOsUnsafeDatabase("owner OS reader requires PostgreSQL")
    return sqlalchemy_database_url(raw_url.strip())


def make_owner_os_engine(raw_url: str) -> Engine:
    return create_engine(
        _postgres_url(raw_url),
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        pool_pre_ping=False,
        pool_timeout=CONNECT_TIMEOUT_SECONDS,
    )


def _assert_select_only_role(session: Session) -> None:
    """Reject effective authority beyond exact SELECT use, including PUBLIC grants."""

    role = session.execute(
        text(
            "SELECT r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb, "
            "r.rolcanlogin, r.rolreplication, r.rolbypassrls, "
            "pg_get_userbyid(d.datdba) = current_user AS owns_database, "
            "has_database_privilege(current_user, current_database(), 'CREATE') AS db_create, "
            "has_database_privilege(current_user, current_database(), 'TEMP') AS db_temp "
            "FROM pg_roles r JOIN pg_database d ON d.datname = current_database() "
            "WHERE r.rolname = current_user"
        )
    ).mappings().one()
    forbidden_role_flags = (
        "rolsuper",
        "rolcreaterole",
        "rolcreatedb",
        "rolreplication",
        "rolbypassrls",
        "owns_database",
        "db_create",
        "db_temp",
    )
    if any(bool(role[name]) for name in forbidden_role_flags):
        raise OwnerOsUnsafeDatabase("reader role has broad database authority")

    extra_memberships = session.scalar(
        text(
            "SELECT count(*) FROM pg_roles candidate "
            "WHERE candidate.rolname <> current_user "
            "AND pg_has_role(current_user, candidate.oid, 'MEMBER')"
        )
    )
    if int(extra_memberships or 0):
        raise OwnerOsUnsafeDatabase("reader role can assume another application role")

    allowed_names = ",".join(f"'{name}'" for name in _ALLOWED_SELECT_TABLES)
    unsafe = session.execute(
        text(
            "SELECT "
            "EXISTS (SELECT 1 FROM pg_namespace n "
            " WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            " AND (pg_get_userbyid(n.nspowner) = current_user "
            " OR has_schema_privilege(current_user, n.oid, 'CREATE'))) AS schema_write, "
            "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            " WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            " AND has_schema_privilege(current_user,n.oid,'USAGE') "
            " AND c.relkind IN ('r','p','v','m','f') "
            " AND (pg_get_userbyid(c.relowner)=current_user OR "
            " has_table_privilege(current_user,c.oid,"
            "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))) "
            " AS table_write, "
            "EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            " JOIN pg_namespace n ON n.oid=c.relnamespace "
            " WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            " AND has_schema_privilege(current_user,n.oid,'USAGE') "
            " AND c.relkind IN ('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped "
            " AND has_column_privilege(current_user,c.oid,a.attnum,'INSERT,UPDATE,REFERENCES')) "
            " AS column_write, "
            "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            " WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            " AND has_schema_privilege(current_user,n.oid,'USAGE') "
            " AND c.relkind IN ('r','p','v','m','f') "
            " AND has_table_privilege(current_user,c.oid,'SELECT') "
            f" AND NOT (n.nspname=current_schema() AND c.relname IN ({allowed_names}))) "
            " AS broad_select, "
            "EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            " JOIN pg_namespace n ON n.oid=c.relnamespace "
            " WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            " AND has_schema_privilege(current_user,n.oid,'USAGE') "
            " AND c.relkind IN ('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped "
            " AND has_column_privilege(current_user,c.oid,a.attnum,'SELECT') "
            " AND NOT has_table_privilege(current_user,c.oid,'SELECT') "
            f" AND NOT (n.nspname=current_schema() AND c.relname IN ({allowed_names}))) "
            " AS broad_column_select, "
            "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            " WHERE c.relkind='S' AND n.nspname !~ '^pg_' "
            " AND n.nspname <> 'information_schema' "
            " AND has_schema_privilege(current_user,n.oid,'USAGE') "
            " AND (pg_get_userbyid(c.relowner)=current_user "
            " OR has_sequence_privilege(current_user,c.oid,'USAGE,UPDATE'))) AS sequence_write, "
            "EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            " WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            " AND has_schema_privilege(current_user,n.oid,'USAGE') "
            " AND has_function_privilege(current_user,p.oid,'EXECUTE')) AS callable_side_effect"
        )
    ).mappings().one()
    if any(bool(value) for value in unsafe.values()):
        raise OwnerOsUnsafeDatabase("reader role has an effective write or callable path")

    required = session.execute(
        text(
            "SELECT "
            + ",".join(
                f"has_table_privilege(current_user, '{name}', 'SELECT')"
                for name in _ALLOWED_SELECT_TABLES
            )
        )
    ).one()
    if not all(bool(value) for value in required):
        raise OwnerOsUnsafeDatabase("reader role lacks an exact required SELECT grant")


@contextmanager
def read_snapshot(settings: object) -> Iterator[Session]:
    """Yield one guarded read snapshot and always roll it back."""

    if not bool(getattr(settings, "owner_os_enabled", False)):
        raise OwnerOsDisabled("owner OS is disabled")
    raw_url = str(getattr(settings, "owner_os_read_database_url", "") or "")
    engine = make_owner_os_engine(raw_url)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        # Must precede every query in this session.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        _assert_select_only_role(session)
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _safe_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise OwnerOsSourceDataInvalid(f"invalid {field}")
    return value


def _bounded(
    value: object, maximum: int, field: str, *, count_utf16_units: bool = True
) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or any(ord(char) < 9 for char in value):
        raise OwnerOsSourceDataInvalid(f"invalid {field}")
    try:
        utf16_units = len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise OwnerOsSourceDataInvalid(f"invalid {field}") from None
    measured_length = utf16_units if count_utf16_units else len(value)
    if measured_length > maximum:
        raise OwnerOsSourceDataInvalid(f"invalid {field}")
    return value


def _utf16_excerpt(value: str, maximum_units: int, field: str) -> tuple[str, bool]:
    """Bound presentation like JavaScript String.length without splitting a code point."""

    try:
        encoded = value.encode("utf-16-le")
    except UnicodeEncodeError:
        raise OwnerOsSourceDataInvalid(f"invalid {field}") from None
    if len(encoded) // 2 <= maximum_units:
        return value, False
    prefix: list[str] = []
    used_units = 0
    prefix_budget = maximum_units - 1  # U+2026 is one UTF-16 code unit.
    for character in value:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if used_units + character_units > prefix_budget:
            break
        prefix.append(character)
        used_units += character_units
    return "".join(prefix) + "…", True


def _utc_string(value: object, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise OwnerOsSourceDataInvalid(f"invalid {field}") from None
    else:
        raise OwnerOsSourceDataInvalid(f"invalid {field}")
    if parsed.tzinfo is None:
        raise OwnerOsSourceDataInvalid(f"naive {field}")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fields(row: CrmContactRow) -> Mapping[str, object]:
    try:
        value = json.loads(row.fields_json or "{}")
    except (TypeError, json.JSONDecodeError):
        raise OwnerOsSourceDataInvalid("invalid contact fields") from None
    if not isinstance(value, dict):
        raise OwnerOsSourceDataInvalid("invalid contact fields")
    return value


def _contact_checksum(row: CrmContactRow) -> int:
    return zlib.crc32(f"{row.updated_at}\0{row.revision}".encode())


def _activity_checksum(row: CrmActivityRow) -> int:
    return zlib.crc32(f"{row.occurred_at}\0{row.contact_id}".encode())


def _reminder_checksum(row: OwnerReminderRow) -> int:
    return zlib.crc32(
        f"{_utc_string(row.scheduled_at, 'scheduled_at')}\0{row.revision}\0{row.status}".encode()
    )


def _contact_dto(row: CrmContactRow, *, detail: bool = False) -> dict[str, object]:
    contact_id = _safe_id(row.id, "contact_id")
    if not isinstance(row.revision, int) or row.revision < 1:
        raise OwnerOsSourceDataInvalid("invalid contact revision")
    values = _fields(row)
    name = _bounded(values.get("name"), 160, "name")
    phone = _bounded(values.get("phone"), 40, "phone")
    email = _bounded(values.get("email"), 320, "email")
    business = _bounded(values.get("business"), 160, "business")
    source = _bounded(values.get("source"), 160, "source")
    status = _bounded(values.get("status"), 160, "status")
    next_step = _bounded(values.get("next_step"), 2_000, "next_step")
    legacy_date = _bounded(values.get("date"), 64, "date")
    identity_fields = (("name", name), ("phone", phone), ("email", email))
    missing = [key for key, value in identity_fields if value is None]
    reasons = ["business_classification_not_collected", "watermark_missing"]
    if legacy_date:
        reasons.append("legacy_date_semantics_unverified")
    result: dict[str, object] = {
        "contact_id": contact_id,
        "revision": row.revision,
        "name": name,
        "business": business,
        "phone": phone,
        "email": email,
        "updated_at": _utc_string(row.updated_at, "updated_at"),
        "source": {
            "source_id": "crm_contacts",
            "recorded_label": source,
            "evidence_key": None,
            "attribution_state": "unverified" if source else "unknown",
        },
        "status": {"recorded_value": status, "qualification": None, "pipeline_stage": None},
        "next_action": {
            "text": next_step,
            "due_date": None,
            "due_at": None,
            "precision": "none",
            "timezone": TIMEZONE,
            "state": "recorded" if next_step else "not_collected",
        },
        "record_quality": {
            "stored_read": "verified",
            "business_classification": "not_collected",
            "source_coverage": "unknown",
            "missing_fields": missing,
            "reason_codes": reasons,
        },
    }
    if detail:
        result["need"] = _bounded(values.get("want"), 1_000, "need")
        result["summary"] = _bounded(values.get("summary"), 1_000, "summary")
    return result


def _next_action_dto(row: CrmContactRow) -> dict[str, object]:
    values = _fields(row)
    next_step = _bounded(values.get("next_step"), 2_000, "next_step")
    if not next_step or not next_step.strip():
        raise OwnerOsSourceDataInvalid("next action row is blank")
    contact_id = _safe_id(row.id, "contact_id")
    legacy_date = _bounded(values.get("date"), 64, "date")
    due_reasons = ["legacy_date_semantics_unverified"] if legacy_date else ["due_not_collected"]
    quality_reasons = ["watermark_missing", *due_reasons]
    return {
        "item_id": f"contact_next_action:{contact_id}",
        "kind": "contact_next_action",
        "contact_id": contact_id,
        "revision": row.revision,
        "contact_display_name": _bounded(values.get("name"), 160, "name"),
        "text": next_step,
        "updated_at": _utc_string(row.updated_at, "updated_at"),
        "due": {
            "date": None,
            "at": None,
            "precision": "none",
            "timezone": TIMEZONE,
            "semantic_state": "unverified" if legacy_date else "not_collected",
            "reason_codes": due_reasons,
        },
        "due_state": "unknown",
        "source": {"source_id": "crm_contacts", "field": "next_step", "evidence_key": None},
        "record_quality": {
            "stored_read": "verified",
            "source_coverage": "unknown",
            "reason_codes": quality_reasons,
        },
    }


def _activity_dto(row: CrmActivityRow) -> dict[str, object]:
    return {
        "activity_id": _safe_id(row.id, "activity_id"),
        "contact_id": _safe_id(row.contact_id, "contact_id"),
        "occurred_at": _utc_string(row.occurred_at, "occurred_at"),
        "channel": _bounded(row.channel, 32, "channel"),
        "action": _bounded(row.action, 2_000, "action"),
        "result": _bounded(row.result, 2_000, "result"),
        "revision": None,
        "immutable_record": "unknown",
    }


def _reminder_dto(row: OwnerReminderRow) -> dict[str, object]:
    if row.status not in {"active", "completed", "cancelled"}:
        raise OwnerOsSourceDataInvalid("invalid reminder status")
    if row.delivery_state not in {"pending", "dispatching", "accepted", "rejected", "unknown"}:
        raise OwnerOsSourceDataInvalid("invalid reminder delivery state")
    if row.timezone != TIMEZONE or not isinstance(row.revision, int) or row.revision < 1:
        raise OwnerOsSourceDataInvalid("invalid reminder metadata")
    reminder_id = _safe_id(row.id, "reminder_id")
    contact_id = None if row.contact_id is None else _safe_id(row.contact_id, "contact_id")
    reminder_text = _bounded(
        row.text, 2_800, "reminder text", count_utf16_units=False
    )
    if reminder_text is None:
        raise OwnerOsSourceDataInvalid("blank reminder text")
    presented_text, truncated = _utf16_excerpt(reminder_text, 500, "reminder text")
    result: dict[str, object] = {
        "reminder_id": reminder_id,
        "revision": row.revision,
        "text": presented_text,
        "contact_id": contact_id,
        "scheduled_at": _utc_string(row.scheduled_at, "scheduled_at"),
        "timezone": TIMEZONE,
        "status": row.status,
        "delivery_state": row.delivery_state,
    }
    if truncated:
        result.update(
            {
                "text_truncated": True,
                "text_presentation": "excerpt",
                "full_text_available": False,
                "reason_codes": ["reminder_text_excerpt"],
            }
        )
    return result


class OwnerOsReader:
    def __init__(
        self, session: Session, *, owner_id: str, workspace_id: str, cursor_secret: str
    ) -> None:
        if not owner_id.isascii() or not owner_id.isdecimal():
            raise OwnerOsInvalidQuery("invalid owner binding")
        if not _SAFE_ID.fullmatch(workspace_id):
            raise OwnerOsInvalidQuery("invalid workspace binding")
        self.session = session
        self.owner_id = owner_id
        self.workspace_id = workspace_id
        self.cursor = CursorCodec(cursor_secret)
        observed = session.scalar(text("SELECT transaction_timestamp()"))
        self.observed_at = _utc_string(observed, "observed_at")

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise OwnerOsInvalidQuery("limit outside 1..50")
        return limit

    def _contact_boundary(self, token: str | None, context: str) -> CrmContactRow | None:
        if token is None:
            return None
        boundary = self.cursor.decode(
            owner_id=self.owner_id,
            workspace_id=self.workspace_id,
            context=context,
            token=token,
        )
        row = self.session.get(CrmContactRow, boundary.row_id)
        if row is None or _contact_checksum(row) != boundary.checksum:
            raise OwnerOsCursorInvalid("contact boundary changed")
        return row

    def list_contacts(self, *, limit: int = 20, cursor: str | None = None) -> Page:
        limit = self._limit(limit)
        context = f"contacts|updated_at_desc_id_desc|limit={limit}"
        boundary = self._contact_boundary(cursor, context)
        statement = select(CrmContactRow).order_by(
            CrmContactRow.updated_at.desc(), CrmContactRow.id.desc()
        )
        if boundary is not None:
            statement = statement.where(
                or_(
                    CrmContactRow.updated_at < boundary.updated_at,
                    and_(
                        CrmContactRow.updated_at == boundary.updated_at,
                        CrmContactRow.id < boundary.id,
                    ),
                )
            )
        rows = list(self.session.scalars(statement.limit(limit + 1)).all())
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = self.cursor.encode(
                owner_id=self.owner_id,
                workspace_id=self.workspace_id,
                context=context,
                row_id=last.id,
                checksum=_contact_checksum(last),
            )
        return Page([_contact_dto(row) for row in page_rows], next_cursor, has_more)

    def get_contact(self, contact_id: str) -> dict[str, object]:
        contact_id = _safe_id(contact_id, "contact_id")
        row = self.session.get(CrmContactRow, contact_id)
        if row is None:
            raise OwnerOsNotFound("contact not found")
        return _contact_dto(row, detail=True)

    def list_next_actions(self, *, limit: int = 20, cursor: str | None = None) -> Page:
        limit = self._limit(limit)
        context = f"next_actions|updated_at_desc_id_desc|limit={limit}"
        boundary = self._contact_boundary(cursor, context)
        found: list[CrmContactRow] = []
        scan_boundary = boundary
        scanned = 0
        scan_capped = False
        while len(found) <= limit:
            statement = select(CrmContactRow).order_by(
                CrmContactRow.updated_at.desc(), CrmContactRow.id.desc()
            )
            if scan_boundary is not None:
                statement = statement.where(
                    or_(
                        CrmContactRow.updated_at < scan_boundary.updated_at,
                        and_(
                            CrmContactRow.updated_at == scan_boundary.updated_at,
                            CrmContactRow.id < scan_boundary.id,
                        ),
                    )
                )
            chunk = list(self.session.scalars(statement.limit(200)).all())
            if not chunk:
                break
            scanned += len(chunk)
            for row in chunk:
                value = _bounded(_fields(row).get("next_step"), 2_000, "next_step")
                if value and value.strip():
                    found.append(row)
                    if len(found) > limit:
                        break
            if len(found) > limit or len(chunk) < 200:
                break
            scan_boundary = chunk[-1]
            if scanned >= MAX_NEXT_ACTION_SCAN_ROWS:
                scan_capped = True
                break
        has_more = len(found) > limit or scan_capped
        page_rows = found[:limit]
        next_cursor = None
        cursor_row = page_rows[-1] if len(found) > limit and page_rows else scan_boundary
        if has_more and cursor_row is not None:
            next_cursor = self.cursor.encode(
                owner_id=self.owner_id,
                workspace_id=self.workspace_id,
                context=context,
                row_id=cursor_row.id,
                checksum=_contact_checksum(cursor_row),
            )
        return Page([_next_action_dto(row) for row in page_rows], next_cursor, has_more)

    def list_activities(
        self, *, contact_id: str, limit: int = 20, cursor: str | None = None
    ) -> Page:
        contact_id = _safe_id(contact_id, "contact_id")
        if self.session.get(CrmContactRow, contact_id) is None:
            raise OwnerOsNotFound("contact not found")
        limit = self._limit(limit)
        context = f"activities|contact={contact_id}|occurred_at_desc_id_desc|limit={limit}"
        boundary_row: CrmActivityRow | None = None
        if cursor is not None:
            boundary = self.cursor.decode(
                owner_id=self.owner_id,
                workspace_id=self.workspace_id,
                context=context,
                token=cursor,
            )
            boundary_row = self.session.get(CrmActivityRow, boundary.row_id)
            if (
                boundary_row is None
                or boundary_row.contact_id != contact_id
                or _activity_checksum(boundary_row) != boundary.checksum
            ):
                raise OwnerOsCursorInvalid("activity boundary changed")
        statement = (
            select(CrmActivityRow)
            .where(CrmActivityRow.contact_id == contact_id)
            .order_by(CrmActivityRow.occurred_at.desc(), CrmActivityRow.id.desc())
        )
        if boundary_row is not None:
            statement = statement.where(
                or_(
                    CrmActivityRow.occurred_at < boundary_row.occurred_at,
                    and_(
                        CrmActivityRow.occurred_at == boundary_row.occurred_at,
                        CrmActivityRow.id < boundary_row.id,
                    ),
                )
            )
        rows = list(self.session.scalars(statement.limit(limit + 1)).all())
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = self.cursor.encode(
                owner_id=self.owner_id,
                workspace_id=self.workspace_id,
                context=context,
                row_id=last.id,
                checksum=_activity_checksum(last),
            )
        return Page([_activity_dto(row) for row in page_rows], next_cursor, has_more)

    def list_reminders(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        status: str = "active",
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> Page:
        limit = self._limit(limit)
        if status not in {"active", "completed", "cancelled"}:
            raise OwnerOsInvalidQuery("invalid reminder status")
        for value in (from_at, to_at):
            if value is not None and value.tzinfo is None:
                raise OwnerOsInvalidQuery("reminder bounds must be timezone-aware")
        if from_at is not None and to_at is not None and from_at >= to_at:
            raise OwnerOsInvalidQuery("invalid reminder range")
        normalized_from = from_at.astimezone(UTC).isoformat() if from_at else ""
        normalized_to = to_at.astimezone(UTC).isoformat() if to_at else ""
        context = (
            f"reminders|status={status}|from={normalized_from}|to={normalized_to}"
            f"|scheduled_at_asc_id_asc|limit={limit}"
        )
        boundary_row: OwnerReminderRow | None = None
        if cursor is not None:
            boundary = self.cursor.decode(
                owner_id=self.owner_id,
                workspace_id=self.workspace_id,
                context=context,
                token=cursor,
            )
            boundary_row = self.session.get(OwnerReminderRow, boundary.row_id)
            if (
                boundary_row is None
                or boundary_row.owner_id != self.owner_id
                or _reminder_checksum(boundary_row) != boundary.checksum
            ):
                raise OwnerOsCursorInvalid("reminder boundary changed")
        statement = (
            select(OwnerReminderRow)
            .where(
                OwnerReminderRow.owner_id == self.owner_id,
                OwnerReminderRow.status == status,
            )
            .order_by(OwnerReminderRow.scheduled_at.asc(), OwnerReminderRow.id.asc())
        )
        if from_at is not None:
            statement = statement.where(OwnerReminderRow.scheduled_at >= from_at.astimezone(UTC))
        if to_at is not None:
            statement = statement.where(OwnerReminderRow.scheduled_at < to_at.astimezone(UTC))
        if boundary_row is not None:
            statement = statement.where(
                or_(
                    OwnerReminderRow.scheduled_at > boundary_row.scheduled_at,
                    and_(
                        OwnerReminderRow.scheduled_at == boundary_row.scheduled_at,
                        OwnerReminderRow.id > boundary_row.id,
                    ),
                )
            )
        rows = list(self.session.scalars(statement.limit(limit + 1)).all())
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = self.cursor.encode(
                owner_id=self.owner_id,
                workspace_id=self.workspace_id,
                context=context,
                row_id=last.id,
                checksum=_reminder_checksum(last),
            )
        return Page([_reminder_dto(row) for row in page_rows], next_cursor, has_more)


def pagination(page: Page) -> dict[str, object]:
    return {
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
        "page_complete": page.page_complete,
    }


def section(page: Page, *, coverage_scope: str, first_page: bool) -> dict[str, object]:
    coverage = "complete" if first_page and not page.has_more else "partial"
    item_reasons = {
        reason
        for item in page.items
        for reason in item.get("reason_codes", [])
        if isinstance(reason, str)
    }
    reasons = ([] if coverage == "complete" else ["paginated_snapshot"]) + sorted(item_reasons)
    return {
        "section_state": "available",
        "items": page.items,
        "coverage": coverage,
        "coverage_scope": coverage_scope,
        "freshness": "fresh",
        "reason_codes": reasons,
        "pagination": pagination(page),
    }


def envelope(
    reader: OwnerOsReader,
    *,
    data: object,
    coverage: str,
    coverage_scope: str,
    reason_codes: Sequence[str] = (),
    capabilities: Sequence[dict[str, object]] = (),
    pagination_value: dict[str, object] | None = None,
) -> dict[str, object]:
    request_id = "req_" + hashlib.sha256(f"{time.time_ns()}:{id(reader)}".encode()).hexdigest()[:24]
    snapshot_id = "snap_" + hashlib.sha256(
        f"{reader.owner_id}:{reader.observed_at}:{time.time_ns()}".encode()
    ).hexdigest()[:24]
    return {
        "api_version": API_VERSION,
        "synthetic": False,
        "source": "mia_postgresql",
        "owner_scope": "self",
        "request_id": request_id,
        "snapshot_id": snapshot_id,
        "observed_at": reader.observed_at,
        "coverage": coverage,
        "coverage_scope": coverage_scope,
        "freshness": "fresh",
        "reason_codes": list(reason_codes),
        "source_statuses": [],
        "capabilities": list(capabilities),
        "data": data,
        "pagination": pagination_value,
    }


def read_capability(name: str) -> dict[str, object]:
    return {"capability_id": name, "availability": "ready", "reason_codes": []}


def blocked_capability(name: str, reason: str) -> dict[str, object]:
    return {"capability_id": name, "availability": "blocked", "reason_codes": [reason]}
