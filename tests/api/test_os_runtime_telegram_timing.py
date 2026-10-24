"""OS-RUNTIME F5.6: Telegram decisions on an Assaf OS card are timed.

Three log lines, each with an operation, a decision, a fixed outcome code and integer
milliseconds. None carries a proposal id, a request id, a name, a phone or any card text:

* ``owner_actions op=sheets_decide`` around the Apps Script decision of a Sheets card;
* ``owner_callback op=assaf_os_execute`` around the whole worker-thread execution;
* ``owner_callback op=assaf_os_visible`` when the final edit has been attempted, from the
  moment the callback handler was entered (so it includes the acknowledgement and the edits).
"""

from __future__ import annotations

import logging
import re

import pytest
from app.api import telegram as telegram_api
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.owner.callbacks import approval_token
from app.services import owner_actions as owner_actions_service
from app.services.assaf_os import AssafOsError
from app.services.owner_actions import (
    OwnerActionExecution,
    OwnerActionNotStarted,
    _decide_sheets_card,
    propose_owner_action,
)

from tests.api.test_telegram_os_pg import (  # noqa: F401  (fixtures and helpers)
    _callback,
    _CallbackPort,
    _card,
    _press,
    factory,
    world,
)
from tests.services.os_v2_28_support import OWNER
from tests.unit.test_telegram_owner_outbound import _CallbackPort as SheetsCallbackPort

_SHEETS_DECIDE = re.compile(
    r"owner_actions op=sheets_decide operation=(approve|reject) "
    r"outcome=([a-z][a-z0-9_]{1,47}|error) ms=\d+"
)
_EXECUTE = re.compile(
    r"owner_callback op=assaf_os_execute decision=(approve|reject) card=(sheets|ledger) "
    r"outcome=([a-z][a-z0-9_]{1,47}|error) ms=\d+"
)
_VISIBLE = re.compile(
    r"owner_callback op=assaf_os_visible decision=(approve|reject) "
    r"outcome=([a-z][a-z0-9_]{1,47}|error|none) edited=(true|false) ms=\d+"
)
SECRETS = ("PR_timing_secret_01", "approve-timing-secret", "reject-timing-secret")


