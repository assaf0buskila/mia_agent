"""Ten SITE Mia demo upgrades. No invented prices, metrics, JSON-LD, or GSC."""

from __future__ import annotations

import re

from app.api.deps import get_transcription_port
from app.core.config import Settings
from app.integrations.transcribe import FakeTranscriptionPort
from app.main import app
from app.surfaces.crm import FakeContactsCrm
from app.surfaces.site import reset_site_book, run_site_turn, site_book, site_opening
from app.surfaces.site_policy import (
    KNOWLEDGE_TOOL,
    SITE_ACTIONS,
    VISITOR_TOOL_LEAKS,
    PublishedFact,
    append_burst,
    classify_site_intent,
    decide_site_turn,
    facts_from_knowledge_hits,
    pick_language,
    published_price_line,
    scrub_visitor_reply,
    tool_status_reply,
)
from fastapi.testclient import TestClient


class _Hit:
    def __init__(self, text: str, source_ref: str, label: str = "") -> None:
        self.text = text
        self.source_ref = source_ref
        self.label = label


def _turn(
    session_id: str,
    text: str,
    *,
    phone: str = "",
    email: str = "",
    name: str = "",
    facts: tuple[PublishedFact, ...] = (),
    tools_ran: tuple[str, ...] = (),
    now: float | None = None,
    voice_failed: bool = False,
    settings: Settings | None = None,
) -> object:
    book = site_book()
    if book.get(session_id) is None:
        book.open(session_id)
    return run_site_turn(
        session_id=session_id,
        text=text,
        settings=settings or Settings(),
        crm=FakeContactsCrm(),
        name=name,
        phone=phone,
        email=email,
        book=book,
        facts=facts,
        tools_ran=tools_ran,
        now=now,
        voice_failed=voice_failed,
    )


def _assert_no_visitor_tool_leak(text: str) -> None:
    lowered = text.lower()
    for leak in VISITOR_TOOL_LEAKS:
        assert leak.lower() not in lowered
    assert re.search(r"(^|[\s.])רץ(\s|$)", text) is None
    assert ".Search Console" not in text
    assert "—" not in text
    assert "אפשר להקליט כאן" not in text


def setup_function() -> None:
    reset_site_book()


def test_off_topic_weather_is_one_joke_then_assafweb_hook_and_cta() -> None:
    he = _turn("web_off_he", "מה מזג האוויר מחר?")
    en = _turn("web_off_en", "What's the weather in Tel Aviv?")
    assert he.next_action == "off_topic"
    assert en.next_action == "off_topic"
    assert "תחזית" in he.reply
    assert "AssafWeb" in he.reply
    assert "אסף" in he.reply
    assert he.reply.count("?") == 1
    assert "weather" in en.reply.lower()
    assert "AssafWeb" in en.reply
    assert "Assaf" in en.reply
    assert en.reply.count("?") == 1
    assert "api" not in he.reply.lower()
    assert "api" not in en.reply.lower()
    assert "°" not in he.reply
    assert "celsius" not in en.reply.lower()
    assert he.crm_wrote is False
    assert he.whatsapp_url is None


def test_match_visitor_language() -> None:
    assert pick_language("What's the weather") == "en"
    assert pick_language("מה מזג האוויר") == "he"
    en = _turn("web_lang_en", "Are you a bot?")
    he = _turn("web_lang_he", "האם את בוט?")
    assert "Assaf" in en.reply
    assert "I am Assaf" in en.reply
    assert "סוכנת" in he.reply
    assert "אסף" in he.reply


def test_answer_first_phone_only_when_next_step_is_assaf_or_sheet() -> None:
    need = _turn("web_ans1", "צריכים אתר לעסק")
    assert need.next_action == "answer"
    assert need.crm_wrote is False
    assert need.whatsapp_url is None
    assert "טלפון" not in need.reply
    assert "אימייל" not in need.reply
    assert "AssafWeb" in need.reply or "אתרים" in need.reply
    assaf = _turn("web_ans1", "אפשר להגיע לאסף?")
    assert assaf.next_action == "ask_contact"
    assert "טלפון" in assaf.reply or "אימייל" in assaf.reply


