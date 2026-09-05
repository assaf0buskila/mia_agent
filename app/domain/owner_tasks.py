"""Deterministic owner voice-note task classification (no LLM, no execution)."""

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from app.domain.approvals import LEAD_ID_RE
from app.domain.gmail.drafts import parse_gmail_send_intent
from app.domain.gmail.summaries import THREAD_ID_RE
from app.domain.learning import InstructionKind, classify_instruction_kind
from app.domain.owner_calendar_writes import (
    parse_calendar_change_decision,
    parse_calendar_change_request,
)

_HEBREW_LETTER = "\u0590-\u05ff"

_REVIEW_PHRASES: tuple[str, ...] = (
    "lead review",
    "review lead",
    "tell me about lead",
    "tell me about the lead",
    "סקירת ליד",
    "מה המצב של הליד",
    "איפה הליד",
    "תספרי לי על ליד",
    "תספרי לי על הליד",
    "ספר לי על הליד",
    "תספר לי על הליד",
)

_CONTENT_IDEA_PHRASES: tuple[str, ...] = (
    "content ideas",
    "content idea",
    "רעיונות לתוכן",
    "רעיון לתוכן",
)

_GMAIL_SUMMARY_PHRASES: tuple[str, ...] = (
    "summarize email",
    "summarize thread",
    "email summary",
    "thread summary",
    "סיכום מייל",
    "סיכום שרשור",
    "סיכום האימייל",
)

_GMAIL_DRAFT_PHRASES: tuple[str, ...] = (
    "send email to",
    "draft email",
    "שלח מייל ל",
    "תשלחי מייל",
    "כתבי מייל ל",
    "טיוטת מייל",
)

_SEO_PHRASES: tuple[str, ...] = (
    "check seo",
    "search console",
    "google search console",
    "gsc",
    "ga4",
    "google analytics",
    "site traffic",
    "website traffic",
    "organic traffic",
    "organic search",
    "weak ctr",
    "which pages rank",
    "seo audit",
    "בדיקת seo",
    "קונסולת חיפוש",
    "גוגל אנליטיקס",
    "אנליטיקס אתר",
    "תנועה לאתר",
    "חיפוש אורגני",
    "בדוק seo",
    "ביקורת seo",
)

_CALENDAR_PHRASES: tuple[str, ...] = (
    "calendar availability",
    "what's free on my calendar",
    "whats free on my calendar",
    "free slots",
    "check my calendar",
    "זמינות ביומן",
    "מה פנוי ביומן",
    "מועדים פנויים",
    "תבדוק את היומן",
    "בדוק את היומן",
)

_OWNER_NOTIFY_PHRASES: tuple[str, ...] = (
    "booked meetings",
    "what got booked",
    "meeting notifications",
    "מה נקבע",
    "פגישות שנקבעו",
    "התראות פגישות",
)

_MEETING_BRIEF_PHRASES: tuple[str, ...] = (
    "meeting brief",
    "pre-meeting brief",
    "pre meeting brief",
    "תקציר פגישה",
    "בריף פגישה",
)

_HUMAN_TAKEOVER_RESUME_PHRASES: tuple[str, ...] = (
    "resume this lead",
    "release this lead",
    "mia can reply",
    "give this lead back to mia",
    "give back to mia",
    "let mia handle",
    "שחרר את הליד",
    "החזר למיאה",
    "תני למיאה",
    "תחזיר למיאה",
)

_HUMAN_TAKEOVER_PHRASES: tuple[str, ...] = (
    "human takeover",
    "take over this lead",
    "take over this conversation",
    "take over",
    "אני לוקח את הליד",
    "תפיסה אנושית",
)

_CONVERSATION_SCOPE_PHRASES: tuple[str, ...] = (
    "never automate",
    "do not automate",
    "don't automate",
    "mark this contact personal",
    "this is personal",
    "אל תאוטומטי",
    "סמן אישי",
    "זה אישי",
)

