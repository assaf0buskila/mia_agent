"""V2-37 golden shapes: the owner's request/collect bodies and the bearer bodies.

Each shape builds its own deterministic world (a FakeLedger with a frozen clock, counter-made ids, a
fixed synthetic peppers and counter-made keys) and returns the body
the real services and the routes' own body builders produced. V2-20's
``scripts/export_os_contract_fixtures.py`` registry loads this module and writes
``tests/contract/golden/v2-37/<shape>.json``; until V2-20 merges,
``tests/contract/test_v2_37_goldens.py`` pins the same files byte for byte. Nothing here is
hand-written: a body the services cannot build does not belong here. The one secret in these files
is the synthetic key of ``key_issued``.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from app.api.assistant.os import context_body, status_body, submitted_body
from app.services import assistant_keys as keys
from app.services.assistant_issue import AssistantIssueService
from tests.fakes.os_ledger_env import NOW
from tests.services.os_ledger.assistant.support import IP_A, ORIGIN, AWorld
from tests.services.os_ledger.statements.support import (
    CLAUDE,
    World,
    batch,
    three_lines,
)

WEEK = 7 * 24 * 3600


def _world() -> AWorld:
    """An assistant world whose keys and ids are counters, and whose clock is the ledger's."""
    aw = AWorld("fake")
    ids, secrets = itertools.count(1), itertools.count(1)
    aw.service = AssistantIssueService(
        aw.w.ledger, aw.w.config, aw.statements,
        revoke_watermark=lambda owner: None,
        new_id=lambda: f"AC_{next(ids):032x}",
        new_secret=lambda: "aosp_" + f"S{next(secrets):042d}")
    aw.clock.value = NOW
    return aw


def _use(aw: AWorld, issued: dict[str, Any], times: int) -> None:
    """``times`` valid calls of a key (its use count and last-used time move)."""
    stored = aw.store.find(keys.secret_hash(issued["secret"]))
    caller = keys.Caller(stored, aw.w.assistant(issued["kind"], issued["credential_id"]),
                         aw.ip_hmac(IP_A))
    for _ in range(times):
        aw.clock.advance(60)
        aw.gateway.run(caller, "context", lambda facts: {})


# --- the owner's keys ---------------------------------------------------------------------


def keys_list_empty() -> dict:
    return _world().listing()


def _three_keys(aw: AWorld) -> dict[str, Any]:
    used = aw.issue(label="Claude desktop", kind="claude")
    aw.issue(label="ChatGPT", kind="gpt")
    gone = aw.issue(label="Old laptop", kind="claude", days=7)
    _use(aw, used, 3)
    aw.w.advance(3600)  # the ledger clock moves, so the revocation has its own time
    aw.revoke(gone["credential_id"])
    return used


def keys_list() -> dict:
    aw = _world()
    _three_keys(aw)
    return aw.listing()


def key_issued() -> dict:
    return _world().issue(label="Claude desktop", kind="claude")


def key_revoked() -> dict:
    aw = _world()
    first = aw.issue(label="Claude desktop", kind="claude")
    aw.issue(label="ChatGPT", kind="gpt")
    aw.paste_as(first, batch(three_lines()))
    return aw.revoke(first["credential_id"])


def keys_revoked_all() -> dict:
    aw = _world()
    aw.issue(label="Claude desktop", kind="claude")
    aw.issue(label="ChatGPT", kind="gpt")
    return aw.revoke_all()


def issue_requested() -> dict:
    aw = _world()
    # Stable synthetic request identity, produced by the real request writer.
    import unittest.mock

    with unittest.mock.patch(
        "app.services.assistant_issue.secrets.token_hex", return_value="1" * 32
    ):
        return aw.request()


def issue_approved() -> dict:
    aw = _world()
    import unittest.mock

    with unittest.mock.patch(
        "app.services.assistant_issue.secrets.token_hex", return_value="1" * 32
    ):
        request = aw.request()
    aw.decide_issue(request)
    return aw.issue_status(request)


# --- the bearer routes ---------------------------------------------------------------------


def _assistant_world() -> World:
    return World("fake")


def assistant_context() -> dict:
    world = _assistant_world()
    first = world.paste(batch(three_lines()))
    keys_by_name = {i["merchant_display"]: i["line_key"] for i in world.lines(first.proposal_id)}
    world.draft(first.proposal_id, [
        {"line_key": keys_by_name["DEMO SHOP"], "tag": "business", "category": "Tools",
         "is_subscription": True, "make_rule": True},
        {"line_key": keys_by_name["PERSONAL CAFE"], "tag": "personal", "make_rule": True}])
    world.approve(first.proposal_id)
    world.paste(batch(three_lines(), number=2, label="Second Synthetic Card"))
    return context_body(world.service.context_for_assistant(world.assistant()))


def assistant_batch_submitted() -> dict:
    world = _assistant_world()
    submitted = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    return submitted_body(ORIGIN, submitted)


def assistant_batch_status() -> dict:
    world = _assistant_world()
    ctx = world.assistant()
    submitted = world.paste(batch(three_lines()), ctx=ctx, source=CLAUDE)
    return status_body(world.service.status_for_credential(ctx, submitted.proposal_id))


def assistant_batch_status_rejected() -> dict:
    world = _assistant_world()
    ctx = world.assistant()
    submitted = world.paste(batch(three_lines()), ctx=ctx, source=CLAUDE)
    world.reject(submitted.proposal_id)
    return status_body(world.service.status_for_credential(ctx, submitted.proposal_id))


SHAPES: dict[str, Callable[[], dict]] = {
    "keys_list_empty": keys_list_empty,
    "keys_list": keys_list,
    "key_issued": key_issued,
    "key_revoked": key_revoked,
    "keys_revoked_all": keys_revoked_all,
    "issue_requested": issue_requested,
    "issue_approved": issue_approved,
    "assistant_context": assistant_context,
    "assistant_batch_submitted": assistant_batch_submitted,
    "assistant_batch_status": assistant_batch_status,
    "assistant_batch_status_rejected": assistant_batch_status_rejected,
}
