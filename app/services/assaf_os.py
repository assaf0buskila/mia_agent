"""Signed, bounded port to the one pinned Assaf OS Apps Script deployment."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import Settings

API_VERSION = "assaf-os.v1"
SCHEMA_VERSION = "assaf-os-unified.v1"
MAX_BODY_BYTES = 32_768
MAX_RESPONSE_BYTES = 524_288
MAX_ROWS_PER_ENTITY = 1_000
REQUEST_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{2,63}$"
ActorSource = Literal["OS", "MIA", "TELEGRAM_APPROVAL"]
Entity = Literal[
    "leads", "clients", "works", "payments", "time", "expenses", "tasks", "promote_lead"
]
OPERATIONS = frozenset(
    {"schema", "snapshot", "propose", "proposal", "approve", "reject", "receipt"}
)
UNCERTAIN_OPERATIONS = frozenset({"propose", "approve", "reject"})
RECORD_ENTITIES = ("leads", "clients", "works", "payments", "time", "expenses", "tasks")
_APPROVAL_GUARD_VERSION = 1
_DECISION_BINDING_CACHE_MAX = 256
_DECISION_BINDING_CACHE_LEASE_SECONDS = 60.0
_DecisionBindingKey = tuple[str, str, str, str, str, bytes, str, int, str]


@dataclass(frozen=True, slots=True)
class _DecisionBinding:
    approval_request_id: str
    rejection_request_id: str
    signed_expires_at: float
    inserted_wall: float
    inserted_monotonic: float
    monotonic_deadline: float


_DECISION_BINDINGS: OrderedDict[_DecisionBindingKey, _DecisionBinding] = OrderedDict()
_DECISION_BINDINGS_LOCK = Lock()

# Copied from the checked-in schema contract. System/calculated keys are accepted on
# reads, while proposals use INPUT_FIELDS only.
INPUT_FIELDS: dict[str, frozenset[str]] = {
    "leads": frozenset({"name", "phone", "email", "path", "status", "source_note"}),
    "clients": frozenset({"name", "phone", "email", "status", "notes"}),
    "works": frozenset(
        {"person_id", "title", "path", "start_date", "status", "agreed_amount_ils", "currency"}
    ),
    "payments": frozenset(
        {
            "person_id",
            "work_id",
            "path",
            "date",
            "actual_amount_ils",
            "currency",
            "kind",
            "original_payment_id",
            "notes",
        }
    ),
    "time": frozenset({"person_id", "work_id", "path", "date", "minutes", "description", "status"}),
    "expenses": frozenset(
        {
            "date",
            "category",
            "platform",
            "path",
            "vendor",
            "amount_ils",
            "currency",
            "payment_status",
            "source_type",
            "source_key",
            "notes",
        }
    ),
    "tasks": frozenset({"title", "person_id", "work_id", "path", "status", "due_date", "notes"}),
}
READ_FIELDS: dict[str, frozenset[str]] = {
    "leads": INPUT_FIELDS["leads"]
    | frozenset(
        {
            "lead_id",
            "person_id",
            "created_at",
            "updated_at",
            "revision",
            "write_source",
            "request_id",
        }
    ),
    "clients": INPUT_FIELDS["clients"]
    | frozenset(
        {
            "person_id",
            "created_at",
            "updated_at",
            "revision",
            "write_source",
            "request_id",
            "received_learning_ils",
            "received_automation_ils",
            "total_received_ils",
            "recorded_hours",
            "hourly_ils",
            "foreign_currency_flag",
        }
    ),
    "works": INPUT_FIELDS["works"]
    | frozenset(
        {
            "work_id",
            "created_at",
            "updated_at",
            "revision",
            "write_source",
            "request_id",
            "received_ils",
            "recorded_hours",
            "hourly_ils",
            "foreign_currency_flag",
        }
    ),
    "payments": INPUT_FIELDS["payments"]
    | frozenset({"payment_id", "created_at", "revision", "write_source", "request_id"}),
    "time": INPUT_FIELDS["time"]
    | frozenset({"time_id", "created_at", "revision", "write_source", "request_id"}),
    "expenses": INPUT_FIELDS["expenses"]
    | frozenset({"expense_id", "created_at", "revision", "write_source", "request_id"}),
    "tasks": INPUT_FIELDS["tasks"]
    | frozenset({"task_id", "created_at", "updated_at", "revision", "write_source", "request_id"}),
}
MONEY_FIELDS = frozenset(
    {
        "agreed_amount_ils",
        "actual_amount_ils",
        "amount_ils",
        "received_learning_ils",
        "received_automation_ils",
        "total_received_ils",
        "hourly_ils",
        "received_ils",
    }
)
SUMMARY_FIELDS = frozenset(
    {
        "summary_scope",
        "month",
        "person_id",
        "client_name",
        "path",
        "currency",
        "net_received_ils",
        "hours",
        "weighted_hourly_ils",
        "missing_time",
        "paid_expenses_ils",
        "net_cash_ils",
        "coverage",
        "calculated_at",
    }
)
SUMMARY_MONTHLY_FIELDS = frozenset(
    {
        "month",
        "currency",
        "paid_income_ils",
        "paid_expenses_ils",
        "net_cash_ils",
        "recorded_minutes",
        "effective_hourly_ils",
        "coverage",
        "reason_codes",
    }
)
ENTITY_COVERAGE_FIELDS = frozenset(RECORD_ENTITIES)
TAB_FIELDS = {
    **READ_FIELDS,
    "summary": SUMMARY_FIELDS,
    "settings": frozenset({"key", "value", "description", "updated_at"}),
}
PROPOSAL_ID = Field(pattern=ID_PATTERN, min_length=3, max_length=64)


class AssafOsError(Exception):
    """A fixed reason code suitable for an owner response."""

    def __init__(self, code: str, status: int = 503):
        self.code, self.status = code, status
        super().__init__(code)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposeRequest(StrictModel):
    request_id: str = Field(pattern=REQUEST_PATTERN)
    entity: Entity
    record_id: str | None = Field(default=None, pattern=ID_PATTERN)
    expected_revision: int = Field(strict=True, ge=0, le=1_000_000)
    input: dict[str, Any] = Field(max_length=16)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.entity == "promote_lead":
            if self.record_id is None or self.expected_revision < 1 or self.input:
                raise ValueError("invalid_promotion")
            return self
        allowed = INPUT_FIELDS[self.entity]
        if not self.input or not set(self.input) <= allowed:
            raise ValueError("non_input_field")
        if self.record_id is None and self.expected_revision != 0:
            raise ValueError("invalid_create_revision")
        if self.record_id is not None and self.expected_revision < 1:
            raise ValueError("invalid_update_revision")
        for key, value in self.input.items():
            if key in MONEY_FIELDS and (type(value) is not int or value < 0):
                raise ValueError("invalid_money_minor")
            if isinstance(value, str) and (len(value) > 2_000 or value != value.strip()):
                raise ValueError("invalid_text")
            if isinstance(value, (dict, list)):
                raise ValueError("invalid_input_value")
        return self


class DecisionRequest(StrictModel):
    request_id: str = Field(pattern=REQUEST_PATTERN)


class _NoRedirect(HTTPRedirectHandler):
    """Expose the one redirect so _post can enforce one shared time budget."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_response(response) -> bytes:
    if response.status != 200 or "application/json" not in response.headers.get("Content-Type", ""):
        raise AssafOsError("source_unavailable")
    result = response.read(MAX_RESPONSE_BYTES + 1)
    if len(result) > MAX_RESPONSE_BYTES:
        raise AssafOsError("source_unavailable")
    return result