_HOT_LEADS_PHRASES: tuple[str, ...] = (
    "show hot leads",
    "hot leads",
    "who is hottest",
    "who's hottest",
    "לידים חמים",
    "מי הכי חם",
)

_PENDING_APPROVALS_PHRASES: tuple[str, ...] = (
    "what needs approval",
    "what's waiting for approval",
    "whats waiting for approval",
    "pending approvals",
    "waiting for approval",
    "מה מחכה לאישור",
    "מה מחכה לאישורי",
    "מה ממתין לאישור",
    "אישורים ממתינים",
    "מה צריך אישור",
)

_WEBSITE_CONVERSATIONS_PHRASES: tuple[str, ...] = (
    "analyze the website conversations",
    "website conversations",
    "site conversations",
    "השיחות מהאתר",
    "שיחות מהאתר",
    "השיחות באתר",
    "שיחות באתר",
    "תנתחי את השיחות",
    "תנתח את השיחות",
)

# "Check that with him" after Mia named a lead. It is a real instruction with a
# known subject, so answering "I could not classify that" is wrong. It is also an
# instruction to message a customer, so it confirms first and never sends.
_LEAD_OUTREACH_PHRASES: tuple[str, ...] = (
    "תבדקי איתו",
    "תבדקי איתה",
    "תבדוק איתו",
    "תשאלי אותו",
    "תשאלי אותה",
    "תשאל אותו",
    "תמשיכי איתו",
    "תמשיכי איתה",
    "תחזרי אליו",
    "תחזרי אליה",
    "תעקבי אחריו",
    "תעקבי אחריה",
    "check that with him",
    "check with him",
    "check with her",
    "ask him",
    "ask her",
    "follow up with him",
    "follow up with her",
)

_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "preference": (
        "from now on",
        "remember",
        "prefer",
        "preference",
        "never say",
        "always say",
        "that's wrong",
        "correction",
        "מעכשיו",
        "תזכרי",
        "תעדיפי",
        "אל תגידי",
        "תמיד תגידי",
        "זה לא נכון",
    ),
    "sales": (
        "follow-up",
        "follow up",
        "lead",
        "deal",
        "prospect",
        "ליד",
        "תעקבי",
        "תזכורת",
        "עסקה",
        "לקוח פוטנציאלי",
    ),
    "analytics": (
        "ctr",
        "instagram insights",
        "instagram content",
        "analyze instagram",
        "content performance",
        "ביצועי תוכן",
        "תובנות אינסטגרם",
        "האינסטגרם שלי",
    ),
    "research": (
        "research",
        "competitor",
        "look up",
        "lookup",
        "מחקר",
        "מתחרה",
        "מתחרים",
        "חפשי על",
    ),
    "linkedin": ("linkedin", "לינקדאין"),
    "support": (
        "invoice",
        "website down",
        "bug",
        "חשבונית",
        "האתר נפל",
        "האתר לא עולה",
        "האתר לא עובד",
        "תקלה באתר",
    ),
    "meeting_debrief": (
        "meeting summary",
        "we spoke",
        "call with",
        "סיכום פגישה",
        "פגישה",
        "דיברנו עם",
        "אחרי הפגישה",
        "סיכום השיחה",
    ),
    "approval": (
        "approve the proposal",
        "reject the proposal",
        "approve the quote",
        "reject the quote",
        "אשר את ההצעה",
        "דחה את ההצעה",
        "דחי את ההצעה",
        "אשר הצעת מחיר",
        "דחה הצעת מחיר",
    ),
    "daily_brief": (
        "daily brief",
        "daily summary",
        "what happened today",
        "סיכום יומי",
        "תמצית יומית",
        "מה קרה היום",
    ),
    "weekly_brief": (
        "weekly brief",
        "weekly summary",
        "סיכום שבועי",
        "תמצית שבועית",
    ),
}