def test_number_already_in_session_confirms_once_then_pings() -> None:
    settings = Settings().model_copy(
        update={"telegram_owner_user_ids": "550077", "whatsapp_click_to_chat": "972501111111"}
    )
    first = _turn(
        "web_num1",
        "צריכים אתר",
        phone="0501234567",
        name="דנה",
        settings=settings,
    )
    session = site_book().get("web_num1")
    assert first.next_action == "confirm_contact"
    assert first.crm_wrote is True
    assert session is not None
    assert session.confirmed is True
    assert session.awaiting_ping is True
    assert "טלפון או אימייל" not in first.reply
    assert "מעבירה לאסף" in first.reply or "יש לי את המספר" in first.reply
    again = _turn(
        "web_num1",
        "עוד שאלה על האתר",
        phone="0501234567",
        settings=settings,
    )
    assert "טלפון או אימייל" not in again.reply
    assert again.owner_pinged is False
    assert site_book().get("web_num1").confirmed is True


def test_http_confirm_pings_assaf_once(monkeypatch) -> None:
    from app.api.deps import get_telegram_port
    from app.integrations.base import RecordingMessagePort

    port = RecordingMessagePort()
    app.dependency_overrides[get_telegram_port] = lambda: port
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "111")
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            first = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "צריכים אתר", "phone": "0501234567", "name": "דנה"},
            )
            assert first.json()["next_action"] == "confirm_contact"
            assert port.sent
            # A structured brief, not a clipped transcript: Assaf gets the captured
            # fields, what is missing, and what to do next.
            ping = port.sent[0].text
            assert "ליד חדש מהאתר" in ping
            assert "שם: דנה" in ping
            assert "טלפון: 0501234567" in ping
            assert "המלצה:" in ping
            assert "חסר:" in ping
            assert "0501234567" not in str(first.json())
            second = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "עוד שאלה", "phone": "0501234567"},
            )
            assert "טלפון או אימייל" not in second.json()["message"]
            assert len(port.sent) == 1
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)


def test_complaint_no_jokes_offer_assaf_capture_identity_stop_selling() -> None:
    first = _turn("web_cmp1", "This is unacceptable. I want to complain.")
    assert first.next_action == "complaint" or first.next_action == "ask_contact"
    assert "sorry" in first.reply.lower() or "מצטערים" in first.reply
    assert "joke" not in first.reply.lower()
    assert "תחזית" not in first.reply
    assert "weather" not in first.reply.lower()
    assert "Assaf" in first.reply or "אסף" in first.reply
    assert "phone" in first.reply.lower() or "email" in first.reply.lower()
    weather = _turn("web_cmp1", "What's the weather?")
    assert weather.next_action != "off_topic"
    assert "joke" not in weather.reply.lower()
    assert "תחזית" not in weather.reply
    assert "Assaf" in weather.reply or "אסף" in weather.reply


def test_prices_only_from_assafweb_else_assaf_will_say() -> None:
    empty = _turn("web_pr1", "כמה עולה?")
    assert empty.next_action == "no_price"
    assert "אסף יגיד" in empty.reply
    assert not any(ch.isdigit() for ch in empty.reply)
    foreign = PublishedFact(
        text="Our starter plan is 9999 USD a month.",
        url="https://example.com/pricing",
    )
    ignored = _turn("web_pr2", "what's the price", facts=(foreign,))
    assert ignored.next_action == "no_price"
    assert "9999" not in ignored.reply
    assert "Assaf will say" in ignored.reply
    published = PublishedFact(
        text="Starter automations begin at a fixed monthly fee, no setup cost.",
        url="https://www.assafweb.com/pricing.md",
    )
    quoted = _turn("web_pr3", "what's the price", facts=(published,))
    assert "fixed monthly fee" in quoted.reply
    assert "assafweb.com" in quoted.reply
    assert "9999" not in quoted.reply
    assert published_price_line((foreign,)) == ""
    assert "fixed monthly fee" in published_price_line((published,))


def test_are_you_a_bot_is_honest_assaf_agent() -> None:
    turn = _turn("web_bot1", "Are you a bot?")
    assert turn.next_action == "identity"
    assert "Assaf's agent" in turn.reply
    assert "published AssafWeb" in turn.reply or "AssafWeb" in turn.reply
    assert turn.reply.count(".") >= 1
    he = _turn("web_bot2", "האם את בוט?")
    assert "סוכנת" in he.reply
    assert "אסף" in he.reply


def test_voice_fail_says_so_offers_type_does_not_hang() -> None:
    book = site_book()
    book.open("web_vf1")
    turn = run_site_turn(
        session_id="web_vf1",
        text="",
        settings=Settings(),
        crm=FakeContactsCrm(),
        book=book,
        voice_failed=True,
    )
    assert turn.next_action == "voice_fail"
    assert "לא הצלחתי לשמוע" in turn.reply or "could not hear" in turn.reply.lower()
    assert "כתבו" in turn.reply or "type" in turn.reply.lower()
    follow = _turn("web_vf1", "צריכים אתר")
    assert follow.next_action == "answer"
    assert follow.reply


