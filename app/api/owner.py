"""Owner inbound turn. Telegram and inbound.py both call process_owner_item."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NamedTuple

from app.api.inbound_common import (
    event_conversation_id,
    outbound_reply,
    owner_telegram_reply_markup,
    stt_latency_ms,
    transcript_duration_ms,
)
from app.brain.store import BrainStore
from app.core.config import Settings, get_settings
from app.core.demo import demo_mode_active
from app.core.logging import log_comm, log_owner_agent
from app.core.outbound import send_inbound_reply
from app.db.store import LeadStore
from app.domain.approvals import ack_for_approval_result, apply_owner_approval_decision
from app.domain.briefs import apply_owner_meeting_brief
from app.domain.commitments import TRIGGER_SPEND_THRESHOLD, parse_due_at, plan_owner_commitment
from app.domain.content_ideas import apply_owner_content_ideas
from app.domain.conversation_scope import apply_owner_scope_mark
from app.domain.debriefs import ack_for_debrief_result, apply_owner_meeting_debrief
from app.domain.events import (
    Channel,
    build_message_in_event,
    build_message_out_event,
    new_correlation_id,
    persist_tool_outcome,
    stamp_correlation,
    transcription_outcome,
    webhook_envelope_kind,
)
from app.domain.feedback import persist_owner_correction
from app.domain.gmail_summaries import apply_owner_gmail_summary
from app.domain.handoff import inbound_text_without_token
from app.domain.hot_handoff import format_hot_leads_ack
from app.domain.lead_reviews import apply_owner_lead_review
from app.domain.learning import (
    InstructionKind,
    classify_instruction_kind,
    propose_owner_instruction,
)
from app.domain.owner_brain import answer_owner, learn_from_exchange, run_owner_turn
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
from app.domain.ownership_freshness import owner_permissions_outcome
from app.domain.seo import enrich_seo_ack
from app.domain.takeover import apply_owner_human_resume, apply_owner_human_takeover
from app.domain.tools import ToolOutcome
from app.integrations.base import MessagePort
from app.integrations.calendar import CalendarPort, build_calendar_port
from app.integrations.ga4 import Ga4Port, build_ga4_port
from app.integrations.instagram_insights import (
    InstagramInsightsPort,
    build_instagram_insights_port,
    enrich_content_insights_ack,
)
from app.integrations.linkedin import LinkedInPort, build_linkedin_port, enrich_linkedin_ack
from app.integrations.owner_reply import OwnerReplyPort, build_owner_reply_port
from app.integrations.research import ResearchPort, build_research_port, enrich_research_ack
from app.integrations.search_console import SearchConsolePort, build_search_console_port
from app.integrations.seo_audit import SeoAuditPort, build_seo_audit_port
from app.integrations.sheets import SheetsPort, build_sheets_port
from app.integrations.thread_summary import build_thread_summary_port


class OwnerTurnResult(NamedTuple):
    processed: bool
    sent: bool
    last_reply: str | None


async def process_owner_item(
    *,
    item: dict[str, str],
    provider: str,
    channel: Channel,
    store: LeadStore,
    port: MessagePort,
    kill_switch: bool,
    owner_ids: set[str],
    settings: Settings,
    calendar_port: CalendarPort,
    sheets_port: SheetsPort,
    instagram_insights_port: InstagramInsightsPort,
    research_port: ResearchPort,
    linkedin_port: LinkedInPort,
    search_console_port: SearchConsolePort,
    ga4_port: Ga4Port,
    seo_audit_port: SeoAuditPort,
    owner_reply_port: OwnerReplyPort,
) -> OwnerTurnResult:
    channel_value = channel.value
    owner_text = inbound_text_without_token(item["text"])
    correlation_id = new_correlation_id()
    if owner_ids:
        persist_tool_outcome(
            store,
            provider=provider,
            channel=channel,
            inbound_provider_event_id=f"owner:{item['from']}",
            conversation_id=event_conversation_id(item),
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
        conversation_id=event_conversation_id(item),
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
            duration_ms=transcript_duration_ms(item),
            confidence=item.get("confidence", ""),
        )
        if owner_text.strip():
            persist_tool_outcome(
                store,
                provider=provider,
                channel=channel,
                inbound_provider_event_id=item["id"],
                conversation_id=event_conversation_id(item),
                lead_id=None,
                outcome=transcription_outcome(
                    transcribed=True,
                    latency_ms=stt_latency_ms(item),
                ),
                correlation_id=correlation_id,
            )
    # Routing sees follow-up references expanded against Mia's own previous
    # replies. The stored transcript above keeps what Assaf actually wrote.
    owner_history = store.list_conversation_turns(event_conversation_id(item))
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
                conversation_id=event_conversation_id(item),
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
            conversation_id=event_conversation_id(item),
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
                conversation_id=event_conversation_id(item),
                lead_id=None,
                outcome=extra_outcome,
                correlation_id=correlation_id,
            )
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
            conversation_id=event_conversation_id(item),
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
                conversation_id=event_conversation_id(item),
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
            conversation_id=event_conversation_id(item),
            lead_id=None,
            outcome=linkedin_outcome,
            correlation_id=correlation_id,
        )
    # The agent answers reads and free conversation, with everything the
    # deterministic chain already computed as its fallback. Write and approval
    # intents never reach it (DETERMINISTIC_TASK_TYPES). If it is unconfigured or
    # fails, `brain_result.text` is exactly the canned ack computed above.
    brain_store = BrainStore(store.session)
    brain_result = run_owner_turn(
        owner_id=item["from"],
        telegram_chat_id=item.get("chat_id") or item["from"],
        run_id=correlation_id,
        latest_message=owner_text,
        kill_switch=kill_switch,
        brain=brain_store,
        settings=settings,
        produce=lambda: answer_owner(
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
            linkedin=linkedin_port,
            search_console=search_console_port,
            ga4=ga4_port,
            seo_audit=seo_audit_port,
            instagram_insights=instagram_insights_port,
            research=research_port,
            source_ref=f"{provider}:{item['id']}",
        ),
    )
    # One line per owner turn saying whether the agent answered and, when it did
    # not, exactly why. Without this a model the account cannot call is
    # indistinguishable from normal operation.
    log_owner_agent(
        used_agent=brain_result.used_agent,
        model=brain_result.model,
        task_type=decision.task_type.value,
        tools_used=brain_result.tools_used,
        reason=brain_result.fallback_reason,
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
    owner_markup = owner_telegram_reply_markup(
        store, channel=channel, task_type=decision.task_type
    )
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
        message=outbound_reply(
            item,
            text=ack_text,
            channel=channel,
            reply_markup=owner_markup,
        ),
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
        pass  # counted by caller via result.sent
        owner_message_out = build_message_out_event(
            provider=provider,
            channel=channel,
            inbound_provider_event_id=item["id"],
            conversation_id=event_conversation_id(item),
            text=ack_text,
            lead_id=None,
        )
        stamp_correlation(owner_message_out, correlation_id)
        store.save_canonical_event(
            provider=provider,
            event=owner_message_out,
        )
    pass  # counted by caller via result.processed
    log_comm(
        channel=channel_value,
        provider=provider,
        actor_type="owner",
        direction="in",
        external_message_id=item["id"],
        conversation_id=event_conversation_id(item),
        policy_result=decision.task_type.value,
        success=True,
        automation_mode=settings.automation_mode.value,
    )
    return OwnerTurnResult(processed=True, sent=sent, last_reply=last_reply)


async def process_owner_texts(
    *,
    provider: str,
    channel: Channel,
    items: list[dict[str, str]],
    store: LeadStore,
    port: MessagePort,
    kill_switch: bool,
    owner_ids: set[str] | None = None,
    calendar: CalendarPort | None = None,
    calendar_booking: Any = None,
    sheets: SheetsPort | None = None,
    instagram_insights: InstagramInsightsPort | None = None,
    research: ResearchPort | None = None,
    linkedin: LinkedInPort | None = None,
    search_console: SearchConsolePort | None = None,
    ga4: Ga4Port | None = None,
    seo_audit: SeoAuditPort | None = None,
    owner_reply: OwnerReplyPort | None = None,
) -> dict[str, int | bool | str | None]:
    """Owner-only inbound. Prospect WhatsApp/Instagram stay on process_inbound_texts."""
    del calendar_booking
    processed = 0
    duplicates = 0
    sent_count = 0
    last_reply: str | None = None
    owner_ids = owner_ids or set()
    settings = get_settings()
    calendar_port = calendar if calendar is not None else build_calendar_port(settings)
    sheets_port = sheets if sheets is not None else build_sheets_port(settings)
    instagram_insights_port = (
        instagram_insights
        if instagram_insights is not None
        else build_instagram_insights_port(settings)
    )
    research_port = research if research is not None else build_research_port(settings)
    linkedin_port = linkedin if linkedin is not None else build_linkedin_port(settings)
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
        turn = await process_owner_item(
            item=item,
            provider=provider,
            channel=channel,
            store=store,
            port=port,
            kill_switch=kill_switch,
            owner_ids=owner_ids,
            settings=settings,
            calendar_port=calendar_port,
            sheets_port=sheets_port,
            instagram_insights_port=instagram_insights_port,
            research_port=research_port,
            linkedin_port=linkedin_port,
            search_console_port=search_console_port,
            ga4_port=ga4_port,
            seo_audit_port=seo_audit_port,
            owner_reply_port=owner_reply_port,
        )
        if turn.processed:
            processed += 1
        if turn.sent:
            sent_count += 1
        if turn.last_reply is not None:
            last_reply = turn.last_reply
    return {
        "processed": processed,
        "duplicates": duplicates,
        "sent": sent_count > 0,
        "sent_count": sent_count,
        "reply": last_reply,
    }
