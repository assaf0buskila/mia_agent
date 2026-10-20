"""Render one reviewable Telegram-HTML card per pending owner approval.

Owner tools used to hand back thin acknowledgement prose ("Prepared an exact Gmail
draft proposal.") and the turn glued its approve/reject buttons directly onto that
sentence. The owner could tap approve without ever seeing the recipient, the body, the
event time, or the CRM fields about to change.

This module is presentation only: it reads the exact envelope `propose_owner_action`
already persisted (via `read_owner_action`) and renders it, verbatim, into a safe,
escaped card. It never re-derives, invents or executes anything, and it never prints
raw JSON, a connection id or an account hash.

`render_owner_proposal_card` covers the current v2 `owner_external_write` kinds.
`render_owner_approval_card` is the one entry point callers should use for an actual
`ApprovalRow`: it also renders a bounded generic card for a pre-v2 ("legacy") approval
row that carries no full envelope, so an old outstanding approval still displays and
still resolves.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.domain.approvals import (
    ACTION_CALENDAR_CREATE,
    ACTION_CALENDAR_RESCHEDULE,
    ACTION_COMPOSIO_WRITE,
    ACTION_GMAIL_SEND,
    ACTION_LINKEDIN_COMPOSIO_WRITE,
    ACTION_PROPOSAL_HANDOFF,
    ACTION_WEBSITE_EDIT,
)
from app.integrations.telegram_format import (
    bold,
    code,
    esc,
    hebrew_datetime,
    isolate,
    join_sections,
    section,
)
from app.services.crm_v2 import SYSTEM_OWNED_FIELDS
from app.services.owner_actions import read_owner_action

_NOT_EXECUTED = "עדיין לא בוצע."
_MAX_CARD_VALUE_CHARS = 300
_DEFAULT_TIMEZONE = "Asia/Jerusalem"
_MAX_TELEGRAM_APPROVAL_CARD_CHARS = 3_800

# V2-28 (V2_PG_CONTRACTS.md section 13). A ledger proposal reaches Telegram as this compatibility
# body: the v2 proposal fields, with ``input``/``before``/``after`` projected from the proposal's
# immutable mutations. It is the same shape V2-20's ``proposal_v2`` emits, so a card stored by the
# Mia tool and a card stored by the website-lead path compare through one view.
LEDGER_PROPOSAL_API = "assaf-os.v2"
_LEDGER_GUARD_VERSION = 1
# The byte-stable proposal view: the immutable identity and effect of one proposal. ``status``
# (pending -> terminal), ``input`` (derived from before/after), the binding fields and the epoch
# are excluded; the ledger itself enforces the epoch (I12) and the exactly-once decision.
_LEDGER_VIEW_KEYS = (
    "workspace_id",
    "proposal_id",
    "request_id",
    "approval_request_id",
    "rejection_request_id",
    "kind",
    "entity",
    "record_id",
    "expected_revision",
    "expires_at",
    "before",
    "after",
)
_HTTPS_ORIGIN = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?")

_ASSAF_OS_ENTITY_LABELS = {
    "leads": "ליד",
    "clients": "לקוח",
    "works": "עבודה",
    "payments": "תקבול",
    "time": "דיווח זמן",
    "expenses": "הוצאה",
    "tasks": "משימה",
    "promote_lead": "הפיכת ליד ללקוח",
}
_ASSAF_OS_FIELD_LABELS = {
    "name": "שם",
    "phone": "טלפון",
    "email": "אימייל",
    "path": "מסלול",
    "status": "סטטוס",
    "source_note": "הערת מקור",
    "notes": "הערות",
    "person_id": "מזהה אדם",
    "title": "כותרת",
    "start_date": "תאריך התחלה",
    "agreed_amount_ils": "סכום מוסכם",
    "currency": "מטבע",
    "work_id": "מזהה עבודה",
    "date": "תאריך",
    "actual_amount_ils": "סכום בפועל",
    "kind": "סוג",
    "original_payment_id": "מזהה תקבול מקורי",
    "minutes": "דקות",
    "description": "תיאור",
    "category": "קטגוריה",
    "platform": "פלטפורמה",
    "vendor": "ספק",
    "amount_ils": "סכום",
    "payment_status": "מצב תשלום",
    "source_type": "סוג מקור",
    "source_key": "מפתח מקור",
    "due_date": "תאריך יעד",
}
_ASSAF_OS_MONEY_FIELDS = {"agreed_amount_ils", "actual_amount_ils", "amount_ils"}

_CONTACT_FIELD_LABELS: dict[str, str] = {
    "name": "שם",
    "phone": "טלפון",
    "email": "אימייל",
    "date": "תאריך",
    "business": "עסק",
    "source": "מקור",
    "language": "שפה",
    "want": "צורך",
    "status": "סטטוס",
    "summary": "תקציר",
    "next_step": "השלב הבא",
    "pinged": "פינג אחרון",
}

_RESOLUTION_LABELS: dict[str, str] = {
    "database": "מהמערכת",
    "sheet": "מהגיליון",
    "value": "ערך שהוזן",
}

_LEGACY_ACTION_LABELS: dict[str, str] = {
    ACTION_PROPOSAL_HANDOFF: "העברת ליד לטיפול",
    ACTION_WEBSITE_EDIT: "שינוי באתר",
    ACTION_GMAIL_SEND: "שליחת מייל",
    ACTION_CALENDAR_CREATE: "יצירת אירוע ביומן",
    ACTION_CALENDAR_RESCHEDULE: "העברת אירוע ביומן",
    ACTION_LINKEDIN_COMPOSIO_WRITE: "פעולה בלינקדאין",
    ACTION_COMPOSIO_WRITE: "פעולה חיצונית",
}


def _card(title: str, *sections: str, note: str = "") -> str:
    """Assemble one card: bold "לאישור: <title>", non-empty sections, then the note
    and the mandatory "not yet executed" line, always last so it can never be missed.
    """
    blocks = [bold(f"לאישור: {title}")]
    blocks.extend(block for block in sections if block and block.strip())
    if note:
        blocks.append(note)
    blocks.append(_NOT_EXECUTED)
    return join_sections(*blocks)


def _kv_lines(pairs: Iterable[tuple[str, Any, bool]]) -> str:
    """`label: value` lines, one per non-empty pair. `mono` picks code() over esc().

    The isolate goes INSIDE the `code()` call, never around it: `code(isolate(v))`,
    never `isolate(code(v))`. `owner_text(html=True)` treats a `<code>...</code>`
    span as opaque (so it is never re-isolated or dash-normalised a second time),
    which means it will never isolate content *inside* one either -- so a
    monospaced id/email that needs LTR protection has to get it here, at the
    point the tag is built, or it never gets it at all.
    """
    lines: list[str] = []
    for label, value, mono in pairs:
        text = str(value if value is not None else "").strip()
        if not text:
            continue
        rendered = code(isolate(text)) if mono else esc(text)
        lines.append(f"{bold(label)}: {rendered}")
    return "\n".join(lines)


def _bounded(value: Any, *, limit: int = _MAX_CARD_VALUE_CHARS) -> str:
    """A short, human-bounded rendering of an arbitrary argument value.

    Never used for LinkedIn post text (which must show in full) or for anything from
    `target` (connection ids, account hashes, schemas) -- only for the small typed
    arguments of a generic Composio write.
    """
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > limit:
        return text[:limit].rstrip() + " …"
    return text


def _parse_iso(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _local_when(value: object, *, timezone: str) -> str:
    moment = _parse_iso(value)
    if moment is None:
        return str(value or "").strip()
    return hebrew_datetime(moment, timezone=timezone or _DEFAULT_TIMEZONE)


def _format_duration(start: object, end: object) -> str:
    start_at, end_at = _parse_iso(start), _parse_iso(end)
    if start_at is None or end_at is None:
        return ""
    minutes = int((end_at - start_at).total_seconds() // 60)
    if minutes <= 0:
        return ""
    hours, remainder = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes} דקות"
    hour_word = "שעה" if hours == 1 else f"{hours} שעות"
    return hour_word if remainder == 0 else f"{hour_word} ו-{remainder} דקות"


def _card_gmail_create_draft(parameters: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    del target
    fields = _kv_lines(
        [
            ("אל", parameters.get("to"), True),
            ("נושא", parameters.get("subject"), False),
        ]
    )
    body = str(parameters.get("body") or "").strip()
    body_section = section("תוכן ההודעה", esc(body)) if body else ""
    return _card(
        "טיוטת מייל",
        fields,
        body_section,
        note="תיווצר טיוטה, לא יישלח מייל.",
    )


def _card_composio_write(parameters: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    slug = str(parameters.get("slug") or "").strip()
    toolkit = str(parameters.get("toolkit") or "").strip().upper()
    arguments = parameters.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}

    if slug.upper() == "GMAIL_SEND_DRAFT":
        resource = target.get("resource")
        resource = resource if isinstance(resource, dict) else {}
        identity = resource.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        fields = _kv_lines([(key, value, True) for key, value in identity.items()])
        return _card("שליחת מייל", fields, note="המייל יישלח.")

    if toolkit == "LINKEDIN":
        # A string argument (post text, visibility, ...) shows in full, never
        # bounded: this is the actual post the owner is publishing. A non-string
        # argument (nested media, lists, ...) has no natural "full" rendering, so
        # it gets the same formatted-and-bounded treatment as any other tool.
        fields = "\n".join(
            f"{bold(key)}: {esc(value if isinstance(value, str) else _bounded(value))}"
            for key, value in arguments.items()
            if str(value or "").strip()
        )
        return _card(
            "פרסום בלינקדאין",
            fields,
            note="הפעולה תבוצע בחשבון הלינקדאין המחובר.",
        )

    bounded_fields = "\n".join(
        f"{bold(key)}: {esc(_bounded(value))}"
        for key, value in arguments.items()
        if str(value or "").strip()
    )
    title = f"פעולה חיצונית ({toolkit})" if toolkit else "פעולה חיצונית"
    return _card(
        title,
        _kv_lines([("כלי", slug, True)]),
        bounded_fields,
        note="הפעולה תבוצע מול הספק המחובר.",
    )


def _card_calendar_create(parameters: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    del target
    timezone = str(parameters.get("timezone") or "").strip() or _DEFAULT_TIMEZONE
    fields = _kv_lines(
        [
            ("כותרת", parameters.get("title"), False),
            ("מתי", _local_when(parameters.get("start"), timezone=timezone), False),
            ("משך", _format_duration(parameters.get("start"), parameters.get("end")), False),
            ("מיקום", parameters.get("location"), False),
        ]
    )
    return _card("יצירת אירוע ביומן", fields, note="לא נשלחות הזמנות.")


def _card_calendar_reschedule(parameters: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    timezone = str(parameters.get("timezone") or "").strip() or _DEFAULT_TIMEZONE
    fields = _kv_lines(
        [
            ("מועד נוכחי", _local_when(target.get("start"), timezone=timezone), False),
            ("מועד חדש", _local_when(parameters.get("start"), timezone=timezone), False),
            (
                "משך חדש",
                _format_duration(parameters.get("start"), parameters.get("end")),
                False,
            ),
            ("מזהה אירוע", parameters.get("event_id") or target.get("event_id"), True),
        ]
    )
    return _card("העברת אירוע ביומן", fields, note="לא נשלחת התראה למוזמנים.")


def _card_crm_upsert(parameters: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    new_fields = parameters.get("fields")
    new_fields = new_fields if isinstance(new_fields, dict) else {}
    contact_id = str(target.get("contact_id") or "").strip()
    is_new = not contact_id
    old_fields = {} if is_new else target.get("fields")
    old_fields = old_fields if isinstance(old_fields, dict) else {}

    lines: list[str] = []
    for key, raw_new in new_fields.items():
        if key in SYSTEM_OWNED_FIELDS:
            continue
        new_value = str(raw_new if raw_new is not None else "").strip()
        if not new_value:
            continue
        old_value = str(old_fields.get(key, "") or "").strip()
        if not is_new and old_value == new_value:
            continue
        label = _CONTACT_FIELD_LABELS.get(key, key)
        if is_new or not old_value:
            lines.append(f"{bold(label)}: {esc(new_value)}")
        else:
            lines.append(f"{bold(label)}: {esc(old_value)} → {esc(new_value)}")

    status_line = (
        "איש קשר חדש." if is_new else _kv_lines([("איש קשר קיים", contact_id, True)])
    )
    title = "איש קשר חדש ב-CRM" if is_new else "עדכון איש קשר ב-CRM"
    return _card(title, status_line, "\n".join(lines))


def _card_crm_activity(parameters: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    fields = target.get("fields") if isinstance(target.get("fields"), dict) else {}
    contact_id = str(parameters.get("contact_id") or target.get("contact_id") or "").strip()
    name = str(fields.get("name") or "").strip()
    who = f"{name} ({contact_id})" if name and contact_id else name or contact_id
    body = _kv_lines(
        [
            ("איש קשר", who, False),
            ("סוג", parameters.get("kind"), False),
        ]
    )
    summary = str(parameters.get("summary") or "").strip()
    summary_section = section("תקציר", esc(summary)) if summary else ""
    return _card("רישום פעילות ב-CRM", body, summary_section)


def _card_crm_resolve_conflict(parameters: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    conflict = target.get("conflict") if isinstance(target.get("conflict"), dict) else {}
    contact = target.get("contact") if isinstance(target.get("contact"), dict) else {}
    contact_fields = contact.get("fields") if isinstance(contact.get("fields"), dict) else {}
    name = str(contact_fields.get("name") or "").strip()
    contact_id = str(parameters.get("contact_id") or contact.get("contact_id") or "").strip()
    who = f"{name} ({contact_id})" if name and contact_id else name or contact_id
    resolution = str(parameters.get("resolution") or "").strip()
    chosen = {
        "database": conflict.get("database_value"),
        "sheet": conflict.get("sheet_value"),
        "value": parameters.get("value"),
    }.get(resolution, "")
    fields = _kv_lines(
        [
            ("איש קשר", who, False),
            ("שדה", conflict.get("field_name"), False),
            ("במערכת", conflict.get("database_value"), False),
            ("בגיליון", conflict.get("sheet_value"), False),
            ("הכרעה", _RESOLUTION_LABELS.get(resolution, resolution), False),
            ("ערך סופי", chosen, False),
        ]
    )
    return _card("פתרון קונפליקט ב-CRM", fields)


def _card_sheets_write(
    parameters: Mapping[str, Any], target: Mapping[str, Any], *, append: bool
) -> str:
    del target
    spreadsheet_id = str(parameters.get("spreadsheet_id") or "").strip()
    a1_range = str(parameters.get("range") or "").strip()
    values = parameters.get("values")
    values = values if isinstance(values, list) else []
    rows_text = "\n".join(
        " | ".join(_bounded(cell, limit=120) for cell in row)
        for row in values
        if isinstance(row, list)
    )
    header = _kv_lines([("גיליון", spreadsheet_id, True), ("טווח", a1_range, True)])
    rows_section = section("שורות", esc(rows_text)) if rows_text else ""
    title = "הוספה לגיליון" if append else "עדכון גיליון"
    return _card(title, header, rows_section)


def _card_unknown(reason: str = "") -> str:
    detail = (
        f"סוג הבקשה ({esc(reason)}) אינו נתמך לתצוגה מפורטת."
        if reason
        else "פרטי הבקשה אינם זמינים לתצוגה."
    )
    return _card("פעולה", detail, "אפשר עדיין לאשר או לבטל דרך הכפתורים.")


def _card_assaf_os(parameters: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    proposal = target.get("proposal") if isinstance(target.get("proposal"), dict) else {}
    changed = proposal.get("input") if isinstance(proposal.get("input"), dict) else {}
    entity = str(proposal.get("entity") or "")
    before = proposal.get("before") if isinstance(proposal.get("before"), dict) else {}
    after = proposal.get("after") if isinstance(proposal.get("after"), dict) else {}
    before_currency = str(before.get("currency") or "ILS")
    after_currency = str(after.get("currency") or changed.get("currency") or before_currency)

    def shown(key: str, value: Any, currency: str) -> str:
        if value is None:
            return "ללא"
        if value == "":
            return "ריק"
        if value is True:
            return "כן"
        if value is False:
            return "לא"
        if key in _ASSAF_OS_MONEY_FIELDS and type(value) is int:
            major = f"{value / 100:,.2f}"
            return f"{major} ₪" if currency == "ILS" else f"{major} {currency}"
        return str(value)

    fields = _kv_lines(
        [
            ("סוג עדכון", _ASSAF_OS_ENTITY_LABELS.get(entity, entity), False),
            ("רשומה", proposal.get("record_id") or "חדשה", True),
            ("גרסה צפויה", proposal.get("expected_revision"), True),
            ("מזהה הצעה", parameters.get("proposal_id"), True),
            ("בתוקף עד", proposal.get("expires_at"), True),
        ]
    )
    changes = ""
    if entity == "promote_lead":
        lead = after.get("lead") if isinstance(after.get("lead"), dict) else {}
        client = after.get("client") if isinstance(after.get("client"), dict) else {}
        promotion_pairs = [
            ("מזהה אדם", client.get("person_id") or lead.get("person_id"), True),
            ("מזהה ליד", proposal.get("record_id") or lead.get("lead_id"), True),
            ("שם", client.get("name") or lead.get("name"), False),
            ("טלפון", client.get("phone") or lead.get("phone"), True),
            ("אימייל", client.get("email") or lead.get("email"), True),
            ("סטטוס לקוח", client.get("status"), False),
        ]
        changes = _kv_lines(promotion_pairs)
    else:
        lines = []
        for key in changed:
            label = _ASSAF_OS_FIELD_LABELS.get(key, key)
            old = shown(key, before.get(key), before_currency) if before else "לא קיים"
            new = shown(key, after.get(key, changed[key]), after_currency)
            lines.append(f"{bold(label)}: {esc(old)} ← {esc(new)}")
        changes = "\n".join(lines)

    provenance = target.get("contact_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    provenance_lines = _kv_lines(
        [
            ("טלפון שנקלט", provenance.get("raw_phone"), True),
            ("טלפון מנורמל", provenance.get("normalized_phone"), True),
            ("אימייל שנקלט", provenance.get("raw_email"), True),
            (
                "תיקון בעלים",
                (
                    "כן"
                    if any(
                        provenance.get(key)
                        for key in (
                            "owner_corrected_name",
                            "owner_corrected_phone",
                            "owner_corrected_email",
                        )
                    )
                    else None
                ),
                False,
            ),
        ]
    )
    if is_ledger_proposal(proposal):
        # The ledger, not the shared Sheet, holds the authority under backend=postgres.
        title = "עדכון ב-Assaf OS"
        note = "האישור יכתוב את הרשומה המוצעת ב-Assaf OS לאחר בדיקת יעד חוזרת."
    else:
        title = "עדכון בגיליון העסקי המשותף"
        note = "האישור יעדכן את הרשומה המוצעת בגיליון המשותף לאחר בדיקת יעד חוזרת."
    return _card(
        title,
        fields,
        section("לפני ואחרי", changes) if changes else "",
        section("מקור פרטי קשר", provenance_lines) if provenance_lines else "",
        note=note,
    )


def _card_assaf_os_lead_prepare(
    parameters: Mapping[str, Any], target: Mapping[str, Any]
) -> str:
    """Show captured identity and recommendation before the owner chooses a path."""
    source = target.get("source") if isinstance(target.get("source"), dict) else {}
    captured = source.get("captured") if isinstance(source.get("captured"), dict) else {}
    raw = source.get("raw_fields") if isinstance(source.get("raw_fields"), dict) else {}
    template = target.get("template") if isinstance(target.get("template"), dict) else {}
    missing = (
        template.get("missing_fields")
        if isinstance(template.get("missing_fields"), list)
        else []
    )
    raw_phone = str(raw.get("phone") or "")
    normalized_phone = str(captured.get("phone") or "")
    phone_value = normalized_phone
    if raw_phone and normalized_phone and raw_phone != normalized_phone:
        phone_value = f"{raw_phone} (נרמל ל-{normalized_phone})"
    contact = _kv_lines(
        [
            ("שם", captured.get("name") or "חסר", False),
            ("טלפון", phone_value or (raw_phone or "חסר"), True),
            ("אימייל", captured.get("email") or raw.get("email") or "חסר", True),
            ("מזהה מקור CRM", parameters.get("contact_id"), True),
        ]
    )
    recommendation = _kv_lines(
        [
            ("מסלול מומלץ", template.get("recommended_path") or "unknown", False),
            ("סיכום", template.get("summary") or "לא סופק", False),
            ("צעד הבא", template.get("next_step") or "לא סופק", False),
        ]
    )
    if missing:
        labels = {
            "name": "שם",
            "phone_or_email": "טלפון או אימייל",
            "phone_correction": "תיקון טלפון",
        }
        note = (
            "חסרים פרטים: "
            + ", ".join(labels.get(str(item), str(item)) for item in missing)
            + ". יש להשיב למיה עם התיקון והמסלול; לא הוכנה הצעת כתיבה."
        )
    else:
        note = (
            "בחירת מסלול רק מכינה הצעה מדויקת. לאחר מכן יישלח כרטיס אישור נוסף; "
            "רק אישור הכרטיס הבא יכול לכתוב לגיליון."
        )
    return _card(
        "הכנת ליד לגיליון העסקי",
        section("מקור שנקלט", contact),
        section("המלצת מיה", recommendation),
        note=note,
    )


def assaf_os_proposal_is_reviewable(envelope: Mapping[str, Any]) -> bool:
    """True only when Telegram can show the complete, untruncated proposal card."""
    try:
        parameters = envelope.get("parameters")
        target = envelope.get("target")
        if not isinstance(parameters, dict) or not isinstance(target, dict):
            return False
        proposal = target.get("proposal")
        if not isinstance(proposal, dict):
            return False
        if is_ledger_proposal(proposal):
            # Only a single generic proposal is decided from Telegram (I5). A statement batch,
            # an owner-direct commit or an import never gets a card, so never a button.
            if proposal.get("kind") != "proposal":
                return False
            proposal_view_bytes(proposal)  # every view key present and well-formed
        card = _card_assaf_os(parameters, target)
        return bool(card) and len(card) <= _MAX_TELEGRAM_APPROVAL_CARD_CHARS
    except Exception:  # noqa: BLE001 - malformed proposals must fail closed
        return False


def is_ledger_proposal(proposal: object) -> bool:
    """True for a v2 (ledger) proposal body; a v1 Apps Script body is never one."""
    return isinstance(proposal, Mapping) and proposal.get("api_version") == LEDGER_PROPOSAL_API


def _utc_millis(value: object) -> str:
    """One timestamp format for the view: UTC, millisecond precision (truncated), ``Z``."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("proposal_view_timestamp")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _plain_record(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return json.loads(json.dumps(dict(record), ensure_ascii=False, allow_nan=False))


