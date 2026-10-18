"""V2-19 (V2_PG_CONTRACTS.md §9): the channel x operation matrix enforced by ``authorize.require``.

Every one of the 15 x 6 cells is enumerated with the exact §9 refusal code. The expected table is
restated here from §9, never imported from the implementation, so a drift in either one fails.
Conditional cells (a scope, a verified browser mutation, a worker job) are also checked with the
condition missing. The constructors and the ledger end-to-end cases live in
``test_authorize_constructors.py``.
"""

from __future__ import annotations

import dataclasses
import typing
from datetime import UTC, datetime, timedelta

import pytest
from app.services.os_ledger import api, authorize

from tests.fakes.contexts import make_ctx as fake_ctx


def make_ctx(*args, **kwargs) -> api.TrustedOwnerContext:
    # require() accepts only contexts authorize registered (review r1); register the fakes.
    return authorize._register(fake_ctx(*args, **kwargs))

CHANNELS = ("OS", "MIA", "TELEGRAM_APPROVAL", "ASSISTANT_CLAUDE", "ASSISTANT_GPT", "WORKER")
OPERATIONS = typing.get_args(api.Operation)
JOBS = ("import", "mirror", "verifier", "authority", "retention")
DENY = "channel_operation_denied"
OK = None
CREDENTIAL = "AC_" + "a" * 32

READ, PROPOSE, APPROVE, DIRECT = (
    "assaf-os:read", "assaf-os:propose", "assaf-os:approve", "assaf-os:direct")
OS_SCOPES = frozenset({READ, PROPOSE, APPROVE, DIRECT})
ASSISTANT_SCOPES = frozenset({"merchant_rules:read", "own_proposals:read", "statement:propose"})

# §9, restated. OS: (required scope, needs a verified browser mutation) or a refusal code.
OS_CELLS: dict[str, tuple[str, bool] | str] = {
    "read": (READ, False),
    "read_rules": (READ, False),
    "read_own_batches": (READ, False),
    "propose": (PROPOSE, True),
    "propose_statement_batch": (PROPOSE, True),
    "decide": (APPROVE, True),
    "decide_statement_batch": (APPROVE, True),
    "draft_statement_batch": (APPROVE, True),
    "commit_direct": (DIRECT, True),
    "undo": (DIRECT, True),
    "abandon": (DIRECT, True),
    "manage_credentials": (APPROVE, True),
    "import_promote": DENY,
    "authority_admin": DENY,
    "expire_batches": DENY,
}
MIA_CELLS = {op: DENY for op in OPERATIONS} | {
    "read": OK, "propose": OK, "decide": "owner_approval_required"}
TELEGRAM_CELLS = {op: DENY for op in OPERATIONS} | {"decide": OK}
# ASSISTANT_*: the op's fixed credential scope, or a refusal code.
ASSISTANT_CELLS: dict[str, str | None] = {op: DENY for op in OPERATIONS} | {
    "read_rules": "merchant_rules:read",
    "read_own_batches": "own_proposals:read",
    "propose_statement_batch": "statement:propose",
}
# WORKER: the jobs that may run the op; every other op is denied to the channel.
WORKER_CELLS: dict[str, frozenset[str]] = {
    "read": frozenset({"verifier", "mirror"}),
    "import_promote": frozenset({"import"}),
    "authority_admin": frozenset({"authority"}),
    "expire_batches": frozenset({"retention"}),
}


def os_ctx(*, scopes=OS_SCOPES, verified=True, **fields):
    return make_ctx("OS", scopes=scopes, browser_mutation_verified=verified, **fields)


def channel_ctx(channel: str, *, job: str = "verifier", **fields):
    """A well-formed context of ``channel``, holding everything that channel can hold."""
    if channel == "OS":
        return os_ctx(**fields)
    if channel == "WORKER":
        return make_ctx("WORKER", scopes={job}, browser_mutation_verified=False, **fields)
    if channel.startswith("ASSISTANT_"):
        return make_ctx(channel, scopes=ASSISTANT_SCOPES, browser_mutation_verified=False,
                        credential_id=CREDENTIAL, **fields)
    return make_ctx(channel, scopes=frozenset(), browser_mutation_verified=False, **fields)


