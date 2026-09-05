import importlib
import inspect

import pytest
from app.core.capabilities import CapabilityId, require_alive
from app.domain.booking_voice import (
    BOOKING_CONFIRMED,
    BOOKING_DENIED,
    BOOKING_RETRY,
    CONFLICT_SLOT_TAKEN,
)
from app.domain.conversation_scope import MIA_INTRO_HE
from app.domain.followup_voice import MEETING_OFFERED_FOLLOW_UP
from app.domain.humanity import (
    HumanityVerdict,
    customer_reply_or_canned,
    lint_customer_reply,
)
from app.domain.meeting_changes import (
    CANCELLATION_DENIED_REPLY,
    CANCELLATION_REQUESTED_REPLY,
    RESCHEDULE_CONFIRMED,
    RESCHEDULE_CONFLICT,
    RESCHEDULE_DENIED,
    RESCHEDULE_OFFER_INTRO,
    RESCHEDULE_RETRY,
)
from app.domain.sales import NextAction, SalesState
from app.graph.replies import (
    OBJECTION_REPLIES,
    QUALIFY_REPLIES,
    REFRAME_REPLIES,
    WEBSITE_REPLIES,
    WEBSITE_RETRY_REPLIES,
    reply_for,
)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Absolutely! Happy to help.", "ai_phrase"),
        ("Let's dive in and explore.", "ai_phrase"),
        ("It's important to note that we can help.", "ai_phrase"),
        ("This is game-changing for your business.", "ai_phrase"),
        ("We offer a seamless experience.", "ai_phrase"),
        ("We can leverage your data.", "ai_phrase"),
        ("Let’s dive in and explore.", "ai_phrase"),
        ("It’s important to note that we can help.", "ai_phrase"),
        ("Limited time offer for you.", "unsupported_claim"),
        ("Only today we can do this.", "unsupported_claim"),
        ("This is your last chance to book.", "unsupported_claim"),
        ("Act now and we can start.", "unsupported_claim"),
        ("Our ROI is proven.", "unsupported_claim"),
    ],
)
def test_english_anti_patterns_fail(text: str, reason: str) -> None:
    verdict = lint_customer_reply(text)
    assert verdict.ok is False
    assert reason in verdict.reasons


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("בהחלט! נשמח לעזור.", "ai_phrase"),
        ("בואו נצלול לפרטים.", "ai_phrase"),
        ("חשוב לציין שזה רלוונטי.", "ai_phrase"),
        ("רק היום יש לך את ההזדמנות.", "unsupported_claim"),
        ("זו הזדמנות אחרונה להצטרף.", "unsupported_claim"),
        ("הגדלת מכירות ב-40 אחוז מובטחת.", "unsupported_claim"),
    ],
)
def test_hebrew_phrases_fail(text: str, reason: str) -> None:
    verdict = lint_customer_reply(text)
    assert verdict.ok is False
    assert reason in verdict.reasons


@pytest.mark.parametrize(
    "text",
    [
        "זה טוב — באמת.",
        "A–B range",
        "path\\to\\file",
        "foo / bar",
        "hello -- there",
        "hello - there",
        "W-E-L-C-O-M-E",
        "ש'ל'ו'ם",
        "a\\b\\c",
    ],
)
def test_typography_fails(text: str) -> None:
    verdict = lint_customer_reply(text)
    assert verdict.ok is False
    assert "typography" in verdict.reasons


@pytest.mark.parametrize(
    "text",
    [
        "יום-יום זה נורמלי.",
        "השקעה-עכשיו זה לא נכון.",
        "מיה מ-AssafWeb",
    ],
)
def test_any_hyphen_fails_typography(text: str) -> None:
    verdict = lint_customer_reply(text)
    assert verdict.ok is False
    assert "typography" in verdict.reasons


@pytest.mark.parametrize(
    "text",
    [
        "10/2026",
        "Let's go.",
        "ג'ינג'י זה בסדר.",
        "see // comment",
        "https://meet.google.com/abc-defg-hij and we can talk.",
    ],
)
def test_dates_apostrophes_and_urls_pass(text: str) -> None:
    assert lint_customer_reply(text).ok is True