_TYPE_LABELS_HE: dict[str, str] = {
    "preference": "העדפה",
    "sales": "מכירות",
    "analytics": "אנליטיקה",
    "research": "מחקר",
    "linkedin": "לינקדאין",
    "support": "תמיכה",
    "meeting_debrief": "סיכום פגישה",
    "approval": "אישור",
    "daily_brief": "סיכום יומי",
    "weekly_brief": "סיכום שבועי",
    "lead_review": "סקירת ליד",
    "content_idea": "רעיונות לתוכן",
    "gmail_summary": "סיכום מייל",
    "gmail_draft": "טיוטת מייל",
    "seo": "קידום אתר",
    "calendar": "יומן",
    "calendar_write": "שינוי יומן",
    "owner_notify": "התראות פגישות",
    "meeting_brief": "תקציר פגישה",
    "human_takeover": "תפיסה אנושית",
    "human_takeover_resume": "שחרור תפיסה",
    "conversation_scope": "סימון שיחה",
    "hot_leads": "לידים חמים",
    "pending_approvals": "אישורים ממתינים",
    "website_conversations": "שיחות מהאתר",
    "lead_outreach": "פנייה לליד",
    "owner_status": "סטטוס בעלים",
    "operator_snapshot": "תמונת מצב",
    "note": "פתק",
}


class OwnerTaskType(StrEnum):
    NOTE = "note"
    PREFERENCE = "preference"
    APPROVAL = "approval"
    SALES = "sales"
    ANALYTICS = "analytics"
    RESEARCH = "research"
    LINKEDIN = "linkedin"
    SUPPORT = "support"
    MEETING_DEBRIEF = "meeting_debrief"
    DAILY_BRIEF = "daily_brief"
    WEEKLY_BRIEF = "weekly_brief"
    LEAD_REVIEW = "lead_review"
    CONTENT_IDEA = "content_idea"
    GMAIL_SUMMARY = "gmail_summary"
    GMAIL_DRAFT = "gmail_draft"
    SEO = "seo"
    CALENDAR = "calendar"
    CALENDAR_WRITE = "calendar_write"
    OWNER_NOTIFY = "owner_notify"
    MEETING_BRIEF = "meeting_brief"
    HUMAN_TAKEOVER = "human_takeover"
    HUMAN_TAKEOVER_RESUME = "human_takeover_resume"
    CONVERSATION_SCOPE = "conversation_scope"
    HOT_LEADS = "hot_leads"
    PENDING_APPROVALS = "pending_approvals"
    WEBSITE_CONVERSATIONS = "website_conversations"
    LEAD_OUTREACH = "lead_outreach"
    OWNER_STATUS = "owner_status"
    OPERATOR_SNAPSHOT = "operator_snapshot"


class OwnerTaskDecision(BaseModel):
    task_type: OwnerTaskType
    needs_clarification: bool
    matched_types: list[str] = Field(default_factory=list)


def _token_in_text(text: str, token: str) -> bool:
    if any(ord(ch) > 127 for ch in token):
        pattern = rf"(?<![{_HEBREW_LETTER}]){re.escape(token)}(?![{_HEBREW_LETTER}])"
        return re.search(pattern, text) is not None
    haystack = text.lower()
    needle = token.lower()
    if " " in needle:
        return needle in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _phrase_in_text(text: str, phrase: str) -> bool:
    if phrase.isascii():
        return phrase.lower() in text.lower()
    return phrase in text


def _matches_lead_review(text: str) -> bool:
    if any(_phrase_in_text(text, phrase) for phrase in _REVIEW_PHRASES):
        return True
    if LEAD_ID_RE.search(text) is None:
        return False
    return _token_in_text(text, "review") or _token_in_text(text, "סקירה")


