"""Owner Composio meta-tools.

`_composio_execute_with_catalog` is the Composio write boundary. Its check order is
load-bearing: archive-tab ban, active-toolkit check, schema bound, argument validation,
the never-auto-send / Instagram-publish / bounded-Sheets refusals, and only then the
non-R0 approval proposal. LinkedIn publishing routes to its named approval workflow.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from app.capabilities.policy import authorize
from app.core.errors import PermissionDenied
from app.core.risk import RiskLevel
from app.domain.owner.composio_effects import (
    EffectRoute,
    composio_effect,
    snapshot_effect_target,
)
from app.domain.owner.composio_writes import (
    _generic_write_denial,
    generic_composio_effect_supported,
)
from app.integrations.composio_catalog import (
    NEVER_AUTO_PUBLISH_SLUGS,
    NEVER_AUTO_SEND_SLUGS,
    SHEETS_BOUNDED_WRITE_SLUGS,
    ComposioCatalog,
    bounded_result_text,
    risk_for_slug,
    schema_text,
    validate_arguments,
)
from app.services.owner_actions import propose_owner_action
from app.surfaces.crm import a1_targets_archive_tab, is_archive_tab
from app.tools.owner.types import _NOT_CONNECTED, ToolContext, ToolResult

_LINKEDIN_MESSAGE_WORDS = frozenset({"MESSAGE", "DM", "INMAIL"})

# ---------------------------------------------------------- Composio meta-tools


def _catalog(ctx: ToolContext) -> ComposioCatalog | None:
    return ComposioCatalog.from_settings(ctx.settings)


def _composio_search_tools(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        authorize(
            "composio.catalog_search",
            principal=ctx.principal,
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="Composio catalog access denied")
    catalog = _catalog(ctx)
    if catalog is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    query = str(args.get("query") or "").strip()
    toolkit = str(args.get("toolkit") or "").strip()
    raw_limit = args.get("limit")
    if raw_limit is None or raw_limit == "":
        search_limit = 25
    else:
        try:
            search_limit = int(raw_limit)
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="limit must be an integer")
        search_limit = max(1, min(search_limit, 50))
    with catalog:
        tools = catalog.search(query, toolkit, limit=search_limit)
    if not tools:
        return ToolResult(ok=True, text="No matching tool in an ACTIVE owner Composio toolkit.")
    lines = [f"- {tool.slug} ({tool.toolkit}): {tool.description[:320]}" for tool in tools]
    return ToolResult(ok=True, text="\n".join(lines))


def _composio_get_tool_schema(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        authorize(
            "composio.tool_schema",
            principal=ctx.principal,
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="Composio schema access denied")
    catalog = _catalog(ctx)
    if catalog is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    slug = str(args.get("tool_slug") or "").strip().upper()
    with catalog:
        tool = catalog.detail(slug)
    if tool is None:
        return ToolResult(ok=True, text="That tool is not in an ACTIVE owner Composio toolkit.")
    rendered_schema = schema_text(tool)
    if rendered_schema is None:
        return ToolResult(
            ok=False,
            error="tool schema exceeds Mia's safe bound and cannot be executed generically",
        )
    return ToolResult(
        ok=True,
        text=(f"{tool.slug} ({tool.toolkit}) input schema:\n{rendered_schema}"),
        # Schema is loaded only after an intentional meta-tool call, never attached to
        # every model prompt.  Keep it bounded even when a provider has a pathological schema.
        max_chars=12_500,
    )


def _composio_execute_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    # Policy is the first boundary: a killed or non-owner request must not even discover
    # whether a slug exists, much less make a provider catalog call.
    try:
        authorize(
            "composio.execute_read",
            principal=ctx.principal,
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="Composio execution denied")
    values = _parse_composio_arguments(args)
    if isinstance(values, ToolResult):
        return values
    catalog = _catalog(ctx)
    if catalog is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    with catalog:
        return _composio_execute_with_catalog(ctx, catalog, args, values)


def _composio_propose_side_effect(
    ctx: ToolContext,
    catalog: ComposioCatalog,
    slug: str,
    values: dict[str, Any],
    *,
    named_linkedin: bool = False,
) -> ToolResult:
    try:
        authorize(
            "composio.propose_write",
            principal=ctx.principal,
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="Composio execution denied")
    slug = slug.strip().upper()
    tool = catalog.detail(slug)
    if tool is None or tool.slug != slug:
        return ToolResult(ok=False, error="tool is not in an ACTIVE owner toolkit")
    risk = risk_for_slug(tool.slug, tool.toolkit)
    effect = composio_effect(tool.slug, tool.toolkit)
    denial = _generic_write_denial(tool.slug, tool.toolkit, risk)
    if named_linkedin and tool.toolkit == "LINKEDIN":
        denial = ""
        if risk is RiskLevel.R5_DESTRUCTIVE:
            denial = "Destructive LinkedIn tools are denied."
        elif frozenset(tool.slug.split("_")) & _LINKEDIN_MESSAGE_WORDS:
            denial = "LinkedIn direct messages are not available."
    problem = validate_arguments(tool.input_schema, values)
    if (
        effect.route is EffectRoute.NAMED_WORKFLOW
        and risk is not RiskLevel.R0_READ
        and risk is not RiskLevel.R5_DESTRUCTIVE
        and not problem
    ):
        return _route_named_effect(ctx, effect.workflow, values)
    if risk is RiskLevel.R0_READ or denial or problem:
        error = denial or problem or "verified reads must use composio_execute_tool"
        return ToolResult(ok=False, error=error)
    if not generic_composio_effect_supported(tool.slug):
        return ToolResult(
            ok=False,
            error=(
                "This mutation is not a supported create-only effect and has no "
                "typed current-resource snapshot reader, so exact approval is unavailable."
            ),
        )
    if effect.identity_argument:
        identity = values.get(effect.identity_argument)
        if not isinstance(identity, str) or not identity.strip():
            return ToolResult(
                ok=False,
                error="The exact target identity is required before approval.",
            )
    connection = catalog.active_connection_snapshot(tool.toolkit)
    if connection is None:
        return ToolResult(
            ok=False,
            error="An exact single active Composio connection could not be bound.",
        )
    target = {
        "slug": tool.slug,
        "toolkit": tool.toolkit,
        "input_schema": tool.input_schema,
        "risk": risk.value,
        "account_hash": sha256(ctx.settings.composio_user_id.encode()).hexdigest(),
        "connection": asdict(connection),
        "effect_route": effect.route.value,
    }
    if effect.route is EffectRoute.SNAPSHOT_WRITE:
        resource = snapshot_effect_target(
            catalog,
            effect,
            values,
            connected_account_id=connection.connected_account_id,
        )
        if resource is None:
            return ToolResult(
                ok=False,
                error="The current target could not be read, so exact approval is unavailable.",
            )
        target["resource"] = resource
    try:
        proposal = propose_owner_action(
            ctx.store,
            principal=ctx.principal,
            source_ref=ctx.source_ref,
            kind="composio.write",
            parameters={
                "slug": tool.slug,
                "toolkit": tool.toolkit,
                "arguments": values,
            },
            target=target,
            risk=risk.value,
        )
    except (PermissionError, TypeError, ValueError) as exc:
        return ToolResult(ok=False, error=f"Composio proposal could not be bound: {exc}")
    return ToolResult(
        ok=True,
        text=f"Composio action is ready for exact approval: {tool.slug}.",
        approval_id=proposal.approval_id,
    )


def _composio_propose_action_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        authorize(
            "composio.propose_write", principal=ctx.principal, kill_switch=ctx.kill_switch
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="Composio execution denied")
    values = _parse_composio_arguments(args)
    if isinstance(values, ToolResult):
        return values
    catalog = _catalog(ctx)
    if catalog is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    slug = str(args.get("tool_slug") or "").strip().upper()
    with catalog:
        tool = catalog.detail(slug)
        if tool is not None and tool.toolkit == "LINKEDIN":
            return _composio_propose_linkedin_tool(ctx, args)
        return _composio_propose_side_effect(ctx, catalog, slug, values)


def _composio_propose_linkedin_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        authorize(
            "composio.propose_linkedin_write",
            principal=ctx.principal,
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="Composio execution denied")
    values = _parse_composio_arguments(args)
    if isinstance(values, ToolResult):
        return values
    catalog = _catalog(ctx)
    if catalog is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    slug = str(args.get("tool_slug") or "").strip().upper()
    with catalog:
        return _composio_propose_side_effect(
            ctx, catalog, slug, values, named_linkedin=True
        )


def _parse_composio_arguments(args: dict[str, Any]) -> dict[str, Any] | ToolResult:
    """Decode the dynamic argument map without making the strict tool schema open-ended."""
    raw_arguments = args.get("arguments_json")
    if not isinstance(raw_arguments, str):
        return ToolResult(ok=False, error="arguments_json must be a JSON object")
    try:
        values = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return ToolResult(ok=False, error="arguments_json must be valid JSON")
    if not isinstance(values, dict):
        return ToolResult(ok=False, error="arguments_json must decode to a JSON object")
    return values


def _composio_sheet_args_banned(values: dict[str, Any]) -> bool:
    for key in ("range", "a1_range", "sheetName", "sheet_name"):
        raw = str(values.get(key) or "")
        if a1_targets_archive_tab(raw) or is_archive_tab(raw):
            return True
    return False


def _route_named_effect(ctx: ToolContext, workflow: str, values: dict[str, Any]) -> ToolResult:
    """Translate documented provider arguments into an existing typed owner workflow."""
    if workflow == "gmail_create_draft":
        if values.get("is_html") is True:
            return ToolResult(ok=False, error="HTML drafts are unavailable in the typed Gmail path")
        from app.tools.owner.gmail import _gmail_create_draft

        return _gmail_create_draft(
            ctx,
            {
                "to": values.get("recipient_email"),
                "subject": values.get("subject"),
                "body": values.get("body"),
            },
        )
    if workflow in {"sheets_update", "sheets_append"}:
        from app.tools.owner.sheets import _sheets_append, _sheets_update

        if not (
            isinstance(values.get("spreadsheetId"), str)
            and isinstance(values.get("range"), str)
            and isinstance(values.get("values"), list)
        ):
            return ToolResult(
                ok=False,
                error="The typed Sheets path requires spreadsheetId, range and values.",
            )
        routed = {
            "spreadsheet_id": values.get("spreadsheetId"),
            "range": values.get("range"),
            "values": values.get("values"),
        }
        handler = _sheets_append if workflow == "sheets_append" else _sheets_update
        return handler(ctx, routed)
    if workflow == "crm_upsert":
        return ToolResult(
            ok=False,
            error=(
                "Upsert Rows requires the typed CRM contact workflow with a current "
                "identity read."
            ),
        )
    if workflow == "calendar_create_meeting":
        from app.tools.owner.calendar import _calendar_create_meeting

        allowed = {
            "calendar_id", "event_duration_hour", "event_duration_minutes", "location",
            "start_datetime", "summary", "timezone",
        }
        if any(
            value not in (None, "", False, [], {})
            for key, value in values.items()
            if key not in allowed
        ):
            return ToolResult(
                ok=False,
                error="Use the typed calendar tool for these event options.",
            )
        if str(values.get("calendar_id") or "primary") != "primary":
            return ToolResult(
                ok=False,
                error="The typed calendar path supports the primary calendar only.",
            )
        minutes = int(values.get("event_duration_minutes") or 0) + 60 * int(
            values.get("event_duration_hour") or 0
        )
        return _calendar_create_meeting(
            ctx,
            {
                "title": values.get("summary"),
                "start": values.get("start_datetime"),
                "minutes": minutes or 30,
                "location": values.get("location"),
            },
        )
    if workflow == "calendar_reschedule":
        from datetime import datetime

        from app.tools.owner.calendar import _calendar_reschedule

        allowed = {
            "calendar_id",
            "end_time",
            "event_id",
            "send_updates",
            "start_time",
            "timezone",
        }
        if any(
            value not in (None, "", False, "none")
            for key, value in values.items()
            if key not in allowed
        ):
            return ToolResult(
                ok=False,
                error="Use the typed calendar tool for these patch fields.",
            )
        if str(values.get("calendar_id") or "primary") != "primary":
            return ToolResult(
                ok=False,
                error="The typed calendar path supports the primary calendar only.",
            )
        try:
            start = datetime.fromisoformat(
                str(values.get("start_time") or "").replace("Z", "+00:00")
            )
            end = datetime.fromisoformat(
                str(values.get("end_time") or "").replace("Z", "+00:00")
            )
            minutes = int((end - start).total_seconds() // 60)
        except ValueError:
            return ToolResult(ok=False, error="A complete new start and end are required.")
        return _calendar_reschedule(
            ctx,
            {"event_id": values.get("event_id"), "start": start.isoformat(), "minutes": minutes},
        )
    return ToolResult(ok=False, error="This effect requires an unavailable typed owner workflow.")


def _composio_execute_with_catalog(
    ctx: ToolContext,
    catalog: ComposioCatalog,
    args: dict[str, Any],
    values: dict[str, Any],
) -> ToolResult:
    slug = str(args.get("tool_slug") or "").strip().upper()
    if slug.startswith("GOOGLESHEETS") and _composio_sheet_args_banned(values):
        return ToolResult(ok=False, error="01 Leads is an archive tab and is banned")
    tool = catalog.detail(slug)
    if tool is None or tool.slug != slug:
        return ToolResult(ok=False, error="tool is not in an ACTIVE owner Composio toolkit")
    if schema_text(tool) is None:
        return ToolResult(
            ok=False,
            error="tool schema exceeds Mia's safe bound and cannot be executed generically",
        )
    problem = validate_arguments(tool.input_schema, values)
    if problem:
        return ToolResult(ok=False, error=problem)
    risk = risk_for_slug(tool.slug, tool.toolkit)
    effect = composio_effect(tool.slug, tool.toolkit)
    if effect.route is EffectRoute.NAMED_WORKFLOW and risk is not RiskLevel.R5_DESTRUCTIVE:
        return _route_named_effect(ctx, effect.workflow, values)
    if tool.slug in NEVER_AUTO_SEND_SLUGS and tool.slug != "GMAIL_SEND_DRAFT":
        return ToolResult(
            ok=False,
            error=(
                "this Composio tool sends and is never auto-executed; "
                "owner-requested Gmail send uses the named Telegram draft "
                "and approve path"
            ),
        )
    if tool.slug in NEVER_AUTO_PUBLISH_SLUGS:
        if tool.toolkit == "LINKEDIN":
            # Publishing stays approval-bound. Route the exact validated action to
            # its existing proposal workflow instead of asking the model to retry.
            return _composio_propose_linkedin_tool(ctx, args)
        return ToolResult(
            ok=False,
            error=(
                "this Composio tool publishes and is never auto-executed; "
                "Instagram is analytics-only; LinkedIn writes use the named "
                "Telegram approval path"
            ),
        )
    if tool.slug in SHEETS_BOUNDED_WRITE_SLUGS:
        return ToolResult(
            ok=False,
            error=(
                "bounded Sheets writes use the named sheets_read / sheets_update / "
                "sheets_append tools with the allowlisted spreadsheet id"
            ),
        )
    if risk is not RiskLevel.R0_READ:
        return _composio_propose_side_effect(ctx, catalog, slug, values)
    response = catalog.execute_read(tool, values)
    if response is None:
        return ToolResult(ok=False, error="Composio execution failed")
    # Results are provider data, never instructions. Oversized results remain valid
    # JSON and retain continuation metadata instead of silently slicing off a cursor.
    evidence = ""
    if tool.toolkit == "LINKEDIN" and tool.slug in {
        "LINKEDIN_GET_MY_INFO",
        "LINKEDIN_GET_MY_PROFILE",
    } and _has_meaningful_linkedin_profile_data(response):
        evidence = "linkedin_profile"
    return ToolResult(ok=True, text=bounded_result_text(response), evidence=evidence)


def _has_meaningful_linkedin_profile_data(value: object) -> bool:
    """Require successful mapped own-profile data, not a provider envelope."""
    if not isinstance(value, dict) or value.get("successful") is not True:
        return False
    raw = value.get("data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return False
    if not isinstance(raw, dict):
        return False
    from app.integrations.linkedin import _map_data_to_profile

    return _map_data_to_profile(raw) is not None
