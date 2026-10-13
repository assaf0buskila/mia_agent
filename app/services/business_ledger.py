"""Bounded signed port to the one authoritative TEST Sheets business service.

No financial database, background jobs, automatic retry, or provider credentials
in browser code. A lost write response must be recovered through its receipt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from calendar import monthrange
from datetime import UTC, date, datetime
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.core.config import MiaEnv, Settings

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
MAX_RESPONSE_BYTES = 262_144
MAX_MONEY_MINOR = 10**12
MAX_FINANCE_ROWS = 500
WRITE_OPERATIONS = frozenset(
    {
        "save_deal", "save_schedule", "record_payment", "record_expense", "save_project",
        "save_crm_projection",
    }
)
CANONICAL_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class LedgerError(Exception):
    """Only fixed reason codes leave the port, never remote exception text."""

    def __init__(self, code: str, status: int = 503):
        self.code, self.status = code, status
        super().__init__(code)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DealInput(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    amount_minor: int = Field(strict=True, gt=0, le=MAX_MONEY_MINOR)
    currency: Literal["ILS"]
    payment_plan: Literal["full", "installments", "monthly"]
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: Literal["active", "cancelled"]
    # Project links are unavailable until canonical Projects validation is implemented.
    project_id: None = None
    notes: str = Field(default="", max_length=1000)

    @field_validator("title", "notes")
    @classmethod
    def literal_text(cls, value: str) -> str:
        if value != value.strip() or value.startswith(("=", "+", "-", "@")):
            raise ValueError("literal_text_required")
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise ValueError("literal_text_required")
        return value

    @field_validator("start_date")
    @classmethod
    def calendar_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class DealWrite(StrictModel):
    request_id: str = Field(pattern=UUID_PATTERN)
    expected_revision: int = Field(strict=True, ge=0, le=1_000_000)
    deal: DealInput


class AcceptedDeal(DealInput):
    revision: int = Field(strict=True, ge=1, le=1_000_000)
    updated_at: AwareDatetime


class DealTotals(StrictModel):
    deal_value_minor: int = Field(strict=True, ge=0, le=MAX_MONEY_MINOR * 500)
    received_minor: None
    outstanding_minor: None
    expense_minor: None


class LedgerSnapshot(StrictModel):
    api_version: Literal["business-ledger.v1"]
    mode: Literal["TEST"]
    source: Literal["google_sheets"]
    observed_at: AwareDatetime
    coverage: Literal["deals_only"]
    deals: list[AcceptedDeal] = Field(max_length=500)
    totals: DealTotals
    reason_codes: list[Literal["financial_coverage_incomplete"]] = Field(min_length=1, max_length=1)


class LedgerReceipt(StrictModel):
    status: Literal["committed"]
    request_id: str = Field(pattern=UUID_PATTERN)
    id: str = Field(pattern=UUID_PATTERN)
    revision: int = Field(strict=True, ge=1, le=1_000_000)
    receipt_id: str = Field(pattern=UUID_PATTERN)
    committed_at: AwareDatetime
    source: Literal["google_sheets"]
    mode: Literal["TEST"]


def _literal_text(value: str) -> str:
    if value != value.strip() or value.startswith(("=", "+", "-", "@")):
        raise ValueError("literal_text_required")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError("literal_text_required")
    return value


def _calendar_date(value: str) -> str:
    date.fromisoformat(value)
    return value


def _canonical_timestamp(value):
    if not isinstance(value, str) or not CANONICAL_TIMESTAMP.fullmatch(value):
        raise ValueError("canonical_utc_timestamp_required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        parsed.tzinfo != UTC
        or parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") != value
    ):
        raise ValueError("canonical_utc_timestamp_required")
    return value


def _serialize_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FinanceDeal(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    revision: int = Field(strict=True, ge=1, le=1_000_000)
    title: str = Field(min_length=1, max_length=160)
    amount_minor: int = Field(strict=True, gt=0, le=MAX_MONEY_MINOR)
    currency: Literal["ILS"]
    payment_plan: Literal["full", "installments", "monthly"]
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: Literal["active", "cancelled"]
    project_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    notes: str = Field(default="", max_length=1000)
    updated_at: AwareDatetime

    @field_validator("title", "notes")
    @classmethod
    def validate_literal(cls, value: str) -> str:
        return _literal_text(value)

    @field_validator("start_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _calendar_date(value)

    @field_validator("updated_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return _canonical_timestamp(value)

    @field_serializer("updated_at")
    def serialize_timestamp(self, value: datetime) -> str:
        return _serialize_timestamp(value)


class FinanceCharge(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    revision: int = Field(strict=True, ge=1, le=1_000_000)
    deal_id: str = Field(pattern=UUID_PATTERN)
    status: Literal["planned", "cancelled"]
    amount_minor: int = Field(strict=True, gt=0, le=MAX_MONEY_MINOR)
    currency: Literal["ILS"]
    due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    updated_at: AwareDatetime

    @field_validator("due_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _calendar_date(value)

    @field_validator("updated_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return _canonical_timestamp(value)

    @field_serializer("updated_at")
    def serialize_timestamp(self, value: datetime) -> str:
        return _serialize_timestamp(value)


class FinancePayment(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    revision: int = Field(strict=True, ge=1, le=1_000_000)
    charge_id: str = Field(pattern=UUID_PATTERN)
    status: Literal["posted"]
    amount_minor: int = Field(strict=True, gt=0, le=MAX_MONEY_MINOR)
    currency: Literal["ILS"]
    received_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("received_at", "updated_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return _canonical_timestamp(value)

    @field_serializer("received_at", "updated_at")
    def serialize_timestamp(self, value: datetime) -> str:
        return _serialize_timestamp(value)


class FinanceExpense(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    revision: int = Field(strict=True, ge=1, le=1_000_000)
    project_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    amount_minor: int = Field(strict=True, gt=0, le=MAX_MONEY_MINOR)
    currency: Literal["ILS"]
    occurred_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    notes: str = Field(default="", max_length=1000)
    updated_at: AwareDatetime

    @field_validator("occurred_on")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _calendar_date(value)

    @field_validator("notes")
    @classmethod
    def validate_literal(cls, value: str) -> str:
        return _literal_text(value)

    @field_validator("updated_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return _canonical_timestamp(value)

    @field_serializer("updated_at")
    def serialize_timestamp(self, value: datetime) -> str:
        return _serialize_timestamp(value)


class FinanceProject(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    revision: int = Field(strict=True, ge=1, le=1_000_000)
    title: str = Field(min_length=1, max_length=160)
    status: Literal["planned", "active", "completed", "cancelled"]
    deadline: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    notes: str = Field(default="", max_length=1000)
    updated_at: AwareDatetime

    @field_validator("title", "notes")
    @classmethod
    def validate_literal(cls, value: str) -> str:
        return _literal_text(value)

    @field_validator("deadline")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        return None if value is None else _calendar_date(value)

    @field_validator("updated_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return _canonical_timestamp(value)

    @field_serializer("updated_at")
    def serialize_timestamp(self, value: datetime) -> str:
        return _serialize_timestamp(value)


class FinanceCounts(StrictModel):
    deals: int = Field(strict=True, ge=0, le=MAX_FINANCE_ROWS)
    charges: int = Field(strict=True, ge=0, le=MAX_FINANCE_ROWS)
    payments: int = Field(strict=True, ge=0, le=MAX_FINANCE_ROWS)
    expenses: int = Field(strict=True, ge=0, le=MAX_FINANCE_ROWS)
    projects: int = Field(strict=True, ge=0, le=MAX_FINANCE_ROWS)


class FinanceTotals(StrictModel):
    deal_value_minor: int | None = Field(strict=True, ge=0, le=MAX_MONEY_MINOR * MAX_FINANCE_ROWS)
    scheduled_minor: int | None = Field(strict=True, ge=0, le=MAX_MONEY_MINOR * MAX_FINANCE_ROWS)
    received_minor: int | None = Field(strict=True, ge=0, le=MAX_MONEY_MINOR * MAX_FINANCE_ROWS)
    outstanding_minor: int | None = Field(strict=True, ge=0, le=MAX_MONEY_MINOR * MAX_FINANCE_ROWS)
    expense_minor: int | None = Field(strict=True, ge=0, le=MAX_MONEY_MINOR * MAX_FINANCE_ROWS)
    net_cash_minor: int | None = Field(
        strict=True, ge=-(MAX_MONEY_MINOR * MAX_FINANCE_ROWS), le=MAX_MONEY_MINOR * MAX_FINANCE_ROWS
    )


class FinanceSnapshot(StrictModel):
    api_version: Literal["business-ledger.finance.v1"]
    mode: Literal["TEST"]
    source: Literal["google_sheets"]
    observed_at: AwareDatetime
    currency: Literal["ILS"]
    coverage: Literal["financial_complete", "unknown"]
    counts: FinanceCounts
    totals: FinanceTotals
    reason_codes: list[Literal["financial_coverage_incomplete"]] = Field(max_length=1)
    deals: list[FinanceDeal] = Field(max_length=MAX_FINANCE_ROWS)
    charges: list[FinanceCharge] = Field(max_length=MAX_FINANCE_ROWS)
    payments: list[FinancePayment] = Field(max_length=MAX_FINANCE_ROWS)
    expenses: list[FinanceExpense] = Field(max_length=MAX_FINANCE_ROWS)
    projects: list[FinanceProject] = Field(max_length=MAX_FINANCE_ROWS)

    @field_validator("observed_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return _canonical_timestamp(value)

    @field_serializer("observed_at")
    def serialize_timestamp(self, value: datetime) -> str:
        return _serialize_timestamp(value)


class ScheduleWrite(StrictModel):
    request_id: str = Field(pattern=UUID_PATTERN)
    expected_revision: int = Field(strict=True, ge=1, le=1_000_000)
    deal_id: str = Field(pattern=UUID_PATTERN)
    number_of_charges: int = Field(strict=True, ge=1, le=36)
    first_due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    charge_ids: list[str] = Field(min_length=1, max_length=36)

    @field_validator("first_due_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _calendar_date(value)

    @field_validator("charge_ids")
    @classmethod
    def validate_charge_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            re.fullmatch(UUID_PATTERN, item) is None for item in value
        ):
            raise ValueError("unique_charge_ids_required")
        return value

    @model_validator(mode="after")
    def validate_count(self):
        if len(self.charge_ids) != self.number_of_charges:
            raise ValueError("charge_count_mismatch")
        return self


class PaymentInput(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    charge_id: str = Field(pattern=UUID_PATTERN)
    amount_minor: int = Field(strict=True, gt=0, le=MAX_MONEY_MINOR)
    currency: Literal["ILS"]
    received_at: AwareDatetime

    @field_validator("received_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return _canonical_timestamp(value)

    @field_serializer("received_at")
    def serialize_timestamp(self, value: datetime) -> str:
        return _serialize_timestamp(value)


class PaymentWrite(StrictModel):
    request_id: str = Field(pattern=UUID_PATTERN)
    expected_charge_revision: int = Field(strict=True, ge=1, le=1_000_000)
    confirmed_received: Literal[True]
    payment: PaymentInput

    @field_validator("confirmed_received", mode="before")
    @classmethod
    def validate_confirmation(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("explicit_true_required")
        return value


class ExpenseInput(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    project_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    amount_minor: int = Field(strict=True, gt=0, le=MAX_MONEY_MINOR)
    currency: Literal["ILS"]
    occurred_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    notes: str = Field(default="", max_length=1000)

    @field_validator("occurred_on")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _calendar_date(value)

    @field_validator("notes")
    @classmethod
    def validate_literal(cls, value: str) -> str:
        return _literal_text(value)


class ExpenseWrite(StrictModel):
    request_id: str = Field(pattern=UUID_PATTERN)
    expected_revision: Literal[0]
    expense: ExpenseInput

    @field_validator("expected_revision", mode="before")
    @classmethod
    def validate_create_revision(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError("integer_zero_required")
        return value


class ProjectInput(StrictModel):
    id: str = Field(pattern=UUID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    status: Literal["planned", "active", "completed", "cancelled"]
    deadline: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    notes: str = Field(default="", max_length=1000)

    @field_validator("title", "notes")
    @classmethod
    def validate_literal(cls, value: str) -> str:
        return _literal_text(value)

    @field_validator("deadline")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        return None if value is None else _calendar_date(value)


class ProjectWrite(StrictModel):
    request_id: str = Field(pattern=UUID_PATTERN)
    expected_revision: int = Field(strict=True, ge=0, le=1_000_000)
    project: ProjectInput


class GoogleResponseRedirect(HTTPRedirectHandler):
    """Apps Script redirects responses to Google content; never an arbitrary host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if (
            req.get_method() != "POST"
            or target.scheme != "https"
            or target.hostname != "script.googleusercontent.com"
            or target.username
            or target.password
            or target.port not in {None, 443}
            or code not in {302, 303}
        ):
            raise LedgerError("source_unavailable")
        return Request(newurl, headers={"Accept": "application/json"}, method="GET")