def _matches_lead_outreach(text: str) -> bool:
    """An instruction to reach out, but only once we know who it is about.

    Requiring the lead id means an unanchored pronoun still falls through to the
    normal Understanding Check instead of being aimed at whoever came up last.
    """
    if LEAD_ID_RE.search(text) is None:
        return False
    return any(_phrase_in_text(text, phrase) for phrase in _LEAD_OUTREACH_PHRASES)


def _matches_content_idea(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _CONTENT_IDEA_PHRASES)


def _matches_gmail_summary(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _GMAIL_SUMMARY_PHRASES)


def _matches_gmail_draft(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _GMAIL_DRAFT_PHRASES)


def _matches_seo(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _SEO_PHRASES)


def _matches_calendar(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _CALENDAR_PHRASES)


def _matches_owner_notify(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _OWNER_NOTIFY_PHRASES)


def _matches_meeting_brief(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _MEETING_BRIEF_PHRASES)


def _matches_human_takeover_resume(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _HUMAN_TAKEOVER_RESUME_PHRASES)


def _matches_human_takeover(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _HUMAN_TAKEOVER_PHRASES)


def _matches_conversation_scope(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _CONVERSATION_SCOPE_PHRASES)


def _matches_hot_leads(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _HOT_LEADS_PHRASES)


def _matches_pending_approvals(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _PENDING_APPROVALS_PHRASES)


def _matches_website_conversations(text: str) -> bool:
    return any(_phrase_in_text(text, phrase) for phrase in _WEBSITE_CONVERSATIONS_PHRASES)


def _has_gmail_summary_identifier(text: str) -> bool:
    return LEAD_ID_RE.search(text) is not None or THREAD_ID_RE.search(text) is not None


def _keyword_in_text(text: str, keyword: str) -> bool:
    """Match phrases as substrings; match single tokens on ASCII word boundaries."""
    haystack = text.lower()
    needle = keyword.lower()
    if " " in needle:
        return needle in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _matching_types(text: str) -> set[OwnerTaskType]:
    matched: set[OwnerTaskType] = set()
    for type_key, keywords in _TYPE_KEYWORDS.items():
        if any(_keyword_in_text(text, keyword) for keyword in keywords):
            matched.add(OwnerTaskType(type_key))
    return matched


def _hebrew_type_label(type_key: str) -> str:
    return _TYPE_LABELS_HE.get(type_key, type_key.replace("_", " "))


def _type_labels(types: list[str]) -> str:
    labels = [_hebrew_type_label(type_key) for type_key in types]
    if len(labels) == 2:
        return f"{labels[0]} או {labels[1]}"
    if len(labels) > 2:
        return ", ".join(labels[:-1]) + f" או {labels[-1]}"
    return labels[0] if labels else ""


_READ_COMBINE_TYPES: frozenset[OwnerTaskType] = frozenset(
    {
        OwnerTaskType.DAILY_BRIEF,
        OwnerTaskType.WEEKLY_BRIEF,
        OwnerTaskType.HOT_LEADS,
        OwnerTaskType.PENDING_APPROVALS,
        OwnerTaskType.WEBSITE_CONVERSATIONS,
        OwnerTaskType.CALENDAR,
        OwnerTaskType.OWNER_NOTIFY,
        OwnerTaskType.OWNER_STATUS,
    }
)


