"""Google Sheets mirror port.

Postgres is the system of record. This port upserts snapshots to ``01 Leads``,
``04 Meetings``, ``05 Deals``, ``06 Lead Sources``, ``08 Follow-ups``, ``09 Weekly KPI``, and
``10 Mia Activity`` — never read sheet data back. Lead source and weekly KPI
rows upsert on website session create; lead, follow-up, deal, meeting, activity, and
weekly KPI rows upsert after sales graph turns and owner-relevant website events
(finalization, WhatsApp-click briefing).

``01 Leads`` columns: Lead ID, Channel, Stage, Fit, Pain Level, Next Action,
Timestamp, Discovery Summary, WhatsApp Offered, Disqualified, Last Message Short.
No phone or email columns.

Production adapter: Composio ``GOOGLESHEETS`` toolkit version ``20260813_00``,
pin ``GOOGLESHEETS_UPSERT_ROWS`` only when ``MIA_COMPOSIO_API_KEY``,
``MIA_COMPOSIO_USER_ID``, and ``MIA_SHEETS_SPREADSHEET_ID`` are set.
If those are missing, ``DisabledSheetsPort`` no-ops and chat continues.
Never clear/delete/create-spreadsheet/read tools this slice.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol
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
from app.domain.events import Channel, sheets_tab_mirror_outcome
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
from app.domain.sales import NextAction, SalesState
from app.domain.tools import AdapterHttpError, ToolOutcome

SHEETS_MIRROR_SCOPE = "sheets_mirror"
SHEETS_MIRROR_TABS = frozenset({"sales", "session", "content"})

COMPOSIO_GOOGLESHEETS_VERSION = "20260813_00"
COMPOSIO_UPSERT_ROWS_TOOL = "GOOGLESHEETS_UPSERT_ROWS"
COMPOSIO_SEARCH_SPREADSHEETS_TOOL = "GOOGLESHEETS_SEARCH_SPREADSHEETS"
_COMPOSIO_EXECUTE_BASE = "https://backend.composio.dev/api/v3.1/tools/execute"
_COMPOSIO_EXECUTE_URL = (
    f"{_COMPOSIO_EXECUTE_BASE}/{COMPOSIO_UPSERT_ROWS_TOOL}"
)
PREFERRED_SHEET_NAME = "mia"

LEADS_SHEET_NAME = "01 Leads"
LEADS_KEY_COLUMN = "Lead ID"
LEADS_HEADERS = [
    "Lead ID",
    "Channel",
    "Stage",
    "Fit",
    "Pain Level",
    "Next Action",
    "Timestamp",
    "Discovery Summary",
    "WhatsApp Offered",
    "Disqualified",
    "Last Message Short",
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
_METRIC_PATTERN = re.compile(r"^[0-9]*$")

BUDGET_SHEET_NAME = "02 Campaign Budget"
BUDGET_KEY_COLUMN = "Campaign"
BUDGET_HEADERS = [
    "Campaign",
    "Monthly Budget",
    "Spend",
    "Expected Spend",
    "Remaining",
    "Projected",
    "Over Under",
    "Status",
]

PERF_SHEET_NAME = "03 Campaign Performance"
PERF_KEY_COLUMN = "Campaign"
PERF_HEADERS = [
    "Campaign",
    "Spend",
    "CTR",
    "CPC",
    "CPL",
    "Qualified CPL",
    "Meetings",
    "Deals",
    "Revenue",
    "ROAS",
]
_CAMPAIGN_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,32}$")
_NUMERIC_MIRROR_PATTERN = re.compile(r"^-?\d+(\.\d{1,2})?$")
_PACING_STATUSES = frozenset({"on_track", "over", "under", "uncertain"})


class LeadMirrorRow(BaseModel):
    lead_id: str
    channel: str
    stage: str
    fit: str
    pain_level: int
    next_action: str
    timestamp: str = ""
    discovery_summary: str = Field(default="", max_length=240)
    whatsapp_offered: str = ""
    disqualified: str = ""
    last_message_short: str = Field(default="", max_length=80)


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


class BudgetMirrorRow(BaseModel):
    campaign: str
    monthly_budget: str
    spend: str = ""
    expected_spend: str = ""
    remaining: str = ""
    projected: str = ""
    over_under: str = ""
    status: str


class PerformanceMirrorRow(BaseModel):
    campaign: str
    spend: str = ""
    ctr: str = ""
    cpc: str = ""
    cpl: str = ""
    qualified_cpl: str = ""
    meetings: str = ""
    deals: str = ""
    revenue: str = ""
    roas: str = ""


class SheetsPort(Protocol):
    def upsert_lead(self, row: LeadMirrorRow) -> None: ...

    def upsert_source(self, row: SourceMirrorRow) -> None: ...

    def upsert_follow_up(self, row: FollowUpMirrorRow) -> None: ...

    def upsert_deal(self, row: DealMirrorRow) -> None: ...

    def upsert_meeting(self, row: MeetingMirrorRow) -> None: ...

    def upsert_activity(self, row: ActivityMirrorRow) -> None: ...

    def upsert_kpi(self, row: KpiMirrorRow) -> None: ...

    def upsert_content(self, row: ContentMirrorRow) -> None: ...

    def upsert_budget(self, row: BudgetMirrorRow) -> None: ...

    def upsert_performance(self, row: PerformanceMirrorRow) -> None: ...


class DisabledSheetsPort:
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

    def upsert_budget(self, row: BudgetMirrorRow) -> None:
        del row

    def upsert_performance(self, row: PerformanceMirrorRow) -> None:
        del row


class ComposioSheetsPort:
    """Live Composio adapter. HTTP/transport raises AdapterHttpError; 200 unsuccessful skips."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        spreadsheet_id: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._spreadsheet_id = spreadsheet_id.strip()
        self._client = client

    def _execute_tool(
        self, tool_slug: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
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
                response = self._client.post(
                    url, json=payload, headers=request_headers
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        url, json=payload, headers=request_headers
                    )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("successful") is not True:
                return None
            data = body.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    return None
            if isinstance(data, dict):
                return data
            return None
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return None

    def _execute_upsert(
        self,
        *,
        sheet_name: str,
        key_column: str,
        headers: list[str],
        values: list[list[object]],
    ) -> None:
        spreadsheet_id = self._spreadsheet_id
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

    def upsert_lead(self, row: LeadMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=LEADS_SHEET_NAME,
            key_column=LEADS_KEY_COLUMN,
            headers=LEADS_HEADERS,
            values=[
                [
                    row.lead_id,
                    row.channel,
                    row.stage,
                    row.fit,
                    row.pain_level,
                    row.next_action,
                    row.timestamp,
                    row.discovery_summary,
                    row.whatsapp_offered,
                    row.disqualified,
                    row.last_message_short,
                ]
            ],
        )

    def upsert_source(self, row: SourceMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=SOURCES_SHEET_NAME,
            key_column=SOURCES_KEY_COLUMN,
            headers=SOURCES_HEADERS,
            values=[
                [
                    row.lead_id,
                    row.utm_source,
                    row.utm_medium,
                    row.utm_campaign,
                    row.utm_content,
                    row.landing_page,
                    row.referrer,
                ]
            ],
        )

    def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=FOLLOWUPS_SHEET_NAME,
            key_column=FOLLOWUPS_KEY_COLUMN,
            headers=FOLLOWUPS_HEADERS,
            values=[
                [
                    row.lead_id,
                    row.due_at,
                    row.channel,
                    row.status,
                    row.result,
                ]
            ],
        )

    def upsert_deal(self, row: DealMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=DEALS_SHEET_NAME,
            key_column=DEALS_KEY_COLUMN,
            headers=DEALS_HEADERS,
            values=[
                [
                    row.lead_id,
                    row.stage,
                    row.source,
                    row.attribution_confidence,
                    row.expected_value,
                    row.closed_value,
                ]
            ],
        )

    def upsert_meeting(self, row: MeetingMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=MEETINGS_SHEET_NAME,
            key_column=MEETINGS_KEY_COLUMN,
            headers=MEETINGS_HEADERS,
            values=[
                [
                    row.lead_id,
                    row.status,
                    row.source,
                    row.scheduled_at,
                    row.calendar_event_id,
                    row.summary,
                ]
            ],
        )

    def upsert_activity(self, row: ActivityMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=ACTIVITY_SHEET_NAME,
            key_column=ACTIVITY_KEY_COLUMN,
            headers=ACTIVITY_HEADERS,
            values=[
                [
                    row.run_id,
                    row.occurred_on,
                    row.channel,
                    row.next_action,
                    row.model,
                    str(row.kill_switch).lower(),
                    row.cost_usd,
                    row.lead_id or "",
                ]
            ],
        )

    def upsert_kpi(self, row: KpiMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=KPI_SHEET_NAME,
            key_column=KPI_KEY_COLUMN,
            headers=KPI_HEADERS,
            values=[
                [
                    row.week_start,
                    row.leads,
                    row.meetings_offered,
                    row.handoffs,
                    row.messages_in,
                    row.follow_ups_pending,
                ]
            ],
        )

    def upsert_content(self, row: ContentMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=CONTENT_SHEET_NAME,
            key_column=CONTENT_KEY_COLUMN,
            headers=CONTENT_HEADERS,
            values=[
                [
                    row.media_id,
                    row.media_type,
                    row.views,
                    row.reach,
                    row.likes,
                    row.comments,
                    row.saved,
                    row.lead_signals,
                ]
            ],
        )

    def upsert_budget(self, row: BudgetMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=BUDGET_SHEET_NAME,
            key_column=BUDGET_KEY_COLUMN,
            headers=BUDGET_HEADERS,
            values=[
                [
                    row.campaign,
                    row.monthly_budget,
                    row.spend,
                    row.expected_spend,
                    row.remaining,
                    row.projected,
                    row.over_under,
                    row.status,
                ]
            ],
        )

    def upsert_performance(self, row: PerformanceMirrorRow) -> None:
        self._execute_upsert(
            sheet_name=PERF_SHEET_NAME,
            key_column=PERF_KEY_COLUMN,
            headers=PERF_HEADERS,
            values=[
                [
                    row.campaign,
                    row.spend,
                    row.ctr,
                    row.cpc,
                    row.cpl,
                    row.qualified_cpl,
                    row.meetings,
                    row.deals,
                    row.revenue,
                    row.roas,
                ]
            ],
        )


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
        self._budget_rows: dict[str, BudgetMirrorRow] = {}
        self._performance_rows: dict[str, PerformanceMirrorRow] = {}

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

    @property
    def budget_rows(self) -> dict[str, BudgetMirrorRow]:
        return dict(self._budget_rows)

    @property
    def performance_rows(self) -> dict[str, PerformanceMirrorRow]:
        return dict(self._performance_rows)

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

    def upsert_budget(self, row: BudgetMirrorRow) -> None:
        self._budget_rows[row.campaign] = row

    def upsert_performance(self, row: PerformanceMirrorRow) -> None:
        self._performance_rows[row.campaign] = row


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


