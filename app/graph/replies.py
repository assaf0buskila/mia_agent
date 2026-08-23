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
    "timeline": "מתי זה יהיה חשוב שזה ייפתר?",
    "metric": "מה זה עולה לך בזמן או בלידים שאבדו?",
}

QUALIFY_REPLIES_EN: dict[str, str] = {
    "decision_maker": "Who else needs to be involved if you decide to change this?",
    "timeline": "When does this need to be solved?",
    "metric": "What is this costing you in time or in lost leads?",
}

WEBSITE_REPLIES: dict[NextAction, str] = {
    NextAction.UNDERSTAND_WORKFLOW: (
        "ספרו לי קצת איך נראה יום רגיל בעסק. במה אתם רוב הזמן עסוקים?"
    ),
    NextAction.DEEPEN_PAIN: "איזה חלק בעבודה הזאת נעשה אצלכם ידנית?",
    NextAction.QUANTIFY: "כמה זמן זה לוקח לך בפועל?",
    NextAction.REFLECT: "אז רוב העבודה הידנית מתרכזת בשלב הזה. זה מדויק?",
    NextAction.OFFER_HYPOTHESIS: (
        "נשמע כמו משהו שאפשר להוריד ממך. רוצה שאתאר איך זה היה עובד?"
    ),
    NextAction.QUALIFY: _QUALIFY_DECISION_MAKER,
    NextAction.OFFER_MEETING: (
        "אפשר לקבוע שיחה קצרה עם אסף ולמפות את האוטומציה הראשונה. מתאים לך?"
    ),
    NextAction.OFFER_WHATSAPP: (
        "יש פה משהו ששווה לפתוח יותר לעומק. נוח לך שאעביר אותך לאסף בוואטסאפ? "
        "הוא יקבל את כל מה שסיפרת לי כאן."
    ),
    NextAction.HANDOFF: "את זה עדיף להעביר ישירות לאסף. אעביר לו את ההקשר.",
    NextAction.DISQUALIFY: (
        "כרגע זה לא נראה כמו התאמה, בלי לחץ. אני כאן אם זה ישתנה."
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
    NextAction.HANDOFF: "This one is better straight from Assaf. I'll pass on the context.",
    NextAction.DISQUALIFY: (
        "This doesn't look like a fit right now, no pressure. I'm here if that changes."
    ),
    NextAction.STOP: "I'll leave it with you. Write whenever you want to pick it up.",
}

# Second phrasing for a rung that has to be asked again because the previous answer
# was not usable. Never send the same line twice; that is the discovery loop.
WEBSITE_RETRY_REPLIES: dict[NextAction, str] = {
    NextAction.UNDERSTAND_WORKFLOW: "מה העסק עושה בפועל?",
    NextAction.DEEPEN_PAIN: "מה החלק שהכי גוזל לך זמן שם?",
    NextAction.QUANTIFY: "זה קורה כל יום, או פעם בשבוע?",
    NextAction.REFLECT: "תקנו אותי אם פספסתי משהו.",
    NextAction.OFFER_HYPOTHESIS: "רוצה שאראה לך איך זה נראה בפועל?",
    NextAction.QUALIFY: "מה הדבר שהיה עושה לך את ההבדל הגדול פה?",
    NextAction.OFFER_WHATSAPP: "אפשר להעביר אותך לאסף בוואטסאפ?",
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
        "המחיר תלוי בהיקף. במה אתם רוב הזמן עסוקים בעסק?"
    ),
    ObjectionKind.AI_TRUST: "מה החשש המרכזי שלך סביב זה?",
    ObjectionKind.NO_TIME: "החשש הוא זמן ההטמעה או שאין ראש לזה עכשיו?",
    ObjectionKind.HAS_VENDOR: "מה עובד היום, ואיפה זה עדיין נתקע?",
    ObjectionKind.NOT_URGENT: "אם לא משנים כלום, מה ממשיך לעלות לך?",
    ObjectionKind.NEED_PARTNER: "מי עוד צריך להיות בתמונה כדי להחליט?",
}

OBJECTION_REPLIES_EN: dict[ObjectionKind, str] = {
    ObjectionKind.PRICE: "What exactly feels expensive here?",
    ObjectionKind.PRICE_QUESTION: (
        "Pricing depends on scope. What takes up most of your day in the business?"
    ),
    ObjectionKind.AI_TRUST: "What's the main worry for you around that?",
    ObjectionKind.NO_TIME: "Is the worry the setup time, or just no headspace right now?",
    ObjectionKind.HAS_VENDOR: "What works today, and where does it still get stuck?",
    ObjectionKind.NOT_URGENT: "If nothing changes, what keeps costing you?",
    ObjectionKind.NEED_PARTNER: "Who else needs to be in the picture to decide?",
}

REFRAME_REPLIES: dict[ObjectionKind, str] = {
    ObjectionKind.PRICE: (
        "ההשקעה נמדדת מול החיכוך שתיארת, לא מול כלי באוויר. מה מרגיש יקר?"
    ),
    ObjectionKind.PRICE_QUESTION: (
        "המחיר תלוי בהיקף. לפי מה שתיארת מדובר באוטומציה אחת ממוקדת. "
        "שאעביר את זה לאסף למספר מדויק?"
    ),
    ObjectionKind.AI_TRUST: (
        "מיה עובדת עם אישורים על פעולות רגישות, לא לבד. מה חייב להישאר אצלכם?"
    ),
    ObjectionKind.NO_TIME: (
        "המסלול הוא פיילוט קטן על השלב שתיארת, לא פרויקט גדול. "
        "מה נראה לך כצעד ראשון שאפשר לסחוב?"
    ),
    ObjectionKind.HAS_VENDOR: (
        "לא מחליפים לשם החלפה. איפה מה שיש היום משאיר אותך באותו חיכוך?"
    ),
    ObjectionKind.NOT_URGENT: (
        "בלי לחץ מלאכותי: אם נשאר כמו היום, מה ממשיך לקרות בשלב שתיארת?"
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
        "Pricing depends on scope. From what you described this is one focused "
        "automation. Want me to get an exact number from Assaf?"
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
