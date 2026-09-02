"""Owner-authorized Sheets values operations behind named capabilities."""

from __future__ import annotations

from typing import Any

from app.core.errors import InvalidArguments
from app.integrations.sheets import SheetsPort, validate_owner_sheet_request
from app.surfaces.crm import DEFAULT_CONTACTS_READ_RANGE, LOCKED_SPREADSHEET_ID, prefer_crm_tabs


def _args(
    args: dict[str, Any], *, include_values: bool, allowed_spreadsheet_ids: frozenset[str]
) -> tuple[str, str, list[list[str]]]:
    spreadsheet_id = str(args.get("spreadsheet_id") or "").strip() or LOCKED_SPREADSHEET_ID
    a1_range = str(args.get("range") or "").strip()
    if not a1_range and not include_values:
        a1_range = DEFAULT_CONTACTS_READ_RANGE
    if not a1_range:
        raise InvalidArguments("range is required for Sheets writes")
    values = args.get("values") if include_values else []
    if include_values and not isinstance(values, list):
        raise InvalidArguments("values must be a two-dimensional list")
    try:
        return validate_owner_sheet_request(
            spreadsheet_id=spreadsheet_id,
            a1_range=a1_range,
            values=values if include_values else None,
            allowed_spreadsheet_ids=allowed_spreadsheet_ids,
        )
    except ValueError as exc:
        raise InvalidArguments(str(exc)) from None


def sheets_read(
    port: SheetsPort, args: dict[str, Any], *, allowed_spreadsheet_ids: frozenset[str]
) -> dict[str, Any]:
    spreadsheet_id, a1_range, _ = _args(
        args, include_values=False, allowed_spreadsheet_ids=allowed_spreadsheet_ids
    )
    rows = port.read_values(spreadsheet_id=spreadsheet_id, a1_range=a1_range)
    return {"count": len(rows), "rows": rows}


def sheets_list_tabs(
    port: SheetsPort, args: dict[str, Any], *, allowed_spreadsheet_ids: frozenset[str]
) -> dict[str, Any]:
    reference = str(args.get("spreadsheet_id") or "").strip() or LOCKED_SPREADSHEET_ID
    try:
        spreadsheet_id, _range, _ = validate_owner_sheet_request(
            spreadsheet_id=reference,
            a1_range="A1",
            values=None,
            allowed_spreadsheet_ids=allowed_spreadsheet_ids,
        )
    except ValueError as exc:
        raise InvalidArguments(str(exc)) from None
    names = prefer_crm_tabs(port.list_sheet_names(spreadsheet_id=spreadsheet_id))
    return {"count": len(names), "tabs": names}


def sheets_update(
    port: SheetsPort, args: dict[str, Any], *, allowed_spreadsheet_ids: frozenset[str]
) -> dict[str, Any]:
    spreadsheet_id, a1_range, values = validate_sheets_write_args(
        args, allowed_spreadsheet_ids=allowed_spreadsheet_ids
    )
    port.update_values(spreadsheet_id=spreadsheet_id, a1_range=a1_range, values=values)
    return {"updated": len(values)}


def sheets_append(
    port: SheetsPort, args: dict[str, Any], *, allowed_spreadsheet_ids: frozenset[str]
) -> dict[str, Any]:
    spreadsheet_id, a1_range, values = validate_sheets_write_args(
        args, allowed_spreadsheet_ids=allowed_spreadsheet_ids
    )
    port.append_values(spreadsheet_id=spreadsheet_id, a1_range=a1_range, values=values)
    return {"appended": len(values)}


def validate_sheets_write_args(
    args: dict[str, Any], *, allowed_spreadsheet_ids: frozenset[str]
) -> tuple[str, str, list[list[str]]]:
    """Pure owner-write validation shared by the pre-claim boundary and handlers."""
    return _args(
        args, include_values=True, allowed_spreadsheet_ids=allowed_spreadsheet_ids
    )


def sheets_handlers(
    port: SheetsPort, *, allowed_spreadsheet_ids: frozenset[str] = frozenset()
) -> dict[str, Any]:
    return {
        "sheets.read": lambda args: sheets_read(
            port, args, allowed_spreadsheet_ids=allowed_spreadsheet_ids
        ),
        "sheets.list_tabs": lambda args: sheets_list_tabs(
            port, args, allowed_spreadsheet_ids=allowed_spreadsheet_ids
        ),
        "sheets.update": lambda args: sheets_update(
            port, args, allowed_spreadsheet_ids=allowed_spreadsheet_ids
        ),
        "sheets.append": lambda args: sheets_append(
            port, args, allowed_spreadsheet_ids=allowed_spreadsheet_ids
        ),
    }
