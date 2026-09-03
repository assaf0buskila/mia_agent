"""Phrase the deterministic site decision with the shared sales reply port.

`decide_site_turn` stays the only thing that picks the action. This module phrases the
chosen action in context, exactly the way `app.graph.orchestrator` does for WhatsApp and
Instagram: same `SalesReplyPort`, same OpenAI -> OpenAI fallback -> Gemini -> canned
chain, same `lint_customer_reply` and `repeats_previous_mia_turn` guards.

Two hard invariants the site keeps that the inbound path does not need:

* Guardrail actions are never paraphrased. `no_price`, `no_metric`, `tool_status`,
  `identity`, `voice_fail` and `off_topic` carry the "no invented price, no invented
  metric, no named tools" contract in their exact wording, so they are returned verbatim
  and the model never sees them.
* No lead is minted. This layer takes the visitor's turns as plain data and never needs
  a `lead_id`, so the site keeps its "anonymous visitor is not a CRM lead" invariant.
"""

from __future__ import annotations

from app.core.config import Settings
from app.domain.memory import ConversationTurn, repeats_previous_mia_turn
from app.domain.sales import NextAction
from app.integrations.sales_reply import (
    ReplyContext,
    SalesReplyPort,
    build_sales_reply_port,
)
from app.surfaces.site_policy import PublishedFact, scrub_visitor_reply

# Site action -> what the turn is trying to achieve. The model phrases the intent; it
# never gets to pick a different one.
SITE_ACTION_TO_NEXT: dict[str, NextAction] = {
    "answer": NextAction.UNDERSTAND_WORKFLOW,
    "ask_need": NextAction.UNDERSTAND_WORKFLOW,
    "ask_contact": NextAction.OFFER_WHATSAPP,
    "confirm_contact": NextAction.HANDOFF,
    "handoff": NextAction.HANDOFF,
    "complaint": NextAction.HANDLE_OBJECTION,
}

# Copy whose exact wording is the guardrail. Never paraphrased, never sent to a model.
VERBATIM_SITE_ACTIONS = frozenset(
    {
        "no_price",
        "no_metric",
        "tool_status",
        "identity",
        "voice_fail",
        "off_topic",
    }
)


def build_site_reply_port(settings: Settings) -> SalesReplyPort:
    """Same port the inbound channels use. Canned when no model is configured."""
    return build_sales_reply_port(settings)


def knowledge_lines(facts: tuple[PublishedFact, ...]) -> tuple[str, ...]:
    """Provenance-tagged published lines. Only assafweb.com rows may reach the model."""
    lines: list[str] = []
    for fact in facts:
        if not fact.from_assafweb():
            continue
        text = " ".join(fact.text.split())
        if not text:
            continue
        lines.append(f"{text[:280]} [{fact.url}]")
    return tuple(lines)


def phrase_site_reply(
    *,
    action: str,
    canned: str,
    latest_message: str,
    language: str,
    turns: tuple[ConversationTurn, ...] = (),
    facts: tuple[PublishedFact, ...] = (),
    port: SalesReplyPort | None = None,
    kill_switch: bool = False,
) -> str:
    """Return the visitor-facing line. Falls back to `canned` on every failure path."""
    if action in VERBATIM_SITE_ACTIONS:
        return canned
    if port is None or kill_switch:
        return canned
    next_action = SITE_ACTION_TO_NEXT.get(action)
    if next_action is None:
        return canned
    try:
        composed = port.compose(
            action=next_action,
            canned=canned,
            latest_message=latest_message,
            channel="website",
            kill_switch=kill_switch,
            knowledge_hits=(),
            context=ReplyContext(
                turns=tuple(turns),
                language=language,
                knowledge=knowledge_lines(facts),
            ),
        )
    except Exception:
        return canned
    text = scrub_visitor_reply(composed.text or "")
    if not text:
        return canned
    # Outer guard, mirroring `app.graph.orchestrator`: a paraphrase that lands on a line
    # Mia already said is worse than the canned line.
    if repeats_previous_mia_turn(text, list(turns)):
        return canned
    return text