def _post(url: str, body: bytes) -> bytes:
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        # The approved Sheets finance read was measured at 9.6 seconds. A POST
        # plus its one Google response redirect each have a finite 15s budget.
        with build_opener(GoogleResponseRedirect()).open(request, timeout=15) as response:
            if response.status != 200 or "application/json" not in response.headers.get(
                "Content-Type", ""
            ):
                raise LedgerError("source_unavailable")
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise LedgerError("source_unavailable")
            return data
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        raise LedgerError("source_unavailable") from None


class BusinessLedger:
    def __init__(self, settings: Settings, owner_id: str):
        if (
            not settings.owner_os_enabled
            or not settings.business_ledger_test_enabled
            or settings.env != MiaEnv.TEST
            or not settings.business_ledger_workspace_id
            or settings.business_ledger_workspace_id != settings.owner_os_workspace_id
            or owner_id not in settings.telegram_owner_user_id_set()
        ):
            raise LedgerError("operation_unavailable")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", owner_id):
            raise LedgerError("operation_unavailable")
        target = urlsplit(settings.business_ledger_script_url)
        if (
            target.scheme != "https"
            or target.netloc != "script.google.com"
            or target.query
            or target.fragment
            or not re.fullmatch(r"/macros/s/[A-Za-z0-9_-]{20,256}/exec", target.path)
            or len(settings.business_ledger_hmac_secret) < 32
        ):
            raise LedgerError("operation_unavailable")
        self.settings, self.owner_id = settings, owner_id

    def _call(self, operation: str, data: dict) -> dict:
        request_id = str(uuid4())
        payload = json.dumps(
            {
                "request_id": request_id,
                "timestamp": int(time.time()),
                "workspace_id": self.settings.business_ledger_workspace_id,
                "owner_id": self.owner_id,
                "operation": operation,
                "data": data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        secret = self.settings.business_ledger_hmac_secret.encode()
        signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        body = json.dumps({"payload": payload, "signature": signature}).encode()
        try:
            if len(body) > 16_384:
                raise LedgerError("validation_error", 422)
            raw = _post(self.settings.business_ledger_script_url, body)
            wrapper = json.loads(raw)
            response_payload = wrapper["payload"]
            response_signature = wrapper["signature"]
            if (
                not isinstance(response_payload, str)
                or not isinstance(response_signature, str)
                or not hmac.compare_digest(
                    response_signature,
                    hmac.new(secret, response_payload.encode(), hashlib.sha256).hexdigest(),
                )
            ):
                raise ValueError
            response = json.loads(response_payload)
            if (
                response["request_id"] != request_id
                or response["owner_id"] != self.owner_id
                or response["workspace_id"] != self.settings.business_ledger_workspace_id
            ):
                raise ValueError
        except LedgerError as exc:
            if operation in WRITE_OPERATIONS and exc.status != 422:
                raise LedgerError("unknown") from None
            raise
        except (KeyError, TypeError, ValueError):
            raise LedgerError(
                "unknown" if operation in WRITE_OPERATIONS else "source_unavailable"
            ) from None
        status = response.get("status")
        if not isinstance(status, str):
            raise LedgerError(
                "unknown" if operation in WRITE_OPERATIONS else "source_unavailable"
            )
        if status != "ok":
            if operation in WRITE_OPERATIONS and status not in {"conflict", "invalid", "not_found"}:
                raise LedgerError("unknown")
            code, http = {
                "conflict": ("conflict", 409),
                "not_found": ("record_not_found", 404),
                "invalid": ("validation_error", 422),
                "unavailable": ("source_unavailable", 503),
            }.get(status, ("source_unavailable", 503))
            raise LedgerError(code, http)
        if not isinstance(response.get("result"), dict):
            raise LedgerError("unknown" if operation in WRITE_OPERATIONS else "source_unavailable")
        return response["result"]

    def snapshot(self) -> dict:
        try:
            snapshot = LedgerSnapshot.model_validate(self._call("snapshot", {}))
            ids = [deal.id for deal in snapshot.deals]
            total = sum(d.amount_minor for d in snapshot.deals if d.status == "active")
            if len(ids) != len(set(ids)) or total != snapshot.totals.deal_value_minor:
                raise ValueError
            observed = snapshot.observed_at.timestamp()
            if observed > time.time() + 60 or observed < time.time() - 300:
                raise ValueError
            return snapshot.model_dump(mode="json")
        except ValueError:
            raise LedgerError("source_unavailable") from None

    def save(self, change: DealWrite) -> dict:
        if self.settings.kill_switch:
            raise LedgerError("forbidden", 403)
        result = self._call("save_deal", change.model_dump(mode="json"))
        try:
            receipt = LedgerReceipt.model_validate(result)
            if (
                receipt.request_id != change.request_id
                or receipt.id != change.deal.id
                or receipt.revision != change.expected_revision + 1
            ):
                raise ValueError
            return receipt.model_dump(mode="json")
        except ValueError:
            raise LedgerError("unknown") from None

    @staticmethod
    def _next_month(value: str, offset: int) -> str:
        current = date.fromisoformat(value)
        absolute = current.year * 12 + current.month - 1 + offset
        year, month_index = divmod(absolute, 12)
        if year > 9999:
            raise ValueError
        day = min(current.day, monthrange(year, month_index + 1)[1])
        return date(year, month_index + 1, day).isoformat()

    @staticmethod
    def _unique(records) -> bool:
        ids = [record.id for record in records]
        return len(ids) == len(set(ids))

    @classmethod
    def _validate_finance_snapshot(cls, snapshot: FinanceSnapshot) -> None:
        groups = (
            snapshot.deals,
            snapshot.charges,
            snapshot.payments,
            snapshot.expenses,
            snapshot.projects,
        )
        if not all(cls._unique(group) for group in groups):
            raise ValueError
        expected_counts = {
            "deals": len(snapshot.deals),
            "charges": len(snapshot.charges),
            "payments": len(snapshot.payments),
            "expenses": len(snapshot.expenses),
            "projects": len(snapshot.projects),
        }
        if snapshot.counts.model_dump() != expected_counts:
            raise ValueError

        projects = {record.id: record for record in snapshot.projects}
        deals = {record.id: record for record in snapshot.deals}
        charges = {record.id: record for record in snapshot.charges}
        if any(
            deal.project_id is not None and deal.project_id not in projects
            for deal in snapshot.deals
        ):
            raise ValueError
        if any(charge.deal_id not in deals for charge in snapshot.charges):
            raise ValueError
        if any(payment.charge_id not in charges for payment in snapshot.payments):
            raise ValueError
        if any(
            expense.project_id is not None and expense.project_id not in projects
            for expense in snapshot.expenses
        ):
            raise ValueError

        paid_by_charge: dict[str, int] = {}
        for payment in snapshot.payments:
            charge = charges[payment.charge_id]
            if charge.status == "cancelled":
                raise ValueError
            paid_by_charge[charge.id] = paid_by_charge.get(charge.id, 0) + payment.amount_minor
            if paid_by_charge[charge.id] > charge.amount_minor:
                raise ValueError

        if snapshot.coverage == "unknown":
            if snapshot.reason_codes != ["financial_coverage_incomplete"]:
                raise ValueError
            if any(value is not None for value in snapshot.totals.model_dump().values()):
                raise ValueError
            return
        if snapshot.reason_codes:
            raise ValueError

        for deal in snapshot.deals:
            plan = [charge for charge in snapshot.charges if charge.deal_id == deal.id]
            if deal.status == "cancelled" and not plan:
                continue
            if not plan:
                raise ValueError
            expected_count = 1 if deal.payment_plan == "full" else len(plan)
            if (
                (deal.payment_plan == "full" and len(plan) != 1)
                or (deal.payment_plan != "full" and not 2 <= len(plan) <= 36)
                or sum(charge.amount_minor for charge in plan) != deal.amount_minor
                or plan[0].due_date < deal.start_date
            ):
                raise ValueError
            if expected_count != len(plan):
                raise ValueError
            for index, charge in enumerate(plan):
                if charge.currency != deal.currency or charge.due_date != cls._next_month(
                    plan[0].due_date, index
                ):
                    raise ValueError
            if deal.status == "cancelled" and any(charge.status != "cancelled" for charge in plan):
                raise ValueError

        deal_value = sum(deal.amount_minor for deal in snapshot.deals if deal.status == "active")
        planned = [charge for charge in snapshot.charges if charge.status == "planned"]
        scheduled = sum(charge.amount_minor for charge in planned)
        received = sum(paid_by_charge.get(charge.id, 0) for charge in planned)
        outstanding = sum(
            max(charge.amount_minor - paid_by_charge.get(charge.id, 0), 0) for charge in planned
        )
        expense = sum(record.amount_minor for record in snapshot.expenses)
        expected_totals = {
            "deal_value_minor": deal_value,
            "scheduled_minor": scheduled,
            "received_minor": received,
            "outstanding_minor": outstanding,
            "expense_minor": expense,
            "net_cash_minor": received - expense,
        }
        if snapshot.totals.model_dump() != expected_totals:
            raise ValueError

    def finance_snapshot(self) -> dict:
        try:
            snapshot = FinanceSnapshot.model_validate(self._call("finance_snapshot", {}))
            observed = snapshot.observed_at.timestamp()
            if observed > time.time() + 60 or observed < time.time() - 300:
                raise ValueError
            self._validate_finance_snapshot(snapshot)
            return snapshot.model_dump(mode="json")
        except ValueError:
            raise LedgerError("source_unavailable") from None

    def _financial_write(self, operation: str, data: dict, entity_id: str, revision: int) -> dict:
        if self.settings.kill_switch:
            raise LedgerError("forbidden", 403)
        result = self._call(operation, data)
        try:
            receipt = LedgerReceipt.model_validate(result)
            if (
                receipt.request_id != data["request_id"]
                or receipt.id != entity_id
                or receipt.revision != revision
            ):
                raise ValueError
            return receipt.model_dump(mode="json")
        except ValueError:
            raise LedgerError("unknown") from None

    def save_schedule(self, change: ScheduleWrite) -> dict:
        return self._financial_write(
            "save_schedule",
            change.model_dump(mode="json"),
            change.deal_id,
            change.expected_revision + 1,
        )

    def record_payment(self, change: PaymentWrite) -> dict:
        return self._financial_write(
            "record_payment", change.model_dump(mode="json"), change.payment.id, 1
        )

    def record_expense(self, change: ExpenseWrite) -> dict:
        return self._financial_write(
            "record_expense", change.model_dump(mode="json"), change.expense.id, 1
        )

    def save_project(self, change: ProjectWrite) -> dict:
        return self._financial_write(
            "save_project",
            change.model_dump(mode="json"),
            change.project.id,
            change.expected_revision + 1,
        )

    def receipt(self, request_id: str) -> dict:
        try:
            receipt = LedgerReceipt.model_validate(
                self._call("receipt", {"request_id": request_id})
            )
            if receipt.request_id != request_id:
                raise ValueError
            return receipt.model_dump(mode="json")
        except ValueError:
            raise LedgerError("source_unavailable") from None
