"""V2-26 golden shapes: the §12.4 statement bodies, built by the real ``LedgerStatementService``.

Each shape builds its own deterministic world (a FakeLedger with a frozen clock, counter-made ids
and a fixed synthetic pepper) and returns the body the service and the wire renderers produced.
V2-20's ``scripts/export_os_contract_fixtures.py`` registry loads this module and writes
``tests/contract/golden/v2-26/<shape>.json``; until V2-20 merges,
``tests/contract/test_v2_26_goldens.py`` pins the same files byte for byte. Nothing here is
hand-written: a body the service cannot build does not belong here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.os_ledger.statement_wire import batch_created, receipt_v2
from tests.services.os_ledger.statements.support import (
    CLAUDE,
    World,
    batch,
    line,
    three_lines,
)

WEEK = 7 * 24 * 3600


def _month(world: World, cycle: str, lines: list[dict[str, Any]], number: int):
    submitted = world.paste(batch(lines, number=number, charge_date=f"{cycle}-15"))
    world.approve(submitted.proposal_id)
    return submitted


def _subscriptions(prices: tuple[int, int]) -> list[dict[str, Any]]:
    return [line("CLAUDE SUB", prices[0], suggested_category="AI", is_subscription=True),
            line("COFFEE", prices[1], suggested_category="Food"),
            line("MY LUNCH", 777, tag="personal")]


# --- reads ----------------------------------------------------------------------------------


def statement_list() -> dict:
    world = World("fake")
    _month(world, "2026-09", _subscriptions((7500, 300)), 1)
    world.paste(batch(three_lines(), number=2))  # October is awaiting approval
    return world.service.cycles(world.reader)


def statement_view() -> dict:
    world = World("fake")
    _month(world, "2026-09", _subscriptions((7500, 300)), 1)
    second = _month(world, "2026-10", _subscriptions((8000, 600)), 2)
    return world.service.statement_view(world.reader, second.statement_id)


def merchant_rules() -> dict:
    world = World("fake")
    submitted = world.paste(batch(three_lines()))
    keys = {i["merchant_display"]: i["line_key"] for i in world.lines(submitted.proposal_id)}
    world.draft(submitted.proposal_id, [
        {"line_key": keys["DEMO SHOP"], "tag": "business", "category": "Tools",
         "is_subscription": True, "make_rule": True},
        {"line_key": keys["PERSONAL CAFE"], "tag": "personal", "make_rule": True}])
    world.approve(submitted.proposal_id)
    return world.service.merchant_rules(world.reader)


# --- POST /statement-batches ------------------------------------------------------------------


def batch_created_pending() -> dict:
    world = World("fake")
    return batch_created(world.owner, world.paste(batch(three_lines())))


def batch_created_superseded() -> dict:
    world = World("fake")
    world.paste(batch(three_lines(), number=1))
    return batch_created(world.owner, world.paste(
        batch(three_lines() + [line("NEW SHOP", 321)], number=2)))


def batch_created_conflict() -> dict:
    world = World("fake")
    world.credential()
    world.paste(batch(three_lines(), number=1))
    second = world.paste(batch(three_lines(), number=2), ctx=world.assistant(), source=CLAUDE)
    return batch_created(world.assistant(), second)


# --- review, lines, draft ---------------------------------------------------------------------


def _coffee_world() -> tuple[World, Any]:
    world = World("fake")
    world.credential()
    world.hand_expense("EXP_coffee_a", vendor="Coffee Cart", amount=1800, day="2026-09-19")
    world.hand_expense("EXP_coffee_b", vendor="coffee cart", amount=1800, day="2026-09-20")
    submitted = world.paste(
        batch([line("COFFEE CART", 1800, day="2026-09-20"), line("MYSTERY", 450, tag=None),
               line("DEMO SHOP", 10000)]),
        ctx=world.assistant(), source=CLAUDE)
    return world, submitted


def batch_review_blocking() -> dict:
    world, submitted = _coffee_world()
    return world.review(submitted.proposal_id)


def batch_review_ready() -> dict:
    world, submitted = _coffee_world()
    keys = {i["merchant_display"]: i["line_key"] for i in world.lines(submitted.proposal_id)}
    world.draft(submitted.proposal_id, [{"line_key": keys["MYSTERY"], "tag": "business"}],
                {"merge": {keys["COFFEE CART"]: "EXP_coffee_b"}})
    return world.review(submitted.proposal_id)


def batch_review_cycle_blocked() -> dict:
    """§22 E31: the cycle already has a statement, so approve is disabled and the review says so."""
    world = World("fake")
    shop = [line("DEMO SHOP", 10000), line("COFFEE", 300, suggested_category="Food")]
    world.approve(world.paste(batch(shop, number=1, charge_date="2026-10-15")).proposal_id)
    return world.review(world.paste(batch(shop, number=2, charge_date="2026-10-16")).proposal_id)


def batch_review_rejected() -> dict:
    world = World("fake")
    submitted = world.paste(batch(three_lines()))
    world.reject(submitted.proposal_id)
    return world.review(submitted.proposal_id)


def batch_lines_page() -> dict:
    world, submitted = _coffee_world()
    return world.service.lines(world.reader, submitted.proposal_id, after=None, limit=100)


def draft_saved() -> dict:
    world, submitted = _coffee_world()
    keys = {i["merchant_display"]: i["line_key"] for i in world.lines(submitted.proposal_id)}
    return world.draft(submitted.proposal_id,
                       [{"line_key": keys["MYSTERY"], "tag": "business", "category": "Tools"}])


# --- POST approve and reject --------------------------------------------------------------------


def receipt_committed() -> dict:
    world = World("fake")
    submitted = world.paste(batch(three_lines()))
    return receipt_v2(world.owner, world.approve(submitted.proposal_id), world.now())


def receipt_rejected() -> dict:
    world = World("fake")
    submitted = world.paste(batch(three_lines()))
    return receipt_v2(world.owner, world.reject(submitted.proposal_id), world.now())


def receipt_expired() -> dict:
    world = World("fake")
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    world.advance(WEEK + 1)
    result = world.service.approve(world.owner, submitted.proposal_id,
                                   review["approval_request_id"], review["review_digest"])
    return receipt_v2(world.owner, result, world.now())


def receipt_credential_revoked() -> dict:
    world = World("fake")
    world.credential()
    submitted = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    review = world.review(submitted.proposal_id)
    world.revoke()
    result = world.service.approve(world.owner, submitted.proposal_id,
                                   review["approval_request_id"], review["review_digest"])
    return receipt_v2(world.owner, result, world.now())


SHAPES: dict[str, Callable[[], dict]] = {
    "statement_list": statement_list,
    "statement_view": statement_view,
    "merchant_rules": merchant_rules,
    "batch_created_pending": batch_created_pending,
    "batch_created_superseded": batch_created_superseded,
    "batch_created_conflict": batch_created_conflict,
    "batch_review_blocking": batch_review_blocking,
    "batch_review_ready": batch_review_ready,
    "batch_review_cycle_blocked": batch_review_cycle_blocked,
    "batch_review_rejected": batch_review_rejected,
    "batch_lines_page": batch_lines_page,
    "draft_saved": draft_saved,
    "receipt_committed": receipt_committed,
    "receipt_rejected": receipt_rejected,
    "receipt_expired": receipt_expired,
    "receipt_credential_revoked": receipt_credential_revoked,
}