def lead_mirror_row_from_state(
    *,
    lead_id: str,
    channel: str,
    stage: str,
    sales: SalesState,
    next_action: str,
    turns: list | None = None,
    now: datetime | None = None,
) -> LeadMirrorRow:
    """Build the 01 Leads snapshot. No phone/email columns. Never invents facts."""
    from app.domain.owner_lead_card import (
        discovery_summary,
        hebrew_yes_no,
        is_disqualified,
        last_message_short,
    )

    instant = now if now is not None else datetime.now(UTC)
    timestamp = instant.replace(microsecond=0).isoformat()
    if timestamp.endswith("+00:00"):
        timestamp = timestamp[:-6] + "Z"
    last_said = last_message_short(list(turns or []))
    offered = bool(sales.whatsapp_handoff_offered) or (
        (next_action or "").strip() == NextAction.OFFER_WHATSAPP.value
    )
    return LeadMirrorRow(
        lead_id=lead_id,
        channel=channel,
        stage=stage,
        fit=sales.fit.value,
        pain_level=int(sales.pain_level),
        next_action=next_action or "",
        timestamp=timestamp,
        discovery_summary=discovery_summary(sales),
        whatsapp_offered=hebrew_yes_no(offered),
        disqualified=hebrew_yes_no(is_disqualified(sales, next_action)),
        last_message_short=last_said,
    )