def _post(url: str, body: bytes) -> bytes:
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    deadline = time.monotonic() + 15
    opener = build_opener(_NoRedirect())
    try:
        try:
            with opener.open(request, timeout=max(0.1, deadline - time.monotonic())) as response:
                return _read_response(response)
        except HTTPError as redirect:
            if redirect.code not in {302, 303} or request.get_method() != "POST":
                raise
            location = redirect.headers.get("Location", "")
            redirect.close()
            target = urlsplit(location)
            if (
                target.scheme != "https"
                or target.hostname != "script.googleusercontent.com"
                or target.username
                or target.password
                or target.port not in {None, 443}
            ):
                raise AssafOsError("source_unavailable") from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssafOsError("source_unavailable") from None
            follow = Request(location, headers={"Accept": "application/json"}, method="GET")
            with opener.open(follow, timeout=remaining) as response:
                return _read_response(response)
    except AssafOsError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        raise AssafOsError("source_unavailable") from None


def _validate_records(records: Any) -> None:
    if not isinstance(records, dict) or set(records) != set(RECORD_ENTITIES):
        raise ValueError
    for entity, rows in records.items():
        if not isinstance(rows, list) or len(rows) > MAX_ROWS_PER_ENTITY:
            raise ValueError
        for row in rows:
            if not isinstance(row, dict) or not set(row) <= READ_FIELDS[entity]:
                raise ValueError
            for key in MONEY_FIELDS & set(row):
                if row[key] is not None and type(row[key]) is not int:
                    raise ValueError


