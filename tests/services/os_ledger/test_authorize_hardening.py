"""V2-19 review r1 (#182): in-process forgery of a minted context or a mutation proof is refused.

``authorize`` remembers every context it mints (by identity, with a snapshot of its fields), and
``require`` refuses anything else: a ``dataclasses.replace`` copy, an ``object.__setattr__`` edit,
a re-construction with the stolen capability, or a copy. A browser mutation proof is bound, once,
to the exact principal ``owner_mutation_scope`` returned, so it cannot be borrowed or widened.
"""

from __future__ import annotations

import copy
import dataclasses
import gc
from datetime import UTC, datetime, timedelta

import pytest
from app.api import owner_sessions
from app.api.owner_sessions import OwnerSessionPrincipal
from app.capabilities.types import Principal
from app.core.config import MiaEnv, Settings
from app.services.os_ledger import api, authorize

from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE

ALL_OS = frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve", "assaf-os:direct"})
DIRECT = frozenset({"assaf-os:direct"})


def settings() -> Settings:
    return Settings(
        _env_file=None, env=MiaEnv.TEST, telegram_owner_user_ids=OWNER_ID, kill_switch=False,
        assaf_os_workspace_id=WORKSPACE, assaf_os_authority_epoch=1, assaf_os_backend="postgres",
        assaf_os_ledger_enabled=True,
    )


def principal(scopes=ALL_OS) -> OwnerSessionPrincipal:
    return OwnerSessionPrincipal(owner_id=OWNER_ID, scopes=frozenset(scopes), session_hash="h" * 64,
                                 expires_at=datetime.now(UTC) + timedelta(minutes=30))


def refused_by_require(ctx, op: str) -> str:
    with pytest.raises(api.LedgerError) as exc:
        authorize.require(ctx, op)
    return exc.value.code


def read_ctx():
    return authorize.from_owner_read(principal(), settings())


def mutation_ctx():
    return authorize.from_owner_mutation(
        owner_sessions._attach_mutation_proof(principal()), settings())


def mia_ctx():
    return authorize.from_mia_tool(Principal.owner(source="telegram", actor_id=OWNER_ID),
                                   settings())


# ---------------------------------------------------------------------------------------------
# Contexts


def test_untouched_minted_contexts_still_pass():
    authorize.require(read_ctx(), "read")
    authorize.require(mutation_ctx(), "commit_direct")
    authorize.require(mia_ctx(), "propose")


@pytest.mark.parametrize("make", [read_ctx, mia_ctx], ids=["os-read", "mia"])
def test_a_replace_copy_upgraded_to_a_verified_direct_context_is_refused(make):
    forged = dataclasses.replace(make(), channel="OS", scopes=DIRECT,
                                 browser_mutation_verified=True)
    for op in ("commit_direct", "undo", "abandon", "read"):
        assert refused_by_require(forged, op) == "invalid_trusted_context"


def test_a_replace_copy_that_only_extends_the_expiry_is_refused():
    later = datetime.now(UTC) + timedelta(days=3650)
    forged = dataclasses.replace(read_ctx(), auth_expires_at=later)
    assert refused_by_require(forged, "read") == "invalid_trusted_context"


def test_an_in_place_setattr_edit_of_a_minted_context_is_refused():
    ctx = read_ctx()
    object.__setattr__(ctx, "browser_mutation_verified", True)
    object.__setattr__(ctx, "scopes", DIRECT)
    assert refused_by_require(ctx, "commit_direct") == "invalid_trusted_context"
    assert refused_by_require(ctx, "read") == "invalid_trusted_context"


def test_a_reconstruction_with_the_stolen_capability_is_refused():
    ctx = read_ctx()
    fields = {f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx)}
    fields |= {"scopes": DIRECT, "browser_mutation_verified": True}
    forged = type(ctx)(**fields)
    assert refused_by_require(forged, "abandon") == "invalid_trusted_context"


def test_an_unchanged_copy_is_refused_too():
    ctx = read_ctx()
    for forged in (copy.copy(ctx), dataclasses.replace(ctx)):
        assert forged is not ctx
        assert refused_by_require(forged, "read") == "invalid_trusted_context"


def test_a_context_minted_outside_authorize_is_refused():
    forged = api._issue(numeric_owner=int(OWNER_ID), workspace=WORKSPACE, authority_epoch=1,
                        channel="OS", scopes=DIRECT, auth_expires_at=datetime.now(UTC)
                        + timedelta(minutes=5), browser_mutation_verified=True, credential_id=None)
    assert refused_by_require(forged, "commit_direct") == "invalid_trusted_context"


def test_the_registry_forgets_a_context_once_it_is_collected():
    ctx = read_ctx()
    key = id(ctx)
    assert key in authorize._MINTED
    del ctx
    gc.collect()
    assert key not in authorize._MINTED


# ---------------------------------------------------------------------------------------------
# The browser mutation proof


def test_a_borrowed_mutation_proof_does_not_verify_another_principal():
    proven = owner_sessions._attach_mutation_proof(principal())
    other = principal(scopes=frozenset({"assaf-os:read", "assaf-os:direct"}))
    borrowed = dataclasses.replace(other, _mutation_proof=proven._mutation_proof)
    with pytest.raises(api.LedgerError) as exc:
        authorize.from_owner_mutation(borrowed, settings())
    assert exc.value.code == "browser_mutation_verification_required"


def test_a_proven_principal_cannot_be_widened_by_replace_or_setattr():
    proven = owner_sessions._attach_mutation_proof(
        principal(scopes=frozenset({"assaf-os:read", "assaf-os:propose"})))
    widened = dataclasses.replace(proven, scopes=ALL_OS)
    with pytest.raises(api.LedgerError) as exc:
        authorize.from_owner_mutation(widened, settings())
    assert exc.value.code == "browser_mutation_verification_required"
    object.__setattr__(proven, "scopes", ALL_OS)
    with pytest.raises(api.LedgerError) as exc:
        authorize.from_owner_mutation(proven, settings())
    assert exc.value.code == "browser_mutation_verification_required"


def test_a_proof_is_bound_once():
    proven = owner_sessions._attach_mutation_proof(principal())
    assert owner_sessions._mutation_proven(proven)
    other = principal()
    assert not owner_sessions._mutation_proven(
        dataclasses.replace(other, _mutation_proof=proven._mutation_proof))
    with pytest.raises(RuntimeError):
        proven._mutation_proof._bind(other)


def test_a_principal_without_a_proof_or_with_a_foreign_object_is_not_proven():
    assert not owner_sessions._mutation_proven(principal())
    assert not owner_sessions._mutation_proven(
        dataclasses.replace(principal(), _mutation_proof=object()))
