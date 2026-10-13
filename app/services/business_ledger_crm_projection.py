"""Synthetic TEST-only CRM projection derived from canonical Owner OS read DTOs.

This module has no transport, database, scheduler, or provider integration.  Its caller
must obtain an authenticated Owner OS contacts envelope and may pass only a synthetic
test envelope.  The resulting payload is deliberately smaller than the source DTO: it
cannot carry phone, email, visitor text, business details, or financial data.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

API_VERSION = "owner-os.crm-projection.v1"
OWNER_OS_API_VERSION = "owner-os.v1"
MODE = "TEST"
SOURCE = "mia_postgresql"
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_CONTACTS = 100
MAX_AGE_SECONDS = 300


class CrmProjectionInvalid(ValueError):
    """The source cannot truthfully produce the bounded projection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise CrmProjectionInvalid(code)


def _exact(value: object, keys: set[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail(code)
    return value


def _literal(value: object, maximum: int, code: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > maximum:
        _fail(code)
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        _fail(code)
    return value


def _utc(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(code)
    if parsed.tzinfo is None:
        _fail(code)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    # Apps Script Date preserves milliseconds. Canonicalize to that precision so
    # the signed identity consumed by the sheet is exactly parseable there.
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _expected(hash_value: str | None, revision: int) -> tuple[str | None, int]:
    if hash_value is not None and (
        not isinstance(hash_value, str) or not HASH_RE.fullmatch(hash_value)
    ):
        _fail("invalid_expected_snapshot_hash")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 0 <= revision <= 1_000_000
    ):
        _fail("invalid_expected_projection_revision")
    if (revision == 0) != (hash_value is None):
        _fail("inconsistent_expected_projection")
    return hash_value, revision


def _canonical_contact(raw: object) -> dict[str, object]:
    item = _exact(
        raw,
        {
            "contact_id",
            "revision",
            "name",
            "business",
            "phone",
            "email",
            "updated_at",
            "source",
            "status",
            "next_action",
            "record_quality",
        },
        "invalid_contact_shape",
    )
    contact_id = _literal(item["contact_id"], 128, "invalid_contact_id")
    if not SAFE_ID_RE.fullmatch(contact_id or ""):
        _fail("invalid_contact_id")
    revision = item["revision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 1 <= revision <= 1_000_000
    ):
        _fail("invalid_contact_revision")
    name = _literal(item["name"], 160, "invalid_contact_name", nullable=True)
    # These canonical DTO fields are deliberately checked but never copied to the projection.
    _literal(item["business"], 160, "invalid_business", nullable=True)
    _literal(item["phone"], 40, "invalid_phone", nullable=True)
    _literal(item["email"], 320, "invalid_email", nullable=True)
    _utc(item["updated_at"], "invalid_contact_updated_at")

    source = _exact(
        item["source"],
        {"source_id", "recorded_label", "evidence_key", "attribution_state"},
        "invalid_contact_source",
    )
    if source["source_id"] != "crm_contacts" or source["evidence_key"] is not None:
        _fail("invalid_contact_source")
    source_label = _literal(source["recorded_label"], 160, "invalid_source_label", nullable=True)
    if source["attribution_state"] not in {"unverified", "unknown"}:
        _fail("invalid_contact_source")

    status = _exact(
        item["status"],
        {"recorded_value", "qualification", "pipeline_stage"},
        "invalid_contact_status",
    )
    _literal(status["recorded_value"], 160, "invalid_recorded_status", nullable=True)
    if status["qualification"] is not None or status["pipeline_stage"] is not None:
        _fail("unsupported_derived_status")

    action = _exact(
        item["next_action"],
        {"text", "due_date", "due_at", "precision", "timezone", "state"},
        "invalid_next_action",
    )
    _literal(action["text"], 2_000, "invalid_next_action", nullable=True)
    if action["due_date"] is not None or action["due_at"] is not None:
        _fail("invented_next_action_time")
    if action["precision"] != "none" or action["timezone"] != "Asia/Jerusalem":
        _fail("invalid_next_action")
    if action["state"] not in {"recorded", "not_collected"}:
        _fail("invalid_next_action")

    quality = _exact(
        item["record_quality"],
        {
            "stored_read",
            "business_classification",
            "source_coverage",
            "missing_fields",
            "reason_codes",
        },
        "invalid_record_quality",
    )
    if (
        quality["stored_read"] != "verified"
        or not isinstance(quality["missing_fields"], list)
        or not isinstance(quality["reason_codes"], list)
    ):
        _fail("invalid_record_quality")

    return {
        "contact_id": contact_id,
        "revision": revision,
        "name": name,
        "source_label": source_label,
        "next_action_state": action["state"],
    }


def _base_payload(
    *,
    workspace_id: str,
    projection_status: str,
    reason_code: str | None,
    expected_snapshot_hash: str | None,
    expected_projection_revision: int,
    source: dict[str, object],
    contacts: list[dict[str, object]],
) -> dict[str, object]:
    identity = {
        "source": source,
        "contacts": contacts,
        "projection_status": projection_status,
        "reason_code": reason_code,
    }
    return {
        "api_version": API_VERSION,
        "mode": MODE,
        "synthetic": True,
        "workspace_id": workspace_id,
        "projection_status": projection_status,
        "reason_code": reason_code,
        "expected_snapshot_hash": expected_snapshot_hash,
        "expected_projection_revision": expected_projection_revision,
        "projection_revision": expected_projection_revision + 1,
        "source": source,
        "contacts": contacts,
        "snapshot_hash": _hash(identity),
    }


def build_test_crm_projection(
    owner_os_envelope: Mapping[str, object],
    *,
    workspace_id: str,
    expected_snapshot_hash: str | None = None,
    expected_projection_revision: int = 0,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build a known TEST projection from one fresh, complete contacts envelope."""

    if not isinstance(workspace_id, str) or not WORKSPACE_RE.fullmatch(workspace_id):
        _fail("invalid_workspace")
    expected_snapshot_hash, expected_projection_revision = _expected(
        expected_snapshot_hash, expected_projection_revision
    )
    envelope = _exact(
        owner_os_envelope,
        {
            "api_version",
            "synthetic",
            "source",
            "owner_scope",
            "request_id",
            "snapshot_id",
            "observed_at",
            "coverage",
            "coverage_scope",
            "freshness",
            "reason_codes",
            "source_statuses",
            "capabilities",
            "data",
            "pagination",
        },
        "invalid_owner_os_envelope",
    )
    if envelope["api_version"] != OWNER_OS_API_VERSION or envelope["synthetic"] is not True:
        _fail("synthetic_test_source_required")
    if envelope["source"] != SOURCE or envelope["owner_scope"] != "self":
        _fail("invalid_owner_os_source")
    if (
        envelope["coverage"] != "complete"
        or envelope["coverage_scope"] != "stored_pg_contacts"
        or envelope["freshness"] != "fresh"
    ):
        _fail("source_coverage_incomplete")
    if envelope["pagination"] not in (
        None,
        {"next_cursor": None, "has_more": False, "page_complete": True},
    ):
        _fail("source_coverage_incomplete")
    observed = _utc(envelope["observed_at"], "invalid_observed_at")
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    age = (clock - observed).total_seconds()
    if age < -5 or age > MAX_AGE_SECONDS:
        _fail("stale_source_snapshot")
    snapshot_id = _literal(envelope["snapshot_id"], 128, "invalid_snapshot_id")
    if not SAFE_ID_RE.fullmatch(snapshot_id or ""):
        _fail("invalid_snapshot_id")

    data = _exact(
        envelope["data"],
        {
            "section_state",
            "items",
            "coverage",
            "coverage_scope",
            "freshness",
            "reason_codes",
            "pagination",
        },
        "invalid_contacts_section",
    )
    if (
        data["section_state"] != "available"
        or data["coverage"] != "complete"
        or data["coverage_scope"] != "stored_pg_contacts"
        or data["freshness"] != "fresh"
    ):
        _fail("source_coverage_incomplete")
    pagination = _exact(
        data["pagination"], {"next_cursor", "has_more", "page_complete"}, "invalid_pagination"
    )
    if pagination != {"next_cursor": None, "has_more": False, "page_complete": True}:
        _fail("source_coverage_incomplete")
    items = data["items"]
    if (
        not isinstance(items, Sequence)
        or isinstance(items, (str, bytes))
        or len(items) > MAX_CONTACTS
    ):
        _fail("contact_limit_exceeded")
    contacts = [_canonical_contact(item) for item in items]
    if len({item["contact_id"] for item in contacts}) != len(contacts):
        _fail("duplicate_contact_id")
    contacts.sort(key=lambda item: str(item["contact_id"]))
    source = {
        "api_version": OWNER_OS_API_VERSION,
        "source": SOURCE,
        "snapshot_id": snapshot_id,
        "observed_at": _iso(observed),
        "coverage": "complete",
        "coverage_scope": "stored_pg_contacts",
    }
    return _base_payload(
        workspace_id=workspace_id,
        projection_status="known",
        reason_code=None,
        expected_snapshot_hash=expected_snapshot_hash,
        expected_projection_revision=expected_projection_revision,
        source=source,
        contacts=contacts,
    )


def build_unknown_test_crm_projection(
    *,
    workspace_id: str,
    reason_code: str,
    expected_snapshot_hash: str | None = None,
    expected_projection_revision: int = 0,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Publish an explicit unknown only when no truthful canonical snapshot exists."""

    if not isinstance(workspace_id, str) or not WORKSPACE_RE.fullmatch(workspace_id):
        _fail("invalid_workspace")
    if reason_code not in {
        "source_unavailable",
        "source_coverage_incomplete",
        "source_snapshot_stale",
    }:
        _fail("invalid_unknown_reason")
    expected_snapshot_hash, expected_projection_revision = _expected(
        expected_snapshot_hash, expected_projection_revision
    )
    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    source = {
        "api_version": OWNER_OS_API_VERSION,
        "source": SOURCE,
        "snapshot_id": None,
        "observed_at": _iso(observed),
        "coverage": "unknown",
        "coverage_scope": "stored_pg_contacts",
    }
    return _base_payload(
        workspace_id=workspace_id,
        projection_status="unknown",
        reason_code=reason_code,
        expected_snapshot_hash=expected_snapshot_hash,
        expected_projection_revision=expected_projection_revision,
        source=source,
        contacts=[],
    )
