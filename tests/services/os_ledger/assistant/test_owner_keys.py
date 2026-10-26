"""V2-37, the owner side of the assistant keys (V2_PG_CONTRACTS.md §17.1, §9, D5).

``AssistantCredentialService`` issues, lists and revokes keys inside ONE ledger transaction each
(``kind="statement_batch"``, the kind both V2-26's ``revoke_pending_for_credential`` and a
credential-only write are admitted under). Every test runs on the FakeLedger and, when the URL is
set, on PostgreSQL.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from app.services import assistant_keys as ac
from app.services.os_ledger import api
from app.services.owner_sessions import credential_hash

from tests.services.os_ledger.statements.support import batch, three_lines

ITEM_KEYS = {"credential_id", "label", "kind", "created_at", "expires_at", "last_used_at",
             "use_count", "revoked_at"}
BINDING = {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": "test",
           "authority_epoch": 1}
SECRET = re.compile(r"aosp_[A-Za-z0-9_-]{43}")


def refused(call, code):
    with pytest.raises(api.LedgerError) as caught:
        call()
    assert caught.value.code == code, (caught.value.code, caught.value.reason)
    return caught.value


def pending(aw):
    return [p for p in aw.proposals() if p["status"] == "pending"]


# ---------------------------------------------------------------------------------------------
# issue


def test_issue_returns_the_list_item_plus_the_secret_once(aw):
    issued = aw.issue(label="Claude desktop", kind="claude")
    assert set(issued) == BINDING.keys() | ITEM_KEYS | {"secret"}
    assert {k: issued[k] for k in BINDING} == BINDING
    assert "observed_version" not in issued  # E12 b: credentials are not versioned state
    assert SECRET.fullmatch(issued["secret"])
    assert re.fullmatch(r"AC_[0-9a-f]{32}", issued["credential_id"])
    assert (issued["label"], issued["kind"], issued["use_count"]) == ("Claude desktop", "claude", 0)
    assert issued["last_used_at"] is None and issued["revoked_at"] is None
    listed = aw.listing()
    assert issued["secret"] not in repr(listed)


def test_only_the_hash_is_stored_and_the_secret_is_nowhere(aw):
    issued = aw.issue()
    [row] = aw.credential_rows()
    assert row["secret_hash"] == credential_hash(issued["secret"], purpose="assistant")
    assert row["secret_hash"] != issued["secret"]
    assert issued["secret"] not in aw.everything()
    assert issued["secret"][5:] not in aw.everything()


def test_scopes_and_binding_are_fixed(aw):
    issued = aw.issue(kind="gpt")
    [row] = aw.credential_rows()
    assert row["scopes"] == "merchant_rules:read own_proposals:read statement:propose"
    assert (row["owner_id"], row["epoch"], row["kind"]) == ("123", 1, "gpt")
    assert row["credential_id"] == issued["credential_id"]


@pytest.mark.parametrize("days,expected", [(None, 30), (1, 1), (30, 30), (90, 90)])
def test_expiry_defaults_to_thirty_days_and_never_exceeds_ninety(aw, days, expected):
    issued = aw.issue(days=days)
    row = aw.credential(issued["credential_id"])
    assert row["expires_at"] - row["created_at"] == timedelta(days=expected)


@pytest.mark.parametrize("days", [0, -1, 91, 365, True, False, "30", 30.0, 1.5, [30]])
def test_an_expiry_outside_one_to_ninety_whole_days_is_refused(aw, days):
    error = refused(lambda: aw.issue(days=days), "validation_error")
    assert error.details == {"reason": "invalid_expiry"}
    assert aw.credential_rows() == []


@pytest.mark.parametrize("kind", ["", "Claude", "chatgpt", "claude ", None, 1, "gpt\n"])
def test_a_kind_other_than_claude_or_gpt_is_refused(aw, kind):
    assert refused(lambda: aw.issue(kind=kind), "validation_error").details == {
        "reason": "invalid_kind"}


@pytest.mark.parametrize("label", ["", "   ", "x" * 41, "a\nb", "a\x00b", "tab\there", None, 5,
                                   "‮" + "x", "x y"])
def test_a_bad_label_is_refused(aw, label):
    assert refused(lambda: aw.issue(label=label), "validation_error").details == {
        "reason": "invalid_label"}
    assert aw.credential_rows() == []


def test_a_label_is_trimmed_and_may_be_hebrew_up_to_forty_characters(aw):
    issued = aw.issue(label="  הקלוד שלי  ")
    assert issued["label"] == "הקלוד שלי"
    assert aw.issue(label="x" * 40)["label"] == "x" * 40


def test_two_issues_give_two_distinct_keys(aw):
    a, b = aw.issue(), aw.issue()
    assert a["credential_id"] != b["credential_id"] and a["secret"] != b["secret"]
    assert len(aw.listing()["credentials"]) == 2


# ---------------------------------------------------------------------------------------------
# list


def test_the_list_has_the_binding_the_items_and_no_secret_and_no_version(aw):
    first = aw.issue(label="one")
    aw.clock.advance(5)
    second = aw.issue(label="two", kind="gpt")
    body = aw.listing()
    assert set(body) == BINDING.keys() | {"credentials"}
    assert {k: body[k] for k in BINDING} == BINDING and "observed_version" not in body
    assert all(set(item) == ITEM_KEYS for item in body["credentials"])
    assert {i["credential_id"] for i in body["credentials"]} == {
        first["credential_id"], second["credential_id"]}
    assert first["secret"] not in repr(body) and second["secret"] not in repr(body)
    assert "secret_hash" not in repr(body)


def test_an_empty_list_is_still_a_bound_body(aw):
    body = aw.listing()
    assert body["credentials"] == [] and {k: body[k] for k in BINDING} == BINDING


# ---------------------------------------------------------------------------------------------
# admission: the §9 matrix and the switches


def test_only_an_owner_mutation_with_the_approve_scope_may_manage_keys(aw):
    w = aw.w
    refused(lambda: aw.issue(ctx=w.reader), "browser_mutation_verification_required")
    refused(lambda: aw.issue(ctx=w.assistant()), "channel_operation_denied")
    refused(lambda: aw.issue(ctx=w.ctx("MIA")), "channel_operation_denied")
    refused(lambda: aw.issue(ctx=w.ctx("TELEGRAM_APPROVAL")), "channel_operation_denied")
    refused(lambda: aw.issue(ctx=w.worker), "channel_operation_denied")
    refused(lambda: aw.issue(ctx=w.ctx("OS", scopes=frozenset({"assaf-os:read",
                                                               "assaf-os:propose"}))),
            "scope_denied")
    refused(lambda: aw.revoke("AC_" + "a" * 32, ctx=w.assistant()), "channel_operation_denied")
    refused(lambda: aw.revoke_all(ctx=w.assistant()), "channel_operation_denied")
    assert aw.credential_rows() == []


def test_listing_needs_an_owner_read_and_denies_every_other_channel(aw):
    w = aw.w
    for channel in ("MIA", "TELEGRAM_APPROVAL"):
        refused(lambda c=channel: aw.listing(ctx=w.ctx(c)), "channel_operation_denied")
    refused(lambda: aw.listing(ctx=w.assistant()), "channel_operation_denied")
    refused(lambda: aw.listing(ctx=w.worker), "channel_operation_denied")


def test_the_feature_flag_and_the_backend_gate_every_method(new_aw):
    off = new_aw(assaf_os_assistant_enabled=False)
    for call in (off.listing, off.issue, lambda: off.revoke("AC_" + "a" * 32), off.revoke_all):
        refused(call, "operation_unavailable")
    sheets = new_aw(assaf_os_backend="sheets")
    refused(sheets.issue, "operation_unavailable")


def test_the_kill_switch_stops_issue_and_revoke_inside_the_ledger(aw):
    issued = aw.issue()
    aw.config.kill_switch = True
    refused(aw.issue, "kill_switch")
    refused(lambda: aw.revoke(issued["credential_id"]), "kill_switch")
    aw.config.kill_switch = False
    assert aw.credential(issued["credential_id"])["revoked_at"] is None


def test_a_fenced_statements_domain_refuses_with_authority_not_active(aw):
    aw.w.fence_statements()
    refused(aw.issue, "authority_not_active")


def test_an_issue_is_not_a_ledger_version_bump_and_writes_no_proposal(aw):
    before = aw.w.version()
    aw.issue()
    assert aw.w.version() == before
    assert aw.proposals() == [] and aw.receipts() == []


# ---------------------------------------------------------------------------------------------
# revoke


def test_revoke_marks_the_key_and_returns_the_list(aw):
    one, two = aw.issue(label="one"), aw.issue(label="two")
    body = aw.revoke(one["credential_id"])
    by_id = {i["credential_id"]: i for i in body["credentials"]}
    assert by_id[one["credential_id"]]["revoked_at"] is not None
    assert by_id[two["credential_id"]]["revoked_at"] is None
    assert {k: body[k] for k in BINDING} == BINDING and "observed_version" not in body


def test_revoking_twice_changes_nothing_the_second_time(aw):
    issued = aw.issue()
    aw.revoke(issued["credential_id"])
    first = aw.credential(issued["credential_id"])["revoked_at"]
    version = aw.w.version()
    aw.w.advance(60)
    aw.revoke(issued["credential_id"])
    assert aw.credential(issued["credential_id"])["revoked_at"] == first
    assert aw.w.version() == version


@pytest.mark.parametrize("bad", ["", "x", "AC_" + "A" * 32, "AC_" + "a" * 31, "ac_" + "a" * 32,
                                 "AC_" + "a" * 32 + "0", 5, None])
def test_a_malformed_or_unknown_credential_id_is_404(aw, bad):
    error = refused(lambda: aw.revoke(bad), "credential_unknown")
    assert error.status == 404 and error.details == {}
    error = refused(lambda: aw.revoke("AC_" + "e" * 32), "credential_unknown")
    assert error.status == 404 and error.details == {}


def test_revoke_rejects_that_keys_pending_batches_in_the_same_transaction(aw):
    one, two = aw.issue(label="one"), aw.issue(label="two", kind="gpt")
    first = aw.paste_as(one, batch(three_lines(), number=1, label="Card One"))
    other = aw.paste_as(two, batch(three_lines(), number=2, label="Card Two"), kind="gpt")
    assert len(pending(aw)) == 2
    aw.revoke(one["credential_id"])
    states = {p["proposal_id"]: p["status"] for p in aw.proposals()}
    assert states[first.proposal_id] == "rejected" and states[other.proposal_id] == "pending"
    [receipt] = aw.receipts()
    assert receipt["status"] == "rejected" and tuple(receipt["reason_codes"]) == (
        "credential_revoked",)
    # the personal detail is purged with the rejection (I11)
    held = aw.statement_tables()["batch_lines"]
    assert all(r.detail is None for (pid, _), r in held.items() if pid == first.proposal_id)
    assert any(r.detail is not None for (pid, _), r in held.items() if pid == other.proposal_id)


def test_revoke_all_covers_every_unrevoked_key_including_an_expired_one(aw):
    live = aw.issue(label="live")
    old = aw.issue(label="old", days=1)
    gone = aw.issue(label="gone")
    aw.revoke(gone["credential_id"])
    held = aw.paste_as(old, batch(three_lines(), number=1, label="Card Old"))
    aw.paste_as(live, batch(three_lines(), number=2, label="Card Live"))
    aw.w.advance(3 * 86_400)  # the 1-day key has expired; its batch (7 days) has not
    first_revoked = aw.credential(gone["credential_id"])["revoked_at"]
    body = aw.revoke_all()
    assert all(i["revoked_at"] is not None for i in body["credentials"])
    assert aw.credential(gone["credential_id"])["revoked_at"] == first_revoked
    assert pending(aw) == []
    assert {p["proposal_id"] for p in aw.proposals() if p["status"] == "rejected"} >= {
        held.proposal_id}


def test_revoke_all_with_no_keys_is_an_empty_list(aw):
    body = aw.revoke_all()
    assert body["credentials"] == [] and aw.receipts() == []


class Faulty:
    """V2-26's service with one fault around ``revoke_pending_for_credential``."""

    def __init__(self, inner, *, before=None, after=None, on_call=None) -> None:
        self.inner, self.before, self.after, self.on_call = inner, before, after, on_call
        self.calls: list[tuple[object, str]] = []

    def revoke_pending_for_credential(self, tx, credential_id):
        self.calls.append((tx, credential_id))
        if self.on_call is not None:
            self.on_call(tx, credential_id)
        if self.before is not None:
            raise self.before
        count = self.inner.revoke_pending_for_credential(tx, credential_id)
        if self.after is not None:
            raise self.after
        return count


