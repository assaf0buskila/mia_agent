"""`website_ping_scope` (`app/domain/handoff/delivery.py`).

Every website Telegram ping deterministically failed in production: `lead_id` was
"site:" (5 chars) plus a canonical UUID4 session id (36 chars, with dashes) = 41
characters, one over `OwnerNotificationRecipientClaimRow.lead_id`'s `String(40)`
column. `try_claim_owner_notification_recipient_compatible` raised `DataError` on
every attempt, and `CrmDeliveryWorker._deliver` did not catch `SQLAlchemyError`, so
that one uncaught exception aborted the entire delivery cycle before the job could be
marked done - blocking every other queued job behind it too, on every 5-second poll,
indefinitely. Two live-tested leads never reached Telegram because of this.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.db.models import OwnerNotificationRecipientClaimRow
from app.domain.handoff.delivery import website_ping_scope


def _claim_column_length(name: str) -> int:
    column = OwnerNotificationRecipientClaimRow.__table__.columns[name]
    return column.type.length


def test_a_real_session_id_produces_a_lead_id_that_fits_the_real_column() -> None:
    """Checked against the actual schema, not a duplicated constant, so a column
    change can't silently reopen this bug without failing this test."""
    session_id = str(uuid4())
    lead_id, notification_key = website_ping_scope(session_id)
    assert len(lead_id) <= _claim_column_length("lead_id")
    assert len(notification_key) <= _claim_column_length("notification_key")


@pytest.mark.parametrize("session_id", [str(uuid4()) for _ in range(20)])
def test_every_canonical_session_id_fits(session_id: str) -> None:
    lead_id, _ = website_ping_scope(session_id)
    assert len(lead_id) <= _claim_column_length("lead_id")


def test_the_scope_is_stable_and_distinct_per_session() -> None:
    """The claim key must be reproducible across retries and unique per conversation -
    both the retry logic and the primary key depend on this."""
    a, b = str(uuid4()), str(uuid4())
    assert website_ping_scope(a) == website_ping_scope(a)
    assert website_ping_scope(a) != website_ping_scope(b)


def test_the_lead_id_still_carries_the_website_scope_prefix() -> None:
    """The prefix is what keeps a website conversation's claim from ever colliding
    with a real numeric owner-flow lead id (ADR-049: the website never mints a lead)."""
    lead_id, _ = website_ping_scope(str(uuid4()))
    assert lead_id.startswith("site:")
