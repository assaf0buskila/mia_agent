"""Optional live-model synthetic owner-graph evaluation.

Only the configured LLM is live. Tool calls are strictly dispatched to synthetic fixtures;
the local SQLite database exists only in memory and no provider or customer data is used.
"""

import json
from time import monotonic

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.db import models as _models  # noqa: F401
from app.db.base import Base
from app.db.session import make_engine
from app.db.store import LeadStore
from app.domain.memory import ConversationTurn
from app.domain.owner.brain import build_agent_client
from app.graph import owner_agent
from app.tools.owner.types import ToolContext, ToolResult
from sqlalchemy.orm import sessionmaker

FIXTURES = {
    "crm_search": "Maya Cohen, maya.synthetic@example.test, AssafWeb demo lead",
    "linkedin_snapshot": "Neta Ben-Ami, Founder at Synthetic Labs, https://synthetic.example/profile/neta-9K2B",
    "calendar_agenda": "Synthetic Labs discovery, 2026-09-11 10:00 Asia/Jerusalem",
    "gmail_inbox": "Maya Cohen synthetic follow-up, subject: Demo",
    "gmail_search": "Maya Cohen synthetic follow-up, subject: Demo",
    "gmail_read": "Maya Cohen synthetic follow-up, body: synthetic fixture",
}
ALLOWED = frozenset(FIXTURES) | {
    "composio_search_tools",
    "composio_get_tool_schema",
    "composio_execute_tool",
    "search_memory",
    "search_knowledge",
    "find_leads",
}
FRESH_MARKERS = ("maya.synthetic@example.test", "https://synthetic.example/profile/neta-9K2B")


def _context(settings, message):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    context = ToolContext(
        principal=Principal.owner(source="synthetic-live-probe"),
        store=LeadStore(session),
        brain=BrainStore(session),
        settings=settings,
        embedding_port=FakeEmbeddingPort(),
        source_ref="synthetic-live-probe",
        owner_text=message,
    )
    return context, session, engine


def _run(client, settings, message: str) -> dict:
    calls: list[str] = []
    ctx, session, engine = _context(settings, message)

    def dispatch(name, arguments, _ctx, **kwargs):
        if name not in ALLOWED:
            raise AssertionError("unapproved synthetic tool")
        calls.append(name)
        values = {
            "ok": True,
            "text": FIXTURES.get(name, "SYNTHETIC_METADATA_ONLY"),
        }
        if "evidence" in getattr(ToolResult, "__dataclass_fields__", {}):
            values["evidence"] = "linkedin_profile" if name == "linkedin_snapshot" else ""
        return ToolResult(**values)

    original = owner_agent._run_tool_with_timeout
    owner_agent._run_tool_with_timeout = dispatch
    started = monotonic()
    try:
        outcome = owner_agent.run_owner_agent(
            client=client,
            ctx=ctx,
            owner_message=message,
            history=(ConversationTurn(role="owner", text="STALE_HISTORY_PROFILE_OLD"),),
            deadline_at=started + 60.0,
        )
    finally:
        owner_agent._run_tool_with_timeout = original
        session.close()
        engine.dispose()
    text = outcome.text or ""
    return {
        "model": getattr(client, "last_model", "") or getattr(client, "model", ""),
        "completed": outcome.completed,
        "steps": outcome.steps_used,
        "tool_count": len(calls),
        "tools": sorted(set(calls)),
        "latency_ms": int((monotonic() - started) * 1000),
        "fresh_sentinel_used": any(value in text for value in FRESH_MARKERS),
        "crm_value_used": "maya.synthetic@example.test" in text,
        "profile_value_used": "https://synthetic.example/profile/neta-9K2B" in text,
        "stale_sentinel_absent": "STALE_HISTORY_PROFILE_OLD" not in text,
    }


def main() -> int:
    settings = get_settings().model_copy(update={"memory_write_enabled": False})
    client = build_agent_client(settings)
    if not client.enabled():
        print(json.dumps({"mode": "live_model_synthetic", "configured": False}))
        return 1
    report = {"mode": "live_model_synthetic", "configured": True, "cases": {}}
    for name, message in {
        "crm": "Check my CRM contacts and include maya.synthetic@example.test",
        "full_linkedin": "Show my full LinkedIn profile and include the profile URL",
        "multi_source": "Check CRM and my full LinkedIn profile; include the email and profile URL",
    }.items():
        try:
            report["cases"][name] = _run(build_agent_client(settings), settings, message)
        except Exception as exc:
            report["cases"][name] = {"error_type": type(exc).__name__}
    checks = {
        "crm": (
            report["cases"]["crm"].get("completed")
            and "crm_search" in report["cases"]["crm"].get("tools", [])
            and report["cases"]["crm"].get("crm_value_used")
        ),
        "full_linkedin": (
            report["cases"]["full_linkedin"].get("completed")
            and "linkedin_snapshot" in report["cases"]["full_linkedin"].get("tools", [])
            and report["cases"]["full_linkedin"].get("fresh_sentinel_used")
            and report["cases"]["full_linkedin"].get("profile_value_used")
            and report["cases"]["full_linkedin"].get("stale_sentinel_absent")
        ),
        "multi_source": (
            report["cases"]["multi_source"].get("completed")
            and {"crm_search", "linkedin_snapshot"}.issubset(
                report["cases"]["multi_source"].get("tools", [])
            )
            and report["cases"]["multi_source"].get("crm_value_used")
            and report["cases"]["multi_source"].get("profile_value_used")
            and report["cases"]["multi_source"].get("stale_sentinel_absent")
        ),
    }
    report["pass"] = all(checks.values())
    report["checks"] = checks
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
