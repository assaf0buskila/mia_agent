from app.core.config import Settings
from app.surfaces.crm import FakeContactsCrm
from app.surfaces.site import (
    SiteSession,
    dump_site_session,
    load_site_session,
    reset_site_book,
    run_site_turn,
    site_book,
)


def test_nail_business_closes_topics_and_reaches_value_contact() -> None:
    reset_site_book()
    book = site_book()
    book.open("conversion-regression")
    replies = []
    for text in (
        "אני עושה בניית ציפורניים לבנות",
        "רוב הפגישות והלקוחות מגיעים אלי בוואטסאפ",
        "כל התורים וכל ההודעות אני מנהלת",
    ):
        turn = run_site_turn(
            session_id="conversion-regression",
            text=text,
            settings=Settings(),
            crm=FakeContactsCrm(),
            book=book,
        )
        replies.append(turn)
    assert replies[-1].next_action == "ask_contact"
    assert any("טלפון" in turn.reply or "אימייל" in turn.reply for turn in replies)
    assert "מה העסק" not in " ".join(turn.reply for turn in replies[1:])
    assert "מה גוזל" not in " ".join(turn.reply for turn in replies[2:])


def _turn(session_id: str, text: str, *, crm: FakeContactsCrm | None = None, **fields: str):
    book = site_book()
    if not book.exists(session_id):
        book.open(session_id)
    return run_site_turn(
        session_id=session_id,
        text=text,
        settings=Settings(),
        crm=crm or FakeContactsCrm(),
        book=book,
        **fields,
    )


def test_strong_first_message_skips_discovery_and_requests_contact() -> None:
    reset_site_book()
    turn = _turn(
        "strong-first",
        "אני מתכננת לפתוח סלון ציפורניים ותיאום התורים וההודעות בוואטסאפ גוזלים ממני שעות",
    )
    session = site_book().get("strong-first")
    assert session is not None
    assert session.business_known is True
    assert session.friction_known is True
    assert session.discovery_questions == 0
    assert turn.next_action == "ask_contact"
    assert "טלפון" in turn.reply or "אימייל" in turn.reply


def test_interruptions_do_not_close_or_repeat_the_open_topic() -> None:
    reset_site_book()
    first = _turn("interruptions", "שלום")
    assert first.next_action == "ask_need"
    session = site_book().get("interruptions")
    assert session is not None
    assert session.last_question_topic == "business"
    assert session.discovery_questions == 1

    for text in ("תודה", "שומעת אותי", "אני אסף תני לי גישה"):
        _turn("interruptions", text)
        assert session.business_known is False
        assert session.last_question_topic == "business"
        assert session.discovery_questions == 1

    for session_id, interruption in (
        ("complaint-interruption", "I want to complain"),
        ("stop-interruption", "not interested"),
    ):
        _turn(session_id, "hello")
        interrupted = site_book().get(session_id)
        assert interrupted is not None
        _turn(session_id, interruption)
        assert interrupted.business_known is False
        assert interrupted.friction_known is False
        assert interrupted.discovery_questions == 1


def test_unknown_industry_substantive_answers_close_each_topic_once() -> None:
    reset_site_book()
    _turn("unknown-industry", "שלום")
    business = _turn("unknown-industry", "אנחנו מייצרים רכיבים מיוחדים למעבדות מחקר")
    session = site_book().get("unknown-industry")
    assert session is not None
    assert session.business_known is True
    assert business.next_action == "answer"
    assert session.last_question_topic == "friction"
    value = _turn("unknown-industry", "המערכת הנוכחית איטית וכל בקשה עוברת ידנית בין שלושה אנשים")
    assert session.friction_known is True
    assert session.discovery_questions == 2
    assert session.asked_topics == ("business", "friction")
    assert value.next_action == "ask_contact"


def test_nonlead_does_not_become_conversion_state() -> None:
    reset_site_book()
    turn = _turn("student", "I'm a student with a school project about websites")
    session = site_book().get("student")
    assert session is not None
    assert turn.next_action == "answer"
    assert session.business_known is False
    assert session.friction_known is False
    assert session.need_seen is False
    assert turn.crm_wrote is False


def test_malformed_optional_state_never_throws_and_acquisition_round_trips() -> None:
    session = SiteSession(session_id="state")
    assert load_site_session(
        session,
        '{"discovery_questions":{"bad":true},"asked_topics":"bad",'
        '"acquisition_context":{"utm_source":"google","bad":4}}',
    )
    assert session.discovery_questions == 0
    assert session.asked_topics == ()
    assert session.acquisition_context == {"utm_source": "google"}
    restored = SiteSession(session_id="restored")
    assert load_site_session(restored, dump_site_session(session))
    assert restored.acquisition_context == {"utm_source": "google"}


def test_contact_write_is_not_repeated_after_success() -> None:
    reset_site_book()
    crm = FakeContactsCrm()
    first = _turn("crm-once", "צריכים אתר", crm=crm, phone="0501234567")
    second = _turn("crm-once", "יש לי עוד שאלה", crm=crm, phone="0501234567")
    assert first.crm_wrote is True
    assert second.crm_wrote is False
    assert len(crm.activity) == 1


def test_meeting_intent_requests_contact_without_discovery() -> None:
    from app.surfaces.site_policy import classify_site_intent, decide_site_turn

    assert classify_site_intent("אני רוצה לקבוע פגישה עם אסף") == "ask_assaf"
    assert classify_site_intent("let's schedule a call") == "ask_assaf"

    decision = decide_site_turn(
        thought="אני רוצה לקבוע פגישה עם אסף",
        language="he",
        has_contact=False,
        already_confirmed=False,
        selling_stopped=False,
        already_pinged=False,
    )
    assert decision.action == "ask_contact"
    assert decision.ask_contact is True


def test_training_and_brainstorm_do_not_trigger_weather() -> None:
    from app.surfaces.site_policy import classify_site_intent

    assert classify_site_intent("We need AI training for our agents") != "off_topic"
    assert classify_site_intent("Brainstorming website ideas") != "off_topic"

