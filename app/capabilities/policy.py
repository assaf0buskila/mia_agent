"""Python policy in front of every capability. Prompts cannot add tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.capabilities.registry import get_capability
from app.capabilities.types import Principal, Sensitivity
from app.core.errors import (
    ApprovalRequired,
    CapabilityUnavailable,
    PermissionDenied,
    PolicyDenied,
)
from app.core.risk import PolicyDecision, RiskAction, decide

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def authorize(
    name: str,
    *,
    principal: Principal,
    kill_switch: bool = False,
    preapproved: bool = False,
) -> None:
    """Gate one capability for one principal.

    `principal` is derived from the request at a channel entry point. It is passed,
    never chosen here: a callee that could name its own trust level is not a
    permission boundary.
    """
    spec = get_capability(name)
    if spec is None:
        raise CapabilityUnavailable(name)
    if principal.graph not in spec.graphs:
        raise PermissionDenied(name)
    try:
        decision = decide(
            RiskAction(name=name, risk=spec.risk, in_approved_scope=preapproved),
            kill_switch=kill_switch,
        )
    except PolicyDenied as exc:
        raise PermissionDenied(name) from exc
    if decision is PolicyDecision.DENY:
        raise PermissionDenied(name)
    if decision is PolicyDecision.APPROVAL and not preapproved:
        raise ApprovalRequired(name)
    if spec.confirmation_required and not preapproved:
        raise ApprovalRequired(name)


def execute_capability(
    name: str,
    *,
    principal: Principal,
    args: dict[str, Any] | None = None,
    handlers: dict[str, Handler],
    kill_switch: bool = False,
    preapproved: bool = False,
) -> dict[str, Any]:
    authorize(
        name, principal=principal, kill_switch=kill_switch, preapproved=preapproved
    )
    handler = handlers.get(name)
    if handler is None:
        raise CapabilityUnavailable(name)
    return handler(args or {})


def is_safe_read(name: str) -> bool:
    spec = get_capability(name)
    return spec is not None and spec.sensitivity is Sensitivity.READ
