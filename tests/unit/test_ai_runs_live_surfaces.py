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


def test_all_three_action_vocabularies_are_accepted() -> None:
    """Three surfaces, three vocabularies. All must be storable, none may be dropped."""
    # 1. Website (app/surfaces/site_policy.SITE_ACTIONS) -- overlaps NextAction on
    #    "handoff" alone, so this is the one that was silently failing.
    for action in SITE_ACTIONS:
        assert _valid_next_action(action) is True, f"website action rejected: {action}"

    # 2. Owner Telegram -- has no sales action of its own.
    assert _valid_next_action(OWNER_REPLY_ACTION) is True

    # 3. ClientGraph / prospect sales path.
    for action in NextAction:
        assert _valid_next_action(action.value) is True, f"NextAction rejected: {action}"

    # And it is still a gate, not a free-for-all.
    assert _valid_next_action("") is False
    assert _valid_next_action("delete_everything") is False
    assert _valid_next_action("owner_reply_but_evil") is False


def test_engine_health_counts_one_day_not_all_time() -> None:
    """The day window used to be accepted and ignored, so "today" meant "ever".

    Dated in 2019 on purpose. The suite shares one database and other tests write
    ai_runs stamped with the real clock, so asserting exact counts around "now" makes
    this pass alone and fail in the suite. A window nothing else can land in tests the
    filter itself rather than the order tests happen to run in.
    """
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)

        def _run(run_id: str, occurred_at: str) -> None:
            store.save_ai_run(
                run_id=run_id,
                lead_id=None,
                channel="website",
                graph_version="v1",
                model="gpt-test",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0,
                next_action="answer",
                kill_switch=False,
                policy_version="v1",
                latency_ms=100,
                occurred_at=occurred_at,
            )

        _run("run_day_a", "2019-03-05T09:00:00+00:00")
        _run("run_day_b", "2019-03-05T21:00:00+00:00")
        _run("run_day_yesterday", "2019-03-04T09:00:00+00:00")
        # A row as it exists in production today, written before the column did.
        # Inserted directly: save_ai_run stamps `now` for a new write, so a blank can
        # only come from pre-migration data. Nobody knows when it ran, so it belongs
        # to no day rather than silently to this one.
        db.add(
            AiRunRow(
                run_id="run_day_legacy",
                lead_id=None,
                channel="website",
                graph_version="v1",
                model="gpt-test",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0,
                next_action="answer",
                kill_switch=False,
                policy_version="v1",
                latency_ms=100,
                occurred_at="",
            )
        )
        db.commit()

        first_day = store.aggregate_ai_runs(
            occurred_from="2019-03-05T00:00:00+00:00",
            occurred_to="2019-03-06T00:00:00+00:00",
        )
        assert first_day.total_runs == 2

        day_before = store.aggregate_ai_runs(
            occurred_from="2019-03-04T00:00:00+00:00",
            occurred_to="2019-03-05T00:00:00+00:00",
        )
        assert day_before.total_runs == 1
    finally:
        db.close()


def test_a_run_is_stamped_even_when_the_caller_says_nothing() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        store.save_ai_run(
            run_id="run_stamp_default",
            lead_id=None,
            channel="website",
            graph_version="v1",
            model="gpt-test",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            next_action="answer",
            kill_switch=False,
            policy_version="v1",
        )
        db.commit()
        saved = store.get_ai_run("run_stamp_default")
        assert saved is not None
        assert saved.occurred_at  # not blank: a new row always knows when it ran
    finally:
        db.close()


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
