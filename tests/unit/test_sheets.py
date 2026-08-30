import json
import re
from types import SimpleNamespace

import app.integrations.sheets as sheets_module
import httpx
import pytest
from app.api.deps import get_sheets_port
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.ai_runs import MODEL_CANNED
from app.domain.events import Channel
from app.domain.followups import REASON_MEETING_OFFERED, STATUS_PENDING
from app.domain.meetings import STATUS_OFFERED
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.sheets import (
    ACTIVITY_HEADERS,
    ACTIVITY_KEY_COLUMN,
    ACTIVITY_SHEET_NAME,
    COMPOSIO_GOOGLESHEETS_VERSION,
    COMPOSIO_UPSERT_ROWS_TOOL,
    FOLLOWUPS_HEADERS,
    FOLLOWUPS_KEY_COLUMN,
    FOLLOWUPS_SHEET_NAME,
    KPI_HEADERS,
    KPI_KEY_COLUMN,
    KPI_SHEET_NAME,
    LEADS_HEADERS,
    LEADS_KEY_COLUMN,
    LEADS_SHEET_NAME,
    MEETINGS_HEADERS,
    MEETINGS_KEY_COLUMN,
    MEETINGS_SHEET_NAME,
    SHEETS_MIRROR_SCOPE,
    SOURCES_HEADERS,
    SOURCES_KEY_COLUMN,
    SOURCES_SHEET_NAME,
    ActivityMirrorRow,
    ComposioSheetsPort,
    ContentMirrorRow,
    DealMirrorRow,
    DisabledSheetsPort,
    FakeSheetsPort,
    FollowUpMirrorRow,
    KpiMirrorRow,
    LeadMirrorRow,
    MeetingMirrorRow,
    SourceMirrorRow,
    activity_mirror_row_from_persisted,
    activity_occurred_on,
    build_sheets_port,
    claim_sheets_mirror,
    complete_sheets_mirror,
    mirror_activity,
    mirror_follow_up,
    mirror_lead,
    mirror_meeting,
    mirror_sales_turn,
    mirror_source,
    sheets_mirror_claim_key,
)
from app.main import app
from fastapi.testclient import TestClient

SHEET_LEAD_EMAIL = "sheet.lead.1@example.com"
SHEET_LEAD_2_EMAIL = "sheet.lead.2@example.com"
WEB_SESSION = "web_sheet_mirror_1"
WEB_CLINIC_SESSION = "web_sheet_fu_clinic_1"
WEB_ACTIVITY_SESSION = "web_sheet_activity_1"
WEB_ACTIVITY_SESSION_2 = "web_sheet_activity_2"
WHATSAPP_ACTIVITY_PHONE = "972509994902"
SHEET_CLAIM_RETRY_EMAIL = "sheet.claim.retry@example.com"
SHEET_CLAIM_RETRY_EVENT = "evt.sheet.claim.retry.1"


class CountingSheetsPort(FakeSheetsPort):
    def __init__(self) -> None:
        super().__init__()
        self.lead_calls = 0

    def upsert_lead(self, row: LeadMirrorRow) -> None:
        self.lead_calls += 1
        super().upsert_lead(row)


class SalesTurnStore:
    def __init__(self, events: list[str], *, claimed: bool = True) -> None:
        self.events = events
        self.claimed = claimed

    def claim_operation(self, *, scope: str, key: str) -> bool:
        assert scope == SHEETS_MIRROR_SCOPE
        assert key == "evt.sales.turn.1:sheets:sales"
        self.events.append("claim")
        return self.claimed

    def complete_operation(self, *, scope: str, key: str, result_json: str) -> None:
        assert scope == SHEETS_MIRROR_SCOPE
        assert key == "evt.sales.turn.1:sheets:sales"
        assert json.loads(result_json) == {"ok": True}
        self.events.append("complete")

    def get_sales(self, lead_id: str) -> SimpleNamespace:
        assert lead_id == "lead_sales_turn_1"
        return SimpleNamespace(fit=FitLevel.GOOD, pain_level=PainLevel.P3)

    def get_lead_stage(self, lead_id: str) -> str:
        assert lead_id == "lead_sales_turn_1"
        return "open"

    def get_follow_up(self, lead_id: str) -> SimpleNamespace:
        assert lead_id == "lead_sales_turn_1"
        return SimpleNamespace(
            due_at="2026-08-29",
            channel=Channel.WEBSITE.value,
            status=STATUS_PENDING,
            reason=REASON_MEETING_OFFERED,
        )

    def get_deal(self, lead_id: str) -> SimpleNamespace:
        assert lead_id == "lead_sales_turn_1"
        return SimpleNamespace(
            stage="meeting_offered",
            source=Channel.WEBSITE.value,
            attribution_confidence="unknown",
            expected_value="",
            closed_value="",
        )

    def get_meeting(self, lead_id: str) -> SimpleNamespace:
        assert lead_id == "lead_sales_turn_1"
        return SimpleNamespace(
            status=STATUS_OFFERED,
            source=Channel.WEBSITE.value,
            scheduled_at="",
            calendar_event_id="",
            summary="",
        )

    def get_ai_run(self, run_id: str) -> SimpleNamespace:
        assert run_id == "run_sales_turn_1"
        return SimpleNamespace(
            run_id=run_id,
            lead_id="lead_sales_turn_1",
            channel=Channel.WEBSITE.value,
            next_action=NextAction.OFFER_MEETING.value,
            model=MODEL_CANNED,
            kill_switch=False,
            cost_usd=0,
        )


