import re

from app.domain.company import extract_explicit_company_domain
from app.domain.sales import (
    FitLevel,
    NextAction,
    ObjectionKind,
    PainLevel,
    SalesState,
    compute_missing_fields,
)

_MAX_DISCOVERY_TURNS = 99

_WORKFLOW = (
    "day",
    "process",
    "we use",
    "whatsapp",
    "calls",
    "leads",
    "customers",
    "clinic",
    "restaurant",
    "לקוחות",
    "בעסק",
    "תהליך",
    "וואטסאפ",
    "שיחות",
    "לידים",
    "פניות",
    "פנייה",
    "inquiries",
    "קליניקה",
    "מסעדה",
    "חנות",
    "טלפון",
    "נעליים",
    "מלאי",
    "shoes",
    "inventory",
)
_BUSINESS_FIT = (
    "clinic",
    "restaurant",
    "shop",
    "my business",
    "online store",
    "e-commerce",
    "ecommerce",
    "real estate",
    "realtor",
    "קליניקה",
    "מסעדה",
    "חנות",
    "העסק שלי",
    "נדלן",
    "נעליים",
    "shoes",
    "בגדים",
    "clothing",
)
_BUYING_INTENT = (
    "לבנות אתר",
    "לפתוח עסק",
    "מכירות באינטרנט",
    "יביא לי לידים",
    "דף נחיתה",
    "landing page",
    "want a website",
    "build a website",
    "open a business",
    "חנות בגדים",
    "עסק של בגדים",
    "לא קרה עדיין",
    "בשלב הרעיון",
)
_CHAT_TOO_LONG = (
    "ארוך השיחה",
    "זה ארוך",
    "ארוך מדי",
    "too long",
)
_PAIN = (
    "missed",
    "overwhelm",
    "too many",
    "no time",
    "losing",
    "forget",
    "abandoned",
    "chaos",
    "לא תמיד עונה",
    "נעלמים",
    "אין זמן",
    "עמוס",
    "פספס",
    "שוכח",
    "כאוס",
    "בלגן",
    "מבולגן",
    "על הפנים",
    "נשרף לי",
    "שיטס",
    "sheets",
    "spreadsheet",
    "ידנית",
    "manually",
)
_FRICTION_DETAIL = (
    "מידות",
    "דגמים",
    "כמויות",
    "sizes",
    "models",
    "quantities",
    "sku",
)
# A concrete repeated manual step, not just "I am busy".
_MANUAL_STEP = (
    "ידנית",
    "ידני",
    "מזין",
    "מזינה",
    "מקליד",
    "מקלידה",
    "מעתיק",
    "מעתיקה",
    "רושם",
    "רושמת",
    "להכניס",
    "מכניס",
    "מכניסה",
    "לעדכן",
    "מעדכן",
    "מעדכנת",
    "שיטס",
    "אקסל",
    "גוגל שיטס",
    "טבלה",
    "sheets",
    "spreadsheet",
    "excel",
    "manually",
    "by hand",
    "copy paste",
    "copy-paste",
    "data entry",
    "type it in",
    "retype",
    "update the sheet",
)
# Where the information arrives from before the manual step.
_DATA_SOURCE = (
    "ספק",
    "ספקים",
    "מהספק",
    "קטלוג",
    "חשבונית",
    "חשבוניות",
    "תעודת משלוח",
    "מייל",
    "אימייל",
    "וואטסאפ",
    "טלפון",
    "פקס",
    "מהמחסן",
    "מלאי פיזי",
    "supplier",
    "suppliers",
    "catalog",
    "catalogue",
    "invoice",
    "invoices",
    "packing slip",
    "email",
    "whatsapp",
    "phone",
    "warehouse",
    "pos",
    "csv",
)
# Short acknowledgements that carry no new fact.
_LOW_CONTENT = (
    "כן",
    "לא",
    "אוקיי",
    "אוקי",
    "בסדר",
    "סבבה",
    "יאללה",
    "תודה",
    "מה",
    "אה",
    "ok",
    "okay",
    "yes",
    "no",
    "sure",
    "thanks",
    "yep",
    "nope",
    "hi",
    "hello",
    "hey",
    "היי",
    "שלום",
    "אהלן",
)
_IMPACT = (
    "every day",
    "all day",
    "hours",
    "lost",
    "a week",
    "כל יום",
    "כל היום",
    "שעות",
    "נעלמים",
    "בשבוע",
)
_MEET = (
    "book",
    "meeting",
    "schedule",
    "let's talk",
    "פגישה",
    "לקבוע",
    "נדבר",
    "שיחה עם אסף",
)
_STOP = (
    "not interested",
    "no thanks",
    "stop messaging",
    "לא מעוניין",
    "לא מעוניינת",
    "תפסיק",
    "תפסיקי",
    "בי תודה",
    "ביי תודה",
    "תודה ביי",
    "bye thanks",
)
_POOR = (
    "student",
    "homework",
    "school project",
    "סטודנט",
    "שיעורים",
    "עבודת בית",
)
_AUTHORITY = (
    "i decide",
    "i'm the owner",
    "im the owner",
    "decision maker",
    "אני מחליט",
    "אני מחליטה",
    "אני הבעלים",
)
_TIMELINE = (
    "this quarter",
    "we need it by",
    "הרבעון",
    "עד סוף הרבעון",
    "עד סוף החודש",
)
_P4 = (
    "losing money",
    "lost revenue",
    "costs me",
    "money",
    "shekel",
    "עולה לי",
    "כסף",
    "שקל",
    "הכנסות",
)
_OWNER_REQUIRED = (
    "send me a proposal",
    "send a proposal",
    "send me a quote",
    "written proposal",
    "speak with assaf",
    "talk to assaf",
    "speak with אסף",
    "want a contract",
    "send a contract",
    "custom terms",
    "want to start",
    "let's start",
    "meeting to close",
    "special discount",
    "negotiate the price",
    "תשלחי הצעה",
    "הצעת מחיר",
    "מחיר מיוחד",
    "לדבר עם אסף",
    "רוצה את אסף",
    "תנאים מיוחדים",
    "רוצה להתחיל",
    "הנחה מיוחדת",
    "משא ומתן",
)