def test_voice_http_stt_fail_returns_200_and_keeps_session() -> None:
    class _Boom(FakeTranscriptionPort):
        async def transcribe(self, **kwargs: object) -> object:
            raise RuntimeError("stt down")

    app.dependency_overrides[get_transcription_port] = lambda: _Boom("x")
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            reply = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                files={"file": ("note.webm", b"fake-webm-bytes", "audio/webm")},
            )
            assert reply.status_code == 200
            body = reply.json()
            assert body["next_action"] == "voice_fail"
            assert body["heard"] == ""
            assert body["message"]
            assert "כתבו" in body["message"] or "type" in body["message"].lower()
            typed = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "We need a website"},
            )
            assert typed.status_code == 200
            assert typed.json()["next_action"] in SITE_ACTIONS
            assert typed.json()["message"]
    finally:
        app.dependency_overrides.pop(get_transcription_port, None)


def test_stitch_site_message_bursts_into_one_thought() -> None:
    parts, thought = append_burst([], "what's", now=1.0)
    parts, thought = append_burst(parts, "the weather", now=1.4)
    parts, thought = append_burst(parts, "in tel aviv", now=1.8)
    assert thought == "what's the weather in tel aviv"
    assert classify_site_intent(thought) == "off_topic"
    later, later_thought = append_burst(parts, "צריכים אתר", now=20.0)
    assert later_thought == "צריכים אתר"
    assert classify_site_intent(later_thought) == "need"
    first = _turn("web_burst1", "what's", now=100.0)
    second = _turn("web_burst1", "the weather in tel aviv", now=100.5)
    assert second.next_action == "off_topic"
    assert "AssafWeb" in second.reply
    assert first.next_action in SITE_ACTIONS


def test_tool_honesty_names_what_ran_and_never_invents_jsonld_or_gsc() -> None:
    none = _turn("web_tool1", "Did you run a JSON-LD or GSC check?")
    assert none.next_action == "tool_status"
    assert none.reply
    _assert_no_visitor_tool_leak(none.reply)
    assert "clicks" not in none.reply.lower()
    assert "impressions" not in none.reply.lower()
    ran = _turn(
        "web_tool2",
        "what tool did you run",
        tools_ran=(KNOWLEDGE_TOOL,),
    )
    _assert_no_visitor_tool_leak(ran.reply)
    foreign = facts_from_knowledge_hits(
        [_Hit("secret price 12", "https://other.example/gsc", "gsc")]
    )
    assert foreign == ()
    keep = facts_from_knowledge_hits(
        [_Hit("Published fee, no setup cost.", "https://www.assafweb.com/pricing.md", "pricing")]
    )
    assert len(keep) == 1
    named = tool_status_reply((), "en")
    _assert_no_visitor_tool_leak(named)
    assert "invent" in named.lower() or "published" in named.lower()
    assert decide_site_turn(
        thought="check my search console clicks",
        language="en",
        has_contact=False,
        already_confirmed=False,
        selling_stopped=False,
        already_pinged=False,
        tools_ran=(),
    ).action == "tool_status"


def test_site_actions_stay_closed() -> None:
    assert "offer_meeting" not in SITE_ACTIONS
    assert classify_site_intent("what's the weather") == "off_topic"
    assert classify_site_intent("Are you a bot?") == "bot"
    assert classify_site_intent("כמה עולה") == "price"
    assert classify_site_intent("json-ld") == "tool_status"


def test_opening_does_not_ask_identity() -> None:
    opening = site_opening()
    assert opening
    assert "טלפון" not in opening
    assert "אימייל" not in opening
    assert "phone" not in opening.lower()
    assert "email" not in opening.lower()


def test_greeting_with_number_confirms_once_and_never_silent() -> None:
    turn = _turn("web_hi_num", "hi", phone="0501234567")
    assert turn.next_action == "confirm_contact"
    assert turn.reply
    assert "טלפון או אימייל" not in turn.reply
    empty_voice = _turn("web_hi_num", "", voice_failed=True)
    assert empty_voice.next_action == "voice_fail"
    assert empty_voice.reply
    toolkit = _turn("web_hi_num", "did you run a search console check?")
    assert toolkit.next_action == "tool_status"
    assert toolkit.reply
    _assert_no_visitor_tool_leak(toolkit.reply)