class OrderedSalesSheetsPort(FakeSheetsPort):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def upsert_lead(self, row: LeadMirrorRow) -> None:
        self.events.append("lead")
        super().upsert_lead(row)

    def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
        self.events.append("follow_up")
        super().upsert_follow_up(row)

    def upsert_deal(self, row: DealMirrorRow) -> None:
        self.events.append("deal")
        super().upsert_deal(row)

    def upsert_meeting(self, row: MeetingMirrorRow) -> None:
        self.events.append("meeting")
        super().upsert_meeting(row)

    def upsert_activity(self, row: ActivityMirrorRow) -> None:
        self.events.append("activity")
        super().upsert_activity(row)

    def upsert_kpi(self, row: KpiMirrorRow) -> None:
        self.events.append("kpi")
        super().upsert_kpi(row)


def _sample_kpi_row(*, week_start: str = "2026-08-17") -> KpiMirrorRow:
    return KpiMirrorRow(
        week_start=week_start,
        leads=2,
        meetings_offered=1,
        handoffs=0,
        messages_in=3,
        follow_ups_pending=1,
    )


def _sample_row(
    *, lead_id: str = "lead_sheet_1", next_action: str = "understand_workflow"
) -> LeadMirrorRow:
    return LeadMirrorRow(
        lead_id=lead_id,
        channel="gmail",
        stage="open",
        fit="unknown",
        pain_level=0,
        next_action=next_action,
    )


def _sample_follow_up_row(
    *, lead_id: str = "lead_fu_1", due_at: str = "2026-08-22"
) -> FollowUpMirrorRow:
    return FollowUpMirrorRow(
        lead_id=lead_id,
        due_at=due_at,
        channel="whatsapp",
        status=STATUS_PENDING,
        result=REASON_MEETING_OFFERED,
    )


def _sample_activity_row(
    *,
    run_id: str = "run_sheet_act_1",
    occurred_on: str = "2026-08-21",
    channel: str = "website",
    next_action: str = "understand_workflow",
    model: str = MODEL_CANNED,
    kill_switch: bool = False,
    cost_usd: int = 0,
    lead_id: str | None = "lead_sheet_1",
) -> ActivityMirrorRow:
    return ActivityMirrorRow(
        run_id=run_id,
        occurred_on=occurred_on,
        channel=channel,
        next_action=next_action,
        model=model,
        kill_switch=kill_switch,
        cost_usd=cost_usd,
        lead_id=lead_id,
    )


def _sample_source_row(
    *,
    lead_id: str = "lead_src_1",
    utm_source: str = "meta",
    utm_campaign: str = "yuma",
) -> SourceMirrorRow:
    return SourceMirrorRow(
        lead_id=lead_id,
        utm_source=utm_source,
        utm_campaign=utm_campaign,
    )


def _sample_meeting_row(*, lead_id: str = "lead_meet_1") -> MeetingMirrorRow:
    return MeetingMirrorRow(
        lead_id=lead_id,
        status=STATUS_OFFERED,
        source="website",
    )


def _due_pattern() -> re.Pattern[str]:
    return re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _run_clinic_funnel_to_meeting(client: TestClient, session_id: str) -> str:
    messages = [
        "We run a clinic and miss calls all day.",
        "ok that's right",
        "I decide this quarter",
        "let's book a meeting",
    ]
    lead_id = ""
    for text in messages:
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": text},
        )
        assert response.status_code == 200
        body = response.json()
        lead_id = body["lead_id"]
    assert body["next_action"] == "offer_meeting"
    return lead_id


def test_sheets_mirror_claim_key_format() -> None:
    assert sheets_mirror_claim_key("evt.1", "sales") == "evt.1:sheets:sales"


def test_claim_sheets_mirror_empty_inbound_id_returns_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert claim_sheets_mirror(store=store, inbound_id="", tab="sales") is False
        assert store.get_operation_result(scope=SHEETS_MIRROR_SCOPE, key=":sheets:sales") == "{}"
    finally:
        db.close()


def test_claim_sheets_mirror_unknown_tab_returns_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert claim_sheets_mirror(store=store, inbound_id="evt.tab.1", tab="unknown") is False
    finally:
        db.close()


def test_claim_sheets_mirror_first_true_complete_second_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        inbound_id = "evt.helper.claim.1"
        assert claim_sheets_mirror(store=store, inbound_id=inbound_id, tab="sales") is True
        complete_sheets_mirror(store=store, inbound_id=inbound_id, tab="sales")
        assert claim_sheets_mirror(store=store, inbound_id=inbound_id, tab="sales") is False
        result = store.get_operation_result(
            scope=SHEETS_MIRROR_SCOPE,
            key=sheets_mirror_claim_key(inbound_id, "sales"),
        )
        assert json.loads(result) == {"ok": True}
    finally:
        db.close()


def test_mirror_sales_turn_preserves_write_persist_complete_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    store = SalesTurnStore(events)
    sheets = OrderedSalesSheetsPort(events)
    persisted: dict[str, object] = {}

    monkeypatch.setattr(
        sheets_module,
        "compute_weekly_kpi",
        lambda store, *, timezone: SimpleNamespace(
            week_start="2026-08-24",
            leads=4,
            meetings_offered=2,
            handoffs=1,
            messages_in=6,
            follow_ups_pending=1,
        ),
    )

    def record_persist(store_arg, **kwargs) -> None:
        assert store_arg is store
        events.append("persist")
        persisted.update(kwargs)

    monkeypatch.setattr(sheets_module, "persist_tool_outcome", record_persist)

    outcome = mirror_sales_turn(
        store=store,
        sheets=sheets,
        settings=Settings(),
        provider="website",
        channel=Channel.WEBSITE,
        inbound_id="evt.sales.turn.1",
        conversation_id="web_sales_turn_1",
        lead_id="lead_sales_turn_1",
        run_id="run_sales_turn_1",
        next_action=NextAction.OFFER_MEETING.value,
        kill_switch=False,
    )

    assert events == [
        "claim",
        "lead",
        "follow_up",
        "deal",
        "meeting",
        "activity",
        "kpi",
        "persist",
        "complete",
    ]
    assert outcome is persisted["outcome"]
    assert outcome is not None
    assert outcome.status == "ok"
    assert outcome.result_count == 6
    assert persisted["provider"] == "website"
    assert persisted["channel"] == Channel.WEBSITE
    assert persisted["inbound_provider_event_id"] == "evt.sales.turn.1"
    assert persisted["conversation_id"] == "web_sales_turn_1"
    assert persisted["lead_id"] == "lead_sales_turn_1"
    assert persisted["correlation_id"] == "run_sales_turn_1"
    assert set(sheets.rows) == {"lead_sales_turn_1"}
    assert set(sheets.follow_up_rows) == {"lead_sales_turn_1"}
    assert set(sheets.deal_rows) == {"lead_sales_turn_1"}
    assert set(sheets.meeting_rows) == {"lead_sales_turn_1"}
    assert set(sheets.activity_rows) == {"run_sales_turn_1"}
    assert set(sheets.kpi_rows) == {"2026-08-24"}


