from typing import NoReturn
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.core.errors import MiaError
from app.domain.tools import AdapterHttpError
from app.integrations.base import DisabledMessagePort, MessagePort, OutboundMessage

_GRAPH_BASE = "https://graph.facebook.com"
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


def whatsapp_template_send_allowed() -> bool:
    """Templates are mass-outbound. Not wired. Not an env override."""
    return False


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


def build_whatsapp_port(settings: Settings) -> MessagePort:
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
