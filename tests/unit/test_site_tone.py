"""The website adapts to a frustrated visitor the same way WhatsApp does.

The sales prompt carries a whole delivery contract keyed on PROSPECT TONE, hash
pinned and covered by tests. It was inert on the Ask Mia widget, because
`site_reply` built its ReplyContext without ever passing the cues.
"""

from __future__ import annotations

from app.integrations.sales_reply import ComposeResult, ReplyContext
from app.surfaces.site_reply import phrase_site_reply


class CapturingPort:
    def __init__(self) -> None:
        self.context: ReplyContext | None = None

    def compose(self, **kwargs: object) -> ComposeResult:
        self.context = kwargs["context"]  # type: ignore[assignment]
        return ComposeResult(text="בסדר, ספרו לי עוד.")


def _phrase(text: str) -> ReplyContext:
    port = CapturingPort()
    phrase_site_reply(
        action="answer",
        canned="canned",
        latest_message=text,
        language="he",
        port=port,
        visitor_turns=1,
    )
    assert port.context is not None
    return port.context


def test_a_frustrated_visitor_is_flagged_to_the_model() -> None:
    context = _phrase("נמאס לי מזה, שום דבר לא עובד")
    assert context.emotional_cues, "the widget must pass tone like every other channel"
    assert "frustrated" in context.emotional_cues


def test_a_neutral_visitor_earns_no_manufactured_empathy() -> None:
    """An empty tuple renders no PROSPECT TONE block at all."""
    assert _phrase("יש לי עסק של לק ג'ל") .emotional_cues == ()


def test_the_owner_persona_is_defined_not_just_named() -> None:
    """'Talk like Dude' is Assaf's shorthand; the model has never met Dude."""
    from app.graph.owner_agent import SYSTEM_PROMPT

    assert "Talk like Dude" in SYSTEM_PROMPT
    assert "no preamble" in SYSTEM_PROMPT
