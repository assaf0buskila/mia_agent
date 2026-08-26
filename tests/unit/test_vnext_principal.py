"""The permission boundary is derived from the request, not declared by the callee.

Before this, every `execute_capability` call site passed a `graph=GraphName.OWNER`
literal. Isolation therefore held only because of module topology: the owner registry
happened to be reachable only from the Telegram route. Anything new that called an
owner helper from a web-triggered path would have inherited owner trust silently, and
no test could have seen it.

These tests pin the property itself rather than one instance of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.capabilities.policy import authorize, execute_capability
from app.capabilities.types import GraphName, Principal
from app.core.errors import ApprovalRequired, PermissionDenied

APP = Path(__file__).resolve().parents[2] / "app"

# Trust may only be named where it is defined or where a request first arrives.
_MAY_NAME_A_GRAPH = {
    Path("capabilities/types.py"),  # the Principal constructors themselves
    Path("capabilities/registry.py"),  # the per-capability allowlists
    Path("api/owner.py"),  # owner entry: after the numeric allowlist check
    Path("api/website.py"),  # client entry: website transport
    Path("api/inbound.py"),  # client entry: prospect channels
    Path("agents/client/graph.py"),  # client-only default, never owner
}


def _graph_literals(path: Path) -> list[str]:
    """Find `GraphName.OWNER` / `GraphName.CLIENT` attribute accesses in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "GraphName"
            and node.attr in {"OWNER", "CLIENT"}
        ):
            found.append(f"{path.name}:{node.lineno} GraphName.{node.attr}")
    return found


def test_no_module_names_its_own_trust_level() -> None:
    """A callee that can name its own graph is not a permission boundary.

    If this fails, a new call site is choosing its own trust instead of receiving a
    Principal. Thread the principal from the entry point rather than adding the file
    to the allowlist -- the allowlist is for places where trust is *established*.
    """
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path.relative_to(APP) in _MAY_NAME_A_GRAPH:
            continue
        offenders.extend(_graph_literals(path))
    assert offenders == [], (
        "these modules name a trust level instead of receiving a Principal: "
        + ", ".join(offenders)
    )


def test_a_client_principal_cannot_reach_an_owner_capability() -> None:
    with pytest.raises(PermissionDenied):
        authorize("mail.read", principal=Principal.client(source="website"))
    with pytest.raises(PermissionDenied):
        authorize("leads.get_recent", principal=Principal.client(source="website"))


def test_a_client_principal_cannot_be_widened_by_its_holder() -> None:
    """Principal is frozen: downstream code cannot promote itself to owner."""
    visitor = Principal.client(source="website", actor_id="web_abc")
    assert visitor.graph is GraphName.CLIENT
    with pytest.raises((AttributeError, TypeError)):
        visitor.graph = GraphName.OWNER  # type: ignore[misc]


def test_a_handler_cannot_be_reached_without_passing_authorize() -> None:
    """Handlers are supplied by the caller, so the gate must run before dispatch."""
    called: list[str] = []

    def handler(_args: dict) -> dict:
        called.append("ran")
        return {}

    with pytest.raises(PermissionDenied):
        execute_capability(
            "leads.get_recent",
            principal=Principal.client(source="website"),
            handlers={"leads.get_recent": handler},
        )
    assert called == [], "the handler ran despite the principal being denied"


def test_destructive_capabilities_still_require_approval() -> None:
    """The gate that mail.create_draft deliberately skips must still bite elsewhere."""
    with pytest.raises((ApprovalRequired, PermissionDenied)):
        authorize("mail.delete", principal=Principal.owner(source="test"))


def test_drafting_is_allowed_because_sending_is_gated_separately() -> None:
    """Documents the deliberate choice, so a future reader does not 'fix' it.

    A draft never leaves the building on its own: sending needs an explicit Approve
    and MIA_GMAIL_SEND. Gating the draft would gate the safe half only.
    """
    authorize("mail.create_draft", principal=Principal.owner(source="test"))
