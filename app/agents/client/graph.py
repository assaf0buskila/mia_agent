"""ClientGraph — website visitors. Never executes owner capabilities."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.shared.state import ClientState, empty_client_state
from app.brain.embeddings import build_embedding_port
from app.brain.store import BrainStore
from app.capabilities.knowledge import knowledge_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.errors import MiaError
from app.db.store import LeadStore
from app.domain.hot_handoff import apply_hot_handoff
from app.domain.website_handoff_brief import (
    KIND_WEBSITE_WHATSAPP,
    format_website_human_handoff_brief,
)
from app.graph.orchestrator import build_graph
from app.graph.replies import (
    HANDOFF_OWNER_NOTIFIED,
    HANDOFF_OWNER_NOTIFIED_EN,
    HANDOFF_OWNER_UNREACHABLE,
    HANDOFF_OWNER_UNREACHABLE_EN,
)
from app.graph.state import empty_state
from app.integrations.sales_reply import SalesReplyPort
from app.services.finalization import KIND, qualify_and_finalize

ClientRespond = Callable[[ClientState], dict]

_END_TURNS = frozenset({"session_end", "inactivity"})


def _compact_hits(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    hits: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        hits.append(
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or "site"),
                "text": str(item.get("text") or "")[:500],
            }
        )
    return hits


def compile_client_graph(
    store: LeadStore | None = None,
    reply_port: SalesReplyPort | None = None,
    *,
    respond: ClientRespond | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    knowledge_lookup: Callable[[str], tuple[str, ...]] | None = None,
    principal: Principal | None = None,
):
    """Compile the client LangGraph.

    Production closes over LeadStore, settings, and the sales reply port. The inner
    sales orchestrator is reused until that node is inlined (ADR-036 strangler).
    Knowledge retrieve and conversation complete/finalize are graph nodes so looking
    at ClientGraph matches what Mia actually does.
    """
    principal = principal or Principal.client(source="website")
    inner = (
        build_graph(store, reply_port=reply_port, knowledge_lookup=knowledge_lookup)
        if store is not None
        else None
    )

    def load_conversation(state: ClientState) -> dict:
        errors = list(state.get("errors", []))
        if not state.get("conversation_id"):
            errors.append("missing conversation_id")
        if not state.get("lead_id"):
            errors.append("missing lead_id")
        return {"errors": errors}

    def retrieve_knowledge(state: ClientState) -> dict:
        tools = list(state.get("tools_used") or [])
        query = (state.get("latest_message") or "").strip()
        if not query or store is None:
            return {"knowledge_hits": [], "tools_used": tools}
        try:
            brain = BrainStore(store.session)
            result = execute_capability(
                "knowledge.search",
                principal=principal,
                args={"query": query},
                handlers=knowledge_handlers(
                    brain=brain,
                    embedding_port=build_embedding_port(settings or Settings()),
                ),
                kill_switch=bool(state.get("kill_switch")),
            )
        except MiaError:
            return {"knowledge_hits": [], "tools_used": tools}
        if "knowledge.search" not in tools:
            tools.append("knowledge.search")
        return {"knowledge_hits": _compact_hits(result.get("hits")), "tools_used": tools}

    def sales_turn(state: ClientState) -> dict:
        if respond is not None:
            return respond(state)
        if inner is None:
            return {"reply": "", "errors": [*state.get("errors", []), "no_store"]}
        channel = state.get("channel") or "website"
        result = inner.invoke(
            empty_state(
                run_id=state["run_id"],
                thread_id=state["conversation_id"],
                channel=channel,
                lead_id=state.get("lead_id"),
                latest_message=state.get("latest_message", ""),
                kill_switch=state.get("kill_switch", False),
                page_path=state.get("page_path", ""),
                page_section=state.get("page_section", ""),
                knowledge_hits=list(state.get("knowledge_hits") or []),
                meeting_first=bool(state.get("meeting_first", False)),
            )
        )
        return {
            "reply": result.get("reply", ""),
            "next_action": result.get("next_action", ""),
            "language": result.get("language", ""),
            "tokens_in": result.get("tokens_in", 0),
            "tokens_out": result.get("tokens_out", 0),
        }

    def complete_turn(state: ClientState) -> dict:
        if store is None:
            return {"finalized": bool(state.get("finalized"))}
        next_action = state.get("next_action") or ""
        lead_id = state.get("lead_id") or ""
        conversation_id = state.get("conversation_id") or ""
        inbound_id = state.get("inbound_id") or conversation_id
        kill_switch = bool(state.get("kill_switch"))
        turn_kind = state.get("turn_kind") or "message"
        channel = state.get("channel") or ""
        owner_notified = False
        overwrite_handoff = False
        if (
            next_action == "handoff"
            and settings is not None
            and lead_id
            and channel == "website"
        ):
            brief = None
            parse_mode = None
            if conversation_id:
                stage = ""
                try:
                    stage = store.get_lead_stage(lead_id)
                except KeyError:
                    stage = ""
                brief = format_website_human_handoff_brief(
                    lead_id=lead_id,
                    sales=store.get_sales(lead_id),
                    turns=store.list_conversation_turns(conversation_id),
                    stage=stage,
                )
                parse_mode = "HTML"
            attempt = apply_hot_handoff(
                store,
                lead_id=lead_id,
                inbound_id=inbound_id,
                want=state.get("latest_message") or "",
                kill_switch=kill_switch,
                settings=settings,
                brief=brief,
                parse_mode=parse_mode,
            )
            if attempt.attempted:
                overwrite_handoff = True
                owner_notified = bool(attempt.delivered)
        if channel != "website":
            return {"finalized": False}
        if settings is None or not lead_id or not conversation_id:
            return _handoff_reply(
                state,
                owner_notified=owner_notified,
                finalized=False,
                overwrite=overwrite_handoff,
            )
        next_step: str | None = None
        require_visitor = False
        if next_action == "handoff":
            next_step = "human_handoff"
        elif turn_kind == "session_end":
            next_step = "session_closed"
            require_visitor = True
        elif turn_kind == "inactivity":
            next_step = "inactivity"
            require_visitor = True
        if next_step is None:
            return {"finalized": False}
        result = qualify_and_finalize(
            store,
            session_id=conversation_id,
            lead_id=lead_id,
            settings=settings,
            next_step=next_step,
            require_visitor_message=require_visitor,
            now=now,
        )
        finalized = bool(result is not None and result.claimed)
        if result is not None and result.sent:
            owner_notified = True
            overwrite_handoff = True
        return _handoff_reply(
            state,
            owner_notified=owner_notified,
            finalized=finalized,
            overwrite=overwrite_handoff,
        )

    def route_after_retrieve(state: ClientState) -> str:
        if (state.get("turn_kind") or "message") in _END_TURNS:
            return "complete_turn"
        return "sales_turn"

    graph = StateGraph(ClientState)
    graph.add_node("load_conversation", load_conversation)
    graph.add_node("retrieve_knowledge", retrieve_knowledge)
    graph.add_node("sales_turn", sales_turn)
    graph.add_node("complete_turn", complete_turn)
    graph.add_edge(START, "load_conversation")
    graph.add_edge("load_conversation", "retrieve_knowledge")
    graph.add_conditional_edges(
        "retrieve_knowledge",
        route_after_retrieve,
        {"sales_turn": "sales_turn", "complete_turn": "complete_turn"},
    )
    graph.add_edge("sales_turn", "complete_turn")
    graph.add_edge("complete_turn", END)
    return graph.compile()


def _handoff_reply(
    state: ClientState,
    *,
    owner_notified: bool,
    finalized: bool,
    overwrite: bool,
) -> dict:
    """Overwrite HANDOFF copy so a failed Telegram ping cannot claim a transfer."""
    if not overwrite or (state.get("next_action") or "") != "handoff":
        return {"finalized": finalized}
    english = (state.get("language") or "") == "en"
    if owner_notified:
        text = HANDOFF_OWNER_NOTIFIED_EN if english else HANDOFF_OWNER_NOTIFIED
    else:
        text = HANDOFF_OWNER_UNREACHABLE_EN if english else HANDOFF_OWNER_UNREACHABLE
    return {"finalized": finalized, "reply": text}


def finalize_inactive_website_conversations(
    store: LeadStore,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    """Due-scan entry: inactivity finalization runs through ClientGraph, not HTTP."""
    minutes = settings.website_inactivity_minutes
    if minutes <= 0:
        return 0
    clock = now or datetime.now(UTC)
    cutoff = (clock.astimezone(UTC) - timedelta(minutes=minutes)).isoformat()
    rows = store.list_inactive_website_conversations(
        cutoff_iso=cutoff,
        # WhatsApp handoff retires the lead's website sessions; finalization retires only
        # the one conversation it finalized, so a returning lead is scanned again.
        skip_kinds=(KIND_WEBSITE_WHATSAPP,),
        skip_conversation_kinds=(KIND,),
        limit=50,
    )
    graph = compile_client_graph(store, settings=settings, now=clock)
    finalized = 0
    for session_id, lead_id in rows:
        out: dict[str, Any] = graph.invoke(
            empty_client_state(
                run_id=f"inact:{session_id}",
                conversation_id=session_id,
                visitor_id=session_id,
                lead_id=lead_id,
                turn_kind="inactivity",
            )
        )
        if out.get("finalized"):
            finalized += 1
    return finalized
