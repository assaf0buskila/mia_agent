"""Helpers for asserting "a sales reply was sent" without pinning one language.

Mia answers in the prospect's language, so a test that hardcodes the Hebrew opener
fails the moment the fixture text is English. These helpers assert the property the
tests actually care about — the prospect sales path produced the reply, not an owner
tool or a raw fallback — and they follow the copy tables instead of duplicating them.
"""

from __future__ import annotations

from app.domain.sales import NextAction
from app.graph.replies import (
    OBJECTION_REPLIES,
    OBJECTION_REPLIES_EN,
    QUALIFY_REPLIES,
    QUALIFY_REPLIES_EN,
    REFRAME_REPLIES,
    REFRAME_REPLIES_EN,
    WEBSITE_REPLIES,
    WEBSITE_REPLIES_EN,
    WEBSITE_RETRY_REPLIES,
    WEBSITE_RETRY_REPLIES_EN,
)

_DISCOVERY_ACTIONS = (
    NextAction.UNDERSTAND_WORKFLOW,
    NextAction.DEEPEN_PAIN,
    NextAction.QUANTIFY,
)

DISCOVERY_COPY: frozenset[str] = frozenset(
    table[action]
    for table in (
        WEBSITE_REPLIES,
        WEBSITE_REPLIES_EN,
        WEBSITE_RETRY_REPLIES,
        WEBSITE_RETRY_REPLIES_EN,
    )
    for action in _DISCOVERY_ACTIONS
)

SALES_COPY: frozenset[str] = DISCOVERY_COPY | frozenset(
    [
        *WEBSITE_REPLIES.values(),
        *WEBSITE_REPLIES_EN.values(),
        *WEBSITE_RETRY_REPLIES.values(),
        *WEBSITE_RETRY_REPLIES_EN.values(),
        *QUALIFY_REPLIES.values(),
        *QUALIFY_REPLIES_EN.values(),
        *OBJECTION_REPLIES.values(),
        *OBJECTION_REPLIES_EN.values(),
        *REFRAME_REPLIES.values(),
        *REFRAME_REPLIES_EN.values(),
    ]
)


def assert_discovery_reply(text: str) -> None:
    """The reply is a discovery question from the prospect sales path."""
    assert text in DISCOVERY_COPY, text


def assert_sales_reply(text: str) -> None:
    """The reply came from the prospect sales copy tables, in either language."""
    assert text in SALES_COPY, text


def stop_copy(*, english: bool) -> str:
    table = WEBSITE_REPLIES_EN if english else WEBSITE_REPLIES
    return table[NextAction.STOP]
