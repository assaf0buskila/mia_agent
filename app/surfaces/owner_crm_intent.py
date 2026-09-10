"""Small, request-local authorization check for owner CRM writes.

The model can choose ``crm_upsert`` after understanding a turn, but contact data,
provider output, or quoted instructions never authorize that mutation.
"""

from __future__ import annotations

import re
import unicodedata

_BLOCKQUOTE = re.compile(r"(?m)^\s*>[^\r\n]*$")
_FENCED = re.compile(r"```[\s\S]*?```")
_QUOTED = re.compile(
    r'"(?:[^"\\]|\\.)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'|'
    r'“[^”]*”|”[^“]*“|‘[^’]*’|’[^‘]*‘|„[^“]*“|«[^»]*»|‹[^›]*›|`[^`\r\n]*`'
)
_CONTACTS = re.compile(
    r"\b(?:contacts?|crm|google\s+sheets?|sheets?)\b|"
    r"איש(?:\s+ה)?קשר|אנשי\s+קשר|שיטס"
)
_ENGLISH_WRITE = re.compile(
    r"^\s*(?:(?:mia|please|can\s+you|could\s+you)\s+)?"
    r"(?:save|add|log|write|upsert|record|update)\b"
)
_HEBREW_WRITE = re.compile(
    r"^\s*(?:(?:מיה|בבקשה)\s+)?"
    r"(?:רשום|רשמי|תרשום|תרשמי|הוסף|הוסיפי|תעד|תעדי|עדכן|עדכני)(?=\s|$)"
)
_NEGATION = re.compile(
    r"\b(?:do\s+not|don't|dont|never|not|no\s+need\s+to)\b|"
    r"(?<![\u0590-\u05ff])(?:אל|לא|בלי)(?![\u0590-\u05ff])"
)
_QUESTION = re.compile(r"\b(?:should\s+i|can\s+i|do\s+i|who\s+is|what\s+about)\b")


def _has_unclosed_delimiter(text: str) -> bool:
    if text.count("```") % 2:
        return True
    remainder = text.replace("```", "")
    if remainder.count("`") % 2 or remainder.count('"') % 2:
        return True
    quote_text = re.sub(r"(?<=\w)[‘’](?=\w)", "", remainder)
    for opening, closing in (("“", "”"), ("‘", "’"), ("«", "»"), ("‹", "›")):
        if quote_text.count(opening) != quote_text.count(closing):
            return True
    return False


def is_explicit_owner_crm_write_intent(text: str) -> bool:
    """Return true only for one affirmative CRM write in the current turn."""
    raw = text or ""
    if _has_unclosed_delimiter(raw):
        return False
    masked = _BLOCKQUOTE.sub(" ", raw)
    masked = _FENCED.sub(" ", masked)
    masked = _QUOTED.sub(" ", masked)
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKD", masked)
        if unicodedata.category(char) != "Cf"
        and not unicodedata.category(char).startswith("M")
    ).casefold()
    # Treat typographic apostrophes inside unquoted English words like ASCII
    # apostrophes for negation matching. Paired curly quote data was masked above.
    normalized = normalized.replace("’", "'").replace("‘", "'")
    if _NEGATION.search(normalized) or _QUESTION.search(normalized):
        return False
    if "?" in normalized and not re.match(r"^\s*(?:please|can\s+you)\b", normalized):
        return False
    english = _ENGLISH_WRITE.match(normalized)
    hebrew = _HEBREW_WRITE.match(normalized)
    if english:
        return bool(_CONTACTS.search(normalized))
    # Hebrew contact verbs are conventionally unambiguous in this owner surface
    # (the established command is "תרשמי את ...").
    return bool(hebrew)