def maybe_mirror_lead_snapshot(
    *,
    sheets: SheetsPort,
    store: LeadStore,
    lead_id: str,
    channel: str,
    next_action: str,
    conversation_id: str = "",
    kill_switch: bool,
) -> bool:
    """Best-effort 01 Leads upsert. Fail closed: never raises into chat."""
    if not lead_id or kill_switch:
        return False
    try:
        sales = store.get_sales(lead_id)
    except KeyError:
        return False
    stage = "open"
    try:
        stage = store.get_lead_stage(lead_id) or "open"
    except KeyError:
        stage = "open"
    turns: list = []
    if conversation_id:
        list_turns = getattr(store, "list_conversation_turns", None)
        if callable(list_turns):
            turns = list_turns(conversation_id)
    return mirror_lead(
        sheets=sheets,
        row=lead_mirror_row_from_state(
            lead_id=lead_id,
            channel=channel,
            stage=stage,
            sales=sales,
            next_action=next_action,
            turns=turns,
        ),
        kill_switch=kill_switch,
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
    cleaned = sanitize_attribution({
        "utm_source": row.utm_source or None,
        "utm_medium": row.utm_medium or None,
        "utm_campaign": row.utm_campaign or None,
        "utm_content": row.utm_content or None,
        "landing_page": row.landing_page or None,
        "referrer": row.referrer or None,
    })
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


def mirror_activity(
    *, sheets: SheetsPort, row: ActivityMirrorRow, kill_switch: bool
) -> bool:
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


def mirror_follow_up(
    *, sheets: SheetsPort, row: FollowUpMirrorRow, kill_switch: bool
) -> bool:
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
        return row.model_copy(
            update={"scheduled_at": normalized, "calendar_event_id": event_id}
        )
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


def _valid_campaign_key(campaign: str) -> bool:
    return campaign == "account" or _CAMPAIGN_KEY_PATTERN.fullmatch(campaign) is not None


def _valid_numeric_field(value: str) -> bool:
    if value == "":
        return True
    return _NUMERIC_MIRROR_PATTERN.fullmatch(value) is not None


def _sanitized_budget_row(row: BudgetMirrorRow) -> BudgetMirrorRow | None:
    if not _valid_campaign_key(row.campaign):
        return None
    if row.status not in _PACING_STATUSES:
        return None
    for field in (
        "monthly_budget",
        "spend",
        "expected_spend",
        "remaining",
        "projected",
        "over_under",
    ):
        if not _valid_numeric_field(getattr(row, field)):
            return None
    if not _valid_numeric_field(row.monthly_budget) or row.monthly_budget == "":
        return None
    return row


def mirror_budget(*, sheets: SheetsPort, row: BudgetMirrorRow, kill_switch: bool) -> bool:
    clean = _sanitized_budget_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_budget(clean)
        return True
    except (AdapterHttpError, PolicyDenied, RuntimeError):
        return False


def _sanitized_performance_row(row: PerformanceMirrorRow) -> PerformanceMirrorRow | None:
    if not _valid_campaign_key(row.campaign):
        return None
    if row.revenue != "" or row.roas != "" or row.qualified_cpl != "":
        return None
    for field in ("spend", "cpc", "cpl", "meetings", "deals"):
        if not _valid_numeric_field(getattr(row, field)):
            return None
    if row.ctr != "" and re.fullmatch(r"\d+(\.\d+)?%?", row.ctr) is None:
        return None
    return row


def mirror_performance(
    *, sheets: SheetsPort, row: PerformanceMirrorRow, kill_switch: bool
) -> bool:
    clean = _sanitized_performance_row(row)
    if clean is None:
        return False
    try:
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
        sheets.upsert_performance(clean)
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


def pick_spreadsheet_id(
    files: list[tuple[str, str]], *, preferred: str = ""
) -> str:
    explicit = preferred.strip()
    if explicit:
        return explicit
    mia_named = [
        (file_id, name)
        for file_id, name in files
        if PREFERRED_SHEET_NAME in name.lower()
    ]
    exact = [
        file_id
        for file_id, name in mia_named
        if name.strip().lower() == PREFERRED_SHEET_NAME
    ]
    if len(exact) == 1:
        return exact[0]
    if len(mia_named) == 1:
        return mia_named[0][0]
    return ""


def _map_spreadsheet_files(
    data: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    if data is None:
        return []
    entries = data.get("files") or data.get("spreadsheets") or data.get("items")
    if not isinstance(entries, list):
        return []
    mapped: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id") or entry.get("spreadsheetId")
        raw_name = entry.get("name") or entry.get("title") or ""
        file_id = raw_id.strip() if isinstance(raw_id, str) else ""
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if file_id:
            mapped.append((file_id, name))
    return mapped


def build_sheets_port(settings: Settings) -> SheetsPort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioSheetsPort(
            api_key=api_key,
            user_id=user_id,
            spreadsheet_id=settings.sheets_spreadsheet_id.strip(),
        )
    return DisabledSheetsPort()
