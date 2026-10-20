"""V2-28: Telegram cards for ledger proposals (V2_PG_CONTRACTS.md section 13, "Cards").

Cards are a compatibility projection of the proposal's immutable mutations (promote_lead too),
stay within 3,800 characters, and compare through one byte-stable proposal view. A statement
batch never gets a card or a button, only a link notice.
"""

from __future__ import annotations

import re
from dataclasses import replace

from app.domain.owner.proposal_cards import (
    LEDGER_PROPOSAL_API,
    assaf_os_proposal_is_reviewable,
    ledger_proposal_body,
    proposal_view_bytes,
    render_ledger_batch_notice,
    render_owner_proposal_card,
)
from app.integrations.telegram_format import esc

from tests.services.os_v2_28_support import LedgerWorld

_LEAD_INPUT = ("name", "phone", "email", "path", "status", "source_note")


def _envelope(body: dict) -> dict:
    return {
        "kind": "assaf_os.approve",
        "version": 1,
        "parameters": {
            "proposal_id": body["proposal_id"],
            "request_id": body["approval_request_id"],
            "rejection_request_id": body["rejection_request_id"],
        },
        "target": {"proposal": body, "approval_guard_version": 1},
    }


def test_a_lead_card_is_projected_from_its_mutation():
    world = LedgerWorld()
    row = world.propose_lead()
    (mutation,) = row.mutations
    body = ledger_proposal_body(row)
    assert body["api_version"] == LEDGER_PROPOSAL_API
    assert body["before"] is None and body["after"] == dict(mutation.after)
    assert body["input"] == {key: mutation.after[key] for key in _LEAD_INPUT}
    assert body["approval_request_id"] == row.approval_id
    card = render_owner_proposal_card(_envelope(body))
    for key in _LEAD_INPUT:
        value = str(mutation.after[key])
        if value:
            assert esc(value) in card
    assert "Assaf OS" in card and "עדיין לא בוצע." in card
    assert assaf_os_proposal_is_reviewable(_envelope(body))
    assert len(card) <= 3_800


def test_a_promote_lead_card_is_projected_from_both_mutations():
    world = LedgerWorld()
    lead = world.commit_lead(source_id="promote-source")
    record = lead.mutations[0].after
    from app.services.os_ledger import api

    promotion = world.ledger.propose(
        world.ctx("MIA"),
        api.ProposeInput(
            request_id=api.mia_request_uuid("123456", "test", "promote"),
            entity="promote_lead",
            record_id=lead.record_id,
            expected_revision=1,
            input={},
        ),
    )
    by_entity = {m.entity: m for m in promotion.mutations}
    body = ledger_proposal_body(promotion)
    assert body["entity"] == "promote_lead" and body["input"] == {}
    assert body["after"] == {
        "lead": dict(by_entity["leads"].after),
        "client": dict(by_entity["clients"].after),
    }
    assert body["before"] == {"lead": dict(by_entity["leads"].before), "client": None}
    card = render_owner_proposal_card(_envelope(body))
    assert esc(record["name"]) in card
    assert record["person_id"] in card
    assert assaf_os_proposal_is_reviewable(_envelope(body))


def test_an_oversized_card_is_never_offered_for_approval():
    world = LedgerWorld()
    row = world.propose_lead(source_note="ש" * 2_000, name="נ" * 2_000)
    body = ledger_proposal_body(row)
    assert len(render_owner_proposal_card(_envelope(body))) > 3_800
    assert not assaf_os_proposal_is_reviewable(_envelope(body))


def test_a_batch_or_direct_kind_never_gets_an_approval_card():
    world = LedgerWorld()
    body = ledger_proposal_body(world.propose_lead())
    for kind in ("statement_batch", "owner_direct", "owner_undo", "import"):
        assert not assaf_os_proposal_is_reviewable(_envelope({**body, "kind": kind}))


def test_a_batch_only_ever_gets_a_link_notice_without_a_button():
    notice = render_ledger_batch_notice(origin="https://www.assafweb.com")
    assert "https://www.assafweb.com" in notice
    assert not re.search(r"\b(ok|no|lp):", notice)
    assert "callback_data" not in notice and "inline_keyboard" not in notice
    hostile = render_ledger_batch_notice(origin='javascript:alert("x")')
    assert "javascript" not in hostile


def test_the_proposal_view_is_byte_stable():
    world = LedgerWorld()
    row = world.propose_lead()
    body = ledger_proposal_body(row)
    reordered = dict(reversed(list(body.items())))
    decided = ledger_proposal_body(replace(row, status="committed"))
    plus_zero = {**body, "expires_at": body["expires_at"].replace("Z", "+00:00")}
    v2_20_shape = {**body, "source": "postgresql", "input": {}, "authority_epoch": 7}
    reference = proposal_view_bytes(body)
    assert isinstance(reference, bytes)
    for variant in (reordered, decided, plus_zero, v2_20_shape):
        assert proposal_view_bytes(variant) == reference
    changed = {**body, "after": {**body["after"], "name": "אחר"}}
    assert proposal_view_bytes(changed) != reference
