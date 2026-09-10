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

import re

from app.core.config import Settings
from app.domain.emotion import infer_emotional_cues
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
    "handoff": NextAction.HANDOFF,
    "complaint": NextAction.HANDLE_OBJECTION,
}

# The selling ladder for an ongoing conversation. `answer` used to mean
# UNDERSTAND_WORKFLOW on every single turn, so Mia asked a discovery question forever
# and never offered anything. A real visitor answered six of them and left.
#
# Turn 1  learn what the business is
# Turn 2  name what we would take off their hands, and ask if they want to hear how
# Turn 3+ one sharp question about the step that actually costs them time
ANSWER_LADDER: tuple[NextAction, ...] = (
    NextAction.UNDERSTAND_WORKFLOW,
    NextAction.OFFER_HYPOTHESIS,
    NextAction.DEEPEN_PAIN,
)


def answer_intent(*, visitor_turns: int, frustrated: bool) -> NextAction:
    """Pick the rung. Frustration jumps straight to the offer, never another question."""
    if frustrated:
        return NextAction.OFFER_HYPOTHESIS
    index = max(0, visitor_turns - 1)
    return ANSWER_LADDER[min(index, len(ANSWER_LADDER) - 1)]


# Copy whose exact wording is the guardrail. Never paraphrased, never sent to a model.
VERBATIM_SITE_ACTIONS = frozenset(
    {
        "no_price",
        "no_metric",
        "tool_status",
        "identity",
        "voice_fail",
        "off_topic",
        # A promise about what happens next. Phrasing it against the HANDOFF intent,
        # which forbids claiming a transfer, put the model in an impossible position.
        "confirm_contact",
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
        lines.append(f"{text[:800]} [{fact.url}]")
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
    visitor_turns: int = 0,
    frustrated: bool = False,
    usage: dict[str, int] | None = None,
    known_facts: tuple[str, ...] = (),
    open_questions: tuple[str, ...] = (),
    asked_actions: tuple[str, ...] = (),
    page_path: str = "",
    page_section: str = "",
    value_only: bool = False,
    answer_only: bool = False,
) -> str:
    """Return the visitor-facing line. Falls back to `canned` on every failure path.

    `usage`, when given, is filled with the tokens this turn actually cost. It is an
    out-parameter rather than a changed return type because thirteen call sites treat
    the result as a plain string. Spend is recorded even when the composed text is
    then discarded -- the provider billed for it either way.
    """
    if action in VERBATIM_SITE_ACTIONS:
        return canned
    if port is None or kill_switch:
        return canned
    if value_only:
        next_action = NextAction.OFFER_HYPOTHESIS
    elif answer_only:
        next_action = SITE_ACTION_TO_NEXT.get(action, NextAction.UNDERSTAND_WORKFLOW)
    elif action in {"answer", "ask_need"}:
        next_action = answer_intent(visitor_turns=visitor_turns, frustrated=frustrated)
    else:
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
            page_path=page_path,
            page_section=page_section,
            knowledge_hits=(),
            context=ReplyContext(
                turns=tuple(turns),
                language=language,
                known_facts=known_facts,
                open_questions=open_questions,
                asked_actions=asked_actions,
                website_value_only=value_only,
                website_answer_only=answer_only,
                knowledge=knowledge_lines(facts),
                # The prompt has a whole delivery contract keyed on PROSPECT TONE, and
                # it was inert on the widget because this was never passed. A visitor
                # who sounds frustrated on the website now gets the same
                # acknowledgement they would get on WhatsApp. Deterministic labels from
                # a closed vocabulary; an empty tuple renders no block at all, so a
                # neutral message never earns manufactured empathy.
                emotional_cues=infer_emotional_cues(latest_message, recent_turns=tuple(turns)),
            ),
        )
    except Exception:
        return canned
    if usage is not None:
        usage["tokens_in"] = int(getattr(composed, "tokens_in", 0) or 0)
        usage["tokens_out"] = int(getattr(composed, "tokens_out", 0) or 0)
    text = scrub_visitor_reply(composed.text or "")
    if not text:
        return canned
    if value_only or answer_only:
        # The live website never delegates discovery or contact capture to the model.
        # Reject question-shaped statements too; punctuation alone is insufficient.
        request = re.search(
            r"(?:^|[.!]\s*)(?:מה\s+(?!ש)|איזה\s|ספרו\s+לי\b|תארו\s+לי\b|אמרו\s+לי\b|"
            r"what\s+(?!we\b|assaf\b)|which\s|tell\s+me\b|describe\b|can\s+you\b)",
            text,
            re.IGNORECASE,
        )
        if "?" in text or "？" in text or request:
            return canned
    if value_only:
        contact_or_price_leak = bool(
            re.search(
                r"טלפון|אימייל|מייל|\bphone\b|\bemail\b|0[2-9]\d{7,8}|(?:\+972|972)\d{8,9}|₪|\$",
                text,
                re.I,
            )
        )
        has_guarantee = bool(re.search(r"מבטיח|בהתחייבות|guarantee", text, re.I))
        has_hypothesis_modal = bool(
            re.search(
                r"אפשר|ניתן|עשוי|יכול|נוכל|נשמח|could|may|might|can|explore|help",
                text,
                re.I,
            )
        )
        if contact_or_price_leak or has_guarantee or not has_hypothesis_modal:
            return canned
    # Outer guard, mirroring `app.graph.orchestrator`: a paraphrase that lands on a line
    # Mia already said is worse than the canned line.
    if repeats_previous_mia_turn(text, list(turns)):
        return canned
    if usage is not None and (composed.text or "").strip() != canned.strip() and text != canned:
        usage["model_reply_used"] = 1
    return text
