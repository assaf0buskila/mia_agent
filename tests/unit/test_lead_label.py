"""Lead headlines: the "who is the watches guy?" fix.

Assaf asked that about a lead whose own conversation says he sells watches, and the console
answered with a stats dump — because every lead was listed as an opaque id plus state flags.
"""

from __future__ import annotations

import pytest
from app.domain.lead_label import (
    MAX_LABEL_CHARS,
    derive_headline,
    lead_display,
    sanitize_label,
)
from app.domain.memory import ConversationTurn as Turn


def test_a_business_description_becomes_a_label() -> None:
    assert "שעונים" in sanitize_label("אז אני מוכר שעונים ורוב הזמן בוואטסאפ")
    assert "קליניקה" in sanitize_label("יש לי קליניקה לפיזיותרפיה")


@pytest.mark.parametrize("filler", ["שלום", "היי", "כן", "hi", "ok", "   "])
def test_filler_produces_no_label(filler: str) -> None:
    assert sanitize_label(filler) == ""


@pytest.mark.parametrize(
    "text",
    [
        "תתקשר 052-3393768",
        "המייל שלי assaf@example.com",
        "זה עולה 5000 ₪",
        "תראה ב https://example.com/x",
        "הנחה של 20%",
    ],
)
def test_identifiers_prices_and_links_never_survive(text: str) -> None:
    """A label is shown in owner lists; it must not carry a phone, price or URL."""
    label = sanitize_label(text)
    assert "@" not in label
    assert "http" not in label
    assert "₪" not in label and "%" not in label
    assert not any(run.isdigit() for run in label)


def test_labels_are_length_capped() -> None:
    long_text = "אני מוכר שעונים יוקרתיים ומטפל בכל הפניות ידנית בוואטסאפ כל היום " * 3
    assert len(sanitize_label(long_text)) <= MAX_LABEL_CHARS


def test_headline_prefers_the_first_substantive_prospect_line() -> None:
    turns = [
        Turn(role="mia", text="ספרו לי קצת על העסק"),
        Turn(role="prospect", text="היי"),
        Turn(role="prospect", text="אני מוכר שעונים"),
        Turn(role="prospect", text="שעה ביום"),
    ]
    assert "שעונים" in derive_headline(turns)


def test_headline_ignores_what_mia_said() -> None:
    turns = [Turn(role="mia", text="אני מוכרת שעונים")]
    assert derive_headline(turns) == ""


def test_no_conversation_yields_no_headline() -> None:
    assert derive_headline([]) == ""


def test_display_keeps_the_full_lead_id() -> None:
    """Assaf references the id back to Mia, so it must stay whole and copyable."""
    shown = lead_display("lead_82f527e3be5e", "מוכר שעונים")
    assert shown.startswith("lead_82f527e3be5e")
    assert "מוכר שעונים" in shown


def test_display_degrades_to_the_id_alone() -> None:
    assert lead_display("lead_82f527e3be5e", "") == "lead_82f527e3be5e"
