"""Deterministic request ids: a UUIDv5 MIA, paste or assistant id replays idempotently (§5.1)."""

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import TASK_OPEN, counts, ctx, rid, workspace_row
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE

BODY = {"title": "From Mia", "status": TASK_OPEN}


def mia_request(source_id="tool-call-1", body=BODY):
    request_id = api.mia_request_uuid(OWNER_ID, WORKSPACE, source_id)
    return api.ProposeInput(request_id, "tasks", None, 0, dict(body))


def test_a_mia_uuid_v5_request_replays_to_the_stored_proposal(ledger):
    request = mia_request()
    first = ledger.propose(ctx("MIA"), request)
    again = ledger.propose(ctx("MIA"), request)
    assert again == first and first.source == "MIA"
    assert len(ledger.rows("os_proposals")) == 1
    assert workspace_row(ledger)["pending_seq"] == 1


def test_a_replay_after_the_decision_returns_the_stored_proposal_whatever_its_status(ledger):
    request = mia_request()
    first = ledger.propose(ctx("MIA"), request)
    ledger.decide(ctx(), first.proposal_id, first.approval_id, "approve")
    before = counts(ledger)
    replay = ledger.propose(ctx("MIA"), request)
    assert replay.proposal_id == first.proposal_id and replay.status == "committed"
    assert counts(ledger) == before


def test_the_same_mia_id_with_a_different_body_or_source_is_idempotency_mismatch(ledger):
    request = mia_request()
    ledger.propose(ctx("MIA"), request)
    other_body = api.ProposeInput(request.request_id, "tasks", None, 0,
                                  {"title": "Different", "status": TASK_OPEN})
    with pytest.raises(api.LedgerError, match="^idempotency_mismatch$"):
        ledger.propose(ctx("MIA"), other_body)
    with pytest.raises(api.LedgerError, match="^idempotency_mismatch$"):
        ledger.propose(ctx("OS"), request)  # the digest binds the source channel
    assert len(ledger.rows("os_proposals")) == 1


def test_a_different_source_id_is_a_different_request(ledger):
    first = ledger.propose(ctx("MIA"), mia_request("tool-call-1"))
    second = ledger.propose(ctx("MIA"), mia_request("tool-call-2"))
    assert first.proposal_id != second.proposal_id and first.request_id != second.request_id
    assert len(ledger.rows("os_proposals")) == 2


def test_the_request_id_helpers_are_deterministic_and_do_not_collide():
    ids = {
        api.mia_request_uuid("1", "w", "s"), api.paste_request_uuid("1", "s"),
        api.assistant_request_uuid("AC_" + "0" * 32, "s"), api.import_request_uuid(1, "d" * 64),
        api.import_request_uuid(2, "d" * 64),
    }
    assert len(ids) == 5
    assert api.mia_request_uuid("1", "w", "s") == api.mia_request_uuid("1", "w", "s")
    for value in ids:
        assert api.canonical_uuid(value) == value


def test_a_server_minted_id_is_not_needed_to_replay(ledger):
    fresh = rid()
    request = api.ProposeInput(fresh, "tasks", None, 0, dict(BODY))
    assert ledger.propose(ctx("OS"), request) == ledger.propose(ctx("OS"), request)
