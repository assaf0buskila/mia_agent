from app.agents.shared.state import empty_client_state
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel


def test_two_website_sessions_do_not_share_lead_or_state() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_a = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_a")
        _, lead_b = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_b")
        db.commit()
        assert lead_a != lead_b
        a = empty_client_state(
            run_id="r1",
            conversation_id="web_a",
            visitor_id="web_a",
            lead_id=lead_a,
            latest_message="secret-a",
        )
        b = empty_client_state(
            run_id="r2",
            conversation_id="web_b",
            visitor_id="web_b",
            lead_id=lead_b,
            latest_message="secret-b",
        )
        assert a["conversation_id"] != b["conversation_id"]
        assert a["lead_id"] != b["lead_id"]
        assert a["latest_message"] not in b["latest_message"]
        assert store.get_website_lead_id("web_a") == lead_a
        assert store.get_website_lead_id("web_b") == lead_b
    finally:
        db.close()
