from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from app.brain.store import BrainStore
from app.core.config import get_settings
from app.core.demo import demo_mode_active
from app.core.logging import log_comm
from app.core.outbound import send_inbound_reply
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms, persist_ai_run
from app.domain.approvals import (
    ack_for_approval_result,
    apply_approval_policy,
    apply_owner_approval_decision,
)
from app.domain.attribution import INSTAGRAM_ATTRIBUTION_KEYS
from app.domain.briefs import apply_meeting_brief_policy, apply_owner_meeting_brief
from app.domain.calendar_booking import resolve_meeting_reply
from app.domain.commitments import (
    TRIGGER_SPEND_THRESHOLD,
    parse_due_at,
    plan_owner_commitment,
)
from app.domain.content_ideas import apply_owner_content_ideas
from app.domain.conversation_kill import apply_conversation_kill_policy
from app.domain.conversation_scope import (
    AutomationScope,
    apply_owner_scope_mark,
    existing_whatsapp_scope,
    prepare_whatsapp_inbound,
    prepend_mia_intro,
)
from app.domain.deals import apply_deal_policy
from app.domain.debriefs import (
    ack_for_debrief_result,
    apply_owner_meeting_debrief,
)
from app.domain.events import (
    Channel,
    build_attribution_event,
    build_message_in_event,
    build_message_out_event,
    new_correlation_id,
    persist_tool_outcome,
    sheets_mirror_outcome,
    stamp_correlation,
    transcription_outcome,
    webhook_envelope_kind,
)
from app.domain.feedback import persist_owner_correction
from app.domain.followups import apply_follow_up_policy
from app.domain.gmail_summaries import apply_owner_gmail_summary
from app.domain.handoff import inbound_text_without_token
from app.domain.hot_handoff import apply_hot_handoff, format_hot_leads_ack
from app.domain.identity import REASON_HANDOFF_TOKEN, persist_verified_identity_link
from app.domain.lead_reviews import apply_owner_lead_review
from app.domain.learning import (
    InstructionKind,
    classify_instruction_kind,
    propose_owner_instruction,
)
from app.domain.meetings import apply_meeting_policy
from app.domain.owner_brain import answer_owner, learn_from_exchange
from app.domain.owner_briefs import apply_owner_brief
from app.domain.owner_calendar import apply_owner_calendar
from app.domain.owner_followups import needs_data_anchor, routed_owner_text
from app.domain.owner_notify import apply_owner_notify
from app.domain.owner_reads import (
    format_pending_approvals_ack,
    format_website_conversations_ack,
    top_website_lead_id,
)
from app.domain.owner_snapshot import format_operator_snapshot_ack
from app.domain.owner_status import format_owner_status_ack
from app.domain.owner_tasks import (
    OwnerTaskType,
    ack_for_owner_task,
    classify_owner_task,
    promote_unclassified_text_to_status,
)
from app.domain.owner_weeklies import apply_owner_weekly
from app.domain.ownership_freshness import (
    VALID_INSTAGRAM_SENDERS,
    conversation_ownership_outcome,
    owner_permissions_outcome,
)
from app.domain.prelaunch import (
    apply_prelaunch_policy,
    evaluate_prelaunch,
    format_prelaunch_line,
    should_run_prelaunch,
)
from app.domain.sales import NextAction
from app.domain.seo import enrich_seo_ack
from app.domain.shadow import persist_shadow_decision, should_skip_prospect_send
from app.domain.takeover import apply_owner_human_resume, apply_owner_human_takeover
from app.domain.tools import ToolOutcome
from app.graph.orchestrator import build_graph
from app.graph.state import empty_state
from app.integrations.base import MessagePort, OutboundMessage
from app.integrations.calendar import CalendarPort, build_calendar_port
from app.integrations.calendar_booking import CalendarBookingPort, build_calendar_booking_port
from app.integrations.ga4 import Ga4Port, build_ga4_port
from app.integrations.instagram_insights import (
    InstagramInsightsPort,
    build_instagram_insights_port,
    enrich_content_insights_ack,
)
from app.integrations.linkedin import LinkedInPort, build_linkedin_port, enrich_linkedin_ack
from app.integrations.linkedin_analytics import (
    LinkedInAnalyticsPort,
    build_linkedin_analytics_port,
    enrich_linkedin_analytics_ack,
)
from app.integrations.meta_ads import MetaAdsPort, build_meta_ads_port, enrich_analytics_ack
from app.integrations.owner_reply import OwnerReplyPort, build_owner_reply_port
from app.integrations.research import ResearchPort, build_research_port, enrich_research_ack
from app.integrations.sales_reply import build_sales_reply_port
from app.integrations.search_console import SearchConsolePort, build_search_console_port
from app.integrations.seo_audit import SeoAuditPort, build_seo_audit_port
from app.integrations.sheets import (
    DealMirrorRow,
    FollowUpMirrorRow,
    LeadMirrorRow,
    MeetingMirrorRow,
    SheetsPort,
    activity_mirror_row_from_persisted,
    build_sheets_port,
    claim_sheets_mirror,
    complete_sheets_mirror,
    maybe_mirror_weekly_kpi,
    mirror_activity,
    mirror_deal,
    mirror_follow_up,
    mirror_lead,
    mirror_meeting,
)
from app.integrations.thread_summary import build_thread_summary_port

_MAX_STT_DURATION_MS = 86_400_000


