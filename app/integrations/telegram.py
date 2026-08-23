"""Telegram Bot API owner channel. Numeric user ids only. No username auth."""

from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.core.errors import MiaError
from app.domain.tools import AdapterHttpError
from app.integrations.base import DisabledMessagePort, MessagePort, OutboundMessage

_TELEGRAM_API = "https://api.telegram.org"
_MAX_AUDIO_BYTES = 16_000_000
_TIMEOUT = 20.0


class TelegramSendError(MiaError):
    code = "telegram_send_failed"
    http_status = 502


class TelegramMediaError(MiaError):
    code = "telegram_media_failed"
    http_status = 502


def _reraise_classified(error_cls: type[MiaError], prefix: str, exc: AdapterHttpError) -> NoReturn:
    detail = f": HTTP {exc.status_code}" if exc.status_code is not None else ""
    raise error_cls(f"{prefix}{detail}") from exc


class TelegramPort:
    def __init__(
        self,
        *,
        bot_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._client = client

    def _url(self, method: str) -> str:
        return f"{_TELEGRAM_API}/bot{self._bot_token}/{method}"

    async def send(self, message: OutboundMessage) -> None:
        payload = {
            "chat_id": message.conversation_id,
            "text": message.text,
        }
        if message.reply_to_id and message.reply_to_id.isdigit():
            payload["reply_to_message_id"] = int(message.reply_to_id)
        headers = {"Content-Type": "application/json"}
        try:
            try:
                if self._client is not None:
                    response = await self._client.post(
                        self._url("sendMessage"), json=payload, headers=headers
                    )
                else:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.post(
                            self._url("sendMessage"), json=payload, headers=headers
                        )
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
            if response.status_code >= 400:
                raise AdapterHttpError(response.status_code)
        except AdapterHttpError as exc:
            _reraise_classified(TelegramSendError, "Telegram send failed", exc)

    async def download_voice(self, file_id: str) -> tuple[bytes, str]:
        headers = {"Content-Type": "application/json"}
        try:
            try:
                if self._client is not None:
                    meta = await self._client.post(
                        self._url("getFile"), json={"file_id": file_id}, headers=headers
                    )
                else:
                    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                        meta = await client.post(
                            self._url("getFile"), json={"file_id": file_id}, headers=headers
                        )
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
            if meta.status_code >= 400:
                raise AdapterHttpError(meta.status_code)
            body = meta.json()
            if not isinstance(body, dict) or not body.get("ok"):
                raise TelegramMediaError("Telegram getFile failed")
            result = body.get("result") or {}
            file_path = str(result.get("file_path") or "")
            if not file_path or ".." in file_path:
                raise TelegramMediaError("Telegram media path missing")
            file_url = f"{_TELEGRAM_API}/file/bot{self._bot_token}/{file_path}"
            parsed = urlparse(file_url)
            if parsed.scheme != "https" or (parsed.hostname or "") != "api.telegram.org":
                raise TelegramMediaError("Telegram media host is not allowlisted")
            try:
                if self._client is not None:
                    media = await self._client.get(file_url)
                else:
                    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                        media = await client.get(file_url)
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
            if media.status_code >= 400:
                raise AdapterHttpError(media.status_code)
            data = media.content
            if len(data) > _MAX_AUDIO_BYTES:
                raise TelegramMediaError("Telegram media exceeds maximum size")
            mime = media.headers.get("content-type", "audio/ogg") or "audio/ogg"
            return data, mime.split(";")[0].strip()
        except AdapterHttpError as exc:
            _reraise_classified(TelegramMediaError, "Telegram media download failed", exc)


def parse_telegram_update(payload: dict[str, Any]) -> dict[str, str] | None:
    update_id = payload.get("update_id")
    message = payload.get("message") or payload.get("edited_message") or {}
    if not isinstance(message, dict):
        return None
    user = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = str(user.get("id") or "")
    chat_id = str(chat.get("id") or "") or user_id
    if update_id is None or not user_id:
        return None
    text = str(message.get("text") or message.get("caption") or "")
    voice = message.get("voice") or message.get("audio") or {}
    file_id = ""
    mime_type = "audio/ogg"
    if isinstance(voice, dict):
        file_id = str(voice.get("file_id") or "")
        mime_type = str(voice.get("mime_type") or mime_type) or mime_type
    message_id = str(message.get("message_id") or "")
    return {
        "id": str(update_id),
        "from": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "file_id": file_id,
        "mime_type": mime_type,
    }


def build_telegram_port(settings: Settings) -> MessagePort:
    if settings.telegram_bot_token.strip():
        return TelegramPort(bot_token=settings.telegram_bot_token)
    return DisabledMessagePort()