def _validate_summary(summary: Any) -> None:
    if not isinstance(summary, dict) or set(summary) != {"coverage", "monthly"}:
        raise ValueError
    if summary["coverage"] not in {"complete", "partial", "unknown"}:
        raise ValueError
    monthly = summary["monthly"]
    if not isinstance(monthly, list) or len(monthly) > MAX_ROWS_PER_ENTITY:
        raise ValueError
    for row in monthly:
        if not isinstance(row, dict) or set(row) != SUMMARY_MONTHLY_FIELDS:
            raise ValueError
        if row["coverage"] not in {"complete", "partial", "unknown"}:
            raise ValueError
        if not isinstance(row["month"], str) or not isinstance(row["currency"], str):
            raise ValueError
        if not isinstance(row["reason_codes"], list) or any(
            not isinstance(code, str) for code in row["reason_codes"]
        ):
            raise ValueError
        for key in {
            "paid_income_ils",
            "paid_expenses_ils",
            "net_cash_ils",
            "recorded_minutes",
            "effective_hourly_ils",
        }:
            if row[key] is not None and type(row[key]) is not int:
                raise ValueError


def _validate_result(
    operation: str,
    result: dict[str, Any],
    settings: Settings,
    request_data: dict[str, Any],
) -> dict[str, Any]:
    binding = {
        "api_version": API_VERSION,
        "source": "google_sheets",
        "mode": settings.assaf_os_mode,
        "workbook_id": settings.assaf_os_workbook_id,
        "workspace_id": settings.assaf_os_workspace_id,
    }
    if any(result.get(key) != value for key, value in binding.items()):
        raise ValueError
    if operation == "schema":
        required = set(binding) | {"schema_version", "money_unit", "tabs", "enums"}
        if (
            set(result) != required
            or result["schema_version"] != SCHEMA_VERSION
            or result["money_unit"] != "minor"
            or not isinstance(result["tabs"], list)
            or not isinstance(result["enums"], dict)
        ):
            raise ValueError
        for tab in result["tabs"]:
            if not isinstance(tab, dict) or tab.get("key") not in TAB_FIELDS:
                raise ValueError
            fields = tab.get("fields")
            if not isinstance(fields, list) or any(
                not isinstance(field, dict) or field.get("key") not in TAB_FIELDS[tab["key"]]
                for field in fields
            ):
                raise ValueError
    elif operation == "snapshot":
        required = set(binding) | {
            "observed_at",
            "coverage",
            "entity_coverage",
            "records",
            "summary",
            "issues",
            "pending_proposals",
        }
        if set(result) != required or result["coverage"] not in {"complete", "partial", "unknown"}:
            raise ValueError
        observed_at = datetime.fromisoformat(str(result["observed_at"]).replace("Z", "+00:00"))
        if (
            observed_at.tzinfo is None
            or abs(datetime.now(UTC).timestamp() - observed_at.timestamp()) > 300
        ):
            raise ValueError
        _validate_records(result["records"])
        entity_coverage = result["entity_coverage"]
        if (
            not isinstance(entity_coverage, dict)
            or set(entity_coverage) != ENTITY_COVERAGE_FIELDS
            or any(
                value not in {"complete", "partial", "unknown"}
                for value in entity_coverage.values()
            )
            or not isinstance(result["issues"], list)
            or not isinstance(result["pending_proposals"], list)
        ):
            raise ValueError
        _validate_summary(result["summary"])
    elif operation in {"propose", "proposal"}:
        required = set(binding) | {
            "approval_guard_version",
            "status",
            "proposal_id",
            "request_id",
            "approval_request_id",
            "rejection_request_id",
            "expires_at",
            "entity",
            "record_id",
            "expected_revision",
            "input",
            "before",
            "after",
            "actor_source",
        }
        if (
            set(result) != required
            or result["approval_guard_version"] != 1
            or result["status"]
            not in (
                {"pending"}
                if operation == "propose"
                else {"pending", "committed", "rejected", "expired", "target_changed"}
            )
            or result["actor_source"] not in {"OS", "MIA"}
            or result["entity"] not in INPUT_FIELDS | {"promote_lead": frozenset()}
        ):
            raise ValueError
        if not isinstance(result["input"], dict) or (
            result["entity"] != "promote_lead"
            and not set(result["input"]) <= INPUT_FIELDS[result["entity"]]
        ):
            raise ValueError
        for key in (
            "proposal_id",
            "request_id",
            "approval_request_id",
            "rejection_request_id",
            "expires_at",
        ):
            if not isinstance(result[key], str) or not result[key]:
                raise ValueError
    elif operation in {"approve", "reject", "receipt"}:
        required = set(binding) | {
            "actor_source",
            "status",
            "request_id",
            "proposal_id",
            "entity",
            "record_id",
            "revision",
            "committed_at",
            "reason_codes",
        }
        if (
            set(result) != required
            or result.get("actor_source") not in {"OS", "MIA"}
            or result.get("status")
            not in {"committed", "rejected", "expired", "target_changed", "unknown"}
            or (operation != "receipt" and result.get("status") == "unknown")
            or result.get("request_id") != request_data.get("request_id")
            or not isinstance(result.get("reason_codes"), list)
            or any(not isinstance(code, str) for code in result["reason_codes"])
        ):
            raise ValueError
        if result["status"] == "unknown":
            if (
                result["proposal_id"] is not None
                or result["entity"] is not None
                or result["record_id"] is not None
                or result["revision"] is not None
                or result["committed_at"] is not None
                or result["reason_codes"] != ["receipt_not_found"]
            ):
                raise ValueError
    return result


