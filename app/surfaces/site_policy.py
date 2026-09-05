"""Deterministic SITE Mia policy. No invented prices, metrics, JSON-LD, or GSC."""

from __future__ import annotations

import re
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
        "no_metric",
    }
)

BURST_WINDOW_S = 4.0
# A visitor who has answered this many questions has earned an offer, not another
# question. Six unanswered discovery turns is what made a real prospect type
# "נכשלת" and leave.
ASK_CONTACT_AFTER_TURNS = 4
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
OFF_TOPIC_JOKE_OTHER_HE = "אין לי כדור בדולח."
OFF_TOPIC_JOKE_OTHER_EN = "I left my crystal ball at home."
ANSWER_HE = f"{ASSAFWEB_HOOK_HE} ספרו עוד על מה שצריך לפתור."
ANSWER_EN = f"{ASSAFWEB_HOOK_EN} Tell me more about what you need solved."
TOOL_NONE_HE = "עניתי ממה שפורסם ב-AssafWeb. אין לי בדיקת תנועה או סימון מפה מכאן."
TOOL_NONE_EN = (
    "I answered from published AssafWeb facts. "
    "I do not invent traffic numbers from here."
)
NO_METRIC_HE = "אין לי את המספר הזה מכאן. אני לא ממציאה מדדים."
NO_METRIC_EN = "I do not have that number from here. I do not invent metrics."
VOICE_PRODUCT_HE = (
    "כן. ב-AssafWeb בונים סוכן קולי לאתר: מבקרים מדברים באתר שלכם, "
    "מיה ממירה לטקסט ועונה. ספרו מה האתר צריך לכסות."
)
VOICE_PRODUCT_EN = (
    "Yes. AssafWeb builds a voice agent for your site: visitors speak, "
    "Mia turns that into text and answers. Tell me what the site needs to cover."
)
WIDGET_STT_HE = "כן. קול כאן הופך לטקסט. אם ההקלטה לא נקלטה, כתבו."
WIDGET_STT_EN = "Yes. Voice here becomes text. If the recording did not capture, type."

