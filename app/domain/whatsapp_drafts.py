"""WhatsApp draft for Assaf. Never fire at a lead."""

from __future__ import annotations

from dataclasses import dataclass

from app.surfaces.identity import extract_fields

_LEAD_FIRE = (
    "send to the lead",
    "שלח לליד",
    "שלחי לליד",
    "fire at",
    "whatsapp the lead",
)


@dataclass(frozen=True)
class WhatsAppDraft:
    body: str
    destination: str
    sent: bool


def draft_whatsapp_for_assaf(*, body: str, destination: str = "") -> WhatsAppDraft | str:
    """Draft a WhatsApp note for Assaf. Refuse a lead destination. Never send."""
    text = body.strip()
    if not text:
        return "Need draft text. Not sent."
    lowered = text.casefold()
    if any(needle in lowered for needle in _LEAD_FIRE):
        return "I draft WhatsApp for Assaf only. I never fire at a lead."
    target = destination.strip()
    if target:
        fields = extract_fields(target)
        if fields.has_phone_or_email() and "assaf" not in target.casefold():
            return "I draft WhatsApp for Assaf only. I never fire at a lead."
        if target.casefold() not in {"assaf", "אסף", "owner"}:
            return "I draft WhatsApp for Assaf only. I never fire at a lead."
    return WhatsAppDraft(body=text[:2000], destination="assaf", sent=False)
