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
from collections.abc import Iterable, Mapping
from datetime import datetime
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
    join_sections,
    section,
)
from app.services.crm_v2 import SYSTEM_OWNED_FIELDS
from app.services.owner_actions import read_owner_action

_NOT_EXECUTED = "עדיין לא בוצע."
_MAX_CARD_VALUE_CHARS = 300
_DEFAULT_TIMEZONE = "Asia/Jerusalem"

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
    """`label: value` lines, one per non-empty pair. `mono` picks code() over esc()."""
    lines: list[str] = []
    for label, value, mono in pairs:
        text = str(value if value is not None else "").strip()
        if not text:
            continue
        rendered = code(text) if mono else esc(text)
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
        note="תיווצר טיוטה — לא יישלח מייל.",
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