def test_mirror_sales_turn_claim_collision_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    store = SalesTurnStore(events, claimed=False)
    sheets = OrderedSalesSheetsPort(events)
    monkeypatch.setattr(
        sheets_module,
        "persist_tool_outcome",
        lambda *args, **kwargs: pytest.fail("claim collision must not persist"),
    )
    monkeypatch.setattr(
        sheets_module,
        "compute_weekly_kpi",
        lambda *args, **kwargs: pytest.fail("claim collision must not compute KPI"),
    )

    outcome = mirror_sales_turn(
        store=store,
        sheets=sheets,
        settings=Settings(),
        provider="website",
        channel=Channel.WEBSITE,
        inbound_id="evt.sales.turn.1",
        conversation_id="web_sales_turn_1",
        lead_id="lead_sales_turn_1",
        run_id="run_sales_turn_1",
        next_action=NextAction.OFFER_MEETING.value,
        kill_switch=False,
    )

    assert outcome is None
    assert events == ["claim"]
    assert sheets.rows == {}


def test_mirror_sales_turn_persist_failure_does_not_complete_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    store = SalesTurnStore(events)
    sheets = OrderedSalesSheetsPort(events)
    monkeypatch.setattr(
        sheets_module,
        "compute_weekly_kpi",
        lambda store, *, timezone: None,
    )

    def fail_persist(*args, **kwargs) -> None:
        events.append("persist")
        raise RuntimeError("persist failed")

    monkeypatch.setattr(sheets_module, "persist_tool_outcome", fail_persist)

    with pytest.raises(RuntimeError, match="persist failed"):
        mirror_sales_turn(
            store=store,
            sheets=sheets,
            settings=Settings(),
            provider="website",
            channel=Channel.WEBSITE,
            inbound_id="evt.sales.turn.1",
            conversation_id="web_sales_turn_1",
            lead_id="lead_sales_turn_1",
            run_id="run_sales_turn_1",
            next_action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )

    assert events[-1] == "persist"
    assert "complete" not in events


@pytest.mark.asyncio
async def test_inbound_failed_webhook_retry_skips_second_sheets_upsert() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = CountingSheetsPort()
        port = RecordingMessagePort()
        item = {
            "id": SHEET_CLAIM_RETRY_EVENT,
            "from": SHEET_CLAIM_RETRY_EMAIL,
            "text": "hello",
        }

        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[item],
            store=store,
            port=port,
            kill_switch=False,
            sheets=sheets,
        )
        db.commit()
        assert sheets.lead_calls == 1

        store.mark_webhook(
            provider="gmail",
            provider_event_id=SHEET_CLAIM_RETRY_EVENT,
            status="failed",
        )
        db.commit()

        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[item],
            store=store,
            port=port,
            kill_switch=False,
            sheets=sheets,
        )
        db.commit()
        assert sheets.lead_calls == 1
        result = store.get_operation_result(
            scope=SHEETS_MIRROR_SCOPE,
            key=sheets_mirror_claim_key(SHEET_CLAIM_RETRY_EVENT, "sales"),
        )
        assert json.loads(result) == {"ok": True}
    finally:
        db.close()


def test_fake_upsert_overwrites_same_lead_id() -> None:
    port = FakeSheetsPort()
    port.upsert_lead(_sample_row(lead_id="lead_a", next_action="understand_workflow"))
    port.upsert_lead(_sample_row(lead_id="lead_a", next_action="deepen_pain"))
    assert len(port.rows) == 1
    assert port.rows["lead_a"].next_action == "deepen_pain"


def test_fake_upsert_different_lead_ids_are_separate_rows() -> None:
    port = FakeSheetsPort()
    port.upsert_lead(_sample_row(lead_id="lead_a"))
    port.upsert_lead(_sample_row(lead_id="lead_b"))
    assert len(port.rows) == 2
    assert "lead_a" in port.rows
    assert "lead_b" in port.rows


def test_disabled_sheets_port_is_no_op() -> None:
    port = DisabledSheetsPort()
    port.upsert_lead(_sample_row())
    port.upsert_source(_sample_source_row())
    port.upsert_follow_up(_sample_follow_up_row())
    port.upsert_deal(
        DealMirrorRow(
            lead_id="lead_deal_1",
            stage="meeting_offered",
            source="website",
            attribution_confidence="unknown",
        )
    )
    port.upsert_meeting(_sample_meeting_row())
    port.upsert_activity(_sample_activity_row())
    port.upsert_kpi(_sample_kpi_row())


def test_fake_upsert_follow_up_overwrites_same_lead_id() -> None:
    port = FakeSheetsPort()
    port.upsert_follow_up(_sample_follow_up_row(lead_id="lead_fu_a", due_at="2026-08-22"))
    port.upsert_follow_up(_sample_follow_up_row(lead_id="lead_fu_a", due_at="2026-08-23"))
    assert len(port.follow_up_rows) == 1
    assert port.follow_up_rows["lead_fu_a"].due_at == "2026-08-23"


