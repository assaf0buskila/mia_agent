import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import (
    AiRunRow,
    ApprovalRow,
    CanonicalEventRow,
    ChannelIdentityRow,
    ContentIdeaRow,
    ContentInsightRow,
    ConversationControlRow,
    CustomerRow,
    DealRow,
    FollowUpRow,
    GmailThreadSummaryRow,
    HandoffTokenRow,
    IdempotencyRow,
    IdentityLinkRow,
    LeadReviewRow,
    LeadRow,
    MeetingBriefRow,
    MeetingDebriefRow,
    MeetingRow,
    OwnerBriefRow,
    OwnerCorrectionRow,
    OwnerInstructionRow,
    OwnerNotificationClaimRow,
    OwnerNotificationRecipientClaimRow,
    OwnerNotificationRow,
    OwnerTaskRow,
    OwnerWeeklyRow,
    ReconciliationFindingRow,
    SalesStateRow,
    SeoRecommendationRow,
    ShadowDecisionRow,
    ToolRunRow,
    VoiceTranscriptRow,
    WebhookEventRow,
    WebsiteSessionStateRow,
)
from app.domain.ai_runs import (
    MODEL_CANNED,
    sanitize_automation_mode,
    sanitize_decision_confidence,
    sanitize_prompt_version,
)
from app.domain.approvals import (
    ACTION_CALENDAR_CREATE,
    ACTION_CALENDAR_RESCHEDULE,
    ACTION_GMAIL_SEND,
    ACTION_PROPOSAL_HANDOFF,
    ACTION_WEBSITE_EDIT,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    LEAD_ID_RE,
    RESOURCE_CALENDAR,
    RESOURCE_GMAIL,
    RESOURCE_LEAD,
    RESOURCE_WEBSITE,
    RISK_R3,
    new_approval_id,
    proposed_parameters_json,
)
from app.domain.behavior import ALL_BEHAVIOR_KINDS
from app.domain.commitments import ALLOWLISTED_OWNER_TASK_SCAN_REASONS
from app.domain.company import sanitize_company_domain
from app.domain.content_ideas import ALLOWLISTED_KINDS, ContentIdeaRecord
from app.domain.content_insights import (
    ALLOWLISTED_MEDIA_TYPES,
    ContentInsightRecord,
    is_allowlisted_media_id,
)
from app.domain.deals import (
    ALLOWLISTED_CONFIDENCE,
    ALLOWLISTED_STAGES,
    STAGE_MEETING_OFFERED,
    STAGE_PROPOSAL,
)
from app.domain.debriefs import ALLOWLISTED_NEXT_STEPS, ALLOWLISTED_OUTCOMES
from app.domain.engine_health import AiRunAggregate
from app.domain.events import (
    CanonicalEvent,
    Channel,
    EventType,
    build_lead_created_event,
    sanitize_webhook_channel,
    sanitize_webhook_envelope_kind,
    stamp_payload_version,
)
from app.domain.followups import ALLOWLISTED_SEND_REASONS, STATUS_CANCELLED, STATUS_PENDING
from app.domain.handoff.tokens import generate_handoff_token, hash_handoff_token
from app.domain.idempotency import (
    ALLOWLISTED_OPERATION_SCOPES,
    OPERATION_TTL_SECONDS,
    sanitize_operation_result,
)
from app.domain.identity import ALLOWLISTED_LINK_REASONS
from app.domain.kpis import COUNTABLE_EVENT_TYPES
from app.domain.lead_reviews import (
    ALLOWLISTED_DEAL_STAGE,
    ALLOWLISTED_FIT,
    ALLOWLISTED_FOLLOW_UP_STATUS,
    ALLOWLISTED_MEETING_STATUS,
    ALLOWLISTED_NEXT_ACTION,
)
from app.domain.meeting_slots import (
    normalize_scheduled_at_utc,
    offered_slots_from_json,
    offered_slots_to_json,
    sanitize_event_id,
    sanitize_meet_link,
    validate_offered_slots,
)
from app.domain.meetings import (
    ALLOWLISTED_MEETING_TYPES,
    ALLOWLISTED_STATUSES,
    MEETING_TYPE_INTRO_CALL,
    STATUS_BOOKED,
    STATUS_CANCELLATION_REQUESTED,
    STATUS_OFFERED,
)
from app.domain.memory import (
    MAX_TURNS,
    ConversationTurn,
    clip_turn_text,
    normalize_turn_role,
)
from app.domain.reconciliation import is_stale_received
from app.domain.sales import (
    MAX_ASKED_ACTIONS,
    FitLevel,
    NextAction,
    ObjectionKind,
    PainLevel,
    SalesState,
)
from app.integrations.calendar import TimeSlot
from app.integrations.thread_summary import ALLOWLISTED_INTENTS as GMAIL_SUMMARY_INTENTS
from app.integrations.transcribe import (
    sanitize_confidence,
    sanitize_language,
    sanitize_stt_model,
    sanitize_stt_provider,
)

WEBHOOK_STATUSES = frozenset({"received", "processed", "sent", "failed"})
RECONCILIATION_FINDING_KINDS = frozenset(
    {"webhook_received", "sent_without_out", "handoff_expired"}
)
FREQUENCY_EVENT_TYPES = frozenset({"message_out"})

HANDOFF_TTL_MINUTES = 60

_DEAL_STAGE_RANK = {
    STAGE_MEETING_OFFERED: 0,
    STAGE_PROPOSAL: 1,
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _willingness_from_row(value: str | None) -> bool | None:
    if value is None:
        return None
    return value == "true"


def _objection_from_row(value: str | None) -> ObjectionKind | None:
    if not value:
        return None
    try:
        return ObjectionKind(value)
    except ValueError:
        return None


_VALID_NEXT_ACTIONS = frozenset(action.value for action in NextAction)


def _asked_actions_from_row(raw: str | None) -> list[str]:
    """Allowlisted next-action values only. Unknown strings are dropped, not trusted."""
    try:
        parsed = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and item in _VALID_NEXT_ACTIONS][
        -MAX_ASKED_ACTIONS:
    ]


def sales_from_row(row: SalesStateRow) -> SalesState:
    return SalesState(
        lead_id=row.lead_id,
        pain_level=PainLevel(row.pain_level),
        fit=FitLevel(row.fit),
        workflow_known=row.workflow_known,
        impact_confirmed=row.impact_confirmed,
        reflected=row.reflected,
        hypothesis_offered=row.hypothesis_offered,
        buying_reality_known=row.buying_reality_known,
        authority_known=row.authority_known,
        timeline_known=row.timeline_known,
        metric_known=row.metric_known,
        willingness_to_meet=_willingness_from_row(row.willingness_to_meet),
        owner_required=row.owner_required,
        active_objection=_objection_from_row(row.active_objection),
        missing_fields=json.loads(row.missing_fields or "[]"),
        company_domain=sanitize_company_domain(row.company_domain or "") or "",
        whatsapp_handoff_offered=bool(row.whatsapp_handoff_offered),
        manual_step_known=bool(row.manual_step_known),
        data_source_known=bool(row.data_source_known),
        discovery_turns=int(row.discovery_turns or 0),
        asked_actions=_asked_actions_from_row(row.asked_actions),
        explicit_buying_intent=bool(row.explicit_buying_intent),
        headline=row.headline or "",
        display_name=row.display_name or "",
        meeting_exit_offered=bool(row.meeting_exit_offered),
    )


class LeadStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def open_website_session(self, session_id: str) -> str:
        """Remember a website conversation without minting a lead_ id."""
        row = self.session.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.channel == Channel.WEBSITE.value,
                ChannelIdentityRow.external_id == session_id,
            )
        ).one_or_none()
        if row is not None:
            return row.customer_id
        customer = CustomerRow(id=_new_id("cust"))
        identity = ChannelIdentityRow(
            customer_id=customer.id,
            channel=Channel.WEBSITE.value,
            external_id=session_id,
            verified=False,
        )
        customer.identities.append(identity)
        self.session.add(customer)
        self.session.flush()
        return customer.id

    def website_session_exists(self, session_id: str) -> bool:
        identity = self.session.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.channel == Channel.WEBSITE.value,
                ChannelIdentityRow.external_id == session_id,
            )
        ).one_or_none()
        return identity is not None

    def open_channel_lead(self, *, channel: Channel, external_id: str) -> tuple[str, str]:
        row = self.session.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.channel == channel.value,
                ChannelIdentityRow.external_id == external_id,
            )
        ).one_or_none()
        if row is None:
            customer = CustomerRow(id=_new_id("cust"))
            identity = ChannelIdentityRow(
                customer_id=customer.id,
                channel=channel.value,
                external_id=external_id,
                verified=False,
            )
            customer.identities.append(identity)
            lead = LeadRow(id=_new_id("lead"), customer_id=customer.id, stage="open")
            sales = SalesStateRow(lead_id=lead.id)
            lead.sales_state = sales
            self.session.add_all([customer, lead])
            self.session.flush()
            self._save_lead_created(channel=channel, lead_id=lead.id, conversation_id=external_id)
            return customer.id, lead.id
        lead = self.session.scalars(
            select(LeadRow).where(LeadRow.customer_id == row.customer_id).order_by(LeadRow.id)
        ).first()
        if lead is None:
            lead = LeadRow(id=_new_id("lead"), customer_id=row.customer_id, stage="open")
            lead.sales_state = SalesStateRow(lead_id=lead.id)
            self.session.add(lead)
            self.session.flush()
            self._save_lead_created(channel=channel, lead_id=lead.id, conversation_id=external_id)
        return row.customer_id, lead.id

    def _save_lead_created(self, *, channel: Channel, lead_id: str, conversation_id: str) -> None:
        self.save_canonical_event(
            provider=channel.value,
            event=build_lead_created_event(
                provider=channel.value,
                channel=channel,
                lead_id=lead_id,
                conversation_id=conversation_id,
            ),
        )

    def get_lead_stage(self, lead_id: str) -> str:
        row = self.session.get(LeadRow, lead_id)
        if row is None:
            raise KeyError(lead_id)
        return row.stage

    def get_lead(self, lead_id: str) -> LeadRow | None:
        return self.session.get(LeadRow, lead_id)

    def is_conversation_killed(self, lead_id: str) -> bool:
        row = self.session.get(LeadRow, lead_id)
        if row is None:
            return False
        return row.conversation_killed

    def set_conversation_killed(self, lead_id: str, killed: bool) -> None:
        row = self.session.get(LeadRow, lead_id)
        if row is None:
            return
        row.conversation_killed = killed
        self.session.flush()

    def is_human_takeover(self, lead_id: str) -> bool:
        row = self.session.get(LeadRow, lead_id)
        if row is None:
            return False
        return row.human_takeover

    def set_human_takeover(self, lead_id: str, enabled: bool) -> None:
        from app.domain.conversation_scope import TakeoverState

        row = self.session.get(LeadRow, lead_id)
        if row is None:
            return
        row.human_takeover = enabled
        row.takeover_state = (
            TakeoverState.HUMAN_ACTIVE.value if enabled else TakeoverState.MIA_ACTIVE.value
        )
        self.session.flush()

    def set_takeover_state(self, lead_id: str, state: str) -> None:
        from app.domain.conversation_scope import human_takeover_flag

        row = self.session.get(LeadRow, lead_id)
        if row is None:
            return
        row.takeover_state = state
        row.human_takeover = human_takeover_flag(state)
        self.session.flush()

    def get_takeover_state(self, lead_id: str) -> str:
        from app.domain.conversation_scope import TakeoverState

        row = self.session.get(LeadRow, lead_id)
        if row is None:
            return TakeoverState.MIA_ACTIVE.value
        return row.takeover_state or TakeoverState.MIA_ACTIVE.value

    def cancel_pending_follow_up(self, lead_id: str) -> None:
        row = self.get_follow_up(lead_id)
        if row is None or row.status != STATUS_PENDING:
            return
        row.status = STATUS_CANCELLED
        row.send_ready = False
        row.block_reason = "human_takeover"
        self.session.flush()

    def get_conversation_control(self, channel: str, external_id: str):
        return self.session.scalars(
            select(ConversationControlRow).where(
                ConversationControlRow.channel == channel,
                ConversationControlRow.external_id == external_id,
            )
        ).one_or_none()

    def upsert_conversation_control(
        self,
        *,
        channel: str,
        external_id: str,
        automation_scope: str,
        source: str = "",
        lead_id: str = "",
        mia_introduced: bool | None = None,
    ) -> None:
        row = self.get_conversation_control(channel, external_id)
        if row is None:
            self.session.add(
                ConversationControlRow(
                    channel=channel,
                    external_id=external_id,
                    automation_scope=automation_scope,
                    source=source,
                    lead_id=lead_id or "",
                    mia_introduced=bool(mia_introduced) if mia_introduced is not None else False,
                )
            )
        else:
            row.automation_scope = automation_scope
            if source:
                row.source = source
            if lead_id:
                row.lead_id = lead_id
            if mia_introduced is not None:
                row.mia_introduced = mia_introduced
        self.session.flush()

    def mark_mia_introduced(self, *, channel: str, external_id: str) -> None:
        row = self.get_conversation_control(channel, external_id)
        if row is None:
            return
        row.mia_introduced = True
        self.session.flush()

    def whatsapp_external_id_for_lead(self, lead_id: str) -> str | None:
        lead = self.session.get(LeadRow, lead_id)
        if lead is None:
            return None
        identity = self.session.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.customer_id == lead.customer_id,
                ChannelIdentityRow.channel == Channel.WHATSAPP.value,
            )
        ).first()
        if identity is None:
            return None
        return identity.external_id

    def list_hot_lead_ids(self) -> list[str]:
        from app.domain.conversation_scope import TakeoverState

        rows = self.session.scalars(
            select(LeadRow.id).where(
                LeadRow.takeover_state == TakeoverState.HUMAN_TAKEOVER_REQUIRED.value
            )
        ).all()
        return list(rows)

    def list_all_pending_approvals(self) -> list[ApprovalRow]:
        """Every pending approval, newest first. Read-only; deciding stays typed."""
        return list(
            self.session.scalars(
                select(ApprovalRow)
                .where(ApprovalRow.decision == DECISION_PENDING)
                .order_by(ApprovalRow.id.desc())
            )
        )

    def count_sales_snapshots(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(SalesStateRow)) or 0)

    def list_sales_snapshots(self, *, limit: int = 20) -> list[SalesState]:
        """Sales state for the most recent leads. Facts only, no message text."""
        if limit <= 0:
            return []
        rows = self.session.scalars(
            select(SalesStateRow)
            .join(LeadRow, LeadRow.id == SalesStateRow.lead_id)
            .order_by(LeadRow.id.desc())
            .limit(limit)
        ).all()
        return [sales_from_row(row) for row in rows]

    def find_leads(self, query: str, *, limit: int = 8) -> list[SalesState]:
        """Match a lead by id, stated name, or headline. No fuzzy guessing."""
        needle = query.strip()
        if not needle:
            return []
        cap = max(1, min(int(limit or 8), 8))
        if LEAD_ID_RE.fullmatch(needle):
            try:
                return [self.get_sales(needle)]
            except KeyError:
                return []
        escaped = needle.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        rows = self.session.scalars(
            select(SalesStateRow)
            .where(
                or_(
                    func.lower(SalesStateRow.lead_id).like(pattern, escape="\\"),
                    func.lower(SalesStateRow.display_name).like(pattern, escape="\\"),
                    func.lower(SalesStateRow.headline).like(pattern, escape="\\"),
                )
            )
            .order_by(SalesStateRow.lead_id.desc())
            .limit(cap)
        ).all()
        return [sales_from_row(row) for row in rows]

    def count_pending_approvals(self) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ApprovalRow)
                .where(ApprovalRow.decision == DECISION_PENDING)
            )
            or 0
        )

    def count_human_takeover(self) -> int:
        return int(
            self.session.scalar(
                select(func.count()).select_from(LeadRow).where(LeadRow.human_takeover.is_(True))
            )
            or 0
        )

    def count_failed_webhooks(self) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(WebhookEventRow)
                .where(WebhookEventRow.status == "failed")
            )
            or 0
        )

    def load_website_session_state(self, session_id: str) -> str:
        """Serialized SiteSession state, or empty when this session is new."""
        if not session_id:
            return ""
        row = self.session.get(WebsiteSessionStateRow, session_id)
        return row.state_json if row is not None else ""

    def save_website_session_state(self, session_id: str, state_json: str) -> None:
        """Flush only. The caller's request transaction owns the commit — using a
        second connection here rolls the live request back underneath itself."""
        if not session_id:
            return
        stamp = datetime.now(UTC).isoformat()
        row = self.session.get(WebsiteSessionStateRow, session_id)
        if row is None:
            self.session.add(
                WebsiteSessionStateRow(
                    session_id=session_id, state_json=state_json, updated_at=stamp
                )
            )
        else:
            row.state_json = state_json
            row.updated_at = stamp
        self.session.flush()

    def count_open_reconciliation(self) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ReconciliationFindingRow)
                .where(ReconciliationFindingRow.open.is_(True))
            )
            or 0
        )

    def get_sales(self, lead_id: str) -> SalesState:
        row = self.session.get(SalesStateRow, lead_id)
        if row is None:
            raise KeyError(lead_id)
        return sales_from_row(row)

    def save_sales(self, sales: SalesState) -> None:
        row = self.session.get(SalesStateRow, sales.lead_id)
        if row is None:
            raise KeyError(sales.lead_id)
        row.pain_level = int(sales.pain_level)
        row.fit = sales.fit.value
        row.workflow_known = sales.workflow_known
        row.impact_confirmed = sales.impact_confirmed
        row.reflected = sales.reflected
        row.hypothesis_offered = sales.hypothesis_offered
        row.buying_reality_known = sales.buying_reality_known
        row.authority_known = sales.authority_known
        row.timeline_known = sales.timeline_known
        row.metric_known = sales.metric_known
        if sales.willingness_to_meet is None:
            row.willingness_to_meet = None
        else:
            row.willingness_to_meet = "true" if sales.willingness_to_meet else "false"
        row.owner_required = sales.owner_required
        row.active_objection = (
            sales.active_objection.value if sales.active_objection is not None else None
        )
        row.missing_fields = json.dumps(sales.missing_fields)
        row.company_domain = sanitize_company_domain(sales.company_domain or "") or ""
        row.whatsapp_handoff_offered = sales.whatsapp_handoff_offered
        row.manual_step_known = sales.manual_step_known
        row.data_source_known = sales.data_source_known
        row.discovery_turns = max(0, int(sales.discovery_turns))
        row.asked_actions = json.dumps(
            [value for value in sales.asked_actions if value in _VALID_NEXT_ACTIONS][
                -MAX_ASKED_ACTIONS:
            ]
        )
        row.explicit_buying_intent = sales.explicit_buying_intent
        row.headline = (sales.headline or "")[:120]
        row.display_name = (sales.display_name or "")[:80]
        row.meeting_exit_offered = sales.meeting_exit_offered
        self.session.flush()

    def is_webhook_duplicate(self, *, provider: str, provider_event_id: str) -> bool:
        existing = self.session.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == provider,
                WebhookEventRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()
        if existing is None:
            return False
        return existing.status != "failed"

    def save_transcript(
        self,
        *,
        provider: str,
        provider_event_id: str,
        channel: str,
        external_id: str,
        actor_role: str,
        transcript: str,
        stt_provider: str = "",
        stt_model: str = "",
        language: str = "",
        duration_ms: int = 0,
        confidence: str = "",
    ) -> None:
        existing = self.session.scalars(
            select(VoiceTranscriptRow).where(
                VoiceTranscriptRow.provider == provider,
                VoiceTranscriptRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()
        if existing is not None:
            return
        stored_duration_ms = 0
        if not isinstance(duration_ms, bool):
            try:
                stored_duration_ms = max(0, min(int(duration_ms), 86_400_000))
            except (TypeError, ValueError):
                stored_duration_ms = 0
        self.session.add(
            VoiceTranscriptRow(
                provider=provider,
                provider_event_id=provider_event_id,
                channel=channel,
                external_id=external_id,
                actor_role=actor_role,
                transcript=transcript,
                stt_provider=sanitize_stt_provider(stt_provider),
                stt_model=sanitize_stt_model(stt_model),
                language=sanitize_language(language),
                duration_ms=stored_duration_ms,
                confidence=sanitize_confidence(confidence),
                cost_usd=0,
                retention_status="text_only",
            )
        )
        self.session.flush()

    def get_transcript(self, *, provider: str, provider_event_id: str) -> VoiceTranscriptRow | None:
        return self.session.scalars(
            select(VoiceTranscriptRow).where(
                VoiceTranscriptRow.provider == provider,
                VoiceTranscriptRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()

    def save_ai_run(
        self,
        *,
        run_id: str,
        lead_id: str | None,
        channel: str,
        graph_version: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: int,
        next_action: str,
        kill_switch: bool,
        policy_version: str,
        latency_ms: int = 0,
        automation_mode: str = "",
        prompt_version: str = "",
        decision_confidence: str = "",
        occurred_at: str = "",
    ) -> None:
        existing = self.get_ai_run(run_id)
        if existing is not None:
            return
        self.session.add(
            AiRunRow(
                run_id=run_id,
                lead_id=lead_id,
                channel=channel,
                graph_version=graph_version,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                next_action=next_action,
                kill_switch=kill_switch,
                policy_version=policy_version[:32],
                latency_ms=latency_ms,
                automation_mode=sanitize_automation_mode(automation_mode),
                prompt_version=sanitize_prompt_version(prompt_version),
                decision_confidence=sanitize_decision_confidence(decision_confidence),
                occurred_at=(occurred_at or datetime.now(UTC).isoformat())[:32],
            )
        )
        self.session.flush()

    def get_ai_run(self, run_id: str) -> AiRunRow | None:
        return self.session.scalars(select(AiRunRow).where(AiRunRow.run_id == run_id)).one_or_none()

    def save_shadow_decision(
        self,
        *,
        run_id: str,
        lead_id: str | None,
        channel: str,
        next_action: str,
        proposed_reply: str,
        policy_version: str,
    ) -> None:
        existing = self.get_shadow_decision(run_id)
        if existing is not None:
            return
        self.session.add(
            ShadowDecisionRow(
                run_id=run_id,
                lead_id=lead_id,
                channel=channel,
                next_action=next_action,
                proposed_reply=proposed_reply,
                policy_version=policy_version[:32],
            )
        )
        self.session.flush()

    def get_shadow_decision(self, run_id: str) -> ShadowDecisionRow | None:
        return self.session.scalars(
            select(ShadowDecisionRow).where(ShadowDecisionRow.run_id == run_id)
        ).one_or_none()

    def save_tool_run(
        self,
        *,
        provider_event_id: str,
        provider: str,
        channel: str,
        lead_id: str | None,
        conversation_id: str | None,
        tool: str,
        status: str,
        result_count: int,
        latency_ms: int,
        cost_usd: int,
        freshness: str = "",
        correlation_id: str = "",
    ) -> None:
        existing = self.get_tool_run(provider_event_id)
        if existing is not None:
            return
        self.session.add(
            ToolRunRow(
                provider_event_id=provider_event_id,
                provider=provider[:32],
                channel=channel,
                lead_id=lead_id,
                conversation_id=conversation_id[:255] if conversation_id else None,
                tool=tool,
                status=status,
                result_count=result_count,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                freshness=freshness[:16],
                correlation_id=correlation_id[:64],
            )
        )
        self.session.flush()

    def get_tool_run(self, provider_event_id: str) -> ToolRunRow | None:
        return self.session.scalars(
            select(ToolRunRow).where(ToolRunRow.provider_event_id == provider_event_id)
        ).one_or_none()

    def get_follow_up(self, lead_id: str) -> FollowUpRow | None:
        return self.session.scalars(
            select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)
        ).one_or_none()

    def upsert_follow_up(
        self,
        *,
        lead_id: str,
        channel: str,
        reason: str,
        status: str,
        due_at: str,
    ) -> None:
        row = self.get_follow_up(lead_id)
        if row is None:
            self.session.add(
                FollowUpRow(
                    lead_id=lead_id,
                    channel=channel,
                    reason=reason,
                    status=status,
                    due_at=due_at,
                    send_ready=False,
                    block_reason="",
                    draft="",
                )
            )
        else:
            row.channel = channel
            row.reason = reason
            row.status = status
            row.due_at = due_at
            row.send_ready = False
            row.block_reason = ""
            row.draft = ""
        self.session.flush()

    def list_due_pending_follow_ups(self, *, due_on: str) -> list[FollowUpRow]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_on):
            return []
        return list(
            self.session.scalars(
                select(FollowUpRow).where(
                    FollowUpRow.status == STATUS_PENDING,
                    FollowUpRow.due_at <= due_on,
                )
            ).all()
        )

    def save_follow_up_scan(
        self, *, lead_id: str, send_ready: bool, block_reason: str, draft: str = ""
    ) -> None:
        if block_reason not in ALLOWLISTED_SEND_REASONS:
            return
        row = self.get_follow_up(lead_id)
        if row is None:
            return
        row.send_ready = send_ready
        row.block_reason = block_reason
        if len(draft) > 500:
            draft = ""
        row.draft = draft if send_ready else ""
        self.session.flush()

    def get_seo_recommendation(self, scope: str = "site") -> SeoRecommendationRow | None:
        return self.session.scalars(
            select(SeoRecommendationRow).where(SeoRecommendationRow.scope == scope)
        ).one_or_none()

    def upsert_seo_recommendation(
        self,
        *,
        scope: str,
        problem: str,
        evidence: str,
        why: str,
        change: str,
        metric: str,
    ) -> None:
        row = self.get_seo_recommendation(scope)
        if row is None:
            self.session.add(
                SeoRecommendationRow(
                    scope=scope,
                    problem=problem,
                    evidence=evidence,
                    why=why,
                    change=change,
                    metric=metric,
                )
            )
        else:
            row.problem = problem
            row.evidence = evidence
            row.why = why
            row.change = change
            row.metric = metric
        self.session.flush()

    def count_attribution_for_ig_content(self, media_id: str) -> int:
        if not is_allowlisted_media_id(media_id):
            return 0
        rows = self.session.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.event_type == "attribution",
            )
        ).all()
        count = 0
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            ig_content_id = payload.get("ig_content_id")
            if isinstance(ig_content_id, str) and ig_content_id == media_id:
                count += 1
        return count

    def upsert_content_insight(
        self,
        *,
        media_id: str,
        media_type: str,
        views: str,
        reach: str,
        likes: str,
        comments: str,
        saved: str,
        lead_signals: int,
    ) -> None:
        if not is_allowlisted_media_id(media_id):
            return
        if media_type not in ALLOWLISTED_MEDIA_TYPES:
            return
        if lead_signals < 0:
            return
        row = self.session.scalars(
            select(ContentInsightRow).where(ContentInsightRow.media_id == media_id)
        ).one_or_none()
        if row is None:
            self.session.add(
                ContentInsightRow(
                    media_id=media_id,
                    media_type=media_type,
                    views=views,
                    reach=reach,
                    likes=likes,
                    comments=comments,
                    saved=saved,
                    lead_signals=lead_signals,
                )
            )
        else:
            row.media_type = media_type
            row.views = views
            row.reach = reach
            row.likes = likes
            row.comments = comments
            row.saved = saved
            row.lead_signals = lead_signals
        self.session.flush()

    def list_content_insights(self) -> list[ContentInsightRecord]:
        rows = self.session.scalars(
            select(ContentInsightRow).order_by(ContentInsightRow.media_id)
        ).all()
        return [
            ContentInsightRecord(
                media_id=row.media_id,
                media_type=row.media_type,
                views=row.views,
                reach=row.reach,
                likes=row.likes,
                comments=row.comments,
                saved=row.saved,
                lead_signals=row.lead_signals,
            )
            for row in rows
        ]

    def get_content_idea(self, idea_date: str) -> ContentIdeaRecord | None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", idea_date):
            return None
        row = self.session.scalars(
            select(ContentIdeaRow).where(ContentIdeaRow.idea_date == idea_date)
        ).one_or_none()
        if row is None:
            return None
        try:
            parsed = json.loads(row.kinds or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        kinds = [item for item in parsed if isinstance(item, str) and item in ALLOWLISTED_KINDS]
        return ContentIdeaRecord(idea_date=row.idea_date, kinds=kinds)

    def upsert_content_idea(self, *, idea_date: str, kinds: list[str]) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", idea_date):
            return
        if len(kinds) > 3:
            return
        seen: set[str] = set()
        cleaned: list[str] = []
        for kind in kinds:
            if kind not in ALLOWLISTED_KINDS or kind in seen:
                return
            seen.add(kind)
            cleaned.append(kind)
        payload = json.dumps(cleaned)
        row = self.session.scalars(
            select(ContentIdeaRow).where(ContentIdeaRow.idea_date == idea_date)
        ).one_or_none()
        if row is None:
            self.session.add(ContentIdeaRow(idea_date=idea_date, kinds=payload))
        else:
            row.kinds = payload
        self.session.flush()

    def get_meeting_brief(self, lead_id: str) -> MeetingBriefRow | None:
        return self.session.scalars(
            select(MeetingBriefRow).where(MeetingBriefRow.lead_id == lead_id)
        ).one_or_none()

    def upsert_meeting_brief(
        self,
        *,
        lead_id: str,
        channel: str,
        payload_json: str,
    ) -> None:
        row = self.get_meeting_brief(lead_id)
        if row is None:
            self.session.add(
                MeetingBriefRow(
                    lead_id=lead_id,
                    channel=channel,
                    payload_json=payload_json,
                )
            )
        else:
            row.channel = channel
            row.payload_json = payload_json
        self.session.flush()

    def get_meeting_debrief(self, lead_id: str) -> MeetingDebriefRow | None:
        return self.session.scalars(
            select(MeetingDebriefRow).where(MeetingDebriefRow.lead_id == lead_id)
        ).one_or_none()

    def upsert_meeting_debrief(
        self,
        *,
        lead_id: str,
        outcome: str,
        next_step: str,
        estimated_value: str,
        notes: str,
    ) -> None:
        if outcome not in ALLOWLISTED_OUTCOMES:
            return
        if estimated_value or notes:
            return
        if next_step not in ALLOWLISTED_NEXT_STEPS:
            return
        row = self.get_meeting_debrief(lead_id)
        if row is None:
            self.session.add(
                MeetingDebriefRow(
                    lead_id=lead_id,
                    outcome=outcome,
                    next_step=next_step,
                    estimated_value="",
                    notes="",
                )
            )
        else:
            row.outcome = outcome
            row.next_step = next_step
            row.estimated_value = ""
            row.notes = ""
        self.session.flush()

    def get_lead_review(self, lead_id: str) -> LeadReviewRow | None:
        return self.session.scalars(
            select(LeadReviewRow).where(LeadReviewRow.lead_id == lead_id)
        ).one_or_none()

    def upsert_lead_review(
        self,
        *,
        lead_id: str,
        stage: str,
        fit: str,
        pain_level: int,
        next_action: str,
        missing_fields: str,
        follow_up_status: str,
        follow_up_due_at: str,
        meeting_status: str,
        deal_stage: str,
        conversation_killed: bool,
    ) -> None:
        if LEAD_ID_RE.fullmatch(lead_id) is None:
            return
        if fit not in ALLOWLISTED_FIT:
            return
        if next_action not in ALLOWLISTED_NEXT_ACTION:
            return
        if follow_up_status not in ALLOWLISTED_FOLLOW_UP_STATUS:
            return
        if meeting_status not in ALLOWLISTED_MEETING_STATUS:
            return
        if deal_stage not in ALLOWLISTED_DEAL_STAGE:
            return
        if pain_level < 0 or pain_level > 5:
            return
        if follow_up_due_at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", follow_up_due_at):
            return
        for part in missing_fields.split(","):
            name = part.strip()
            if name and name not in ("decision_maker", "timeline", "metric"):
                return
        row = self.get_lead_review(lead_id)
        if row is None:
            self.session.add(
                LeadReviewRow(
                    lead_id=lead_id,
                    stage=stage,
                    fit=fit,
                    pain_level=pain_level,
                    next_action=next_action,
                    missing_fields=missing_fields,
                    follow_up_status=follow_up_status,
                    follow_up_due_at=follow_up_due_at,
                    meeting_status=meeting_status,
                    deal_stage=deal_stage,
                    conversation_killed=conversation_killed,
                )
            )
        else:
            row.stage = stage
            row.fit = fit
            row.pain_level = pain_level
            row.next_action = next_action
            row.missing_fields = missing_fields
            row.follow_up_status = follow_up_status
            row.follow_up_due_at = follow_up_due_at
            row.meeting_status = meeting_status
            row.deal_stage = deal_stage
            row.conversation_killed = conversation_killed
        self.session.flush()

    def list_gmail_message_in(
        self,
        *,
        lead_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[CanonicalEventRow]:
        if (lead_id is None) == (conversation_id is None):
            return []
        if limit < 1:
            return []
        filters = [
            CanonicalEventRow.provider == "gmail",
            CanonicalEventRow.event_type == "message_in",
        ]
        if lead_id is not None:
            filters.append(CanonicalEventRow.lead_id == lead_id)
        else:
            filters.append(CanonicalEventRow.conversation_id == conversation_id)
        query = (
            select(CanonicalEventRow)
            .where(*filters)
            .order_by(CanonicalEventRow.occurred_at.asc())
            .limit(min(limit, 20))
        )
        return list(self.session.scalars(query).all())

    def get_gmail_thread_summary(self, thread_id: str) -> GmailThreadSummaryRow | None:
        if not thread_id or len(thread_id) > 255:
            return None
        return self.session.scalars(
            select(GmailThreadSummaryRow).where(GmailThreadSummaryRow.thread_id == thread_id)
        ).one_or_none()

    def upsert_gmail_thread_summary(
        self,
        *,
        thread_id: str,
        message_count: int,
        intent: str,
        summary: str,
    ) -> None:
        if not thread_id or len(thread_id) > 255:
            return
        if intent not in GMAIL_SUMMARY_INTENTS:
            return
        if message_count < 0:
            return
        safe_summary = summary if isinstance(summary, str) else ""
        if len(safe_summary) > 400:
            safe_summary = safe_summary[:400]
        row = self.get_gmail_thread_summary(thread_id)
        if row is None:
            self.session.add(
                GmailThreadSummaryRow(
                    thread_id=thread_id,
                    message_count=message_count,
                    intent=intent,
                    summary=safe_summary,
                )
            )
        else:
            row.message_count = message_count
            row.intent = intent
            row.summary = safe_summary
        self.session.flush()

    def get_meeting(self, lead_id: str) -> MeetingRow | None:
        return self.session.scalars(
            select(MeetingRow).where(MeetingRow.lead_id == lead_id)
        ).one_or_none()

    def lock_meeting_for_update(self, lead_id: str) -> MeetingRow | None:
        stmt = select(MeetingRow).where(MeetingRow.lead_id == lead_id)
        bind = self.session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).one_or_none()

    def upsert_meeting_offered(self, *, lead_id: str, source: str) -> None:
        if STATUS_OFFERED not in ALLOWLISTED_STATUSES:
            return
        try:
            Channel(source)
        except ValueError:
            return
        row = self.get_meeting(lead_id)
        if row is None:
            self.session.add(
                MeetingRow(
                    lead_id=lead_id,
                    status=STATUS_OFFERED,
                    source=source,
                    scheduled_at="",
                    calendar_event_id="",
                    summary="",
                    offered_slots_json="[]",
                    meet_link="",
                    meeting_type=MEETING_TYPE_INTRO_CALL,
                    booked_at="",
                    reschedule_slots_json="[]",
                    rescheduled_at="",
                    cancellation_requested_at="",
                )
            )
        elif row.status in {STATUS_BOOKED, STATUS_CANCELLATION_REQUESTED}:
            return
        else:
            row.status = STATUS_OFFERED
            row.source = source
        self.session.flush()

    def save_offered_slots(
        self,
        *,
        lead_id: str,
        slots: list[TimeSlot],
        now: datetime | None = None,
        timezone: str = "Asia/Jerusalem",
    ) -> bool:
        row = self.get_meeting(lead_id)
        if row is None or row.status != STATUS_OFFERED:
            return False
        validated = validate_offered_slots(slots, now=now, timezone=timezone)
        if not validated:
            return False
        row.offered_slots_json = offered_slots_to_json(validated)
        row.meeting_type = MEETING_TYPE_INTRO_CALL
        self.session.flush()
        return True

    def get_offered_slots(self, lead_id: str) -> list[TimeSlot]:
        row = self.get_meeting(lead_id)
        if row is None:
            return []
        offered = offered_slots_from_json(row.offered_slots_json or "[]")
        return [TimeSlot(start=item.start, end=item.end) for item in offered]

    def clear_offered_slots(self, lead_id: str) -> None:
        row = self.get_meeting(lead_id)
        if row is None:
            return
        if row.status != STATUS_OFFERED:
            return
        row.offered_slots_json = "[]"
        self.session.flush()

    def save_reschedule_slots(
        self,
        *,
        lead_id: str,
        slots: list[TimeSlot],
        now: datetime | None = None,
        timezone: str = "Asia/Jerusalem",
    ) -> bool:
        row = self.lock_meeting_for_update(lead_id)
        if (
            row is None
            or row.status != STATUS_BOOKED
            or sanitize_event_id(row.calendar_event_id) is None
        ):
            return False
        validated = validate_offered_slots(slots, now=now, timezone=timezone)
        if not validated or len(validated) > 3:
            return False
        row.reschedule_slots_json = offered_slots_to_json(validated)
        self.session.flush()
        return True

    def clear_reschedule_slots(self, lead_id: str) -> None:
        row = self.lock_meeting_for_update(lead_id)
        if row is None:
            return
        if row.status not in {STATUS_BOOKED, STATUS_CANCELLATION_REQUESTED}:
            return
        row.reschedule_slots_json = "[]"
        self.session.flush()

    def mark_meeting_rescheduled(
        self,
        *,
        lead_id: str,
        scheduled_at: str,
        calendar_event_id: str,
        rescheduled_at: str,
    ) -> bool:
        normalized_at = normalize_scheduled_at_utc(scheduled_at)
        normalized_rescheduled_at = normalize_scheduled_at_utc(rescheduled_at)
        clean_event_id = sanitize_event_id(calendar_event_id)
        if normalized_at is None or normalized_rescheduled_at is None or clean_event_id is None:
            return False
        row = self.lock_meeting_for_update(lead_id)
        if row is None or row.status != STATUS_BOOKED:
            return False
        if sanitize_event_id(row.calendar_event_id) != clean_event_id:
            return False
        if not row.booked_at or row.meeting_type not in ALLOWLISTED_MEETING_TYPES:
            return False
        row.scheduled_at = normalized_at
        row.rescheduled_at = normalized_rescheduled_at
        row.reschedule_slots_json = "[]"
        self.session.flush()
        return True

    def mark_meeting_cancellation_requested(
        self,
        *,
        lead_id: str,
        requested_at: str,
    ) -> bool:
        normalized_requested_at = normalize_scheduled_at_utc(requested_at)
        if normalized_requested_at is None:
            return False
        row = self.lock_meeting_for_update(lead_id)
        if row is None:
            return False
        if row.status == STATUS_CANCELLATION_REQUESTED:
            return True
        if row.status != STATUS_BOOKED:
            return False
        if sanitize_event_id(row.calendar_event_id) is None:
            return False
        row.status = STATUS_CANCELLATION_REQUESTED
        row.cancellation_requested_at = normalized_requested_at
        row.reschedule_slots_json = "[]"
        self.session.flush()
        return True

    def mark_meeting_booked(
        self,
        *,
        lead_id: str,
        scheduled_at: str,
        calendar_event_id: str,
        meet_link: str = "",
        booked_at: str | None = None,
        meeting_type: str = MEETING_TYPE_INTRO_CALL,
    ) -> bool:
        normalized_at = normalize_scheduled_at_utc(scheduled_at)
        clean_event_id = sanitize_event_id(calendar_event_id)
        clean_meet_link = sanitize_meet_link(meet_link)
        if normalized_at is None or clean_event_id is None:
            return False
        if meeting_type not in ALLOWLISTED_MEETING_TYPES:
            return False
        normalized_booked_at = ""
        if booked_at is not None:
            normalized_booked_at = normalize_scheduled_at_utc(booked_at) or ""
            if not normalized_booked_at:
                return False
        row = self.lock_meeting_for_update(lead_id)
        if row is None:
            return False
        if row.status == STATUS_BOOKED:
            if row.calendar_event_id and row.calendar_event_id != clean_event_id:
                return False
            if row.calendar_event_id:
                return True
            row.scheduled_at = normalized_at
            row.calendar_event_id = clean_event_id
            if clean_meet_link:
                row.meet_link = clean_meet_link
            row.meeting_type = meeting_type
            if normalized_booked_at:
                row.booked_at = normalized_booked_at
            row.offered_slots_json = "[]"
            row.reschedule_slots_json = "[]"
            self.session.flush()
            return True
        if row.status != STATUS_OFFERED:
            return False
        if not normalized_booked_at:
            return False
        row.status = STATUS_BOOKED
        row.scheduled_at = normalized_at
        row.calendar_event_id = clean_event_id
        row.meet_link = clean_meet_link
        row.meeting_type = meeting_type
        row.booked_at = normalized_booked_at
        row.offered_slots_json = "[]"
        row.reschedule_slots_json = "[]"
        row.rescheduled_at = ""
        row.cancellation_requested_at = ""
        row.summary = ""
        self.session.flush()
        return True

    def upsert_meeting(
        self,
        *,
        lead_id: str,
        status: str,
        source: str,
        scheduled_at: str,
        calendar_event_id: str,
        summary: str,
    ) -> None:
        """Legacy offered-only upsert; prefer upsert_meeting_offered."""
        del scheduled_at, calendar_event_id, summary
        if status != STATUS_OFFERED:
            return
        self.upsert_meeting_offered(lead_id=lead_id, source=source)

    def has_attribution(self, lead_id: str) -> bool:
        return self.get_attribution_payload(lead_id) is not None

    def get_attribution_payload(self, lead_id: str) -> dict[str, str] | None:
        row = self.session.scalars(
            select(CanonicalEventRow)
            .where(
                CanonicalEventRow.lead_id == lead_id,
                CanonicalEventRow.event_type == "attribution",
            )
            .limit(1)
        ).first()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        return {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def get_deal(self, lead_id: str) -> DealRow | None:
        return self.session.scalars(select(DealRow).where(DealRow.lead_id == lead_id)).one_or_none()

    def upsert_deal(
        self,
        *,
        lead_id: str,
        stage: str,
        source: str,
        attribution_confidence: str,
    ) -> None:
        if stage not in ALLOWLISTED_STAGES or attribution_confidence not in ALLOWLISTED_CONFIDENCE:
            return
        row = self.get_deal(lead_id)
        if row is None:
            self.session.add(
                DealRow(
                    lead_id=lead_id,
                    stage=stage,
                    expected_value="",
                    closed_value="",
                    source=source,
                    attribution_confidence=attribution_confidence,
                )
            )
        else:
            existing_rank = _DEAL_STAGE_RANK.get(row.stage, -1)
            new_rank = _DEAL_STAGE_RANK.get(stage, -1)
            if new_rank >= existing_rank:
                row.stage = stage
            row.source = source
            row.attribution_confidence = attribution_confidence
            row.expected_value = ""
            row.closed_value = ""
        self.session.flush()

    def get_approval_by_approval_id(self, approval_id: str) -> ApprovalRow | None:
        if not approval_id:
            return None
        return self.session.scalars(
            select(ApprovalRow).where(ApprovalRow.approval_id == approval_id)
        ).one_or_none()

    def get_approval(self, lead_id: str, action: str) -> ApprovalRow | None:
        if not lead_id:
            return None
        return self.session.scalars(
            select(ApprovalRow).where(
                ApprovalRow.lead_id == lead_id,
                ApprovalRow.lead_id.isnot(None),
                ApprovalRow.action == action,
            )
        ).one_or_none()

    def get_approval_by_resource(
        self, resource_type: str, resource_id: str, action: str
    ) -> ApprovalRow | None:
        return self.session.scalars(
            select(ApprovalRow).where(
                ApprovalRow.resource_type == resource_type,
                ApprovalRow.resource_id == resource_id,
                ApprovalRow.action == action,
            )
        ).one_or_none()

    def upsert_approval(
        self,
        *,
        lead_id: str,
        channel: str,
        action: str,
        risk: str,
        payload_hash: str,
        decision: str,
        resource_type: str,
        resource_id: str,
        expires_at: str,
        approver: str = "",
    ) -> None:
        if action != ACTION_PROPOSAL_HANDOFF or risk != RISK_R3 or decision != DECISION_PENDING:
            return
        if resource_type != RESOURCE_LEAD:
            return
        if resource_id != lead_id:
            return
        if not expires_at:
            return
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return
        params = proposed_parameters_json(
            action=action,
            risk=risk,
            channel=channel,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        row = self.get_approval(lead_id, action)
        if row is None:
            self.session.add(
                ApprovalRow(
                    lead_id=lead_id,
                    channel=channel,
                    action=action,
                    risk=risk,
                    payload_hash=payload_hash,
                    decision=decision,
                    approver="",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    expires_at=expires_at,
                    approval_id=new_approval_id(),
                    proposed_parameters=params,
                )
            )
        elif row.decision == DECISION_PENDING:
            row.channel = channel
            row.risk = risk
            row.payload_hash = payload_hash
            row.decision = decision
            row.approver = ""
            row.resource_type = resource_type
            row.resource_id = resource_id
            row.expires_at = expires_at
            row.proposed_parameters = params
        self.session.flush()

    def upsert_gmail_approval(
        self,
        *,
        channel: str,
        action: str,
        risk: str,
        payload_hash: str,
        decision: str,
        resource_type: str,
        resource_id: str,
        expires_at: str,
    ) -> None:
        if action != ACTION_GMAIL_SEND or risk != RISK_R3 or decision != DECISION_PENDING:
            return
        if resource_type != RESOURCE_GMAIL:
            return
        if not resource_id or len(resource_id) > 40 or not expires_at:
            return
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return
        params = proposed_parameters_json(
            action=action,
            risk=risk,
            channel=channel,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        row = self.get_approval_by_resource(RESOURCE_GMAIL, resource_id, ACTION_GMAIL_SEND)
        if row is None:
            self.session.add(
                ApprovalRow(
                    lead_id=None,
                    channel=channel,
                    action=action,
                    risk=risk,
                    payload_hash=payload_hash,
                    decision=decision,
                    approver="",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    expires_at=expires_at,
                    approval_id=new_approval_id(),
                    proposed_parameters=params,
                )
            )
        elif row.decision == DECISION_PENDING:
            row.channel = channel
            row.risk = risk
            row.payload_hash = payload_hash
            row.decision = decision
            row.approver = ""
            row.resource_type = resource_type
            row.resource_id = resource_id
            row.expires_at = expires_at
            row.proposed_parameters = params
        self.session.flush()

    def upsert_calendar_approval(
        self,
        *,
        channel: str,
        action: str,
        risk: str,
        payload_hash: str,
        decision: str,
        resource_id: str,
        expires_at: str,
        proposed_parameters: str,
    ) -> None:
        """Persist an exact owner calendar change awaiting approval.

        The compact JSON is not display text: it is the immutable payload that is
        hashed and re-checked immediately before the provider write.
        """
        if action not in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE):
            return
        if risk != RISK_R3 or decision != DECISION_PENDING:
            return
        if not resource_id or len(resource_id) > 80 or not expires_at:
            return
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return
        if not proposed_parameters or len(proposed_parameters) > 255:
            return
        row = self.get_approval_by_resource(RESOURCE_CALENDAR, resource_id, action)
        if row is None:
            self.session.add(
                ApprovalRow(
                    lead_id=None,
                    channel=channel,
                    action=action,
                    risk=risk,
                    payload_hash=payload_hash,
                    decision=decision,
                    approver="",
                    resource_type=RESOURCE_CALENDAR,
                    resource_id=resource_id,
                    expires_at=expires_at,
                    approval_id=new_approval_id(),
                    proposed_parameters=proposed_parameters,
                )
            )
        elif row.decision == DECISION_PENDING:
            row.channel = channel
            row.risk = risk
            row.payload_hash = payload_hash
            row.expires_at = expires_at
            row.proposed_parameters = proposed_parameters
        self.session.flush()

    def upsert_linkedin_approval(
        self, *, channel: str, action: str, risk: str, payload_hash: str,
        decision: str, resource_id: str, expires_at: str, proposed_parameters: str,
    ) -> None:
        """Persist one exact LinkedIn provider write; its arguments are rehashed at execute."""
        if action != "linkedin_composio_write" or risk not in ("R3", "R4"):
            return
        if decision != DECISION_PENDING or not resource_id or len(resource_id) > 80:
            return
        from app.domain.owner_linkedin_writes import linkedin_parameters_within_bound

        if not linkedin_parameters_within_bound(proposed_parameters):
            return
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return
        row = self.get_approval_by_resource("linkedin_tool", resource_id, action)
        if row is None:
            self.session.add(ApprovalRow(
                lead_id=None, channel=channel, action=action, risk=risk,
                payload_hash=payload_hash, decision=decision, approver="",
                resource_type="linkedin_tool", resource_id=resource_id,
                expires_at=expires_at, approval_id=new_approval_id(),
                proposed_parameters=proposed_parameters,
            ))
        elif row.decision == DECISION_PENDING:
            row.channel, row.risk, row.payload_hash = channel, risk, payload_hash
            row.expires_at, row.proposed_parameters = expires_at, proposed_parameters
        self.session.flush()

    def decide_linkedin_approval(self, *, resource_id: str, decision: str) -> bool:
        if decision not in (DECISION_APPROVED, DECISION_REJECTED):
            return False
        row = self.get_approval_by_resource("linkedin_tool", resource_id, "linkedin_composio_write")
        if row is None or row.decision != DECISION_PENDING:
            return False
        row.decision, row.approver = decision, ""
        if decision == DECISION_APPROVED:
            row.approved_at = datetime.now(UTC).isoformat()
        self.session.flush()
        return True

    def upsert_composio_approval(
        self, *, channel: str, action: str, risk: str, payload_hash: str,
        decision: str, resource_id: str, expires_at: str, proposed_parameters: str,
    ) -> None:
        """Persist one exact Composio provider write; arguments are rehashed at execute."""
        if action != "composio_write" or risk not in ("R3", "R4", "R5"):
            return
        if decision != DECISION_PENDING or not resource_id or len(resource_id) > 80:
            return
        from app.domain.owner_composio_writes import composio_parameters_within_bound

        if not composio_parameters_within_bound(proposed_parameters):
            return
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return
        row = self.get_approval_by_resource("composio_tool", resource_id, action)
        if row is None:
            self.session.add(ApprovalRow(
                lead_id=None, channel=channel, action=action, risk=risk,
                payload_hash=payload_hash, decision=decision, approver="",
                resource_type="composio_tool", resource_id=resource_id,
                expires_at=expires_at, approval_id=new_approval_id(),
                proposed_parameters=proposed_parameters,
            ))
        elif row.decision == DECISION_PENDING:
            row.channel, row.risk, row.payload_hash = channel, risk, payload_hash
            row.expires_at, row.proposed_parameters = expires_at, proposed_parameters
        self.session.flush()

    def decide_composio_approval(self, *, resource_id: str, decision: str) -> bool:
        if decision not in (DECISION_APPROVED, DECISION_REJECTED):
            return False
        row = self.get_approval_by_resource("composio_tool", resource_id, "composio_write")
        if row is None or row.decision != DECISION_PENDING:
            return False
        row.decision, row.approver = decision, ""
        if decision == DECISION_APPROVED:
            row.approved_at = datetime.now(UTC).isoformat()
        self.session.flush()
        return True

    def decide_calendar_approval(
        self,
        *,
        resource_id: str,
        action: str,
        decision: str,
        now: datetime | None = None,
    ) -> bool:
        if action not in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE):
            return False
        if decision not in (DECISION_APPROVED, DECISION_REJECTED):
            return False
        row = self.get_approval_by_resource(RESOURCE_CALENDAR, resource_id, action)
        if row is None or row.decision != DECISION_PENDING:
            return False
        effective_now = now if now is not None else datetime.now(UTC)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=UTC)
        row.decision = decision
        row.approver = ""
        if decision == DECISION_APPROVED:
            row.approved_at = effective_now.isoformat()
        self.session.flush()
        return True

    def upsert_website_approval(
        self,
        *,
        channel: str,
        action: str,
        risk: str,
        payload_hash: str,
        decision: str,
        resource_type: str,
        resource_id: str,
        expires_at: str,
        proposed_parameters: str,
    ) -> None:
        if action != ACTION_WEBSITE_EDIT or risk != RISK_R3 or decision != DECISION_PENDING:
            return
        if resource_type != RESOURCE_WEBSITE:
            return
        if not resource_id or not expires_at:
            return
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return
        row = self.get_approval_by_resource(RESOURCE_WEBSITE, resource_id, ACTION_WEBSITE_EDIT)
        if row is None:
            self.session.add(
                ApprovalRow(
                    lead_id=None,
                    channel=channel,
                    action=action,
                    risk=risk,
                    payload_hash=payload_hash,
                    decision=decision,
                    approver="",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    expires_at=expires_at,
                    approval_id=new_approval_id(),
                    proposed_parameters=proposed_parameters[:255],
                )
            )
        elif row.decision == DECISION_PENDING:
            row.channel = channel
            row.risk = risk
            row.payload_hash = payload_hash
            row.decision = decision
            row.approver = ""
            row.resource_type = resource_type
            row.resource_id = resource_id
            row.expires_at = expires_at
            row.proposed_parameters = proposed_parameters[:255]
        self.session.flush()

    def list_pending_approvals(self, *, action: str) -> list[ApprovalRow]:
        if action != ACTION_PROPOSAL_HANDOFF:
            return []
        return list(
            self.session.scalars(
                select(ApprovalRow).where(
                    ApprovalRow.action == action,
                    ApprovalRow.decision == DECISION_PENDING,
                )
            )
        )

    def decide_approval(
        self,
        *,
        lead_id: str,
        action: str,
        decision: str,
        now: datetime | None = None,
    ) -> bool:
        if action != ACTION_PROPOSAL_HANDOFF:
            return False
        if decision not in (DECISION_APPROVED, DECISION_REJECTED):
            return False
        row = self.get_approval(lead_id, action)
        if row is None or row.decision != DECISION_PENDING:
            return False
        effective_now = now if now is not None else datetime.now(UTC)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=UTC)
        row.decision = decision
        row.approver = ""
        if decision == DECISION_APPROVED:
            row.approved_at = effective_now.isoformat()
        self.session.flush()
        return True

    def decide_gmail_approval(
        self,
        *,
        resource_id: str,
        decision: str,
        now: datetime | None = None,
    ) -> bool:
        if decision not in (DECISION_APPROVED, DECISION_REJECTED):
            return False
        row = self.get_approval_by_resource(RESOURCE_GMAIL, resource_id, ACTION_GMAIL_SEND)
        if row is None or row.decision != DECISION_PENDING:
            return False
        effective_now = now if now is not None else datetime.now(UTC)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=UTC)
        row.decision = decision
        row.approver = ""
        if decision == DECISION_APPROVED:
            row.approved_at = effective_now.isoformat()
        self.session.flush()
        return True

    def decide_website_approval(
        self,
        *,
        resource_id: str,
        decision: str,
        now: datetime | None = None,
    ) -> bool:
        if decision not in (DECISION_APPROVED, DECISION_REJECTED):
            return False
        row = self.get_approval_by_resource(RESOURCE_WEBSITE, resource_id, ACTION_WEBSITE_EDIT)
        if row is None or row.decision != DECISION_PENDING:
            return False
        effective_now = now if now is not None else datetime.now(UTC)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=UTC)
        row.decision = decision
        row.approver = ""
        if decision == DECISION_APPROVED:
            row.approved_at = effective_now.isoformat()
        self.session.flush()
        return True

    def save_owner_task(
        self,
        *,
        provider: str,
        provider_event_id: str,
        channel: str,
        external_id: str,
        task_type: str,
        status: str,
        summary: str = "",
        due_at: str | None = None,
        trigger: str = "none",
        condition: str = "none",
        action: str = "none",
    ) -> None:
        existing = self.session.scalars(
            select(OwnerTaskRow).where(
                OwnerTaskRow.provider == provider,
                OwnerTaskRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()
        if existing is not None:
            return
        self.session.add(
            OwnerTaskRow(
                provider=provider,
                provider_event_id=provider_event_id,
                channel=channel,
                external_id=external_id,
                task_type=task_type,
                status=status,
                summary=summary,
                due_at=due_at,
                trigger=trigger,
                condition=condition,
                action=action,
            )
        )
        self.session.flush()

    def get_owner_task(self, *, provider: str, provider_event_id: str) -> OwnerTaskRow | None:
        return self.session.scalars(
            select(OwnerTaskRow).where(
                OwnerTaskRow.provider == provider,
                OwnerTaskRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()

    def list_due_owner_tasks(self, *, due_on: str) -> list[OwnerTaskRow]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_on):
            return []
        rows = self.session.scalars(
            select(OwnerTaskRow).where(
                OwnerTaskRow.status == "logged",
                OwnerTaskRow.due_at.isnot(None),
                OwnerTaskRow.due_at <= due_on,
            )
        ).all()
        return [
            row for row in rows if row.due_at and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.due_at)
        ]

    def save_owner_task_scan(
        self,
        *,
        provider: str,
        provider_event_id: str,
        due_ready: bool,
        block_reason: str,
    ) -> None:
        if block_reason not in ALLOWLISTED_OWNER_TASK_SCAN_REASONS:
            return
        row = self.get_owner_task(provider=provider, provider_event_id=provider_event_id)
        if row is None:
            return
        row.due_ready = due_ready
        row.block_reason = block_reason
        self.session.flush()

    def save_proposed_instruction(
        self,
        *,
        provider: str,
        provider_event_id: str,
        kind: str,
        body: str,
        status: str = "proposed",
    ) -> None:
        existing = self.session.scalars(
            select(OwnerInstructionRow).where(
                OwnerInstructionRow.provider == provider,
                OwnerInstructionRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()
        if existing is not None:
            return
        status = "proposed"
        self.session.add(
            OwnerInstructionRow(
                provider=provider,
                provider_event_id=provider_event_id,
                kind=kind,
                body=body,
                status=status,
            )
        )
        self.session.flush()

    def get_proposed_instruction(
        self, *, provider: str, provider_event_id: str
    ) -> OwnerInstructionRow | None:
        return self.session.scalars(
            select(OwnerInstructionRow).where(
                OwnerInstructionRow.provider == provider,
                OwnerInstructionRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()

    def list_active_instructions(self) -> list[OwnerInstructionRow]:
        return list(
            self.session.scalars(
                select(OwnerInstructionRow).where(OwnerInstructionRow.status == "active")
            ).all()
        )

    def save_owner_correction(
        self,
        *,
        provider: str,
        provider_event_id: str,
        scope: str,
        body: str,
        status: str = "logged",
    ) -> bool:
        existing = self.session.scalars(
            select(OwnerCorrectionRow).where(
                OwnerCorrectionRow.provider == provider,
                OwnerCorrectionRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()
        if existing is not None:
            return False
        status = "logged"
        self.session.add(
            OwnerCorrectionRow(
                provider=provider,
                provider_event_id=provider_event_id,
                scope=scope,
                body=body,
                status=status,
            )
        )
        self.session.flush()
        return True

    def get_owner_correction(
        self, *, provider: str, provider_event_id: str
    ) -> OwnerCorrectionRow | None:
        return self.session.scalars(
            select(OwnerCorrectionRow).where(
                OwnerCorrectionRow.provider == provider,
                OwnerCorrectionRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()

    def claim_operation(
        self, *, scope: str, key: str, ttl_seconds: int = OPERATION_TTL_SECONDS
    ) -> bool:
        """Return True when operation is newly claimed or reclaimed after expiry/failure."""
        if not scope or not key:
            return False
        if scope not in ALLOWLISTED_OPERATION_SCOPES:
            return False
        now = datetime.now(UTC)
        ttl_seconds = max(1, min(int(ttl_seconds), 86_400))
        new_expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        claimed_at = now.isoformat()
        existing = self.session.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        if existing is None:
            self.session.add(
                IdempotencyRow(
                    scope=scope,
                    key=key,
                    created_at=claimed_at,
                    status="in_flight",
                    expires_at=new_expires_at,
                    result_json="{}",
                )
            )
            self.session.flush()
            return True
        if existing.status == "completed":
            return False
        if existing.status == "in_flight":
            if not existing.expires_at:
                return False
            try:
                expiry = datetime.fromisoformat(existing.expires_at)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=UTC)
            except ValueError:
                return False
            if expiry > now:
                return False
            existing.status = "in_flight"
            existing.expires_at = new_expires_at
            existing.result_json = "{}"
            self.session.flush()
            return True
        if existing.status == "failed":
            existing.status = "in_flight"
            existing.expires_at = new_expires_at
            existing.result_json = "{}"
            self.session.flush()
            return True
        return False

    def complete_operation(self, *, scope: str, key: str, result_json: str = "{}") -> None:
        if not key or scope not in ALLOWLISTED_OPERATION_SCOPES:
            return
        existing = self.session.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        if existing is None:
            return
        existing.status = "completed"
        existing.result_json = sanitize_operation_result(result_json)
        self.session.flush()

    def fail_operation(self, *, scope: str, key: str) -> None:
        if not key or scope not in ALLOWLISTED_OPERATION_SCOPES:
            return
        existing = self.session.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        if existing is None or existing.status != "in_flight":
            return
        existing.status = "failed"
        self.session.flush()

    def get_operation_result(self, *, scope: str, key: str) -> str:
        if not key or scope not in ALLOWLISTED_OPERATION_SCOPES:
            return "{}"
        existing = self.session.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        if existing is None or existing.status != "completed":
            return "{}"
        return existing.result_json or "{}"

    def claim_provider_write(self, *, scope: str, key: str) -> bool:
        """Durably reserve a provider write before its external call.

        Unlike ordinary operation claims, this reservation never expires or becomes
        reclaimable.  A process can die after the provider accepts a write but before
        our outcome commit; reissuing that write automatically would be unsafe.
        """
        if not key or scope not in ALLOWLISTED_OPERATION_SCOPES:
            return False
        existing = self.session.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        if existing is not None:
            return False
        now = datetime.now(UTC).isoformat()
        self.session.add(
            IdempotencyRow(
                scope=scope,
                key=key,
                created_at=now,
                status="provider_claimed",
                expires_at="",
                result_json="{}",
            )
        )
        try:
            self.session.commit()
        except SQLAlchemyError:
            self.session.rollback()
            return False
        return True

    def complete_provider_write(self, *, scope: str, key: str, result_json: str = "{}") -> bool:
        """Persist a known provider success without making an ambiguous claim retryable."""
        if not key or scope not in ALLOWLISTED_OPERATION_SCOPES:
            return False
        existing = self.session.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        if existing is None or existing.status != "provider_claimed":
            return False
        existing.status = "completed"
        existing.result_json = sanitize_operation_result(result_json)
        try:
            self.session.commit()
        except SQLAlchemyError:
            self.session.rollback()
            return False
        return True

    def mark_provider_write_pending_review(self, *, scope: str, key: str) -> None:
        """Keep an indeterminate provider outcome permanently out of auto-retry."""
        if not key or scope not in ALLOWLISTED_OPERATION_SCOPES:
            return
        existing = self.session.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        if existing is None or existing.status != "provider_claimed":
            return
        existing.status = "pending_review"
        try:
            self.session.commit()
        except SQLAlchemyError:
            self.session.rollback()

    def get_provider_write_status(self, *, scope: str, key: str) -> str:
        if not key or scope not in ALLOWLISTED_OPERATION_SCOPES:
            return ""
        existing = self.session.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == scope,
                IdempotencyRow.key == key,
            )
        ).one_or_none()
        return existing.status if existing is not None else ""

    def _fill_webhook_envelope_if_empty(
        self,
        row: WebhookEventRow,
        *,
        channel: str,
        envelope_kind: str,
    ) -> None:
        if not row.channel and channel:
            row.channel = channel
        if not row.envelope_kind and envelope_kind:
            row.envelope_kind = envelope_kind

    def claim_webhook(
        self,
        *,
        provider: str,
        provider_event_id: str,
        channel: str = "",
        envelope_kind: str = "",
    ) -> bool:
        """Return True if newly claimed or reclaimed after failed/stale received.

        In-flight ``received`` rows older than reconciliation ``STALE_AFTER_SECONDS``
        (300) may be reclaimed; ``processed`` and ``sent`` stay unique regardless of age.
        """
        safe_channel = sanitize_webhook_channel(channel)
        safe_kind = sanitize_webhook_envelope_kind(envelope_kind)
        now = datetime.now(UTC)
        claimed_at = now.isoformat()
        existing = self.session.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == provider,
                WebhookEventRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()
        if existing is not None:
            if existing.status == "failed":
                existing.status = "received"
                existing.claimed_at = claimed_at
                self._fill_webhook_envelope_if_empty(
                    existing, channel=safe_channel, envelope_kind=safe_kind
                )
                self.session.flush()
                return True
            if existing.status == "received":
                if is_stale_received(claimed_at=existing.claimed_at, now=now):
                    existing.status = "received"
                    existing.claimed_at = claimed_at
                    self._fill_webhook_envelope_if_empty(
                        existing, channel=safe_channel, envelope_kind=safe_kind
                    )
                    self.session.flush()
                    return True
                return False
            return False
        self.session.add(
            WebhookEventRow(
                provider=provider,
                provider_event_id=provider_event_id,
                status="received",
                claimed_at=claimed_at,
                channel=safe_channel,
                envelope_kind=safe_kind,
            )
        )
        self.session.flush()
        return True

    def get_webhook(self, *, provider: str, provider_event_id: str) -> WebhookEventRow | None:
        return self.session.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == provider,
                WebhookEventRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()

    def mark_webhook(self, *, provider: str, provider_event_id: str, status: str) -> None:
        if status not in WEBHOOK_STATUSES:
            return
        row = self.session.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == provider,
                WebhookEventRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()
        if row is None:
            raise KeyError(f"{provider}:{provider_event_id}")
        row.status = status
        self.session.flush()

    def list_webhooks_by_status(self, *, status: str) -> list[WebhookEventRow]:
        if status not in WEBHOOK_STATUSES:
            return []
        return list(
            self.session.scalars(
                select(WebhookEventRow).where(WebhookEventRow.status == status)
            ).all()
        )

    def list_expired_unconsumed_handoffs(self, *, now_iso: str) -> list[HandoffTokenRow]:
        if not now_iso:
            return []
        return list(
            self.session.scalars(
                select(HandoffTokenRow).where(
                    HandoffTokenRow.consumed_at.is_(None),
                    HandoffTokenRow.expires_at < now_iso,
                )
            ).all()
        )

    def upsert_reconciliation_finding(
        self,
        *,
        kind: str,
        subject_key: str,
        reason: str,
        open: bool = True,
    ) -> None:
        if kind not in RECONCILIATION_FINDING_KINDS or reason not in RECONCILIATION_FINDING_KINDS:
            return
        row = self.get_reconciliation_finding(kind=kind, subject_key=subject_key)
        if row is None:
            self.session.add(
                ReconciliationFindingRow(
                    kind=kind,
                    subject_key=subject_key,
                    reason=reason,
                    open=open,
                )
            )
        else:
            row.reason = reason
            row.open = open
        self.session.flush()

    def get_reconciliation_finding(
        self, *, kind: str, subject_key: str
    ) -> ReconciliationFindingRow | None:
        if kind not in RECONCILIATION_FINDING_KINDS:
            return None
        return self.session.scalars(
            select(ReconciliationFindingRow).where(
                ReconciliationFindingRow.kind == kind,
                ReconciliationFindingRow.subject_key == subject_key,
            )
        ).one_or_none()

    def list_open_reconciliation_findings(self) -> list[ReconciliationFindingRow]:
        return list(
            self.session.scalars(
                select(ReconciliationFindingRow).where(
                    ReconciliationFindingRow.open.is_(True),
                    ReconciliationFindingRow.kind.in_(RECONCILIATION_FINDING_KINDS),
                )
            ).all()
        )

    def save_canonical_event(self, *, provider: str, event: CanonicalEvent) -> None:
        existing = self.get_canonical_event(
            provider=provider, provider_event_id=event.idempotency_key
        )
        if existing is not None:
            return
        stamp_payload_version(event)
        self.session.add(
            CanonicalEventRow(
                event_id=event.event_id,
                provider=provider,
                provider_event_id=event.idempotency_key,
                event_type=event.event_type.value,
                channel=event.channel.value,
                occurred_at=event.occurred_at.isoformat(),
                idempotency_key=event.idempotency_key,
                lead_id=event.lead_id,
                conversation_id=event.conversation_id,
                actor_role=event.actor_role,
                payload_json=json.dumps(event.payload),
                source_json=json.dumps(event.source),
                correlation_id=event.correlation_id,
                payload_version=event.payload_version,
            )
        )
        self.session.flush()

    def get_canonical_event(
        self, *, provider: str, provider_event_id: str
    ) -> CanonicalEventRow | None:
        return self.session.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider == provider,
                CanonicalEventRow.provider_event_id == provider_event_id,
            )
        ).one_or_none()

    def count_canonical_events(
        self, *, event_type: str, occurred_from: str, occurred_to: str
    ) -> int:
        if event_type not in COUNTABLE_EVENT_TYPES:
            return 0
        count = self.session.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(
                CanonicalEventRow.event_type == event_type,
                CanonicalEventRow.occurred_at >= occurred_from,
                CanonicalEventRow.occurred_at < occurred_to,
            )
        )
        return int(count or 0)

    def count_behavior_events(self, *, kind: str, occurred_from: str, occurred_to: str) -> int:
        if kind not in ALL_BEHAVIOR_KINDS:
            return 0
        rows = self.session.scalars(
            select(CanonicalEventRow.payload_json).where(
                CanonicalEventRow.event_type == "behavior",
                CanonicalEventRow.occurred_at >= occurred_from,
                CanonicalEventRow.occurred_at < occurred_to,
            )
        ).all()
        count = 0
        for payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("kind") == kind:
                count += 1
        return count

    def count_canonical_events_for_lead(
        self, *, lead_id: str, event_type: str, occurred_from: str, occurred_to: str
    ) -> int:
        if event_type not in FREQUENCY_EVENT_TYPES:
            return 0
        count = self.session.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(
                CanonicalEventRow.lead_id == lead_id,
                CanonicalEventRow.event_type == event_type,
                CanonicalEventRow.occurred_at >= occurred_from,
                CanonicalEventRow.occurred_at < occurred_to,
            )
        )
        return int(count or 0)

    def count_follow_ups(self, *, status: str) -> int:
        if status not in {"pending", "cancelled", "recovered"}:
            return 0
        count = self.session.scalar(
            select(func.count()).select_from(FollowUpRow).where(FollowUpRow.status == status)
        )
        return int(count or 0)

    def count_follow_ups_due_on(self, *, due_on: str, status: str) -> int:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_on):
            return 0
        if status not in {"pending", "cancelled", "recovered"}:
            return 0
        count = self.session.scalar(
            select(func.count())
            .select_from(FollowUpRow)
            .where(
                FollowUpRow.status == status,
                FollowUpRow.due_at == due_on,
            )
        )
        return int(count or 0)

    def get_owner_brief(self, brief_date: str) -> OwnerBriefRow | None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", brief_date):
            return None
        return self.session.scalars(
            select(OwnerBriefRow).where(OwnerBriefRow.brief_date == brief_date)
        ).one_or_none()

    def has_owner_notification(self, *, kind: str, lead_id: str) -> bool:
        if not kind or not lead_id:
            return False
        return (
            self.session.scalars(
                select(OwnerNotificationRow).where(
                    OwnerNotificationRow.kind == kind,
                    OwnerNotificationRow.lead_id == lead_id,
                )
            ).one_or_none()
            is not None
        )

    def _insert_ignoring_conflicts(self, table, values: dict[str, str]) -> bool:
        """`INSERT ... ON CONFLICT DO NOTHING`. True when this call inserted the row.

        A `SELECT` followed by an `INSERT` is not a claim: two workers both see nothing,
        both insert, and the loser gets an IntegrityError out of the request. The database
        decides here instead, in one statement, and a lost race returns False rather than
        raising. SQLite and PostgreSQL both support the clause and both report
        `rowcount == 0` when the conflict target already existed, so the two dialects stay
        behaviour-identical.
        """
        dialect = self.session.get_bind().dialect.name
        builder = postgres_insert if dialect == "postgresql" else sqlite_insert
        statement = builder(table).values(**values).on_conflict_do_nothing()
        result = self.session.execute(statement)
        return int(result.rowcount or 0) > 0

    def upsert_owner_notification(
        self,
        *,
        kind: str,
        lead_id: str,
        scheduled_at: str,
    ) -> None:
        """Owner-inbox row, one per (kind, lead_id). Never raises on a concurrent duplicate."""
        if not kind or not lead_id or not scheduled_at:
            return
        self._insert_ignoring_conflicts(
            OwnerNotificationRow.__table__,
            {
                "kind": kind,
                "lead_id": lead_id,
                "scheduled_at": scheduled_at,
                "seen_at": "",
            },
        )

    def try_claim_owner_notification(
        self,
        *,
        kind: str,
        lead_id: str,
        conversation_id: str = "",
        claimed_at: str,
    ) -> bool:
        """Claim the right to send one owner notification. True for exactly one caller.

        Keyed on (kind, lead_id, conversation_id): a returning lead's second website
        conversation is a distinct claim, not a duplicate. Lead-scoped kinds pass an
        empty conversation_id and so claim once per lead, as before.
        """
        if not kind or not lead_id:
            return False
        return self._insert_ignoring_conflicts(
            OwnerNotificationClaimRow.__table__,
            {
                "kind": kind,
                "lead_id": lead_id,
                "conversation_id": conversation_id or "",
                "claimed_at": claimed_at,
            },
        )

    def has_owner_notification_claim(
        self, *, kind: str, lead_id: str, conversation_id: str = ""
    ) -> bool:
        if not kind or not lead_id:
            return False
        return (
            self.session.get(OwnerNotificationClaimRow, (kind, lead_id, conversation_id or ""))
            is not None
        )

    def has_owner_notification_delivery_claim(
        self, *, kind: str, lead_id: str, notification_key: str = ""
    ) -> bool:
        """Whether legacy or per-recipient state conservatively protects a delivery.

        Recipient claims are retained after Telegram acceptance or an ambiguous
        transport result and released after an explicit rejection. The legacy ledger
        has no outcome detail, so its presence remains conservative duplicate evidence.
        """
        if not kind or not lead_id or not isinstance(notification_key, str):
            return False
        if self.has_owner_notification_claim(
            kind=kind,
            lead_id=lead_id,
            conversation_id=notification_key,
        ):
            return True
        row = self.session.scalars(
            select(OwnerNotificationRecipientClaimRow)
            .where(
                OwnerNotificationRecipientClaimRow.kind == kind,
                OwnerNotificationRecipientClaimRow.lead_id == lead_id,
                OwnerNotificationRecipientClaimRow.notification_key == notification_key,
            )
            .limit(1)
        ).first()
        return row is not None

    def confirmed_owner_notification_recipients(
        self,
        *,
        kind: str,
        lead_id: str,
        notification_key: str = "",
    ) -> tuple[str, ...]:
        """Return only recipients with a durably confirmed Telegram acceptance.

        Historical recipient rows predate outcome recording and deliberately remain
        conservative: they protect against resend but never let the widget claim a
        completed transfer.
        """
        if not kind or not lead_id or not isinstance(notification_key, str):
            return ()
        rows = self.session.scalars(
            select(OwnerNotificationRecipientClaimRow.recipient_id)
            .where(
                OwnerNotificationRecipientClaimRow.kind == kind,
                OwnerNotificationRecipientClaimRow.lead_id == lead_id,
                OwnerNotificationRecipientClaimRow.notification_key == notification_key,
                OwnerNotificationRecipientClaimRow.delivery_status == "accepted",
            )
            .order_by(OwnerNotificationRecipientClaimRow.recipient_id)
        )
        return tuple(rows)

    def commit_owner_notification_delivery_state(self) -> bool:
        """Durably close local state before or after an external owner delivery.

        A website request normally commits after the FastAPI dependency yields back.
        That is too late for Telegram: an accepted request followed by a database
        commit failure would lose its recipient claim and make the next click resend.
        Callers use this boundary before the external call, and again after releasing
        explicitly rejected recipients. A failed commit is rolled back so the shared
        request session remains usable and the caller can return a truthful status.
        """
        try:
            self.session.commit()
        except Exception:  # noqa: BLE001 - transaction boundary must fail closed
            self.session.rollback()
            return False
        return True

    def try_claim_owner_notification_recipient_compatible(
        self,
        *,
        kind: str,
        compatible_kinds: tuple[str, ...],
        lead_id: str,
        recipient_id: str,
        claimed_at: str,
        notification_key: str = "",
    ) -> bool:
        """Claim a canonical recipient key while honoring historical kind aliases.

        Historical rows are stable compatibility evidence. New callers all insert the
        same canonical ``kind``, so the database primary key still decides concurrent
        hot-handoff versus WhatsApp-click races without a read-then-send window.
        """
        legacy_kinds = tuple(sorted(set(compatible_kinds) - {kind}))
        if (
            not kind
            or not lead_id
            or not isinstance(notification_key, str)
            or not recipient_id.isdecimal()
        ):
            return False
        # Insert the shared new key first. That atomic PK conflict, not the historical
        # compatibility read, decides races between new hot and click callers.
        inserted = self.try_claim_owner_notification_recipient(
            kind=kind,
            lead_id=lead_id,
            notification_key=notification_key,
            recipient_id=recipient_id,
            claimed_at=claimed_at,
        )
        if not inserted:
            return False
        if legacy_kinds:
            historical = self.session.scalars(
                select(OwnerNotificationRecipientClaimRow)
                .where(
                    OwnerNotificationRecipientClaimRow.kind.in_(legacy_kinds),
                    OwnerNotificationRecipientClaimRow.lead_id == lead_id,
                    OwnerNotificationRecipientClaimRow.notification_key == notification_key,
                    OwnerNotificationRecipientClaimRow.recipient_id == recipient_id,
                )
                .limit(1)
            ).first()
            if historical is not None:
                self.release_owner_notification_recipient_claim(
                    kind=kind,
                    lead_id=lead_id,
                    notification_key=notification_key,
                    recipient_id=recipient_id,
                )
                return False
        return True

    def release_owner_notification_recipient_claims_durably(
        self,
        *,
        kind: str,
        lead_id: str,
        recipient_ids: tuple[str, ...],
        notification_key: str = "",
    ) -> bool:
        """Persist confirmed-rejection releases with one bounded recovery attempt.

        A failed commit rolls its DELETEs back. Re-issuing the exact idempotent deletes
        once prevents a transient post-send commit failure from retaining a known
        rejected recipient forever, without risking another Telegram call here.
        """
        if not recipient_ids:
            return True
        for _attempt in range(2):
            for recipient_id in recipient_ids:
                self.release_owner_notification_recipient_claim(
                    kind=kind,
                    lead_id=lead_id,
                    notification_key=notification_key,
                    recipient_id=recipient_id,
                )
            if self.commit_owner_notification_delivery_state():
                return True
        return False

    def record_owner_notification_recipient_delivery_outcomes_durably(
        self,
        *,
        kind: str,
        lead_id: str,
        delivered_recipient_ids: tuple[str, ...],
        rejected_recipient_ids: tuple[str, ...],
        notification_key: str = "",
    ) -> bool:
        """Durably record accepted recipients and release known rejections.

        A failed commit leaves every prior claim pending, which is intentionally
        ambiguous and blocks a duplicate send. Retrying these local idempotent
        updates once cannot cause an external side effect.
        """
        for _attempt in range(2):
            for recipient_id in delivered_recipient_ids:
                row = self.session.get(
                    OwnerNotificationRecipientClaimRow,
                    (kind, lead_id, notification_key, recipient_id),
                )
                if row is not None:
                    row.delivery_status = "accepted"
            for recipient_id in rejected_recipient_ids:
                self.release_owner_notification_recipient_claim(
                    kind=kind,
                    lead_id=lead_id,
                    notification_key=notification_key,
                    recipient_id=recipient_id,
                )
            if self.commit_owner_notification_delivery_state():
                return True
        return False

    def release_owner_notification_claim(
        self, *, kind: str, lead_id: str, conversation_id: str = ""
    ) -> None:
        """Drop a claim that never produced a Telegram delivery so a later turn can retry."""
        if not kind or not lead_id:
            return
        row = self.session.get(OwnerNotificationClaimRow, (kind, lead_id, conversation_id or ""))
        if row is not None:
            self.session.delete(row)

    def try_claim_owner_notification_recipient(
        self,
        *,
        kind: str,
        lead_id: str,
        recipient_id: str,
        claimed_at: str,
        notification_key: str = "",
    ) -> bool:
        if (
            not kind
            or not lead_id
            or not isinstance(notification_key, str)
            or not recipient_id.isdecimal()
        ):
            return False
        return self._insert_ignoring_conflicts(
            OwnerNotificationRecipientClaimRow.__table__,
            {
                "kind": kind,
                "lead_id": lead_id,
                "notification_key": notification_key,
                "recipient_id": recipient_id,
                "claimed_at": claimed_at,
                # The migration defaults old/raw rows to ``legacy`` so historical
                # claims cannot manufacture acceptance. New application claims must
                # state ``pending`` explicitly; ambiguous sends retain that value.
                "delivery_status": "pending",
            },
        )

    def owner_notification_claimed_at(
        self, *, kind: str, lead_id: str, conversation_id: str = ""
    ) -> str | None:
        """Return a legacy claim timestamp without turning it into recipient history.

        The pre-recipient ledger has no recipient outcome, so callers may use its
        presence only as conservative compatibility evidence.  The timestamp is
        useful for legacy *daily* reminders, whose new key is a local day.
        """
        if not kind or not lead_id:
            return None
        row = self.session.get(OwnerNotificationClaimRow, (kind, lead_id, conversation_id or ""))
        return str(row.claimed_at) if row is not None else None

    def release_owner_notification_recipient_claim(
        self,
        *,
        kind: str,
        lead_id: str,
        recipient_id: str,
        notification_key: str = "",
    ) -> None:
        if (
            not kind
            or not lead_id
            or not isinstance(notification_key, str)
            or not recipient_id.isdecimal()
        ):
            return
        row = self.session.get(
            OwnerNotificationRecipientClaimRow,
            (kind, lead_id, notification_key, recipient_id),
        )
        if row is not None:
            self.session.delete(row)
            # A same-session retry must see this explicit rejection immediately;
            # otherwise the pending DELETE is ordered after the next conflict insert.
            self.session.flush()

    def try_insert_owner_notification(
        self,
        *,
        kind: str,
        lead_id: str,
        scheduled_at: str,
        conversation_id: str = "",
    ) -> bool:
        """Claim once per (kind, lead_id, conversation_id). False on a duplicate claim.

        The claim and the owner-inbox row are two different keys on purpose: the inbox
        keeps one unseen row per lead per kind, while the claim is what gates the send.
        Both inserts are conflict-tolerant, so a concurrent caller loses the claim and
        returns False instead of raising an IntegrityError at the caller.
        """
        if not kind or not lead_id or not scheduled_at:
            return False
        claimed = self.try_claim_owner_notification(
            kind=kind,
            lead_id=lead_id,
            conversation_id=conversation_id,
            claimed_at=scheduled_at,
        )
        if not claimed:
            return False
        self.upsert_owner_notification(kind=kind, lead_id=lead_id, scheduled_at=scheduled_at)
        return True

    def list_unseen_owner_notifications(
        self, *, kinds: tuple[str, ...], limit: int = 3
    ) -> list[OwnerNotificationRow]:
        if not kinds or limit <= 0:
            return []
        return list(
            self.session.scalars(
                select(OwnerNotificationRow)
                .where(
                    OwnerNotificationRow.kind.in_(kinds),
                    OwnerNotificationRow.seen_at == "",
                )
                .order_by(
                    OwnerNotificationRow.scheduled_at,
                    OwnerNotificationRow.id,
                )
                .limit(limit)
            ).all()
        )

    def count_unseen_owner_notifications(self, *, kinds: tuple[str, ...]) -> int:
        if not kinds:
            return 0
        count = self.session.scalar(
            select(func.count())
            .select_from(OwnerNotificationRow)
            .where(
                OwnerNotificationRow.kind.in_(kinds),
                OwnerNotificationRow.seen_at == "",
            )
        )
        return int(count or 0)

    def mark_owner_notifications_seen(self, ids: list[int], seen_at: str) -> None:
        if not ids or not seen_at:
            return
        rows = self.session.scalars(
            select(OwnerNotificationRow).where(OwnerNotificationRow.id.in_(ids))
        ).all()
        for row in rows:
            if row.seen_at == "":
                row.seen_at = seen_at
        self.session.flush()

    def upsert_owner_brief(
        self,
        *,
        brief_date: str,
        leads: int,
        meetings_offered: int,
        handoffs: int,
        messages_in: int,
        follow_ups_due: int,
        meetings_booked: int,
        cancellation_requests: int,
    ) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", brief_date):
            return
        if (
            leads < 0
            or meetings_offered < 0
            or handoffs < 0
            or messages_in < 0
            or follow_ups_due < 0
            or meetings_booked < 0
            or cancellation_requests < 0
        ):
            return
        row = self.get_owner_brief(brief_date)
        if row is None:
            self.session.add(
                OwnerBriefRow(
                    brief_date=brief_date,
                    leads=leads,
                    meetings_offered=meetings_offered,
                    handoffs=handoffs,
                    messages_in=messages_in,
                    follow_ups_due=follow_ups_due,
                    meetings_booked=meetings_booked,
                    cancellation_requests=cancellation_requests,
                )
            )
        else:
            row.leads = leads
            row.meetings_offered = meetings_offered
            row.handoffs = handoffs
            row.messages_in = messages_in
            row.follow_ups_due = follow_ups_due
            row.meetings_booked = meetings_booked
            row.cancellation_requests = cancellation_requests
        self.session.flush()

    def get_owner_weekly(self, week_start: str) -> OwnerWeeklyRow | None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", week_start):
            return None
        return self.session.scalars(
            select(OwnerWeeklyRow).where(OwnerWeeklyRow.week_start == week_start)
        ).one_or_none()

    def upsert_owner_weekly(
        self,
        *,
        week_start: str,
        leads: int,
        meetings_offered: int,
        handoffs: int,
        messages_in: int,
        follow_ups_pending: int,
        meetings_booked: int,
        cancellation_requests: int,
    ) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", week_start):
            return
        if (
            leads < 0
            or meetings_offered < 0
            or handoffs < 0
            or messages_in < 0
            or follow_ups_pending < 0
            or meetings_booked < 0
            or cancellation_requests < 0
        ):
            return
        row = self.get_owner_weekly(week_start)
        if row is None:
            self.session.add(
                OwnerWeeklyRow(
                    week_start=week_start,
                    leads=leads,
                    meetings_offered=meetings_offered,
                    handoffs=handoffs,
                    messages_in=messages_in,
                    follow_ups_pending=follow_ups_pending,
                    meetings_booked=meetings_booked,
                    cancellation_requests=cancellation_requests,
                )
            )
        else:
            row.leads = leads
            row.meetings_offered = meetings_offered
            row.handoffs = handoffs
            row.messages_in = messages_in
            row.follow_ups_pending = follow_ups_pending
            row.meetings_booked = meetings_booked
            row.cancellation_requests = cancellation_requests
        self.session.flush()

    def list_conversation_turns(
        self, conversation_id: str, *, limit: int = MAX_TURNS
    ) -> list[ConversationTurn]:
        """Oldest-first message history for one conversation. Text only."""
        if not conversation_id or limit <= 0:
            return []
        rows = self.session.scalars(
            select(CanonicalEventRow)
            .where(
                CanonicalEventRow.conversation_id == conversation_id,
                CanonicalEventRow.event_type.in_(("message_in", "message_out")),
            )
            .order_by(CanonicalEventRow.occurred_at.desc(), CanonicalEventRow.id.desc())
            .limit(min(limit, MAX_TURNS))
        ).all()
        turns: list[ConversationTurn] = []
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            text_value = payload.get("text")
            if not isinstance(text_value, str):
                continue
            clipped = clip_turn_text(text_value)
            if not clipped:
                continue
            turns.append(
                ConversationTurn(
                    role=normalize_turn_role(row.actor_role, row.event_type),
                    text=clipped,
                )
            )
        turns.reverse()
        return turns

    def latest_behavior_payload(self, conversation_id: str, kind: str) -> dict[str, str] | None:
        rows = self.session.scalars(
            select(CanonicalEventRow)
            .where(
                CanonicalEventRow.conversation_id == conversation_id,
                CanonicalEventRow.event_type == "behavior",
            )
            .order_by(CanonicalEventRow.occurred_at.desc())
        ).all()
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict) or payload.get("kind") != kind:
                continue
            return {
                key: value
                for key, value in payload.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        return None

    def get_lead_customer_id(self, lead_id: str) -> str | None:
        row = self.session.get(LeadRow, lead_id)
        if row is not None:
            return row.customer_id
        return self._website_customer_id(lead_id)

    def _website_customer_id(self, session_id: str) -> str | None:
        if not session_id:
            return None
        identity = self.session.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.channel == Channel.WEBSITE.value,
                ChannelIdentityRow.external_id == session_id,
            )
        ).one_or_none()
        return identity.customer_id if identity is not None else None

    def _customer_id_for_handoff(self, lead_id: str, website_session_id: str) -> str | None:
        lead = self.session.get(LeadRow, lead_id)
        if lead is not None:
            return lead.customer_id
        return self._website_customer_id(website_session_id or lead_id)

    def get_channel_identity(self, *, channel: str, external_id: str) -> ChannelIdentityRow | None:
        return self.session.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.channel == channel,
                ChannelIdentityRow.external_id == external_id,
            )
        ).one_or_none()

    def save_identity_link(self, *, identity_id: int, customer_id: str, reason: str) -> bool:
        if reason not in ALLOWLISTED_LINK_REASONS:
            return False
        identity = self.session.get(ChannelIdentityRow, identity_id)
        if identity is None or identity.customer_id != customer_id:
            return False
        existing = self.session.scalars(
            select(IdentityLinkRow).where(IdentityLinkRow.identity_id == identity_id)
        ).one_or_none()
        if existing is not None:
            return False
        self.session.add(
            IdentityLinkRow(
                identity_id=identity_id,
                customer_id=customer_id,
                reason=reason,
                reversed_at=None,
            )
        )
        self.session.flush()
        return True

    def get_identity_link(self, identity_id: int) -> IdentityLinkRow | None:
        return self.session.scalars(
            select(IdentityLinkRow).where(IdentityLinkRow.identity_id == identity_id)
        ).one_or_none()

    def get_website_lead_id(self, session_id: str) -> str | None:
        identity = self.session.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.channel == Channel.WEBSITE.value,
                ChannelIdentityRow.external_id == session_id,
            )
        ).one_or_none()
        if identity is None:
            return None
        lead = self.session.scalars(
            select(LeadRow).where(LeadRow.customer_id == identity.customer_id).order_by(LeadRow.id)
        ).first()
        return lead.id if lead is not None else None

    def has_website_prospect_message(self, lead_id: str, conversation_id: str) -> bool:
        """Whether this exact website conversation, not merely its lead, has started."""
        if not lead_id or not conversation_id:
            return False
        row = self.session.scalars(
            select(CanonicalEventRow)
            .where(
                CanonicalEventRow.lead_id == lead_id,
                CanonicalEventRow.conversation_id == conversation_id,
                CanonicalEventRow.provider == Channel.WEBSITE.value,
                CanonicalEventRow.event_type == EventType.MESSAGE_IN.value,
                CanonicalEventRow.actor_role == "prospect",
            )
            .limit(1)
        ).first()
        return row is not None

    def list_inactive_website_conversations(
        self,
        *,
        cutoff_iso: str,
        skip_kinds: tuple[str, ...] = (),
        skip_conversation_kinds: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[tuple[str, str]]:
        """Website session_id + lead_id whose last visitor message is at or before cutoff.

        `skip_kinds` drops every session of a lead that has retained delivery claim state
        for that kind. It deliberately does not use the owner inbox row: a known failed
        WhatsApp-click Telegram attempt remains eligible for later finalization.

        `skip_conversation_kinds` drops only the sessions that were already claimed, so a
        returning lead's next conversation is still scanned. Using the lead-scoped skip for
        the finalization kind is what silently retired returning leads.
        """
        if not cutoff_iso or limit <= 0:
            return []
        last_in = (
            select(
                CanonicalEventRow.lead_id,
                CanonicalEventRow.conversation_id,
                func.max(CanonicalEventRow.occurred_at).label("last_at"),
            )
            .where(
                CanonicalEventRow.provider == Channel.WEBSITE.value,
                CanonicalEventRow.event_type == EventType.MESSAGE_IN.value,
                CanonicalEventRow.actor_role == "prospect",
            )
            .group_by(CanonicalEventRow.lead_id, CanonicalEventRow.conversation_id)
            .subquery()
        )
        query = (
            select(ChannelIdentityRow.external_id, LeadRow.id)
            .join(LeadRow, LeadRow.customer_id == ChannelIdentityRow.customer_id)
            .join(last_in, last_in.c.lead_id == LeadRow.id)
            .where(
                ChannelIdentityRow.channel == Channel.WEBSITE.value,
                last_in.c.conversation_id == ChannelIdentityRow.external_id,
                last_in.c.last_at <= cutoff_iso,
            )
            .limit(limit)
        )
        if skip_kinds:
            legacy_claimed = select(OwnerNotificationClaimRow.lead_id).where(
                OwnerNotificationClaimRow.kind.in_(skip_kinds),
                OwnerNotificationClaimRow.conversation_id == "",
            )
            recipient_claimed = select(OwnerNotificationRecipientClaimRow.lead_id).where(
                OwnerNotificationRecipientClaimRow.kind.in_(skip_kinds),
                OwnerNotificationRecipientClaimRow.notification_key == "",
            )
            query = query.where(
                LeadRow.id.notin_(legacy_claimed),
                LeadRow.id.notin_(recipient_claimed),
            )
        if skip_conversation_kinds:
            claimed = (
                select(OwnerNotificationClaimRow.kind)
                .where(
                    OwnerNotificationClaimRow.kind.in_(skip_conversation_kinds),
                    OwnerNotificationClaimRow.lead_id == LeadRow.id,
                    OwnerNotificationClaimRow.conversation_id == ChannelIdentityRow.external_id,
                )
                .exists()
            )
            query = query.where(~claimed)
        rows = self.session.execute(query)
        return [(str(session_id), str(lead_id)) for session_id, lead_id in rows]

    def issue_handoff_token(self, lead_id: str, website_session_id: str) -> tuple[str, str]:
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=HANDOFF_TTL_MINUTES)
        expires_at_iso = expires_at.isoformat()
        for row in self.session.scalars(
            select(HandoffTokenRow).where(
                HandoffTokenRow.lead_id == lead_id,
                HandoffTokenRow.consumed_at.is_(None),
            )
        ).all():
            row.expires_at = now.isoformat()
        raw_token = generate_handoff_token()
        self.session.add(
            HandoffTokenRow(
                token_hash=hash_handoff_token(raw_token),
                lead_id=lead_id,
                website_session_id=website_session_id,
                expires_at=expires_at_iso,
                consumed_at=None,
            )
        )
        self.session.flush()
        return raw_token, expires_at_iso

    def consume_handoff_token(self, raw_token: str, *, whatsapp_external_id: str) -> str | None:
        row = self.session.scalars(
            select(HandoffTokenRow).where(
                HandoffTokenRow.token_hash == hash_handoff_token(raw_token)
            )
        ).one_or_none()
        if row is None or row.consumed_at is not None:
            return None
        expires_at = datetime.fromisoformat(row.expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) >= expires_at:
            return None
        customer_id = self._customer_id_for_handoff(row.lead_id, row.website_session_id)
        if customer_id is None:
            return None
        wa_identity = self.session.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.channel == Channel.WHATSAPP.value,
                ChannelIdentityRow.external_id == whatsapp_external_id,
            )
        ).one_or_none()
        if wa_identity is not None and wa_identity.customer_id != customer_id:
            return None
        if wa_identity is None:
            self.session.add(
                ChannelIdentityRow(
                    customer_id=customer_id,
                    channel=Channel.WHATSAPP.value,
                    external_id=whatsapp_external_id,
                    verified=True,
                )
            )
        row.consumed_at = datetime.now(UTC).isoformat()
        self.session.flush()
        return row.lead_id

    def aggregate_ai_runs(self, *, occurred_from: str, occurred_to: str) -> AiRunAggregate:
        """Aggregate persisted `AiRunRow`s inside one day window.

        The window is real now. It used to be accepted and ignored, because the table
        had no timestamp, so every "today" number on the owner brief was an all-time
        total. Rows written before `occurred_at` existed carry an empty string and so
        fall outside every window -- correct, since nobody knows when they ran.

        Percentiles are computed in Python over the fetched rows so behavior is
        identical on SQLite and Postgres (no database-specific percentile function).
        """
        rows = self.session.execute(
            select(
                AiRunRow.model,
                AiRunRow.latency_ms,
                AiRunRow.tokens_in,
                AiRunRow.tokens_out,
                AiRunRow.cost_usd,
            ).where(
                AiRunRow.occurred_at >= occurred_from,
                AiRunRow.occurred_at < occurred_to,
            )
        ).all()
        total_runs = len(rows)
        canned_runs = sum(1 for row in rows if row.model == MODEL_CANNED)
        latencies = sorted(int(row.latency_ms) for row in rows)
        tokens_in = sum(int(row.tokens_in) for row in rows)
        tokens_out = sum(int(row.tokens_out) for row in rows)
        cost_usd = sum(int(row.cost_usd) for row in rows)
        return AiRunAggregate(
            total_runs=total_runs,
            canned_runs=canned_runs,
            median_latency_ms=_latency_percentile(latencies, 50),
            p95_latency_ms=_latency_percentile(latencies, 95),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )


def _latency_percentile(sorted_values: list[int], pct: int) -> int:
    """Nearest-rank-with-interpolation percentile. Defensive: never raises on an
    empty list. Pure Python so it behaves identically on SQLite and Postgres.
    """
    if not sorted_values:
        return 0
    n = len(sorted_values)
    if n == 1:
        return int(sorted_values[0])
    rank = (n - 1) * (pct / 100)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    if lower == upper:
        return int(sorted_values[lower])
    weight = rank - lower
    interpolated = sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight
    return int(round(interpolated))
