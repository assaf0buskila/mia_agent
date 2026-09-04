"""What Mia learned in a website chat must outlive a deploy.

`SiteSession` lives in a process-local dict. When the task was replaced mid
conversation the visitor's captured phone number, the fact Assaf had already been
told about them, and the fact they had said "not interested" all vanished. Mia then
re-asked for the number, pinged Assaf about the same person again, and resumed
selling to someone who had declined.

`reset_site_book()` is exactly what a restart does to that dict, so these tests use
it as the deploy.
"""

from __future__ import annotations

from app.db.session import init_db
from app.main import app
from app.surfaces.identity import CapturedFields
from app.surfaces.site import (
    SiteSession,
    dump_site_session,
    load_site_session,
    reset_site_book,
    site_book,
)
from fastapi.testclient import TestClient

ORIGIN = {"Origin": "https://www.assafweb.com"}


def _post(client: TestClient, session_id: str, text: str, **body):
    return client.post(
        f"/v1/website/sessions/{session_id}/messages",
        json={"text": text, **body},
        headers=ORIGIN,
    )


def test_a_phone_number_survives_a_deploy() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions", headers=ORIGIN).json()[
            "session_id"
        ]
        _post(client, session_id, "יש לי מספרה וצריך עזרה בוואטסאפ", phone="0501234567")
        assert site_book().get(session_id).fields.phone

        reset_site_book()  # the deploy
        assert site_book().get(session_id) is None

        _post(client, session_id, "מה השלב הבא?")
        restored = site_book().get(session_id)
        assert restored is not None
        assert restored.fields.phone, "Mia must not ask again for a number she has"


def test_a_visitor_who_said_no_is_not_sold_to_again_after_a_deploy() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions", headers=ORIGIN).json()[
            "session_id"
        ]
        _post(client, session_id, "לא מעוניין")
        assert site_book().get(session_id).selling_stopped is True

        reset_site_book()
        _post(client, session_id, "אוקיי")
        assert site_book().get(session_id).selling_stopped is True


def test_the_already_told_assaf_flag_survives() -> None:
    """The double-ping: `pinged` reset to False and Assaf heard about them twice."""
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions", headers=ORIGIN).json()[
            "session_id"
        ]
        _post(client, session_id, "יש לי קליניקה", phone="0507654321")
        live = site_book().get(session_id)
        live.pinged = True
        _post(client, session_id, "עוד משהו")

        reset_site_book()
        _post(client, session_id, "ומה עכשיו?")
        assert site_book().get(session_id).pinged is True


def test_round_trip_keeps_every_field_that_matters() -> None:
    original = SiteSession(session_id="web_rt")
    original.fields = CapturedFields(
        name="דנה", phone="0501112222", email="d@example.com", want="ניהול תורים"
    )
    original.pinged = True
    original.confirmed = True
    original.selling_stopped = True
    original.complaint_open = True
    original.need_seen = True
    original.language = "he"
    original.tools_ran = ("knowledge_search",)
    original.turns = [("visitor", "היי"), ("mia", "שלום")]

    restored = SiteSession(session_id="web_rt")
    assert load_site_session(restored, dump_site_session(original)) is True

    assert restored.fields.phone == "0501112222"
    assert restored.fields.want == "ניהול תורים"
    assert restored.pinged and restored.confirmed and restored.selling_stopped
    assert restored.complaint_open and restored.need_seen
    assert restored.language == "he"
    assert restored.tools_ran == ("knowledge_search",)
    assert restored.turns == [("visitor", "היי"), ("mia", "שלום")]


def test_broken_state_never_costs_the_visitor_their_turn() -> None:
    session = SiteSession(session_id="web_bad")
    for junk in ("", "not json", "[]", '{"fields": "wrong type"}'):
        assert load_site_session(session, junk) in (True, False)
    # A new visitor with no stored state is simply new.
    assert load_site_session(SiteSession(session_id="web_new"), "") is False
