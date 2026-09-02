"""Extract visitor/owner contact fields. Never mint a lead_ id."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.surfaces.crm import ContactRecord, normalize_email, normalize_phone

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+972[\s\-]?(?:5\d|[2-9])[\s\-]?\d{3}[\s\-]?\d{4}"
    r"|0(?:5\d|[2-9])[\s\-]?\d{3}[\s\-]?\d{4})"
)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4})\b")
_NAME_RE = re.compile(
    r"(?:קוראים לי|שמי|אני|my name is|i am|i'm)\s+([A-Za-zא-ת][A-Za-zא-ת\s'\-]{1,40})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CapturedFields:
    name: str = ""
    phone: str = ""
    email: str = ""
    date: str = ""
    business: str = ""
    want: str = ""
    language: str = ""
    summary: str = ""

    def has_phone_or_email(self) -> bool:
        return bool(self.phone.strip() or self.email.strip())

    def to_contact(self, *, source: str = "") -> ContactRecord:
        return ContactRecord(
            name=self.name.strip(),
            phone=self.phone,
            email=self.email,
            date=self.date.strip(),
            business=self.business.strip(),
            source=source,
            language=self.language.strip(),
            want=self.want.strip(),
            summary=self.summary.strip(),
        )


def merge_fields(base: CapturedFields, incoming: CapturedFields) -> CapturedFields:
    return CapturedFields(
        name=incoming.name.strip() or base.name,
        phone=incoming.phone or base.phone,
        email=incoming.email or base.email,
        date=incoming.date.strip() or base.date,
        business=incoming.business.strip() or base.business,
        want=incoming.want.strip() or base.want,
        language=incoming.language.strip() or base.language,
        summary=incoming.summary.strip() or base.summary,
    )


def extract_fields(
    text: str,
    *,
    name: str = "",
    phone: str = "",
    email: str = "",
    date: str = "",
    language: str = "",
) -> CapturedFields:
    blob = text or ""
    found_email = normalize_email(email) or _first(_EMAIL_RE.findall(blob))
    found_phone = normalize_phone(phone) or normalize_phone(_first(_PHONE_RE.findall(blob)))
    found_date = (date or "").strip() or _first(_DATE_RE.findall(blob))
    found_name = (name or "").strip()
    if not found_name:
        match = _NAME_RE.search(blob)
        if match:
            found_name = match.group(1).strip()
    lang = language.strip() or ("he" if _looks_hebrew(blob) else "en" if blob.strip() else "")
    return CapturedFields(
        name=found_name,
        phone=found_phone,
        email=found_email,
        date=found_date,
        language=lang,
        summary=blob.strip()[:400],
    )


def apply_form(
    current: CapturedFields,
    *,
    name: str = "",
    phone: str = "",
    email: str = "",
    date: str = "",
    text: str = "",
) -> CapturedFields:
    from_text = extract_fields(text, name=name, phone=phone, email=email, date=date)
    return merge_fields(current, from_text)


def _first(values: list[str]) -> str:
    return values[0].strip() if values else ""


def _looks_hebrew(text: str) -> bool:
    return any("א" <= ch <= "ת" for ch in text)


def with_want(fields: CapturedFields, want: str) -> CapturedFields:
    return replace(fields, want=want.strip() or fields.want)
