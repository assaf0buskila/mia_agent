"""The ai_runs table is fed by the surfaces that actually serve people.

`persist_ai_run` had exactly one call site — the muted WhatsApp prospect path in
`app/api/inbound.py`. Neither the live website turn nor the live Telegram owner turn
wrote a row, so the engine numbers on the daily brief measured a path nobody could
reach. Both live surfaces write one now.

The validator is the other half of this. It only accepted `NextAction`, which shares
exactly one value with the website's own vocabulary ("handoff"), so a website row
would have been silently dropped for twelve of its thirteen actions and an owner row
could not be written at all.
"""

from __future__ import annotations

from app.db.models import AiRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.ai_runs import (
    OWNER_REPLY_ACTION,
    WEBSITE_ACTIONS,
    _valid_next_action,
)
from app.domain.sales import NextAction
from app.surfaces.site_policy import SITE_ACTIONS
from sqlalchemy import select


def test_the_mirrored_website_vocabulary_has_not_drifted() -> None:
    """`WEBSITE_ACTIONS` is duplicated so domain does not import a surface. Pin it."""
    assert WEBSITE_ACTIONS == SITE_ACTIONS


def test_every_live_surface_vocabulary_is_recordable() -> None:
    for action in SITE_ACTIONS:
        assert _valid_next_action(action) is True, action
    for action in NextAction:
        assert _valid_next_action(action.value) is True, action
    assert _valid_next_action(OWNER_REPLY_ACTION) is True
    # Still a gate, not a free-for-all.
    assert _valid_next_action("") is False
    assert _valid_next_action("delete_everything") is False


def _rows(db) -> list[AiRunRow]:
    return list(db.scalars(select(AiRunRow)).all())


def test_a_live_website_turn_writes_an_ai_run() -> None:
    from app.main import app
    from fastapi.testclient import TestClient

    init_db()
    db = get_session_factory()()
    try:
        before = len(_rows(db))
    finally:
        db.close()

    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        posted = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "צריך אתר לעסק שלי"},
        )
        assert posted.status_code == 200

    db = get_session_factory()()
    try:
        rows = _rows(db)
        assert len(rows) == before + 1
        row = rows[-1]
        assert row.channel == "website"
        # The website's real action, not a lossy translation into NextAction.
        assert row.next_action in SITE_ACTIONS
        assert row.run_id
    finally:
        db.close()


def test_the_owner_reply_action_survives_a_round_trip() -> None:
    """The owner loop has no sales action, so it needs one of its own to be storable."""
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        from app.domain.ai_runs import persist_ai_run

        persist_ai_run(
            store,
            run_id="run_owner_test_1",
            lead_id=None,
            channel="telegram",
            next_action=OWNER_REPLY_ACTION,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
            latency_ms=1234,
            tokens_in=11,
            tokens_out=22,
            model_label="gpt-owner-test",
        )
        db.commit()
        saved = store.get_ai_run("run_owner_test_1")
        assert saved is not None
        assert saved.channel == "telegram"
        assert saved.next_action == OWNER_REPLY_ACTION
        assert saved.tokens_in == 11
        assert saved.tokens_out == 22
        assert saved.latency_ms == 1234
        # The model that actually answered, not one derived from the sales chain.
        assert saved.model == "gpt-owner-test"
    finally:
        db.close()