def _dedicated_matches(text: str) -> list[OwnerTaskDecision]:
    """Dedicated phrase hits in current exclusive-routing order."""
    has_lead_id = LEAD_ID_RE.search(text) is not None
    matches: list[OwnerTaskDecision] = []
    if _matches_lead_review(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.LEAD_REVIEW,
                needs_clarification=not has_lead_id,
                matched_types=["lead_review"],
            )
        )
    if _matches_content_idea(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.CONTENT_IDEA,
                needs_clarification=False,
                matched_types=["content_idea"],
            )
        )
    if _matches_gmail_draft(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.GMAIL_DRAFT,
                needs_clarification=False,
                matched_types=["gmail_draft"],
            )
        )
    if parse_gmail_send_intent(text) is not None:
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.APPROVAL,
                needs_clarification=False,
                matched_types=["approval"],
            )
        )
    if _matches_gmail_summary(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.GMAIL_SUMMARY,
                needs_clarification=not _has_gmail_summary_identifier(text),
                matched_types=["gmail_summary"],
            )
        )
    if _matches_seo(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.SEO,
                needs_clarification=False,
                matched_types=["seo"],
            )
        )
    if parse_calendar_change_request(text, default_timezone="Asia/Jerusalem") is not None:
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.CALENDAR_WRITE,
                needs_clarification=False,
                matched_types=["calendar_write"],
            )
        )
    elif parse_calendar_change_decision(text) is not None:
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.APPROVAL,
                needs_clarification=False,
                matched_types=["approval"],
            )
        )
    elif _matches_calendar(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.CALENDAR,
                needs_clarification=False,
                matched_types=["calendar"],
            )
        )
    if _matches_owner_notify(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.OWNER_NOTIFY,
                needs_clarification=False,
                matched_types=["owner_notify"],
            )
        )
    if _matches_meeting_brief(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.MEETING_BRIEF,
                needs_clarification=not has_lead_id,
                matched_types=["meeting_brief"],
            )
        )
    if _matches_pending_approvals(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.PENDING_APPROVALS,
                needs_clarification=False,
                matched_types=["pending_approvals"],
            )
        )
    if _matches_website_conversations(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.WEBSITE_CONVERSATIONS,
                needs_clarification=False,
                matched_types=["website_conversations"],
            )
        )
    if _matches_conversation_scope(text):
        digits = any(ch.isdigit() for ch in text)
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.CONVERSATION_SCOPE,
                needs_clarification=not digits,
                matched_types=["conversation_scope"],
            )
        )
    if _matches_hot_leads(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.HOT_LEADS,
                needs_clarification=False,
                matched_types=["hot_leads"],
            )
        )
    if _matches_human_takeover_resume(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.HUMAN_TAKEOVER_RESUME,
                needs_clarification=not has_lead_id,
                matched_types=["human_takeover_resume"],
            )
        )
    if _matches_human_takeover(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.HUMAN_TAKEOVER,
                needs_clarification=not has_lead_id,
                matched_types=["human_takeover"],
            )
        )
    if _matches_lead_outreach(text):
        matches.append(
            OwnerTaskDecision(
                task_type=OwnerTaskType.LEAD_OUTREACH,
                needs_clarification=True,
                matched_types=["lead_outreach"],
            )
        )
    return matches


def classify_owner_task(text: str) -> OwnerTaskDecision:
    dedicated = _dedicated_matches(text)
    scrubbed = LEAD_ID_RE.sub(" ", text)
    keywords = _matching_types(scrubbed)
    all_types = {item.task_type for item in dedicated} | keywords
    exclusive = {item for item in all_types if item not in _READ_COMBINE_TYPES}
    reads = {item for item in all_types if item in _READ_COMBINE_TYPES}

    if exclusive:
        # Keep first-match exclusive routing so a write/approval/takeover/scope
        # /outreach in the mix never collapses into a combined read snapshot.
        if dedicated:
            return dedicated[0]
        matched_types = sorted(task_type.value for task_type in keywords)
        if len(keywords) == 0 or len(keywords) >= 2:
            return OwnerTaskDecision(
                task_type=OwnerTaskType.NOTE,
                needs_clarification=True,
                matched_types=matched_types,
            )
        return OwnerTaskDecision(
            task_type=next(iter(keywords)),
            needs_clarification=False,
            matched_types=matched_types,
        )

    if len(reads) >= 2:
        return OwnerTaskDecision(
            task_type=OwnerTaskType.OPERATOR_SNAPSHOT,
            needs_clarification=False,
            matched_types=sorted(task_type.value for task_type in reads),
        )

    if dedicated:
        return dedicated[0]

    matched_types = sorted(task_type.value for task_type in keywords)
    if len(keywords) == 0 or len(keywords) >= 2:
        return OwnerTaskDecision(
            task_type=OwnerTaskType.NOTE,
            needs_clarification=True,
            matched_types=matched_types,
        )
    return OwnerTaskDecision(
        task_type=next(iter(keywords)),
        needs_clarification=False,
        matched_types=matched_types,
    )


