import logging
from typing import Any

from app.core.redact import redact


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(redact(arg) for arg in record.args)
        extra: dict[str, Any] = getattr(record, "__dict__", {})
        for key in ("email", "phone", "token", "api_key"):
            if key in extra:
                extra[key] = "[redacted]"
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[handler],
        force=True,
    )


_COMM_LOG = logging.getLogger("mia.comm")


def log_comm(
    *,
    channel: str,
    provider: str,
    actor_type: str,
    direction: str,
    external_message_id: str,
    lead_id: str = "",
    conversation_id: str = "",
    automation_scope: str = "",
    takeover_state: str = "",
    approval_id: str = "",
    policy_result: str = "",
    latency_ms: int = 0,
    success: bool = True,
    automation_mode: str = "",
) -> None:
    """Operational comm log. Never include message text, tokens, or secrets."""
    _COMM_LOG.info(
        "comm channel=%s provider=%s actor_type=%s direction=%s msg=%s lead=%s conv=%s "
        "scope=%s takeover=%s approval=%s policy=%s latency_ms=%s success=%s mode=%s",
        channel,
        provider,
        actor_type,
        direction,
        external_message_id,
        lead_id or "-",
        conversation_id or "-",
        automation_scope or "-",
        takeover_state or "-",
        approval_id or "-",
        policy_result or "-",
        latency_ms,
        success,
        automation_mode or "-",
    )


_AGENT_LOG = logging.getLogger("mia.agent")


def log_owner_agent(
    *,
    used_agent: bool,
    model: str = "",
    task_type: str = "",
    tools_used: tuple[str, ...] = (),
    reason: str = "",
    steps: int = 0,
    tools_failed: tuple[str, ...] = (),
    completion: str = "",
) -> None:
    """One line per owner turn: did the agent answer, and if not, why.

    `steps` and `completion` are new (Task 3, ADR-031 follow-up): before this, a run that
    silently burned its whole step budget or spiralled on a broken integration looked
    identical in the logs to a clean one-shot answer. `tools_failed` surfaces a tool that
    returned `ok=False` inside an otherwise-successful turn -- previously invisible, since
    only the successful tool names (`tools_used`) were ever logged.

    Never includes message text, tool arguments, or model output. `reason` carries provider
    error codes only, which is what makes a misconfigured model id diagnosable from the
    logs instead of by inference.
    """
    _AGENT_LOG.info(
        "owner_agent used=%s model=%s task=%s steps=%s tools=%s failed=%s completion=%s "
        "reason=%s",
        used_agent,
        model or "-",
        task_type or "-",
        steps or 0,
        ",".join(tools_used) or "-",
        ",".join(tools_failed) or "-",
        completion or "-",
        reason or "-",
    )