_WEATHER = (
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
)
_OFF_TOPIC = (
    *_WEATHER,
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
# Question forms only. Bare "מחיר"/"price" also matches a visitor describing their
# OWN pricing work ("תמחור מחירים לוקח לי את היום"), which is a pain to sell into,
# not a request for our price.
_PRICE = (
    "כמה עולה",
    "כמה זה עולה",
    "כמה אתם גובים",
    "מה המחיר",
    "המחיר שלכם",
    "מחירון",
    "how much",
    "what do you charge",
    "the price",
    "your price",
    "pricing page",
    "what does it cost",
)
_METRIC = (
    "how many",
    "conversion",
    "roi",
    "impressions",
    "metrics",
    "funnel",
    "כמה לקוחות",
    "כמה לידים",
    "אחוז המרה",
    "מדדים",
    "כמה כניסות",
)
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
# The visitor is telling us the conversation is going badly. Asking one more
# discovery question is the worst possible next move.
_FRUSTRATED = (
    "נכשלת",
    "לא הבנת",
    "את לא מבינה",
    "לא עוזר",
    "לא עוזרת",
    "נמאס",
    "מספיק שאלות",
    "די עם השאלות",
    "לא רלוונטי",
    "you failed",
    "not helpful",
    "you don't understand",
    "you dont understand",
    "stop asking",
    "too many questions",
)
_VOICE_PRODUCT = (
    "סוכן קולי",
    "סוכנת קולית",
    "voice agent",
    "voice agents",
    "ai voice",
    "קולי לאתר",
    "קולית לאתר",
)
_VOICE_Q = (
    "understand voice",
    "שומעת אותי",
    "מבינה אותי כאן",
    "מבינים קול",
    "מבינה קול",
    "אתם מבינים קול",
    "איך מקליטים כאן",
    "ההקלטה לא נקלטה",
    "לא נקלטה ההקלטה",
    "mic fail",
)
VISITOR_TOOL_LEAKS = (
    "knowledge_search",
    "Search Console",
    "search console",
    "JSON-LD",
    "JSON LD",
    "json-ld",
    "jsonld",
)
_TOOL_STATUS_HE = re.compile(r"(^|[\s.])רץ(\s|$|[\.\,\!])")


# Mirrors `app.brain.schemas.KnowledgeCategory.PRICING`. Kept as a literal so this
# deterministic policy module does not depend on the brain package.
PRICING_CATEGORY = "pricing"


@dataclass(frozen=True)
class PublishedFact:
    text: str
    url: str
    title: str = ""
    # Assigned at ingest from the document heading. Authoritative for "is this a
    # published price"; the keyword check below is only a fallback for rows that
    # predate the category being carried through retrieval.
    category: str = ""

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
        category = str(getattr(hit, "category", "") or "")
        fact = PublishedFact(text=text, url=url, title=title, category=category)
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


# A bare acknowledgement. Deliberately exact-match only: "תודה" is filler, but
# "תודה, כמה עולה אתר?" is a price question and must still reach retrieval. Kept
# narrow on purpose -- words that can carry meaning inside the sales ladder ("כן",
# "בסדר", "מעולה") are NOT here, because skipping those would change what Mia asks
# next, and this is a cost gate, not a conversation gate.
_FILLER = frozenset(
    {
        "thanks",
        "thank you",
        "thanks a lot",
        "many thanks",
        "ty",
        "ok",
        "okay",
        "k",
        "got it",
        "תודה",
        "תודה רבה",
        "אוקיי",
        "אוקי",
        "סבבה",
    }
)
_FILLER_TRIM = " \t\n.!?,;:-–—…\"'"


def is_filler(text: str) -> bool:
    """True when the whole message is a bare acknowledgement worth no retrieval.

    Embeddings plus two table scans for "thanks" is money spent to answer nothing.
    """
    stripped = text.strip().strip(_FILLER_TRIM).strip().casefold()
    return bool(stripped) and stripped in _FILLER


def classify_site_intent(text: str) -> str:
    lowered = text.lower()
    blob = f"{text} {lowered}"
    if _has(blob, _COMPLAINT):
        return "complaint"
    if _has(blob, _BOT):
        return "bot"
    if _has(blob, _VOICE_PRODUCT):
        return "voice_product"
    if _has(blob, _TOOL_STATUS):
        return "tool_status"
    if _has(blob, _OFF_TOPIC):
        return "off_topic"
    if _has(blob, _PRICE):
        return "price"
    if _has(blob, _METRIC):
        return "metric"
    if _has(blob, _ASK_ASSAF):
        return "ask_assaf"
    if _has(blob, _STOP_SELL):
        return "stop_sell"
    if _is_greeting(text):
        return "greeting"
    if _has(blob, _VOICE_Q):
        return "voice_q"
    if _has(blob, _NEED):
        return "need"
    return "other"


def is_frustrated(text: str) -> bool:
    """The visitor said the conversation is failing. Stop interrogating them."""
    lowered = text.lower()
    return _has(f"{text} {lowered}", _FRUSTRATED)


def _is_greeting(text: str) -> bool:
    stripped = text.strip().lower().strip("!.?")
    return stripped in _GREETING or stripped in {"yo", "sup"}


def _has(blob: str, needles: tuple[str, ...]) -> bool:
    lowered = blob.lower()
    return any(needle in blob or needle in lowered for needle in needles)


def never_silent(reply: str, language: str) -> str:
    """Every seen visitor turn gets a visible line. Missing is allowed; silence is not."""
    stripped = scrub_visitor_reply(reply or "")
    if stripped:
        return stripped
    return line(ANSWER_HE, ANSWER_EN, language)


def scrub_visitor_reply(text: str) -> str:
    """Visitor copy never names tools, even if a model emits the slugs."""
    if not text:
        return ""
    cleaned = text.replace("\u2014", " ").replace("\u2013", " ")
    for leak in VISITOR_TOOL_LEAKS:
        cleaned = re.sub(re.escape(leak), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"לא\s+רץ\s+כלי[^.]*\.?", " ", cleaned)
    cleaned = re.sub(r"(^|[\s.])רץ(\s+\S+)?\.?", r"\1", cleaned)
    cleaned = re.sub(r"\bI ran\b[^.]*\.?", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"Did not run[^.]*\.?", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(^|\s)\.(?=[A-Za-z])", r"\1", cleaned)
    cleaned = _TOOL_STATUS_HE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split()).strip()
    return re.sub(r"^\.+\s*", "", cleaned).strip()


def tool_status_reply(tools_ran: tuple[str, ...], language: str) -> str:
    del tools_ran
    return line(TOOL_NONE_HE, TOOL_NONE_EN, language)


def _published_metric_line(facts: tuple[PublishedFact, ...]) -> str:
    """Quote a published assafweb.com metric sentence. Never invent a count."""
    for fact in facts:
        if not fact.from_assafweb():
            continue
        text = " ".join(fact.text.split())
        if not text:
            continue
        lowered = text.lower()
        if any(
            mark in lowered
            for mark in ("metric", "conversion", "roi", "clients", "מדד", "המרה", "לקוחות")
        ):
            return text[:280]
    return ""


def is_pricing_fact(fact: PublishedFact) -> bool:
    """Is this retrieved chunk a published price?

    The category the ingest already computed is authoritative. The keyword check
    stays only as a fallback for chunks retrieved without one — re-deriving this by
    substring is why Mia answered "there is no published price" while holding
    pricing.md: a priced chunk whose first 280 chars never say "מחיר" failed the test.
    """
    if fact.category.strip().lower() == PRICING_CATEGORY:
        return True
    return _looks_like_price_fact(fact.text)


def published_price_line(facts: tuple[PublishedFact, ...]) -> str:
    """Quote a published assafweb.com sentence. Never invent a number."""
    for fact in facts:
        if not fact.from_assafweb():
            continue
        text = " ".join(fact.text.split())
        if not text:
            continue
        if not is_pricing_fact(fact):
            continue
        return text[:280]
    return ""


def _looks_like_price_fact(text: str) -> bool:
    lowered = text.lower()
    return any(
        mark in lowered
        for mark in ("price", "pricing", "fee", "cost", "מחיר", "תעריף", "עלות")
    )


def off_topic_reply(language: str, thought: str = "") -> str:
    weather = _has(f"{thought} {thought.lower()}", _WEATHER)
    joke = (
        line(OFF_TOPIC_JOKE_HE, OFF_TOPIC_JOKE_EN, language)
        if weather
        else line(OFF_TOPIC_JOKE_OTHER_HE, OFF_TOPIC_JOKE_OTHER_EN, language)
    )
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
    complaint_open: bool = False,
    visitor_turns: int = 0,
    frustrated: bool = False,
    need_seen: bool = False,
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
    if complaint_open and intent not in {
        "complaint",
        "ask_assaf",
        "bot",
        "tool_status",
        "metric",
    }:
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
            return SiteDecision(
                reply=f"{cite} {published}",
                action="answer",
                ask_contact=False,
                write_sheet=False,
                ping_assaf=False,
                stop_selling=selling_stopped,
                confirm_contact=False,
            )
        return SiteDecision(
            reply=line(NO_PRICE_HE, NO_PRICE_EN, language),
            action="no_price",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent == "metric":
        published = _published_metric_line(facts)
        if published:
            cite = line("מאתר assafweb.com:", "From assafweb.com:", language)
            return SiteDecision(
                reply=f"{cite} {published}",
                action="answer",
                ask_contact=False,
                write_sheet=False,
                ping_assaf=False,
                stop_selling=selling_stopped,
                confirm_contact=False,
            )
        return SiteDecision(
            reply=line(NO_METRIC_HE, NO_METRIC_EN, language),
            action="no_metric",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent == "off_topic":
        return SiteDecision(
            reply=off_topic_reply(language, thought),
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

    if intent == "voice_product":
        return SiteDecision(
            reply=line(VOICE_PRODUCT_HE, VOICE_PRODUCT_EN, language),
            action="answer",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent == "voice_q":
        return SiteDecision(
            reply=line(WIDGET_STT_HE, WIDGET_STT_EN, language),
            action="answer",
            ask_contact=False,
            write_sheet=False,
            ping_assaf=False,
            stop_selling=selling_stopped,
            confirm_contact=False,
        )

    if intent in {"need", "other"}:
        answer = _answer_from_facts(facts, language, tools_ran)
        if (
            not has_contact
            and need_seen
            and (frustrated or visitor_turns >= ASK_CONTACT_AFTER_TURNS)
        ):
            # Stop interrogating. Either they told us it is going badly, or they have
            # answered enough that the next honest move is to offer Assaf.
            # `need_seen` gates it on an actual business need: a student asking about
            # a school project is not a lead and must never be asked for a phone.
            return SiteDecision(
                reply=line(ASK_CONTACT_HE, ASK_CONTACT_EN, language),
                action="ask_contact",
                ask_contact=True,
                write_sheet=False,
                ping_assaf=False,
                stop_selling=selling_stopped,
                confirm_contact=False,
            )
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
        need = line(ASK_NEED_HE, ASK_NEED_EN, language)
        confirm = line(CONFIRM_HE, CONFIRM_EN, language)
        return SiteDecision(
            reply=f"{need} {confirm}",
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
    del tools_ran
    for fact in facts:
        if not fact.from_assafweb():
            continue
        text = " ".join(fact.text.split())
        if not text:
            continue
        cite = line("מאתר assafweb.com:", "From assafweb.com:", language)
        return f"{cite} {text[:280]}"
    return line(ANSWER_HE, ANSWER_EN, language)


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