_STATUS_EXACT: frozenset[str] = frozenset(
    {
        "status",
        "what's up",
        "whats up",
        "מה המצב",
        "מה קורה",
        "מה נשמע",
        "עדכון",
        "מה המצב מיה",
        "מה קורה מיה",
        "מה נשמע מיה",
    }
)

_GREETING_EXACT: frozenset[str] = frozenset(
    {
        "היי",
        "היי מיה",
        "שלום",
        "שלום מיה",
        "hello",
        "hello mia",
        "hi",
        "hi mia",
        "hey",
        "hey mia",
        "yo",
        "בוקר טוב",
        "ערב טוב",
        "צהריים טובים",
        "תודה",
        "thanks",
        "thank you",
        "ok",
        "okay",
        "אוקיי",
        "סבבה",
        "יופי",
        "hmm",
        "המ",
    }
)


def _normalize_owner_ping(text: str) -> str:
    return re.sub(r"[?!.,]+", "", text.strip()).strip().lower()


def _looks_like_chatter(text: str) -> bool:
    """True only for a greeting, ack, or status ping.

    Word-count chatter was a bug: `תבדקי את המייל` and `check my inbox` are
    three words and a real request. Those must reach the owner agent.
    """
    stripped = text.strip()
    if not stripped:
        return True
    compact = _normalize_owner_ping(stripped)
    return compact in _STATUS_EXACT or compact in _GREETING_EXACT


def promote_unclassified_text_to_status(
    decision: OwnerTaskDecision, *, inbound_source: str | None, text: str | None = None
) -> OwnerTaskDecision:
    """Greetings become a short hello. Unmatched requests stay a NOTE.

    The agent answers real requests in any phrasing. Snapshot/funnel/engine only
    fire on an explicit brief. Empty or failed audio stays on the Understanding
    Check so it never dumps a command menu and never executes a write.
    """
    stripped = (text or "").strip()
    if inbound_source == "audio" and not stripped:
        return decision
    if (
        decision.task_type == OwnerTaskType.NOTE
        and decision.needs_clarification
        and not decision.matched_types
    ):
        if text is None or _looks_like_chatter(text):
            return OwnerTaskDecision(
                task_type=OwnerTaskType.OWNER_STATUS,
                needs_clarification=False,
                matched_types=["owner_status"],
            )
        return decision
    return decision


def _format_due_at_he(due_at: str | None) -> str | None:
    if due_at is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_at):
        return None
    year, month, day = due_at.split("-")
    return f"{day}.{month}.{year}"


