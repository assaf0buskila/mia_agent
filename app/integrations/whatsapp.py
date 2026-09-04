from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.core.errors import MiaError
from app.domain.tools import AdapterHttpError
from app.integrations.base import DisabledMessagePort, MessagePort, OutboundMessage

_GRAPH_BASE = "https://graph.facebook.com"
VALID_WHATSAPP_SENDERS = frozenset({"direct", "composio", "baileys"})
COMPOSIO_WHATSAPP_VERSION = "20260815_00"
COMPOSIO_SEND_TEXT_TOOL = "WHATSAPP_SEND_MESSAGE"
COMPOSIO_SEND_TEMPLATE_TOOL = "WHATSAPP_SEND_TEMPLATE_MESSAGE"
WHATSAPP_STATUS_TRIGGER = "WHATSAPP_MESSAGE_STATUS_UPDATED_TRIGGER"
WHATSAPP_INBOUND_TRIGGER_SLUGS: frozenset[str] = frozenset()
_COMPOSIO_EXECUTE_BASE = "https://backend.composio.dev/api/v3.1/tools/execute"
_ALLOWED_MEDIA_HOST_SUFFIXES = (
    ".fbsbx.com",
    ".facebook.com",
    ".fbcdn.net",
    ".whatsapp.net",
)
_MAX_AUDIO_BYTES = 16_000_000
_MEDIA_TIMEOUT = 20.0


class WhatsAppSendError(MiaError):
    code = "whatsapp_send_failed"
    http_status = 502


class WhatsAppMediaError(MiaError):
    code = "whatsapp_media_failed"
    http_status = 502


def _reraise_classified(
    error_cls: type[MiaError], prefix: str, exc: AdapterHttpError
) -> NoReturn:
    detail = f": HTTP {exc.status_code}" if exc.status_code is not None else ""
    raise error_cls(f"{prefix}{detail}") from exc


def normalized_whatsapp_sender(settings: Settings) -> str:
    sender = settings.whatsapp_sender.strip().lower()
    if sender in VALID_WHATSAPP_SENDERS:
        return sender
    return "direct"


def whatsapp_template_send_allowed() -> bool:
    """Templates are mass-outbound. Not wired. Not an env override."""
    return False


