"""Canned sales copy.

This table is the fallback, so it has to be good on its own: one question per reply,
no menus of options, nothing that reopens a topic the prospect already answered. The
reply port may rephrase these lines with conversation context; when it cannot, or when
the humanity linter rejects the candidate, this copy ships as-is.
"""

from app.domain.sales import (
    NextAction,
    ObjectionKind,
    SalesState,
    has_reframe_context,
)

LANG_HE = "he"
LANG_EN = "en"

_QUALIFY_DECISION_MAKER = "מי צריך להיות מעורב אם תחליטו לשנות את זה?"
_DOMAIN_QUESTION = "מה כתובת האתר של העסק?"

QUALIFY_REPLIES: dict[str, str] = {
    "decision_maker": _QUALIFY_DECISION_MAKER,
    "timeline": "מתי חשוב שזה ייפתר?",
    "metric": "מה זה עולה לכם בזמן או בלידים שאבדו?",
}

QUALIFY_REPLIES_EN: dict[str, str] = {
    "decision_maker": "Who else needs to be involved if you decide to change this?",
    "timeline": "When does this need to be solved?",
    "metric": "What is this costing you in time or in lost leads?",
}

WEBSITE_REPLIES: dict[NextAction, str] = {
    NextAction.UNDERSTAND_WORKFLOW: (
        "אני מיה, העוזרת של AssafWeb. ספרו לי קצת איך נראה יום רגיל בעסק. "
        "במה אתם רוב הזמן עסוקים?"
    ),
    NextAction.DEEPEN_PAIN: "איזה חלק בעבודה הזאת נעשה אצלכם ידנית?",
    NextAction.QUANTIFY: "כמה זמן זה לוקח לכם בפועל?",
    NextAction.REFLECT: "אז רוב העבודה הידנית מתרכזת בשלב הזה. זה מדויק?",
    NextAction.OFFER_HYPOTHESIS: (
        "נשמע שאפשר לקחת את זה מכם. רוצים שאסביר איך זה היה עובד?"
    ),
    NextAction.QUALIFY: _QUALIFY_DECISION_MAKER,
    NextAction.OFFER_MEETING: (
        "אפשר לקבוע שיחה קצרה עם אסף ולמפות את האוטומציה הראשונה. מתאים לכם?"
    ),
    NextAction.OFFER_WHATSAPP: (
        "יש פה משהו שכדאי להמשיך עליו. נוח לכם שאעביר אתכם לאסף בוואטסאפ? "
        "הוא יקבל את כל מה שסיפרתם לי כאן."
    ),
    NextAction.HANDOFF: "אסף צריך לקחת את זה מכאן.",
    NextAction.DISQUALIFY: (
        "כרגע אין פה התאמה, בלי לחץ. אני כאן אם זה ישתנה."
    ),
    NextAction.STOP: "אני משאירה את זה אצלכם. כתבו כשתרצו להמשיך.",
}

WEBSITE_REPLIES_EN: dict[NextAction, str] = {
    NextAction.UNDERSTAND_WORKFLOW: (
        "Tell me a bit about the business. What takes up most of your day?"
    ),
    NextAction.DEEPEN_PAIN: "Which part of that do you still do by hand?",
    NextAction.QUANTIFY: "How long does that actually take you?",
    NextAction.REFLECT: (
        "So most of the manual work lands on that one step. Is that right?"
    ),
    NextAction.OFFER_HYPOTHESIS: (
        "That sounds like something we can take off your hands. "
        "Want me to walk you through how?"
    ),
    NextAction.QUALIFY: QUALIFY_REPLIES_EN["decision_maker"],
    NextAction.OFFER_MEETING: (
        "We can set up a short call with Assaf and map the first automation. "
        "Does that work?"
    ),
    NextAction.OFFER_WHATSAPP: (
        "There's something here worth going deeper on. Want me to pass you to Assaf "
        "on WhatsApp? He'll get everything you've told me."
    ),
    NextAction.HANDOFF: "Assaf needs to take this from here.",
    NextAction.DISQUALIFY: (
        "This doesn't look like a fit right now, no pressure. I'm here if that changes."
    ),
    NextAction.STOP: "I'll leave it with you. Write whenever you want to pick it up.",
}

