"""Exercise runtime boundaries and durable effects, including adversarial model output."""

import json

import pytest
from app.core.config import Settings
from app.db.session import get_session_factory
from app.db.store import LeadStore
from app.evals.predeploy.sandbox import SealedOwnerPort
from app.integrations.sales_reply import ComposeResult
from app.main import app
from app.surfaces.crm import FakeContactsCrm
from app.surfaces.site import SiteBook, reset_site_book, run_site_turn, site_book
from fastapi.testclient import TestClient


class BadDiscoveryPort:
    def __init__(self):
        self.calls = []

    def compose(self, **kwargs):
        self.calls.append(kwargs)
        return ComposeResult(text="מה חוזר שוב ושוב אצלכם?", tokens_in=10, tokens_out=8)


@pytest.mark.parametrize(
    "question, followup",
    [
        ("אתם יכולים לבנות לי סוכן שיענה ללקוחות בוואטסאפ?", "נשמע מעניין"),
        ("Can you build an agent that answers customers on WhatsApp?", "Sounds interesting"),
    ],
)
def test_product_question_does_not_supply_business_or_friction(question, followup):
    book = SiteBook()
    session = book.open("product-question")
    settings = Settings(_env_file=None)
    crm = FakeContactsCrm()
    first = run_site_turn(
        session_id=session.session_id, text=question, settings=settings, crm=crm, book=book, now=0
    )
    assert first.next_action == "answer"
    assert not session.business_known and not session.friction_known and not session.need_seen
    second = run_site_turn(
        session_id=session.session_id, text=followup, settings=settings, crm=crm, book=book, now=10
    )
    assert second.next_action == "ask_need"
    assert not session.value_shown and not session.contact_requested


def test_model_cannot_replace_discovery_or_reopen_topics_on_value_and_product_turns():
    book = SiteBook()
    session = book.open("adversarial")
    port = BadDiscoveryPort()
    crm = FakeContactsCrm()
    outputs = []
    for i, text in enumerate(
        (
            "אני עושה בניית ציפורניים לבנות",
            "רוב התורים מגיעים בוואטסאפ",
            "אתם יכולים לבנות סוכן ללקוחות?",
        )
    ):
        outputs.append(
            run_site_turn(
                session_id=session.session_id,
                text=text,
                settings=Settings(_env_file=None),
                crm=crm,
                book=book,
                reply_port=port,
                now=i * 10,
            )
        )
    assert len(port.calls) == 2, "deterministic discovery must not call a model"
    assert "מה חוזר" not in " ".join(turn.reply for turn in outputs)
    assert outputs[1].next_action == "ask_contact"
    assert "?" not in outputs[1].reply
    assert outputs[1].tokens_in == 10
    assert not outputs[1].model_reply_used
    assert session.discovery_questions == 1


def test_unanswered_business_question_is_not_asked_again_forever():
    book = SiteBook()
    session = book.open("unanswered")
    replies = [
        run_site_turn(
            session_id=session.session_id,
            text="hello",
            settings=Settings(_env_file=None),
            crm=FakeContactsCrm(),
            book=book,
            now=i * 10,
        )
        for i in range(6)
    ]
    assert sum("?" in turn.reply for turn in replies) == 1
    assert session.discovery_questions == 1
    assert not any(t.next_action == "ask_contact" for t in replies)


def test_contact_confirmation_has_one_crm_write_one_ping_and_repeat_url():
    book = SiteBook()
    book.open("contact-once")
    crm, owner = FakeContactsCrm(), SealedOwnerPort()
    settings = Settings(
        _env_file=None, telegram_owner_user_ids="12345", whatsapp_click_to_chat="972500000001"
    )
    turns = [
        run_site_turn(
            session_id="contact-once",
            text="רוצה לדבר עם אסף",
            phone="0501234567",
            settings=settings,
            crm=crm,
            owner_port=owner,
            book=book,
            now=i * 10,
        )
        for i in range(2)
    ]
    assert [t.next_action for t in turns] == ["confirm_contact", "handoff"]
    assert all(t.whatsapp_url for t in turns)
    assert crm.tabs.count("Contacts") == 1
    assert len(owner.sent) == 1