def test_fake_upsert_activity_overwrites_same_run_id() -> None:
    port = FakeSheetsPort()
    port.upsert_activity(_sample_activity_row(run_id="run_a", next_action="understand_workflow"))
    port.upsert_activity(_sample_activity_row(run_id="run_a", next_action="deepen_pain"))
    assert len(port.activity_rows) == 1
    assert port.activity_rows["run_a"].next_action == "deepen_pain"


def test_fake_upsert_source_overwrites_same_lead_id() -> None:
    port = FakeSheetsPort()
    port.upsert_source(_sample_source_row(lead_id="lead_src_a", utm_source="meta"))
    port.upsert_source(_sample_source_row(lead_id="lead_src_a", utm_source="google"))
    assert len(port.source_rows) == 1
    assert port.source_rows["lead_src_a"].utm_source == "google"


def test_mirror_source_writes_fake_row_without_pii() -> None:
    port = FakeSheetsPort()
    written = mirror_source(
        sheets=port,
        row=_sample_source_row(lead_id="lead_src_mirror_1"),
        kill_switch=False,
    )
    assert written is True
    assert "lead_src_mirror_1" in port.source_rows
    row = port.source_rows["lead_src_mirror_1"]
    assert row.utm_source == "meta"
    assert row.utm_campaign == "yuma"
    serialized = json.dumps({
        "lead_id": row.lead_id,
        "utm_source": row.utm_source,
        "utm_medium": row.utm_medium,
        "utm_campaign": row.utm_campaign,
        "utm_content": row.utm_content,
        "landing_page": row.landing_page,
        "referrer": row.referrer,
    })
    for forbidden in ("@", "email", "phone", "token"):
        assert forbidden not in serialized.lower()


def test_mirror_source_kill_switch_skips_port() -> None:
    class ExplodingSheetsPort:
        def upsert_lead(self, row: LeadMirrorRow) -> None:
            del row

        def upsert_source(self, row: SourceMirrorRow) -> None:
            raise RuntimeError("source mirror must not run when kill switch is on")

        def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
            del row

        def upsert_activity(self, row: ActivityMirrorRow) -> None:
            del row

        def upsert_deal(self, row: DealMirrorRow) -> None:
            del row

        def upsert_meeting(self, row: MeetingMirrorRow) -> None:
            del row

        def upsert_kpi(self, row: KpiMirrorRow) -> None:
            del row

        def upsert_content(self, row: ContentMirrorRow) -> None:
            del row

        def upsert_budget(self, row: object) -> None:
            del row

        def upsert_performance(self, row: object) -> None:
            del row

    written = mirror_source(
        sheets=ExplodingSheetsPort(),
        row=_sample_source_row(),
        kill_switch=True,
    )
    assert written is False


def test_mirror_source_rejects_dirty_payload() -> None:
    port = FakeSheetsPort()
    written = mirror_source(
        sheets=port,
        row=SourceMirrorRow(
            lead_id="lead_src_dirty",
            utm_source="spam@evil.com",
        ),
        kill_switch=False,
    )
    assert written is False
    assert port.source_rows == {}


def test_mirror_meeting_writes_fake_sheets_port() -> None:
    port = FakeSheetsPort()
    written = mirror_meeting(
        sheets=port,
        row=_sample_meeting_row(lead_id="lead_meet_mirror_1"),
        kill_switch=False,
    )
    assert written is True
    row = port.meeting_rows["lead_meet_mirror_1"]
    assert row.status == STATUS_OFFERED
    assert row.source == "website"
    assert row.scheduled_at == ""
    assert row.calendar_event_id == ""
    assert row.summary == ""


def test_mirror_meeting_kill_switch_skips_port() -> None:
    class ExplodingSheetsPort:
        def upsert_lead(self, row: LeadMirrorRow) -> None:
            del row

        def upsert_source(self, row: SourceMirrorRow) -> None:
            del row

        def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
            del row

        def upsert_deal(self, row: DealMirrorRow) -> None:
            del row

        def upsert_meeting(self, row: MeetingMirrorRow) -> None:
            raise RuntimeError("meeting mirror must not run when kill switch is on")

        def upsert_activity(self, row: ActivityMirrorRow) -> None:
            del row

        def upsert_kpi(self, row: KpiMirrorRow) -> None:
            del row

        def upsert_content(self, row: ContentMirrorRow) -> None:
            del row

        def upsert_budget(self, row: object) -> None:
            del row

        def upsert_performance(self, row: object) -> None:
            del row

    written = mirror_meeting(
        sheets=ExplodingSheetsPort(),
        row=_sample_meeting_row(),
        kill_switch=True,
    )
    assert written is False


def test_mirror_meeting_rejects_non_empty_time() -> None:
    port = FakeSheetsPort()
    written = mirror_meeting(
        sheets=port,
        row=MeetingMirrorRow(
            lead_id="lead_meet_dirty",
            status=STATUS_OFFERED,
            source="website",
            scheduled_at="2026-08-21T10:00:00",
        ),
        kill_switch=False,
    )
    assert written is False
    assert port.meeting_rows == {}


def test_mirror_meeting_rejects_non_empty_calendar_event_id() -> None:
    port = FakeSheetsPort()
    written = mirror_meeting(
        sheets=port,
        row=MeetingMirrorRow(
            lead_id="lead_meet_cal",
            status=STATUS_OFFERED,
            source="website",
            calendar_event_id="evt_123",
        ),
        kill_switch=False,
    )
    assert written is False
    assert port.meeting_rows == {}


def test_mirror_meeting_rejects_non_empty_summary() -> None:
    port = FakeSheetsPort()
    written = mirror_meeting(
        sheets=port,
        row=MeetingMirrorRow(
            lead_id="lead_meet_sum",
            status=STATUS_OFFERED,
            source="website",
            summary="secret notes",
        ),
        kill_switch=False,
    )
    assert written is False
    assert port.meeting_rows == {}