def _decision_binding_key(
    settings: Settings, owner_id: str, proposal_id: str
) -> _DecisionBindingKey:
    secret_fingerprint = hashlib.sha256(settings.assaf_os_hmac_secret.encode()).digest()
    return (
        owner_id,
        settings.assaf_os_workspace_id,
        settings.assaf_os_workbook_id,
        settings.assaf_os_mode,
        settings.assaf_os_script_url,
        secret_fingerprint,
        API_VERSION,
        _APPROVAL_GUARD_VERSION,
        proposal_id,
    )


def _remember_decision_binding(
    settings: Settings, owner_id: str, proposal: dict[str, Any]
) -> None:
    guard_version = proposal.get("approval_guard_version")
    if (
        proposal.get("status") != "pending"
        or type(guard_version) is not int
        or guard_version != _APPROVAL_GUARD_VERSION
    ):
        return
    try:
        proposal_id = proposal["proposal_id"]
        approval_request_id = proposal["approval_request_id"]
        rejection_request_id = proposal["rejection_request_id"]
        expires_at = datetime.fromisoformat(str(proposal["expires_at"]).replace("Z", "+00:00"))
        if (
            not isinstance(proposal_id, str)
            or not isinstance(approval_request_id, str)
            or not isinstance(rejection_request_id, str)
            or re.fullmatch(ID_PATTERN, proposal_id) is None
            or re.fullmatch(REQUEST_PATTERN, approval_request_id) is None
            or re.fullmatch(REQUEST_PATTERN, rejection_request_id) is None
            or expires_at.tzinfo is None
        ):
            return
        inserted_wall = time.time()
        inserted_monotonic = time.monotonic()
        signed_expires_at = expires_at.timestamp()
        remaining = signed_expires_at - inserted_wall
        if not all(
            math.isfinite(value)
            for value in (inserted_wall, inserted_monotonic, signed_expires_at, remaining)
        ) or remaining <= 0:
            return
        monotonic_deadline = inserted_monotonic + min(
            remaining, _DECISION_BINDING_CACHE_LEASE_SECONDS
        )
        if not math.isfinite(monotonic_deadline):
            return
        entry = _DecisionBinding(
            approval_request_id=approval_request_id,
            rejection_request_id=rejection_request_id,
            signed_expires_at=signed_expires_at,
            inserted_wall=inserted_wall,
            inserted_monotonic=inserted_monotonic,
            monotonic_deadline=monotonic_deadline,
        )
        key = _decision_binding_key(settings, owner_id, proposal_id)
    except (KeyError, OSError, OverflowError, TypeError, ValueError):
        return
    with _DECISION_BINDINGS_LOCK:
        _DECISION_BINDINGS.pop(key, None)
        _DECISION_BINDINGS[key] = entry
        while len(_DECISION_BINDINGS) > _DECISION_BINDING_CACHE_MAX:
            _DECISION_BINDINGS.popitem(last=False)


