import json
from pathlib import Path

from app.core.config import AutomationMode
from app.db.models import ShadowDecisionRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.sales import NextAction
from app.domain.shadow import persist_shadow_decision, should_skip_prospect_send

PROSPECT_SHADOW_PHONE = "972509994880"
PROSPECT_AUTO_PHONE = "972509994881"
OWNER_SHADOW_PHONE = "972509994882"
VISITOR_TEXT = "hi"


def _shadow_row_dump(row: ShadowDecisionRow) -> str:
    return json.dumps(
        {
            "run_id": row.run_id,
            "lead_id": row.lead_id,
            "channel": row.channel,
            "next_action": row.next_action,
            "proposed_reply": row.proposed_reply,
            "policy_version": row.policy_version,
        }
    )


def test_should_skip_prospect_send_matrix() -> None:
    assert should_skip_prospect_send(AutomationMode.SHADOW, "prospect") is True
    assert should_skip_prospect_send(AutomationMode.SHADOW, "owner") is False
    assert should_skip_prospect_send(AutomationMode.AUTO_APPROVED, "prospect") is False
    assert should_skip_prospect_send(AutomationMode.HYBRID, "prospect") is False








def test_persist_shadow_decision_duplicate_run_id_writes_once() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_shadow_dup")
        db.commit()
        persist_shadow_decision(
            store,
            run_id="run_shadow_dup_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            proposed_reply="first reply",
        )
        persist_shadow_decision(
            store,
            run_id="run_shadow_dup_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.QUALIFY.value,
            proposed_reply="second reply",
        )
        db.commit()
        row = store.get_shadow_decision("run_shadow_dup_1")
        assert row is not None
        assert row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
        assert row.proposed_reply == "first reply"
    finally:
        db.close()


def test_shadow_module_has_no_forbidden_imports() -> None:
    source = Path("app/domain/shadow.py").read_text(encoding="utf-8")
    assert "MessagePort" not in source
    assert "app.graph" not in source
    assert "select_next_action" not in source


def test_should_skip_prospect_send_ignores_demo_mode(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    assert should_skip_prospect_send(AutomationMode.SHADOW, "prospect") is True
    assert should_skip_prospect_send(AutomationMode.AUTO_APPROVED, "prospect") is False
