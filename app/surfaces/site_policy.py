"""Deterministic SITE Mia policy. No invented prices, metrics, JSON-LD, or GSC."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.domain.language import LANG_EN, LANG_HE, language_of, reply_language

SITE_ACTIONS = frozenset(
    {
        "ask_need",
        "ask_contact",
        "handoff",
        "no_price",
        "answer",
        "off_topic",
        "complaint",
        "identity",
        "confirm_contact",
        "voice_fail",
        "tool_status",
    }
)

BURST_WINDOW_S = 4.0
KNOWLEDGE_TOOL = "knowledge_search"
ASSAFWEB_HOSTS = frozenset({"www.assafweb.com", "assafweb.com"})

ASSAFWEB_HOOK_HE = (
    "ב-AssafWeb אסף בונה אתרים, אוטומציות וסוכני AI לעסקים."
)
ASSAFWEB_HOOK_EN = (
    "AssafWeb builds sites, automations, and AI agents for businesses."
)
CTA_ASSAF_HE = "רוצים שאעביר אתכם לאסף?"
CTA_ASSAF_EN = "Want me to pass you to Assaf?"
ASK_CONTACT_HE = "כדי שאסף יוכל להמשיך אתכם, צריך טלפון או אימייל."
ASK_CONTACT_EN = "To pass this to Assaf, I need a phone or email."
ASK_NEED_HE = "מה הכי חשוב לכם שנפתור קודם?"
ASK_NEED_EN = "What is the most important thing you need solved first?"
NO_PRICE_HE = "אין מחיר מפורסם באתר assafweb.com לתת כאן. אסף יגיד."
NO_PRICE_EN = (
    "There is no published price on assafweb.com I can give from here. Assaf will say."
)
CONFIRM_HE = "יש לי את המספר. מעבירה לאסף עכשיו."
CONFIRM_EN = "I have your number. I will ping Assaf now."
AFTER_PING_HE = (
    "תודה. העברתי לאסף את מה שסיפרתם, והוא ימשיך איתכם בוואטסאפ. "
    "אני לא ממציאה מחיר או התחייבות מכאן."
)
AFTER_PING_EN = (
    "Thanks. I passed this to Assaf, and he will continue with you on WhatsApp. "
    "I do not invent a price or a promise from here."
)
VOICE_FAIL_HE = "לא הצלחתי לשמוע. כתבו כאן ואני ממשיכה."
VOICE_FAIL_EN = "I could not hear that. Type here and I will keep going."
BOT_HE = (
    "כן, אני הסוכנת של אסף באתר. "
    "אני עונה ממה שפורסם ב-AssafWeb ומעבירה אליו כשיש טלפון או אימייל."
)
BOT_EN = (
    "Yes. I am Assaf's agent on this site. "
    "I answer from published AssafWeb facts and pass you to him once there is a phone or email."
)
COMPLAINT_ASK_HE = (
    "מצטערים שזה לא עבד. אסף צריך לקחת את זה. איך חוזרים אליכם, טלפון או אימייל?"
)
COMPLAINT_ASK_EN = (
    "Sorry this did not work. Assaf needs to take this. How should he reach you, phone or email?"
)
COMPLAINT_CONFIRM_HE = "מצטערים שזה לא עבד. מעבירה לאסף עכשיו. בלי מכירה מכאן."
COMPLAINT_CONFIRM_EN = (
    "Sorry this did not work. I am passing this to Assaf now. No selling from here."
)
OFF_TOPIC_JOKE_HE = "אין לי תחזית בכיס."
OFF_TOPIC_JOKE_EN = "I do not do weather."
ANSWER_HE = f"{ASSAFWEB_HOOK_HE} ספרו עוד על מה שצריך לפתור."
ANSWER_EN = f"{ASSAFWEB_HOOK_EN} Tell me more about what you need solved."
TOOL_NONE_HE = "לא רץ כאן כלי. אני לא ממציאה JSON-LD או נתוני Search Console."
TOOL_NONE_EN = "No tool ran here. I do not invent JSON-LD or Search Console numbers."
NO_GSC_HE = "לא רץ כלי JSON-LD או Search Console."
NO_GSC_EN = "No JSON-LD or Search Console tool ran."

_OFF_TOPIC = (
    "weather",
    "forecast",
    "temperature",
    "rain",
    "sunny",
    "מזג אוויר",
    "מזג האוויר",
    "גשם",
    "כמה מעלות",
    "חם בחוץ",
    "קר בחוץ",
    "horoscope",
    "lottery",
    "הורוסקופ",
    "לוטו",
)
_BOT = (
    "are you a bot",
    "are you human",
    "are you real",
    "are you ai",
    "את בוט",
    "את רובוט",
    "את בינה",
    "את אמיתית",
    "chatgpt",
    "האם את בוט",
)
_COMPLAINT = (
    "complaint",
    "i want to complain",
    "this is unacceptable",
    "not satisfied",
    "not happy",
    "terrible service",
    "תלונה",
    "לא מרוצה",
    "זה לא מקובל",
    "רוצה להתלונן",
    "שירות גרוע",
)
_PRICE = ("מחיר", "כמה עולה", "כמה זה עולה", "מה המחיר", "price", "cost", "how much")
_TOOL_STATUS = (
    "json-ld",
    "jsonld",
    "json ld",
    "gsc",
    "search console",
    "structured data",
    "schema.org",
    "did you run",
    "what tool",
    "which tool",
    "איזה כלי",
    "הרצת כלי",
)
_ASK_ASSAF = (
    "talk to assaf",
    "speak with assaf",
    "can i reach assaf",
    "connect me with assaf",
    "put me through",
    "לדבר עם אסף",
    "רוצה את אסף",
    "אפשר להגיע לאסף",
    "תחברו אותי לאסף",
    "תעבירו לאסף",
    "תעבירי לאסף",
    "speak to a human",
    "real person",
    "בן אדם",
    "נציג אנושי",
)
_NEED = (
    "website",
    "אתר",
    "automation",
    "אוטומציה",
    "clinic",
    "מרפאה",
    "leads",
    "לידים",
    "whatsapp",
    "וואטסאפ",
    "inventory",
    "מלאי",
    "need",
    "צריך",
    "צריכים",
    "רוצים",
    "looking for",
    "מחפשים",
)
_GREETING = ("hi", "hey", "hello", "היי", "שלום", "בוקר טוב", "ערב טוב")
_STOP_SELL = ("not interested", "לא מעוניין", "לא מעוניינים", "לא צריך")


@dataclass(frozen=True)
class PublishedFact:
    text: str
    url: str
    title: str = ""

    def from_assafweb(self) -> bool:
        host = (urlparse(self.url).hostname or "").lower()
        return host in ASSAFWEB_HOSTS


def facts_from_knowledge_hits(hits: list[object]) -> tuple[PublishedFact, ...]:
    """Keep only published assafweb.com rows. Never mint a price from other hosts."""
    facts: list[PublishedFact] = []
    for hit in hits:
        url = str(getattr(hit, "source_ref", "") or "")
        text = str(getattr(hit, "text", "") or "")
        title = str(getattr(hit, "label", "") or "")
        fact = PublishedFact(text=text, url=url, title=title)
        if fact.from_assafweb() and text.strip():
            facts.append(fact)
    return tuple(facts)


@dataclass(frozen=True)
class SiteDecision:
    reply: str
    action: str
    ask_contact: bool
    write_sheet: bool
    ping_assaf: bool
    stop_selling: bool
    confirm_contact: bool


def pick_language(text: str, prior: str = "") -> str:
    latest = language_of(text)
    if latest != "und":
        return latest
    if prior in {LANG_HE, LANG_EN}:
        return prior
    return reply_language(latest_message=text, default=LANG_HE)


def in_english(language: str) -> bool:
    return language == LANG_EN


def line(he: str, en: str, language: str) -> str:
    return en if in_english(language) else he


def stitch_burst(parts: list[tuple[float, str]]) -> str:
    """Join burst fragments into one thought."""
    return " ".join(text.strip() for _, text in parts if text.strip())


def append_burst(
    parts: list[tuple[float, str]],
    text: str,
    *,
    now: float,
    window_s: float = BURST_WINDOW_S,
) -> tuple[list[tuple[float, str]], str]:
    stripped = text.strip()
    if parts and now - parts[-1][0] > window_s:
        nxt = [(now, stripped)]
    else:
        nxt = [*parts, (now, stripped)]
    return nxt, stitch_burst(nxt)


def classify_site_intent(text: str) -> str:
    lowered = text.lower()
    blob = f"{text} {lowered}"
    if _has(blob, _COMPLAINT):
        return "complaint"
    if _has(blob, _BOT):
        return "bot"
    if _has(blob, _TOOL_STATUS):
        return "tool_status"
    if _has(blob, _PRICE):
        return "price"
    if _has(blob, _ASK_ASSAF):
        return "ask_assaf"
    if _has(blob, _OFF_TOPIC):
        return "off_topic"
    if _has(blob, _STOP_SELL):
        return "stop_sell"
    if _is_greeting(text):
        return "greeting"
    if _has(blob, _NEED):
        return "need"
    return "other"


def _is_greeting(text: str) -> bool:
    stripped = text.strip().lower().strip("!.?")
    return stripped in _GREETING or stripped in {"yo", "sup"}


def _has(blob: str, needles: tuple[str, ...]) -> bool:
    lowered = blob.lower()
    return any(needle in blob or needle in lowered for needle in needles)


def published_price_line(facts: tuple[PublishedFact, ...]) -> str:
    """Quote a published assafweb.com sentence. Never invent a number."""
    for fact in facts:
        if not fact.from_assafweb():
            continue
        text = " ".join(fact.text.split())
        if not text:
            continue
        if not _looks_like_price_fact(text):
            continue
        return text[:280]
    return ""


def _looks_like_price_fact(text: str) -> bool:
    lowered = text.lower()
    return any(
        mark in lowered
        for mark in ("price", "pricing", "fee", "cost", "מחיר", "תעריף", "עלות")
    )


def tool_status_reply(tools_ran: tuple[str, ...], language: str) -> str:
    named = [name for name in tools_ran if name.strip()]
    none = line(TOOL_NONE_HE, TOOL_NONE_EN, language)
    no_gsc = line(NO_GSC_HE, NO_GSC_EN, language)
    if not named:
        return none
    joined = ", ".join(named)
    if in_english(language):
        return f"I ran {joined}. {no_gsc}"
    return f"רץ {joined}. {no_gsc}"


def off_topic_reply(language: str) -> str:
    joke = line(OFF_TOPIC_JOKE_HE, OFF_TOPIC_JOKE_EN, language)
    hook = line(ASSAFWEB_HOOK_HE, ASSAFWEB_HOOK_EN, language)
    cta = line(CTA_ASSAF_HE, CTA_ASSAF_EN, language)
    return f"{joke} {hook} {cta}"


def decide_site_turn(
    *,
    thought: str,
    language: str,
    has_contact: bool,
    already_confirmed: bool,
    selling_stopped: bool,
    already_pinged: bool,
    facts: tuple[PublishedFact, ...] = (),
    tools_ran: tuple[str, ...] = (),
    voice_failed: bool = False,
) -> SiteDecision:
    """Pick one canned reply. Phone/email only when the next step is Assaf or Sheet."""
    if voice_failed:
        return SiteDecision(
            reply=line(VOICE_FAIL_HE, VOICE_FAIL_EN, language),
            action="voice_fail",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    intent = classify_site_intent(thought)
    if selling_stopped and intent not in {"complaint", "ask_assaf", "bot", "tool_status"}:
        return _assaf_or_confirm(
            language,
            has_contact=has_contact,
            already_confirmed=already_confirmed,
            already_pinged=already_pinged,
            stop_selling=True,
            complaint=True,
        )

    if intent == "complaint":
        return _assaf_or_confirm(
            language,
            has_contact=has_contact,
            already_confirmed=already_confirmed,
            already_pinged=already_pinged,
            stop_selling=True,
            complaint=True,
        )

    if intent == "bot":
        return SiteDecision(
            reply=line(BOT_HE, BOT_EN, language),
            action="identity",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent == "tool_status":
        return SiteDecision(
            reply=tool_status_reply(tools_ran, language),
            action="tool_status",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent == "price":
        published = published_price_line(facts)
        if published:
            cite = line(
                "מאתר assafweb.com:",
                "From assafweb.com:",
                language,
            )
            named = tool_status_reply(tools_ran, language) if tools_ran else ""
            reply = f"{cite} {published}"
            if named:
                reply = f"{reply} {named}"
            return SiteDecision(
                reply=reply,
                action="answer",
                ask_contact=False,
                write_sheet=False,
                ping_assaf=False,
                stop_selling=selling_stopped,
                confirm_contact=False,
            )
        named = f" {tool_status_reply(tools_ran, language)}" if tools_ran else ""
        return SiteDecision(
            reply=line(NO_PRICE_HE, NO_PRICE_EN, language) + named,
            action="no_price",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent == "off_topic":
        return SiteDecision(
            reply=off_topic_reply(language),
            action="off_topic",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent == "ask_assaf":
        return _assaf_or_confirm(
            language,
            has_contact=has_contact,
            already_confirmed=already_confirmed,
            already_pinged=already_pinged,
            stop_selling=selling_stopped,
            complaint=False,
        )

    if intent in {"need", "other"}:
        answer = _answer_from_facts(facts, language, tools_ran)
        if has_contact and not already_confirmed:
            # They already left a number. Answer first, then confirm once and ping.
            return SiteDecision(
                reply=f"{answer} {line(CONFIRM_HE, CONFIRM_EN, language)}",
                action="confirm_contact",
                ask_contact=False,
                write_sheet=True,
                ping_assaf=not already_pinged,
                stop_selling=selling_stopped,
                confirm_contact=True,
            )
        return SiteDecision(
            reply=answer,
            action="answer",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent == "stop_sell":
        return SiteDecision(
            reply=line(
                "בסדר. אני כאן אם תשתנה דעתכם.",
                "Understood. I am here if that changes.",
                language,
            ),
            action="answer",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=True,
            confirm_contact=False,
        )

    # Greeting or leftover: one question, unless they already left a number.
    if has_contact and not already_confirmed:
        return SiteDecision(
            reply=f"{line(ASK_NEED_HE, ASK_NEED_EN, language)} {line(CONFIRM_HE, CONFIRM_EN, language)}",
            action="confirm_contact",
            ask_contact=False,
            write_sheet=True,
            ping_assaf=not already_pinged,
            stop_selling=selling_stopped,
            confirm_contact=True,
        )
    return SiteDecision(
        reply=line(ASK_NEED_HE, ASK_NEED_EN, language),
        action="ask_need",
        ask_contact=False,
        write_sheet=False,
        ping_assaf=False,
        stop_selling=selling_stopped,
        confirm_contact=False,
    )


def _answer_from_facts(
    facts: tuple[PublishedFact, ...],
    language: str,
    tools_ran: tuple[str, ...],
) -> str:
    for fact in facts:
        if not fact.from_assafweb():
            continue
        text = " ".join(fact.text.split())
        if not text:
            continue
        cite = line("מאתר assafweb.com:", "From assafweb.com:", language)
        reply = f"{cite} {text[:280]}"
        if tools_ran:
            reply = f"{reply} {tool_status_reply(tools_ran, language)}"
        return reply
    base = line(ANSWER_HE, ANSWER_EN, language)
    if tools_ran:
        return f"{base} {tool_status_reply(tools_ran, language)}"
    return base


def _assaf_or_confirm(
    language: str,
    *,
    has_contact: bool,
    already_confirmed: bool,
    already_pinged: bool,
    stop_selling: bool,
    complaint: bool,
) -> SiteDecision:
    if has_contact:
        if already_confirmed:
            return SiteDecision(
                reply=line(AFTER_PING_HE, AFTER_PING_EN, language),
                action="handoff",
                ask_contact=False,
                write_sheet=True,
                ping_assaf=not already_pinged,
                stop_selling=stop_selling,
                confirm_contact=False,
            )
        reply = (
            line(COMPLAINT_CONFIRM_HE, COMPLAINT_CONFIRM_EN, language)
            if complaint
            else line(CONFIRM_HE, CONFIRM_EN, language)
        )
        return SiteDecision(
            reply=reply,
            action="confirm_contact",
            ask_contact=False,
            write_sheet=True,
            ping_assaf=not already_pinged,
            stop_selling=stop_selling,
            confirm_contact=True,
        )
    reply = (
        line(COMPLAINT_ASK_HE, COMPLAINT_ASK_EN, language)
        if complaint
        else line(ASK_CONTACT_HE, ASK_CONTACT_EN, language)
    )
    return SiteDecision(
        reply=reply,
        action="ask_contact",
        ask_contact=True,
        write_sheet=False,
        ping_assaf=False,
        stop_selling=stop_selling,
        confirm_contact=False,
    )
