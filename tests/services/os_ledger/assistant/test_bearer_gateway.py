"""V2-37, the bearer side (V2_PG_CONTRACTS.md §17.2, D2, D6, D8).

``AssistantGateway.authenticate`` is §17.2's enforcement order and ``run`` is the per-key limits
plus the call's single audit row. These tests drive the gateway and the store directly (the HTTP
routes have their own file): on the FakeLedger with the in-memory store double and, when the URL is
set, on PostgreSQL with the real ``PgAssistantStore``, so the audit-table limiter is proved on both.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from datetime import timedelta

import pytest
from app.services import assistant_keys as ac
from app.services.os_ledger import api, authorize
from app.services.os_ledger.api import LedgerError

from tests.services.os_ledger.assistant.support import AUDIT_COLUMNS, IP_A, IP_B, IP_KEY
from tests.services.os_ledger.statements.support import (
    CLAUDE,
    batch,
    encode,
    three_lines,
)

GOOD = "Bearer aosp_" + "A" * 43


def uniform(call):
    """The one 401 every invalid key gets: the same code, status, details and retryability."""
    with pytest.raises(LedgerError) as caught:
        call()
    error = caught.value
    assert (error.code, error.status, dict(error.details), error.retryable) == (
        "assistant_auth_required", 401, {}, False)
    return error


def limited(call):
    with pytest.raises(LedgerError) as caught:
        call()
    error = caught.value
    assert (error.code, error.status, dict(error.details), error.retryable) == (
        "rate_limited", 429, {}, True)
    return error


def bearer_rows(aw):
    return [r for r in aw.audit() if r["operation"] == "bearer"]


def work_ok(facts):
    return {"ok": True}


def fresh_secret():
    return ac.generate_secret()


# ---------------------------------------------------------------------------------------------
# a valid key


@pytest.mark.parametrize("kind,channel", [("claude", "ASSISTANT_CLAUDE"), ("gpt", "ASSISTANT_GPT")])
def test_a_valid_key_authenticates_with_an_assistant_context_of_its_kind(aw, kind, channel):
    issued = aw.issue(kind=kind)
    caller = aw.authenticate(issued["secret"])
    ctx = caller.ctx
    assert (ctx.channel, ctx.credential_id, ctx.numeric_owner) == (
        channel, issued["credential_id"], 123)
    assert ctx.scopes == authorize.ASSISTANT_SCOPES and ctx.browser_mutation_verified is False
    assert caller.credential.credential_id == issued["credential_id"]
    assert caller.ip_hmac == aw.ip_hmac(IP_A)
    assert aw.audit() == []  # authenticating is not a call: the row is written by ``run``


def test_the_context_is_minted_by_authorize_and_the_secret_is_not_kept_on_the_caller(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    assert issued["secret"] not in repr(caller)
    authorize.require(caller.ctx, "read_rules")  # a minted context, accepted by the matrix


# ---------------------------------------------------------------------------------------------
# steps 0-2: no database is touched


def test_a_disabled_feature_is_the_uniform_401_and_touches_no_store(new_aw):
    aw = new_aw()
    issued = aw.issue()
    aw.config.assaf_os_assistant_enabled = False
    uniform(lambda: aw.authenticate(issued["secret"]))
    assert aw.store.calls == {} and aw.audit() == []


def test_a_cookie_is_the_uniform_401_even_with_a_valid_key(aw):
    issued = aw.issue()
    uniform(lambda: aw.authenticate(issued["secret"], cookie=True))
    assert aw.store.calls == {} and aw.audit() == []


@pytest.mark.parametrize("header", [
    (), ("",), ("aosp_" + "A" * 43,), ("bearer aosp_" + "A" * 43,), ("BEARER aosp_" + "A" * 43,),
    ("Bearer  aosp_" + "A" * 43,), ("Bearer aosp_" + "A" * 43 + " ",),
    (" Bearer aosp_" + "A" * 43,),
    ("Bearer aosp_" + "A" * 42,), ("Bearer aosp_" + "A" * 44,), ("Bearer aosp_" + "A" * 42 + "=",),
    ("Bearer aosp_" + "A" * 42 + "+",), ("Bearer aosp_" + "A" * 42 + "/",),
    ("Bearer aosp_" + "A" * 42 + "\n",), ("Bearer aosp-" + "A" * 43,),
    ("Bearer sk_" + "A" * 43,), ("Basic " + "A" * 48,),
    (GOOD, GOOD),  # two Authorization headers
])
def test_a_malformed_header_is_the_uniform_401_with_no_lookup_and_no_row(aw, header):
    uniform(lambda: aw.gateway.authenticate(cookie=False, authorization=header, ip=IP_A))
    assert aw.store.calls == {} and aw.audit() == []


def test_a_missing_ip_key_fails_closed_before_any_store_call(new_aw):
    aw = new_aw(os_assistant_ip_hmac_key="")
    issued = aw.issue()
    with pytest.raises(LedgerError) as caught:
        aw.authenticate(issued["secret"])
    assert caught.value.code == "ledger_unavailable" and aw.store.calls == {}
    short = new_aw(os_assistant_ip_hmac_key="k" * 31)
    with pytest.raises(LedgerError) as caught:
        short.authenticate(short.issue()["secret"])
    assert caught.value.code == "ledger_unavailable"


# ---------------------------------------------------------------------------------------------
# step 4: every invalid key is the same 401, and each is one counted invalid lookup


def revoke_it(aw, issued):
    aw.revoke(issued["credential_id"])


def expire_it(aw, issued):
    aw.clock.advance(31 * 86_400)


def other_epoch(aw, issued):
    aw.w.bump_epoch()  # the settings and the workspace move to epoch 2; the key stays at 1


def kill_switch(aw, issued):
    aw.config.kill_switch = True


def owner_removed(aw, issued):
    aw.config.telegram_owner_user_ids = "999"


def revoked_all(aw, issued):
    created = aw.credential(issued["credential_id"])["created_at"]
    aw.w.watermark["value"] = created + timedelta(seconds=1)


INVALID = [
    ("key_revoked", revoke_it), ("key_expired", expire_it), ("key_epoch", other_epoch),
    ("kill_switch", kill_switch), ("key_owner", owner_removed), ("key_revoked_all", revoked_all),
]


@pytest.mark.parametrize("reason,breaker", INVALID, ids=[r for r, _ in INVALID])
def test_every_kind_of_invalid_key_is_the_same_uniform_401_and_one_counted_row(aw, reason, breaker):
    issued = aw.issue()
    breaker(aw, issued)
    error = uniform(lambda: aw.authenticate(issued["secret"]))
    assert error.reason == reason  # for the log only; the response is identical
    [row] = bearer_rows(aw)
    assert (row["reason"], row["ok"], row["credential_id"]) == (
        reason, False, issued["credential_id"])
    assert set(row) == set(AUDIT_COLUMNS) and row["client_ip_hmac"] == aw.ip_hmac(IP_A)


def test_an_unknown_key_is_the_same_uniform_401_with_no_credential_id_in_its_row(aw):
    error = uniform(lambda: aw.authenticate(fresh_secret()))
    assert error.reason == "key_unknown"
    [row] = bearer_rows(aw)
    assert row["credential_id"] is None and row["reason"] == "key_unknown"


def test_an_unreadable_revoke_all_watermark_fails_closed_without_counting(aw):
    issued = aw.issue()

    def unreadable(owner_id):
        raise LedgerError("ledger_unavailable", reason="revocation_unreadable")

    gateway = aw.make_gateway(revoke_watermark=unreadable)
    with pytest.raises(LedgerError) as caught:
        gateway.authenticate(cookie=False, authorization=(f"Bearer {issued['secret']}",),
                             ip=IP_A)
    assert caught.value.code == "ledger_unavailable" and bearer_rows(aw) == []


def test_the_scopes_are_fixed_by_the_row_and_by_the_matrix(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    assert aw.credential(issued["credential_id"])["scopes"] == (
        "merchant_rules:read own_proposals:read statement:propose")
    assert caller.ctx.scopes == authorize.ASSISTANT_SCOPES
    allowed = {"read_rules", "read_own_batches", "propose_statement_batch"}
    for op in sorted(authorize.OPERATIONS):
        if op in allowed:
            authorize.require(caller.ctx, op)
        else:
            with pytest.raises(LedgerError) as caught:
                authorize.require(caller.ctx, op)
            assert caught.value.code in {"channel_operation_denied", "scope_denied"}, op


def test_an_assistant_context_can_never_decide_approve_commit_or_manage_keys(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    submitted = aw.paste_as(issued, batch(three_lines()))
    review = aw.w.review(submitted.proposal_id)
    service = aw.statements
    for call in (
        lambda: service.approve(caller.ctx, submitted.proposal_id, review["approval_request_id"],
                                review["review_digest"]),
        lambda: service.reject(caller.ctx, submitted.proposal_id, review["rejection_request_id"]),
        lambda: service.save_draft(caller.ctx, submitted.proposal_id, 0, [], {}),
        lambda: service.review(caller.ctx, submitted.proposal_id),
        lambda: service.lines(caller.ctx, submitted.proposal_id, after=None, limit=10),
        lambda: service.cycles(caller.ctx),
        lambda: service.merchant_rules(caller.ctx),
        lambda: aw.service.issue(caller.ctx, label="x", kind="claude"),
        lambda: aw.service.revoke(caller.ctx, issued["credential_id"]),
        lambda: aw.service.revoke_all(caller.ctx),
        lambda: aw.service.list(caller.ctx),
    ):
        with pytest.raises(LedgerError) as caught:
            call()
        assert caught.value.code in {"channel_operation_denied", "scope_denied"}
    assert aw.w.review(submitted.proposal_id)["status"] == "pending"
    assert [p["status"] for p in aw.proposals()] == ["pending"]


# ---------------------------------------------------------------------------------------------
# the per-address count, before the lookup (D2) and the /64 bucket (D8)


def test_the_61st_call_from_an_address_in_ten_minutes_is_429_before_any_lookup(aw):
    issued = aw.issue()
    aw.seed_audit(60, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    limited(lambda: aw.authenticate(issued["secret"]))
    assert aw.store.calls["ip_calls"] == 1 and "find" not in aw.store.calls
    assert aw.authenticate(issued["secret"], ip=IP_B).ctx is not None  # another address is free


def test_the_60th_call_is_still_served_and_old_calls_age_out_of_the_window(aw):
    issued = aw.issue()
    aw.seed_audit(59, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    assert aw.authenticate(issued["secret"]).ctx is not None
    aw.seed_audit(1, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    limited(lambda: aw.authenticate(issued["secret"]))
    aw.clock.advance(601)
    assert aw.authenticate(issued["secret"]).ctx is not None


def test_ipv4_is_counted_per_address_and_ipv6_per_slash_64(aw):
    key = IP_KEY.encode()
    assert aw.ip_hmac(IP_A) == hmac.new(key, IP_A.encode(), hashlib.sha256).hexdigest()
    assert aw.ip_hmac("203.0.113.10") != aw.ip_hmac("203.0.113.11")
    one, two = "2001:db8:0:1::1", "2001:db8:0:1:ffff:aaaa:bbbb:cccc"
    assert aw.ip_hmac(one) == aw.ip_hmac(two)
    assert aw.ip_hmac(one) != aw.ip_hmac("2001:db8:0:2::1")
    assert aw.ip_hmac("::ffff:203.0.113.10") == aw.ip_hmac("203.0.113.10")
    assert aw.ip_hmac("unknown") == hmac.new(key, b"unknown", hashlib.sha256).hexdigest()
    assert ac.ip_bucket("2001:db8:0:1:ffff::9") == "2001:db8:0:1::/64"
    assert aw.ip_hmac(one) not in aw.everything()


def test_a_client_rotating_inside_its_slash_64_shares_one_bucket(aw):
    issued = aw.issue()
    aw.seed_audit(60, ip_hmac=aw.ip_hmac("2001:db8:0:1::1"), operation="context", ok=True,
                  reason="ok")
    limited(lambda: aw.authenticate(issued["secret"], ip="2001:db8:0:1:1234:5678:9abc:def0"))
    assert aw.authenticate(issued["secret"], ip="2001:db8:0:2::1").ctx is not None


# ---------------------------------------------------------------------------------------------
# the invalid-lookup budgets: per address (20 per 10 min) and global (100 per 10 min)


def test_the_21st_invalid_lookup_from_an_address_is_429_and_writes_no_row(aw):
    for _ in range(ac.FAIL_LIMIT):
        uniform(lambda: aw.authenticate(fresh_secret()))
    assert len(bearer_rows(aw)) == 20
    error = limited(lambda: aw.authenticate(fresh_secret()))
    assert error.reason == "auth_budget_ip" and len(bearer_rows(aw)) == 20


def test_a_valid_key_is_never_blocked_by_the_spent_budget(aw):
    issued = aw.issue()
    for _ in range(ac.FAIL_LIMIT):
        uniform(lambda: aw.authenticate(fresh_secret()))
    limited(lambda: aw.authenticate(fresh_secret()))
    assert aw.authenticate(issued["secret"]).ctx is not None
    uniform_after = limited(lambda: aw.authenticate(fresh_secret()))
    assert uniform_after.reason == "auth_budget_ip"


def test_the_budget_is_per_address_and_slides_with_the_window(aw):
    for _ in range(ac.FAIL_LIMIT):
        uniform(lambda: aw.authenticate(fresh_secret()))
    uniform(lambda: aw.authenticate(fresh_secret(), ip=IP_B))  # another address: still a 401
    limited(lambda: aw.authenticate(fresh_secret()))
    aw.clock.advance(601)
    uniform(lambda: aw.authenticate(fresh_secret()))


def test_only_invalid_lookups_count_against_the_budget(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    for _ in range(25):
        aw.gateway.run(caller, "context", work_ok)
    assert bearer_rows(aw) == []
    for _ in range(ac.FAIL_LIMIT):
        uniform(lambda: aw.authenticate(fresh_secret()))
    limited(lambda: aw.authenticate(fresh_secret()))


def test_a_client_rotating_inside_a_slash_64_cannot_outrun_the_budget(aw):
    for number in range(ac.FAIL_LIMIT):
        uniform(lambda n=number: aw.authenticate(fresh_secret(), ip=f"2001:db8:0:9::{n + 1:x}"))
    limited(lambda: aw.authenticate(fresh_secret(), ip="2001:db8:0:9:aaaa::1"))
    uniform(lambda: aw.authenticate(fresh_secret(), ip="2001:db8:0:a::1"))  # another /64


def spread(aw, count):
    """``count`` invalid-lookup rows, each from its own address (none near the per-address cap)."""
    for number in range(count):
        aw.seed_audit(1, ip_hmac=f"{number + 1:064x}")


def test_the_global_cap_of_100_invalid_lookups_per_ten_minutes_answers_429_and_writes_no_row(aw):
    issued = aw.issue()
    spread(aw, 99)
    uniform(lambda: aw.authenticate(fresh_secret(), ip=IP_A))  # the 100th is still a 401
    assert len(bearer_rows(aw)) == 100
    error = limited(lambda: aw.authenticate(fresh_secret(), ip=IP_B))
    assert error.reason == "auth_budget_global" and len(bearer_rows(aw)) == 100
    # a valid key is never blocked by the budget
    assert aw.authenticate(issued["secret"], ip=IP_B).ctx is not None
    aw.clock.advance(601)
    uniform(lambda: aw.authenticate(fresh_secret(), ip=IP_B))


# ---------------------------------------------------------------------------------------------
# the per-key limits (30 calls an hour, 5 batches a day) and the audit row of every call


def test_the_31st_call_in_an_hour_is_429_and_a_refused_call_does_not_extend_the_block(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    for _ in range(30):
        aw.gateway.run(caller, "context", work_ok)
    for _ in range(3):
        error = limited(lambda: aw.gateway.run(caller, "context", work_ok))
        assert error.reason == "key_hour_limit"
    refused = [r for r in aw.audit() if r["reason"] == "key_hour_limit"]
    assert len(refused) == 3 and all(r["ok"] is False for r in refused)
    aw.clock.advance(3601)
    assert aw.gateway.run(caller, "context", work_ok) == {"ok": True}


def test_the_work_does_not_run_when_the_key_is_over_its_limit(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    aw.seed_audit(30, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok",
                  credential_id=issued["credential_id"])
    ran = []
    limited(lambda: aw.gateway.run(caller, "context", lambda facts: ran.append(1)))
    assert ran == []


def test_the_sixth_batch_in_a_day_is_429_and_only_accepted_batches_count(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])

    def bad(facts):
        raise LedgerError("invalid_statement_batch", details={"issues": []}, reason="bad_line")

    for _ in range(3):
        with pytest.raises(LedgerError):
            aw.gateway.run(caller, "submit", bad)  # a refused batch uses no quota
    for _ in range(5):
        aw.gateway.run(caller, "submit", work_ok)
    aw.gateway.run(caller, "context", work_ok)  # other operations never touch the batch quota
    error = limited(lambda: aw.gateway.run(caller, "submit", work_ok))
    assert error.reason == "key_day_limit"
    aw.clock.advance(86_401)
    assert aw.gateway.run(caller, "submit", work_ok) == {"ok": True}


def test_each_key_has_its_own_limits(aw):
    one, two = aw.issue(label="one"), aw.issue(label="two", kind="gpt")
    first, second = aw.authenticate(one["secret"]), aw.authenticate(two["secret"])
    for _ in range(5):
        aw.gateway.run(first, "submit", work_ok)
    limited(lambda: aw.gateway.run(first, "submit", work_ok))
    assert aw.gateway.run(second, "submit", work_ok) == {"ok": True}


def test_every_call_writes_exactly_one_row_of_codes_and_counts_only(aw):
    issued = aw.issue(label="MY SECRET LABEL")
    caller = aw.authenticate(issued["secret"], ip="198.51.100.7")

    def counted(facts):
        facts.bytes, facts.line_count, facts.proposal_id = 1234, 3, "PRO_" + "1" * 32
        return {"ok": True}

    def refused(facts):
        facts.bytes = 77
        raise LedgerError("invalid_statement_batch", details={"issues": []}, reason="bad_line")

    def broken(facts):
        raise RuntimeError("the statement says COFFEE SHOP 4271")

    aw.gateway.run(caller, "submit", counted)
    aw.clock.advance(1)  # rows are ordered by time; a frozen clock would tie them
    with pytest.raises(LedgerError):
        aw.gateway.run(caller, "submit", refused)
    aw.clock.advance(1)
    with pytest.raises(RuntimeError):
        aw.gateway.run(caller, "status", broken)
    rows = aw.audit()
    assert len(rows) == 3 and all(set(r) == set(AUDIT_COLUMNS) for r in rows)
    ip = hmac.new(IP_KEY.encode(), b"198.51.100.7", hashlib.sha256).hexdigest()
    assert [(r["operation"], r["ok"], r["reason"], r["bytes"], r["line_count"],
             r["proposal_id"]) for r in rows] == [
        ("submit", True, "ok", 1234, 3, "PRO_" + "1" * 32),
        ("submit", False, "bad_line", 77, 0, None),
        ("status", False, "internal_error", 0, 0, None)]
    assert all(r["client_ip_hmac"] == ip and r["credential_id"] == issued["credential_id"]
               for r in rows)
    text = repr(rows)
    for forbidden in ("198.51.100.7", "MY SECRET LABEL", "COFFEE", "4271", "aosp_",
                      issued["secret"]):
        assert forbidden not in text


def test_a_valid_call_updates_last_used_and_use_count_and_an_invalid_one_does_not(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    aw.gateway.run(caller, "context", work_ok)
    aw.clock.advance(5)
    aw.gateway.run(caller, "context", work_ok)
    row = aw.credential(issued["credential_id"])
    assert row["use_count"] == 2 and row["last_used_at"] == aw.clock()
    aw.revoke(issued["credential_id"])
    uniform(lambda: aw.authenticate(issued["secret"]))
    assert aw.credential(issued["credential_id"])["use_count"] == 2


@pytest.mark.parametrize("limit,operation,seeded,reason", [
    (ac.BATCH_LIMIT, "submit", 4, "key_day_limit"),
    (ac.KEY_LIMIT, "context", 29, "key_hour_limit")])
def test_the_limits_are_exact_under_concurrency(aw, limit, operation, seeded, reason):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    aw.seed_audit(seeded, ip_hmac=aw.ip_hmac(IP_A), operation=operation, ok=True, reason="ok",
                  credential_id=issued["credential_id"])
    outcomes: list[str] = []

    def slow(facts):
        time.sleep(0.05)
        return {}

    def attempt():
        try:
            aw.gateway.run(caller, operation, slow)
            outcomes.append("ok")
        except LedgerError as error:
            outcomes.append(error.reason)

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == sorted(["ok"] + [reason] * 4), (limit, outcomes)


# ---------------------------------------------------------------------------------------------
# A6: a key revoked between authentication and the ledger transaction


def test_a_key_revoked_after_authentication_leaves_a_batch_that_approve_rejects(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])  # the bearer call is authenticated ...
    aw.revoke(issued["credential_id"])  # ... the owner revokes ...
    raw = encode(batch(three_lines()))
    submitted = aw.statements.submit(caller.ctx, raw, source=CLAUDE)  # ... then its submit lands
    assert submitted.status == "pending"
    result = aw.w.approve(submitted.proposal_id)  # the owner approves: V2-26 re-checks the key
    assert result.receipt.status == "rejected"
    assert tuple(result.receipt.reason_codes) == ("credential_revoked",)
    assert aw.w.expenses() == []  # nothing was booked
    assert [p["status"] for p in aw.proposals()] == ["rejected"]


def test_revoking_again_clears_a_straggler_batch_of_an_already_revoked_key(aw):
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    aw.revoke(issued["credential_id"])
    submitted = aw.statements.submit(caller.ctx, encode(batch(three_lines())), source=CLAUDE)
    assert [p["status"] for p in aw.proposals()] == ["pending"]
    aw.revoke(issued["credential_id"])
    states = {p["proposal_id"]: p["status"] for p in aw.proposals()}
    assert states[submitted.proposal_id] == "rejected"


def test_the_error_type_of_a_refusal_is_the_ledger_error_of_the_api_module():
    assert LedgerError is api.LedgerError