def faulty_world(aw, **fault):
    statements = Faulty(aw.statements, **fault)
    aw.service = ac.AssistantCredentialService(aw.w.ledger, aw.w.config, statements)
    return statements


@pytest.mark.parametrize("where", ["before", "after"])
def test_a_failure_around_revoke_pending_leaves_both_the_key_and_the_batches_untouched(aw, where):
    issued = aw.issue()
    submitted = aw.paste_as(issued, batch(three_lines()))
    version, receipts = aw.w.version(), aw.receipts()
    faulty_world(aw, **{where: RuntimeError("boom")})
    refused(lambda: aw.revoke(issued["credential_id"]), "ledger_unavailable")
    assert aw.credential(issued["credential_id"])["revoked_at"] is None
    assert [p["status"] for p in aw.proposals()] == ["pending"]
    assert aw.receipts() == receipts and aw.w.version() == version
    assert aw.w.review(submitted.proposal_id)["status"] == "pending"


def test_revoke_runs_the_batch_rejection_on_the_open_ledger_transaction(aw):
    issued = aw.issue()
    seen = {}

    def spy(tx, credential_id):
        # the key's row is already updated INSIDE this transaction, and the transaction is the
        # ledger's own (it carries the context it locked for)
        seen["revoked_in_tx"] = ac.credential_tables(tx).get(credential_id).revoked_at
        seen["owner"] = tx.ctx.numeric_owner

    faulty = faulty_world(aw, on_call=spy)
    aw.revoke(issued["credential_id"])
    assert len(faulty.calls) == 1 and faulty.calls[0][1] == issued["credential_id"]
    assert seen["revoked_in_tx"] is not None and seen["owner"] == 123


def test_a_failure_in_revoke_all_after_the_first_key_undoes_the_first_key_too(aw):
    one, two = aw.issue(label="one"), aw.issue(label="two")
    aw.paste_as(one, batch(three_lines(), number=1, label="Card One"))
    aw.paste_as(two, batch(three_lines(), number=2, label="Card Two"))
    calls = []

    def fail_second(tx, credential_id):
        calls.append(credential_id)
        if len(calls) == 2:
            raise RuntimeError("boom")

    faulty_world(aw, on_call=fail_second)
    refused(aw.revoke_all, "ledger_unavailable")
    assert len(calls) == 2
    assert all(r["revoked_at"] is None for r in aw.credential_rows())
    assert [p["status"] for p in aw.proposals()] == ["pending", "pending"]


def test_a_revoked_key_with_no_batches_needs_no_receipt(aw):
    issued = aw.issue()
    before = aw.w.version()
    aw.revoke(issued["credential_id"])
    assert aw.receipts() == [] and aw.w.version() == before