def ack_for_owner_task(
    decision: OwnerTaskDecision,
    *,
    due_at: str | None = None,
    condition: str | None = None,
    trigger: str | None = None,
    text: str | None = None,
    inbound_source: str | None = None,
) -> str:
    if decision.needs_clarification:
        if decision.task_type == OwnerTaskType.LEAD_REVIEW:
            return "מה שהבנתי: סקירת ליד. אני לא מבצעת כלום. מה מזהה הליד?"
        if decision.task_type == OwnerTaskType.GMAIL_SUMMARY:
            return "מה שהבנתי: סיכום מייל. אני לא מבצעת כלום. מה מזהה השרשור או הליד?"
        if decision.task_type == OwnerTaskType.GMAIL_DRAFT:
            return "מה שהבנתי: טיוטת מייל. אני לא שולחת בלי אישור. מה המייל, הנושא והתוכן?"
        if decision.task_type == OwnerTaskType.MEETING_BRIEF:
            return "מה שהבנתי: תקציר פגישה. אני לא מבצעת כלום. מה מזהה הליד?"
        if decision.task_type == OwnerTaskType.HUMAN_TAKEOVER:
            return "מה שהבנתי: תפיסה אנושית. אני לא מבצעת כלום. מה מזהה הליד?"
        if decision.task_type == OwnerTaskType.HUMAN_TAKEOVER_RESUME:
            return "מה שהבנתי: שחרור תפיסה. אני לא מבצעת כלום. מה מזהה הליד?"
        if decision.task_type == OwnerTaskType.CONVERSATION_SCOPE:
            return "מה שהבנתי: סימון שיחה. חסר מספר או lead_id. אני לא מבצעת כלום."
        if decision.task_type == OwnerTaskType.LEAD_OUTREACH:
            match = LEAD_ID_RE.search(text) if text is not None else None
            subject = f" של {match.group(0)}" if match is not None else ""
            return f"הבנתי. אכין את השאלה הבאה בשיחה{subject}, אבל לא אשלח בלי אישור שלך. נכון?"
        if text is not None and ("campaign" in text.lower() or "קמפיין" in text):
            return "ניהול ונתוני קמפיינים ממומנים אינם זמינים במיאה. לא נרשמה בקשה ולא בוצע שינוי."
        if decision.matched_types:
            labels = _type_labels(decision.matched_types)
            return f"מה שהבנתי: זה יכול להיות {labels}. אני לא מבצעת כלום. איזה משימה אתה רוצה?"
        if inbound_source == "audio":
            return "לא תפסתי את ההקלטה. אני לא מבצעת כלום."
        return "מה שהבנתי: לא הצלחתי לסווג את ההודעה. אני לא מבצעת כלום. תכתוב מה המשימה."
    if decision.task_type == OwnerTaskType.OWNER_STATUS:
        return "היי אסף, אני כאן."
    # Placeholders. The inbound handler replaces these with the real read so the
    # answer is data, not a promise to look it up.
    if decision.task_type == OwnerTaskType.OPERATOR_SNAPSHOT:
        return "בודקת תמונת מצב."
    if decision.task_type == OwnerTaskType.PENDING_APPROVALS:
        return "בודקת מה מחכה לאישור."
    if decision.task_type == OwnerTaskType.WEBSITE_CONVERSATIONS:
        return "בודקת את השיחות מהאתר."
    if decision.task_type == OwnerTaskType.PREFERENCE:
        kind = classify_instruction_kind(text) if text is not None else InstructionKind.PREFERENCE
        if kind == InstructionKind.CORRECTION:
            return "נשמר כהצעת תיקון. זה לא פעיל ולא ישנה פרומפטים בפרודקשן עד שתאשר."
        if kind == InstructionKind.BEHAVIOR_RULE:
            return "נשמר כהצעת כלל. זה לא פעיל ולא ישנה פרומפטים בפרודקשן עד שתאשר."
        return "נשמר כהצעת העדפה. זה לא פעיל ולא ישנה פרומפטים בפרודקשן עד שתאשר."
    type_label = _hebrew_type_label(decision.task_type.value)
    formatted_due = _format_due_at_he(due_at)
    if formatted_due is not None:
        message = f"נרשם כמשימת {type_label} ל־{formatted_due}. לא ביצעתי אותה."
    else:
        message = f"נרשם כמשימת {type_label}. לא ביצעתי אותה."
    if condition == "if_not_replied":
        message += " רק אם לא תהיה תשובה."
    if decision.task_type == OwnerTaskType.LINKEDIN:
        message += " לא אפרסם, לא אגיב ולא אשלח הודעות בלינקדאין."
    if decision.task_type == OwnerTaskType.SEO:
        message += " לא אשנה את האתר בלי אישור."
    return message