def _decision_binding(
    settings: Settings, owner_id: str, proposal_id: str
) -> tuple[str, str] | None:
    key = _decision_binding_key(settings, owner_id, proposal_id)
    with _DECISION_BINDINGS_LOCK:
        entry = _DECISION_BINDINGS.get(key)
    if entry is None:
        return None
    wall_now = time.time()
    monotonic_now = time.monotonic()
    invalid = (
        not math.isfinite(wall_now)
        or not math.isfinite(monotonic_now)
        or wall_now < entry.inserted_wall
        or monotonic_now < entry.inserted_monotonic
        or wall_now >= entry.signed_expires_at
        or monotonic_now >= entry.monotonic_deadline
    )
    if invalid:
        with _DECISION_BINDINGS_LOCK:
            if _DECISION_BINDINGS.get(key) is entry:
                _DECISION_BINDINGS.pop(key, None)
        return None
    return (entry.approval_request_id, entry.rejection_request_id)


def _forget_decision_binding(settings: Settings, owner_id: str, proposal_id: str) -> None:
    key = _decision_binding_key(settings, owner_id, proposal_id)
    with _DECISION_BINDINGS_LOCK:
        _DECISION_BINDINGS.pop(key, None)


def _clear_assaf_os_decision_binding_cache() -> None:
    with _DECISION_BINDINGS_LOCK:
        _DECISION_BINDINGS.clear()


