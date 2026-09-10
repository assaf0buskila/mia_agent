from app.db.base import Base
from app.db.session import make_engine
from app.db.store import LeadStore
from sqlalchemy.orm import Session


def test_gemini_transcript_provenance_survives_a_new_database_session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'transcripts.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            LeadStore(session).save_transcript(
                provider="telegram",
                provider_event_id="voice-1",
                channel="telegram",
                external_id="123",
                actor_role="owner",
                transcript="שלום מיה",
                stt_provider="gemini",
                stt_model="gemini-audio-test",
            )
            session.commit()
        with Session(engine) as session:
            saved = LeadStore(session).get_transcript(
                provider="telegram",
                provider_event_id="voice-1",
            )
            assert saved is not None
            assert saved.stt_provider == "gemini"
            assert saved.stt_model == "gemini-audio-test"
            assert saved.transcript == "שלום מיה"
    finally:
        engine.dispose()
