"""A bare "thanks" must not buy an embedding call and two table scans.

`classify_site_intent` has no filler class, so "תודה" and "ok" fall through to
`other` — which is in the retrieval trigger set. Every acknowledgement in every
website conversation paid for a full RAG lookup that answered nothing.

The gate is deliberately exact-match: it is a cost gate, not a conversation gate.
"תודה, כמה עולה אתר?" is a price question and must still reach retrieval, and words
that carry meaning inside the sales ladder are not treated as filler at all.
"""

from __future__ import annotations

import pytest
from app.surfaces.site_policy import is_filler


@pytest.mark.parametrize(
    "text",
    [
        "תודה",
        "תודה רבה",
        "תודה!",
        "אוקיי",
        "אוקי",
        "סבבה",
        "thanks",
        "Thanks!",
        "thank you",
        "ok",
        "OK.",
        "okay",
        "  ok  ",
    ],
)
def test_bare_acknowledgements_are_filler(text: str) -> None:
    assert is_filler(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # A real question wearing a polite prefix.
        "תודה, כמה עולה לבנות אתר?",
        "thanks, how much does a website cost?",
        "ok but what about whatsapp automation",
        # Substantive turns.
        "צריך אתר לעסק",
        "כמה זה עולה",
        # Empty is not filler; the caller already handles blank text separately.
        "",
        "   ",
        # Words that steer the sales ladder must never be swallowed.
        "כן",
        "בסדר",
        "מעולה",
    ],
)
def test_anything_carrying_meaning_is_not_filler(text: str) -> None:
    assert is_filler(text) is False


def test_the_website_turn_skips_retrieval_for_filler(monkeypatch) -> None:
    """The whole point: no embedding port is ever built for an acknowledgement."""
    from app.api import website

    calls: list[str] = []

    def _boom(*_args, **_kwargs):
        calls.append("built")
        raise AssertionError("filler must not reach the embedding port")

    monkeypatch.setattr("app.brain.embeddings.build_embedding_port", _boom)

    facts, tools = website._published_facts_for_turn(
        store=None,  # never touched on the filler path
        text="תודה",
        voice_failed=False,
    )
    assert facts == ()
    assert tools == ()
    assert calls == []
