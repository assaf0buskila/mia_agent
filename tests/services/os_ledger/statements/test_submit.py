"""V2-26 submit (contract §12.2, §5.3, FORK-SUPERSEDE): ingress, replay, supersede, refusals."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_env import NOW
from tests.services.os_ledger.statements.support import (
    CLAUDE,
    CRED,
    CRED_B,
    GPT,
    PASTE,
    batch,
    encode,
    line,
    three_lines,
)


def refused(code: str):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


def test_paste_creates_a_pending_batch_with_the_submitted_line_summary(world):
    submitted = world.paste(batch(three_lines()))
    assert submitted.status == "pending"
    assert submitted.conflict is False and submitted.superseded_proposal_id is None
    assert submitted.summary == {"line_count": 3, "business_count": 2, "personal_count": 1,
                                 "unassigned_count": 0, "disagreement_count": 0}
    proposal = next(p for p in world.rows("os_proposals")
                    if p["proposal_id"] == submitted.proposal_id)
    assert (proposal["kind"], proposal["source"], proposal["entity"]) == (
        "statement_batch", "ASSISTANT_PASTE", "statements")
    assert proposal["record_id"] == submitted.statement_id
    assert proposal["credential_id"] is None and proposal["status"] == "pending"
    assert proposal["expires_at"] - proposal["created_at"] == timedelta(days=7)
    assert proposal["created_at"] == NOW
    # The request id is the owner-paste id and the digest binds batch, source and credential.
    assert proposal["request_id"] == api.paste_request_uuid("123", world_client_batch_id(1))
    assert world.version() == 0  # a propose moves pending_seq, not the version


def world_client_batch_id(number: int) -> str:
    from tests.services.os_ledger.statements.support import uuid_v4
    return uuid_v4(0x9000 + number)


def test_the_assistant_request_id_binds_the_credential(world):
    world.credential()
    submitted = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    proposal = world.rows("os_proposals")[0]
    assert proposal["request_id"] == api.assistant_request_uuid(CRED, world_client_batch_id(1))
    assert proposal["credential_id"] == CRED and proposal["source"] == CLAUDE
    assert submitted.status == "pending"


def test_a_replay_returns_the_stored_batch_and_writes_nothing(world):
    first = world.paste(batch(three_lines()))
    before = world.everything()
    second = world.paste(batch(three_lines()))
    assert second.proposal_id == first.proposal_id and second.status == "pending"
    assert second.summary == first.summary
    assert world.everything() == before
    assert len(world.rows("os_proposals")) == 1


def test_the_same_batch_id_with_other_content_is_an_idempotency_mismatch(world):
    world.paste(batch(three_lines()))
    other = three_lines()
    other[0] = line("OTHER SHOP", 777)
    before = world.everything()
    with refused("idempotency_mismatch"):
        world.paste(batch(other))
    assert world.everything() == before


def test_the_same_batch_id_from_another_source_is_its_own_request(world):
    # §12.2: the request id binds the actor (owner paste or credential), so this is not a replay.
    world.credential()
    first = world.paste(batch(three_lines()))
    second = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    assert second.proposal_id != first.proposal_id and second.conflict is True


def test_every_line_keeps_its_detail_while_pending_and_the_proposal_holds_none(world):
    submitted = world.paste(batch(three_lines()))
    tables = world.tables()
    kept = tables["batch_lines"]
    assert len(kept) == 3 and all(row.detail is not None and row.purged_at is None
                                  for row in kept.values())
    details = json.dumps([row.detail for row in kept.values()], ensure_ascii=False)
    assert "PERSONAL CAFE" in details and "421731" in details
    # I11/FORK-PERSONAL-MIN: nothing of a personal line is anywhere else.
    outside = repr(world.rows("os_proposals")) + repr(world.rows("os_events"))
    assert "PERSONAL CAFE" not in outside and "421731" not in outside
    assert submitted.proposal_id in {key[0] for key in kept}


def test_the_proposal_mutations_hold_only_business_expense_effects(world):
    world.paste(batch(three_lines()))
    proposal = world.rows("os_proposals")[0]
    mutations = proposal["mutations"]
    assert [(m["entity"], m["op"], m["before"]) for m in mutations] == [
        ("expenses", "upsert", None)]
    created = mutations[0]["after"]
    assert created["vendor"] == "DEMO SHOP" and created["revision"] == 1
    assert created["source_key"].startswith("stmt:STM_")


def test_a_pending_statement_row_carries_the_header_and_no_committed_lines(world):
    submitted = world.paste(batch(three_lines()))
    tables = world.tables()
    statement = tables["statements"][submitted.statement_id]
    assert statement.account_label == "Synthetic Business Card"
    assert statement.charge_date.isoformat() == "2026-10-15" and statement.cycle == "2026-10"
    assert (statement.line_count, statement.approved_proposal_id) == (0, None)
    assert statement.current_proposal_id == submitted.proposal_id
    assert tables["lines"] == {}


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", encode({"schema": "nope"}),
                                 encode({**batch(three_lines()), "lines": []})])
def test_an_invalid_batch_is_a_422_with_codes_only_and_writes_nothing(world, raw):
    before = world.everything()
    with pytest.raises(api.LedgerError) as caught:
        world.paste(raw)
    error = caught.value
    assert (error.code, error.status) == ("invalid_statement_batch", 422)
    issues = error.details["issues"]
    assert 1 <= len(issues) <= 25 and all(set(i) == {"field", "code"} for i in issues)
    assert world.everything() == before


def test_an_oversize_body_is_refused(world):
    huge = batch([line(f"SHOP {n}", 100 + n) for n in range(10)])
    raw = encode(huge) + b" " * (128 * 1024)
    with pytest.raises(api.LedgerError) as caught:
        world.paste(raw)
    assert caught.value.code == "invalid_statement_batch"
    assert {"field": "", "code": "batch_too_large"} in caught.value.details["issues"]
    assert world.rows("os_proposals") == []


def test_more_than_300_lines_are_refused(world):
    many = batch([line(f"SHOP {n}", 100 + n) for n in range(301)])
    with pytest.raises(api.LedgerError) as caught:
        world.paste(many)
    assert caught.value.code == "invalid_statement_batch"


def test_a_source_that_does_not_match_the_channel_is_denied(world):
    world.credential()
    with refused("channel_operation_denied"):
        world.paste(batch(three_lines()), ctx=world.assistant(), source=PASTE)
    with refused("channel_operation_denied"):
        world.paste(batch(three_lines()), ctx=world.assistant(), source=GPT)
    with refused("channel_operation_denied"):
        world.paste(batch(three_lines()), ctx=world.owner, source=CLAUDE)
    assert world.rows("os_proposals") == []


@pytest.mark.parametrize("channel,code", [
    ("MIA", "channel_operation_denied"), ("TELEGRAM_APPROVAL", "channel_operation_denied"),
    ("WORKER", "channel_operation_denied")])
def test_other_channels_cannot_submit(world, channel, code):
    with refused(code):
        world.paste(batch(three_lines()), ctx=world.ctx(channel))
    assert world.rows("os_proposals") == []


def test_a_paste_needs_the_propose_scope_and_a_verified_mutation(world):
    with refused("scope_denied"):
        world.paste(batch(three_lines()), ctx=world.ctx("OS", scopes={"assaf-os:read"}))
    with refused("browser_mutation_verification_required"):
        world.paste(batch(three_lines()), ctx=world.reader)
    assert world.rows("os_proposals") == []


def test_the_numeric_owner_is_rechecked(world):
    world.config.telegram_owner_user_ids = "999"
    with refused("owner_auth_required"):
        world.paste(batch(three_lines()))


# ---- admission switches ---------------------------------------------------------------------


def test_the_kill_switch_blocks_a_submit(world):
    world.config.kill_switch = True
    with refused("kill_switch"):
        world.paste(batch(three_lines()))
    assert world.rows("os_proposals") == []


def test_the_freeze_blocks_a_submit_as_new_intent(world):
    world.config.assaf_os_freeze = True
    with refused("os_frozen"):
        world.paste(batch(three_lines()))
    assert world.rows("os_proposals") == []


def test_a_disabled_flag_or_the_sheets_backend_is_unavailable(world):
    world.config.assaf_os_statements_enabled = False
    with refused("operation_unavailable"):
        world.paste(batch(three_lines()))
    world.config.assaf_os_statements_enabled = True
    world.config.assaf_os_backend = "sheets"
    with refused("operation_unavailable"):
        world.paste(batch(three_lines()))


def test_statements_must_be_active(new_world):
    shadow = new_world(statements="shadow")
    with refused("authority_not_active"):
        shadow.paste(batch(three_lines()))
    assert shadow.rows("os_proposals") == []


# ---- the pepper pin -------------------------------------------------------------------------


def test_an_unpinned_pepper_refuses_and_writes_nothing(new_world, caplog):
    world = new_world(pin=False)
    with caplog.at_level("INFO"):
        with refused("ledger_unavailable"):
            world.paste(batch(three_lines()))
    assert "reason=statement_pepper_unpinned" in caplog.text
    assert world.rows("os_proposals") == []
    assert not any(world.tables().values())


def test_a_mismatched_pepper_refuses_and_writes_nothing(world, caplog):
    world.config.os_statement_pepper = "a-different-synthetic-pepper-0123456789abcdef"
    with caplog.at_level("INFO"):
        with refused("ledger_unavailable"):
            world.paste(batch(three_lines()))
    assert "reason=statement_pepper_mismatch" in caplog.text
    assert world.rows("os_proposals") == []


def test_no_configured_pepper_refuses(new_world, caplog):
    world = new_world(os_statement_pepper="")
    with caplog.at_level("INFO"):
        with refused("ledger_unavailable"):
            world.paste(batch(three_lines()))
    assert "reason=statement_pepper_unset" in caplog.text


# ---- FORK-SUPERSEDE -------------------------------------------------------------------------


def test_a_second_owner_paste_supersedes_the_first(world):
    first = world.paste(batch(three_lines(), number=1))
    version = world.version()
    second = world.paste(batch(three_lines() + [line("NEW SHOP", 321)], number=2))
    assert second.statement_id == first.statement_id
    assert second.superseded_proposal_id == first.proposal_id and second.conflict is False
    old = next(p for p in world.rows("os_proposals") if p["proposal_id"] == first.proposal_id)
    assert old["status"] == "superseded"
    receipt = next(r for r in world.rows("os_receipts") if r["proposal_id"] == first.proposal_id)
    assert (receipt["status"], receipt["kind"], receipt["request_id"]) == (
        "superseded", "statement_batch", old["rejection_id"])
    assert world.version() == version + 1
    # The older batch's personal detail is purged at once.
    kept = world.tables()["batch_lines"]
    assert all(row.detail is None and row.purged_at is not None
               for key, row in kept.items() if key[0] == first.proposal_id)
    assert all(row.detail is not None for key, row in kept.items() if key[0] == second.proposal_id)
    assert world.tables()["statements"][first.statement_id].current_proposal_id == (
        second.proposal_id)


def test_the_same_credential_supersedes_its_own_older_batch(world):
    world.credential()
    first = world.paste(batch(three_lines(), number=1), ctx=world.assistant(), source=CLAUDE)
    second = world.paste(batch(three_lines(), number=2), ctx=world.assistant(), source=CLAUDE)
    assert second.superseded_proposal_id == first.proposal_id and second.conflict is False


def test_an_older_batch_with_a_saved_draft_is_never_discarded(world):
    first = world.paste(batch(three_lines(), number=1))
    world.tag_all(first.proposal_id)
    key = world.lines(first.proposal_id)[0]["line_key"]
    world.draft(first.proposal_id, [{"line_key": key, "tag": "personal"}])
    second = world.paste(batch(three_lines(), number=2))
    assert second.conflict is True and second.superseded_proposal_id is None
    statuses = {p["proposal_id"]: p["status"] for p in world.rows("os_proposals")}
    assert statuses == {first.proposal_id: "pending", second.proposal_id: "pending"}
    assert world.review(second.proposal_id)["conflict_with"] == [first.proposal_id]
    assert world.review(first.proposal_id)["conflict_with"] == [second.proposal_id]
    # The kept batch still has its detail.
    assert all(row.detail is not None for key, row in world.tables()["batch_lines"].items()
               if key[0] == first.proposal_id)


def test_a_different_source_is_a_conflict_and_both_stay_reviewable(world):
    world.credential()
    first = world.paste(batch(three_lines(), number=1))
    second = world.paste(batch(three_lines(), number=2), ctx=world.assistant(), source=CLAUDE)
    assert second.conflict is True and second.superseded_proposal_id is None
    assert {p["status"] for p in world.rows("os_proposals")} == {"pending"}
    assert world.review(first.proposal_id)["conflict_with"] == [second.proposal_id]


def test_two_credentials_conflict(world):
    world.credential(CRED)
    world.credential(CRED_B, label="GPT custom action")
    first = world.paste(batch(three_lines(), number=1), ctx=world.assistant(), source=CLAUDE)
    second = world.paste(batch(three_lines(), number=2),
                         ctx=world.assistant("gpt", CRED_B), source=GPT)
    assert second.conflict is True
    assert world.review(second.proposal_id)["conflict_with"] == [first.proposal_id]


# ---- personal rules at submit ---------------------------------------------------------------


def test_the_summary_counts_unassigned_lines(world):
    submitted = world.paste(batch([line("MYSTERY", 100, tag=None), line("SHOP", 200)]))
    assert submitted.summary["unassigned_count"] == 1
    assert submitted.summary["business_count"] == 1
