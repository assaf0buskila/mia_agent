"""VNext capability names, sensitivity, and graph allowlists."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.risk import RiskLevel


class GraphName(StrEnum):
    OWNER = "owner"
    CLIENT = "client"


class Sensitivity(StrEnum):
    READ = "read"
    WRITE = "write"
    SENSITIVE_WRITE = "sensitive_write"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class Principal:
    """Who is asking, derived from the request -- never chosen by a helper.

    The permission boundary used to be a `graph=GraphName.OWNER` literal written at
    each call site, so trust was whatever the callee happened to type. Anything new
    that called an owner helper from a web-triggered path inherited owner trust
    silently, and no test could see it.

    A Principal is minted at a channel entry point only: `owner()` after Telegram's
    numeric-allowlist check, `client()` for website and prospect traffic. Everything
    downstream passes the object along and cannot widen it.
    """

    graph: GraphName
    source: str
    actor_id: str = ""

    @classmethod
    def owner(cls, *, source: str, actor_id: str = "") -> Principal:
        """Only after the caller has proven owner identity (numeric allowlist)."""
        return cls(graph=GraphName.OWNER, source=source, actor_id=actor_id)

    @classmethod
    def client(cls, *, source: str, actor_id: str = "") -> Principal:
        """Website visitors and prospect channels. Never owner capabilities."""
        return cls(graph=GraphName.CLIENT, source=source, actor_id=actor_id)


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    sensitivity: Sensitivity
    graphs: frozenset[GraphName]
    risk: RiskLevel
    idempotent: bool = True
    confirmation_required: bool = False


_SENSITIVITY_RISK: dict[Sensitivity, RiskLevel] = {
    Sensitivity.READ: RiskLevel.R0_READ,
    Sensitivity.WRITE: RiskLevel.R1_LOW_WRITE,
    Sensitivity.SENSITIVE_WRITE: RiskLevel.R3_COMMERCIAL,
    Sensitivity.DESTRUCTIVE: RiskLevel.R5_DESTRUCTIVE,
}


def spec(
    name: str,
    sensitivity: Sensitivity,
    graphs: frozenset[GraphName],
    *,
    idempotent: bool = True,
    confirmation_required: bool | None = None,
    risk: RiskLevel | None = None,
) -> CapabilitySpec:
    confirm = (
        confirmation_required
        if confirmation_required is not None
        else sensitivity in {Sensitivity.SENSITIVE_WRITE, Sensitivity.DESTRUCTIVE}
    )
    return CapabilitySpec(
        name=name,
        sensitivity=sensitivity,
        graphs=graphs,
        risk=risk or _SENSITIVITY_RISK[sensitivity],
        idempotent=idempotent,
        confirmation_required=confirm,
    )