class AssafOsPort:
    def __init__(self, settings: Settings, owner_id: str, *, actor_source: ActorSource):
        if (
            not settings.assaf_os_enabled
            or owner_id not in settings.telegram_owner_user_id_set()
            or not re.fullmatch(r"[1-9][0-9]{0,19}", owner_id)
        ):
            raise AssafOsError("operation_unavailable", 404)
        target = urlsplit(settings.assaf_os_script_url)
        if (
            target.scheme != "https"
            or target.netloc != "script.google.com"
            or target.query
            or target.fragment
            or target.username
            or target.password
            or target.port not in {None, 443}
            or not re.fullmatch(r"/macros/s/[A-Za-z0-9_-]{20,256}/exec", target.path)
            or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", settings.assaf_os_workbook_id)
            or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", settings.assaf_os_workspace_id)
            or len(settings.assaf_os_hmac_secret) < 32
        ):
            raise AssafOsError("operation_unavailable")
        self.settings, self.owner_id, self.actor_source = settings, owner_id, actor_source

    def _call(
        self, operation: str, data: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        if operation not in OPERATIONS or (
            operation in {"approve", "reject"}
            and self.actor_source not in {"OS", "TELEGRAM_APPROVAL"}
        ):
            raise AssafOsError("forbidden", 403)
        if operation in {"propose", "approve", "reject"} and self.settings.kill_switch:
            raise AssafOsError("kill_switch", 403)
        request_id = request_id or str(uuid4())
        timestamp = int(time.time())
        request_value = {
            "api_version": API_VERSION,
            "operation": operation,
            "actor_source": self.actor_source,
            "owner_id": self.owner_id,
            "workspace_id": self.settings.assaf_os_workspace_id,
            "workbook_id": self.settings.assaf_os_workbook_id,
            "request_id": request_id,
            "timestamp": timestamp,
            "data": data,
        }
        payload = json.dumps(request_value, ensure_ascii=False, separators=(",", ":"))
        secret = self.settings.assaf_os_hmac_secret.encode()
        body = json.dumps(
            {
                "payload": payload,
                "signature": hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest(),
            },
            separators=(",", ":"),
        ).encode()
        if len(body) > MAX_BODY_BYTES:
            raise AssafOsError("validation_error", 422)
        try:
            raw = _post(self.settings.assaf_os_script_url, body)
            wrapper = json.loads(raw)
            if not isinstance(wrapper, dict) or set(wrapper) != {"payload", "signature"}:
                raise ValueError
            response_payload, signature = wrapper["payload"], wrapper["signature"]
            if (
                not isinstance(response_payload, str)
                or not isinstance(signature, str)
                or not re.fullmatch(r"[0-9a-f]{64}", signature)
                or not hmac.compare_digest(
                    signature,
                    hmac.new(secret, response_payload.encode(), hashlib.sha256).hexdigest(),
                )
            ):
                raise ValueError
            response = json.loads(response_payload)
            expected = {key: value for key, value in request_value.items() if key != "data"}
            if (
                not isinstance(response, dict)
                or set(response) != set(expected) | {"mode", "ok", "data"}
                or response.get("mode") != self.settings.assaf_os_mode
                or any(response.get(key) != value for key, value in expected.items())
                or type(response.get("ok")) is not bool
                or not isinstance(response.get("data"), dict)
            ):
                raise ValueError
        except AssafOsError as exc:
            if operation in UNCERTAIN_OPERATIONS and exc.status >= 500:
                raise AssafOsError("unknown", 503) from None
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise AssafOsError(
                "unknown" if operation in UNCERTAIN_OPERATIONS else "source_unavailable"
            ) from None
        if not response["ok"]:
            error = response["data"]
            if (
                set(error) != {"error"}
                or not isinstance(error["error"], dict)
                or set(error["error"]) != {"code"}
                or not isinstance(error["error"]["code"], str)
            ):
                raise AssafOsError(
                    "unknown" if operation in UNCERTAIN_OPERATIONS else "source_unavailable"
                )
            code = error["error"]["code"]
            status = (
                404
                if code in {"not_found", "proposal_not_found"}
                else 409
                if code in {"target_changed", "expired", "replay"}
                else 422
                if code.startswith("invalid_")
                else 403
                if code.startswith("unauthorized") or code == "actor_cannot_decide"
                else 503
            )
            raise AssafOsError(code, status)
        try:
            return _validate_result(operation, response["data"], self.settings, data)
        except (KeyError, TypeError, ValueError):
            raise AssafOsError(
                "unknown" if operation in UNCERTAIN_OPERATIONS else "source_unavailable"
            ) from None

    def schema(self) -> dict[str, Any]:
        return self._call("schema", {})

    def snapshot(self) -> dict[str, Any]:
        return self._call("snapshot", {})

    def propose(self, request: ProposeRequest) -> dict[str, Any]:
        proposal = self._call(
            "propose", request.model_dump(mode="json"), request_id=request.request_id
        )
        _remember_decision_binding(self.settings, self.owner_id, proposal)
        return proposal

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        proposal = self._call("proposal", {"proposal_id": proposal_id})
        if proposal["status"] == "pending" and proposal["proposal_id"] == proposal_id:
            _remember_decision_binding(self.settings, self.owner_id, proposal)
        else:
            _forget_decision_binding(self.settings, self.owner_id, proposal_id)
        return proposal

    def decide(
        self, operation: Literal["approve", "reject"], proposal_id: str, request: DecisionRequest
    ) -> dict[str, Any]:
        if self.settings.kill_switch:
            raise AssafOsError("kill_switch", 403)
        # Browser input supplies only the stable decision id. Bind it to the
        # immutable remote proposal before issuing the one non-retried decision
        # POST, so a valid id cannot be paired with a different proposal path.
        if self.actor_source == "OS":
            binding = _decision_binding(self.settings, self.owner_id, proposal_id)
            if binding is None:
                proposal = self.proposal(proposal_id)
                binding = (
                    proposal["approval_request_id"],
                    proposal["rejection_request_id"],
                )
            expected = binding[0 if operation == "approve" else 1]
            if not hmac.compare_digest(expected, request.request_id):
                raise AssafOsError("decision_request_mismatch", 409)
        _forget_decision_binding(self.settings, self.owner_id, proposal_id)
        return self._call(
            operation,
            {"proposal_id": proposal_id, "request_id": request.request_id},
            request_id=request.request_id,
        )

    def receipt(self, request_id: str) -> dict[str, Any]:
        # Receipt recovery has its own transport identity. Reusing the original
        # mutation id here would collide with the Apps Script replay journal.
        return self._call("receipt", {"request_id": request_id})
