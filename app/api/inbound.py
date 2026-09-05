from time import perf_counter
from uuid import uuid4

from app.agents.client.graph import compile_client_graph
from app.agents.shared.state import empty_client_state
from app.api.inbound_common import (
    event_conversation_id as _event_conversation_id,
)
from app.api.inbound_common import (
    outbound_reply as _outbound_reply,
)
from app.api.inbound_common import (
    stt_latency_ms as _stt_latency_ms,
)
from app.api.inbound_common import (
    transcript_duration_ms as _transcript_duration_ms,
)
from app.api.owner import _is_authorized_owner, process_owner_item
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.core.demo import demo_mode_active
from app.core.logging import log_comm
from app.core.outbound import send_inbound_reply
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms, persist_ai_run
from app.domain.approvals import (
    apply_approval_policy,
)
from app.domain.conversation_kill import apply_conversation_kill_policy
from app.domain.conversation_scope import (
    AutomationScope,
    existing_whatsapp_scope,
    prepare_whatsapp_inbound,
    prepend_mia_intro,
)
from app.domain.deals import apply_deal_policy
from app.domain.events import (
    Channel,
    build_message_in_event,
    build_message_out_event,
    persist_tool_outcome,
    stamp_correlation,
    transcription_outcome,
    webhook_envelope_kind,
)
from app.domain.followups import apply_follow_up_policy
from app.domain.handoff.hot import apply_hot_handoff
from app.domain.handoff.tokens import inbound_text_without_token
from app.domain.identity import REASON_HANDOFF_TOKEN, persist_verified_identity_link
from app.domain.meetings.booking import resolve_meeting_reply
from app.domain.meetings.briefs import apply_meeting_brief_policy
from app.domain.meetings.state import apply_meeting_policy
from app.domain.sales import NextAction
from app.domain.shadow import persist_shadow_decision, should_skip_prospect_send
from app.integrations.base import MessagePort
from app.integrations.calendar import (
    CalendarAgendaPort,
    CalendarPort,
    build_calendar_agenda_port,
    build_calendar_port,
)
from app.integrations.calendar_booking import CalendarBookingPort, build_calendar_booking_port
from app.integrations.ga4 import Ga4Port, build_ga4_port
from app.integrations.gmail import GmailPort, build_gmail_port
from app.integrations.instagram_insights import (
    InstagramInsightsPort,
    build_instagram_insights_port,
)
from app.integrations.linkedin import LinkedInPort, build_linkedin_port
from app.integrations.owner_reply import OwnerReplyPort, build_owner_reply_port
from app.integrations.research import ResearchPort, build_research_port
from app.integrations.sales_reply import build_sales_reply_port
from app.integrations.search_console import SearchConsolePort, build_search_console_port
from app.integrations.seo_audit import SeoAuditPort, build_seo_audit_port
from app.integrations.sheets import (
    SheetsPort,
    build_sheets_port,
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
    calendar_agenda: CalendarAgendaPort | None = None,
    calendar_booking: CalendarBookingPort | None = None,
    sheets: SheetsPort | None = None,
    instagram_insights: InstagramInsightsPort | None = None,
    research: ResearchPort | None = None,
    linkedin: LinkedInPort | None = None,
    search_console: SearchConsolePort | None = None,
    ga4: Ga4Port | None = None,
    seo_audit: SeoAuditPort | None = None,
    owner_reply: OwnerReplyPort | None = None,
    gmail: GmailPort | None = None,
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
    research_port = research if research is not None else build_research_port(settings)
    for item in items:
        if not item["id"] or not item["from"]:
            continue
        is_authorized_owner = _is_authorized_owner(
            actor_id=item["from"], owner_ids=owner_ids
        )
        if not store.claim_webhook(
            provider=provider,
            provider_event_id=item["id"],
            channel=channel.value,
            envelope_kind=webhook_envelope_kind(item),
        ):
            duplicates += 1
            continue
        if is_authorized_owner:
            calendar_agenda_port = (
                calendar_agenda
                if calendar_agenda is not None
                else build_calendar_agenda_port(settings)
            )
            gmail_port = gmail if gmail is not None else build_gmail_port(settings)
            instagram_insights_port = (
                instagram_insights
                if instagram_insights is not None
                else build_instagram_insights_port(settings)
            )
            linkedin_port = linkedin if linkedin is not None else build_linkedin_port(settings)
            search_console_port = (
                search_console
                if search_console is not None
                else build_search_console_port(settings)
            )
            ga4_port = ga4 if ga4 is not None else build_ga4_port(settings)
            seo_audit_port = (
                seo_audit if seo_audit is not None else build_seo_audit_port(settings)
            )
            owner_reply_port = (
                owner_reply if owner_reply is not None else build_owner_reply_port(settings)
            )
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
                calendar_booking_port=calendar_booking_port,
                calendar_agenda_port=calendar_agenda_port,
                gmail_port=gmail_port,
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
            customer_id = store.get_lead_customer_id(handoff_lead_id)
            if store.get_lead(handoff_lead_id) is None:
                _customer_id, lead_id = store.open_channel_lead(
                    channel=channel, external_id=item["from"]
                )
            else:
                lead_id = handoff_lead_id
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
        # Prospect channels are client trust too - never the owner console.
        graph = compile_client_graph(
            store,
            principal=Principal.client(source=channel_value, actor_id=lead_id),
            reply_port=build_sales_reply_port(settings),
            settings=settings,
        )
        started = perf_counter()
        result = graph.invoke(
            empty_client_state(
                run_id=run_id,
                conversation_id=item["from"],
                visitor_id=item["from"],
                lead_id=lead_id,
                latest_message=latest_message,
                kill_switch=kill_switch,
                channel=channel_value,
                inbound_id=item["id"],
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
        send_failed = False
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
                        whatsapp_require_business_scope=settings.whatsapp_require_business_scope,
                )
            send_failed = bool(reply_text) and not sent
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
            success=not send_failed,
            automation_mode=settings.automation_mode.value,
        )
        processed += 1
        # Make this item durable before touching the next one. Providers batch
        # messages, and `get_db` rolls the whole request back on any exception: a
        # failed send for item B used to erase item A's committed "sent" claim, so the
        # provider retry redelivered A's reply to a customer who already had it.
        # Per-item commit keeps at-least-once for the item that failed (its claim is
        # rolled back and reclaimed on retry) without re-sending the ones that worked.
        store.session.commit()
    return {
        "processed": processed,
        "duplicates": duplicates,
        "sent": sent_count > 0,
        "sent_count": sent_count,
        "reply": last_reply,
    }
