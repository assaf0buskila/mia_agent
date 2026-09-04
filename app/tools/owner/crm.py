"""Owner CRM tools over the locked Contacts workbook."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from app.domain.tools import AdapterHttpError
from app.domain.two_state import is_sheets_health_ask
from app.integrations.sheets import build_sheets_port
from app.surfaces.crm import (
    ACTIVITY_TAB,
    CONTACTS_TAB,
    ContactRecord,
    CrmDenied,
    build_contacts_crm,
    log_contact,
)
from app.tools.owner.types import OUTCOME_PARTIAL, ToolContext, ToolResult, _crm_spreadsheet_id


def _crm_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or ctx.owner_text or "").strip()
    port = ctx.sheets or build_sheets_port(ctx.settings)
    reader = getattr(port, "read_locked_contacts", None)
    rows = reader() if callable(reader) else []
    read_activity = _read_locked_activity(port, ctx)
    activity_failed = read_activity is None
    activity_rows = read_activity or []
    # Reading half of what was asked for is not a success. Every exit below carries
    # this so a broken Activity tab can never look like a quiet one.
    partial = OUTCOME_PARTIAL if activity_failed else ""
    header = (
        "Google Sheets CRM is connected. Live tabs: Contacts and Activity. "
        "No lead ids. The sheet URL is already known."
    )
    if not rows and not activity_rows:
        if activity_failed:
            return ToolResult(
                ok=True,
                outcome=OUTCOME_PARTIAL,
                text=(
                    f"{header} Contacts is empty so far. {ACTIVITY_TAB} could not be "
                    "read on this attempt, so this answer is incomplete."
                ),
            )
        return ToolResult(ok=True, text=f"{header} Contacts is empty so far.")
    body = rows[1:] if len(rows) > 1 else rows
    needle = query.casefold()
    health = _crm_health_query(query)
    matches: list[str] = []
    if not health:
        for row in body:
            blob = " | ".join(str(cell) for cell in row)
            if "lead_" in blob.lower() or "01 Leads" in blob:
                continue
            if not needle or needle in blob.casefold():
                matches.append(blob)
            if len(matches) >= 8:
                break
    lines = [header, f"{CONTACTS_TAB} rows including header: {len(rows)}."]
    if activity_rows:
        lines.append(f"{ACTIVITY_TAB} rows including header: {len(activity_rows)}.")
    elif activity_failed:
        lines.append(
            f"{ACTIVITY_TAB} could not be read on this attempt. The Contacts lines "
            "below are complete; the Activity log is missing from this answer."
        )
    else:
        lines.append(f"{ACTIVITY_TAB} is the log tab.")
    if health:
        return ToolResult(ok=True, outcome=partial, text="\n".join(lines))
    if not matches:
        lines.append("No Contacts row matched.")
        return ToolResult(ok=True, outcome=partial, text="\n".join(lines))
    lines.append("Contacts:")
    lines.extend(matches)
    return ToolResult(ok=True, outcome=partial, text="\n".join(lines))


def _crm_health_query(query: str) -> bool:
    return is_sheets_health_ask(query)


def _read_locked_activity(port: object, ctx: ToolContext) -> list[list[str]] | None:
    """Activity rows, or None when the tab could not be read.

    None and [] are different answers. [] means the tab is empty; None means the
    read failed, and the caller has to say so rather than let a broken integration
    read as a quiet log.
    """
    reader = getattr(port, "read_values", None)
    if not callable(reader):
        return []
    try:
        rows = reader(
            spreadsheet_id=_crm_spreadsheet_id(ctx),
            a1_range=f"{ACTIVITY_TAB}!A1:E20",
        )
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    cleaned: list[list[str]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        cells = [str(cell) for cell in row]
        blob = " ".join(cells)
        if "lead_" in blob.lower() or "01 Leads" in blob:
            continue
        cleaned.append(cells)
    return cleaned


def _crm_upsert(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    record = ContactRecord(
        name=str(args.get("name") or "").strip(),
        phone=str(args.get("phone") or "").strip(),
        email=str(args.get("email") or "").strip(),
        date=str(args.get("date") or "").strip(),
        business=str(args.get("business") or "").strip(),
        source=str(args.get("source") or "telegram").strip() or "telegram",
        language=str(args.get("language") or "").strip(),
        want=str(args.get("want") or "").strip(),
        status=str(args.get("status") or "").strip(),
        summary=str(args.get("summary") or ctx.owner_text or "").strip()[:500],
        next_step=str(args.get("next_step") or "").strip(),
    )
    if not record.has_contact_key():
        return ToolResult(ok=True, text="Need a phone or email before I write Contacts.")
    blob = " ".join(record.cells())
    if "lead_" in blob.lower():
        return ToolResult(ok=False, error="lead ids are not used")
    port = ctx.sheets or build_sheets_port(ctx.settings)
    crm = build_contacts_crm(ctx.settings, port)
    # Durable duplicate protection, same shape as the sheets_append/update writes:
    # keyed on the owner event plus the exact row, so a retried owner message cannot
    # write the contact twice.
    canonical = json.dumps(
        {"event": ctx.source_ref, "operation": "crm_upsert", "cells": record.cells()},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    key = sha256(canonical.encode("utf-8")).hexdigest()
    if not ctx.store.claim_operation(scope="owner_crm_write", key=key):
        return ToolResult(
            ok=True, text="This exact Contacts row was already written for this message."
        )
    try:
        log_contact(
            crm,
            record,
            who="אסף",
            channel="telegram",
            action="עדכון איש קשר",
            result="נרשם",
        )
    except CrmDenied as exc:
        ctx.store.fail_operation(scope="owner_crm_write", key=key)
        return ToolResult(ok=False, error=str(exc) or "lead ids are not used")
    except AdapterHttpError as exc:
        # Composio reported the write failed, or the response did not match the
        # adapter contract. Either way it is NOT a success: saying "Wrote Contacts"
        # here is how a rejected CRM write reached Assaf as done.
        # The row may still have landed before a transport failure, so keep the claim
        # completed rather than freeing it for a silent duplicate retry.
        ctx.store.complete_operation(
            scope="owner_crm_write", key=key, result_json='{"ok":false}'
        )
        return ToolResult(
            ok=False, error=f"Contacts write failed ({exc.tool_status()}); nothing was saved."
        )
    except (RuntimeError, ValueError, OSError):
        ctx.store.fail_operation(scope="owner_crm_write", key=key)
        return ToolResult(ok=False, error="Contacts write failed; nothing was saved.")
    ctx.store.complete_operation(
        scope="owner_crm_write", key=key, result_json='{"ok":true}'
    )
    return ToolResult(
        ok=True,
        text=f"Wrote Contacts on {_crm_spreadsheet_id(ctx)}.",
    )