def parse_composio_whatsapp_inbound(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize Composio WhatsApp trigger payloads.

    Official toolkit has no incoming-customer-message trigger. Status updates
    and unknown slugs yield no inbound items. Do not invent a trigger name.
    """
    metadata = payload.get("metadata") or {}
    slug = str(metadata.get("trigger_slug") or payload.get("trigger_slug") or "")
    trigger = payload.get("trigger") or {}
    if not slug:
        slug = str(trigger.get("slug") or "")
    if slug == WHATSAPP_STATUS_TRIGGER:
        return []
    if slug in WHATSAPP_INBOUND_TRIGGER_SLUGS:
        return []
    return []


class WhatsAppCloudPort:
    """Direct Graph API adapter for WhatsApp Cloud text sends."""

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        graph_version: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._graph_version = graph_version
        self._client = client

    async def send(self, message: OutboundMessage) -> None:
        url = f"{_GRAPH_BASE}/{self._graph_version}/{self._phone_number_id}/messages"
        body: dict[str, object] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.conversation_id,
            "type": "text",
            "text": {"preview_url": False, "body": message.text},
        }
        if message.reply_to_id:
            body["context"] = {"message_id": message.reply_to_id}
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
            _reraise_classified(
                WhatsAppSendError, "WhatsApp Cloud API send failed", exc
            )


class ComposioWhatsAppPort:
    """Composio WHATSAPP_SEND_MESSAGE. Inbound stays Meta webhook (ADR-016)."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        phone_number_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._phone_number_id = phone_number_id
        self._client = client

    async def send(self, message: OutboundMessage) -> None:
        arguments: dict[str, Any] = {
            "phone_number_id": self._phone_number_id,
            "to_number": message.conversation_id,
            "text": message.text,
            "preview_url": False,
        }
        if message.reply_to_id:
            arguments["message_id"] = message.reply_to_id
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_WHATSAPP_VERSION,
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
            _reraise_classified(
                WhatsAppSendError, "WhatsApp Composio send failed", exc
            )


class BaileysWhatsAppPort:
    """Send through the Baileys sidecar in `services/whatsapp-baileys`.

    Baileys speaks the reverse-engineered WhatsApp Web protocol from Node, so the
    connection lives in a separate process and this port is a thin HTTP client to it.
    Failures are raised as `WhatsAppSendError` like every other WhatsApp adapter, so
    the caller cannot tell which transport it got.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def send(self, message: OutboundMessage) -> None:
        payload = {
            "to": message.conversation_id,
            "text": message.text,
            "idempotency_key": message.idempotency_key,
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/send"
        try:
            try:
                if self._client is not None:
                    response = await self._client.post(url, json=payload, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        response = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
            if response.status_code >= 400:
                raise AdapterHttpError(response.status_code)
            try:
                body = response.json()
            except ValueError as exc:
                raise AdapterHttpError(response.status_code) from exc
            if not isinstance(body, dict) or body.get("sent") is not True:
                raise AdapterHttpError(400)
        except AdapterHttpError as exc:
            _reraise_classified(WhatsAppSendError, "WhatsApp Baileys send failed", exc)


def build_whatsapp_port(settings: Settings) -> MessagePort:
    sender = normalized_whatsapp_sender(settings)
    if sender == "baileys":
        base_url = settings.whatsapp_baileys_url.strip()
        token = settings.whatsapp_baileys_token.strip()
        if base_url and token:
            return BaileysWhatsAppPort(base_url=base_url, token=token)
        return DisabledMessagePort()
    if sender == "composio":
        if (
            settings.composio_ready()
            and settings.whatsapp_phone_number_id.strip()
        ):
            return ComposioWhatsAppPort(
                api_key=settings.composio_api_key,
                user_id=settings.composio_user_id,
                phone_number_id=settings.whatsapp_phone_number_id,
            )
        return DisabledMessagePort()
    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        return WhatsAppCloudPort(
            access_token=settings.whatsapp_access_token,
            phone_number_id=settings.whatsapp_phone_number_id,
            graph_version=settings.whatsapp_graph_version,
        )
    return DisabledMessagePort()


def _allowed_media_host(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    exact = {"facebook.com", "fbsbx.com", "fbcdn.net", "whatsapp.net"}
    if host in exact:
        return True
    return any(host.endswith(suffix) for suffix in _ALLOWED_MEDIA_HOST_SUFFIXES)


class WhatsAppMediaPort:
    """Download WhatsApp Cloud media into memory only."""

    def __init__(
        self,
        *,
        access_token: str,
        graph_version: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._graph_version = graph_version
        self._client = client

    async def download(self, media_id: str) -> tuple[bytes, str]:
        meta_url = f"{_GRAPH_BASE}/{self._graph_version}/{media_id}"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            try:
                if self._client is not None:
                    meta_response = await self._client.get(meta_url, headers=headers)
                    return await self._download_from_meta(meta_response, headers, self._client)
                async with httpx.AsyncClient(timeout=_MEDIA_TIMEOUT) as client:
                    meta_response = await client.get(meta_url, headers=headers)
                    return await self._download_from_meta(meta_response, headers, client)
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
        except AdapterHttpError as exc:
            _reraise_classified(
                WhatsAppMediaError, "WhatsApp media metadata failed", exc
            )

    async def _download_from_meta(
        self,
        meta_response: httpx.Response,
        headers: dict[str, str],
        client: httpx.AsyncClient,
    ) -> tuple[bytes, str]:
        if meta_response.status_code >= 400:
            raise AdapterHttpError(meta_response.status_code)
        payload = meta_response.json()
        media_url = payload.get("url", "")
        mime_type = payload.get("mime_type", "audio/ogg")
        if not isinstance(media_url, str) or not media_url:
            raise WhatsAppMediaError("WhatsApp media metadata missing url")
        if not _allowed_media_host(media_url):
            raise WhatsAppMediaError("WhatsApp media url host is not allowlisted")
        try:
            try:
                media_response = await client.get(media_url, headers=headers)
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
            return self._read_audio(media_response, mime_type)
        except AdapterHttpError as exc:
            _reraise_classified(
                WhatsAppMediaError, "WhatsApp media download failed", exc
            )

    @staticmethod
    def _read_audio(response: httpx.Response, mime_type: str) -> tuple[bytes, str]:
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        data = response.content
        if len(data) > _MAX_AUDIO_BYTES:
            raise WhatsAppMediaError("WhatsApp media exceeds maximum size")
        resolved_mime = response.headers.get("content-type", mime_type) or mime_type
        return data, resolved_mime.split(";")[0].strip()


class DisabledWhatsAppMediaPort:
    async def download(self, media_id: str) -> tuple[bytes, str]:
        raise RuntimeError("WhatsApp media download is not configured")


class FakeMediaPort:
    def __init__(self, media: dict[str, tuple[bytes, str]]) -> None:
        self.media = media

    async def download(self, media_id: str) -> tuple[bytes, str]:
        return self.media[media_id]


def build_whatsapp_media_port(settings: Settings) -> WhatsAppMediaPort | DisabledWhatsAppMediaPort:
    if settings.whatsapp_access_token:
        return WhatsAppMediaPort(
            access_token=settings.whatsapp_access_token,
            graph_version=settings.whatsapp_graph_version,
        )
    return DisabledWhatsAppMediaPort()
