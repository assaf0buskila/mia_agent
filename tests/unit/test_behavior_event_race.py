"""Concurrent funnel-event retries must not abort a visitor's transaction."""

from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import build_behavior_event


def test_behavior_insert_survives_a_stale_duplicate_lookup(monkeypatch):
    init_db()
    with get_session_factory()() as db:
        store = LeadStore(db)
        event = build_behavior_event(
            session_id="web_behavior_race",
            lead_id="",
            payload={"kind": "website_conversion"},
        )
        store.save_canonical_event(provider="website", event=event)
        db.commit()
        # Another transaction can insert after a worker's duplicate lookup.
        # Force that stale observation while retaining the real unique constraint.
        monkeypatch.setattr(store, "get_canonical_event", lambda **kwargs: None)
        store.save_canonical_event(provider="website", event=event)
        db.commit()
        monkeypatch.undo()
        assert store.get_canonical_event(
            provider="website", provider_event_id=event.idempotency_key
        ) is not None
