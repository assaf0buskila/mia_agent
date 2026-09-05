"""Owner Composio meta-tools.

`_composio_execute_with_catalog` is the Composio write boundary. Its check order is
load-bearing: archive-tab ban, active-toolkit check, schema bound, argument validation,
the never-auto-send / never-auto-publish / bounded-Sheets refusals, and only then the
non-R0 approval proposal. Reordering would turn a hard refusal into a proposal.
"""

from __future__ import annotations

import json
from typing import Any

from app.capabilities.policy import authorize
from app.core.errors import PermissionDenied
from app.core.risk import RiskLevel
from app.domain.approvals import (
    ACTION_COMPOSIO_WRITE,
    ACTION_LINKEDIN_COMPOSIO_WRITE,
    RESOURCE_COMPOSIO_TOOL,
    RESOURCE_LINKEDIN_TOOL,
)
from app.domain.events import Channel
from app.domain.owner.composio_writes import composio_approval_resource_id, propose_composio_write
from app.domain.owner.linkedin_writes import linkedin_approval_resource_id, propose_linkedin_write
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
from app.surfaces.crm import a1_targets_archive_tab, is_archive_tab
from app.tools.owner.types import _NOT_CONNECTED, ToolContext, ToolResult

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
    ctx: ToolContext, catalog: ComposioCatalog, slug: str, values: dict[str, Any]
) -> ToolResult:
    try:
        authorize(
            "composio.propose_write",
            principal=ctx.principal,
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="Composio execution denied")
    text = propose_composio_write(
        store=ctx.store,
        channel=Channel.TELEGRAM,
        catalog=catalog,
        slug=slug,
        arguments=values,
        kill_switch=ctx.kill_switch,
    )
    ready_prefixes = ("Composio action is ready", "Composio destructive action is ready")
    if not any(text.startswith(prefix) for prefix in ready_prefixes):
        return ToolResult(ok=False, text=text, error=text)
    resource_id = composio_approval_resource_id(slug, values)
    row = ctx.store.get_approval_by_resource(
        RESOURCE_COMPOSIO_TOOL, resource_id, ACTION_COMPOSIO_WRITE
    )
    approval_id = str(row.approval_id or "").strip() if row is not None else ""
    if not approval_id:
        return ToolResult(ok=False, error="Composio approval binding was not persisted")
    return ToolResult(ok=True, text=text, approval_id=approval_id)


def _composio_propose_action_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    values = _parse_composio_arguments(args)
    if isinstance(values, ToolResult):
        return values
    catalog = _catalog(ctx)
    if catalog is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    slug = str(args.get("tool_slug") or "").strip().upper()
    with catalog:
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
        text = propose_linkedin_write(
            store=ctx.store,
            channel=Channel.TELEGRAM,
            catalog=catalog,
            slug=slug,
            arguments=values,
            kill_switch=ctx.kill_switch,
        )
    if not text.startswith("LinkedIn action is ready"):
        return ToolResult(ok=False, text=text, error=text)
    resource_id = linkedin_approval_resource_id(slug, values)
    row = ctx.store.get_approval_by_resource(
        RESOURCE_LINKEDIN_TOOL, resource_id, ACTION_LINKEDIN_COMPOSIO_WRITE
    )
    approval_id = str(row.approval_id or "").strip() if row is not None else ""
    if not approval_id:
        return ToolResult(ok=False, error="LinkedIn approval binding was not persisted")
    return ToolResult(ok=True, text=text, approval_id=approval_id)


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
    if tool.slug in NEVER_AUTO_SEND_SLUGS:
        return ToolResult(
            ok=False,
            error=(
                "this Composio tool sends and is never auto-executed; "
                "owner-requested Gmail send uses the named Telegram draft "
                "and approve path"
            ),
        )
    if tool.slug in NEVER_AUTO_PUBLISH_SLUGS:
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
    return ToolResult(ok=True, text=bounded_result_text(response))
