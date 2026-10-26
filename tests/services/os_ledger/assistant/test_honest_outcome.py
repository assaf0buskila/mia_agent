"""V2-37 review r1: the outcome of a call is honest, and the per-address limit is exact.

MAJOR-2. ``work`` (V2-26's submit) commits the batch on its own ledger transaction. If the call's
accounting transaction then fails (the audit INSERT, the usage UPDATE and its 3 s lock timeout, the
connection or the COMMIT), the caller must still get the truthful result: a refusal with
``write_outcome: "not_started"`` there would be false, and a retry is only safe because the batch
request id is idempotent. Once ``work`` has returned or raised, an accounting failure is logged as
a reason-code line and nothing else; a failure BEFORE ``work`` stays a refusal (nothing ran).

MAJOR-1. The cheap per-address count before the lookup can be passed by concurrent requests, so
the call's accounting transaction takes the address lock (before the key lock, always in that
order) and counts again: with 59 rows, two concurrent valid calls run exactly one.

Every test runs on the FakeLedger with the in-memory store double and, when the URL is set, on
PostgreSQL with the real ``PgAssistantStore`` and a REAL database failure (see
``AWorld.accounting_fault``).
"""

from __future__ import annotations

import threading
import time

import pytest
from app.services.os_ledger.api import LedgerError

from tests.services.os_ledger.assistant.support import IP_A, IP_B
from tests.services.os_ledger.statements.support import CLAUDE, batch, encode, three_lines

AUDIT_LOG_LINE = "os_assistant op=audit outcome=failed reason=audit_write_failed"


def submit_work(aw, caller, number: int = 1):
    """The route's submit work in miniature: V2-26's real ``submit`` on its own ledger tx."""

    def work(facts):
        submitted = aw.statements.submit(
            caller.ctx, encode(batch(three_lines(), number=number)), source=CLAUDE)
        facts.line_count = int(submitted.summary["line_count"])
        facts.proposal_id = submitted.proposal_id
        return {"proposal_id": submitted.proposal_id, "status": submitted.status}

    return work


# ---------------------------------------------------------------------------------------------
# MAJOR-2


def test_an_accounting_failure_after_the_batch_committed_still_returns_the_batch(aw, caplog):
    caplog.set_level("INFO")
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    with aw.accounting_fault("record", issued["credential_id"]):
        result = aw.gateway.run(caller, "submit", submit_work(aw, caller))
    assert result["status"] == "pending"
    [proposal] = aw.proposals()  # the batch exists and is the one returned
    assert (proposal["proposal_id"], proposal["status"]) == (result["proposal_id"], "pending")
    assert caplog.text.count(AUDIT_LOG_LINE) == 1
    assert aw.audit() == []  # the rare missing audit row is the accepted cost
    assert aw.credential(issued["credential_id"])["use_count"] == 0


def test_the_log_line_for_a_lost_audit_row_carries_codes_only(aw, caplog):
    caplog.set_level("DEBUG")
    issued = aw.issue(label="MY PRIVATE LABEL")
    caller = aw.authenticate(issued["secret"], ip="198.51.100.9")
    with aw.accounting_fault("record", issued["credential_id"]):
        aw.gateway.run(caller, "submit", submit_work(aw, caller))
    for forbidden in (issued["secret"], "198.51.100.9", "MY PRIVATE LABEL", "DEMO SHOP",
                      issued["credential_id"], aw.ip_hmac("198.51.100.9")):
        assert forbidden not in caplog.text


def test_a_failure_raised_by_the_work_keeps_its_own_error_even_if_the_accounting_fails(aw, caplog):
    caplog.set_level("INFO")
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])

    def refused(facts):
        raise LedgerError("invalid_statement_batch", details={"issues": []}, reason="bad_line")

    def unknown(facts):
        raise LedgerError("unknown", reason="commit_failed")

    for work, code, outcome in ((refused, "invalid_statement_batch", "not_started"),
                                (unknown, "unknown", "unknown")):
        with aw.accounting_fault("record", issued["credential_id"]):
            with pytest.raises(LedgerError) as caught:
                aw.gateway.run(caller, "submit", work)
        assert (caught.value.code, caught.value.write_outcome) == (code, outcome)
    assert caplog.text.count(AUDIT_LOG_LINE) == 2


def test_an_accounting_failure_before_the_work_is_still_a_refusal_and_the_work_never_runs(aw,
                                                                                         caplog):
    caplog.set_level("INFO")
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    ran = []

    def work(facts):
        ran.append(1)
        return {}

    with aw.accounting_fault("admit", issued["credential_id"]):
        with pytest.raises(LedgerError) as caught:
            aw.gateway.run(caller, "submit", work)
    assert (caught.value.code, caught.value.write_outcome) == ("ledger_unavailable",
                                                               "not_started")
    assert ran == [] and aw.proposals() == []
    assert AUDIT_LOG_LINE not in caplog.text