def _clamp_ms_field(item: dict[str, str], key: str) -> int:
    raw = item.get(key, "0")
    if not raw:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    if value > _MAX_STT_DURATION_MS:
        return _MAX_STT_DURATION_MS
    return value


def _stt_latency_ms(item: dict[str, str]) -> int:
    return _clamp_ms_field(item, "stt_latency_ms")


def _transcript_duration_ms(item: dict[str, str]) -> int:
    return _clamp_ms_field(item, "duration_ms")


def _event_conversation_id(item: dict[str, str]) -> str:
    return item.get("thread_id") or item.get("chat_id") or item["from"]


def _outbound_reply(item: dict[str, str], *, text: str, channel: Channel) -> OutboundMessage:
    if channel is Channel.TELEGRAM:
        reply_to = item.get("message_id") or ""
    else:
        reply_to = item["id"]
    return OutboundMessage(
        conversation_id=item.get("chat_id") or item["from"],
        text=text,
        channel=channel.value,
        idempotency_key=item["id"],
        reply_to_id=reply_to,
    )


def _instagram_attribution_from_item(item: dict[str, str]) -> dict[str, str]:
    return {
        key: item[key]
        for key in INSTAGRAM_ATTRIBUTION_KEYS
        if key in item and item[key]
    }


def _persist_instagram_attribution(
    *,
    store: LeadStore,
    provider: str,
    channel: Channel,
    lead_id: str,
    item: dict[str, str],
) -> None:
    attribution = _instagram_attribution_from_item(item)
    if not attribution:
        return
    store.save_canonical_event(
        provider=provider,
        event=build_attribution_event(
            provider=provider,
            channel=channel,
            lead_id=lead_id,
            conversation_id=_event_conversation_id(item),
            payload=attribution,
        ),
    )


def _whatsapp_automation_scope(store: LeadStore, item: dict[str, str]) -> str:
    control = store.get_conversation_control(
        Channel.WHATSAPP.value, item.get("from", "")
    )
    if control is None:
        return ""
    return control.automation_scope or ""


