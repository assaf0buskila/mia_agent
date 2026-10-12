"""Private numeric-owner tools for shared Assaf OS reads and proposals."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError

from app.services.assaf_os import AssafOsError, AssafOsPort, ProposeRequest
from app.services.owner_actions import propose_owner_action
from app.tools.owner.types import ToolContext, ToolResult


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
    if not isinstance(request_id, str):
        return ToolResult(ok=False, error="invalid_arguments")
    try:
        return ToolResult(
            ok=True,
            text=_json(_port(ctx).receipt(request_id)),
            evidence="assaf_os_signed_receipt",
        )
    except AssafOsError as exc:
        return ToolResult(ok=False, error=exc.code)


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
        normalized = re.sub(r"\D", "", supplied_phone)
        phones = {
            re.sub(r"\D", "", match.group(0))
            for match in re.finditer(
                r"(?<![A-Za-z0-9])\+?[0-9](?:[0-9 ().-]{6,}[0-9])(?![A-Za-z0-9])",
                ctx.owner_text,
            )
        }
        if not normalized or normalized not in phones:
            return False
    return True


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