def test_mirror_meeting_rejects_unknown_status() -> None:
    port = FakeSheetsPort()
    written = mirror_meeting(
        sheets=port,
        row=MeetingMirrorRow(
            lead_id="lead_meet_bad",
            status="confirmed",
            source="website",
        ),
        kill_switch=False,
    )
    assert written is False
    assert port.meeting_rows == {}


def test_mirror_source_dirty_field_dropped_when_other_valid() -> None:
    port = FakeSheetsPort()
    written = mirror_source(
        sheets=port,
        row=SourceMirrorRow(
            lead_id="lead_src_partial",
            utm_source="spam@evil.com",
            utm_campaign="yuma",
        ),
        kill_switch=False,
    )
    assert written is True
    row = port.source_rows["lead_src_partial"]
    assert row.utm_source == ""
    assert row.utm_campaign == "yuma"


def test_mirror_activity_writes_fake_row_without_pii() -> None:
    port = FakeSheetsPort()
    written = mirror_activity(
        sheets=port,
        row=_sample_activity_row(run_id="run_mirror_act_1"),
        kill_switch=False,
    )
    assert written is True
    assert "run_mirror_act_1" in port.activity_rows
    row = port.activity_rows["run_mirror_act_1"]
    assert row.channel == "website"
    assert row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
    assert row.model == MODEL_CANNED
    assert row.cost_usd == 0
    assert _due_pattern().match(row.occurred_on)
    serialized = json.dumps(
        {
            "run_id": row.run_id,
            "occurred_on": row.occurred_on,
            "channel": row.channel,
            "next_action": row.next_action,
            "model": row.model,
            "kill_switch": row.kill_switch,
            "cost_usd": row.cost_usd,
            "lead_id": row.lead_id,
        }
    )
    for forbidden in ("@", "email", "phone", "visitor"):
        assert forbidden not in serialized.lower()


def test_mirror_activity_kill_switch_skips_port() -> None:
    class ExplodingSheetsPort:
        def upsert_lead(self, row: LeadMirrorRow) -> None:
            del row

        def upsert_source(self, row: SourceMirrorRow) -> None:
            del row

        def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
            del row

        def upsert_activity(self, row: ActivityMirrorRow) -> None:
            raise RuntimeError("activity mirror must not run when kill switch is on")

        def upsert_deal(self, row: DealMirrorRow) -> None:
            del row

        def upsert_meeting(self, row: MeetingMirrorRow) -> None:
            del row

        def upsert_kpi(self, row: KpiMirrorRow) -> None:
            del row

        def upsert_content(self, row: ContentMirrorRow) -> None:
            del row

        def upsert_budget(self, row: object) -> None:
            del row

        def upsert_performance(self, row: object) -> None:
            del row

    written = mirror_activity(
        sheets=ExplodingSheetsPort(),
        row=_sample_activity_row(),
        kill_switch=True,
    )
    assert written is False


def test_mirror_activity_rejects_dirty_payload() -> None:
    port = FakeSheetsPort()
    written = mirror_activity(
        sheets=port,
        row=ActivityMirrorRow(
            run_id="run_dirty",
            occurred_on="tomorrow",
            channel="website",
            next_action="not_a_real_action",
            model="canned",
            kill_switch=False,
            cost_usd=0,
            lead_id="lead_dirty",
        ),
        kill_switch=False,
    )
    assert written is False
    assert port.activity_rows == {}


def test_activity_occurred_on_invalid_timezone_returns_none() -> None:
    assert activity_occurred_on("Not/A_Zone") is None


def test_activity_mirror_row_from_persisted_skips_invalid_timezone() -> None:
    assert (
        activity_mirror_row_from_persisted(
            run_id="run_tz_1",
            lead_id="lead_tz_1",
            channel="website",
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            model=MODEL_CANNED,
            kill_switch=False,
            cost_usd=0,
            timezone="Not/A_Zone",
        )
        is None
    )


def test_mirror_follow_up_kill_switch_skips_port() -> None:
    class ExplodingSheetsPort:
        def upsert_lead(self, row: LeadMirrorRow) -> None:
            del row

        def upsert_source(self, row: SourceMirrorRow) -> None:
            del row

        def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
            raise RuntimeError("follow-up mirror must not run when kill switch is on")

        def upsert_activity(self, row: ActivityMirrorRow) -> None:
            del row

        def upsert_deal(self, row: DealMirrorRow) -> None:
            del row

        def upsert_meeting(self, row: MeetingMirrorRow) -> None:
            del row

        def upsert_kpi(self, row: KpiMirrorRow) -> None:
            del row

        def upsert_content(self, row: ContentMirrorRow) -> None:
            del row

        def upsert_budget(self, row: object) -> None:
            del row

        def upsert_performance(self, row: object) -> None:
            del row

    written = mirror_follow_up(
        sheets=ExplodingSheetsPort(),
        row=_sample_follow_up_row(),
        kill_switch=True,
    )
    assert written is False


def test_mirror_follow_up_rejects_dirty_payload() -> None:
    port = FakeSheetsPort()
    written = mirror_follow_up(
        sheets=port,
        row=FollowUpMirrorRow(
            lead_id="lead_fu_dirty",
            due_at="tomorrow",
            channel="whatsapp",
            status=STATUS_PENDING,
            result="secret@example.com",
        ),
        kill_switch=False,
    )
    assert written is False
    assert port.follow_up_rows == {}


@pytest.mark.asyncio
async def test_inbound_prospect_path_mirrors_lead_after_graph() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()

        result = await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[{"id": "evt.sheet.1", "from": SHEET_LEAD_EMAIL, "text": "hello"}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=sheets,
        )
        db.commit()

        assert result["processed"] == 1
        assert len(sheets.rows) == 1
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id=SHEET_LEAD_EMAIL)
        row = sheets.rows[lead_id]
        assert row.channel == "gmail"
        assert row.stage == "open"
        assert row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_second_message_updates_same_mirror_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()

        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[{"id": "evt.sheet.2a", "from": SHEET_LEAD_2_EMAIL, "text": "hello"}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=sheets,
        )
        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[
                {
                    "id": "evt.sheet.2b",
                    "from": SHEET_LEAD_2_EMAIL,
                    "text": "we use whatsapp for customers",
                },
            ],
            store=store,
            port=port,
            kill_switch=False,
            sheets=sheets,
        )
        db.commit()

        assert len(sheets.rows) == 1
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id=SHEET_LEAD_2_EMAIL)
        row = sheets.rows[lead_id]
        assert row.lead_id == lead_id
        assert row.next_action == NextAction.DEEPEN_PAIN.value
    finally:
        db.close()


