from typing import Protocol

from pydantic import BaseModel, Field


class OutboundMessage(BaseModel):
    conversation_id: str
    text: str
    channel: str
    idempotency_key: str
    reply_to_id: str | None = None


class MessagePort(Protocol):
    """Typed outbound port. Channel adapters implement this. Graph never imports SDKs."""

    async def send(self, message: OutboundMessage) -> None: ...


class DisabledMessagePort:
    """Wired default until a real adapter is attached. Refuses silent sends."""

    async def send(self, message: OutboundMessage) -> None:
        raise RuntimeError(
            f"{message.channel} sender is not alive; message {message.idempotency_key} not sent"
        )


class RecordingMessagePort:
    """Test double that records outbound messages without calling a provider."""

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)


class InboundPayload(BaseModel):
    channel: str
    provider_event_id: str
    raw: dict = Field(default_factory=dict)