def test_a_failed_commit_of_the_accounting_after_the_work_also_returns_the_batch(fake_aw, caplog):
    """A COMMIT failure cannot be provoked on a live PostgreSQL; the double raises it at the end
    of the slot, which ``gateway.run`` must treat exactly like a failed INSERT or UPDATE."""
    caplog.set_level("INFO")
    aw = fake_aw
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    with aw.accounting_fault("commit", issued["credential_id"]):
        result = aw.gateway.run(caller, "submit", submit_work(aw, caller))
    assert result["status"] == "pending" and len(aw.proposals()) == 1
    assert caplog.text.count(AUDIT_LOG_LINE) == 1 and aw.audit() == []


def test_the_accounting_works_again_after_the_failure(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    with aw.accounting_fault("record", issued["credential_id"]):
        aw.gateway.run(caller, "context", lambda facts: {"ok": True})
    assert aw.gateway.run(caller, "context", lambda facts: {"ok": True}) == {"ok": True}
    assert len(aw.audit()) == 1 and aw.credential(issued["credential_id"])["use_count"] == 1


# ---------------------------------------------------------------------------------------------
# MAJOR-1


def two_callers(aw, secrets, ips):
    """Two threads that each authenticate (both pass the cheap count at 59) and then, together,
    run one slow call."""
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def slow(facts):
        time.sleep(0.05)
        return {}

    def attempt(secret, ip):
        caller = aw.authenticate(secret, ip=ip)
        barrier.wait()
        try:
            aw.gateway.run(caller, "context", slow)
            outcomes.append("ok")
        except LedgerError as error:
            outcomes.append(error.reason)

    threads = [threading.Thread(target=attempt, args=(s, i)) for s, i in zip(secrets, ips,
                                                                             strict=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_the_per_address_limit_is_exact_for_concurrent_valid_calls_of_one_key(aw):
    issued = aw.issue()
    aw.seed_audit(59, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    outcomes = two_callers(aw, [issued["secret"]] * 2, [IP_A, IP_A])
    assert sorted(outcomes) == ["ip_limit", "ok"]
    mine = [r for r in aw.audit() if r["client_ip_hmac"] == aw.ip_hmac(IP_A)]
    assert len(mine) == 60  # 59 + the one admitted call; the refused one wrote no row


def test_the_per_address_limit_is_exact_across_two_keys_of_the_same_address(aw):
    one, two = aw.issue(label="one"), aw.issue(label="two", kind="gpt")
    aw.seed_audit(59, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    outcomes = two_callers(aw, [one["secret"], two["secret"]], [IP_A, IP_A])
    assert sorted(outcomes) == ["ip_limit", "ok"]


def test_two_different_addresses_never_block_each_other(aw):
    one, two = aw.issue(label="one"), aw.issue(label="two", kind="gpt")
    callers = [aw.authenticate(key["secret"], ip=ip)
               for key, ip in zip((one, two), (IP_A, IP_B), strict=True)]
    entered_a, entered_b = threading.Event(), threading.Event()
    outcomes = []

    def work_a(facts):
        entered_a.set()
        assert entered_b.wait(1), "B must enter while A is inside work"
        return "a"

    def work_b(facts):
        assert entered_a.is_set()
        entered_b.set()
        return "b"

    def attempt(caller, work):
        try:
            outcomes.append(aw.gateway.run(caller, "context", work))
        except Exception as error:
            outcomes.append(error)

    a = threading.Thread(target=attempt, args=(callers[0], work_a))
    b = threading.Thread(target=attempt, args=(callers[1], work_b))
    a.start()
    assert entered_a.wait(2)
    b.start()
    a.join(5)
    b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert sorted(str(value) for value in outcomes) == ["a", "b"]


def test_valid_and_invalid_writers_share_the_address_lock_and_recount(aw, monkeypatch):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    aw.seed_audit(59, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    entered, release, invalid_started = (threading.Event() for _ in range(3))
    outcomes = []
    original = aw.store._inner.invalid_lookup

    def ready_to_write(**kwargs):
        invalid_started.set()  # the cheap count and lookup have both finished at 59
        return original(**kwargs)

    monkeypatch.setattr(aw.store._inner, "invalid_lookup", ready_to_write)

    def work(facts):
        entered.set()
        assert release.wait(2)
        return "ok"

    def valid():
        try:
            outcomes.append(aw.gateway.run(caller, "context", work))
        except LedgerError as error:
            outcomes.append(error.reason)

    def invalid():
        try:
            aw.authenticate("aosp_" + "z" * 43)
        except LedgerError as error:
            outcomes.append(error.reason)

    a, b = threading.Thread(target=valid), threading.Thread(target=invalid)
    a.start()
    assert entered.wait(2)
    b.start()
    assert invalid_started.wait(2)
    time.sleep(0.1)  # allow the invalid writer to reach its lock while valid work is held
    release.set()
    a.join(5)
    b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert sorted(outcomes) == ["ip_limit", "ok"]
    assert len(aw.audit()) == 60


def test_a_call_refused_by_the_address_recount_writes_no_row_and_does_not_run_the_work(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    aw.seed_audit(60, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    ran = []
    with pytest.raises(LedgerError) as caught:
        aw.gateway.run(caller, "context", lambda facts: ran.append(1))
    assert (caught.value.code, caught.value.reason, caught.value.status) == (
        "rate_limited", "ip_limit", 429)
    assert ran == [] and len(aw.audit()) == 60
    assert aw.credential(issued["credential_id"])["use_count"] == 0
