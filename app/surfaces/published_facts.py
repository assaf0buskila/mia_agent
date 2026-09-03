"""Published AssafWeb facts for the visitor. No invented prices or metrics."""

from __future__ import annotations

# Facts only. No prices, dates, or funnel counts.
_FACTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("אתר", "website", "landing", "דף נחיתה", "דפי נחיתה"),
        "בונים אתרים ודפי נחיתה לעסקים. היקף ומחיר לא מפורסמים כאן.",
    ),
    (
        ("אוטומצ", "automation", "zapier", "make.com", "חיבור כלים"),
        "בונים אוטומציות שמחברות כלים ומקצרות עבודה ידנית. בלי מחיר מכאן.",
    ),
    (
        ("מה אתם", "מה אתה", "what do you", "what services", "מה השירות", "מה מציעים"),
        "אסף בונה אתרים ואוטומציות לעסקים. פרטים מדויקים ומחיר אצל אסף, לא כאן.",
    ),
)


def lookup_published_fact(text: str) -> str:
    """Return a published fact, or say it is missing. Never invent."""
    blob = text.casefold()
    for needles, fact in _FACTS:
        if any(needle in blob or needle in text for needle in needles):
            return fact
    return "אין לי עובדה מפורסמת על זה באתר. אסף ידבר איתכם על הפרטים."


def asks_product_question(text: str) -> bool:
    """A question about the product, not a request to be called."""
    lowered = text.casefold()
    needles = (
        "מה אתם",
        "מה אתה",
        "איך זה עובד",
        "איך עובד",
        "what do you",
        "what services",
        "מה השירות",
        "מה מציעים",
        "do you build",
        "מה בונים",
    )
    return any(needle in lowered or needle in text for needle in needles)