# Visitor copy after a website HANDOFF. complete_turn overwrites the canned/paraphrased
# line with one of these so Mia cannot claim a transfer Telegram never accepted.
HANDOFF_OWNER_NOTIFIED = "אסף קיבל את מה שסיפרתם כאן. הוא ייקח את זה מכאן."
HANDOFF_OWNER_UNREACHABLE = (
    "לא הצלחתי להעביר את זה לאסף עכשיו. כתבו שוב עוד רגע."
)
HANDOFF_OWNER_NOTIFIED_EN = "Assaf received what you shared here. He'll take it from here."
HANDOFF_OWNER_UNREACHABLE_EN = (
    "I could not pass this to Assaf just now. Please write again in a moment."
)

# Phrases that claim a completed transfer. Fail-closed copy must never contain these.
HANDOFF_LIE_MARKERS = (
    "מעבירים לאסף",
    "אעביר לו",
    "העברה כבר בוצעה",
    "I'll pass on the context",
    "I'll pass you",
)

# Second phrasing for a rung that has to be asked again because the previous answer
# was not usable. Never send the same line twice; that is the discovery loop.
WEBSITE_RETRY_REPLIES: dict[NextAction, str] = {
    NextAction.UNDERSTAND_WORKFLOW: "מה העסק עושה בפועל?",
    NextAction.DEEPEN_PAIN: "מה החלק שהכי גוזל לכם זמן?",
    NextAction.QUANTIFY: "זה קורה כל יום, או פעם בשבוע?",
    NextAction.REFLECT: "תקנו אותי אם פספסתי משהו.",
    NextAction.OFFER_HYPOTHESIS: "רוצים שאראה לכם איך זה נראה בפועל?",
    NextAction.QUALIFY: "מה הדבר שהיה עושה לכם את ההבדל הגדול?",
    NextAction.OFFER_WHATSAPP: "אפשר להעביר אתכם לאסף בוואטסאפ?",
}

WEBSITE_RETRY_REPLIES_EN: dict[NextAction, str] = {
    NextAction.UNDERSTAND_WORKFLOW: "What does the business actually do?",
    NextAction.DEEPEN_PAIN: "Which part of that eats the most time?",
    NextAction.QUANTIFY: "Is that a daily thing, or once a week?",
    NextAction.REFLECT: "Correct me if I've missed something.",
    NextAction.OFFER_HYPOTHESIS: "Want me to show you what that looks like in practice?",
    NextAction.QUALIFY: "What would make the biggest difference for you here?",
    NextAction.OFFER_WHATSAPP: "I can pass you to Assaf on WhatsApp.",
}

OBJECTION_REPLIES: dict[ObjectionKind, str] = {
    ObjectionKind.PRICE: "מה מרגיש יקר בדיוק?",
    ObjectionKind.PRICE_QUESTION: (
        "אין לי מספר מדויק לתת עכשיו. אעביר את זה לאסף."
    ),
    ObjectionKind.AI_TRUST: "מה החשש המרכזי אצלכם מזה?",
    ObjectionKind.NO_TIME: "החשש הוא זמן ההטמעה או שאין ראש לזה עכשיו?",
    ObjectionKind.HAS_VENDOR: "מה עובד היום, ואיפה זה עדיין נתקע?",
    ObjectionKind.NOT_URGENT: "אם לא משנים כלום, מה ממשיך לעלות לכם?",
    ObjectionKind.NEED_PARTNER: "מי עוד צריך להיות בשיחה כדי להחליט?",
}

OBJECTION_REPLIES_EN: dict[ObjectionKind, str] = {
    ObjectionKind.PRICE: "What exactly feels expensive here?",
    ObjectionKind.PRICE_QUESTION: (
        "I do not have an exact number to give from here. I will pass this to Assaf."
    ),
    ObjectionKind.AI_TRUST: "What's the main worry for you around that?",
    ObjectionKind.NO_TIME: "Is the worry the setup time, or just no headspace right now?",
    ObjectionKind.HAS_VENDOR: "What works today, and where does it still get stuck?",
    ObjectionKind.NOT_URGENT: "If nothing changes, what keeps costing you?",
    ObjectionKind.NEED_PARTNER: "Who else needs to be in the picture to decide?",
}