def test_mirror_lead_kill_switch_skips_port() -> None:
    class ExplodingSheetsPort:
        def upsert_lead(self, row: LeadMirrorRow) -> None:
            raise RuntimeError("sheets must not be called when kill switch is on")

        def upsert_source(self, row: SourceMirrorRow) -> None:
            del row

        def upsert_follow_up(self, row: FollowUpMirrorRow) -> None:
            del row

        def upsert_activity(self, row: ActivityMirrorRow) -> None:
            del row

        def upsert_deal(self, row: DealMirrorRow) -> None:
            del row

        def upsert_meeting(self, row: MeetingMirrorRow) -> None:
            del row

        def upsert_kpi(self, row: KpiMirrorRow) -> None:
            del row

        def upsert_content(self, row: ContentMirrorRow) -> None:
            del row

        def upsert_budget(self, row: object) -> None:
            del row

        def upsert_performance(self, row: object) -> None:
            del row

    written = mirror_lead(
        sheets=ExplodingSheetsPort(),
        row=_sample_row(),
        kill_switch=True,
    )
    assert written is False


def test_fake_upsert_kpi_overwrites_same_week_start() -> None:
    port = FakeSheetsPort()
    port.upsert_kpi(_sample_kpi_row(week_start="2026-08-17"))
    updated = _sample_kpi_row(week_start="2026-08-17")
    updated.leads = 5
    port.upsert_kpi(updated)
    assert len(port.kpi_rows) == 1
    assert port.kpi_rows["2026-08-17"].leads == 5


def test_website_session_create_mirrors_kpi_row() -> None:
    init_db()
    fake = FakeSheetsPort()
    app.dependency_overrides[get_sheets_port] = lambda: fake
    try:
        with TestClient(app) as client:
            response = client.post("/v1/website/sessions")
            assert response.status_code == 200
            assert len(fake.kpi_rows) == 1
            row = next(iter(fake.kpi_rows.values()))
            assert row.leads >= 1
            assert "@" not in json.dumps(row.model_dump())
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)


def test_website_session_create_mirrors_source_row() -> None:
    init_db()
    fake = FakeSheetsPort()
    app.dependency_overrides[get_sheets_port] = lambda: fake
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/website/sessions",
                params={"utm_source": "meta", "utm_campaign": "yuma"},
            )
            assert response.status_code == 200
            body = response.json()
            lead_id = body["lead_id"]
            assert lead_id in fake.source_rows
            row = fake.source_rows[lead_id]
            assert row.utm_source == "meta"
            assert row.utm_campaign == "yuma"
            assert fake.rows == {}
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)


def test_website_session_create_without_utms_skips_source_row() -> None:
    init_db()
    fake = FakeSheetsPort()
    app.dependency_overrides[get_sheets_port] = lambda: fake
    try:
        with TestClient(app) as client:
            response = client.post("/v1/website/sessions")
            assert response.status_code == 200
            assert fake.source_rows == {}
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)


def test_website_post_message_records_fake_mirror_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = WEB_SESSION
        store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()

        fake = FakeSheetsPort()
        app.dependency_overrides[get_sheets_port] = lambda: fake
        try:
            with TestClient(app) as client:
                response = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "tell me about automation"},
                )
                assert response.status_code == 200
                body = response.json()
                assert body["next_action"] == NextAction.UNDERSTAND_WORKFLOW.value
                assert len(fake.rows) == 1
                row = fake.rows[body["lead_id"]]
                assert row.channel == "website"
                assert row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
                assert len(fake.activity_rows) == 1
                run_id = next(iter(fake.activity_rows))
                activity = fake.activity_rows[run_id]
                assert activity.run_id == run_id
                assert activity.channel == "website"
                assert activity.next_action == NextAction.UNDERSTAND_WORKFLOW.value
                assert activity.model == MODEL_CANNED
                assert activity.cost_usd == 0
                assert activity.kill_switch is False
                assert activity.lead_id == body["lead_id"]
                assert _due_pattern().match(activity.occurred_on)
        finally:
            app.dependency_overrides.pop(get_sheets_port, None)
    finally:
        db.close()


def test_website_second_message_records_two_activity_rows() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = WEB_ACTIVITY_SESSION_2
        store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()

        fake = FakeSheetsPort()
        app.dependency_overrides[get_sheets_port] = lambda: fake
        try:
            with TestClient(app) as client:
                first = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "tell me about automation"},
                )
                assert first.status_code == 200
                second = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "We run a clinic and miss calls all day."},
                )
                assert second.status_code == 200
                assert len(fake.activity_rows) == 2
                run_ids = set(fake.activity_rows)
                assert len(run_ids) == 2
                for run_id in run_ids:
                    activity = fake.activity_rows[run_id]
                    assert activity.run_id == run_id
                    assert activity.channel == "website"
                    assert _due_pattern().match(activity.occurred_on)
                    assert activity.model == MODEL_CANNED
                    assert activity.cost_usd == 0
        finally:
            app.dependency_overrides.pop(get_sheets_port, None)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_whatsapp_mirrors_activity_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()

        result = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.sheet.activity.1",
                    "from": WHATSAPP_ACTIVITY_PHONE,
                    "text": "hello",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            sheets=sheets,
        )
        db.commit()

        assert result["processed"] == 1
        assert len(sheets.activity_rows) == 1
        run_id = next(iter(sheets.activity_rows))
        activity = sheets.activity_rows[run_id]
        assert activity.run_id == run_id
        assert activity.channel == "whatsapp"
        assert activity.next_action == NextAction.UNDERSTAND_WORKFLOW.value
        assert activity.model == MODEL_CANNED
        assert activity.cost_usd == 0
        assert _due_pattern().match(activity.occurred_on)
    finally:
        db.close()