def _changed_input(entity: str, before: Mapping[str, Any] | None, after: Mapping[str, Any]):
    from app.services.assaf_os import INPUT_FIELDS

    keys = INPUT_FIELDS.get(entity, frozenset())
    return {
        key: after[key]
        for key in after
        if key in keys and (before is None or before.get(key) != after.get(key))
    }


def ledger_proposal_body(row: Any) -> dict[str, Any]:
    """The card body of one ledger ``ProposalRow``, projected from its immutable mutations.

    A single-record proposal shows the changed input fields and the whole record before and
    after. A lead promotion (``promote_lead``) shows the lead and its client, before and after.
    Any other shape (a bulk, a batch, an import) has no card and raises ``ValueError``.
    """
    if getattr(row, "kind", None) != "proposal":
        raise ValueError("ledger_card_kind")
    mutations = tuple(row.mutations)
    if row.entity == "promote_lead":
        by_entity = {mutation.entity: mutation for mutation in mutations}
        if len(mutations) != 2 or set(by_entity) != {"leads", "clients"}:
            raise ValueError("ledger_card_promotion")
        lead, client = by_entity["leads"], by_entity["clients"]
        changed: dict[str, Any] = {}
        before: Any = {"lead": _plain_record(lead.before), "client": _plain_record(client.before)}
        after: Any = {"lead": _plain_record(lead.after), "client": _plain_record(client.after)}
    elif len(mutations) == 1:
        mutation = mutations[0]
        before = _plain_record(mutation.before)
        after = _plain_record(mutation.after)
        changed = _changed_input(mutation.entity, before, after)
    else:
        raise ValueError("ledger_card_mutations")
    return {
        "api_version": LEDGER_PROPOSAL_API,
        "workspace_id": row.workspace_id,
        "authority_epoch": row.epoch,
        "status": row.status,
        "proposal_id": row.proposal_id,
        "request_id": row.request_id,
        "approval_request_id": row.approval_id,
        "rejection_request_id": row.rejection_id,
        "approval_guard_version": _LEDGER_GUARD_VERSION,
        "actor_source": row.source,
        "kind": row.kind,
        "expires_at": _utc_millis(row.expires_at),
        "entity": row.entity,
        "record_id": row.record_id,
        "expected_revision": row.expected_revision,
        "input": changed,
        "before": before,
        "after": after,
    }