async def process_inbound_texts(
    *,
    provider: str,
    channel: Channel,
    items: list[dict[str, str]],
    store: LeadStore,
    port: MessagePort,
    kill_switch: bool,
    owner_ids: set[str] | None = None,
    calendar: CalendarPort | None = None,
    calendar_booking: CalendarBookingPort | None = None,
    sheets: SheetsPort | None = None,
    meta_ads: MetaAdsPort | None = None,
    instagram_insights: InstagramInsightsPort | None = None,
    research: ResearchPort | None = None,
    linkedin: LinkedInPort | None = None,
    linkedin_analytics: LinkedInAnalyticsPort | None = None,
    search_console: SearchConsolePort | None = None,
    ga4: Ga4Port | None = None,
    seo_audit: SeoAuditPort | None = None,
    owner_reply: OwnerReplyPort | None = None,
) -> dict[str, int | bool | str | None]:
    processed = 0
    duplicates = 0
    sent_count = 0
    last_reply: str | None = None
    channel_value = channel.value
    owner_ids = owner_ids or set()
    settings = get_settings()
    calendar_port = calendar if calendar is not None else build_calendar_port(settings)
    calendar_booking_port = (
        calendar_booking
        if calendar_booking is not None
        else build_calendar_booking_port(settings)
    )
    sheets_port = sheets if sheets is not None else build_sheets_port(settings)
    meta_ads_port = meta_ads if meta_ads is not None else build_meta_ads_port(settings)
    instagram_insights_port = (
        instagram_insights
        if instagram_insights is not None
        else build_instagram_insights_port(settings)
    )
    research_port = research if research is not None else build_research_port(settings)
    linkedin_port = linkedin if linkedin is not None else build_linkedin_port(settings)
    linkedin_analytics_port = (
        linkedin_analytics
        if linkedin_analytics is not None
        else build_linkedin_analytics_port(settings)
    )
    search_console_port = (
        search_console if search_console is not None else build_search_console_port(settings)
    )
    ga4_port = ga4 if ga4 is not None else build_ga4_port(settings)
    seo_audit_port = seo_audit if seo_audit is not None else build_seo_audit_port(settings)
    owner_reply_port = (
        owner_reply if owner_reply is not None else build_owner_reply_port(settings)
    )
    for item in items:
        if not item["id"] or not item["from"]:
            continue
        if not store.claim_webhook(
            provider=provider,
            provider_event_id=item["id"],
            channel=channel.value,
            envelope_kind=webhook_envelope_kind(item),
        ):
            duplicates += 1
            continue
        if item["from"] in owner_ids:
            owner_text = inbound_text_without_token(item["text"])
            correlation_id = new_correlation_id()
            if owner_ids:
                persist_tool_outcome(
                    store,
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=f"owner:{item['from']}",
                    conversation_id=_event_conversation_id(item),
                    lead_id=None,
                    outcome=owner_permissions_outcome(
                        present=True,
                        now=datetime.now(UTC),
                    ),
                    correlation_id=correlation_id,
                )
            owner_message_in = build_message_in_event(
                provider=provider,
                channel=channel,
                provider_event_id=item["id"],
                conversation_id=_event_conversation_id(item),
                text=owner_text,
                actor_role="owner",
                lead_id=None,
            )
            stamp_correlation(owner_message_in, correlation_id)
            store.save_canonical_event(
                provider=provider,
                event=owner_message_in,
            )
            if item.get("source") == "audio":
                store.save_transcript(
                    provider=provider,
                    provider_event_id=item["id"],
                    channel=channel_value,
                    external_id=item["from"],
                    actor_role="owner",
                    transcript=owner_text,
                    stt_provider=item.get("stt_provider", ""),
                    stt_model=item.get("stt_model", ""),
                    language=item.get("language", ""),
                    duration_ms=_transcript_duration_ms(item),
                    confidence=item.get("confidence", ""),
                )
                if owner_text.strip():
                    persist_tool_outcome(
                        store,
                        provider=provider,
                        channel=channel,
                        inbound_provider_event_id=item["id"],
                        conversation_id=_event_conversation_id(item),
                        lead_id=None,
                        outcome=transcription_outcome(
                            transcribed=True,
                            latency_ms=_stt_latency_ms(item),
                        ),
                        correlation_id=correlation_id,
                    )
            # Routing sees follow-up references expanded against Mia's own previous
            # replies. The stored transcript above keeps what Assaf actually wrote.
            owner_history = store.list_conversation_turns(_event_conversation_id(item))
            routed_text = routed_owner_text(
                owner_text,
                history=owner_history,
                fallback_lead_id=(
                    top_website_lead_id(store)
                    if needs_data_anchor(owner_text)
                    else None
                ),
            )
            decision = promote_unclassified_text_to_status(
                classify_owner_task(routed_text),
                inbound_source=item.get("source"),
                text=routed_text,
            )
            due_at: str | None = None
            if (
                not decision.needs_clarification
                and decision.task_type
                not in (
                    OwnerTaskType.PREFERENCE,
                    OwnerTaskType.APPROVAL,
                    OwnerTaskType.DAILY_BRIEF,
                    OwnerTaskType.WEEKLY_BRIEF,
                    OwnerTaskType.LEAD_REVIEW,
                    OwnerTaskType.CONTENT_IDEA,
                    OwnerTaskType.GMAIL_SUMMARY,
                    OwnerTaskType.SEO,
                    OwnerTaskType.CALENDAR,
                    OwnerTaskType.OWNER_NOTIFY,
                    OwnerTaskType.MEETING_BRIEF,
                    OwnerTaskType.HUMAN_TAKEOVER,
                    OwnerTaskType.HUMAN_TAKEOVER_RESUME,
                    OwnerTaskType.CONVERSATION_SCOPE,
                    OwnerTaskType.HOT_LEADS,
                    OwnerTaskType.PENDING_APPROVALS,
                    OwnerTaskType.WEBSITE_CONVERSATIONS,
                    OwnerTaskType.OWNER_STATUS,
                    OwnerTaskType.OPERATOR_SNAPSHOT,
                )
            ):
                due_at = parse_due_at(
                    owner_text,
                    now=datetime.now(UTC),
                    timezone=settings.calendar_timezone,
                )
            plan = plan_owner_commitment(
                decision=decision, text=owner_text, due_at=due_at
            )
            persist_due_at = due_at
            if plan.trigger == TRIGGER_SPEND_THRESHOLD:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.DAILY_BRIEF:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.WEEKLY_BRIEF:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.LEAD_REVIEW:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.CONTENT_IDEA:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.GMAIL_SUMMARY:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.SEO:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.CALENDAR:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.OWNER_NOTIFY:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.MEETING_BRIEF:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.HUMAN_TAKEOVER:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.HUMAN_TAKEOVER_RESUME:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.CONVERSATION_SCOPE:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.HOT_LEADS:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.OWNER_STATUS:
                persist_due_at = None
            if decision.task_type == OwnerTaskType.OPERATOR_SNAPSHOT:
                persist_due_at = None
            owner_task_claim_key = f"{provider}:{item['id']}"
            if store.claim_operation(scope="owner_task", key=owner_task_claim_key):
                store.save_owner_task(
                    provider=provider,
                    provider_event_id=item["id"],
                    channel=channel_value,
                    external_id=item["from"],
                    task_type=decision.task_type.value,
                    status="needs_clarification" if decision.needs_clarification else "logged",
                    due_at=persist_due_at,
                    trigger=plan.trigger,
                    condition=plan.condition,
                    action=plan.action,
                )
                store.complete_operation(
                    scope="owner_task",
                    key=owner_task_claim_key,
                    result_json='{"ok": true}',
                )
            if (
                decision.task_type == OwnerTaskType.PREFERENCE
                and not decision.needs_clarification
            ):
                instruction_kind = classify_instruction_kind(owner_text)
                propose_owner_instruction(
                    store=store,
                    provider=provider,
                    provider_event_id=item["id"],
                    body=owner_text,
                    kill_switch=kill_switch,
                    kind=instruction_kind,
                )
                if instruction_kind == InstructionKind.CORRECTION:
                    persist_owner_correction(
                        store=store,
                        provider=provider,
                        provider_event_id=item["id"],
                        body=owner_text,
                        kill_switch=kill_switch,
                    )
            ack_text = ack_for_owner_task(
                decision,
                due_at=persist_due_at,
                condition=plan.condition,
                trigger=plan.trigger,
                # An outreach confirmation has to name the lead it is about, and the
                # lead only exists in the routed text when Assaf used a pronoun.
                text=routed_text
                if decision.task_type == OwnerTaskType.LEAD_OUTREACH
                else owner_text
                if (
                    decision.task_type == OwnerTaskType.PREFERENCE
                    and not decision.needs_clarification
                )
                or (
                    decision.task_type == OwnerTaskType.APPROVAL
                    and decision.needs_clarification
                )
                else None,
                inbound_source=item.get("source"),
            )
            if (
                decision.task_type == OwnerTaskType.APPROVAL
                and not decision.needs_clarification
            ):
                result = apply_owner_approval_decision(
                    store,
                    text=owner_text,
                    channel=channel,
                    kill_switch=kill_switch,
                )
                if result.status != "skipped":
                    ack_text = ack_for_approval_result(result)
            if (
                decision.task_type == OwnerTaskType.MEETING_DEBRIEF
                and not decision.needs_clarification
            ):
                result = apply_owner_meeting_debrief(
                    store,
                    text=routed_text,
                    channel=channel,
                    kill_switch=kill_switch,
                )
                debrief_ack = ack_for_debrief_result(result)
                if debrief_ack is not None:
                    ack_text = debrief_ack
            if (
                decision.task_type == OwnerTaskType.LEAD_REVIEW
                and not decision.needs_clarification
            ):
                review_ack = apply_owner_lead_review(
                    store,
                    text=routed_text,
                    kill_switch=kill_switch,
                    demo_active=demo_mode_active(settings),
                )
                if review_ack is not None:
                    ack_text = review_ack
            if (
                decision.task_type == OwnerTaskType.DAILY_BRIEF
                and not decision.needs_clarification
            ):
                brief_ack = apply_owner_brief(
                    store,
                    timezone=settings.calendar_timezone,
                    kill_switch=kill_switch,
                    demo_active=demo_mode_active(settings),
                )
                if brief_ack is not None:
                    ack_text = brief_ack
            if (
                decision.task_type == OwnerTaskType.WEEKLY_BRIEF
                and not decision.needs_clarification
            ):
                weekly_ack = apply_owner_weekly(
                    store,
                    timezone=settings.calendar_timezone,
                    kill_switch=kill_switch,
                    demo_active=demo_mode_active(settings),
                )
                if weekly_ack is not None:
                    ack_text = weekly_ack
            if (
                decision.task_type == OwnerTaskType.CONTENT_IDEA
                and not decision.needs_clarification
            ):
                ideas_ack = apply_owner_content_ideas(
                    store,
                    timezone=settings.calendar_timezone,
                    kill_switch=kill_switch,
                    demo_active=demo_mode_active(settings),
                )
                if ideas_ack is not None:
                    ack_text = ideas_ack
            if (
                decision.task_type == OwnerTaskType.GMAIL_SUMMARY
                and not decision.needs_clarification
            ):
                gmail_ack = apply_owner_gmail_summary(
                    store,
                    text=routed_text,
                    kill_switch=kill_switch,
                    demo_active=demo_mode_active(settings),
                    port=build_thread_summary_port(settings),
                )
                if gmail_ack is not None:
                    ack_text = gmail_ack
            if (
                decision.task_type == OwnerTaskType.CALENDAR
                and not decision.needs_clarification
            ):
                ack_text, calendar_outcome = apply_owner_calendar(
                    ack_text,
                    calendar_port,
                    kill_switch=kill_switch,
                    timezone=settings.calendar_timezone,
                    demo_active=demo_mode_active(settings),
                )
                if calendar_outcome is not None:
                    persist_tool_outcome(
                        store,
                        provider=provider,
                        channel=channel,
                        inbound_provider_event_id=item["id"],
                        conversation_id=_event_conversation_id(item),
                        lead_id=None,
                        outcome=calendar_outcome,
                        correlation_id=correlation_id,
                    )
            if (
                decision.task_type == OwnerTaskType.OWNER_NOTIFY
                and not decision.needs_clarification
            ):
                notify_ack = apply_owner_notify(
                    store,
                    timezone=settings.calendar_timezone,
                    kill_switch=kill_switch,
                    demo_active=demo_mode_active(settings),
                )
                if notify_ack is not None:
                    ack_text = notify_ack
            if (
                decision.task_type == OwnerTaskType.MEETING_BRIEF
                and not decision.needs_clarification
            ):
                brief_ack = apply_owner_meeting_brief(
                    store,
                    text=routed_text,
                    timezone=settings.calendar_timezone,
                    kill_switch=kill_switch,
                    demo_active=demo_mode_active(settings),
                )
                if brief_ack is not None:
                    ack_text = brief_ack
            if (
                decision.task_type == OwnerTaskType.HUMAN_TAKEOVER
                and not decision.needs_clarification
                and not demo_mode_active(settings)
            ):
                takeover_ack = apply_owner_human_takeover(
                    store,
                    text=routed_text,
                    kill_switch=kill_switch,
                )
                if takeover_ack is not None:
                    ack_text = takeover_ack
            if (
                decision.task_type == OwnerTaskType.HUMAN_TAKEOVER_RESUME
                and not decision.needs_clarification
                and not demo_mode_active(settings)
            ):
                resume_ack = apply_owner_human_resume(
                    store,
                    text=routed_text,
                    kill_switch=kill_switch,
                )
                if resume_ack is not None:
                    ack_text = resume_ack
            if (
                decision.task_type == OwnerTaskType.CONVERSATION_SCOPE
                and not decision.needs_clarification
                and not demo_mode_active(settings)
            ):
                scope_ack = apply_owner_scope_mark(
                    store,
                    text=owner_text,
                    kill_switch=kill_switch,
                )
                if scope_ack is not None:
                    ack_text = scope_ack
            if (
                decision.task_type == OwnerTaskType.HOT_LEADS
                and not decision.needs_clarification
            ):
                ack_text = format_hot_leads_ack(store)
            if (
                decision.task_type == OwnerTaskType.PENDING_APPROVALS
                and not decision.needs_clarification
            ):
                ack_text = format_pending_approvals_ack(store)
            if (
                decision.task_type == OwnerTaskType.WEBSITE_CONVERSATIONS
                and not decision.needs_clarification
            ):
                ack_text = format_website_conversations_ack(store)
            if (
                decision.task_type == OwnerTaskType.OWNER_STATUS
                and not decision.needs_clarification
            ):
                ack_text = format_owner_status_ack(
                    store,
                    timezone=settings.calendar_timezone,
                )
            if (
                decision.task_type == OwnerTaskType.OPERATOR_SNAPSHOT
                and not decision.needs_clarification
            ):
                ack_text = format_operator_snapshot_ack(
                    store,
                    timezone=settings.calendar_timezone,
                    matched_types=decision.matched_types,
                )
            if (
                decision.task_type == OwnerTaskType.ANALYTICS
                and not decision.needs_clarification
                and plan.trigger != TRIGGER_SPEND_THRESHOLD
            ):
                analytics_extras: list[ToolOutcome] = []
                ack_text, analytics_outcome = enrich_analytics_ack(
                    ack_text,
                    meta_ads_port,
                    kill_switch,
                    store=store,
                    settings=settings,
                    sheets=sheets_port,
                    extra_outcomes=analytics_extras,
                    inbound_id=item["id"],
                )
                persist_tool_outcome(
                    store,
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=item["id"],
                    conversation_id=_event_conversation_id(item),
                    lead_id=None,
                    outcome=analytics_outcome,
                    correlation_id=correlation_id,
                )
                for extra_outcome in analytics_extras:
                    persist_tool_outcome(
                        store,
                        provider=provider,
                        channel=channel,
                        inbound_provider_event_id=item["id"],
                        conversation_id=_event_conversation_id(item),
                        lead_id=None,
                        outcome=extra_outcome,
                        correlation_id=correlation_id,
                    )
                content_extras: list[ToolOutcome] = []
                ack_text, insights_outcome = enrich_content_insights_ack(
                    ack_text,
                    instagram_insights_port,
                    store,
                    kill_switch,
                    sheets=sheets_port,
                    settings=settings,
                    extra_outcomes=content_extras,
                    inbound_id=item["id"],
                )
                persist_tool_outcome(
                    store,
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=item["id"],
                    conversation_id=_event_conversation_id(item),
                    lead_id=None,
                    outcome=insights_outcome,
                    correlation_id=correlation_id,
                )
                for extra_outcome in content_extras:
                    persist_tool_outcome(
                        store,
                        provider=provider,
                        channel=channel,
                        inbound_provider_event_id=item["id"],
                        conversation_id=_event_conversation_id(item),
                        lead_id=None,
                        outcome=extra_outcome,
                        correlation_id=correlation_id,
                    )
                if should_run_prelaunch(settings) and not demo_mode_active(settings):
                    prelaunch = evaluate_prelaunch(settings)
                    apply_prelaunch_policy(
                        store,
                        snapshot=prelaunch,
                        kill_switch=kill_switch,
                        demo_active=False,
                    )
                    ack_text = f"{ack_text}\n{format_prelaunch_line(prelaunch)}"
            if (
                decision.task_type == OwnerTaskType.RESEARCH
                and not decision.needs_clarification
            ):
                ack_text, research_outcome = enrich_research_ack(
                    ack_text,
                    research_port,
                    query=item["text"],
                    kill_switch=kill_switch,
                )
                persist_tool_outcome(
                    store,
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=item["id"],
                    conversation_id=_event_conversation_id(item),
                    lead_id=None,
                    outcome=research_outcome,
                    correlation_id=correlation_id,
                )
            if (
                decision.task_type == OwnerTaskType.SEO
                and not decision.needs_clarification
            ):
                ack_text, seo_outcomes = enrich_seo_ack(
                    ack_text,
                    search_console_port,
                    ga4_port,
                    seo_audit_port,
                    kill_switch=kill_switch,
                    store=store,
                    settings=settings,
                    demo_active=demo_mode_active(settings),
                )
                for seo_outcome in seo_outcomes:
                    persist_tool_outcome(
                        store,
                        provider=provider,
                        channel=channel,
                        inbound_provider_event_id=item["id"],
                        conversation_id=_event_conversation_id(item),
                        lead_id=None,
                        outcome=seo_outcome,
                        correlation_id=correlation_id,
                    )
            if (
                decision.task_type == OwnerTaskType.LINKEDIN
                and not decision.needs_clarification
            ):
                ack_text, linkedin_outcome = enrich_linkedin_ack(
                    ack_text,
                    linkedin_port,
                    kill_switch,
                )
                persist_tool_outcome(
                    store,
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=item["id"],
                    conversation_id=_event_conversation_id(item),
                    lead_id=None,
                    outcome=linkedin_outcome,
                    correlation_id=correlation_id,
                )
                ack_text, analytics_outcome = enrich_linkedin_analytics_ack(
                    ack_text,
                    linkedin_analytics_port,
                    kill_switch,
                    now=datetime.now(UTC),
                    timezone=settings.calendar_timezone,
                )
                persist_tool_outcome(
                    store,
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=item["id"],
                    conversation_id=_event_conversation_id(item),
                    lead_id=None,
                    outcome=analytics_outcome,
                    correlation_id=correlation_id,
                )
            # The agent answers reads and free conversation, with everything the
            # deterministic chain already computed as its fallback. Write and approval
            # intents never reach it (DETERMINISTIC_TASK_TYPES). If it is unconfigured or
            # fails, `brain_result.text` is exactly the canned ack computed above.
            brain_store = BrainStore(store.session)
            brain_result = answer_owner(
                store=store,
                brain=brain_store,
                settings=settings,
                task_type=decision.task_type,
                owner_text=owner_text,
                history=tuple(owner_history),
                fallback_text=ack_text,
                kill_switch=kill_switch,
                demo_active=demo_mode_active(settings),
                calendar=calendar_port,
                source_ref=f"{provider}:{item['id']}",
            )
            if brain_result.used_agent:
                ack_text = brain_result.text
            else:
                phrased = owner_reply_port.compose(
                    task_type=decision.task_type.value,
                    canned=ack_text,
                    owner_message=owner_text,
                    history=tuple(owner_history),
                    kill_switch=kill_switch,
                )
                ack_text = phrased.text
            last_reply = ack_text
            # Learn after the reply is settled, never before: memory formation must not
            # delay or change what Assaf sees this turn.
            learn_from_exchange(
                brain=brain_store,
                settings=settings,
                owner_text=owner_text,
                history=tuple(owner_history),
                source_ref=f"{provider}:{item['id']}",
                kill_switch=kill_switch,
                demo_active=demo_mode_active(settings),
            )
            sent = await send_inbound_reply(
                port=port,
                message=_outbound_reply(item, text=ack_text, channel=channel),
                kill_switch=kill_switch,
                automation_mode=settings.automation_mode,
                actor_role="owner",
            )
            store.mark_webhook(
                provider=provider,
                provider_event_id=item["id"],
                status="sent" if sent else "processed",
            )
            if sent:
                sent_count += 1
                owner_message_out = build_message_out_event(
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=item["id"],
                    conversation_id=_event_conversation_id(item),
                    text=ack_text,
                    lead_id=None,
                )
                stamp_correlation(owner_message_out, correlation_id)
                store.save_canonical_event(
                    provider=provider,
                    event=owner_message_out,
                )
            processed += 1
            log_comm(
                channel=channel_value,
                provider=provider,
                actor_type="owner",
                direction="in",
                external_message_id=item["id"],
                conversation_id=_event_conversation_id(item),
                policy_result=decision.task_type.value,
                success=True,
                automation_mode=settings.automation_mode.value,
            )
            continue
        message_text = item["text"]
        latest_message = item["text"]
        handoff_lead_id: str | None = None
        fresh_handoff = False
        if channel == Channel.WHATSAPP:
            allowed, handoff_lead_id, fresh_handoff, gated_text = prepare_whatsapp_inbound(
                store,
                external_id=item["from"],
                text=item["text"],
                require_business_scope=settings.whatsapp_require_business_scope,
            )
            if not allowed:
                scope = existing_whatsapp_scope(store, item["from"])
                log_comm(
                    channel=channel_value,
                    provider=provider,
                    actor_type="personal"
                    if scope
                    in {
                        AutomationScope.PERSONAL.value,
                        AutomationScope.DO_NOT_AUTOMATE.value,
                    }
                    else "unknown",
                    direction="in",
                    external_message_id=item["id"],
                    automation_scope=scope,
                    policy_result="deny",
                    success=True,
                    automation_mode=settings.automation_mode.value,
                )
                store.mark_webhook(
                    provider=provider,
                    provider_event_id=item["id"],
                    status="processed",
                )
                processed += 1
                continue
            message_text = gated_text
            latest_message = gated_text
        audio_transcript: str | None = None
        if item.get("source") == "audio":
            audio_transcript = inbound_text_without_token(message_text)
            store.save_transcript(
                provider=provider,
                provider_event_id=item["id"],
                channel=channel_value,
                external_id=item["from"],
                actor_role="prospect",
                transcript=audio_transcript,
                stt_provider=item.get("stt_provider", ""),
                stt_model=item.get("stt_model", ""),
                language=item.get("language", ""),
                duration_ms=_transcript_duration_ms(item),
                confidence=item.get("confidence", ""),
            )
        if handoff_lead_id is not None:
            lead_id = handoff_lead_id
            customer_id = store.get_lead_customer_id(handoff_lead_id)
            if customer_id is not None:
                persist_verified_identity_link(
                    store,
                    customer_id=customer_id,
                    channel=Channel.WHATSAPP,
                    external_id=item["from"],
                    reason=REASON_HANDOFF_TOKEN,
                )
        else:
            _customer_id, lead_id = store.open_channel_lead(
                channel=channel, external_id=item["from"]
            )
        run_id = f"run_{uuid4().hex[:12]}"
        if channel == Channel.INSTAGRAM:
            _persist_instagram_attribution(
                store=store,
                provider=provider,
                channel=channel,
                lead_id=lead_id,
                item=item,
            )
            sender = settings.instagram_sender
            ownership_present = sender in VALID_INSTAGRAM_SENDERS
            persist_tool_outcome(
                store,
                provider=provider,
                channel=channel,
                inbound_provider_event_id=f"{lead_id}:ownership",
                conversation_id=_event_conversation_id(item),
                lead_id=lead_id,
                outcome=conversation_ownership_outcome(
                    present=ownership_present,
                    now=datetime.now(UTC),
                ),
                correlation_id=run_id,
            )
        if not message_text.strip():
            store.mark_webhook(
                provider=provider,
                provider_event_id=item["id"],
                status="processed",
            )
            processed += 1
            continue
        prospect_message_in = build_message_in_event(
            provider=provider,
            channel=channel,
            provider_event_id=item["id"],
            conversation_id=_event_conversation_id(item),
            text=message_text,
            actor_role="prospect",
            lead_id=lead_id,
        )
        stamp_correlation(prospect_message_in, run_id)
        store.save_canonical_event(
            provider=provider,
            event=prospect_message_in,
        )
        if audio_transcript and audio_transcript.strip():
            persist_tool_outcome(
                store,
                provider=provider,
                channel=channel,
                inbound_provider_event_id=item["id"],
                conversation_id=_event_conversation_id(item),
                lead_id=lead_id,
                outcome=transcription_outcome(
                    transcribed=True,
                    latency_ms=_stt_latency_ms(item),
                ),
                correlation_id=run_id,
            )
        graph = build_graph(store, reply_port=build_sales_reply_port(settings))
        started = perf_counter()
        result = graph.invoke(
            empty_state(
                run_id=run_id,
                thread_id=item["from"],
                channel=channel_value,
                lead_id=lead_id,
                latest_message=latest_message,
                kill_switch=kill_switch,
            )
        )
        persist_ai_run(
            store,
            run_id=run_id,
            lead_id=lead_id,
            channel=channel_value,
            next_action=result.get("next_action", ""),
            kill_switch=kill_switch,
            sales_model=settings.sales_model,
            openai_api_key=settings.openai_api_key,
            sales_fallback_model=settings.sales_fallback_model,
            gemini_api_key=settings.gemini_api_key,
            sales_gemini_model=settings.sales_gemini_model,
            latency_ms=elapsed_ms(started),
            tokens_in=result.get("tokens_in", 0),
            tokens_out=result.get("tokens_out", 0),
            automation_mode=settings.automation_mode.value,
        )
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=channel,
            action=result.get("next_action", ""),
            sales=store.get_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=kill_switch,
            inbound_id=item["id"],
        )
        opt_out_outcome = apply_conversation_kill_policy(
            store,
            lead_id=lead_id,
            action=result.get("next_action", ""),
        )
        meeting_research_outcome = apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=channel,
            action=result.get("next_action", ""),
            sales=store.get_sales(lead_id),
            kill_switch=kill_switch,
            research_port=research_port,
        )
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=channel,
            action=result.get("next_action", ""),
            kill_switch=kill_switch,
        )
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=channel,
            action=result.get("next_action", ""),
            sales=store.get_sales(lead_id),
            kill_switch=kill_switch,
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=channel,
            action=result.get("next_action", ""),
            kill_switch=kill_switch,
        )
        reply_text, calendar_outcomes, _meeting_changed = resolve_meeting_reply(
            store,
            lead_id=lead_id,
            channel=channel,
            provider=provider,
            conversation_id=_event_conversation_id(item),
            inbound_provider_event_id=item["id"],
            message=message_text,
            base_reply=result.get("reply", ""),
            next_action=result.get("next_action", ""),
            calendar=calendar_port,
            booking_port=calendar_booking_port,
            kill_switch=kill_switch,
            timezone=settings.calendar_timezone,
            demo_active=demo_mode_active(settings),
        )
        if reply_text and channel == Channel.WHATSAPP:
            control = store.get_conversation_control(
                Channel.WHATSAPP.value, item["from"]
            )
            is_business = bool(
                fresh_handoff
                or (
                    control is not None
                    and control.automation_scope == AutomationScope.MIA_BUSINESS.value
                )
            )
            already = bool(control is not None and control.mia_introduced)
            if is_business:
                reply_text = prepend_mia_intro(
                    reply_text, already_introduced=already
                )
                if not already:
                    store.mark_mia_introduced(
                        channel=Channel.WHATSAPP.value,
                        external_id=item["from"],
                    )
        if reply_text:
            last_reply = reply_text
        for calendar_outcome in calendar_outcomes:
            persist_tool_outcome(
                store,
                provider=provider,
                channel=channel,
                inbound_provider_event_id=item["id"],
                conversation_id=_event_conversation_id(item),
                lead_id=lead_id,
                outcome=calendar_outcome,
                correlation_id=run_id,
            )
        if meeting_research_outcome is not None:
            persist_tool_outcome(
                store,
                provider=provider,
                channel=channel,
                inbound_provider_event_id=item["id"],
                conversation_id=_event_conversation_id(item),
                lead_id=lead_id,
                outcome=meeting_research_outcome,
                correlation_id=run_id,
            )
        if opt_out_outcome is not None:
            persist_tool_outcome(
                store,
                provider=provider,
                channel=channel,
                inbound_provider_event_id=item["id"],
                conversation_id=_event_conversation_id(item),
                lead_id=lead_id,
                outcome=opt_out_outcome,
                correlation_id=run_id,
            )
        if not demo_mode_active(settings):
            if claim_sheets_mirror(store=store, inbound_id=item["id"], tab="sales"):
                started = perf_counter()
                sales = store.get_sales(lead_id)
                sheets_written = mirror_lead(
                    sheets=sheets_port,
                    row=LeadMirrorRow(
                        lead_id=lead_id,
                        channel=channel_value,
                        stage=store.get_lead_stage(lead_id),
                        fit=sales.fit.value,
                        pain_level=int(sales.pain_level),
                        next_action=result.get("next_action", ""),
                    ),
                    kill_switch=kill_switch,
                )
                fu_written = False
                fu = store.get_follow_up(lead_id)
                if fu is not None:
                    fu_written = mirror_follow_up(
                        sheets=sheets_port,
                        row=FollowUpMirrorRow(
                            lead_id=lead_id,
                            due_at=fu.due_at,
                            channel=fu.channel,
                            status=fu.status,
                            result=fu.reason,
                        ),
                        kill_switch=kill_switch,
                    )
                deal_written = False
                deal = store.get_deal(lead_id)
                if deal is not None:
                    deal_written = mirror_deal(
                        sheets=sheets_port,
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
                        sheets=sheets_port,
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
                ai = store.get_ai_run(run_id)
                if ai is not None:
                    activity_row = activity_mirror_row_from_persisted(
                        run_id=ai.run_id,
                        lead_id=ai.lead_id,
                        channel=ai.channel,
                        next_action=ai.next_action,
                        model=ai.model,
                        kill_switch=ai.kill_switch,
                        cost_usd=ai.cost_usd,
                        timezone=settings.calendar_timezone,
                    )
                    if activity_row is not None:
                        activity_written = mirror_activity(
                            sheets=sheets_port,
                            row=activity_row,
                            kill_switch=kill_switch,
                        )
                kpi_written = maybe_mirror_weekly_kpi(
                    store=store,
                    sheets=sheets_port,
                    settings=settings,
                    kill_switch=kill_switch,
                )
                persist_tool_outcome(
                    store,
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=item["id"],
                    conversation_id=_event_conversation_id(item),
                    lead_id=lead_id,
                    outcome=sheets_mirror_outcome(
                        int(sheets_written)
                        + int(fu_written)
                        + int(deal_written)
                        + int(meeting_written)
                        + int(activity_written)
                        + int(kpi_written),
                        latency_ms=elapsed_ms(started),
                    ),
                    correlation_id=run_id,
                )
                complete_sheets_mirror(store=store, inbound_id=item["id"], tab="sales")
        if reply_text:
            automation_scope = (
                _whatsapp_automation_scope(store, item)
                if channel == Channel.WHATSAPP
                else ""
            )
            if should_skip_prospect_send(
                settings.automation_mode,
                "prospect",
                channel=channel_value,
                automation_scope=automation_scope,
                whatsapp_handoff_send=settings.whatsapp_handoff_send,
                auto_reply_instagram=settings.auto_reply_instagram,
                whatsapp_require_business_scope=settings.whatsapp_require_business_scope,
            ):
                persist_shadow_decision(
                    store,
                    run_id=run_id,
                    lead_id=lead_id,
                    channel=channel_value,
                    next_action=result.get("next_action", ""),
                    proposed_reply=reply_text,
                )
                sent = False
            else:
                sent = await send_inbound_reply(
                    port=port,
                    message=_outbound_reply(item, text=reply_text, channel=channel),
                    kill_switch=kill_switch,
                    automation_mode=settings.automation_mode,
                    actor_role="prospect",
                    lead_id=lead_id,
                    store=store,
                    automation_scope=automation_scope,
                    whatsapp_handoff_send=settings.whatsapp_handoff_send,
                    auto_reply_instagram=settings.auto_reply_instagram,
                    whatsapp_require_business_scope=settings.whatsapp_require_business_scope,
                )
            store.mark_webhook(
                provider=provider,
                provider_event_id=item["id"],
                status="sent" if sent else "processed",
            )
            if sent:
                sent_count += 1
                prospect_message_out = build_message_out_event(
                    provider=provider,
                    channel=channel,
                    inbound_provider_event_id=item["id"],
                    conversation_id=_event_conversation_id(item),
                    text=reply_text,
                    lead_id=lead_id,
                )
                stamp_correlation(prospect_message_out, run_id)
                store.save_canonical_event(
                    provider=provider,
                    event=prospect_message_out,
                )
        else:
            store.mark_webhook(
                provider=provider,
                provider_event_id=item["id"],
                status="processed",
            )
        if result.get("next_action") == NextAction.HANDOFF.value:
            apply_hot_handoff(
                store,
                lead_id=lead_id,
                inbound_id=item["id"],
                want=message_text,
                kill_switch=kill_switch,
                settings=settings,
            )
        control = None
        if channel == Channel.WHATSAPP:
            control = store.get_conversation_control(Channel.WHATSAPP.value, item["from"])
        log_comm(
            channel=channel_value,
            provider=provider,
            actor_type="business_lead",
            direction="in",
            external_message_id=item["id"],
            lead_id=lead_id,
            conversation_id=_event_conversation_id(item),
            automation_scope=control.automation_scope if control is not None else "",
            takeover_state=store.get_takeover_state(lead_id),
            policy_result=result.get("next_action", ""),
            success=True,
            automation_mode=settings.automation_mode.value,
        )
        processed += 1
    return {
        "processed": processed,
        "duplicates": duplicates,
        "sent": sent_count > 0,
        "sent_count": sent_count,
        "reply": last_reply,
    }
