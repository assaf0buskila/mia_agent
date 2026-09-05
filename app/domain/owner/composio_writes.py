"""Approval-bound Composio side effects for any ACTIVE owner toolkit."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.approvals import (
    ACTION_COMPOSIO_WRITE,
    DECISION_APPROVED,
    DECISION_PENDING,
    RESOURCE_COMPOSIO_TOOL,
    approval_expires_at,
    is_approval_expired,
)
from app.domain.events import Channel, build_approval_required_event
from app.integrations.composio_catalog import (
    DENIED_COMPOSIO_SLUGS,
    ComposioCatalog,
    risk_for_slug,
    validate_arguments,
)

_NO_COLD_DM_WORDS = frozenset({"MESSAGE", "DM", "INMAIL"})
MAX_COMPOSIO_APPROVAL_PARAMETERS_BYTES = 16 * 1024


def _parameters(slug: str, arguments: dict) -> str:
    return json.dumps({"arguments": arguments, "slug": slug}, sort_keys=True, separators=(",", ":"))


def composio_approval_resource_id(slug: str, arguments: dict) -> str:
    """Deterministic resource bound to one exact Composio tool invocation."""
    parameters = _parameters(slug, arguments)
    return "cp_" + hashlib.sha256(parameters.encode()).hexdigest()[:40]


def composio_parameters_within_bound(parameters: str) -> bool:
    return bool(parameters) and (
        len(parameters.encode("utf-8")) <= MAX_COMPOSIO_APPROVAL_PARAMETERS_BYTES
    )


def _digest(*, channel: str, resource_id: str, risk: str, parameters: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "action": ACTION_COMPOSIO_WRITE,
                "channel": channel,
                "parameters": parameters,
                "resource_id": resource_id,
                "risk": risk,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _risk_level(value: str) -> RiskLevel:
    try:
        return RiskLevel(value)
    except ValueError:
        return RiskLevel.R3_COMMERCIAL


def propose_composio_write(
    *,
    store,
    channel: Channel,
    catalog: ComposioCatalog,
    slug: str,
    arguments: dict,
    kill_switch: bool,
) -> str:
    if kill_switch:
        return "Composio action denied by kill switch."
    tool = catalog.detail(slug)
    if tool is None:
        return "That Composio tool is not active for this owner."
    risk = risk_for_slug(tool.slug, tool.toolkit)
    if risk is RiskLevel.R0_READ:
        return "This is a read; use composio_execute_tool."
    if tool.toolkit == "LINKEDIN" and frozenset(tool.slug.split("_")) & _NO_COLD_DM_WORDS:
        return "LinkedIn direct messages are not available; cold outreach remains denied."
    problem = validate_arguments(tool.input_schema, arguments)
    if problem:
        return problem
    parameters = _parameters(tool.slug, arguments)
    if not composio_parameters_within_bound(parameters):
        return "The exact Composio action is too large to bind safely for approval."
    resource_id = composio_approval_resource_id(tool.slug, arguments)
    risk_value = risk.value
    store.upsert_composio_approval(
        channel=channel.value,
        action=ACTION_COMPOSIO_WRITE,
        risk=risk_value,
        payload_hash=_digest(
            channel=channel.value, resource_id=resource_id, risk=risk_value, parameters=parameters
        ),
        decision=DECISION_PENDING,
        resource_id=resource_id,
        expires_at=approval_expires_at(now=datetime.now(UTC)),
        proposed_parameters=parameters,
    )
    row = store.get_approval_by_resource(
        RESOURCE_COMPOSIO_TOOL, resource_id, ACTION_COMPOSIO_WRITE
    )
    if row is None:
        return "I could not record the Composio approval. Nothing was executed."
    key = f"{resource_id}:approval"
    if store.claim_operation(scope="approval", key=key):
        store.save_canonical_event(
            provider=channel.value,
            event=build_approval_required_event(
                provider=channel.value,
                channel=channel,
                action=ACTION_COMPOSIO_WRITE,
                risk=risk_value,
                resource_id=resource_id,
            ),
        )
        store.complete_operation(scope="approval", key=key, result_json='{"ok":true}')
    destructive = tool.slug in DENIED_COMPOSIO_SLUGS or risk is RiskLevel.R5_DESTRUCTIVE
    prefix = "Composio destructive action" if destructive else "Composio action"
    return f"{prefix} is ready for your exact approval: {tool.slug}. Nothing was executed."


def composio_row_valid(row) -> tuple[str, dict] | None:
    try:
        data = json.loads(row.proposed_parameters)
        slug, arguments = data["slug"], data["arguments"]
        if (
            not isinstance(slug, str)
            or not isinstance(arguments, dict)
            or not composio_parameters_within_bound(row.proposed_parameters)
        ):
            return None
        if row.payload_hash != _digest(
            channel=row.channel,
            resource_id=row.resource_id,
            risk=row.risk,
            parameters=_parameters(slug, arguments),
        ):
            return None
        return slug, arguments
    except (KeyError, TypeError, ValueError):
        return None


def execute_approved_composio_write(*, store, settings, resource_id: str, kill_switch: bool) -> str:
    row = store.get_approval_by_resource(
        RESOURCE_COMPOSIO_TOOL, resource_id, ACTION_COMPOSIO_WRITE
    )
    if kill_switch or row is None or row.decision != DECISION_APPROVED:
        return "Composio action was not executed."
    bound = composio_row_valid(row)
    if bound is None or is_approval_expired(row, now=datetime.now(UTC)):
        return "Composio approval is no longer valid. Nothing was executed."
    slug, arguments = bound
    catalog = ComposioCatalog.from_settings(settings)
    if catalog is None:
        return "Composio is not connected. Nothing was executed."
    if row.risk != RiskLevel.R5_DESTRUCTIVE.value:
        try:
            assert_allowed(
                RiskAction(name=ACTION_COMPOSIO_WRITE, risk=_risk_level(row.risk)),
                kill_switch=kill_switch,
            )
        except Exception:
            return "Composio action denied by policy."
    key = f"{resource_id}:execute"
    with catalog:
        tool = catalog.detail(slug)
        if (
            tool is None
            or risk_for_slug(slug, tool.toolkit).value != row.risk
            or validate_arguments(tool.input_schema, arguments)
        ):
            return "Composio action no longer matches its approved tool contract."
        if not store.claim_provider_write(scope="composio_approval", key=key):
            return "Composio outcome is already handled or pending review; it was not sent again."
        try:
            response = catalog.execute(tool, arguments)
        except Exception:
            response = None
    if response is None:
        store.mark_provider_write_pending_review(scope="composio_approval", key=key)
        return "Composio outcome is uncertain and pending review; it was not sent again."
    if not store.complete_provider_write(
        scope="composio_approval", key=key, result_json='{"ok":true}'
    ):
        return "Composio action may have completed and is pending review; it was not sent again."
    return "Composio action completed."
