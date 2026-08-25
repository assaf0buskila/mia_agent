from typing import Any

from langgraph.graph import END, START, StateGraph

from app.db.store import LeadStore
from app.domain.events import (
    CanonicalEvent,
    Channel,
    build_handoff_event,
    build_meeting_offered_event,
    build_qualification_updated_event,
    stamp_correlation,
)
from app.domain.extract import extract_sales_signals
from app.domain.language import reply_language
from app.domain.lead_label import derive_headline
from app.domain.memory import repeats_previous_mia_turn
from app.domain.sales import (
    NextAction,
    SalesState,
    mark_action_delivered,
    select_next_action,
    times_asked,
)
from app.domain.value import ValueKind, persist_business_value
from app.graph.replies import reply_for
from app.graph.state import GraphState
from app.integrations.sales_reply import (
    CannedSalesReplyPort,
    ReplyContext,
    SalesReplyPort,
)


def _persist_canonical_event(
    store: LeadStore,
    *,
    run_id: str,
    channel_str: str,
    event: CanonicalEvent,
) -> None:
    if not run_id:
        return
    try:
        Channel(channel_str)
    except ValueError:
        return
    stamp_correlation(event, run_id)
    store.save_canonical_event(provider=channel_str, event=event)


def _qualification_snapshot(sales: SalesState) -> dict[str, Any]:
    return {
        "fit": sales.fit.value,
        "pain_level": int(sales.pain_level),
        "workflow_known": sales.workflow_known,
        "impact_confirmed": sales.impact_confirmed,
        "reflected": sales.reflected,
        "hypothesis_offered": sales.hypothesis_offered,
        "buying_reality_known": sales.buying_reality_known,
        "authority_known": sales.authority_known,
        "timeline_known": sales.timeline_known,
        "metric_known": sales.metric_known,
        "missing_fields": sales.missing_fields,
        "willingness_to_meet": sales.willingness_to_meet,
        "owner_required": sales.owner_required,
        "whatsapp_handoff_offered": sales.whatsapp_handoff_offered,
        "active_objection": sales.active_objection.value
        if sales.active_objection is not None
        else None,
    }


def build_graph(store: LeadStore, reply_port: SalesReplyPort | None = None):
    port = reply_port if reply_port is not None else CannedSalesReplyPort()

    def sales_next_action(state: GraphState) -> dict:
        lead_id = state.get("lead_id")
        if not lead_id:
            return {"errors": [*state.get("errors", []), "missing lead_id"]}
        sales = store.get_sales(lead_id)
        before = _qualification_snapshot(sales)
        sales = extract_sales_signals(sales, state.get("latest_message", ""))
        channel_str = state.get("channel", "website")
        action = select_next_action(sales, channel=channel_str)
        repeat_ask = times_asked(sales, action) > 0
        sales = mark_action_delivered(sales, action)
        after = _qualification_snapshot(sales)
        store.save_sales(sales)
        run_id = state.get("run_id", "")
        conversation_id = state.get("thread_id", "")
        event_channel: Channel | None
        try:
            event_channel = Channel(channel_str)
        except ValueError:
            event_channel = None
        if before != after and event_channel is not None:
            _persist_canonical_event(
                store,
                run_id=run_id,
                channel_str=channel_str,
                event=build_qualification_updated_event(
                    provider=channel_str,
                    channel=event_channel,
                    run_id=run_id,
                    lead_id=lead_id,
                    conversation_id=conversation_id,
                    payload=after,
                ),
            )
        if (
            event_channel is not None
            and after.get("fit") == "good"
            and before.get("fit") != "good"
        ):
            persist_business_value(
                store,
                provider=channel_str,
                channel=event_channel,
                lead_id=lead_id,
                kind=ValueKind.QUALIFIED,
                conversation_id=conversation_id,
            )
        if event_channel is not None:
            if action == NextAction.OFFER_MEETING:
                _persist_canonical_event(
                    store,
                    run_id=run_id,
                    channel_str=channel_str,
                    event=build_meeting_offered_event(
                        provider=channel_str,
                        channel=event_channel,
                        run_id=run_id,
                        lead_id=lead_id,
                        conversation_id=conversation_id,
                    ),
                )
            elif action == NextAction.HANDOFF:
                _persist_canonical_event(
                    store,
                    run_id=run_id,
                    channel_str=channel_str,
                    event=build_handoff_event(
                        provider=channel_str,
                        channel=event_channel,
                        run_id=run_id,
                        lead_id=lead_id,
                        conversation_id=conversation_id,
                    ),
                )
                persist_business_value(
                    store,
                    provider=channel_str,
                    channel=event_channel,
                    lead_id=lead_id,
                    kind=ValueKind.HANDOFF,
                    conversation_id=conversation_id,
                )
        channel = state.get("channel", "website")
        latest_message = state.get("latest_message", "")
        turns = store.list_conversation_turns(conversation_id)
        # Give the lead a human label the first time the prospect says something
        # substantive, so owner-facing lists can name it instead of showing an id.
        if not sales.headline:
            headline = derive_headline(turns)
            if headline:
                sales.headline = headline
                store.save_sales(sales)
        language = reply_language(latest_message=latest_message, turns=turns)
        canned = reply_for(
            channel, action, sales, language=language, repeat_ask=repeat_ask
        )
        composed = port.compose(
            action=action,
            canned=canned,
            latest_message=latest_message,
            channel=channel,
            kill_switch=state.get("kill_switch", False),
            page_path=state.get("page_path", ""),
            page_section=state.get("page_section", ""),
            knowledge_hits=list(state.get("knowledge_hits") or []),
            context=ReplyContext(
                turns=tuple(turns),
                known_facts=tuple(sales.known_facts()),
                open_questions=tuple(sales.open_questions()),
                asked_actions=tuple(dict.fromkeys(sales.asked_actions)),
                language=language,
            ),
        )
        reply_text = composed.text
        if repeats_previous_mia_turn(reply_text, turns):
            reply_text = canned
        return {
            "next_action": action.value,
            "reply": reply_text,
            "language": language,
            "tokens_in": composed.tokens_in,
            "tokens_out": composed.tokens_out,
        }

    graph = StateGraph(GraphState)
    graph.add_node("sales_next_action", sales_next_action)
    graph.add_edge(START, "sales_next_action")
    graph.add_edge("sales_next_action", END)
    return graph.compile()
