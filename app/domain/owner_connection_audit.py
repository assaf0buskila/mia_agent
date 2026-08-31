"""Deterministic rendering for Owner Mia's complete-work connection audit.

One owner-tool call may fan out to the small, explicitly listed set of live and
local reads.  This is deliberately not a provider catalogue inspection: an item
is reported as checked only after its own read returned, and no result is turned
into a vague claim that the whole provider is working.
"""
from __future__ import annotations

from dataclasses import dataclass

_NOT_CONNECTED = "not connected"
_EMPTY_MARKERS = (
    "no free slots found",
    "the requested sheet range is empty",
    "instagram insights returned nothing",
    "linkedin returned nothing",
    "no rows returned",
    "אין מיילים",
    "אין שיחות",
    "אין לידים",
    "אין נתונים",
    "לא נמצאו",
    "לא נמצאו נתונים",
)


@dataclass(frozen=True)
class OwnerAuditResult:
    """A single auditable probe result; text is already bounded by its tool."""

    label: str
    ok: bool
    text: str


def _status(result: OwnerAuditResult) -> str:
    text = result.text.strip()
    if "no allowlisted spreadsheet is configured" in text.casefold():
        return "לא נבדק: אין גיליון מורשה מוגדר"
    if _NOT_CONNECTED in text.casefold() or "not configured" in text.casefold():
        return "לא מחובר או לא מוגדר"
    if not result.ok:
        return "לא נבדק: הקריאה נכשלה"
    if any(marker in text.casefold() for marker in _EMPTY_MARKERS):
        return "נבדק: אין נתונים בטווח שנבדק"
    return "נבדק: התקבלה תשובה"


def format_owner_connection_audit(results: list[OwnerAuditResult]) -> str:
    """Render every requested surface with evidence, never a blanket verdict."""
    lines = [
        "בדיקת מערכת מלאה: כל שורה היא קריאה נפרדת שבוצעה עכשיו. "
        "אין כאן מגבלת שתי קריאות.",
    ]
    for result in results:
        detail = " ".join(result.text.split())[:420] if result.text.strip() else "ללא פירוט."
        lines.append(f"- {result.label}: {_status(result)}. {detail}")
    lines.append(
        "פעולות כתיבה, שליחה, פרסום או מחיקה לא נבדקו ולא בוצעו בבדיקה הזאת."
    )
    return "\n".join(lines)