def test_price_with_contact_answers_then_confirms():
    book = SiteBook()
    book.open("price-contact")
    turn = run_site_turn(
        session_id="price-contact",
        text="כמה עולה? 0501234567",
        book=book,
        settings=Settings(_env_file=None, whatsapp_click_to_chat="972500000001"),
        crm=FakeContactsCrm(),
    )
    assert turn.next_action == "confirm_contact"
    assert "אין מחיר מפורסם" in turn.reply
    assert turn.crm_wrote and turn.whatsapp_url


def test_student_does_not_become_lead_on_followup_business_vocabulary():
    book = SiteBook()
    session = book.open("student")
    for i, text in enumerate(
        ("אני סטודנט ועושה עבודה ללימודים", "צריכים אתר לפרויקט", "הלקוחות שולחים הודעות בוואטסאפ")
    ):
        turn = run_site_turn(
            session_id=session.session_id,
            text=text,
            book=book,
            settings=Settings(_env_file=None),
            crm=FakeContactsCrm(),
            now=i * 10,
        )
        assert turn.next_action != "ask_contact"
        assert not turn.crm_wrote and not turn.whatsapp_url


def test_deferred_contact_write_survives_restart_and_handoff_rehydrates(monkeypatch):
    crm = FakeContactsCrm()
    monkeypatch.setattr("app.api.website.build_contacts_crm", lambda *args: crm)
    monkeypatch.setenv("MIA_WHATSAPP_CLICK_TO_CHAT", "972500000001")
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "רוצה להמשיך עם אסף",
                "email": "test@example.test",
            },
        )
        assert first.status_code == 200
        with get_session_factory()() as db:
            state = json.loads(LeadStore(db).load_website_session_state(session_id))
            assert state["crm_written"] is True
        reset_site_book()
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert handoff.status_code == 200
        assert handoff.json()["whatsapp_url"]
        again = client.post(
            f"/v1/website/sessions/{session_id}/messages", json={"text": "רוצה להמשיך עם אסף"}
        )
        assert again.status_code == 200
        assert crm.tabs.count("Contacts") == 1
        assert site_book().get(session_id).crm_written


@pytest.mark.parametrize("url", ["https://[", "https://user:pass@host.example/", "/%0asecret"])
def test_malformed_acquisition_urls_do_not_break_session_creation(url):
    with TestClient(app) as client:
        assert client.post("/v1/website/sessions", params={"landing_page": url}).status_code == 200


@pytest.mark.parametrize(
    "business",
    [
        "יש לי בית ספר לסטודנטים וכל היום עונים בוואטסאפ",
        "אני מוכר קורסים לסטודנטים וכל היום עונים בוואטסאפ",
        "יש לי אפליקציה ללימודים וכל היום עונים בוואטסאפ",
    ],
)
def test_education_business_is_a_lead(business):
    book = SiteBook()
    session = book.open("education")
    turn = run_site_turn(
        session_id=session.session_id,
        text=business,
        book=book,
        settings=Settings(_env_file=None),
        crm=FakeContactsCrm(),
    )
    assert not session.nonlead
    assert session.business_known and session.friction_known
    assert turn.next_action == "ask_contact"


@pytest.mark.parametrize("fail_first", [False, True])
def test_end_after_restart_rehydrates_and_persists_finalization(monkeypatch, fail_first):
    seen = []

    async def ping(**kwargs):
        session = site_book().get(kwargs["session_id"])
        assert session.confirmed and session.fields.has_phone_or_email()
        seen.append(session.session_id)
        if fail_first and len(seen) == 1:
            return False
        session.pinged = True
        return True

    monkeypatch.setattr("app.api.website._maybe_ping_owner", ping)
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        with get_session_factory()() as db:
            store = LeadStore(db)
            state = json.loads(store.load_website_session_state(session_id))
            state.update(
                turns=[["visitor", "רוצה לדבר עם אסף"]], confirmed=True, awaiting_ping=True
            )
            state["fields"]["phone"] = "0501234567"
            store.save_website_session_state(session_id, json.dumps(state))
            db.commit()
        reset_site_book()
        if fail_first:
            assert not client.post(f"/v1/website/sessions/{session_id}/end").json()["finalized"]
            reset_site_book()
        assert client.post(f"/v1/website/sessions/{session_id}/end").json()["finalized"]
        reset_site_book()
        assert not client.post(f"/v1/website/sessions/{session_id}/end").json()["finalized"]
        assert seen == [session_id] * (2 if fail_first else 1)
