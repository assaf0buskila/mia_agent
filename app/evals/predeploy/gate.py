"""Opt-in gate for the real-model predeploy suite.

`app/evals/harness.py` stays the deterministic eval that runs in CI on every commit and
never calls a model. This suite is the opposite trade: it calls the *configured* models
against the real website and owner code paths, so it costs money, needs keys, and must
never run by accident.

The contract this module exists to enforce:

* `gate_status` is a pure function of an environment mapping. It reads no `os.environ`,
  constructs no `Settings`, opens no socket. That is what makes "did we opt in" provable
  from a plain unit test, and what keeps a default `uv run pytest` from making a single
  model call.
* Opting in takes **two** independent things: the explicit `MIA_PREDEPLOY_EVAL` flag and
  real model credentials. A key alone is not consent, and a flag alone is not capability.
* A partially-ready gate is worse than no gate, because a green line that only covered
  half the product reads as full coverage. Both surfaces must be callable or the suite
  reports skipped.

Embeddings are deliberately *not* part of the gate: the suite injects
`FakeEmbeddingPort`, so retrieval is deterministic and what is being measured is the
sales and owner models, not an embedding provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

PREDEPLOY_FLAG = "MIA_PREDEPLOY_EVAL"

# Every env var the gate reads. Mirrors `app/core/config.py`'s `MIA_` prefix so the
# names an operator sets are the same names the deployed process reads.
OPENAI_KEY = "MIA_OPENAI_API_KEY"
GEMINI_KEY = "MIA_GEMINI_API_KEY"
SALES_MODEL = "MIA_SALES_MODEL"
SALES_FALLBACK_MODEL = "MIA_SALES_FALLBACK_MODEL"
SALES_GEMINI_MODEL = "MIA_SALES_GEMINI_MODEL"
OWNER_MODEL = "MIA_OWNER_AGENT_MODEL"
OWNER_FALLBACK_MODEL = "MIA_OWNER_AGENT_FALLBACK_MODEL"
OWNER_GEMINI_MODEL = "MIA_OWNER_AGENT_GEMINI_MODEL"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class GateStatus:
    """Why the suite may or may not run. `reasons` is operator-facing copy."""

    opted_in: bool
    site_ready: bool
    owner_ready: bool
    reasons: tuple[str, ...]

    def enabled(self) -> bool:
        """True only when the flag is set and both surfaces have a callable model."""
        return self.opted_in and self.site_ready and self.owner_ready


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _flag_set(env: Mapping[str, str]) -> bool:
    return _value(env, PREDEPLOY_FLAG).lower() in _TRUTHY


def _site_ready(env: Mapping[str, str]) -> bool:
    """Mirrors `Settings.sales_llm_ready`, on raw env so no `.env` is read."""
    openai_ok = bool(
        _value(env, OPENAI_KEY)
        and (_value(env, SALES_MODEL) or _value(env, SALES_FALLBACK_MODEL))
    )
    gemini_ok = bool(_value(env, GEMINI_KEY) and _value(env, SALES_GEMINI_MODEL))
    return openai_ok or gemini_ok


def _owner_ready(env: Mapping[str, str]) -> bool:
    """Mirrors `Settings.owner_agent_ready`, including the sales-model fallback rungs."""
    chain = (
        _value(env, OWNER_MODEL),
        _value(env, OWNER_FALLBACK_MODEL),
        _value(env, SALES_MODEL),
        _value(env, SALES_FALLBACK_MODEL),
    )
    openai_ok = bool(_value(env, OPENAI_KEY) and any(chain))
    gemini_ok = bool(_value(env, GEMINI_KEY) and _value(env, OWNER_GEMINI_MODEL))
    return openai_ok or gemini_ok


def gate_status(env: Mapping[str, str]) -> GateStatus:
    """Decide whether the real-model suite may run, and say why when it may not."""
    opted_in = _flag_set(env)
    site_ready = _site_ready(env)
    owner_ready = _owner_ready(env)
    reasons: list[str] = []
    if not opted_in:
        reasons.append(
            f"{PREDEPLOY_FLAG} is not set to 1. This suite calls real models and costs "
            "money, so it never runs unless it is asked for explicitly."
        )
    if not site_ready:
        reasons.append(
            f"No callable website sales model. Set {OPENAI_KEY} plus {SALES_MODEL} "
            f"(or {SALES_FALLBACK_MODEL}), or {GEMINI_KEY} plus {SALES_GEMINI_MODEL}."
        )
    if not owner_ready:
        reasons.append(
            f"No callable owner agent model. Set {OPENAI_KEY} plus {OWNER_MODEL} "
            f"(or {OWNER_FALLBACK_MODEL} / {SALES_MODEL}), or {GEMINI_KEY} plus "
            f"{OWNER_GEMINI_MODEL}."
        )
    return GateStatus(
        opted_in=opted_in,
        site_ready=site_ready,
        owner_ready=owner_ready,
        reasons=tuple(reasons),
    )
