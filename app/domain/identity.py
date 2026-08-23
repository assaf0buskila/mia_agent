from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.errors import MergeRejected, PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.events import Channel

REASON_HANDOFF_TOKEN = "handoff_token"
ALLOWLISTED_LINK_REASONS = frozenset({REASON_HANDOFF_TOKEN})


class ChannelIdentity(BaseModel):
    channel: Channel
    external_id: str
    verified: bool = False


class Customer(BaseModel):
    customer_id: str
    identities: list[ChannelIdentity] = Field(default_factory=list)


def _key(channel: Channel, external_id: str) -> tuple[str, str]:
    return (channel.value, external_id)


class IdentityIndex:
    """In-memory identity map. Exact channel IDs reuse a customer. Merges require verification."""

    def __init__(self) -> None:
        self._customers: dict[str, Customer] = {}
        self._index: dict[tuple[str, str], str] = {}

    def observe(self, identity: ChannelIdentity) -> Customer:
        existing = self._index.get(_key(identity.channel, identity.external_id))
        if existing:
            customer = self._customers[existing]
            self._refresh(customer, identity)
            return customer
        customer = Customer(customer_id=f"cust_{uuid4().hex[:12]}", identities=[identity])
        self._customers[customer.customer_id] = customer
        self._index[_key(identity.channel, identity.external_id)] = customer.customer_id
        return customer

    def merge(self, left_id: str, right_id: str, *, verified: bool) -> Customer:
        if not verified:
            raise MergeRejected("never merge identities on weak similarity")
        if left_id == right_id:
            return self._customers[left_id]
        left = self._customers[left_id]
        right = self._customers[right_id]
        for identity in right.identities:
            left.identities.append(identity)
            self._index[_key(identity.channel, identity.external_id)] = left.customer_id
        del self._customers[right_id]
        return left

    def _refresh(self, customer: Customer, identity: ChannelIdentity) -> None:
        for item in customer.identities:
            if item.channel == identity.channel and item.external_id == identity.external_id:
                item.verified = item.verified or identity.verified
                return
        customer.identities.append(identity)


def persist_verified_identity_link(
    store,
    *,
    customer_id: str,
    channel: Channel,
    external_id: str,
    reason: str,
) -> bool:
    """Persist one verified identity link. First write wins; never merges customers."""
    if reason not in ALLOWLISTED_LINK_REASONS:
        return False
    try:
        assert_allowed(
            RiskAction(name="identity_link_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=False,
        )
    except PolicyDenied:
        return False
    identity = store.get_channel_identity(channel=channel.value, external_id=external_id)
    if identity is None:
        return False
    if identity.customer_id != customer_id:
        return False
    return store.save_identity_link(
        identity_id=identity.id,
        customer_id=customer_id,
        reason=reason,
    )
