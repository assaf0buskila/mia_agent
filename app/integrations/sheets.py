"""Google Sheets port.

Live CRM writes go to the locked Contacts + Activity workbook only.
Archive tabs are gone. Leftover mirror upserts (``01 Leads``, ``10 Mia Activity``,
and the numbered 04–09 tabs) are no-ops so they cannot recreate them.
Postgres remains recoverable memory. Owner reads/updates of other allowlisted
Sheets stay bounded.

Production adapter: Composio ``GOOGLESHEETS`` toolkit version ``20260826_00``,
pins the legacy mirror ``GOOGLESHEETS_UPSERT_ROWS`` plus owner-operational
``GOOGLESHEETS_VALUES_GET``, ``GOOGLESHEETS_VALUES_UPDATE``, and
``GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND`` only when ``MIA_COMPOSIO_API_KEY``,
``MIA_COMPOSIO_USER_ID``, and ``MIA_SHEETS_SPREADSHEET_ID`` are set.
It may add the fixed CRM tabs inside the configured spreadsheet, but never
clear/delete/create-spreadsheet/share or discover Drive files.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from time import monotonic, perf_counter
from typing import Any, Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings
from app.core.demo import demo_mode_active
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms
from app.domain.attribution import sanitize_attribution
from app.domain.content_insights import ALLOWLISTED_MEDIA_TYPES, is_allowlisted_media_id
from app.domain.deals import (
    ALLOWLISTED_CONFIDENCE,
    ALLOWLISTED_STAGES,
)
from app.domain.events import (
    Channel,
    persist_tool_outcome,
    sheets_mirror_outcome,
    sheets_tab_mirror_outcome,
)
from app.domain.followups import (
    REASON_MEETING_BOOKED,
    REASON_MEETING_OFFERED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_RECOVERED,
)
from app.domain.kpis import compute_weekly_kpi
from app.domain.meeting_slots import normalize_scheduled_at_utc, sanitize_event_id
from app.domain.meetings import (
    ALLOWLISTED_STATUSES,
    STATUS_BOOKED,
    STATUS_CANCELLATION_REQUESTED,
    STATUS_OFFERED,
)
from app.domain.sales import NextAction
from app.domain.tools import (
    AdapterHttpError,
    AdapterResponseError,
    AdapterSchemaError,
    ToolOutcome,
)
from app.surfaces.crm import (
    ACTIVITY_HEADERS as CONTACTS_ACTIVITY_HEADERS,
)
from app.surfaces.crm import (
    ACTIVITY_TAB as CONTACTS_ACTIVITY_TAB,
)
from app.surfaces.crm import (
    CONTACTS_HEADERS,
    CONTACTS_READ_COLUMNS,
    CONTACTS_TAB,
    LOCKED_SPREADSHEET_ID,
    a1_targets_archive_tab,
    sheet_tab_from_a1,
)

SHEETS_MIRROR_SCOPE = "sheets_mirror"
SHEETS_MIRROR_TABS = frozenset({"sales", "session", "content"})

COMPOSIO_GOOGLESHEETS_VERSION = "20260826_00"
COMPOSIO_UPSERT_ROWS_TOOL = "GOOGLESHEETS_UPSERT_ROWS"
COMPOSIO_VALUES_GET_TOOL = "GOOGLESHEETS_VALUES_GET"
COMPOSIO_VALUES_UPDATE_TOOL = "GOOGLESHEETS_VALUES_UPDATE"
COMPOSIO_VALUES_APPEND_TOOL = "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND"
COMPOSIO_GET_SHEET_NAMES_TOOL = "GOOGLESHEETS_GET_SHEET_NAMES"
COMPOSIO_ADD_SHEET_TOOL = "GOOGLESHEETS_ADD_SHEET"
_COMPOSIO_EXECUTE_BASE = "https://backend.composio.dev/api/v3.1/tools/execute"
_COMPOSIO_EXECUTE_URL = f"{_COMPOSIO_EXECUTE_BASE}/{COMPOSIO_UPSERT_ROWS_TOOL}"

_GOOGLE_SHEETS_PATH = re.compile(r"^/spreadsheets/d/([A-Za-z0-9_-]{6,})(?:/|$)")

LEADS_SHEET_NAME = "01 Leads"
LEADS_KEY_COLUMN = "Lead ID"
LEADS_HEADERS = [
    "Lead ID",
    "Channel",
    "Stage",
    "Fit",
    "Pain Level",
    "Next Action",
]

FOLLOWUPS_SHEET_NAME = "08 Follow-ups"
FOLLOWUPS_KEY_COLUMN = "Lead ID"
FOLLOWUPS_HEADERS = ["Lead ID", "Due Date", "Channel", "Status", "Result"]
_FOLLOWUP_STATUSES = frozenset({STATUS_PENDING, STATUS_CANCELLED, STATUS_RECOVERED})
_FOLLOWUP_RESULTS = frozenset({REASON_MEETING_OFFERED, REASON_MEETING_BOOKED})

DEALS_SHEET_NAME = "05 Deals"
DEALS_KEY_COLUMN = "Lead ID"
DEALS_HEADERS = [
    "Lead ID",
    "Stage",
    "Source",
    "Attribution Confidence",
    "Expected Value",
    "Closed Value",
]

MEETINGS_SHEET_NAME = "04 Meetings"
MEETINGS_KEY_COLUMN = "Lead ID"
MEETINGS_HEADERS = [
    "Lead ID",
    "Status",
    "Source",
    "Time",
    "Calendar Event ID",
    "Summary",
]
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MODEL_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")

ACTIVITY_SHEET_NAME = "10 Mia Activity"
ACTIVITY_KEY_COLUMN = "Run ID"
ACTIVITY_HEADERS = [
    "Run ID",
    "Date",
    "Channel",
    "Next Action",
    "Model",
    "Kill Switch",
    "Cost",
    "Lead ID",
]

SOURCES_SHEET_NAME = "06 Lead Sources"
SOURCES_KEY_COLUMN = "Lead ID"
SOURCES_HEADERS = [
    "Lead ID",
    "UTM Source",
    "UTM Medium",
    "UTM Campaign",
    "UTM Content",
    "Landing",
    "Referrer",
]

KPI_SHEET_NAME = "09 Weekly KPI"
KPI_KEY_COLUMN = "Week Start"
KPI_HEADERS = [
    "Week Start",
    "Leads",
    "Meetings Offered",
    "Handoffs",
    "Messages In",
    "Follow-ups Pending",
]

CONTENT_SHEET_NAME = "07 Content Performance"
CONTENT_KEY_COLUMN = "Media ID"
CONTENT_HEADERS = [
    "Media ID",
    "Type",
    "Views",
    "Reach",
    "Likes",
    "Comments",
    "Saved",
    "Lead Signals",
]

CRM_WORKSPACE_TABS: tuple[tuple[str, list[str]], ...] = (
    (CONTACTS_TAB, list(CONTACTS_HEADERS)),
    (CONTACTS_ACTIVITY_TAB, list(CONTACTS_ACTIVITY_HEADERS)),
)
CRM_WORKSPACE_SCHEMA_VERSION = "mia-contacts-v1"
CRM_WORKSPACE_SCHEMA_RANGE = f"{CONTACTS_ACTIVITY_TAB}!F1"


def _crm_header_range(sheet_name: str, headers: list[str]) -> str:
    """Contacts is A1:N1 (14 cols). Owner-read 10-col bound does not apply here."""
    end = chr(ord("A") + len(headers) - 1)
    return f"{sheet_name}!A1:{end}1"


_METRIC_PATTERN = re.compile(r"^[0-9]*$")
_A1_RANGE_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9 _-]{1,80}!)?[A-Z]{1,3}[1-9][0-9]{0,5}(?::[A-Z]{1,3}[1-9][0-9]{0,5})?$"
)
_A1_ENDPOINT_PATTERN = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,5})$")
MAX_OWNER_SHEET_ROWS = 20
MAX_OWNER_SHEET_COLUMNS = 10
MAX_OWNER_SHEET_CELL_CHARS = 500


def _normalize_sheet_values(raw: object) -> list[list[str]]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_OWNER_SHEET_ROWS:
        raise ValueError("values must contain 1-20 rows")
    values: list[list[str]] = []
    for row in raw:
        if not isinstance(row, list) or not row or len(row) > MAX_OWNER_SHEET_COLUMNS:
            raise ValueError("each row must contain 1-10 cells")
        normalized_row: list[str] = []
        for cell in row:
            if not isinstance(cell, str):
                raise ValueError("sheet cells must be strings")
            text = cell.strip()
            if not text:
                raise ValueError("sheet cells must not be empty")
            if len(text) > MAX_OWNER_SHEET_CELL_CHARS:
                raise ValueError("sheet cell exceeds 500 characters")
            if text.startswith("="):
                raise ValueError("formula-leading sheet values are forbidden")
            normalized_row.append(text)
        values.append(normalized_row)
    return values


def _normalize_sheet_read_values(
    raw: object, *, max_columns: int = MAX_OWNER_SHEET_COLUMNS
) -> list[list[str]]:
    if not isinstance(raw, list) or len(raw) > MAX_OWNER_SHEET_ROWS:
        raise AdapterSchemaError()
    values: list[list[str]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) > max_columns:
            raise AdapterSchemaError()
        normalized_row: list[str] = []
        for cell in row:
            if not isinstance(cell, str) or len(cell) > MAX_OWNER_SHEET_CELL_CHARS:
                raise AdapterSchemaError()
            normalized_row.append(cell)
        values.append(normalized_row)
    return values


def validate_owner_sheet_request(
    *,
    spreadsheet_id: str,
    a1_range: str,
    values: object | None,
    allowed_spreadsheet_ids: frozenset[str],
) -> tuple[str, str, list[list[str]]]:
    raw_target = spreadsheet_id.strip()
    # URL convenience is read-only. A write must keep the original explicit opaque ID
    # so the owner-text binding and audit target cannot be changed by URL parsing.
    target = normalize_owner_spreadsheet_id(raw_target) if values is None else raw_target
    if values is not None and "://" in target:
        target = ""
    bounded_range = a1_range.strip()
    if a1_targets_archive_tab(bounded_range):
        raise ValueError("01 Leads is an archive tab and is banned")
    if "!" not in bounded_range:
        bounded_range = f"{CONTACTS_TAB}!{bounded_range}"
    if not target or target not in allowed_spreadsheet_ids:
        raise ValueError("spreadsheet id is not allowlisted")
    max_rows, max_columns = _parse_bounded_a1_range(bounded_range)
    if values is None:
        return target, bounded_range, []
    normalized = _normalize_sheet_values(values)
    if len(normalized) > max_rows or any(len(row) > max_columns for row in normalized):
        raise ValueError("values exceed the target A1 range")
    return target, bounded_range, normalized


def normalize_owner_spreadsheet_id(reference: str) -> str:
    """Accept an opaque ID or extract one from an exact Google Sheets URL.

    A URL is only a convenient reference; the extracted ID must still pass the existing
    owner allowlist. Other hosts and other Google document types are never accepted.
    """
    value = reference.strip()
    if not value:
        return ""
    if "://" not in value:
        return value
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "docs.google.com":
        return ""
    match = _GOOGLE_SHEETS_PATH.match(parsed.path)
    return match.group(1) if match is not None else ""


def _parse_bounded_a1_range(a1_range: str) -> tuple[int, int]:
    """Validate endpoint order and the approved 20-row by 10-column request window."""
    if not _A1_RANGE_PATTERN.fullmatch(a1_range):
        raise ValueError("range must be a bounded A1 range")
    _prefix, _bang, cells = a1_range.partition("!")
    if not _bang:
        cells = _prefix
    start, separator, end = cells.partition(":")
    if not separator:
        end = start
    start_match = _A1_ENDPOINT_PATTERN.fullmatch(start)
    end_match = _A1_ENDPOINT_PATTERN.fullmatch(end)
    if start_match is None or end_match is None:
        raise ValueError("range must be a bounded A1 range")
    start_column = _a1_column_number(start_match.group(1))
    end_column = _a1_column_number(end_match.group(1))
    start_row = int(start_match.group(2))
    end_row = int(end_match.group(2))
    if end_column < start_column or end_row < start_row:
        raise ValueError("range endpoints must be ordered")
    row_span = end_row - start_row + 1
    column_span = end_column - start_column + 1
    tab = sheet_tab_from_a1(a1_range)
    column_limit = CONTACTS_READ_COLUMNS if tab == CONTACTS_TAB else MAX_OWNER_SHEET_COLUMNS
    if row_span > MAX_OWNER_SHEET_ROWS or column_span > column_limit:
        raise ValueError("range exceeds the 20-row by 10-column limit")
    return row_span, column_span


def _a1_column_number(column: str) -> int:
    number = 0
    for letter in column:
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


class LeadMirrorRow(BaseModel):
    lead_id: str
    channel: str
    stage: str
    fit: str
    pain_level: int
    next_action: str


class FollowUpMirrorRow(BaseModel):
    lead_id: str
    due_at: str
    channel: str
    status: str
    result: str


class DealMirrorRow(BaseModel):
    lead_id: str
    stage: str
    source: str
    attribution_confidence: str
    expected_value: str = ""
    closed_value: str = ""


class MeetingMirrorRow(BaseModel):
    lead_id: str
    status: str
    source: str
    scheduled_at: str = ""
    calendar_event_id: str = ""
    summary: str = ""


class ActivityMirrorRow(BaseModel):
    run_id: str
    occurred_on: str
    channel: str
    next_action: str
    model: str = Field(max_length=64)
    kill_switch: bool
    cost_usd: int = Field(ge=0)
    lead_id: str | None = None


class SourceMirrorRow(BaseModel):
    lead_id: str
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    utm_content: str = ""
    landing_page: str = ""
    referrer: str = ""


class KpiMirrorRow(BaseModel):
    week_start: str
    leads: int = Field(ge=0)
    meetings_offered: int = Field(ge=0)
    handoffs: int = Field(ge=0)
    messages_in: int = Field(ge=0)
    follow_ups_pending: int = Field(ge=0)


class ContentMirrorRow(BaseModel):
    media_id: str
    media_type: str
    views: str = ""
    reach: str = ""
    likes: str = ""
    comments: str = ""
    saved: str = ""
    lead_signals: int = Field(default=0, ge=0)


class SheetsPort(Protocol):
    def ensure_crm_workspace(self) -> None: ...

    def list_sheet_names(self, *, spreadsheet_id: str) -> list[str]: ...

    def read_values(self, *, spreadsheet_id: str, a1_range: str) -> list[list[str]]: ...

    def update_values(
        self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]
    ) -> None: ...

    def append_values(
        self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]
    ) -> None: ...
    def upsert_lead(self, row: LeadMirrorRow) -> None: ...

    def upsert_source(self, row: SourceMirrorRow) -> None: ...

    def upsert_follow_up(self, row: FollowUpMirrorRow) -> None: ...

    def upsert_deal(self, row: DealMirrorRow) -> None: ...

    def upsert_meeting(self, row: MeetingMirrorRow) -> None: ...

    def upsert_activity(self, row: ActivityMirrorRow) -> None: ...

    def upsert_kpi(self, row: KpiMirrorRow) -> None: ...

    def upsert_content(self, row: ContentMirrorRow) -> None: ...

    def write_locked_contact(self, cells: list[str], *, key_column: str) -> None: ...

    def append_locked_activity(self, cells: list[str]) -> None: ...

    def read_locked_contacts(self) -> list[list[str]]: ...


class DisabledSheetsPort:
    def ensure_crm_workspace(self) -> None:
        return

    def list_sheet_names(self, *, spreadsheet_id: str) -> list[str]:
        del spreadsheet_id
        return []

    def read_values(self, *, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
        del spreadsheet_id, a1_range
        return []

    def update_values(self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]) -> None:
        del spreadsheet_id, a1_range, values

    def append_values(self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]) -> None:
        del spreadsheet_id, a1_range, values

    def upsert_lead(self, row: LeadMirrorRow) -> None:
        del row

    def upsert_source(self, row: SourceMirrorRow) -> None:
        del row

    def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
        del row

    def upsert_deal(self, row: DealMirrorRow) -> None:
        del row

    def upsert_meeting(self, row: MeetingMirrorRow) -> None:
        del row

    def upsert_activity(self, row: ActivityMirrorRow) -> None:
        del row

    def upsert_kpi(self, row: KpiMirrorRow) -> None:
        del row

    def upsert_content(self, row: ContentMirrorRow) -> None:
        del row

    def write_locked_contact(self, cells: list[str], *, key_column: str) -> None:
        del cells, key_column

    def append_locked_activity(self, cells: list[str]) -> None:
        del cells

    def read_locked_contacts(self) -> list[list[str]]:
        return []


class ComposioSheetsPort:
    """Live Composio adapter. HTTP/transport raises AdapterHttpError; 200 unsuccessful skips."""

    _workspace_ready_until: dict[tuple[str, str, str], float] = {}
    _workspace_ready_lock = Lock()
    _workspace_refresh_seconds = 3600.0

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        spreadsheet_id: str = "",
        allowed_spreadsheet_ids: frozenset[str] = frozenset(),
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._spreadsheet_id = spreadsheet_id.strip()
        authorized = set(allowed_spreadsheet_ids)
        if self._spreadsheet_id:
            authorized.add(self._spreadsheet_id)
        self._allowed_spreadsheet_ids = frozenset(authorized)
        self._client = client
        self._crm_ready = False
        self._workspace_key = (
            sha256(api_key.encode("utf-8")).hexdigest()[:16],
            user_id,
            self._spreadsheet_id,
        )

    def _execute_tool(self, tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_GOOGLESHEETS_VERSION,
            "arguments": arguments,
        }
        request_headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        url = f"{_COMPOSIO_EXECUTE_BASE}/{tool_slug}"
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, headers=request_headers)
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(url, json=payload, headers=request_headers)
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("successful"), bool):
                raise AdapterSchemaError()
            if body["successful"] is False:
                raise AdapterResponseError()
            data = body.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    raise AdapterSchemaError() from None
            if isinstance(data, dict):
                return data
            raise AdapterSchemaError()
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            raise AdapterSchemaError() from None

    def read_values(self, *, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
        target, bounded_range, _ = validate_owner_sheet_request(
            spreadsheet_id=spreadsheet_id,
            a1_range=a1_range,
            values=None,
            allowed_spreadsheet_ids=self._allowed_spreadsheet_ids,
        )
        data = self._execute_tool(
            COMPOSIO_VALUES_GET_TOOL,
            {"spreadsheetId": target, "range": bounded_range},
        )
        raw_values = (data or {}).get("values")
        if not isinstance(raw_values, list):
            raise AdapterSchemaError()
        tab = sheet_tab_from_a1(bounded_range)
        max_columns = CONTACTS_READ_COLUMNS if tab == CONTACTS_TAB else MAX_OWNER_SHEET_COLUMNS
        return _normalize_sheet_read_values(raw_values, max_columns=max_columns)

    def list_sheet_names(self, *, spreadsheet_id: str) -> list[str]:
        """Discover tabs only inside an already allowlisted spreadsheet.

        This is deliberately not Drive discovery: it receives the same exact target
        check as a values read, then returns only small plain-text tab titles.  It
        lets an owner recover from an empty first tab without guessing a range.
        """
        target, _bounded_range, _ = validate_owner_sheet_request(
            spreadsheet_id=spreadsheet_id,
            a1_range="A1",
            values=None,
            allowed_spreadsheet_ids=self._allowed_spreadsheet_ids,
        )
        data = self._execute_tool(
            COMPOSIO_GET_SHEET_NAMES_TOOL, {"spreadsheetId": target}
        )
        raw_names = (data or {}).get("sheetNames")
        if not isinstance(raw_names, list):
            # Current provider responses have used both field names. Accept neither
            # silently: an unknown shape stays an adapter error rather than a false
            # "no tabs" report.
            raw_names = (data or {}).get("sheets")
        if not isinstance(raw_names, list) or len(raw_names) > 50:
            raise AdapterSchemaError()
        names: list[str] = []
        for raw_name in raw_names:
            if not isinstance(raw_name, str):
                raise AdapterSchemaError()
            name = raw_name.strip()
            if not name or len(name) > 80 or "\n" in name or "\r" in name:
                raise AdapterSchemaError()
            names.append(name)
        return names

    def update_values(self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]) -> None:
        target, bounded_range, bounded_values = validate_owner_sheet_request(
            spreadsheet_id=spreadsheet_id,
            a1_range=a1_range,
            values=values,
            allowed_spreadsheet_ids=self._allowed_spreadsheet_ids,
        )
        self._execute_tool(
            COMPOSIO_VALUES_UPDATE_TOOL,
            {
                "spreadsheetId": target,
                "range": bounded_range,
                "values": bounded_values,
                "valueInputOption": "RAW",
            },
        )

    def append_values(self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]) -> None:
        target, bounded_range, bounded_values = validate_owner_sheet_request(
            spreadsheet_id=spreadsheet_id,
            a1_range=a1_range,
            values=values,
            allowed_spreadsheet_ids=self._allowed_spreadsheet_ids,
        )
        self._execute_tool(
            COMPOSIO_VALUES_APPEND_TOOL,
            {
                "spreadsheetId": target,
                "range": bounded_range,
                "values": bounded_values,
                "valueInputOption": "RAW",
            },
        )

    def ensure_crm_workspace(self) -> None:
        """Create and repair Mia's fixed CRM tabs in her configured spreadsheet.

        This authority is intentionally narrower than the owner Sheets allowlist:
        only ``MIA_SHEETS_SPREADSHEET_ID`` can be organized, no Drive lookup occurs,
        and existing business rows are never cleared. Repeated calls are a no-op in
        the same process after the complete structure has been confirmed.
        """
        if self._crm_ready:
            return
        spreadsheet_id = self._spreadsheet_id
        if not spreadsheet_id:
            return
        if spreadsheet_id not in self._allowed_spreadsheet_ids:
            raise ValueError("configured CRM spreadsheet id is not allowlisted")
        if self._workspace_ready_until.get(self._workspace_key, 0.0) > monotonic():
            self._crm_ready = True
            return

        with self._workspace_ready_lock:
            if self._workspace_ready_until.get(self._workspace_key, 0.0) > monotonic():
                self._crm_ready = True
                return
            existing = set(self.list_sheet_names(spreadsheet_id=spreadsheet_id))
            marker_is_current = False
            if CONTACTS_ACTIVITY_TAB in existing:
                marker_data = self._execute_tool(
                    COMPOSIO_VALUES_GET_TOOL,
                    {
                        "spreadsheetId": spreadsheet_id,
                        "range": CRM_WORKSPACE_SCHEMA_RANGE,
                    },
                )
                marker_is_current = (marker_data or {}).get("values") == [
                    [CRM_WORKSPACE_SCHEMA_VERSION]
                ]

            header_is_current: dict[str, bool] = {}
            for sheet_name, headers in CRM_WORKSPACE_TABS:
                if sheet_name not in existing:
                    header_is_current[sheet_name] = False
                    continue
                header_range = _crm_header_range(sheet_name, headers)
                header_data = self._execute_tool(
                    COMPOSIO_VALUES_GET_TOOL,
                    {"spreadsheetId": spreadsheet_id, "range": header_range},
                )
                header_is_current[sheet_name] = (header_data or {}).get("values") == [
                    headers
                ]

            if marker_is_current and all(header_is_current.values()):
                self._workspace_ready_until[self._workspace_key] = (
                    monotonic() + self._workspace_refresh_seconds
                )
                self._crm_ready = True
                return
            for sheet_name, _headers in CRM_WORKSPACE_TABS:
                if sheet_name in existing:
                    continue
                self._execute_tool(
                    COMPOSIO_ADD_SHEET_TOOL,
                    {
                        "spreadsheetId": spreadsheet_id,
                        "properties": {"title": sheet_name},
                    },
                )

            for sheet_name, headers in CRM_WORKSPACE_TABS:
                if header_is_current[sheet_name]:
                    continue
                self._execute_tool(
                    COMPOSIO_VALUES_UPDATE_TOOL,
                    {
                        "spreadsheetId": spreadsheet_id,
                        "range": _crm_header_range(sheet_name, headers),
                        "values": [headers],
                        "valueInputOption": "RAW",
                    },
                )
            self._execute_tool(
                COMPOSIO_VALUES_UPDATE_TOOL,
                {
                    "spreadsheetId": spreadsheet_id,
                    "range": CRM_WORKSPACE_SCHEMA_RANGE,
                    "values": [[CRM_WORKSPACE_SCHEMA_VERSION]],
                    "valueInputOption": "RAW",
                },
            )
            self._workspace_ready_until[self._workspace_key] = (
                monotonic() + self._workspace_refresh_seconds
            )
            self._crm_ready = True

    def _execute_upsert(
        self,
        *,
        sheet_name: str,
        key_column: str,
        headers: list[str],
        values: list[list[object]],
        spreadsheet_id: str | None = None,
    ) -> None:
        spreadsheet_id = (spreadsheet_id or self._spreadsheet_id or LOCKED_SPREADSHEET_ID).strip()
        if not spreadsheet_id:
            return
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_GOOGLESHEETS_VERSION,
            "arguments": {
                "spreadsheetId": spreadsheet_id,
                "sheetName": sheet_name,
                "keyColumn": key_column,
                "headers": headers,
                "rows": values,
                "strictMode": True,
            },
        }
        request_headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _COMPOSIO_EXECUTE_URL,
                    json=payload,
                    headers=request_headers,
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        _COMPOSIO_EXECUTE_URL,
                        json=payload,
                        headers=request_headers,
                    )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("successful") is not True:
                return
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return

    def write_locked_contact(self, cells: list[str], *, key_column: str) -> None:
        payload_values = [list(cells)]
        self._execute_upsert(
            sheet_name=CONTACTS_TAB,
            key_column=key_column,
            headers=list(CONTACTS_HEADERS),
            values=payload_values,
            spreadsheet_id=self._spreadsheet_id or LOCKED_SPREADSHEET_ID,
        )

    def read_locked_contacts(self) -> list[list[str]]:
        data = self._execute_tool(
            COMPOSIO_VALUES_GET_TOOL,
            {
                "spreadsheetId": self._spreadsheet_id or LOCKED_SPREADSHEET_ID,
                "range": f"{CONTACTS_TAB}!A1:N100",
            },
        )
        raw_values = (data or {}).get("values")
        if not isinstance(raw_values, list):
            return []
        rows: list[list[str]] = []
        for row in raw_values[:100]:
            if not isinstance(row, list):
                continue
            cells = [str(cell) if cell is not None else "" for cell in row[:14]]
            rows.append(cells)
        return rows

    def append_locked_activity(self, cells: list[str]) -> None:
        self._execute_tool(
            COMPOSIO_VALUES_APPEND_TOOL,
            {
                "spreadsheetId": self._spreadsheet_id or LOCKED_SPREADSHEET_ID,
                "range": f"{CONTACTS_ACTIVITY_TAB}!A:E",
                "values": [list(cells)],
                "valueInputOption": "RAW",
            },
        )

    def upsert_lead(self, row: LeadMirrorRow) -> None:
        del row

    def upsert_source(self, row: SourceMirrorRow) -> None:
        del row

    def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
        del row

    def upsert_deal(self, row: DealMirrorRow) -> None:
        del row

    def upsert_meeting(self, row: MeetingMirrorRow) -> None:
        del row

    def upsert_activity(self, row: ActivityMirrorRow) -> None:
        del row

    def upsert_kpi(self, row: KpiMirrorRow) -> None:
        del row

    def upsert_content(self, row: ContentMirrorRow) -> None:
        del row


class FakeSheetsPort:
    """Test double. Dict keyed by lead_id; second upsert overwrites."""

    def __init__(self) -> None:
        self._rows: dict[str, LeadMirrorRow] = {}
        self._source_rows: dict[str, SourceMirrorRow] = {}
        self._follow_up_rows: dict[str, FollowUpMirrorRow] = {}
        self._deal_rows: dict[str, DealMirrorRow] = {}
        self._meeting_rows: dict[str, MeetingMirrorRow] = {}
        self._activity_rows: dict[str, ActivityMirrorRow] = {}
        self._kpi_rows: dict[str, KpiMirrorRow] = {}
        self._content_rows: dict[str, ContentMirrorRow] = {}
        self.owner_values: dict[tuple[str, str], list[list[str]]] = {}
        self.owner_operations: list[tuple[str, str, str, list[list[str]]]] = []
        self.sheet_names: dict[str, list[str]] = {}
        self.locked_contacts: list[list[str]] = []
        self.crm_workspace_ensures = 0

    def ensure_crm_workspace(self) -> None:
        self.crm_workspace_ensures += 1

    def list_sheet_names(self, *, spreadsheet_id: str) -> list[str]:
        return list(self.sheet_names.get(spreadsheet_id, []))

    def read_values(self, *, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
        return [list(row) for row in self.owner_values.get((spreadsheet_id, a1_range), [])]

    def update_values(self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]) -> None:
        normalized = _normalize_sheet_values(values)
        self.owner_values[(spreadsheet_id, a1_range)] = normalized
        self.owner_operations.append(("update", spreadsheet_id, a1_range, normalized))

    def append_values(self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]) -> None:
        normalized = _normalize_sheet_values(values)
        self.owner_values.setdefault((spreadsheet_id, a1_range), []).extend(normalized)
        self.owner_operations.append(("append", spreadsheet_id, a1_range, normalized))

    @property
    def rows(self) -> dict[str, LeadMirrorRow]:
        return dict(self._rows)

    @property
    def source_rows(self) -> dict[str, SourceMirrorRow]:
        return dict(self._source_rows)

    @property
    def follow_up_rows(self) -> dict[str, FollowUpMirrorRow]:
        return dict(self._follow_up_rows)

    @property
    def deal_rows(self) -> dict[str, DealMirrorRow]:
        return dict(self._deal_rows)

    @property
    def meeting_rows(self) -> dict[str, MeetingMirrorRow]:
        return dict(self._meeting_rows)

    @property
    def activity_rows(self) -> dict[str, ActivityMirrorRow]:
        return dict(self._activity_rows)

    @property
    def kpi_rows(self) -> dict[str, KpiMirrorRow]:
        return dict(self._kpi_rows)

    @property
    def content_rows(self) -> dict[str, ContentMirrorRow]:
        return dict(self._content_rows)

    def write_locked_contact(self, cells: list[str], *, key_column: str) -> None:
        row = list(cells)
        self.locked_contacts.append(row)
        self.owner_operations.append(
            ("contact", LOCKED_SPREADSHEET_ID, key_column, [row])
        )

    def read_locked_contacts(self) -> list[list[str]]:
        return [list(CONTACTS_HEADERS), *[list(row) for row in self.locked_contacts]]

    def append_locked_activity(self, cells: list[str]) -> None:
        self.owner_operations.append(
            ("activity", LOCKED_SPREADSHEET_ID, CONTACTS_ACTIVITY_TAB, [list(cells)])
        )

    def upsert_lead(self, row: LeadMirrorRow) -> None:
        self._rows[row.lead_id] = row

    def upsert_source(self, row: SourceMirrorRow) -> None:
        self._source_rows[row.lead_id] = row

    def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
        self._follow_up_rows[row.lead_id] = row

    def upsert_deal(self, row: DealMirrorRow) -> None:
        self._deal_rows[row.lead_id] = row

    def upsert_meeting(self, row: MeetingMirrorRow) -> None:
        self._meeting_rows[row.lead_id] = row

    def upsert_activity(self, row: ActivityMirrorRow) -> None:
        self._activity_rows[row.run_id] = row

    def upsert_kpi(self, row: KpiMirrorRow) -> None:
        self._kpi_rows[row.week_start] = row

    def upsert_content(self, row: ContentMirrorRow) -> None:
        self._content_rows[row.media_id] = row


def sheets_mirror_claim_key(inbound_id: str, tab: str) -> str:
    return f"{inbound_id}:sheets:{tab}"


def claim_sheets_mirror(*, store: LeadStore, inbound_id: str, tab: str) -> bool:
    if not inbound_id or tab not in SHEETS_MIRROR_TABS:
        return False
    return store.claim_operation(
        scope=SHEETS_MIRROR_SCOPE,
        key=sheets_mirror_claim_key(inbound_id, tab),
    )


def complete_sheets_mirror(*, store: LeadStore, inbound_id: str, tab: str) -> None:
    if not inbound_id or tab not in SHEETS_MIRROR_TABS:
        return
    store.complete_operation(
        scope=SHEETS_MIRROR_SCOPE,
        key=sheets_mirror_claim_key(inbound_id, tab),
        result_json='{"ok": true}',
    )


def mirror_lead(*, sheets: SheetsPort, row: LeadMirrorRow, kill_switch: bool) -> bool:
    """R1 assert_allowed before upsert. Returns True if written; never raises."""
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_lead(row)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def _sanitized_source_row(row: SourceMirrorRow) -> SourceMirrorRow | None:
    if not row.lead_id or "@" in row.lead_id:
        return None
    cleaned = sanitize_attribution(
        {
            "utm_source": row.utm_source or None,
            "utm_medium": row.utm_medium or None,
            "utm_campaign": row.utm_campaign or None,
            "utm_content": row.utm_content or None,
            "landing_page": row.landing_page or None,
            "referrer": row.referrer or None,
        }
    )
    if not cleaned:
        return None
    return SourceMirrorRow(
        lead_id=row.lead_id,
        utm_source=cleaned.get("utm_source", ""),
        utm_medium=cleaned.get("utm_medium", ""),
        utm_campaign=cleaned.get("utm_campaign", ""),
        utm_content=cleaned.get("utm_content", ""),
        landing_page=cleaned.get("landing_page", ""),
        referrer=cleaned.get("referrer", ""),
    )


def mirror_source(*, sheets: SheetsPort, row: SourceMirrorRow, kill_switch: bool) -> bool:
    """R1 assert_allowed before upsert. Returns True if written; never raises."""
    clean = _sanitized_source_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_source(clean)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def _sanitized_follow_up_row(row: FollowUpMirrorRow) -> FollowUpMirrorRow | None:
    try:
        Channel(row.channel)
    except ValueError:
        return None
    if row.status not in _FOLLOWUP_STATUSES:
        return None
    if row.result not in _FOLLOWUP_RESULTS:
        return None
    if _DATE_PATTERN.match(row.due_at) is None:
        return None
    return row


def activity_occurred_on(timezone: str) -> str | None:
    try:
        return datetime.now(UTC).astimezone(ZoneInfo(timezone)).date().isoformat()
    except (ValueError, OSError, KeyError):
        return None


def activity_mirror_row_from_persisted(
    *,
    run_id: str,
    lead_id: str | None,
    channel: str,
    next_action: str,
    model: str,
    kill_switch: bool,
    cost_usd: int,
    timezone: str,
) -> ActivityMirrorRow | None:
    occurred_on = activity_occurred_on(timezone)
    if occurred_on is None:
        return None
    try:
        row = ActivityMirrorRow(
            run_id=run_id,
            occurred_on=occurred_on,
            channel=channel,
            next_action=next_action,
            model=model,
            kill_switch=kill_switch,
            cost_usd=cost_usd,
            lead_id=lead_id,
        )
    except ValidationError:
        return None
    return _sanitized_activity_row(row)


def _sanitized_activity_row(row: ActivityMirrorRow) -> ActivityMirrorRow | None:
    if not row.run_id or "@" in row.run_id:
        return None
    if _DATE_PATTERN.match(row.occurred_on) is None:
        return None
    try:
        Channel(row.channel)
    except ValueError:
        return None
    try:
        NextAction(row.next_action)
    except ValueError:
        return None
    model = row.model.strip()
    if not model or "@" in model or _MODEL_PATTERN.match(model) is None:
        return None
    if row.cost_usd < 0:
        return None
    return ActivityMirrorRow(
        run_id=row.run_id,
        occurred_on=row.occurred_on,
        channel=row.channel,
        next_action=row.next_action,
        model=model[:64],
        kill_switch=row.kill_switch,
        cost_usd=row.cost_usd,
        lead_id=row.lead_id,
    )


def mirror_activity(*, sheets: SheetsPort, row: ActivityMirrorRow, kill_switch: bool) -> bool:
    """R1 assert_allowed before upsert. Returns True if written; never raises."""
    clean = _sanitized_activity_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_activity(clean)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def mirror_follow_up(*, sheets: SheetsPort, row: FollowUpMirrorRow, kill_switch: bool) -> bool:
    """R1 assert_allowed before upsert. Returns True if written; never raises."""
    clean = _sanitized_follow_up_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_follow_up(clean)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def _sanitized_deal_row(row: DealMirrorRow) -> DealMirrorRow | None:
    if not row.lead_id or "@" in row.lead_id:
        return None
    if row.stage not in ALLOWLISTED_STAGES:
        return None
    if row.attribution_confidence not in ALLOWLISTED_CONFIDENCE:
        return None
    try:
        Channel(row.source)
    except ValueError:
        return None
    if row.expected_value != "" or row.closed_value != "":
        return None
    return row


def mirror_deal(*, sheets: SheetsPort, row: DealMirrorRow, kill_switch: bool) -> bool:
    """R1 assert_allowed before upsert. Returns True if written; never raises."""
    clean = _sanitized_deal_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_deal(clean)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def _sanitized_meeting_row(row: MeetingMirrorRow) -> MeetingMirrorRow | None:
    if not row.lead_id or "@" in row.lead_id:
        return None
    if row.status not in ALLOWLISTED_STATUSES:
        return None
    try:
        Channel(row.source)
    except ValueError:
        return None
    if row.summary:
        return None
    if row.status == STATUS_OFFERED:
        if row.scheduled_at or row.calendar_event_id:
            return None
        return row
    if row.status in {STATUS_BOOKED, STATUS_CANCELLATION_REQUESTED}:
        normalized = normalize_scheduled_at_utc(row.scheduled_at)
        event_id = sanitize_event_id(row.calendar_event_id)
        if normalized is None or event_id is None:
            return None
        return row.model_copy(update={"scheduled_at": normalized, "calendar_event_id": event_id})
    return row


def mirror_meeting(*, sheets: SheetsPort, row: MeetingMirrorRow, kill_switch: bool) -> bool:
    """R1 assert_allowed before upsert. Returns True if written; never raises."""
    clean = _sanitized_meeting_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_meeting(clean)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def _sanitized_kpi_row(row: KpiMirrorRow) -> KpiMirrorRow | None:
    if _DATE_PATTERN.match(row.week_start) is None:
        return None
    if (
        row.leads < 0
        or row.meetings_offered < 0
        or row.handoffs < 0
        or row.messages_in < 0
        or row.follow_ups_pending < 0
    ):
        return None
    return row


def mirror_kpi(*, sheets: SheetsPort, row: KpiMirrorRow, kill_switch: bool) -> bool:
    """R1 assert_allowed before upsert. Returns True if written; never raises."""
    clean = _sanitized_kpi_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_kpi(clean)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def maybe_mirror_weekly_kpi(
    *,
    store: LeadStore,
    sheets: SheetsPort,
    settings: Settings,
    kill_switch: bool,
) -> bool:
    if demo_mode_active(settings):
        return False
    snapshot = compute_weekly_kpi(store, timezone=settings.calendar_timezone)
    if snapshot is None:
        return False
    return mirror_kpi(
        sheets=sheets,
        row=KpiMirrorRow(
            week_start=snapshot.week_start,
            leads=snapshot.leads,
            meetings_offered=snapshot.meetings_offered,
            handoffs=snapshot.handoffs,
            messages_in=snapshot.messages_in,
            follow_ups_pending=snapshot.follow_ups_pending,
        ),
        kill_switch=kill_switch,
    )


def mirror_sales_turn(
    *,
    store: LeadStore,
    sheets: SheetsPort,
    settings: Settings,
    provider: str,
    channel: Channel,
    inbound_id: str,
    conversation_id: str,
    lead_id: str,
    run_id: str,
    next_action: str,
    kill_switch: bool,
    measure_elapsed: Callable[[float], int] = elapsed_ms,
) -> ToolOutcome | None:
    """Mirror one completed sales turn with the existing claim/outcome ordering."""
    if demo_mode_active(settings):
        return None
    if not claim_sheets_mirror(store=store, inbound_id=inbound_id, tab="sales"):
        return None

    started = perf_counter()
    sales = store.get_sales(lead_id)
    sheets_written = mirror_lead(
        sheets=sheets,
        row=LeadMirrorRow(
            lead_id=lead_id,
            channel=channel.value,
            stage=store.get_lead_stage(lead_id),
            fit=sales.fit.value,
            pain_level=int(sales.pain_level),
            next_action=next_action,
        ),
        kill_switch=kill_switch,
    )
    follow_up_written = False
    follow_up = store.get_follow_up(lead_id)
    if follow_up is not None:
        follow_up_written = mirror_follow_up(
            sheets=sheets,
            row=FollowUpMirrorRow(
                lead_id=lead_id,
                due_at=follow_up.due_at,
                channel=follow_up.channel,
                status=follow_up.status,
                result=follow_up.reason,
            ),
            kill_switch=kill_switch,
        )
    deal_written = False
    deal = store.get_deal(lead_id)
    if deal is not None:
        deal_written = mirror_deal(
            sheets=sheets,
            row=DealMirrorRow(
                lead_id=lead_id,
                stage=deal.stage,
                source=deal.source,
                attribution_confidence=deal.attribution_confidence,
                expected_value=deal.expected_value,
                closed_value=deal.closed_value,
            ),
            kill_switch=kill_switch,
        )
    meeting_written = False
    meeting = store.get_meeting(lead_id)
    if meeting is not None:
        meeting_written = mirror_meeting(
            sheets=sheets,
            row=MeetingMirrorRow(
                lead_id=lead_id,
                status=meeting.status,
                source=meeting.source,
                scheduled_at=meeting.scheduled_at,
                calendar_event_id=meeting.calendar_event_id,
                summary=meeting.summary,
            ),
            kill_switch=kill_switch,
        )
    activity_written = False
    ai_run = store.get_ai_run(run_id)
    if ai_run is not None:
        activity_row = activity_mirror_row_from_persisted(
            run_id=ai_run.run_id,
            lead_id=ai_run.lead_id,
            channel=ai_run.channel,
            next_action=ai_run.next_action,
            model=ai_run.model,
            kill_switch=ai_run.kill_switch,
            cost_usd=ai_run.cost_usd,
            timezone=settings.calendar_timezone,
        )
        if activity_row is not None:
            activity_written = mirror_activity(
                sheets=sheets,
                row=activity_row,
                kill_switch=kill_switch,
            )
    kpi_written = maybe_mirror_weekly_kpi(
        store=store,
        sheets=sheets,
        settings=settings,
        kill_switch=kill_switch,
    )
    outcome = sheets_mirror_outcome(
        int(sheets_written)
        + int(follow_up_written)
        + int(deal_written)
        + int(meeting_written)
        + int(activity_written)
        + int(kpi_written),
        latency_ms=measure_elapsed(started),
    )
    persist_tool_outcome(
        store,
        provider=provider,
        channel=channel,
        inbound_provider_event_id=inbound_id,
        conversation_id=conversation_id,
        lead_id=lead_id,
        outcome=outcome,
        correlation_id=run_id,
    )
    complete_sheets_mirror(store=store, inbound_id=inbound_id, tab="sales")
    return outcome


def _sanitized_content_row(row: ContentMirrorRow) -> ContentMirrorRow | None:
    if not is_allowlisted_media_id(row.media_id):
        return None
    if row.media_type not in ALLOWLISTED_MEDIA_TYPES:
        return None
    if row.lead_signals < 0:
        return None
    for field in ("views", "reach", "likes", "comments", "saved"):
        value = getattr(row, field)
        if value is None:
            return None
        if "@" in value or "http" in value.lower():
            return None
        if _METRIC_PATTERN.fullmatch(value) is None:
            return None
    return row


def mirror_content(*, sheets: SheetsPort, row: ContentMirrorRow, kill_switch: bool) -> bool:
    """R1 assert_allowed before upsert. Returns True if written; never raises."""
    clean = _sanitized_content_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_content(clean)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def maybe_mirror_content_insights(
    *,
    store: LeadStore,
    sheets: SheetsPort,
    settings: Settings,
    kill_switch: bool,
    inbound_id: str = "",
) -> ToolOutcome | None:
    if demo_mode_active(settings):
        return None
    if inbound_id:
        if not claim_sheets_mirror(store=store, inbound_id=inbound_id, tab="content"):
            return None
    started = perf_counter()
    written_count = 0
    for record in store.list_content_insights():
        if mirror_content(
            sheets=sheets,
            row=ContentMirrorRow(
                media_id=record.media_id,
                media_type=record.media_type,
                views=record.views,
                reach=record.reach,
                likes=record.likes,
                comments=record.comments,
                saved=record.saved,
                lead_signals=record.lead_signals,
            ),
            kill_switch=kill_switch,
        ):
            written_count += 1
    if inbound_id:
        complete_sheets_mirror(store=store, inbound_id=inbound_id, tab="content")
        return sheets_tab_mirror_outcome(
            "sheets_mirror_content",
            written_count,
            latency_ms=elapsed_ms(started),
        )
    return None


def build_sheets_port(settings: Settings) -> SheetsPort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioSheetsPort(
            api_key=api_key,
            user_id=user_id,
            spreadsheet_id=settings.resolved_sheets_spreadsheet_id(),
            allowed_spreadsheet_ids=settings.allowed_sheets_spreadsheet_ids(),
        )
    return DisabledSheetsPort()


def maintain_crm_workspace(settings: Settings) -> str:
    """Best-effort background maintenance for Mia's one configured CRM workbook.

    This function belongs in a worker/one-off command, never a visitor request. The
    result is deliberately aggregate and contains no spreadsheet id or provider body.
    """
    if settings.kill_switch:
        return "disabled"
    port = build_sheets_port(settings)
    if isinstance(port, DisabledSheetsPort):
        return "not_configured"
    try:
        port.ensure_crm_workspace()
    except (
        AdapterHttpError,
        AdapterResponseError,
        AdapterSchemaError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        return "unavailable"
    return "ready"
