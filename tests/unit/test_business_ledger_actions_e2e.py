"""Opt-in Slice E proof for the one approved synthetic TEST project update.

The normal test suite skips the live proof. The parent operator may run it only with::

    $env:MIA_ENV='test'
    $env:MIA_BUSINESS_LEDGER_E2E_ENABLED='1'
    $env:MIA_BUSINESS_LEDGER_E2E_CASE='slice-e'
    python -m pytest tests/unit/test_business_ledger_actions_e2e.py -k actual_slice_e -q

The private operator config remains at
``.cache/business-ledger-test/operator-config.json`` and is read only after all
three opt-in gates pass. The harness never reads ``.env``. Its sole provider
mutation is the existing TEST project note update described below.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

APPROVED_WORKBOOK_ID = "1T9Is3iJ_RlI8KndpTXYCfuTkVsMbncw3_0bAVze0RE4"
APPROVED_WORKSPACE_ID = "assafweb-staging"
APPROVED_SCRIPT_ID = "1gvkcnDHB3d21lYipOdg84zm2omvoLbmY7EoiQssMVvUCvPhaG9rJ2VV6"
PROJECT_ID = "d2103f88-37f7-42b4-9326-7fde8030cdc8"
EXPECTED_TOTALS = {
    "deal_value_minor": 44567,
    "scheduled_minor": 44567,
    "received_minor": 1200,
    "outstanding_minor": 43367,
    "expense_minor": 500,
    "net_cash_minor": 700,
}
EXPECTED_COUNTS = {"deals": 3, "charges": 4, "payments": 1, "expenses": 1, "projects": 1}
DESIRED_NOTES = "בדיקת אישור MIA סינתטית בלבד"
EVENT_ID = str(uuid5(NAMESPACE_URL, "assafweb:slice-e-project-note-v1"))
SOURCE_REF = f"slice-e:event:{EVENT_ID}"
CONFIG_KEYS = {
    "mode",
    "spreadsheet_id",
    "script_id",
    "workspace_id",
    "owner_id",
    "hmac_secret",
    "script_url",
}
MARKER_KEYS = {
    "version",
    "phase",
    "event_id",
    "source_ref",
    "request_id",
    "project_id",
    "expected_revision",
    "proposal_id",
    "approval_id",
    "recovery_attempt",
    "receipt_id",
}
PHASES = {"prepared", "proposed", "approved", "inflight", "completed"}
LIVE_STAGES = {
    "configuration",
    "local_store",
    "initial_read",
    "proposal",
    "approval",
    "execution",
    "receipt_recovery",
    "completed_read",
    "cleanup",
}
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _selected() -> bool:
    return (
        os.getenv("MIA_ENV") == "test"
        and os.getenv("MIA_BUSINESS_LEDGER_E2E_ENABLED") == "1"
        and os.getenv("MIA_BUSINESS_LEDGER_E2E_CASE") == "slice-e"
    )


def _reject(reason: str) -> None:
    pytest.fail(f"Slice E TEST harness configuration rejected: {reason}", pytrace=False)


def _live_failure_boundary(progress: dict[str, str], call):
    try:
        return call()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        stage = progress.get("stage")
        safe_stage = stage if stage in LIVE_STAGES else "configuration"
        try:
            pytest.fail(
                f"Slice E TEST failed: stage={safe_stage} code=unexpected",
                pytrace=False,
            )
        except BaseException as redacted:
            raise redacted from None


def _load_operator_config(repo: Path) -> dict[str, str]:
    path = repo / ".cache" / "business-ledger-test" / "operator-config.json"
    if not path.is_file() or path.is_symlink():
        _reject("private operator config is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _reject("private operator config is invalid")
    if not isinstance(value, dict) or set(value) != CONFIG_KEYS:
        _reject("private operator config keys do not match the bounded schema")
    if value.get("mode") != "TEST":
        _reject("operator mode is not TEST")
    if value.get("spreadsheet_id") != APPROVED_WORKBOOK_ID:
        _reject("workbook is not the approved TEST resource")
    if value.get("workspace_id") != APPROVED_WORKSPACE_ID:
        _reject("workspace is not the approved TEST workspace")
    if value.get("script_id") != APPROVED_SCRIPT_ID:
        _reject("bound script is not the approved TEST resource")
    owner = value.get("owner_id")
    secret = value.get("hmac_secret")
    url = value.get("script_url")
    if not isinstance(owner, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", owner):
        _reject("owner identity is not canonical")
    if not isinstance(secret, str) or len(secret) < 32:
        _reject("ledger signing secret is unavailable")
    if not isinstance(url, str) or not re.fullmatch(
        r"https://script\.google\.com/macros/s/[A-Za-z0-9_-]{20,256}/exec", url
    ):
        _reject("Apps Script URL is not an approved TEST endpoint shape")
    return value


def _marker_path(repo: Path) -> Path:
    return repo / ".cache" / "business-ledger-test" / "pending-action-e2e.json"


def _load_marker(path: Path) -> dict | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        _reject("action marker is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _reject("action marker is invalid")
    if not isinstance(value, dict) or set(value) != MARKER_KEYS:
        _reject("action marker shape is invalid")
    if (
        value["version"] != 1
        or value["phase"] not in PHASES
        or value["source_ref"] != SOURCE_REF
        or value["event_id"] != EVENT_ID
        or value["project_id"] != PROJECT_ID
        or value["expected_revision"] != 1
        or not isinstance(value["recovery_attempt"], int)
        or isinstance(value["recovery_attempt"], bool)
        or not 0 <= value["recovery_attempt"] <= 100
        or not isinstance(value["proposal_id"], str)
        or not isinstance(value["approval_id"], str)
        or not isinstance(value["receipt_id"], str)
        or UUID_RE.fullmatch(value["request_id"]) is None
    ):
        _reject("action marker values are invalid")
    if value["phase"] in {"proposed", "approved", "inflight", "completed"} and (
        not value["proposal_id"].startswith("op_") or not value["approval_id"]
    ):
        _reject("action marker proposal binding is missing")
    if value["phase"] == "completed" and UUID_RE.fullmatch(value["receipt_id"]) is None:
        _reject("completed action marker has no receipt")
    return value


def _save_marker(path: Path, marker: dict) -> None:
    if set(marker) != MARKER_KEYS:
        raise AssertionError("refusing to persist an unbounded action marker")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _settings(config):
    from app.core.config import MiaEnv, Settings

    return Settings(
        _env_file=None,
        env=MiaEnv.TEST,
        kill_switch=False,
        owner_os_enabled=True,
        owner_os_actions_enabled=False,
        owner_os_workspace_id=APPROVED_WORKSPACE_ID,
        telegram_owner_user_ids=config["owner_id"],
        business_ledger_test_enabled=True,
        business_ledger_workspace_id=APPROVED_WORKSPACE_ID,
        business_ledger_script_url=config["script_url"],
        business_ledger_hmac_secret=config["hmac_secret"],
        database_url="sqlite:///:memory:",
    )


def _open_store(repo: Path):
    from app.db.base import Base
    from app.db.store import LeadStore
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    path = repo / ".cache" / "business-ledger-test" / "action-e2e.sqlite3"
    if path.exists() and (not path.is_file() or path.is_symlink()):
        _reject("local approval database is unsafe")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    return LeadStore(session), session, engine


def _assert_finance_state(snapshot: dict, marker: dict, *, completed: bool) -> dict:
    assert snapshot["mode"] == "TEST"
    assert snapshot["source"] == "google_sheets"
    assert snapshot["coverage"] == "financial_complete"
    assert snapshot["counts"] == EXPECTED_COUNTS
    assert snapshot["totals"] == EXPECTED_TOTALS
    project = next(row for row in snapshot["projects"] if row["id"] == PROJECT_ID)
    assert project["revision"] == (2 if completed else 1)
    if completed:
        assert project["notes"] == DESIRED_NOTES
    return project


class _ProposalReadOnlyLedger:
    def __init__(self, ledger):
        self.ledger = ledger
        self.write_calls = 0

    def finance_snapshot(self):
        return self.ledger.finance_snapshot()

    def save_project(self, _change):
        self.write_calls += 1
        raise AssertionError("proposal attempted a provider write")


class _LoseAfterProjectCommit:
    def __init__(self, ledger):
        self.ledger = ledger
        self.save_calls = 0

    def finance_snapshot(self):
        return self.ledger.finance_snapshot()

    def save_project(self, change):
        from app.services.business_ledger import LedgerError

        self.save_calls += 1
        self.ledger.save_project(change)
        raise LedgerError("unknown")


def _recover_receipt(
    *, store, session, settings, principal, ledger, marker: dict, marker_path: Path
) -> dict:
    from app.domain.approvals import DECISION_APPROVED
    from app.services.business_ledger_actions import propose_business_action
    from app.services.owner_actions import (
        decide_owner_action,
        execute_approved_owner_action_with_adapters,
    )

    if marker["recovery_attempt"] >= 100:
        _reject("receipt recovery attempt limit reached")
    marker["recovery_attempt"] += 1
    _save_marker(marker_path, marker)
    source_ref = f"slice-e-receipt-recovery-{marker['recovery_attempt']}"
    recovery = propose_business_action(
        store,
        settings=settings,
        principal=principal,
        private_owner_chat=True,
        source_ref=source_ref,
        kind="business.recover_receipt",
        parameters={
            "request_id": marker["request_id"],
            "expected_id": PROJECT_ID,
            "expected_revision": 2,
        },
        ledger=ledger,
    )
    session.commit()
    decision = decide_owner_action(
        store,
        principal=principal,
        approval_id=recovery.approval_id,
        decision=DECISION_APPROVED,
    )
    assert decision.status in {"decided", "already_decided"}
    session.commit()
    outcome = execute_approved_owner_action_with_adapters(
        store,
        settings=settings,
        principal=principal,
        proposal_id=recovery.proposal_id,
    )
    session.commit()
    assert outcome.status == "executed", "receipt is not yet durably recoverable"
    receipt = ledger.receipt(marker["request_id"])
    assert receipt["request_id"] == marker["request_id"]
    assert receipt["id"] == PROJECT_ID
    assert receipt["revision"] == 2
    assert receipt["mode"] == "TEST" and receipt["source"] == "google_sheets"
    UUID(receipt["receipt_id"])
    marker["receipt_id"] = receipt["receipt_id"]
    marker["phase"] = "completed"
    _save_marker(marker_path, marker)
    return receipt


def test_slice_e_is_default_off_and_rejects_before_provider_access(monkeypatch):
    from app.capabilities.types import Principal
    from app.services.business_ledger_actions import propose_business_action

    class Exploding:
        def __getattr__(self, _name):
            raise AssertionError("provider or store touched")

    with pytest.raises(PermissionError):
        propose_business_action(
            Exploding(),
            settings=type(
                "Disabled",
                (),
                {
                    "env": "test",
                    "owner_os_enabled": True,
                    "business_ledger_test_enabled": False,
                    "kill_switch": False,
                    "telegram_owner_user_id_set": lambda self: {"123456"},
                },
            )(),
            principal=Principal.owner(source="telegram", actor_id="123456"),
            private_owner_chat=True,
            source_ref="slice-e-disabled",
            kind="business.save_project",
            parameters={},
            ledger=Exploding(),
        )
    monkeypatch.delenv("MIA_BUSINESS_LEDGER_E2E_ENABLED", raising=False)
    monkeypatch.delenv("MIA_BUSINESS_LEDGER_E2E_CASE", raising=False)
    assert not _selected()


def test_slice_e_marker_roundtrip_contains_only_stable_recovery_metadata(tmp_path):
    request_id = str(uuid5(NAMESPACE_URL, "slice-e-offline-request"))
    marker = {
        "version": 1,
        "phase": "prepared",
        "event_id": EVENT_ID,
        "source_ref": SOURCE_REF,
        "request_id": request_id,
        "project_id": PROJECT_ID,
        "expected_revision": 1,
        "proposal_id": "",
        "approval_id": "",
        "recovery_attempt": 0,
        "receipt_id": "",
    }
    path = tmp_path / "pending-action-e2e.json"
    _save_marker(path, marker)
    assert _load_marker(path) == marker
    serialized = path.read_text(encoding="utf-8")
    assert set(json.loads(serialized)) == MARKER_KEYS
    assert "owner_id" not in serialized
    assert "hmac" not in serialized
    assert "title" not in serialized
    assert "notes" not in serialized


def _run_actual_slice_e(monkeypatch, progress: dict[str, str]) -> None:
    progress["stage"] = "configuration"
    repo = Path(__file__).resolve().parents[2]
    config = _load_operator_config(repo)
    settings = _settings(config)
    marker_path = _marker_path(repo)

    from app.capabilities.types import Principal
    from app.domain.approvals import DECISION_APPROVED
    from app.services import business_ledger_actions
    from app.services.business_ledger import BusinessLedger
    from app.services.business_ledger_actions import (
        propose_business_action,
        stable_request_id,
    )
    from app.services.owner_actions import (
        decide_owner_action,
        execute_approved_owner_action_with_adapters,
    )

    principal = Principal.owner(source="telegram", actor_id=config["owner_id"])
    ledger = BusinessLedger(settings, principal.actor_id)
    store = session = engine = None
    try:
        progress["stage"] = "local_store"
        store, session, engine = _open_store(repo)
        marker = _load_marker(marker_path)
        if marker is None:
            source_ref = SOURCE_REF
            marker = {
                "version": 1,
                "phase": "prepared",
                "event_id": EVENT_ID,
                "source_ref": source_ref,
                "request_id": stable_request_id(principal, source_ref, "save_project"),
                "project_id": PROJECT_ID,
                "expected_revision": 1,
                "proposal_id": "",
                "approval_id": "",
                "recovery_attempt": 0,
                "receipt_id": "",
            }
            _save_marker(marker_path, marker)

        if marker["phase"] == "completed":
            progress["stage"] = "completed_read"
            receipt = ledger.receipt(marker["request_id"])
            assert receipt["receipt_id"] == marker["receipt_id"]
            _assert_finance_state(ledger.finance_snapshot(), marker, completed=True)
            return

        if marker["phase"] == "inflight":
            progress["stage"] = "receipt_recovery"
            _recover_receipt(
                store=store,
                session=session,
                settings=settings,
                principal=principal,
                ledger=ledger,
                marker=marker,
                marker_path=marker_path,
            )
            _assert_finance_state(ledger.finance_snapshot(), marker, completed=True)
            return

        progress["stage"] = "initial_read"
        before = ledger.finance_snapshot()
        project = _assert_finance_state(before, marker, completed=False)
        parameters = {
            "request_id": marker["request_id"],
            "expected_revision": 1,
            "project": {
                "id": PROJECT_ID,
                "title": project["title"],
                "status": project["status"],
                "deadline": project["deadline"],
                "notes": DESIRED_NOTES,
            },
        }
        progress["stage"] = "proposal"
        proposal_ledger = _ProposalReadOnlyLedger(ledger)
        proposal = propose_business_action(
            store,
            settings=settings,
            principal=principal,
            private_owner_chat=True,
            source_ref=marker["source_ref"],
            kind="business.save_project",
            parameters=parameters,
            ledger=proposal_ledger,
        )
        session.commit()
        assert proposal_ledger.write_calls == 0
        assert ledger.finance_snapshot()["projects"] == before["projects"]
        marker.update(
            phase="proposed",
            proposal_id=proposal.proposal_id,
            approval_id=proposal.approval_id,
        )
        _save_marker(marker_path, marker)

        progress["stage"] = "approval"
        decision = decide_owner_action(
            store,
            principal=principal,
            approval_id=marker["approval_id"],
            decision=DECISION_APPROVED,
        )
        assert decision.status in {"decided", "already_decided"}
        session.commit()
        marker["phase"] = "approved"
        _save_marker(marker_path, marker)

        loss = _LoseAfterProjectCommit(ledger)
        original_factory = business_ledger_actions.make_ledger
        monkeypatch.setattr(business_ledger_actions, "make_ledger", lambda *_args: loss)
        from app.integrations.composio_catalog import ComposioCatalog

        monkeypatch.setattr(
            ComposioCatalog,
            "from_settings",
            classmethod(
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("business action reached unrelated Composio construction")
                )
            ),
        )
        marker["phase"] = "inflight"
        _save_marker(marker_path, marker)
        progress["stage"] = "execution"
        outcome = execute_approved_owner_action_with_adapters(
            store,
            settings=settings,
            principal=principal,
            proposal_id=marker["proposal_id"],
        )
        session.commit()
        assert outcome.status == "unknown"
        assert loss.save_calls == 1
        repeated = execute_approved_owner_action_with_adapters(
            store,
            settings=settings,
            principal=principal,
            proposal_id=marker["proposal_id"],
        )
        session.commit()
        assert repeated.status in {"already_handled", "target_changed"}
        assert loss.save_calls == 1
        monkeypatch.setattr(business_ledger_actions, "make_ledger", original_factory)

        progress["stage"] = "receipt_recovery"
        _recover_receipt(
            store=store,
            session=session,
            settings=settings,
            principal=principal,
            ledger=ledger,
            marker=marker,
            marker_path=marker_path,
        )
        progress["stage"] = "completed_read"
        after = ledger.finance_snapshot()
        updated = _assert_finance_state(after, marker, completed=True)
        assert updated["title"] == project["title"]
        assert updated["status"] == project["status"]
        assert updated["deadline"] == project["deadline"]
    finally:
        if sys.exc_info()[0] is None:
            progress["stage"] = "cleanup"
        if session is not None:
            session.close()
        if engine is not None:
            engine.dispose()


def test_live_failure_boundary_redacts_assertion_payload(capsys):
    sentinels = {
        "id": "f00dbabe-0000-4000-8000-000000000001",
        "content": "sensitive synthetic provider notes",
        "secret": "synthetic_secret_1234567890abcdef",
    }

    def fail_with_provider_payload() -> None:
        assert sentinels == {"expected": "different"}

    with pytest.raises(pytest.fail.Exception) as caught:
        _live_failure_boundary({"stage": "proposal"}, fail_with_provider_payload)
    captured = capsys.readouterr()
    rendered = str(caught.value) + captured.out + captured.err
    assert rendered == "Slice E TEST failed: stage=proposal code=unexpected"
    assert not any(value in rendered for value in sentinels.values())


def test_actual_slice_e_project_approval_and_receipt_recovery(monkeypatch):
    if not _selected():
        pytest.skip("explicit TEST Slice E opt-in and exact case selector are required")

    progress = {"stage": "configuration"}
    _live_failure_boundary(progress, lambda: _run_actual_slice_e(monkeypatch, progress))
