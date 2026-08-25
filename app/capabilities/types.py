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