REFRAME_REPLIES: dict[ObjectionKind, str] = {
    ObjectionKind.PRICE: (
        "המחיר נמדד מול מה שתיארתם, לא במנותק. מה מרגיש יקר?"
    ),
    ObjectionKind.PRICE_QUESTION: "אין לי מספר מדויק לתת עכשיו. אעביר את זה לאסף.",
    ObjectionKind.AI_TRUST: (
        "מיה עובדת עם אישורים על פעולות רגישות, לא לבד. מה חייב להישאר אצלכם?"
    ),
    ObjectionKind.NO_TIME: (
        "המסלול הוא פיילוט קטן על השלב שתיארתם, לא פרויקט גדול. "
        "מה נראה לכם כצעד ראשון שאפשר לסחוב?"
    ),
    ObjectionKind.HAS_VENDOR: (
        "אין טעם להחליף סתם. איפה מה שיש היום עדיין תוקע אתכם?"
    ),
    ObjectionKind.NOT_URGENT: (
        "בלי לחץ מיותר: אם נשאר כמו היום, מה ממשיך לקרות בשלב שתיארתם?"
    ),
    ObjectionKind.NEED_PARTNER: (
        "אפשר לסכם את מה שברור לשיחה עם מי שמחליט. מי זה?"
    ),
}

REFRAME_REPLIES_EN: dict[ObjectionKind, str] = {
    ObjectionKind.PRICE: (
        "The cost is measured against the friction you described, not in the abstract. "
        "What feels expensive?"
    ),
    ObjectionKind.PRICE_QUESTION: (
        "I do not have an exact number to give from here. I will pass this to Assaf."
    ),
    ObjectionKind.AI_TRUST: (
        "Mia asks for approval on anything sensitive, she doesn't act alone. "
        "What has to stay with you?"
    ),
    ObjectionKind.NO_TIME: (
        "It's a small pilot on the step you described, not a big project. "
        "What would a first step look like?"
    ),
    ObjectionKind.HAS_VENDOR: (
        "No point switching for the sake of it. "
        "Where does what you have today leave you in the same spot?"
    ),
    ObjectionKind.NOT_URGENT: (
        "No manufactured urgency. If it stays as it is, what keeps happening at that step?"
    ),
    ObjectionKind.NEED_PARTNER: (
        "We can write up what's already clear for whoever decides. Who is that?"
    ),
}


def reply_for(
    _channel: str,
    action: NextAction,
    sales: SalesState | None = None,
    *,
    language: str = LANG_HE,
    repeat_ask: bool = False,
) -> str:
    english = language == LANG_EN
    if repeat_ask:
        table = WEBSITE_RETRY_REPLIES_EN if english else WEBSITE_RETRY_REPLIES
        retry = table.get(action)
        if retry is not None:
            return retry
    if action == NextAction.HANDLE_OBJECTION:
        reframe = has_reframe_context(sales)
        if english:
            replies = REFRAME_REPLIES_EN if reframe else OBJECTION_REPLIES_EN
        else:
            replies = REFRAME_REPLIES if reframe else OBJECTION_REPLIES
        if sales is not None and sales.active_objection is not None:
            return replies[sales.active_objection]
        return replies[ObjectionKind.PRICE]
    if action == NextAction.QUALIFY:
        table = QUALIFY_REPLIES_EN if english else QUALIFY_REPLIES
        if sales is None or not sales.missing_fields:
            return table["decision_maker"]
        field = sales.missing_fields[0]
        return table.get(field, table["decision_maker"])
    if action == NextAction.OFFER_MEETING:
        if sales is None or not sales.company_domain:
            # One question per reply: ask for the domain instead of "does that work",
            # so Assaf can arrive prepared.
            return (
                "We can set up a short call with Assaf and map the first automation. "
                "What's the business website?"
                if english
                else "אפשר לקבוע שיחה קצרה עם אסף ולמפות את האוטומציה הראשונה. "
                f"{_DOMAIN_QUESTION}"
            )
        return WEBSITE_REPLIES_EN[action] if english else WEBSITE_REPLIES[action]
    if english:
        return WEBSITE_REPLIES_EN[action]
    return WEBSITE_REPLIES[action]
