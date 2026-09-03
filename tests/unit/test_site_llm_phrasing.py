"""Site Mia phrases the deterministic decision instead of repeating one canned line.

Regression cover for the live bug: every substantive sales question fell into the
`need`/`other` bucket and returned the identical `ANSWER_HE` string, so a visitor who
described their business got the same sentence as one who asked what the services were.
"""

from __future__ import annotations

from app.domain.memory import ConversationTurn
from app.domain.sales import NextAction
from app.integrations.sales_reply import ComposeResult, ReplyContext
from app.surfaces.site_policy import NO_PRICE_HE, decide_site_turn
from app.surfaces.site_reply import (
    SITE_ACTION_TO_NEXT,
    VERBATIM_SITE_ACTIONS,
    knowledge_lines,
    phrase_site_reply,
)

SERVICES_Q = "היי מיה, אני רוצה לדעת איזה שירותים אתם מציעים?"
GEL_NAILS_Q = (
    "יש לי עסק של לק ג'ל ואני צריך שיהיה מענה אוטומטי "
    "ושמירת מספרי טלפון כדי שאוכל לחזור ללקוחות"
)


class EchoPort:
    """Phrases the turn by echoing the visitor's own words back, like a live model."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def compose(self, **kwargs: object) -> ComposeResult:
        self.calls.append(kwargs)
        latest = str(kwargs["latest_message"])
        return ComposeResult(text=f"בקשר ל{latest[:40]}, ספרו לי עוד.")


class BoomPort:
    def compose(self, **kwargs: object) -> ComposeResult:
        raise RuntimeError("provider down")


class EmptyPort:
    def compose(self, **kwargs: object) -> ComposeResult:
        return ComposeResult(text="   ")


def _canned(text: str) -> tuple[str, str]:
    decision = decide_site_turn(
        thought=text,
        language="he",
        has_contact=False,
        already_confirmed=False,
        selling_stopped=False,
        already_pinged=False,
    )
    return decision.action, decision.reply


def test_the_bug_two_questions_share_one_canned_line() -> None:
    """The deterministic layer alone still collapses both questions onto one string."""
    _, services = _canned(SERVICES_Q)
    _, gel = _canned(GEL_NAILS_Q)
    assert services == gel


def test_phrasing_makes_the_two_replies_differ() -> None:
    port = EchoPort()
    services_action, services_canned = _canned(SERVICES_Q)
    gel_action, gel_canned = _canned(GEL_NAILS_Q)

    services = phrase_site_reply(
        action=services_action,
        canned=services_canned,
        latest_message=SERVICES_Q,
        language="he",
        port=port,
    )
    gel = phrase_site_reply(
        action=gel_action,
        canned=gel_canned,
        latest_message=GEL_NAILS_Q,
        language="he",
        port=port,
    )
    assert services != gel
    assert services not in {services_canned, gel_canned}
    assert "לק ג'ל" in gel


def test_guardrail_actions_are_never_paraphrased() -> None:
    """No published price means the exact refusal, even with a live port."""
    port = EchoPort()
    reply = phrase_site_reply(
        action="no_price",
        canned=NO_PRICE_HE,
        latest_message="כמה זה עולה?",
        language="he",
        port=port,
    )
    assert reply == NO_PRICE_HE
    assert port.calls == []


def test_every_verbatim_action_bypasses_the_port() -> None:
    port = EchoPort()
    for action in VERBATIM_SITE_ACTIONS:
        assert (
            phrase_site_reply(
                action=action,
                canned="קבוע",
                latest_message="שאלה",
                language="he",
                port=port,
            )
            == "קבוע"
        )
    assert port.calls == []


def test_no_port_configured_falls_back_to_canned() -> None:
    action, canned = _canned(GEL_NAILS_Q)
    assert (
        phrase_site_reply(
            action=action,
            canned=canned,
            latest_message=GEL_NAILS_Q,
            language="he",
            port=None,
        )
        == canned
    )


def test_provider_exception_falls_back_to_canned_never_raises() -> None:
    action, canned = _canned(GEL_NAILS_Q)
    assert (
        phrase_site_reply(
            action=action,
            canned=canned,
            latest_message=GEL_NAILS_Q,
            language="he",
            port=BoomPort(),
        )
        == canned
    )


def test_blank_model_output_falls_back_to_canned() -> None:
    action, canned = _canned(GEL_NAILS_Q)
    assert (
        phrase_site_reply(
            action=action,
            canned=canned,
            latest_message=GEL_NAILS_Q,
            language="he",
            port=EmptyPort(),
        )
        == canned
    )


def test_repeating_a_previous_mia_turn_falls_back_to_canned() -> None:
    """The outer anti-repeat guard, mirroring app.graph.orchestrator."""
    said = "בקשר לזה שאתם צריכים מענה אוטומטי לעסק, ספרו לי עוד על הלקוחות שלכם."

    class RepeatPort:
        def compose(self, **kwargs: object) -> ComposeResult:
            return ComposeResult(text=said)

    action, canned = _canned(GEL_NAILS_Q)
    reply = phrase_site_reply(
        action=action,
        canned=canned,
        latest_message=GEL_NAILS_Q,
        language="he",
        turns=(ConversationTurn(role="mia", text=said),),
        port=RepeatPort(),
    )
    assert reply == canned


def test_port_receives_history_and_website_channel() -> None:
    port = EchoPort()
    turns = (
        ConversationTurn(role="prospect", text=SERVICES_Q),
        ConversationTurn(role="mia", text="ספרו עוד."),
    )
    action, canned = _canned(GEL_NAILS_Q)
    phrase_site_reply(
        action=action,
        canned=canned,
        latest_message=GEL_NAILS_Q,
        language="he",
        turns=turns,
        port=port,
    )
    call = port.calls[0]
    assert call["channel"] == "website"
    assert call["action"] is SITE_ACTION_TO_NEXT[action]
    context = call["context"]
    assert isinstance(context, ReplyContext)
    assert context.turns == turns
    assert context.language == "he"


def test_action_map_targets_are_real_next_actions() -> None:
    for action, mapped in SITE_ACTION_TO_NEXT.items():
        assert isinstance(mapped, NextAction)
        assert action not in VERBATIM_SITE_ACTIONS


def test_knowledge_lines_keep_only_assafweb_rows() -> None:
    from app.surfaces.site_policy import PublishedFact

    facts = (
        PublishedFact(text="בונים אתרים", url="https://assafweb.com/services"),
        PublishedFact(text="מחיר מומצא", url="https://not-assafweb.example/x"),
    )
    lines = knowledge_lines(facts)
    assert len(lines) == 1
    assert "בונים אתרים" in lines[0]
    assert "assafweb.com" in lines[0]
