"""Deterministic Gmail search-query normalizer.

Gmail's search API AND-matches every bare (non-operator) term against message text.
The owner talks to Mia in natural Hebrew/English — "תבדקי את המייל של דניאל" — and if
that whole phrase is sent to Gmail verbatim, the function word ``של`` (which never
appears literally inside the message) zeroes out the match. No amount of prompt
engineering fixes this: the words the owner uses to *ask* for a search are not the
words that will be *in* the mail.

This module is a small, deterministic, pure-function utility — not a second model
hop, not a second agent, no I/O, no settings, no LLM call. It strips known
conversational filler (function words that essentially never appear as useful Gmail
search terms), optionally widens a single leftover name-like token into a
sender-or-text query, and can append at most one relative-time operator when the
owner's phrasing names one. Every rule is deliberately conservative: over-normalizing
a precise query into something unrelated is worse than leaving an imperfect one
alone, so operators, quoted text, email addresses, digits, and dates always survive
untouched, and a query that would normalize to nothing falls back to the original
raw text rather than searching for nothing.

Hebrew is agglutinative: the one-letter clitics ב/ל/מ/ה/ו/ש/כ attach directly to the
front of a word with no space (``במייל`` = ``ב`` + ``מייל``, "in the mail"). A
whole-token-only stopword match would let a stopword-plus-clitic combination like
``בתיבה`` ("in the inbox") slip through as if it were a real search term — or worse,
get wrapped into a fabricated ``from:`` sender filter. So Hebrew stopword matching is
clitic-aware: a token that isn't a stopword on its own is also checked with a
leading 1- or 2-character clitic prefix stripped off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_GMAIL_OPERATOR_RE = re.compile(
    r"(?i)\b(from|to|subject|is|in|has|label|after|before|newer_than|older_than|filename):"
)

_QUOTED_RE = re.compile(r'"[^"]*"')

# Hebrew and English conversational filler: words that carry the owner's intent
# ("check", "please", "the mail") but essentially never appear as literal search
# terms inside a real message. Matched case-insensitively, whole-token only — never
# as a substring, so stripping "from" never damages "fromage" or a real name.
#
# Hebrew clitics (ב/ל/מ/ה/ו/ש/כ) are handled separately by _strip_hebrew_clitics,
# not by adding every prefixed variant here.
_HEBREW_STOPWORDS = frozenset(
    {
        "של", "את", "לי", "מה", "יש", "האם", "כל", "על", "עם", "אם", "זה", "הזה",
        "רגע", "בבקשה", "תבדקי", "תבדוק", "תחפשי", "תחפש", "תראי", "תראה",
        "תפתחי", "תפתח", "מייל", "מיילים", "המייל", "אימייל", "דואר", "תיבה",
        "שלח", "שלחה", "ענה", "ענתה", "החזיר", "תשובה",
        # Conversational filler exposed by real owner phrasing (deliberately NOT
        # business nouns like invoice/contract/meeting/proposal equivalents).
        "היה", "היתה", "היו", "משהו", "מישהו", "חשוב", "חדש", "חדשה", "כלום",
        "עוד", "גם", "כבר", "אולי", "נכנס", "נכנסו", "הגיע", "הגיעו", "קיבלתי",
        "שלחו", "אותי", "אליי", "ממנו", "ממנה", "שלי", "האחרון", "האחרונה",
    }
)
_ENGLISH_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "my", "me", "i", "is", "are", "did", "do", "does",
        "can", "you", "please", "check", "find", "look", "show", "search", "read",
        "open", "get", "any", "anything", "something", "mail", "email", "emails",
        "inbox", "message", "messages", "from", "sent", "send", "reply", "replied",
        "answer", "answered", "back",
        # Conversational filler exposed by real owner phrasing (deliberately NOT
        # business nouns like invoice/contract/meeting/proposal).
        "was", "were", "new", "important", "arrive", "arrived", "came", "come",
        "got", "received", "there", "it", "that", "this", "last", "latest",
        "recent", "what", "whats", "who", "when", "and", "or", "for", "to", "in",
        "on", "at", "him", "her", "his", "hers", "mine",
    }
)
_STOPWORDS_CASEFOLDED = frozenset(
    word.casefold() for word in (_HEBREW_STOPWORDS | _ENGLISH_STOPWORDS)
)
# Multi-word Hebrew filler ("את ה") is checked as a literal substring pass before
# tokenizing, since whole-token splitting would never see it as one unit.
_MULTI_WORD_STOPWORDS = ("את ה",)

# The one-letter Hebrew clitics that attach directly to the front of a word with no
# space: ב (in/with) ל (to) מ (from) ה (the) ו (and) ש (that) כ (like/as). A token
# that isn't a stopword by itself is also checked with 1 or 2 of these leading
# characters stripped (covers both single clitics like ב+תיבה and the common
# two-letter combinations כש, בה, לה, ...).
_HEBREW_CLITIC_CHARS = frozenset("בלמהושכ")

# Trailing punctuation stripped from every token before matching and before
# emitting, so "?" / Hebrew geresh / gershayim never survive into the query and
# never mask a stopword (e.g. "במייל?" must be recognised as "במייל").
_TRAILING_PUNCT_CHARS = "?!.,;:׳״"

# Relative-time expressions, in priority order (first match wins; at most one
# operator is ever appended). Each pattern is a compiled, case-insensitive,
# whole-word regex so "todayish" or a name containing these letters is untouched.
# ``trigger_words`` lists the literal (casefolded) token(s) that make up the
# expression, so they can be deleted from the residual query once the expression
# has done its job of producing an operator — otherwise e.g. "today" both triggers
# newer_than:1d AND survives as a literal term that Gmail then AND-matches against
# the message text, guaranteeing zero results.
_RELATIVE_TIME_RULES: tuple[tuple[re.Pattern[str], str, frozenset[str]], ...] = (
    (re.compile(r"(?i)\bהיום\b"), "newer_than:1d", frozenset({"היום"})),
    (re.compile(r"(?i)\btoday\b"), "newer_than:1d", frozenset({"today"})),
    (re.compile(r"(?i)\bאתמול\b"), "newer_than:2d", frozenset({"אתמול"})),
    (re.compile(r"(?i)\byesterday\b"), "newer_than:2d", frozenset({"yesterday"})),
    (
        re.compile(r"(?i)\bהשבוע האחרון\b"),
        "newer_than:7d",
        frozenset({"השבוע", "האחרון"}),
    ),
    (re.compile(r"(?i)\bהשבוע\b"), "newer_than:7d", frozenset({"השבוע"})),
    (re.compile(r"(?i)\bthis week\b"), "newer_than:7d", frozenset({"this", "week"})),
    (re.compile(r"(?i)\bהחודש\b"), "newer_than:30d", frozenset({"החודש"})),
    (re.compile(r"(?i)\bthis month\b"), "newer_than:30d", frozenset({"this", "month"})),
)


@dataclass(frozen=True)
class NormalizedGmailQuery:
    """Result of :func:`normalize_gmail_query`.

    ``reason`` is one of: ``passthrough``, ``operators_present``,
    ``stopwords_stripped``, ``sender_or_text``, ``relative_time``, ``empty``.
    """

    query: str  # what to actually send to Gmail
    changed: bool  # True if normalization altered the raw input
    reason: str  # short machine tag for logs


def normalize_gmail_query(raw: str, *, now: datetime | None = None) -> NormalizedGmailQuery:
    """Normalize an owner-phrased Gmail search into something Gmail can actually match.

    ``now`` is accepted for interface symmetry with the rest of the domain layer's
    deterministic helpers; it is not consumed. Gmail's relative-time operators
    (``newer_than:1d`` and friends) are evaluated by Gmail itself at query time, so
    there is nothing here for a local clock to resolve.
    """
    del now
    stripped_raw = raw.strip()
    if not stripped_raw:
        return NormalizedGmailQuery(query=raw, changed=False, reason="empty")

    if _GMAIL_OPERATOR_RE.search(stripped_raw):
        return NormalizedGmailQuery(query=raw, changed=False, reason="operators_present")

    raw_tokens = _tokenize(stripped_raw)
    tokens = _clean_tokens(raw_tokens)
    punct_changed = tokens != raw_tokens

    after_stopwords, stopwords_removed = _strip_stopwords(tokens)

    relative_match = _detect_relative_time(stripped_raw)
    relative_operator = relative_match[0] if relative_match else None
    trigger_words = relative_match[1] if relative_match else frozenset()

    remaining = [
        token
        for token in after_stopwords
        if _is_forced_preserve(token) or token.casefold() not in trigger_words
    ]
    relative_appended = relative_operator is not None

    if not remaining:
        if relative_appended:
            return NormalizedGmailQuery(
                query=relative_operator, changed=True, reason="relative_time"
            )
        # Every token was recognised as a stopword — directly, via a clitic
        # prefix, or as pure punctuation. Never invent a from: filter here; a
        # broad-but-honest fallback to the raw text is always safer than a
        # confidently wrong sender search.
        return NormalizedGmailQuery(query=raw, changed=False, reason="empty")

    wrapped = False
    if len(remaining) == 1 and _looks_like_name(remaining[0]):
        core = f"(from:{remaining[0]} OR {remaining[0]})"
        wrapped = True
    else:
        core = " ".join(remaining)

    query = f"{core} {relative_operator}" if relative_appended else core

    if relative_appended:
        reason = "relative_time"
    elif wrapped:
        reason = "sender_or_text"
    elif stopwords_removed or punct_changed:
        reason = "stopwords_stripped"
    else:
        reason = "passthrough"

    changed = relative_appended or wrapped or stopwords_removed or punct_changed
    return NormalizedGmailQuery(query=query, changed=changed, reason=reason)


def _tokenize(text: str) -> list[str]:
    """Split on whitespace, keeping quoted substrings as one opaque token (with quotes)."""
    normalized = text
    for phrase in _MULTI_WORD_STOPWORDS:
        # Drop multi-word filler before tokenizing so it never survives as loose
        # single-word remnants (e.g. a bare "ה" left over from "את ה").
        normalized = re.sub(rf"(?<!\S){re.escape(phrase)}(?!\S)", " ", normalized)

    tokens: list[str] = []
    pos = 0
    for match in _QUOTED_RE.finditer(normalized):
        tokens.extend(normalized[pos : match.start()].split())
        tokens.append(match.group(0))
        pos = match.end()
    tokens.extend(normalized[pos:].split())
    return tokens


def _is_quoted(token: str) -> bool:
    return len(token) >= 2 and token.startswith('"') and token.endswith('"')


def _clean_tokens(tokens: list[str]) -> list[str]:
    """Strip trailing punctuation from every non-quoted token; drop any that go empty."""
    cleaned: list[str] = []
    for token in tokens:
        if _is_quoted(token):
            cleaned.append(token)
            continue
        stripped = token.rstrip(_TRAILING_PUNCT_CHARS)
        if stripped:
            cleaned.append(stripped)
    return cleaned


def _is_forced_preserve(token: str) -> bool:
    """Quoted text, email-shaped tokens, dates, and any digit always survive verbatim."""
    if _is_quoted(token):
        return True
    if "@" in token or "." in token:
        return True
    return any(char.isdigit() for char in token)


def _strip_hebrew_clitics(token: str) -> str | None:
    """If a leading 1- or 2-character Hebrew clitic strips down to a known
    stopword, return that remainder; otherwise return None.

    Covers a single clitic (``ב``+``תיבה`` -> ``תיבה``) and common two-letter
    combinations such as ``כש`` or a clitic followed by ``ה`` (``בה``/``לה``).
    Never strips below a 2-character remainder.
    """
    for prefix_len in (1, 2):
        if len(token) <= prefix_len:
            continue
        prefix = token[:prefix_len]
        if not all(ch in _HEBREW_CLITIC_CHARS for ch in prefix):
            continue
        remainder = token[prefix_len:]
        if len(remainder) >= 2 and remainder.casefold() in _STOPWORDS_CASEFOLDED:
            return remainder
    return None


def _is_stopword_token(token: str) -> bool:
    if token.casefold() in _STOPWORDS_CASEFOLDED:
        return True
    # Only attempt clitic-stripping when the token is not already a direct
    # stopword match, and never when it's a forced-preserve token.
    return _strip_hebrew_clitics(token) is not None


def _strip_stopwords(tokens: list[str]) -> tuple[list[str], bool]:
    kept: list[str] = []
    removed_any = False
    for token in tokens:
        if _is_forced_preserve(token) or not _is_stopword_token(token):
            kept.append(token)
            continue
        removed_any = True
    return kept, removed_any


def _looks_like_name(token: str) -> bool:
    return (
        token.isalpha()
        and len(token) >= 2
        and token.casefold() not in _STOPWORDS_CASEFOLDED
    )


def _detect_relative_time(text: str) -> tuple[str, frozenset[str]] | None:
    for pattern, operator, trigger_words in _RELATIVE_TIME_RULES:
        if pattern.search(text):
            return operator, trigger_words
    return None