_OBJECTION_TOKENS: tuple[tuple[ObjectionKind, tuple[str, ...]], ...] = (
    (
        # Checked before PRICE so "too expensive" still reads as an objection while a
        # neutral "how much is it?" gets an answer instead of a defensive question.
        ObjectionKind.PRICE_QUESTION,
        (
            "how much does it cost",
            "how much is it",
            "how much would",
            "what's the price",
            "what is the price",
            "what does it cost",
            "pricing",
            "כמה זה עולה",
            "כמה עולה",
            "מה המחיר",
            "מחירון",
        ),
    ),
    (
        ObjectionKind.PRICE,
        (
            "too expensive",
            "that's expensive",
            "too pricey",
            "cost too much",
            "זה יקר",
            "יקר מדי",
            "אין תקציב",
        ),
    ),
    (
        ObjectionKind.AI_TRUST,
        (
            "don't trust ai",
            "ai mistake",
            "privacy",
            "פרטיות",
            "לא סומך על בינה",
            "לא סומך על ai",
        ),
    ),
    (
        ObjectionKind.NO_TIME,
        (
            "no time for this",
            "too busy to implement",
            "אין לי זמן לזה",
            "אין זמן לפרויקט",
        ),
    ),
    (
        ObjectionKind.HAS_VENDOR,
        (
            "already have",
            "we already use",
            "already working with",
            "כבר יש לנו",
            "כבר עובדים עם",
        ),
    ),
    (
        ObjectionKind.NOT_URGENT,
        (
            "not urgent",
            "not a priority",
            "maybe later",
            "לא דחוף",
            "לא עכשיו",
            "אין דחיפות",
        ),
    ),
    (
        ObjectionKind.NEED_PARTNER,
        (
            "need to ask",
            "ask my partner",
            "need approval",
            "צריך אישור",
            "צריך להתייעץ",
            "השותף",
        ),
    ),
)

_HEBREW_LETTER = "\u0590-\u05FF"


def _token_matches(text: str, token: str) -> bool:
    if any("\u0590" <= char <= "\u05FF" for char in token):
        pattern = rf"(?<![{_HEBREW_LETTER}]){re.escape(token)}(?![{_HEBREW_LETTER}])"
    else:
        pattern = rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _detect_objection(message: str) -> ObjectionKind | None:
    text = message.lower()
    for kind, tokens in _OBJECTION_TOKENS:
        for token in tokens:
            if _token_matches(text, token):
                return kind
    return None


def _message_has_token(text: str, tokens: tuple[str, ...]) -> bool:
    return any(_token_matches(text, token) for token in tokens)


def _message_has_p4_token(text: str) -> bool:
    for token in _P4:
        if any("\u0590" <= char <= "\u05FF" for char in token):
            if token in text:
                return True
        elif _token_matches(text, token):
            return True
    return False


def is_substantive_answer(message: str) -> bool:
    """True when a reply carries a fact, even as one imperfect word.

    Short-answer tolerance: `מלאי` answers the workflow question, `כן` does not.
    """
    stripped = message.strip()
    if not stripped:
        return False
    words = [word for word in re.split(r"\s+", stripped) if word]
    if len(words) >= 2:
        return True
    folded = stripped.lower().strip("?!.,:;\u05f4\u05f3\"'")
    return folded not in _LOW_CONTENT_SET


