"""Approval-bound Composio side effects for any ACTIVE owner toolkit."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.approvals import (
    ACTION_COMPOSIO_WRITE,
    DECISION_APPROVED,
    RESOURCE_COMPOSIO_TOOL,
    is_approval_expired,
)
from app.domain.owner.composio_effects import (
    EffectRoute,
    composio_effect,
    is_prohibited_meta_ads_mutation,
)
from app.integrations.composio_catalog import (
    DENIED_COMPOSIO_SLUGS,
    NEVER_AUTO_PUBLISH_SLUGS,
    NEVER_AUTO_SEND_SLUGS,
    SHEETS_BOUNDED_WRITE_SLUGS,
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


def _generic_write_denial(slug: str, toolkit: str, risk: RiskLevel) -> str:
    """Return why a side effect cannot use the generic approval path."""
    action = slug.strip().upper()
    provider = toolkit.strip().upper()
    if is_prohibited_meta_ads_mutation(action, provider):
        return "Meta Ads mutations are prohibited regardless of approval."
    if action in DENIED_COMPOSIO_SLUGS or risk is RiskLevel.R5_DESTRUCTIVE:
        return "Destructive Composio tools are denied."
    if action in NEVER_AUTO_SEND_SLUGS and action != "GMAIL_SEND_DRAFT":
        return "Send tools cannot use generic approval; use the named owner workflow."
    if action in SHEETS_BOUNDED_WRITE_SLUGS or (
        action.startswith("GOOGLESHEETS_") and risk is not RiskLevel.R0_READ
    ):
        return "Sheets writes cannot use generic approval; use the named bounded Sheets tools."
    if action in NEVER_AUTO_PUBLISH_SLUGS or (
        action.startswith("INSTAGRAM_") and risk is not RiskLevel.R0_READ
    ):
        return "Publishing cannot use generic approval. Instagram remains analytics-only."
    if (provider == "LINKEDIN" or action.startswith("LINKEDIN_")) and risk is not RiskLevel.R0_READ:
        return "LinkedIn writes must use the named LinkedIn approval workflow."
    return ""


def generic_composio_effect_supported(slug: str) -> bool:
    """Use the shared finite effect registry for both proposal and execution."""
    return composio_effect(slug).route in {
        EffectRoute.GENERIC_CREATE,
        EffectRoute.SNAPSHOT_WRITE,
    }


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
        if _generic_write_denial(slug, "", _risk_level(row.risk)):
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
        current_risk = risk_for_slug(slug, tool.toolkit) if tool is not None else None
        if (
            tool is None
            or current_risk is None
            or current_risk.value != row.risk
            or bool(_generic_write_denial(slug, tool.toolkit, current_risk))
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
