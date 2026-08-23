from typing import Any, NoReturn

import httpx

from app.core.config import Settings
from app.core.errors import MiaError
from app.domain.ownership_freshness import VALID_INSTAGRAM_SENDERS
from app.domain.tools import AdapterHttpError
from app.integrations.base import DisabledMessagePort, MessagePort, OutboundMessage

_ALLOWED_GRAPH_HOSTS = frozenset({"graph.instagram.com", "graph.facebook.com"})
COMPOSIO_INSTAGRAM_VERSION = "20260819_00"
COMPOSIO_SEND_TEXT_TOOL = "INSTAGRAM_SEND_TEXT_MESSAGE"
COMPOSIO_GET_USER_MEDIA_TOOL = "INSTAGRAM_GET_IG_USER_MEDIA"
COMPOSIO_GET_MEDIA_INSIGHTS_TOOL = "INSTAGRAM_GET_IG_MEDIA_INSIGHTS"
_COMPOSIO_EXECUTE_BASE = "https://backend.composio.dev/api/v3.1/tools/execute"


class InstagramSendError(MiaError):
    code = "instagram_send_failed"
    http_status = 502


def _reraise_classified(exc: AdapterHttpError, *, composio: bool = False) -> NoReturn:
    detail = f": HTTP {exc.status_code}" if exc.status_code is not None else ""
    vendor = "Composio" if composio else "Graph API"
    raise InstagramSendError(f"Instagram {vendor} send failed{detail}") from exc


class InstagramCloudPort:
    """Direct Instagram Graph API adapter for user-initiated DM text sends."""

    def __init__(
        self,
        *,
        access_token: str,
        account_id: str,
        graph_version: str,
        graph_host: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._account_id = account_id
        self._graph_version = graph_version
        self._graph_host = _normalized_graph_host(graph_host)
        self._client = client

    async def send(self, message: OutboundMessage) -> None:
        url = f"https://{self._graph_host}/{self._graph_version}/{self._account_id}/messages"
        body: dict[str, object] = {
            "recipient": {"id": message.conversation_id},
            "message": {"text": message.text},
        }
        if message.reply_to_id:
            body["reply_to"] = {"mid": message.reply_to_id}
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        try:
            try:
                if self._client is not None:
                    response = await self._client.post(url, json=body, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
            if response.status_code >= 400:
                raise AdapterHttpError(response.status_code)
        except AdapterHttpError as exc:
            _reraise_classified(exc)


class ComposioInstagramPort:
    """Composio INSTAGRAM_SEND_TEXT_MESSAGE. Inbound stays Meta webhook (ADR-015)."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        account_id: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._account_id = account_id.strip()
        self._client = client

    async def send(self, message: OutboundMessage) -> None:
        arguments: dict[str, Any] = {
            "recipient_id": message.conversation_id,
            "text": message.text,
        }
        if self._account_id:
            arguments["ig_user_id"] = self._account_id
        if message.reply_to_id:
            arguments["reply_to_message_id"] = message.reply_to_id
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_INSTAGRAM_VERSION,
            "arguments": arguments,
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        url = f"{_COMPOSIO_EXECUTE_BASE}/{COMPOSIO_SEND_TEXT_TOOL}"
        try:
            try:
                if self._client is not None:
                    response = await self._client.post(url, json=payload, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
            if response.status_code >= 400:
                raise AdapterHttpError(response.status_code)
            try:
                body = response.json()
            except ValueError as exc:
                raise AdapterHttpError(response.status_code) from exc
            if not isinstance(body, dict) or body.get("successful") is not True:
                raise AdapterHttpError(400)
        except AdapterHttpError as exc:
            _reraise_classified(exc, composio=True)


def _normalized_graph_host(host: str) -> str:
    cleaned = host.strip().removeprefix("https://").removeprefix("http://").split("/")[0]
    if cleaned not in _ALLOWED_GRAPH_HOSTS:
        raise InstagramSendError("unsupported Instagram graph host")
    return cleaned


def build_instagram_port(settings: Settings) -> MessagePort:
    sender = settings.instagram_sender.strip().lower()
    if sender not in VALID_INSTAGRAM_SENDERS:
        return DisabledMessagePort()
    if sender == "composio":
        if settings.composio_ready():
            return ComposioInstagramPort(
                api_key=settings.composio_api_key,
                user_id=settings.composio_user_id,
                account_id=settings.instagram_account_id,
            )
        return DisabledMessagePort()
    if settings.instagram_access_token and settings.instagram_account_id:
        try:
            return InstagramCloudPort(
                access_token=settings.instagram_access_token,
                account_id=settings.instagram_account_id,
                graph_version=settings.instagram_graph_version,
                graph_host=settings.instagram_graph_host,
            )
        except InstagramSendError:
            return DisabledMessagePort()
    return DisabledMessagePort()
