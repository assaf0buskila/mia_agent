"""E37 two-factor issuance, on the same FakeLedger and PostgreSQL lane."""

from __future__ import annotations

import threading

import pytest
from app.services.os_ledger.api import LedgerError

SESSION = "a" * 64
OTHER = "b" * 64


def refused(call, code):
    with pytest.raises(LedgerError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def test_os_session_alone_stays_pending_and_cannot_mint(aw):
    request = aw.request()
    assert request["status"] == "pending_approval" and "secret" not in request
    refused(lambda: aw.collect(request), "issue_not_approved")
    assert aw.credential_rows() == []


def test_telegram_alone_cannot_collect_without_verified_os_mutation(aw):
    request = aw.request()
    aw.decide_issue(request)
    refused(lambda: aw.collect(request, ctx=aw.w.reader), "browser_mutation_verification_required")
    refused(
        lambda: aw.collect(request, ctx=aw.w.ctx("TELEGRAM_APPROVAL")), "channel_operation_denied"
    )
    assert aw.credential_rows() == []


def test_other_session_and_unknown_id_are_identical_404_on_status_and_collect(aw):
    request = aw.request()
    aw.decide_issue(request)
    for value, session in ((request, OTHER), ({"issue_request_id": "IR_" + "0" * 32}, SESSION)):
        for call in (
            lambda: aw.issue_status(value, session=session),
            lambda: aw.collect(value, session=session),
        ):
            assert refused(call, "credential_unknown").status == 404


def test_plaintext_once_only_hash_stored_and_no_sensitive_logs_or_audit(aw, caplog):
    caplog.set_level("DEBUG")
    request = aw.request()
    row = aw.decide_issue(request)
    result = aw.collect(request)
    refused(lambda: aw.collect(request), "issue_collected")
    assert aw.issue_status(request)["status"] == "collected"
    assert len(aw.credential_rows()) == 1
    stored = repr(aw.issue_rows()) + aw.everything() + row.proposed_parameters
    assert result["secret"] not in stored
    for forbidden in (
        result["secret"],
        SESSION,
        aw.issue_rows()[0]["session_binding"],
        row.approval_id,
        row.resource_id,
    ):
        assert forbidden not in caplog.text + repr(aw.audit())


def test_tampered_immutable_request_refuses_approval(aw):
    request = aw.request()
    aw.tamper_issue(request, label="changed")
    refused(lambda: aw.decide_issue(request), "target_changed")
    refused(lambda: aw.collect(request), "issue_not_approved")


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("blocker", ["watermark", "epoch"])
def test_revoke_all_or_epoch_bump_blocks_pending_and_approved_collect(aw, approved, blocker):
    request = aw.request()
    if approved:
        aw.decide_issue(request)
    if blocker == "watermark":
        aw.w.watermark["value"] = aw.issue_rows()[0]["created_at"]
    else:
        aw.w.bump_epoch()
    with pytest.raises(LedgerError):
        aw.collect(request)
    assert aw.credential_rows() == []


@pytest.mark.parametrize("stage", ["approve", "collect"])
def test_epoch_bump_refuses_old_request_even_with_a_fresh_current_context(aw, stage):
    from app.services.os_ledger import authorize

    from tests.services.os_ledger.statements.support import session_principal

    request = aw.request()
    if stage == "collect":
        aw.decide_issue(request)
    aw.w.bump_epoch()
    fresh = authorize.from_owner_mutation(session_principal(proven=True), aw.config)
    assert fresh.authority_epoch == 2
    call = (lambda: aw.decide_issue(request)) if stage == "approve" else (
        lambda: aw.collect(request, ctx=fresh))
    refused(call, "authority_changed")
    assert aw.credential_rows() == []


@pytest.mark.parametrize("stage", ["request", "approve", "collect"])
def test_kill_switch_refuses_every_issue_stage(aw, stage):
    request = aw.request() if stage != "request" else None
    if stage == "collect":
        aw.decide_issue(request)
    aw.config.kill_switch = True
    call = {
        "request": aw.request,
        "approve": lambda: aw.decide_issue(request),
        "collect": lambda: aw.collect(request),
    }[stage]
    refused(call, "kill_switch")
    assert aw.credential_rows() == []


def test_unset_pepper_fails_closed_before_request_insert(aw):
    aw.config.os_step_up_secret = ""
    assert refused(aw.request, "ledger_unavailable").status == 503
    assert aw.issue_rows() == []


def test_approval_deadline_expires_pending_request(aw):
    request = aw.request()
    aw.w.advance(600)
    refused(lambda: aw.decide_issue(request), "issue_expired")
    refused(lambda: aw.collect(request), "issue_expired")
    assert aw.issue_status(request)["status"] == "expired"


def test_collection_window_is_ten_minutes_from_approval(aw):
    request = aw.request()
    aw.w.advance(599)
    aw.decide_issue(request)
    aw.w.advance(599)
    assert aw.collect(request)["secret"].startswith("aosp_")


def test_collection_window_expires_at_approval_plus_ten_minutes(aw):
    request = aw.request()
    aw.decide_issue(request)
    aw.w.advance(600)
    refused(lambda: aw.collect(request), "issue_expired")
    assert aw.credential_rows() == []


def test_reject_refuses_collect(aw):
    request = aw.request()
    aw.decide_issue(request, decision="reject")
    refused(lambda: aw.collect(request), "issue_rejected")


def test_two_concurrent_collects_mint_exactly_one_key(aw):
    request = aw.request()
    aw.decide_issue(request)
    barrier = threading.Barrier(2)
    outcomes = []

    def collect():
        barrier.wait()
        try:
            outcomes.append(aw.collect(request)["credential_id"])
        except LedgerError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=collect) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert len(outcomes) == 2 and outcomes.count("issue_collected") == 1
    assert len(aw.credential_rows()) == 1


def test_failed_mint_rolls_back_request_and_key_together(aw, monkeypatch):
    request = aw.request()
    aw.decide_issue(request)
    from app.services import assistant_keys as keys

    table = keys.PgCredentialTables if aw.kind == "pg" else keys.MemoryCredentialTables
    original = table.insert

    def fail_after_insert(self, row):
        original(self, row)
        raise LedgerError("ledger_unavailable")

    monkeypatch.setattr(
        table, "insert", fail_after_insert
    )
    with pytest.raises(LedgerError):
        aw.collect(request)
    assert aw.issue_status(request)["status"] == "approved"
    assert aw.credential_rows() == []
    monkeypatch.setattr(table, "insert", original)
    assert aw.collect(request)["secret"]


def test_binding_uses_hmac_and_request_update_grants_are_exact(aw):
    import hashlib
    import hmac

    from app.services.os_ledger import role_policy

    aw.request()
    expected = hmac.new(
        aw.config.os_step_up_secret.encode(), SESSION.encode(), hashlib.sha256
    ).hexdigest()
    assert aw.issue_rows()[0]["session_binding"] == expected != SESSION
    assert role_policy.TABLE_PRIVILEGES["os_assistant_issue_requests"] == {"SELECT", "INSERT"}
    assert role_policy.ADDENDUM_COLUMN_UPDATES["os_assistant_issue_requests"] == (
        "status",
        "approved_at",
        "collected_at",
        "credential_id",
    )