def test_toolkit_question_is_answered_before_weather_or_sell() -> None:
    mixed = _turn("web_tk1", "What's the weather and did you run a JSON-LD check?")
    assert mixed.next_action == "tool_status"
    _assert_no_visitor_tool_leak(mixed.reply)
    assert mixed.reply
    rain = _turn("web_tk2", "how much rain tomorrow")
    assert rain.next_action == "off_topic"
    assert "AssafWeb" in rain.reply
    assert "api" not in rain.reply.lower()


def test_session_names_the_tool_that_already_ran() -> None:
    first = _turn("web_ran1", "צריכים אתר", tools_ran=(KNOWLEDGE_TOOL,))
    assert first.reply
    _assert_no_visitor_tool_leak(first.reply)
    asked = _turn("web_ran1", "what tool did you run")
    assert asked.next_action == "tool_status"
    _assert_no_visitor_tool_leak(asked.reply)


def test_missing_metrics_are_allowed_never_invented() -> None:
    turn = _turn("web_met1", "How many clients do you have? What's the conversion rate?")
    assert turn.next_action == "no_metric"
    assert "invent" in turn.reply.lower() or "ממציאה" in turn.reply
    assert not any(ch.isdigit() for ch in turn.reply)
    assert "73" not in turn.reply
    foreign = PublishedFact(
        text="We have 480 clients and 91% conversion.",
        url="https://metrics.example/funnel",
    )
    ignored = _turn("web_met2", "how many clients", facts=(foreign,))
    assert ignored.next_action == "no_metric"
    assert "480" not in ignored.reply
    assert "91" not in ignored.reply


def test_stop_sell_does_not_reask_identity() -> None:
    first = _turn("web_stop1", "not interested")
    assert first.next_action == "answer"
    assert "טלפון" not in first.reply
    again = _turn("web_stop1", "hi")
    assert again.next_action != "ask_contact"
    assert "phone" not in again.reply.lower()
    assert "email" not in again.reply.lower()
    assert again.reply


def test_site_path_has_no_leads_studio_gmail_or_social() -> None:
    from pathlib import Path

    from app.core.config import Settings as LiveSettings

    blob = (
        Path("app/surfaces/site.py").read_text(encoding="utf-8")
        + Path("app/surfaces/site_policy.py").read_text(encoding="utf-8")
        + Path("app/api/website.py").read_text(encoding="utf-8")
        + Path("app/web/ask_mia.js").read_text(encoding="utf-8")
    )
    lowered = blob.lower()
    assert "01 leads" not in lowered
    assert "my studio" not in lowered
    assert "gmail_send" not in lowered
    assert "kill_switch" not in lowered
    assert "instagram" not in lowered
    assert "social publish" not in lowered
    assert "linkedin.com/feed" not in lowered
    assert LiveSettings().gmail_send is False
    assert LiveSettings().auto_reply_instagram is False
    assert LiveSettings().meta_write is False


def test_visitor_reply_scrubs_live_tool_leak_and_rtl_period() -> None:
    leaked = (
        "ב-AssafWeb אסף בונה אתרים. "
        "רץ knowledge_search. לא רץ כלי Search Console או JSON-LD."
        "\n.Search Console או JSON-LD"
    )
    cleaned = scrub_visitor_reply(leaked)
    assert "AssafWeb" in cleaned
    _assert_no_visitor_tool_leak(cleaned)
    turn = _turn("web_leak1", "היי מיה אני מעוניין בשירותים שהאתר מציע")
    _assert_no_visitor_tool_leak(turn.reply)
    empty_facts = _turn(
        "web_leak2",
        "Did you run a JSON-LD or Search Console check?",
        tools_ran=(),
        facts=(),
    )
    _assert_no_visitor_tool_leak(empty_facts.reply)
    assert "traffic" not in empty_facts.reply.lower() or "invent" in empty_facts.reply.lower()


def test_voice_agent_product_is_sold_not_widget_stt() -> None:
    he = _turn("web_va1", "אני צריך סוכן קולי לאתר שלי")
    assert classify_site_intent("אני צריך סוכן קולי לאתר שלי") == "voice_product"
    assert he.next_action == "answer"
    assert "סוכן קולי" in he.reply
    assert "AssafWeb" in he.reply or "ב-AssafWeb" in he.reply
    _assert_no_visitor_tool_leak(he.reply)
    en = _turn("web_va2", "I need a voice agent for my site")
    assert "voice agent" in en.reply.lower()
    assert "record here" not in en.reply.lower()
    fail = _turn("web_va3", "", voice_failed=True)
    assert fail.next_action == "voice_fail"
    assert "כתבו" in fail.reply or "type" in fail.reply.lower()