def malformed(ctx, op) -> str | None:
    """``code:reason`` for a REGISTERED copy, so require's well-formedness checks are what refuse it
    (an unregistered copy would stop at context_not_issued first; review r2)."""
    try:
        authorize.require(authorize._register(ctx), op)
    except api.LedgerError as exc:
        assert exc.write_outcome == "not_started"
        return f"{exc.code}:{exc.reason}"
    return None


MALFORMED = "invalid_trusted_context:context_malformed"


def refusal(ctx, op) -> str | None:
    try:
        authorize.require(ctx, op)
    except api.LedgerError as exc:
        assert exc.write_outcome == "not_started"
        return exc.code
    return None


def expected_full(channel: str, op: str, job: str = "verifier") -> str | None:
    """The cell's outcome for a context holding every scope, job and verification it can."""
    if channel == "OS":
        return OK if isinstance(OS_CELLS[op], tuple) else OS_CELLS[op]
    if channel == "MIA":
        return MIA_CELLS[op]
    if channel == "TELEGRAM_APPROVAL":
        return TELEGRAM_CELLS[op]
    if channel.startswith("ASSISTANT_"):
        return OK if ASSISTANT_CELLS[op] != DENY else DENY
    jobs = WORKER_CELLS.get(op)
    if jobs is None:
        return DENY
    return OK if job in jobs else "scope_denied"


def test_the_matrix_covers_every_channel_and_operation_of_the_frozen_api():
    assert typing.get_args(api.Channel) == CHANNELS
    assert len(OPERATIONS) == 15
    assert set(OS_CELLS) == set(MIA_CELLS) == set(TELEGRAM_CELLS) == set(ASSISTANT_CELLS) == set(
        OPERATIONS)
    assert set(WORKER_CELLS) <= set(OPERATIONS)


@pytest.mark.parametrize("op", OPERATIONS)
@pytest.mark.parametrize("channel", CHANNELS)
def test_every_cell_of_the_matrix(channel, op):
    """All 90 cells, each with its exact §9 code (worker cells with the job that may run them)."""
    jobs = WORKER_CELLS.get(op, frozenset({"verifier"}))
    job = sorted(jobs)[0]
    assert refusal(channel_ctx(channel, job=job), op) == expected_full(channel, op, job)


@pytest.mark.parametrize("job", JOBS)
@pytest.mark.parametrize("op", OPERATIONS)
def test_every_worker_cell_for_every_job(op, job):
    assert refusal(channel_ctx("WORKER", job=job), op) == expected_full("WORKER", op, job)


@pytest.mark.parametrize("op", [op for op, cell in OS_CELLS.items() if isinstance(cell, tuple)])
def test_an_os_cell_without_its_scope_is_scope_denied(op):
    scope, _ = OS_CELLS[op]
    assert refusal(os_ctx(scopes=OS_SCOPES - {scope}), op) == "scope_denied"
    assert refusal(os_ctx(scopes=frozenset()), op) == "scope_denied"


@pytest.mark.parametrize("op", [op for op, cell in OS_CELLS.items() if isinstance(cell, tuple)])
def test_an_os_mutation_cell_needs_the_verified_browser_mutation(op):
    _, mutation = OS_CELLS[op]
    expected = "browser_mutation_verification_required" if mutation else OK
    assert refusal(os_ctx(verified=False), op) == expected


@pytest.mark.parametrize("op", ["commit_direct", "undo", "abandon"])
def test_owner_direct_is_os_only_with_the_direct_scope_and_the_mutation_proof(op):
    assert refusal(os_ctx(scopes={DIRECT}), op) is OK
    assert refusal(os_ctx(scopes=OS_SCOPES - {DIRECT}), op) == "scope_denied"
    assert refusal(os_ctx(scopes={DIRECT}, verified=False), op) == (
        "browser_mutation_verification_required")
    for channel in CHANNELS[1:]:
        for job in JOBS:
            assert refusal(channel_ctx(channel, job=job), op) == DENY


@pytest.mark.parametrize("channel", ["ASSISTANT_CLAUDE", "ASSISTANT_GPT"])
@pytest.mark.parametrize("op", [op for op, cell in ASSISTANT_CELLS.items() if cell != DENY])
def test_an_assistant_cell_needs_its_fixed_credential_scope(channel, op):
    scope = ASSISTANT_CELLS[op]
    ctx = make_ctx(channel, scopes=ASSISTANT_SCOPES - {scope}, browser_mutation_verified=False,
                   credential_id=CREDENTIAL)
    assert refusal(ctx, op) == "scope_denied"