def lines(caplog, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


# ------------------------------------------------------------------------ the Sheets decision


class _Port:
    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.result, self.error = result, error

    def decide(self, operation, proposal_id, request):
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize(
    ("operation", "result", "outcome"),
    [("approve", {"status": "committed"}, "committed"),
     ("reject", {"status": "rejected"}, "rejected"),
     ("approve", {"status": "Weird Status!"}, "error")],
)
def test_a_sheets_decision_logs_its_receipt_status(caplog, operation, result, outcome):
    with caplog.at_level(logging.INFO, logger="mia.owner_actions"):
        returned = _decide_sheets_card(
            lambda: _Port(result=result), operation, SECRETS[0], SECRETS[1]
        )
    assert returned == result
    (line,) = lines(caplog, "owner_actions op=sheets_decide")
    assert _SHEETS_DECIDE.fullmatch(line), line
    assert f"operation={operation} outcome={outcome} " in line
    assert not [secret for secret in SECRETS if secret in caplog.text]


@pytest.mark.parametrize(
    ("error", "outcome"),
    [(AssafOsError("target_changed", 409), "target_changed"),
     (AssafOsError("Not A Code SECRET", 503), "error"),
     (RuntimeError("PRIVATE visitor text"), "error")],
)
def test_a_failed_sheets_decision_logs_a_code_never_the_message(caplog, error, outcome):
    with caplog.at_level(logging.INFO, logger="mia.owner_actions"):
        with pytest.raises(type(error)):
            _decide_sheets_card(lambda: _Port(error=error), "approve", SECRETS[0], SECRETS[1])
    (line,) = lines(caplog, "owner_actions op=sheets_decide")
    assert _SHEETS_DECIDE.fullmatch(line) and f"outcome={outcome} " in line, line
    assert "PRIVATE" not in caplog.text and "SECRET" not in caplog.text


def test_a_fence_refusal_before_dispatch_logs_not_started(caplog):
    with caplog.at_level(logging.INFO, logger="mia.owner_actions"):
        with pytest.raises(OwnerActionNotStarted):
            _decide_sheets_card(
                lambda: _Port(error=AssafOsError("authority_moved", 409)),
                "reject", SECRETS[0], SECRETS[2],
            )
    (line,) = lines(caplog, "owner_actions op=sheets_decide")
    assert _SHEETS_DECIDE.fullmatch(line), line
    assert "operation=reject outcome=not_started " in line


def test_a_port_that_cannot_be_built_is_still_timed(caplog):
    def make_port():
        raise AssafOsError("operation_unavailable", 404)

    with caplog.at_level(logging.INFO, logger="mia.owner_actions"):
        with pytest.raises(AssafOsError):
            _decide_sheets_card(make_port, "approve", SECRETS[0], SECRETS[1])
    (line,) = lines(caplog, "owner_actions op=sheets_decide")
    assert "outcome=operation_unavailable " in line


# ----------------------------------------------------------------- the Sheets-card callback


def _sheets_card(db, *, tag: str):
    return propose_owner_action(
        LeadStore(db),
        principal=Principal.owner(source="telegram", actor_id=OWNER),
        source_ref=f"telegram:timing-{tag}",
        kind="assaf_os.approve",
        parameters={
            "proposal_id": SECRETS[0],
            "request_id": SECRETS[1],
            "rejection_request_id": SECRETS[2],
        },
        target={"approval_guard_version": 1, "proposal": {"status": "pending"}},
    )


def _callback_for(proposal, *, decision: str, query: str) -> dict:
    return {
        "callback_query_id": query,
        "from": OWNER,
        "data": f"{decision}:{approval_token(proposal.approval_id)}",
        "chat_id": OWNER,
        "chat_type": "private",
        "message_id": "88",
    }


class _EditFailsPort(SheetsCallbackPort):
    async def edit_message_text(self, **_kwargs) -> None:
        raise RuntimeError("edit failed")


@pytest.mark.parametrize(
    ("decision", "patched", "outcome"),
    [("ok", "execute_approved_owner_action_with_adapters", "executed"),
     ("no", "reject_assaf_os_owner_action", "rejected")],
)
async def test_a_sheets_card_callback_logs_execute_and_visible(
    monkeypatch, caplog, decision, patched, outcome
):
    init_db()
    db = get_session_factory()()
    try:
        proposal = _sheets_card(db, tag=decision)
        db.commit()
        monkeypatch.setattr(
            owner_actions_service, patched,
            lambda *_a, **_k: OwnerActionExecution(outcome, text="נעשה"),
        )
        port = SheetsCallbackPort()
        with caplog.at_level(logging.INFO, logger="mia.telegram"):
            result = await telegram_api._handle_callback(
                callback=_callback_for(proposal, decision=decision, query="q-timing"),
                port=port, owner_ids={OWNER}, db=db,
                settings=Settings(telegram_owner_user_ids=OWNER),
            )
    finally:
        db.close()
    assert result["processed"] == 1 and result["sent"] is True
    name = "approve" if decision == "ok" else "reject"
    (execute,) = lines(caplog, "owner_callback op=assaf_os_execute")
    (visible,) = lines(caplog, "owner_callback op=assaf_os_visible")
    assert _EXECUTE.fullmatch(execute), execute
    assert execute.startswith(
        f"owner_callback op=assaf_os_execute decision={name} card=sheets outcome={outcome} ms="
    )
    assert _VISIBLE.fullmatch(visible), visible
    assert visible.startswith(
        f"owner_callback op=assaf_os_visible decision={name} outcome={outcome} edited=true ms="
    )
    assert not [secret for secret in SECRETS if secret in caplog.text]
    assert "נעשה" not in caplog.text


async def test_visible_reports_a_failed_final_edit(monkeypatch, caplog):
    init_db()
    db = get_session_factory()()
    try:
        proposal = _sheets_card(db, tag="edit")
        db.commit()
        monkeypatch.setattr(
            owner_actions_service, "execute_approved_owner_action_with_adapters",
            lambda *_a, **_k: OwnerActionExecution("unknown"),
        )
        with caplog.at_level(logging.INFO, logger="mia.telegram"):
            result = await telegram_api._handle_callback(
                callback=_callback_for(proposal, decision="ok", query="q-edit"),
                port=_EditFailsPort(), owner_ids={OWNER}, db=db,
                settings=Settings(telegram_owner_user_ids=OWNER),
            )
    finally:
        db.close()
    assert result["sent"] is False
    (visible,) = lines(caplog, "owner_callback op=assaf_os_visible")
    assert _VISIBLE.fullmatch(visible) and "outcome=unknown edited=false" in visible, visible


async def test_a_non_assaf_os_callback_logs_neither_line(caplog):
    from datetime import UTC, datetime

    from app.domain.approvals import (
        ACTION_PROPOSAL_HANDOFF,
        DECISION_PENDING,
        RESOURCE_LEAD,
        RISK_R3,
        approval_expires_at,
        payload_hash,
    )
    from app.domain.events import Channel

    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_apr_timing_1"
        )
        store.upsert_approval(
            lead_id=lead_id, channel=Channel.WEBSITE.value, action=ACTION_PROPOSAL_HANDOFF,
            risk=RISK_R3,
            payload_hash=payload_hash(
                action=ACTION_PROPOSAL_HANDOFF, risk=RISK_R3, channel=Channel.WEBSITE.value,
                resource_type=RESOURCE_LEAD, resource_id=lead_id,
            ),
            decision=DECISION_PENDING, resource_type=RESOURCE_LEAD, resource_id=lead_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
        )
        db.commit()
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        with caplog.at_level(logging.INFO, logger="mia.telegram"):
            result = await telegram_api._handle_callback(
                callback={
                    "callback_query_id": "q-other", "from": OWNER,
                    "data": f"ok:{approval_token(row.approval_id)}", "chat_id": OWNER,
                    "chat_type": "private", "message_id": "90",
                },
                port=SheetsCallbackPort(), owner_ids={OWNER}, db=db,
                settings=Settings(telegram_owner_user_ids=OWNER),
            )
    finally:
        db.close()
    assert result["processed"] == 1
    assert lines(caplog, "owner_callback op=assaf_os_") == []


