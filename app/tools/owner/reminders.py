"""Internal reminders: server-parsed times, canonical contacts, no calendar calls."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.capabilities.types import Principal
from app.db.models import CrmContactRow
from app.domain.reminder_time import ReminderTimeError, parse_reminder_time
from app.services.reminders import ReminderService
from app.tools.owner.types import ToolContext, ToolResult

_CREATE = re.compile(
    r"^\s*(?:(?:מיה|Mia)[,\s]+)?(?:(?:please|can you|could you|בבקשה|תוכלי)\s+)?"
    r"(?:remind me|תזכירי לי|תזכיר לי|צור תזכורת|צרי תזכורת)",
    re.I,
)
_LIST = re.compile(r"(?:list.*reminders|my reminders|רשימת תזכורות|התזכורות שלי|הצג תזכורות)", re.I)
_ID = re.compile(r"(?<![a-f0-9])[a-f0-9]{32}(?![a-f0-9])")
_ACTIONS = {
    "complete": r"(?:\bcomplete\b|\bmark done\b|השלם|השלימי|סיימתי|בוצע)",
    "cancel": r"(?:\bcancel\b|בטל|בטלי)",
    "snooze": r"(?:\bsnooze\b|דחה|דחי)",
    "reschedule": r"(?:\breschedule\b|שנה.*מועד|שני.*מועד)",
}


def _explicit_change(text: str, action: str) -> bool:
    prefix = r"^\s*(?:(?:מיה|Mia)[,\s]+)?(?:(?:please|can you|could you|בבקשה|תוכלי)\s+)?"
    return action in _ACTIONS and bool(
        re.match(prefix + _ACTIONS[action] + r"(?![a-z\u0590-\u05ff])", text, re.I)
    )


_REASONS = {
    "missing_date": "לאיזה תאריך לקבוע? שלח בקשה מלאה עם תאריך ושעה.",
    "missing_time": "באיזו שעה? שלח בקשה מלאה עם תאריך ושעה, למשל מחר ב־11:30.",
    "ambiguous_time": "יש כמה אפשרויות לזמן. שלח תאריך אחד ושעה אחת בפורמט 24 שעות.",
    "past": "המועד כבר עבר. בחר מועד עתידי.",
    "dst_gap": "השעה הזו לא קיימת ביום המעבר לשעון קיץ. בחר שעה אחרת.",
    "dst_fold": "השעה הזו מופיעה פעמיים במעבר לשעון חורף. ציין גם היסט UTC.",
}


def _allowed(ctx: ToolContext) -> bool:
    return (
        ctx.principal.is_owner()
        and ctx.principal.source == "telegram"
        and ctx.principal.actor_id in ctx.settings.telegram_owner_user_id_set()
        and getattr(ctx, "private_owner_chat", False)
    )


def _format(row) -> str:
    when = row.scheduled_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    local = when.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%d/%m/%Y %H:%M")
    linked = f"איש קשר: {row.contact_id}" if row.contact_id else "ללא שיוך לאיש קשר"
    return (
        f"{local} · {row.text[:180]}\n{linked}\n"
        f"מזהה: {row.id} · גרסה: {row.revision} · {row.status} · {row.delivery_state}"
    )


def _error(exc: Exception) -> ToolResult:
    if isinstance(exc, ReminderTimeError):
        reason = getattr(exc, "reason", "invalid_time")
        return ToolResult(
            ok=False,
            text=_REASONS.get(reason, "לא הצלחתי לפרש את הזמן. ציין תאריך ושעה."),
            error=reason,
        )
    candidates = getattr(exc, "candidates", None)
    if candidates:
        # Server-owned canonical IDs; owner can restate with contact:<ID>.
        return ToolResult(
            ok=False,
            text="נמצאו כמה אנשי קשר. שלח בקשה עם contact: ומזהה מדויק.\n"
            + "\n".join(f"{candidate.name}: {candidate.id}" for candidate in candidates),
            error=type(exc).__name__,
        )
    return ToolResult(
        ok=False,
        text="התזכורת לא נשמרה או עודכנה. בדוק את המזהה, השיוך והגרסה. "
        "לשמירה ללא שיוך, הוסף 'ללא שיוך'. אם המסירה כבר התחילה, נסה שוב לאחר סיומה.",
        error=type(exc).__name__,
    )


def _contact_query(text: str) -> str:
    explicit = re.search(r"contact:\s*([^\s,;]+)", text, re.I)
    if explicit:
        return explicit[1]
    match = re.search(
        r"(?:לחזור ל|להתקשר ל|לשלוח מייל ל|לכתוב ל|לדבר עם|לשלוח הודעה ל)"
        r"(.+?)(?: לגבי| בקשר| מחר| היום| בעוד| ב-?\d|$)",
        text,
    )
    if match is None:
        match = re.search(
            r"(?:call|contact|email|message|write to|follow up with)\s+"
            r"(.+?)(?: about| regarding| tomorrow| today| at| in \d|$)",
            text,
            re.I,
        )
    return match[1].strip(" ,.") if match else ""


def _reminder_create(ctx: ToolContext, args: dict) -> ToolResult:
    del args  # A model cannot substitute a contact, text, or scheduled timestamp.
    if not _allowed(ctx):
        return ToolResult(ok=False, error="unauthorized")
    if not ctx.settings.reminders_enabled or ctx.kill_switch or ctx.demo_active:
        return ToolResult(ok=False, text="התזכורות הפנימיות אינן פעילות כרגע.", error="disabled")
    if not _CREATE.search(ctx.owner_text) or not ctx.source_ref:
        return ToolResult(ok=False, error="explicit_reminder_request_required")
    now = ctx.now or datetime.now(UTC)
    try:
        service = ReminderService(ctx.store.session)
        existing = service.get_by_source_event(
            owner_id=ctx.principal.actor_id, source_event_id=ctx.source_ref
        )
        if existing is not None:
            ctx.store.session.commit()
            return ToolResult(ok=True, text="התזכורת נשמרה בעבר.\n" + _format(existing))
        scheduled = parse_reminder_time(ctx.owner_text, now=now)
        result = service.create(
            owner_id=ctx.principal.actor_id,
            source_event_id=ctx.source_ref,
            text=ctx.owner_text,
            scheduled_at=scheduled,
            contact_query=_contact_query(ctx.owner_text),
            explicit_unlinked=bool(
                re.search(r"ללא שיוך|unlinked|without contact", ctx.owner_text, re.I)
            ),
            now=now,
        )
    except Exception as exc:
        return _error(exc)
    return ToolResult(ok=True, text="התזכורת הפנימית נשמרה.\n" + _format(result.reminder))


def _reminder_list(ctx: ToolContext, args: dict) -> ToolResult:
    del args  # Page is taken from the owner's current request, never a model-selected scope.
    if not _allowed(ctx):
        return ToolResult(ok=False, error="unauthorized")
    if not ctx.settings.reminders_enabled:
        return ToolResult(ok=False, text="התזכורות הפנימיות אינן פעילות כרגע.", error="disabled")
    match = re.search(r"(?:page|עמוד)\s*(\d+)", ctx.owner_text, re.I)
    page = max(1, min(int(match[1]), 10000)) if match else 1
    rows = ReminderService(ctx.store.session).list(
        owner_id=ctx.principal.actor_id, limit=10, offset=(page - 1) * 10
    )
    content = "\n\n".join(_format(row) for row in rows) or "אין תזכורות פעילות בעמוד הזה."
    if len(rows) == 10:
        content += f"\nלעמוד הבא: התזכורות שלי עמוד {page + 1}"
    return ToolResult(
        ok=True,
        text=content,
        max_chars=5000,
    )


def _reminder_snapshot(ctx: ToolContext, args: dict) -> ToolResult:
    del args
    if not _allowed(ctx):
        return ToolResult(ok=False, error="unauthorized")
    result = ReminderService(ctx.store.session).snapshot(
        owner_id=ctx.principal.actor_id, now=ctx.now or datetime.now(UTC)
    )
    payload = {
        "kind": "internal_reminder",
        "counts": result.counts,
        "upcoming": [
            {
                "id": row.id,
                "text": row.text[:160],
                "scheduled_at": row.scheduled_at.isoformat(),
                "contact_id": row.contact_id,
                "revision": row.revision,
            }
            for row in result.upcoming
        ],
    }
    return ToolResult(ok=True, text=json.dumps(payload, ensure_ascii=False))


def _reminder_change(ctx: ToolContext, args: dict) -> ToolResult:
    if not _allowed(ctx):
        return ToolResult(ok=False, error="unauthorized")
    if not ctx.settings.reminders_enabled or ctx.kill_switch or ctx.demo_active:
        return ToolResult(ok=False, error="disabled")
    reminder_id = str(args.get("reminder_id") or "")
    action = str(args.get("action") or "")
    if re.search(r"\b(?:don't|do not|never)\b|(?:^|\s)(?:אל|לא)\s", ctx.owner_text, re.I):
        return ToolResult(
            ok=False,
            text="הבקשה אינה הוראה מפורשת לשנות תזכורת.",
            error="explicit_reminder_change_required",
        )
    if reminder_id not in ctx.owner_text or not _explicit_change(ctx.owner_text, action):
        return ToolResult(
            ok=False,
            text="הבקשה אינה הוראה מפורשת לשנות תזכורת.",
            error="explicit_reminder_change_required",
        )
    now = ctx.now or datetime.now(UTC)
    try:
        scheduled = None
        if action == "reschedule":
            scheduled = parse_reminder_time(ctx.owner_text, now=now)
        elif action == "snooze":
            timing_text = ctx.owner_text.replace(reminder_id, "")
            timing_text = re.sub(r"(?:revision|גרסה)[:\s]+\d+", "", timing_text, flags=re.I)
            scheduled = (
                parse_reminder_time(ctx.owner_text, now=now)
                if re.search(
                    r"\d|\b(?:in|for|at|after|today|tomorrow|next|hours?|minutes?|noon)\b|"
                    r"בעוד|אחרי|היום|מחר|דקות|שעות|שעה|בוקר|ערב",
                    timing_text,
                    re.I,
                )
                else now + timedelta(minutes=15)
            )
        result = ReminderService(ctx.store.session).change(
            owner_id=ctx.principal.actor_id,
            reminder_id=reminder_id,
            action=action,
            expected_revision=int(args["revision"]),
            scheduled_at=scheduled,
            now=now,
        )
    except Exception as exc:
        return _error(exc)
    return ToolResult(ok=True, text="התזכורת עודכנה.\n" + _format(result.reminder))


def handle_reminder_message(
    *, item: dict, principal: Principal, store, settings, now=None
) -> str | None:
    """Intercept explicit reminder intents before general tools can touch Calendar."""
    text = item.get("owner_request_text", item.get("text", "")).strip()
    create = bool(_CREATE.search(text))
    listing = bool(_LIST.search(text))
    reminder_id = _ID.search(text)
    action = (
        next((key for key, pattern in _ACTIONS.items() if re.search(pattern, text, re.I)), "")
        if reminder_id
        else ""
    )
    if not (create or listing or action):
        return None
    if item.get("chat_id", item["from"]) != item["from"]:
        return "תזכורות פנימיות זמינות רק בצ'אט הפרטי של הבעלים."
    ctx = ToolContext(
        store=store,
        brain=None,
        settings=settings,
        principal=principal,
        embedding_port=None,
        source_ref=item["id"],
        private_owner_chat=True,
        owner_text=text,
        now=now,
        kill_switch=settings.kill_switch,
        demo_active=settings.demo_mode,
    )
    if listing:
        result = _reminder_list(ctx, {})
    elif action:
        revision = re.search(r"(?:revision|גרסה)[:\s]+(\d+)", text, re.I)
        if revision is None:
            return "ציין גם את הגרסה שמופיעה ברשימת התזכורות, כדי למנוע שינוי של תזכורת שהתעדכנה."
        result = _reminder_change(
            ctx, {"reminder_id": reminder_id[0], "action": action, "revision": int(revision[1])}
        )
    else:
        result = _reminder_create(ctx, {})
    return result.text or "הפעולה לא בוצעה."


def handle_reminder_callback(*, callback: dict, store, settings, now=None) -> str:
    if (
        callback["from"] not in settings.telegram_owner_user_id_set()
        or callback.get("chat_id") != callback["from"]
    ):
        return "הפעולה אינה מורשית."
    match = re.fullmatch(
        r"rm:(complete|snooze|contact):([a-f0-9]{32}):(\d{1,8})", callback.get("data", "")
    )
    if not match or not settings.reminders_enabled:
        return "הכפתור אינו זמין."
    action, reminder_id, revision = match.groups()
    service = ReminderService(store.session)
    try:
        row = service.get(owner_id=callback["from"], reminder_id=reminder_id)
        if row is None or row.revision != int(revision):
            return "התזכורת השתנתה. פתח את רשימת התזכורות המעודכנת."
        if action == "contact":
            contact = store.session.get(CrmContactRow, row.contact_id) if row.contact_id else None
            if contact is None:
                return "אין איש קשר משויך."
            fields = json.loads(contact.fields_json or "{}")
            return "איש קשר\n" + "\n".join(
                f"{key}: {fields.get(key, '')}" for key in ("name", "phone", "email", "company")
            )
        if settings.kill_switch or settings.demo_mode:
            return "הפעולה מושהית כרגע."
        moment = now or datetime.now(UTC)
        result = service.change(
            owner_id=callback["from"],
            reminder_id=reminder_id,
            action=action,
            expected_revision=int(revision),
            scheduled_at=moment + timedelta(minutes=15) if action == "snooze" else None,
            now=moment,
        )
        return "התזכורת עודכנה.\n" + _format(result.reminder)
    except Exception as exc:
        return _error(exc).text
