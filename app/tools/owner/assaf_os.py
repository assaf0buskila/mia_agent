"""Private numeric-owner tools for shared Assaf OS reads and proposals."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.domain.owner.assaf_os_follow_up import (
    FOLLOW_UP_MAX_CHARS,
    follow_up_check,
    receipt_check,
    unknown_check,
)
from app.services.assaf_os import REQUEST_PATTERN, AssafOsError, AssafOsPort, ProposeRequest
from app.services.owner_actions import propose_owner_action
from app.services.phone_identity import normalize_new_input_phone
from app.tools.owner.types import OUTCOME_PARTIAL, ToolContext, ToolResult


def assaf_os_enabled(ctx: ToolContext) -> bool:
    return bool(
        ctx.settings.assaf_os_enabled
        and ctx.private_owner_chat
        and ctx.principal.is_owner()
        and ctx.principal.source == "telegram"
        and ctx.principal.actor_id in ctx.settings.telegram_owner_user_id_set()
    )


def _port(ctx: ToolContext) -> AssafOsPort:
    return AssafOsPort(ctx.settings, ctx.principal.actor_id, actor_source="MIA")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assaf_os_snapshot(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    del arguments
    if not assaf_os_enabled(ctx):
        return ToolResult(ok=False, error="assaf os is unavailable")
    try:
        return ToolResult(
            ok=True,
            text=_json(_port(ctx).snapshot()),
            max_chars=16_000,
            evidence="assaf_os_signed_snapshot",
        )
    except AssafOsError as exc:
        return ToolResult(ok=False, error=exc.code)


def _assaf_os_receipt(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not assaf_os_enabled(ctx):
        return ToolResult(ok=False, error="assaf os is unavailable")
    request_id = arguments.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(REQUEST_PATTERN, request_id):
        return ToolResult(ok=False, error="invalid_arguments")
    try:
        check = receipt_check(_port(ctx).receipt(request_id), request_id, now=ctx.now)
        return ToolResult(
            ok=True,
            text=_json(check),
            outcome=OUTCOME_PARTIAL if check["state"] == "unknown" else "",
            evidence="assaf_os_signed_receipt",
        )
    except AssafOsError as exc:
        check = unknown_check("exact_historical_write_only", exc.code)
        check["source"] = {"kind": "assaf_os_receipt_read_failed", "request_id": request_id}
        check["next_action"] = "query_original_receipt_without_replaying_mutation"
        return ToolResult(
            ok=False, error=exc.code,
            text=_json(check),
        )


def _assaf_os_follow_up(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not assaf_os_enabled(ctx):
        return ToolResult(ok=False, error="assaf os is unavailable")
    if arguments:
        return ToolResult(ok=False, error="invalid_arguments")
    try:
        check = follow_up_check(_port(ctx).snapshot())
        rendered = _json(check)
        if len(rendered) > FOLLOW_UP_MAX_CHARS:
            check = unknown_check("shared_sheet_new_leads_and_open_tasks", "output_limit")
            rendered = _json(check)
        return ToolResult(
            ok=True, text=rendered, max_chars=FOLLOW_UP_MAX_CHARS,
            outcome=OUTCOME_PARTIAL if check["state"] == "unknown" else "",
            evidence="assaf_os_signed_snapshot",
        )
    except AssafOsError as exc:
        return ToolResult(
            ok=False, error=exc.code,
            text=_json(unknown_check("shared_sheet_new_leads_and_open_tasks", exc.code)),
        )


def _contact_provenance(ctx: ToolContext, request: ProposeRequest) -> bool:
    if request.entity not in {"leads", "clients"}:
        return True
    supplied_email = request.input.get("email")
    if isinstance(supplied_email, str) and supplied_email:
        emails = {
            match.group(0).casefold()
            for match in re.finditer(
                r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9._%+\-])",
                ctx.owner_text,
            )
        }
        if supplied_email.casefold() not in emails:
            return False
    supplied_phone = request.input.get("phone")
    if isinstance(supplied_phone, str) and supplied_phone:
        if not re.fullmatch(r"\+?[0-9](?:[0-9 ().-]{6,}[0-9])", supplied_phone):
            return False
        normalized = normalize_new_input_phone(supplied_phone)
        phones = {
            normalize_new_input_phone(match.group(0))
            for match in re.finditer(
                r"(?<![A-Za-z0-9])\+?[0-9](?:[0-9 ().-]{6,}[0-9])(?![A-Za-z0-9])",
                ctx.owner_text,
            )
        }
        if not normalized or normalized not in phones:
            return False
    return True


def _owner_correction_provenance(ctx: ToolContext, arguments: Mapping[str, Any]) -> bool:
    name = str(arguments.get("corrected_name") or "").strip()
    if name and name not in ctx.owner_text:
        return False
    phone = str(arguments.get("corrected_phone") or "").strip()
    if phone:
        candidate = normalize_new_input_phone(phone)
        mentioned = {
            normalize_new_input_phone(match.group(0))
            for match in re.finditer(
                r"(?<![A-Za-z0-9])\+?[0-9](?:[0-9 ().-]{6,}[0-9])(?![A-Za-z0-9])",
                ctx.owner_text,
            )
        }
        if not candidate or candidate not in mentioned:
            return False
    email = str(arguments.get("corrected_email") or "").strip().casefold()
    if email:
        mentioned_email = {
            match.group(0).casefold()
            for match in re.finditer(
                r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9._%+\-])",
                ctx.owner_text,
            )
        }
        if email not in mentioned_email:
            return False
    return True


def _assaf_os_lead_candidates(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not assaf_os_enabled(ctx) or ctx.store is None:
        return ToolResult(ok=False, error="assaf os is unavailable")
    from app.services.assaf_os_leads import list_crm_lead_candidates

    try:
        limit = int(arguments.get("limit") or 10)
        return ToolResult(
            ok=True,
            text=_json(list_crm_lead_candidates(ctx.store, limit=limit)),
            evidence="crm_lead_candidates_without_contact_values",
        )
    except (TypeError, ValueError):
        return ToolResult(ok=False, error="invalid_arguments")


def _assaf_os_prepare_crm_lead(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """Prepare one source-bound lead; contact values never come from model arguments."""
    if not assaf_os_enabled(ctx) or ctx.store is None:
        return ToolResult(ok=False, error="assaf os is unavailable")
    if not _owner_correction_provenance(ctx, arguments):
        return ToolResult(
            ok=False,
            error="correction must appear in the current owner message",
        )
    from app.services.assaf_os_leads import prepare_selected_crm_lead

    try:
        proposal = prepare_selected_crm_lead(
            ctx.store,
            settings=ctx.settings,
            principal=ctx.principal,
            contact_id=str(arguments.get("contact_id") or ""),
            conversation_id=str(arguments.get("conversation_id") or ""),
            path=str(arguments.get("path") or ""),
            summary=str(arguments.get("summary") or ""),
            next_step=str(arguments.get("next_step") or ""),
            corrected_name=str(arguments.get("corrected_name") or ""),
            corrected_phone=str(arguments.get("corrected_phone") or ""),
            corrected_email=str(arguments.get("corrected_email") or ""),
        )
        return ToolResult(
            ok=True,
            text=(
                "הוכנה הצעת ליד מדויקת. עדיין לא נכתבה רשומה; רק אישור הכרטיס "
                "בטלגרם יכול לבצע את הכתיבה."
            ),
            approval_id=proposal.approval_id,
            evidence="assaf_os_source_bound_lead_proposal",
        )
    except AssafOsError as exc:
        return ToolResult(ok=False, error=exc.code)
    except (KeyError, TypeError, ValueError, ValidationError):
        return ToolResult(ok=False, error="invalid_arguments")


def _assaf_os_propose(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not assaf_os_enabled(ctx):
        return ToolResult(ok=False, error="assaf os is unavailable")
    try:
        raw_input = json.loads(arguments.get("input_json", ""))
        if not isinstance(raw_input, dict):
            raise ValueError
        request = ProposeRequest(
            request_id=arguments.get("request_id"),
            entity=arguments.get("entity"),
            record_id=arguments.get("record_id"),
            expected_revision=arguments.get("expected_revision"),
            input=raw_input,
        )
        if not _contact_provenance(ctx, request):
            return ToolResult(
                ok=False, error="contact value must come from the current owner message"
            )
        pending = _port(ctx).propose(request)
        proposal_id = str(pending["proposal_id"])
        decision_request_id = str(pending["approval_request_id"])
        # A proposal may contain several full 2,000-character values. Telegram has
        # a smaller single-message limit, so do not create an approval whose exact
        # before/after state cannot be reviewed. The remote proposal remains pending
        # and expires without changing a business row.
        from app.domain.owner.proposal_cards import assaf_os_proposal_is_reviewable

        prospective_envelope = {
            "kind": "assaf_os.approve",
            "parameters": {
                "proposal_id": proposal_id,
                "request_id": decision_request_id,
                "rejection_request_id": str(pending["rejection_request_id"]),
            },
            "target": {"proposal": pending},
        }
        if not assaf_os_proposal_is_reviewable(prospective_envelope):
            return ToolResult(ok=False, error="proposal_unreviewable")
        proposal = propose_owner_action(
            ctx.store,
            principal=ctx.principal,
            source_ref=ctx.source_ref,
            kind="assaf_os.approve",
            parameters={
                "proposal_id": proposal_id,
                "request_id": decision_request_id,
                "rejection_request_id": str(pending["rejection_request_id"]),
            },
            target={
                "proposal": pending,
                "proposal_digest": hashlib.sha256(_json(pending).encode()).hexdigest(),
                "approval_guard_version": pending["approval_guard_version"],
                "workbook_id": ctx.settings.assaf_os_workbook_id,
                "workspace_id": ctx.settings.assaf_os_workspace_id,
                "mode": ctx.settings.assaf_os_mode,
            },
            now=ctx.now,
            ttl_seconds=600,
        )
        return ToolResult(
            ok=True,
            text="הוכנה הצעה מדויקת לעדכון Assaf OS. היא תתבצע רק לאחר אישור הבעלים בטלגרם.",
            approval_id=proposal.approval_id,
            evidence="assaf_os_pending_proposal",
        )
    except (
        AssafOsError,
        ValidationError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        return ToolResult(
            ok=False, error=exc.code if isinstance(exc, AssafOsError) else "invalid_arguments"
        )
