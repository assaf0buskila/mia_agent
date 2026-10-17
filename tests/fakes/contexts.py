"""Test-only issuer. These contexts are not authentication evidence."""

from datetime import UTC, datetime, timedelta

from app.services.os_ledger import api


def make_ctx(
    channel="OS", *, owner=123, workspace="test", epoch=1,
    scopes=frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve", "assaf-os:direct"}),
    browser_mutation_verified=True, credential_id=None, expires_in=3600,
):
    return api._issue(
        numeric_owner=owner, workspace=workspace, authority_epoch=epoch, channel=channel,
        scopes=frozenset(scopes), auth_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        browser_mutation_verified=browser_mutation_verified, credential_id=credential_id,
    )


def for_worker(purpose="retention", **fields):
    return make_ctx("WORKER", scopes=frozenset({purpose}), **fields)
