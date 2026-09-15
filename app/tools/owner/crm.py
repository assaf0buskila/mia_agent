"""Owner CRM tools over the locked Contacts workbook."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.domain.tools import AdapterHttpError
from app.domain.two_state import is_sheets_health_ask
from app.integrations.sheets import build_sheets_port
from app.services.crm_v2 import CONTACT_FIELDS, CrmError, CrmService
from app.services.owner_actions import propose_owner_action, sync_owner_crm_sheet_in_session
from app.surfaces.crm import (
    ACTIVITY_TAB,
    CONTACTS_TAB,
    ContactRecord,
)
from app.surfaces.owner_crm_intent import is_explicit_owner_crm_write_intent
from app.tools.owner.types import ToolContext, ToolResult, _crm_spreadsheet_id


def _crm_workspace_missing(ctx: ToolContext, port: object) -> bool:
    """True only when the CRM tabs are positively confirmed absent.

    Read-only: uses ``list_sheet_names`` (a plain values GET), never
    ``ensure_crm_workspace`` (which creates/repairs tabs and headers) -- a read must
    not provision the destination it is about to read from. When the check itself
    cannot be answered (no lister, or the call fails), this says "not missing" so a
    transient read problem is reported by the sync step that follows, not swallowed
    here as a false "not set up".
    """
    lister = getattr(port, "list_sheet_names", None)
    if not callable(lister):
        return False
    try:
        existing = set(lister(spreadsheet_id=_crm_spreadsheet_id(ctx)))
    except Exception:
        return False
    return CONTACTS_TAB not in existing or ACTIVITY_TAB not in existing


def _crm_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or ctx.owner_text or "").strip()
    port = ctx.sheets or build_sheets_port(ctx.settings)
    if _crm_workspace_missing(ctx, port):
        return ToolResult(ok=False, error="the CRM sheet is not set up")
    problem = _sync_current_sheet_edits(ctx, port)
    if problem:
        return ToolResult(ok=False, error=problem)
    contacts = CrmService(ctx.store.session).lookup(query=query or None, limit=20)
    if not contacts:
        return ToolResult(ok=True, text="No CRM contact matched.")
    return ToolResult(
        ok=True,
        text="CRM contacts:\n"
        + "\n".join(
            f"- {item.id} rev {item.revision}: "
            + " | ".join(value for value in item.fields.values() if value)
            for item in contacts
        ),
    )



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
    if not is_explicit_owner_crm_write_intent(ctx.owner_text):
        return ToolResult(
            ok=False,
            error="Contacts write requires an explicit affirmative request in this owner message.",
        )
    record = ContactRecord(
        name=str(args.get("name") or "").strip(),
        phone=str(args.get("phone") or "").strip(),
        email=str(args.get("email") or "").strip(),
        date=str(args.get("date") or "").strip(),
        business=str(args.get("business") or "").strip(),
        source=str(args.get("source") or "").strip(),
        language=str(args.get("language") or "").strip(),
        want=str(args.get("want") or "").strip(),
        status=str(args.get("status") or "").strip(),
        summary=str(args.get("summary") or "").strip()[:500],
        next_step=str(args.get("next_step") or "").strip(),
    )
    if not record.has_contact_key():
        return ToolResult(ok=True, text="Need a phone or email before I write Contacts.")
    blob = " ".join(record.cells())
    if "lead_" in blob.lower():
        return ToolResult(ok=False, error="lead ids are not used")
    fields = dict(zip(CONTACT_FIELDS, record.cells(), strict=True))
    port = ctx.sheets or build_sheets_port(ctx.settings)
    return _propose_crm_upsert(
        ctx,
        fields=fields,
        port=port,
        success_text="Prepared an exact CRM proposal. Nothing was written.",
        new_contact_defaults={
            "source": "telegram",
            "summary": str(ctx.owner_text or "").strip()[:500],
        },
    )


def _propose_crm_upsert(
    ctx: ToolContext,
    *,
    fields: dict[str, str],
    port: object,
    success_text: str,
    new_contact_defaults: dict[str, str] | None = None,
) -> ToolResult:
    """Turn CRM contact fields into one exact, durable `crm.upsert` proposal.

    Identity is never taken from the model: `snapshot_identity` binds the proposal to
    either the existing contact matched by phone/email, or to observed absence for a
    new one. Shared by the direct owner CRM tool and the Contacts-row Sheets grammar
    in `app.tools.owner.sheets`, so both produce the exact same proposal shape.

    `new_contact_defaults` fill empty fields only when no contact matched. `capture`
    merges every non-empty value, so a default applied to an existing contact would
    overwrite its real source or summary on approval. A new contact's snapshot hashes
    only phone/email, so filling defaults after it leaves the bound target unchanged.
    """
    problem = _sync_current_sheet_edits(ctx, port)
    if problem:
        return ToolResult(ok=False, error=problem)
    try:
        snapshot = CrmService(ctx.store.session).snapshot_identity(fields)
        if not snapshot.contact_id and new_contact_defaults:
            fields = {
                **fields,
                **{
                    key: value
                    for key, value in new_contact_defaults.items()
                    if value and not fields.get(key)
                },
            }
        proposal = propose_owner_action(
            ctx.store,
            principal=ctx.principal,
            source_ref=ctx.source_ref,
            kind="crm.upsert",
            parameters={
                "fields": fields,
                "contact_id": snapshot.contact_id,
                "expected_revision": snapshot.revision,
            },
            target=asdict(snapshot),
        )
    except (CrmError, PermissionError, ValueError) as exc:
        return ToolResult(ok=False, error=f"CRM proposal could not be bound: {exc}")
    return ToolResult(ok=True, text=success_text, approval_id=proposal.approval_id)


def _crm_record_activity(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    contact_id = str(args.get("contact_id") or "").strip()
    kind = str(args.get("kind") or "activity").strip()
    summary = str(args.get("summary") or "").strip()
    if not contact_id or not summary:
        return ToolResult(ok=False, error="contact_id and summary are required")
    port = ctx.sheets or build_sheets_port(ctx.settings)
    problem = _sync_current_sheet_edits(ctx, port)
    if problem:
        return ToolResult(ok=False, error=problem)
    try:
        snapshot = CrmService(ctx.store.session).snapshot_target(contact_id)
        proposal = propose_owner_action(
            ctx.store,
            principal=ctx.principal,
            source_ref=ctx.source_ref,
            kind="crm.activity",
            parameters={"contact_id": contact_id, "kind": kind, "summary": summary},
            target=asdict(snapshot),
        )
    except (CrmError, PermissionError, ValueError) as exc:
        return ToolResult(ok=False, error=f"CRM activity proposal could not be bound: {exc}")
    return ToolResult(
        ok=True,
        text="Prepared an exact CRM activity proposal. Nothing was written.",
        approval_id=proposal.approval_id,
    )


def _crm_conflicts(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    port = ctx.sheets or build_sheets_port(ctx.settings)
    if _crm_workspace_missing(ctx, port):
        return ToolResult(ok=False, error="the CRM sheet is not set up")
    problem = _sync_current_sheet_edits(ctx, port)
    if problem:
        return ToolResult(ok=False, error=problem)
    contact_id = str(args.get("contact_id") or "").strip() or None
    conflicts = CrmService(ctx.store.session).list_conflicts(contact_id=contact_id)
    if not conflicts:
        return ToolResult(ok=True, text="No unresolved CRM conflicts.")
    return ToolResult(
        ok=True,
        text="Unresolved CRM conflicts:\n"
        + "\n".join(
            f"- {item.id} contact={item.contact_id} field={item.field_name or '-'} "
            f"database={item.database_value!r} sheet={item.sheet_value!r}"
            for item in conflicts
        ),
    )


def _crm_resolve_conflict(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    conflict_id = str(args.get("conflict_id") or "").strip()
    resolution = str(args.get("resolution") or "").strip().lower()
    value = args.get("value")
    if not conflict_id or resolution not in {"database", "sheet", "value"}:
        return ToolResult(ok=False, error="conflict_id and a valid resolution are required")
    if resolution == "value" and not isinstance(value, str):
        return ToolResult(ok=False, error="value resolution requires an explicit value")
    port = ctx.sheets or build_sheets_port(ctx.settings)
    problem = _sync_current_sheet_edits(ctx, port)
    if problem:
        return ToolResult(ok=False, error=problem)
    service = CrmService(ctx.store.session)
    conflict = next(
        (item for item in service.list_conflicts() if item.id == conflict_id), None
    )
    if conflict is None:
        return ToolResult(ok=False, error="the unresolved CRM conflict was not found")
    try:
        contact = service.snapshot_target(conflict.contact_id)
        proposal = propose_owner_action(
            ctx.store,
            principal=ctx.principal,
            source_ref=ctx.source_ref,
            kind="crm.resolve_conflict",
            parameters={
                "conflict_id": conflict.id,
                "resolution": resolution,
                "value": value if isinstance(value, str) else None,
                "contact_id": contact.contact_id,
                "expected_revision": contact.revision,
            },
            target={"conflict": asdict(conflict), "contact": asdict(contact)},
        )
    except (CrmError, PermissionError, ValueError) as exc:
        return ToolResult(ok=False, error=f"CRM resolution proposal could not be bound: {exc}")
    return ToolResult(
        ok=True,
        text="Prepared an exact CRM conflict-resolution proposal. Nothing was changed.",
        approval_id=proposal.approval_id,
    )


def _sync_current_sheet_edits(ctx: ToolContext, port: object) -> str:
    """Import owner-edited Contacts before every v2 CRM read/proposal snapshot."""
    if not all(
        callable(getattr(port, name, None))
        for name in ("ensure_crm_workspace", "read_crm_contacts_chunk")
    ):
        return "CRM sheet import is unavailable; refusing to use a stale target"
    try:
        sync_owner_crm_sheet_in_session(ctx.store, sheets=port)
    except (AdapterHttpError, OSError, RuntimeError, TypeError, ValueError):
        return "CRM sheet import failed; refusing to use a stale target"
    return ""