def test_website_clinic_funnel_mirrors_follow_up_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = WEB_CLINIC_SESSION
        store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()

        fake = FakeSheetsPort()
        app.dependency_overrides[get_sheets_port] = lambda: fake
        try:
            with TestClient(app) as client:
                lead_id = _run_clinic_funnel_to_meeting(client, session_id)
                assert len(fake.rows) == 1
                assert lead_id in fake.follow_up_rows
                fu = fake.follow_up_rows[lead_id]
                assert _due_pattern().match(fu.due_at)
                assert fu.channel == "website"
                assert fu.status == STATUS_PENDING
                assert fu.result == REASON_MEETING_OFFERED
                serialized = json.dumps(
                    {
                        "due_at": fu.due_at,
                        "channel": fu.channel,
                        "status": fu.status,
                        "result": fu.result,
                    }
                )
                for forbidden in ("@", "email", "phone"):
                    assert forbidden not in serialized.lower()
        finally:
            app.dependency_overrides.pop(get_sheets_port, None)
    finally:
        db.close()


def test_website_student_disqualify_no_follow_up_mirror_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_sheet_fu_student_1"
        store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()

        fake = FakeSheetsPort()
        app.dependency_overrides[get_sheets_port] = lambda: fake
        try:
            with TestClient(app) as client:
                response = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "I'm a student with a school project"},
                )
                assert response.status_code == 200
                assert response.json()["next_action"] == NextAction.DISQUALIFY.value
                assert fake.follow_up_rows == {}
        finally:
            app.dependency_overrides.pop(get_sheets_port, None)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_sales_state_loaded_from_postgres_not_fake_sheet() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL,
            external_id="sheet.sor.1@example.com",
        )
        store.save_sales(
            SalesState(
                lead_id=lead_id,
                workflow_known=True,
                pain_level=PainLevel.P3,
                fit=FitLevel.GOOD,
            )
        )
        db.commit()

        fake = FakeSheetsPort()
        port = RecordingMessagePort()

        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[{"id": "evt.sor.1", "from": "sheet.sor.1@example.com", "text": "ok"}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=fake,
        )
        db.commit()

        sales = store.get_sales(lead_id)
        assert sales.fit == FitLevel.GOOD
        assert sales.pain_level == PainLevel.P3
        assert fake.rows[lead_id].fit == "good"
        assert fake.rows[lead_id].pain_level == 3
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_audio_does_not_mirror_sheets() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.audio.sheet.1",
                    "from": "972509990001",
                    "text": "pause the ads and update the lead",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990001"},
            sheets=sheets,
        )
        db.commit()
        assert sheets.rows == {}
        assert port.sent[0].text
    finally:
        db.close()


def test_mirror_lead_r1_assert_before_write() -> None:
    with pytest.raises(PolicyDenied):
        assert_allowed(
            RiskAction(name="sheets_mirror", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=True,
        )


def test_build_sheets_port_live_when_all_three_credentials_set() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        sheets_spreadsheet_id="sheet-abc",
    )
    port = build_sheets_port(settings)
    assert isinstance(port, ComposioSheetsPort)
    assert not isinstance(port, DisabledSheetsPort)


def test_build_sheets_port_live_without_spreadsheet_id() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
        sheets_spreadsheet_id="",
    )
    assert isinstance(build_sheets_port(settings), ComposioSheetsPort)


@pytest.mark.parametrize(
    "api_key,user_id,spreadsheet_id",
    [
        ("", "", ""),
        ("cmp-live", "", ""),
        ("", "user-123", ""),
        ("   ", "user-123", "sheet-abc"),
        ("cmp-live", "   ", "sheet-abc"),
    ],
)
def test_build_sheets_port_disabled_when_composio_credentials_missing(
    api_key: str,
    user_id: str,
    spreadsheet_id: str,
) -> None:
    settings = Settings(
        composio_api_key=api_key,
        composio_user_id=user_id,
        sheets_spreadsheet_id=spreadsheet_id,
    )
    port = build_sheets_port(settings)
    assert isinstance(port, DisabledSheetsPort)


def test_composio_sheets_port_http_500_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-123",
        spreadsheet_id="sheet-abc",
        client=client,
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.upsert_lead(_sample_row())
    assert exc_info.value.status_code == 500


class _RaisingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.HTTPError("network error")


def test_composio_sheets_port_network_error_raises_adapter_error() -> None:
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-123",
        spreadsheet_id="sheet-abc",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.upsert_lead(_sample_row())
    assert exc_info.value.status_code is None


def test_mirror_lead_http_500_returns_false() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    sheets = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-123",
        spreadsheet_id="sheet-abc",
        client=client,
    )
    assert mirror_lead(sheets=sheets, row=_sample_row(), kill_switch=False) is False


def test_composio_sheets_port_unsuccessful_response_does_not_raise() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": {}, "error": "tool failed", "successful": False},
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-123",
        spreadsheet_id="sheet-abc",
        client=client,
    )
    port.upsert_lead(_sample_row())


