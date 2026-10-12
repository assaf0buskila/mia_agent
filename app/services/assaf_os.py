"""Signed, bounded port to the one pinned Assaf OS Apps Script deployment."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
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
        return self._call("propose", request.model_dump(mode="json"), request_id=request.request_id)

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._call("proposal", {"proposal_id": proposal_id})

    def decide(
        self, operation: Literal["approve", "reject"], proposal_id: str, request: DecisionRequest
    ) -> dict[str, Any]:
        if self.settings.kill_switch:
            raise AssafOsError("kill_switch", 403)
        # Browser input supplies only the stable decision id. Bind it to the
        # immutable remote proposal before issuing the one non-retried decision
        # POST, so a valid id cannot be paired with a different proposal path.
        if self.actor_source == "OS":
            proposal = self.proposal(proposal_id)
            expected_key = (
                "approval_request_id" if operation == "approve" else "rejection_request_id"
            )
            if proposal.get(expected_key) != request.request_id:
                raise AssafOsError("decision_request_mismatch", 409)
        return self._call(
            operation,
            {"proposal_id": proposal_id, "request_id": request.request_id},
            request_id=request.request_id,
        )

    def receipt(self, request_id: str) -> dict[str, Any]:
        # Receipt recovery has its own transport identity. Reusing the original
        # mutation id here would collide with the Apps Script replay journal.
        return self._call("receipt", {"request_id": request_id})
