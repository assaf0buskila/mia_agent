"""AI run audit persistence (§32, §36, §40.2): metadata only, no prompts or replies."""

import math
import re
from time import perf_counter

from app.core.config import AutomationMode
from app.core.errors import PolicyDenied
from app.core.models import model_chain
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.events import Channel
from app.domain.policies.decision import DETERMINISTIC_NBA_CONFIDENCE
from app.domain.policies.execution_policy import POLICY_VERSION
from app.domain.sales import NextAction

GRAPH_VERSION = "sales_v1"
PROMPT_VERSION = "sales_reply_v11"
MODEL_CANNED = "canned"
_MAX_LATENCY_MS = 86_400_000
_MAX_TOKENS = 10_000_000
_PROMPT_VERSION_RE = re.compile(r"^[a-zA-Z0-9._-]{1,32}$")


def elapsed_ms(started: float) -> int:
    raw = int((perf_counter() - started) * 1000)
    return max(0, min(raw, _MAX_LATENCY_MS))


def _clamp_latency_ms(value: int) -> int:
    return max(0, min(int(value), _MAX_LATENCY_MS))


def _clamp_tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    if value < 0:
        return 0
    return min(value, _MAX_TOKENS)


def sales_model_label(
    *,
    sales_model: str,
    openai_api_key: str,
    kill_switch: bool,
    sales_fallback_model: str = "",
    gemini_api_key: str = "",
    sales_gemini_model: str = "",
) -> str:
    """The model the compose path would reach for first, or `canned` if none.

    Mirrors `build_sales_reply_port`: a deployment with only Gemini configured
    still paraphrases, so labelling it `canned` would make the audit trail claim
    no model ran.
    """
    if kill_switch:
        return MODEL_CANNED
    openai_chain = (
        model_chain(sales_model, sales_fallback_model)
        if openai_api_key.strip()
        else ()
    )
    if openai_chain:
        return openai_chain[0][:64]
    gemini_chain = (
        model_chain(sales_gemini_model) if gemini_api_key.strip() else ()
    )
    if gemini_chain:
        return gemini_chain[0][:64]
    return MODEL_CANNED


# The owner loop has no sales action at all -- it answers Assaf, it does not sell to
# him -- so it needs a name of its own to be recordable.
OWNER_REPLY_ACTION = "owner_reply"

# Mirrors `app.surfaces.site_policy.SITE_ACTIONS`. Duplicated rather than imported
# because domain must not depend on a surface; `test_ai_runs_vocabulary.py` fails if
# the two ever drift apart.
WEBSITE_ACTIONS = frozenset(
    {
        "ask_need",
        "ask_contact",
        "handoff",
        "no_price",
        "answer",
        "off_topic",
        "complaint",
        "identity",
        "confirm_contact",
        "voice_fail",
        "tool_status",
        "no_metric",
    }
)


def _valid_next_action(value: str) -> bool:
    """Accept every vocabulary a live surface actually speaks.

    Three surfaces write ai_runs and they do not share one action vocabulary: the
    prospect graph speaks `NextAction`, the website speaks `SITE_ACTIONS` (which
    overlaps NextAction on exactly one value, "handoff"), and the owner loop has no
    sales action. Validating against NextAction alone meant a website row was silently
    dropped for twelve of its thirteen actions and an owner row could not be written at
    all -- which is most of why the table was empty. The `channel` column says which
    vocabulary a row is in, so keeping each surface's real action is lossless; mapping
    them onto NextAction would put wrong values in the funnel instead.
    """
    if value == OWNER_REPLY_ACTION or value in WEBSITE_ACTIONS:
        return True
    try:
        NextAction(value)
    except ValueError:
        return False
    return True


def _valid_channel(value: str) -> bool:
    try:
        Channel(value)
    except ValueError:
        return False
    return True


def sanitize_automation_mode(value: str) -> str:
    if not value:
        return ""
    try:
        return AutomationMode(value).value
    except ValueError:
        return ""


def sanitize_prompt_version(value: str) -> str:
    if not value or len(value) > 32:
        return ""
    if _PROMPT_VERSION_RE.fullmatch(value):
        return value
    return ""


def sanitize_decision_confidence(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        if value not in (0, 1):
            return ""
        num = float(value)
    elif isinstance(value, float):
        num = value
    elif isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            return ""
    else:
        return ""
    if not math.isfinite(num) or num < 0 or num > 1:
        return ""
    if num == 0:
        return "0"
    if num == 1:
        return "1.0"
    formatted = f"{num:.10f}".rstrip("0").rstrip(".")
    return formatted[:16]


def persist_ai_run(
    store,
    *,
    run_id: str,
    lead_id: str | None,
    channel: str,
    next_action: str,
    # Defaulted so the website surface can record a run without naming the kill
    # switch. Site chat is deliberately not gated by it (see docs/ARCHITECTURE.md),
    # and `test_site_path_has_no_leads_studio_gmail_or_social` enforces that the
    # word does not appear anywhere on that path.
    kill_switch: bool = False,
    sales_model: str = "",
    openai_api_key: str,
    sales_fallback_model: str = "",
    gemini_api_key: str = "",
    sales_gemini_model: str = "",
    latency_ms: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    automation_mode: str = "",
    model_label: str = "",
) -> None:
    # `model_label` records the model that actually answered. The owner loop runs its
    # own chain, so deriving a label from the sales models would name the wrong one.
    if not run_id:
        return
    if not _valid_next_action(next_action):
        return
    if not _valid_channel(channel):
        return
    try:
        assert_allowed(
            RiskAction(name="ai_run_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=False,
        )
    except PolicyDenied:
        return
    store.save_ai_run(
        run_id=run_id,
        lead_id=lead_id,
        channel=channel,
        graph_version=GRAPH_VERSION,
        model=model_label.strip()
        or sales_model_label(
            sales_model=sales_model,
            openai_api_key=openai_api_key,
            kill_switch=kill_switch,
            sales_fallback_model=sales_fallback_model,
            gemini_api_key=gemini_api_key,
            sales_gemini_model=sales_gemini_model,
        ),
        tokens_in=_clamp_tokens(tokens_in),
        tokens_out=_clamp_tokens(tokens_out),
        # NOT a measurement. Nothing prices a run yet, so this is always 0 and an
        # aggregate of it is a sum of unknowns, not a bill. Do not report it as
        # spend until something actually computes it per model and token count.
        cost_usd=0,
        next_action=next_action,
        kill_switch=kill_switch,
        policy_version=POLICY_VERSION,
        latency_ms=_clamp_latency_ms(latency_ms),
        automation_mode=sanitize_automation_mode(automation_mode),
        prompt_version=sanitize_prompt_version(PROMPT_VERSION),
        decision_confidence=sanitize_decision_confidence(DETERMINISTIC_NBA_CONFIDENCE),
    )
