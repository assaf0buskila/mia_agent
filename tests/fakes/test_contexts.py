"""The test-only context issuer (V2-16f). Its contexts are not authentication evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from app.services.os_ledger import api

from tests.fakes.contexts import for_worker, make_ctx


def test_make_ctx_issues_a_trusted_context_through_the_capability():
    context = make_ctx()
    assert isinstance(context, api.TrustedOwnerContext)
    assert (context.numeric_owner, context.workspace, context.authority_epoch) == (123, "test", 1)
    assert context.channel == "OS" and context.browser_mutation_verified
    assert context.credential_id is None and context.auth_expires_at > datetime.now(UTC)
    assert {"assaf-os:read", "assaf-os:propose", "assaf-os:approve", "assaf-os:direct"} <= (
        context.scopes)


def test_make_ctx_fields_are_settable():
    context = make_ctx("MIA", owner=7, workspace="w", epoch=3, scopes={"x"},
                       browser_mutation_verified=False, credential_id="AC_" + "0" * 32,
                       expires_in=60)
    assert (context.channel, context.numeric_owner, context.workspace) == ("MIA", 7, "w")
    assert context.authority_epoch == 3 and context.scopes == frozenset({"x"})
    assert not context.browser_mutation_verified and context.credential_id.startswith("AC_")
    assert context.auth_expires_at < datetime.now(UTC) + timedelta(seconds=61)


def test_for_worker_is_the_worker_channel_with_the_job_as_scope():
    context = for_worker("retention", workspace="w", epoch=2)
    assert context.channel == "WORKER" and context.scopes == frozenset({"retention"})
    assert (context.workspace, context.authority_epoch) == ("w", 2)


def test_a_context_cannot_be_built_without_the_capability():
    with pytest.raises(api.LedgerError) as caught:
        api.TrustedOwnerContext(
            numeric_owner=1, workspace="w", authority_epoch=1, channel="OS", scopes=frozenset(),
            auth_expires_at=datetime.now(UTC), browser_mutation_verified=True, credential_id=None,
            _capability=object())
    assert caught.value.code == "invalid_trusted_context"