def test_two_questions_fail_question_count() -> None:
    verdict = lint_customer_reply("מה קורה? ומתי?")
    assert verdict.ok is False
    assert "question_count" in verdict.reasons


def test_one_question_passes() -> None:
    assert lint_customer_reply("מה קורה?").ok is True


def test_empty_text_fails_without_reasons() -> None:
    verdict = lint_customer_reply("   ")
    assert verdict == HumanityVerdict(ok=False, reasons=())


def test_customer_reply_or_canned_returns_canned_on_failure() -> None:
    canned = "ספרו לי קצת איך נראה יום רגיל בעסק."
    result = customer_reply_or_canned(
        candidate="Absolutely! Let's dive in.",
        canned=canned,
    )
    assert result == canned


def _all_canned_customer_replies() -> list[str]:
    texts: list[str] = []
    texts.extend(WEBSITE_REPLIES.values())
    texts.extend(WEBSITE_RETRY_REPLIES.values())
    texts.extend(QUALIFY_REPLIES.values())
    texts.extend(OBJECTION_REPLIES.values())
    texts.extend(REFRAME_REPLIES.values())
    texts.append(reply_for("website", NextAction.OFFER_MEETING, SalesState(lead_id="l1")))
    texts.append(
        reply_for(
            "website",
            NextAction.OFFER_MEETING,
            SalesState(lead_id="l1", company_domain="acme.com"),
        )
    )
    texts.extend(
        (
            CONFLICT_SLOT_TAKEN,
            BOOKING_RETRY,
            BOOKING_DENIED,
            BOOKING_CONFIRMED,
            RESCHEDULE_OFFER_INTRO,
            RESCHEDULE_RETRY,
            RESCHEDULE_CONFLICT,
            RESCHEDULE_DENIED,
            RESCHEDULE_CONFIRMED,
            CANCELLATION_REQUESTED_REPLY,
            CANCELLATION_DENIED_REPLY,
            MEETING_OFFERED_FOLLOW_UP,
            MIA_INTRO_HE,
            "זמין:\n1. Sun 01 Jan 10:00\nהשיבו 1 כדי לאשר.",
        )
    )
    return texts


@pytest.mark.parametrize("text", _all_canned_customer_replies())
def test_all_canned_customer_replies_pass_lint(text: str) -> None:
    verdict = lint_customer_reply(text)
    assert verdict.ok is True, f"canned failed: {text!r} reasons={verdict.reasons}"


def test_require_alive_humanity_linter() -> None:
    require_alive(CapabilityId.HUMANITY_LINTER)


def test_humanity_module_has_no_http_or_ports() -> None:
    source = inspect.getsource(importlib.import_module("app.domain.humanity"))
    for forbidden in ("httpx", "MessagePort", "OpenAI"):
        assert forbidden not in source


_CUSTOMER_FEMININE_ONLY = (
    "שאלי",
    "נסי ",
    "לחצי",
    "תקליטי",
    "כתבי",
    "המשיכי",
    "הקליטי",
)
_CUSTOMER_MASCULINE_ONLY = (
    " אתה ",
    "ספר לי",
    "תקן אותי",
    "בוא נמשיך",
    "מה שאתה",
    "נסה שוב",
    "בחר מועד",
    "השב ",
)


def test_canned_customer_hebrew_addresses_both_genders() -> None:
    from app.domain.handoff import website_brief as brief

    blob = "\n".join(_all_canned_customer_replies())
    paste_lines = [
        value
        for name, value in vars(brief).items()
        if name.endswith("_LINE") and isinstance(value, str)
    ]
    blob += "\n" + "\n".join(paste_lines)
    for needle in _CUSTOMER_FEMININE_ONLY + _CUSTOMER_MASCULINE_ONLY:
        assert needle not in blob, needle
    assert "ספרו לי" in blob
    assert "אתם" in blob
    assert "בואו נמשיך" in blob
