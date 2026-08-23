from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_calendar_booking_port, get_calendar_port, get_db, get_sheets_port
from app.api.website import process_website_message, process_website_session
from app.core.config import get_settings
from app.core.demo import DEMO_LABEL, SCRIPTED_MESSAGES, demo_mode_active
from app.db.store import LeadStore
from app.integrations.calendar import CalendarPort
from app.integrations.calendar_booking import CalendarBookingPort
from app.integrations.sheets import SheetsPort

router = APIRouter(prefix="/v1/demo", tags=["demo"])


class ScriptedStepOut(BaseModel):
    user: str
    next_action: str
    message: str


class ScriptedOut(BaseModel):
    session_id: str
    lead_id: str
    label: str
    steps: list[ScriptedStepOut]


@router.get("/status")
def demo_status() -> dict[str, str | bool]:
    settings = get_settings()
    if not demo_mode_active(settings):
        raise HTTPException(status_code=404, detail="demo mode inactive")
    return {"active": True, "env": settings.env.value, "label": DEMO_LABEL}


@router.post("/scripted", response_model=ScriptedOut)
def demo_scripted(
    db: Session = Depends(get_db),
    calendar: CalendarPort = Depends(get_calendar_port),
    calendar_booking: CalendarBookingPort = Depends(get_calendar_booking_port),
    sheets: SheetsPort = Depends(get_sheets_port),
) -> ScriptedOut:
    settings = get_settings()
    if not demo_mode_active(settings):
        raise HTTPException(status_code=404, detail="demo mode inactive")
    store = LeadStore(db)
    session = process_website_session(store, settings=settings)
    steps: list[ScriptedStepOut] = []
    for text, _expected in SCRIPTED_MESSAGES:
        result = process_website_message(
            store,
            session_id=session.session_id,
            text=text,
            settings=settings,
            calendar=calendar,
            calendar_booking=calendar_booking,
            sheets=sheets,
        )
        steps.append(
            ScriptedStepOut(
                user=text,
                next_action=result.next_action,
                message=result.message,
            )
        )
    return ScriptedOut(
        session_id=session.session_id,
        lead_id=session.lead_id,
        label=DEMO_LABEL,
        steps=steps,
    )