# --------------------------------------------------------------------- a ledger card callback


async def test_a_ledger_card_callback_logs_card_ledger_and_no_id_or_name(
    factory, world, caplog  # noqa: F811
):
    row, proposal = _card(factory, world)
    port = _CallbackPort()
    with caplog.at_level(logging.INFO):
        await _press(factory, world, _callback(proposal.approval_id), port)
    (execute,) = lines(caplog, "owner_callback op=assaf_os_execute")
    (visible,) = lines(caplog, "owner_callback op=assaf_os_visible")
    assert _EXECUTE.fullmatch(execute) and "card=ledger" in execute, execute
    assert _VISIBLE.fullmatch(visible) and "edited=true" in visible, visible
    forbidden = (
        row.proposal_id, row.approval_id, row.rejection_id, proposal.approval_id,
        "דנה", "+972502222222", "visitor@example.com",
    )
    assert not [value for value in forbidden if value and value in caplog.text]


async def test_a_crashed_ledger_decision_logs_unknown(factory, world, caplog):  # noqa: F811
    row, proposal = _card(factory, world)

    def crash() -> None:
        raise RuntimeError("worker died before the ledger commit")

    world.ledger.before_decide = crash
    with caplog.at_level(logging.INFO):
        await _press(factory, world, _callback(proposal.approval_id), _CallbackPort())
    (execute,) = lines(caplog, "owner_callback op=assaf_os_execute")
    assert _EXECUTE.fullmatch(execute) and "card=ledger outcome=unknown" in execute, execute
    assert "worker died" not in caplog.text and row.proposal_id not in caplog.text