def test_the_named_examples_of_the_brief():
    assert refusal(channel_ctx("MIA"), "decide") == "owner_approval_required"
    assert refusal(channel_ctx("TELEGRAM_APPROVAL"), "read") == DENY
    assert refusal(channel_ctx("TELEGRAM_APPROVAL"), "propose") == DENY
    assert refusal(channel_ctx("TELEGRAM_APPROVAL"), "decide") is OK
    assert refusal(channel_ctx("ASSISTANT_CLAUDE"), "decide") == DENY
    assert refusal(channel_ctx("ASSISTANT_GPT"), "propose") == DENY


# ---------------------------------------------------------------------------------------------
# The context itself is re-validated on every require (I6, I12, §5.1).


def forged(**fields):
    """A TrustedOwnerContext built without the capability, bypassing ``__post_init__``."""
    ctx = object.__new__(api.TrustedOwnerContext)
    good = dataclasses.asdict(os_ctx()) | fields
    for name, value in good.items():
        object.__setattr__(ctx, name, value)
    return ctx


def test_a_context_without_the_capability_is_refused_everywhere():
    for capability in (object(), None):
        with pytest.raises(api.LedgerError) as exc:
            api.TrustedOwnerContext(123, "test", 1, "OS", OS_SCOPES, datetime.now(UTC) +
                                    timedelta(hours=1), True, None, capability)
        assert exc.value.code == "invalid_trusted_context"
        assert refusal(forged(_capability=capability), "read") == "invalid_trusted_context"


@pytest.mark.parametrize("value", [object(), None, "OS", {"channel": "OS"}])
def test_require_refuses_anything_that_is_not_a_trusted_context(value):
    assert refusal(value, "read") == "invalid_trusted_context"


@pytest.mark.parametrize(
    "fields",
    [
        {"channel": "ADMIN"},
        {"numeric_owner": "123"},
        {"numeric_owner": True},
        {"numeric_owner": 0},
        {"numeric_owner": -5},
        {"workspace": ""},
        {"workspace": "bad workspace"},
        {"authority_epoch": 0},
        {"authority_epoch": "1"},
        {"scopes": {READ}},
        {"browser_mutation_verified": 1},
        {"auth_expires_at": datetime(2030, 1, 1)},
        {"credential_id": CREDENTIAL},
    ],
    ids=lambda fields: next(iter(fields)),
)
def test_a_malformed_os_context_is_invalid(fields):
    # A registered replace() copy keeps the capability: only the well-formedness checks refuse it.
    ctx = dataclasses.replace(os_ctx(), **fields)
    assert malformed(ctx, "read") == MALFORMED


@pytest.mark.parametrize("channel", CHANNELS[1:])
def test_only_an_os_context_may_claim_a_verified_browser_mutation(channel):
    ctx = dataclasses.replace(channel_ctx(channel), browser_mutation_verified=True)
    for op in OPERATIONS:
        assert malformed(ctx, op) == MALFORMED


@pytest.mark.parametrize("channel", ["ASSISTANT_CLAUDE", "ASSISTANT_GPT"])
@pytest.mark.parametrize("credential_id", [None, "", "AC_short", "ac_" + "a" * 32])
def test_an_assistant_context_needs_a_well_formed_credential_id(channel, credential_id):
    ctx = dataclasses.replace(channel_ctx(channel), credential_id=credential_id)
    assert malformed(ctx, "read_rules") == MALFORMED


@pytest.mark.parametrize("scopes", [set(), {"mirror", "verifier"}, {"admin"}])
def test_a_worker_context_holds_exactly_one_known_job(scopes):
    ctx = dataclasses.replace(channel_ctx("WORKER"), scopes=frozenset(scopes))
    assert malformed(ctx, "read") == MALFORMED


@pytest.mark.parametrize("channel", CHANNELS)
def test_an_expired_context_is_refused(channel):
    # Built expired (a replace copy is refused as not minted since review r1).
    ctx = channel_ctx(channel, expires_in=-1)
    expected = ("assistant_auth_required" if channel.startswith("ASSISTANT_")
                else "owner_auth_required")
    for op in OPERATIONS:
        assert refusal(ctx, op) == expected


def test_an_unknown_operation_is_a_programming_error():
    with pytest.raises(ValueError):
        authorize.require(os_ctx(), "delete_everything")