_LOW_CONTENT_SET = frozenset(token.lower() for token in _LOW_CONTENT)


def extract_sales_signals(state: SalesState, message: str) -> SalesState:
    """Deterministic signal update. Not an LLM. Conservative: never invent fit."""
    updated = state.model_copy()
    text = message.lower()
    if not text.strip():
        return updated
    if any(token in text for token in _POOR):
        updated.fit = FitLevel.POOR
        updated.active_objection = None
        return updated
    if any(token in text for token in _STOP):
        updated.willingness_to_meet = False
        updated.active_objection = None
        return updated
    updated.active_objection = _detect_objection(message)
    if any(token in text for token in _WORKFLOW) or len(message) >= 80:
        updated.workflow_known = True
        if updated.pain_level < PainLevel.P1:
            updated.pain_level = PainLevel.P1
    if any(token in text for token in _PAIN):
        updated.workflow_known = True
        if updated.pain_level < PainLevel.P2:
            updated.pain_level = PainLevel.P2
    if any(token in text for token in _BUYING_INTENT) or any(
        token in text for token in _CHAT_TOO_LONG
    ):
        updated.workflow_known = True
        updated.explicit_buying_intent = True
        if updated.pain_level < PainLevel.P2:
            updated.pain_level = PainLevel.P2
        if updated.fit == FitLevel.UNKNOWN:
            updated.fit = FitLevel.POSSIBLE
    if any(token in text for token in _FRICTION_DETAIL) and updated.workflow_known:
        if updated.pain_level >= PainLevel.P2:
            if updated.pain_level < PainLevel.P3:
                updated.pain_level = PainLevel.P3
        elif updated.pain_level < PainLevel.P2:
            updated.pain_level = PainLevel.P2
        updated.manual_step_known = True
    if any(token in text for token in _MANUAL_STEP):
        updated.workflow_known = True
        updated.manual_step_known = True
        if updated.pain_level < PainLevel.P2:
            updated.pain_level = PainLevel.P2
    if any(token in text for token in _IMPACT):
        updated.impact_confirmed = True
        if updated.pain_level < PainLevel.P3:
            updated.pain_level = PainLevel.P3
    if updated.workflow_known and any(token in text for token in _DATA_SOURCE):
        updated.data_source_known = True
    if is_substantive_answer(message):
        updated.discovery_turns = min(updated.discovery_turns + 1, _MAX_DISCOVERY_TURNS)
        # A concrete answer to the workflow question counts even when no keyword
        # matches. Without this, one-word answers re-trigger the opening question.
        if NextAction.UNDERSTAND_WORKFLOW.value in updated.asked_actions:
            updated.workflow_known = True
            if updated.pain_level < PainLevel.P1:
                updated.pain_level = PainLevel.P1
        if (
            NextAction.DEEPEN_PAIN.value in updated.asked_actions
            and updated.workflow_known
        ):
            updated.manual_step_known = True
        if (
            NextAction.QUANTIFY.value in updated.asked_actions
            and updated.manual_step_known
        ):
            updated.impact_confirmed = True
            if updated.pain_level < PainLevel.P2:
                updated.pain_level = PainLevel.P2
    if _message_has_token(text, _MEET):
        updated.willingness_to_meet = True
        if updated.fit == FitLevel.UNKNOWN:
            updated.fit = FitLevel.POSSIBLE
    if _message_has_token(text, _BUSINESS_FIT) and updated.fit == FitLevel.UNKNOWN:
        updated.fit = FitLevel.POSSIBLE
    if _message_has_token(text, _AUTHORITY):
        updated.authority_known = True
    if _message_has_token(text, _TIMELINE):
        updated.timeline_known = True
    updated.buying_reality_known = (
        updated.buying_reality_known
        or updated.authority_known
        or updated.timeline_known
    )
    if _message_has_p4_token(text) and updated.pain_level >= PainLevel.P3:
        updated.pain_level = PainLevel.P4
        updated.metric_known = True
    if _message_has_token(text, _TIMELINE) and updated.pain_level >= PainLevel.P3:
        updated.pain_level = PainLevel.P5
    if _message_has_token(text, _OWNER_REQUIRED):
        updated.owner_required = True
    if (
        updated.buying_reality_known
        and updated.pain_level >= PainLevel.P2
        and updated.fit == FitLevel.POSSIBLE
    ):
        updated.fit = FitLevel.GOOD
    if not updated.company_domain:
        domain = extract_explicit_company_domain(message)
        if domain:
            updated.company_domain = domain
    updated.missing_fields = compute_missing_fields(updated)
    return updated