def proposal_view_bytes(body: Mapping[str, Any]) -> bytes:
    """The byte-stable proposal view ``current_target`` compares (V2-28, section 13).

    Canonical JSON (sorted keys, no whitespace) of the immutable keys only, with the timestamp
    normalized. Key order, ``Z`` versus ``+00:00``, the status and the binding fields never change
    the bytes; any change to the identity, the decision ids or the effect does.
    """
    if not isinstance(body, Mapping):
        raise ValueError("proposal_view_body")
    view: dict[str, Any] = {}
    for key in _LEDGER_VIEW_KEYS:
        if key not in body:
            raise ValueError("proposal_view_" + key)
        view[key] = body[key]
    view["expires_at"] = _utc_millis(view["expires_at"])
    return json.dumps(
        view, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def render_ledger_batch_notice(*, origin: str = "") -> str:
    """The only Telegram surface a statement batch ever gets: a notice with a link, no button.

    Batches are decided from OS only (I5), after a line-by-line review, so this text carries no
    callback token and the caller attaches no keyboard. ``origin`` is the OS app origin
    (``owner_os_origin``); anything but a plain https origin is dropped.
    """
    link = origin.strip()
    link_line = esc(link) if _HTTPS_ORIGIN.fullmatch(link) else "Assaf OS"
    return join_sections(
        bold("דף חיובים ממתין לבדיקה ב-Assaf OS"),
        "האישור נעשה רק בתוך Assaf OS, אחרי סקירת השורות. אין כאן כפתור אישור.",
        f"{bold('פתיחה')}: {link_line}",
    )


def _card_business(parameters: Mapping[str, Any], target: Mapping[str, Any], kind: str) -> str:
    """Render every approved finance value while keeping endpoint/key hashes internal."""
    title = {
        "business.save_deal": "שמירת עסקה",
        "business.save_schedule": "שמירת לוח חיובים",
        "business.record_payment": "רישום תקבול ידני",
        "business.record_expense": "רישום הוצאה",
        "business.save_project": "שמירת פרויקט",
        "business.recover_receipt": "בדיקת קבלה בלבד",
    }[kind]
    record = parameters.get("deal") or parameters.get("payment") or parameters.get("expense")
    record = record or parameters.get("project") or {}
    record = record if isinstance(record, dict) else {}

    def shown(value: Any) -> Any:
        if value is None:
            return "ללא"
        if value == "":
            return "ריק"
        if value is True:
            return "כן"
        if value is False:
            return "לא"
        return value

    pairs: list[tuple[str, Any, bool]] = []

    def add(source: Mapping[str, Any], key: str, label: str, mono: bool) -> None:
        if key in source:
            pairs.append((label, shown(source[key]), mono))

    for key, label, mono in (
        ("request_id", "מזהה בקשה", True),
        ("expected_id", "מזהה רשומה צפוי", True),
        ("expected_revision", "גרסה צפויה", True),
        ("expected_charge_revision", "גרסת חיוב צפויה", True),
        ("deal_id", "מזהה עסקה", True),
        ("number_of_charges", "מספר חיובים", True),
        ("first_due_date", "מועד חיוב ראשון", True),
        ("confirmed_received", "אישור שהכסף התקבל", False),
    ):
        add(parameters, key, label, mono)
    for key, label, mono in (
        ("id", "מזהה רשומה", True),
        ("charge_id", "מזהה חיוב", True),
        ("project_id", "מזהה פרויקט", True),
        ("title", "כותרת", False),
        ("amount_minor", "סכום באגורות", True),
        ("currency", "מטבע", True),
        ("payment_plan", "תוכנית תשלום", False),
        ("start_date", "תאריך התחלה", True),
        ("status", "סטטוס", False),
        ("received_at", "התקבל בתאריך", True),
        ("occurred_on", "תאריך הוצאה", True),
        ("deadline", "תאריך יעד", True),
        ("notes", "הערות", False),
    ):
        add(record, key, label, mono)
    body = _kv_lines(pairs)
    charge_ids = parameters.get("charge_ids")
    charges = ""
    if isinstance(charge_ids, list):
        charges = section("מזהי חיובים", "\n".join(code(isolate(str(item))) for item in charge_ids))
    prospective = target.get("prospective_charges")
    schedule = ""
    if isinstance(prospective, list):
        schedule = section(
            "לוח חיובים מדויק",
            "\n".join(
                f"{code(isolate(str(item.get('id', ''))))} | "
                f"{esc(str(item.get('due_date', '')))} | "
                f"{esc(str(item.get('amount_minor', '')))} אגורות"
                for item in prospective
                if isinstance(item, dict)
            ),
        )
    expected = _kv_lines(
        [
            ("יעד מאושר", target.get("record_id"), True),
            ("גרסת יעד", target.get("expected_revision"), True),
            ("סביבה", target.get("mode"), True),
            ("מקור", target.get("source"), True),
        ]
    )
    note = ""
    if kind == "business.record_payment":
        note = "האישור מאשר ידנית שהכסף התקבל; ללא אישור לא יירשם תקבול."
    elif kind == "business.recover_receipt":
        note = "הפעולה בודקת קבלה קיימת בלבד ואינה שולחת שוב את הכתיבה המקורית."
    return _card(title, body, charges, schedule, expected, note=note)


_CARD_RENDERERS = {
    "gmail.create_draft": _card_gmail_create_draft,
    "composio.write": _card_composio_write,
    "calendar.create": _card_calendar_create,
    "calendar.reschedule": _card_calendar_reschedule,
    "crm.upsert": _card_crm_upsert,
    "crm.activity": _card_crm_activity,
    "crm.resolve_conflict": _card_crm_resolve_conflict,
    "sheets.update": lambda p, t: _card_sheets_write(p, t, append=False),
    "sheets.append": lambda p, t: _card_sheets_write(p, t, append=True),
    "business.save_deal": lambda p, t: _card_business(p, t, "business.save_deal"),
    "business.save_schedule": lambda p, t: _card_business(p, t, "business.save_schedule"),
    "business.record_payment": lambda p, t: _card_business(p, t, "business.record_payment"),
    "business.record_expense": lambda p, t: _card_business(p, t, "business.record_expense"),
    "business.save_project": lambda p, t: _card_business(p, t, "business.save_project"),
    "business.recover_receipt": lambda p, t: _card_business(p, t, "business.recover_receipt"),
    "assaf_os.approve": _card_assaf_os,
    "assaf_os.lead.prepare": _card_assaf_os_lead_prepare,
}


def render_owner_proposal_card(envelope: Mapping[str, Any]) -> str:
    """One escaped Telegram-HTML card for a stored v2 owner-action envelope.

    `envelope` is exactly what `read_owner_action` returns: `{"kind", "parameters",
    "target", "version"}`. Never raises: a malformed or unrecognized envelope renders
    the generic safe card instead of raw JSON.
    """
    kind = str(envelope.get("kind") or "").strip()
    parameters = envelope.get("parameters")
    target = envelope.get("target")
    parameters = parameters if isinstance(parameters, dict) else {}
    target = target if isinstance(target, dict) else {}
    renderer = _CARD_RENDERERS.get(kind)
    if renderer is None:
        return _card_unknown(kind)
    try:
        return renderer(parameters, target)
    except Exception:  # noqa: BLE001 - a formatting bug must never block the turn
        return _card_unknown(kind)


def _legacy_website_parts(row: Any) -> tuple[str, str]:
    try:
        data = json.loads(getattr(row, "proposed_parameters", "") or "{}")
    except (TypeError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    before, after = data.get("before"), data.get("after")
    return (
        before.strip() if isinstance(before, str) else "",
        after.strip() if isinstance(after, str) else "",
    )


def _render_legacy_card(row: Any) -> str:
    """A bounded card for a pre-v2 approval row, which stores no full envelope."""
    action = str(getattr(row, "action", "") or "")
    label = _LEGACY_ACTION_LABELS.get(action, action or "פעולה")
    subject = str(
        getattr(row, "lead_id", "") or getattr(row, "resource_id", "") or ""
    ).strip()
    fields = _kv_lines(
        [
            ("מזהה", subject, True),
            ("סיכון", getattr(row, "risk", ""), True),
        ]
    )
    before_after = ""
    if action == ACTION_WEBSITE_EDIT:
        before, after = _legacy_website_parts(row)
        before_after = join_sections(
            section("לפני", esc(before)) if before else "",
            section("אחרי", esc(after)) if after else "",
        )
    return _card(label, fields, before_after)


def render_owner_approval_card(row: Any) -> str:
    """One card for any approval row -- current v2 envelope or pre-v2 legacy row.

    `row` may be `None` (an id the caller could not resolve): this still returns a
    safe generic card rather than raising, so a stale lookup never drops the approve/
    reject buttons that are attached to the same row's exact id regardless.
    """
    if row is None:
        return _card_unknown()
    try:
        envelope = read_owner_action(row)
    except AttributeError:
        envelope = None
    if envelope is not None:
        return render_owner_proposal_card(envelope)
    try:
        return _render_legacy_card(row)
    except AttributeError:
        return _card_unknown()


def pending_approval_cards(store: Any, *, limit: int = 5) -> tuple[list[tuple[str, str]], int]:
    """Up to `limit` newest-first pending proposals as `(approval_id, card_html)`.

    Returns the cards plus how many additional pending rows did not fit, so the
    caller can add its own "ועוד N" notice. `store.list_all_pending_approvals()` is
    already newest-first (see `LeadStore.list_all_pending_approvals`).
    """
    rows = list(store.list_all_pending_approvals())
    selected = rows[: max(0, limit)]
    cards = [
        (str(row.approval_id).strip(), render_owner_approval_card(row))
        for row in selected
        if str(getattr(row, "approval_id", "") or "").strip()
    ]
    return cards, max(0, len(rows) - len(selected))
