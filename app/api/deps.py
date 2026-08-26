from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.integrations.base import MessagePort
from app.integrations.calendar import CalendarPort, build_calendar_port
from app.integrations.calendar_booking import CalendarBookingPort, build_calendar_booking_port
from app.integrations.instagram import build_instagram_port
from app.integrations.instagram_insights import (
    InstagramInsightsPort,
    build_instagram_insights_port,
)
from app.integrations.linkedin import LinkedInPort, build_linkedin_port
from app.integrations.research import ResearchPort, build_research_port
from app.integrations.sheets import SheetsPort, build_sheets_port
from app.integrations.telegram import build_telegram_port
from app.integrations.transcribe import TranscriptionPort, build_transcription_port
from app.integrations.whatsapp import (
    DisabledWhatsAppMediaPort,
    WhatsAppMediaPort,
    build_whatsapp_media_port,
    build_whatsapp_port,
)


def get_db() -> Session:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_whatsapp_port() -> MessagePort:
    return build_whatsapp_port(get_settings())


def get_instagram_port() -> MessagePort:
    return build_instagram_port(get_settings())


def get_telegram_port() -> MessagePort:
    return build_telegram_port(get_settings())


def get_transcription_port() -> TranscriptionPort:
    return build_transcription_port(get_settings())


def get_whatsapp_media_port() -> WhatsAppMediaPort | DisabledWhatsAppMediaPort:
    return build_whatsapp_media_port(get_settings())


def get_calendar_port() -> CalendarPort:
    return build_calendar_port(get_settings())


def get_calendar_booking_port() -> CalendarBookingPort:
    return build_calendar_booking_port(get_settings())


def get_sheets_port() -> SheetsPort:
    return build_sheets_port(get_settings())


def get_instagram_insights_port() -> InstagramInsightsPort:
    return build_instagram_insights_port(get_settings())


def get_research_port() -> ResearchPort:
    return build_research_port(get_settings())


def get_linkedin_port() -> LinkedInPort:
    return build_linkedin_port(get_settings())