def test_composio_sheets_port_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {}, "error": None, "successful": True},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-abc",
        spreadsheet_id="spreadsheet-xyz",
        client=client,
    )
    row = LeadMirrorRow(
        lead_id="lead_mirror_1",
        channel="website",
        stage="open",
        fit="good",
        pain_level=2,
        next_action="deepen_pain",
    )
    port.upsert_lead(row)

    assert str(captured["url"]).endswith(f"/{COMPOSIO_UPSERT_ROWS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["user_id"] == "user-abc"
    assert body["version"] == COMPOSIO_GOOGLESHEETS_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["spreadsheetId"] == "spreadsheet-xyz"
    assert arguments["sheetName"] == LEADS_SHEET_NAME
    assert arguments["keyColumn"] == LEADS_KEY_COLUMN
    assert arguments["headers"] == LEADS_HEADERS
    assert arguments["rows"] == [
        ["lead_mirror_1", "website", "open", "good", 2, "deepen_pain"]
    ]
    assert arguments["strictMode"] is True
    assert "text" not in body
    assert "text" not in arguments
    serialized = json.dumps(body)
    for forbidden in ("VALUES_GET", "CLEAR", "DELETE", "CREATE"):
        assert forbidden not in serialized.upper()


def test_composio_sheets_port_follow_up_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {}, "error": None, "successful": True},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-abc",
        spreadsheet_id="spreadsheet-xyz",
        client=client,
    )
    row = FollowUpMirrorRow(
        lead_id="lead_fu_mirror_1",
        due_at="2026-08-22",
        channel="whatsapp",
        status=STATUS_PENDING,
        result=REASON_MEETING_OFFERED,
    )
    port.upsert_follow_up(row)

    assert str(captured["url"]).endswith(f"/{COMPOSIO_UPSERT_ROWS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["version"] == COMPOSIO_GOOGLESHEETS_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["sheetName"] == FOLLOWUPS_SHEET_NAME
    assert arguments["keyColumn"] == FOLLOWUPS_KEY_COLUMN
    assert arguments["headers"] == FOLLOWUPS_HEADERS
    assert arguments["rows"] == [
        ["lead_fu_mirror_1", "2026-08-22", "whatsapp", STATUS_PENDING, REASON_MEETING_OFFERED]
    ]


def test_composio_sheets_port_activity_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {}, "error": None, "successful": True},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-abc",
        spreadsheet_id="spreadsheet-xyz",
        client=client,
    )
    row = ActivityMirrorRow(
        run_id="run_act_mirror_1",
        occurred_on="2026-08-21",
        channel="website",
        next_action="understand_workflow",
        model=MODEL_CANNED,
        kill_switch=False,
        cost_usd=0,
        lead_id="lead_mirror_1",
    )
    port.upsert_activity(row)

    assert str(captured["url"]).endswith(f"/{COMPOSIO_UPSERT_ROWS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["version"] == COMPOSIO_GOOGLESHEETS_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["sheetName"] == ACTIVITY_SHEET_NAME
    assert arguments["keyColumn"] == ACTIVITY_KEY_COLUMN
    assert arguments["headers"] == ACTIVITY_HEADERS
    assert arguments["rows"] == [
        [
            "run_act_mirror_1",
            "2026-08-21",
            "website",
            "understand_workflow",
            MODEL_CANNED,
            "false",
            0,
            "lead_mirror_1",
        ]
    ]


def test_composio_sheets_port_source_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {}, "error": None, "successful": True},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-abc",
        spreadsheet_id="spreadsheet-xyz",
        client=client,
    )
    row = SourceMirrorRow(
        lead_id="lead_src_mirror_1",
        utm_source="meta",
        utm_medium="cpc",
        utm_campaign="yuma",
        utm_content="",
        landing_page="/pricing",
        referrer="",
    )
    port.upsert_source(row)

    assert str(captured["url"]).endswith(f"/{COMPOSIO_UPSERT_ROWS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["version"] == COMPOSIO_GOOGLESHEETS_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["sheetName"] == SOURCES_SHEET_NAME
    assert arguments["keyColumn"] == SOURCES_KEY_COLUMN
    assert arguments["headers"] == SOURCES_HEADERS
    assert arguments["rows"] == [
        ["lead_src_mirror_1", "meta", "cpc", "yuma", "", "/pricing", ""]
    ]


def test_composio_sheets_port_meeting_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {}, "error": None, "successful": True},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-abc",
        spreadsheet_id="spreadsheet-xyz",
        client=client,
    )
    row = MeetingMirrorRow(
        lead_id="lead_meet_mirror_1",
        status=STATUS_OFFERED,
        source="website",
    )
    port.upsert_meeting(row)

    assert str(captured["url"]).endswith(f"/{COMPOSIO_UPSERT_ROWS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["version"] == COMPOSIO_GOOGLESHEETS_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["sheetName"] == MEETINGS_SHEET_NAME
    assert arguments["keyColumn"] == MEETINGS_KEY_COLUMN
    assert arguments["headers"] == MEETINGS_HEADERS
    assert arguments["rows"] == [
        ["lead_meet_mirror_1", STATUS_OFFERED, "website", "", "", ""]
    ]


def test_composio_sheets_port_kpi_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {}, "error": None, "successful": True},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioSheetsPort(
        api_key="cmp-test",
        user_id="user-abc",
        spreadsheet_id="spreadsheet-xyz",
        client=client,
    )
    row = KpiMirrorRow(
        week_start="2026-08-17",
        leads=3,
        meetings_offered=1,
        handoffs=0,
        messages_in=5,
        follow_ups_pending=2,
    )
    port.upsert_kpi(row)

    assert str(captured["url"]).endswith(f"/{COMPOSIO_UPSERT_ROWS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["version"] == COMPOSIO_GOOGLESHEETS_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["sheetName"] == KPI_SHEET_NAME
    assert arguments["keyColumn"] == KPI_KEY_COLUMN
    assert arguments["headers"] == KPI_HEADERS
    assert arguments["rows"] == [["2026-08-17", 3, 1, 0, 5, 2]]


def test_composio_sheets_port_protocol_has_only_allowlisted_owner_operations() -> None:
    forbidden = frozenset({"clear", "delete", "create", "format", "share", "search"})
    for name in dir(ComposioSheetsPort):
        if name.startswith("_"):
            continue
        words = re.findall(r"[a-z]+", name.lower())
        assert not forbidden.intersection(words)
