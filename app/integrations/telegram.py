"""Telegram Bot API owner channel. Numeric user ids only. No username auth."""

from pathlib import PurePosixPath
from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.core.errors import MiaError
from app.domain.tools import AdapterHttpError
from app.integrations.base import DisabledMessagePort, MessagePort, OutboundMessage
from app.integrations.telegram_format import MAX_MESSAGE_CHARS, split_message

_TELEGRAM_API = "https://api.telegram.org"
# getFile downloads are capped at 20MB by Telegram; stay under it.
_MAX_PHOTO_BYTES = 5_000_000
_SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
)
_TIMEOUT = 20.0
_SUPPORTED_AUDIO_MIME_TYPES = frozenset(
    {
        "audio/aac",
        "audio/flac",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/webm",
        "audio/x-m4a",
        "audio/x-wav",
    }
)
_MIME_BY_EXTENSION = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/x-m4a",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}
_EXTENSION_BY_MIME = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".mp4",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}
_GENERIC_CDN_MEDIA_TYPES = frozenset({"", "application/octet-stream"})

# Must be stated on every setWebhook call. Omitting it reuses the previous server-side
# value, which silently drops button presses if it was ever narrowed to messages only.
ALLOWED_UPDATES: tuple[str, ...] = ("message", "edited_message", "callback_query")


class TelegramSendError(MiaError):
    code = "telegram_send_failed"
    http_status = 502


class TelegramMediaError(MiaError):
    code = "telegram_media_failed"
    http_status = 502


def normalize_telegram_audio_mime(content_type: object) -> str:
    """Return one supported media type, rejecting absent or malformed headers."""
    if not isinstance(content_type, str) or not content_type.strip():
        raise TelegramMediaError("Telegram media content type missing")
    parts = content_type.split(";")
    mime = parts[0].strip().lower()
    if mime not in _SUPPORTED_AUDIO_MIME_TYPES:
        raise TelegramMediaError("Telegram media content type is unsupported")
    for parameter in parts[1:]:
        if "=" not in parameter:
            raise TelegramMediaError("Telegram media content type is malformed")
        name, value = parameter.split("=", 1)
        if not name.strip() or not value.strip():
            raise TelegramMediaError("Telegram media content type is malformed")
    return mime


def validate_telegram_voice_media(audio: object, content_type: object) -> tuple[bytes, str]:
    """Enforce the voice-media contract at every adapter boundary."""
    if not isinstance(audio, bytes) or not audio:
        raise TelegramMediaError("Telegram media is empty or malformed")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise TelegramMediaError("Telegram media exceeds maximum size")
    return audio, normalize_telegram_audio_mime(content_type)


def _mime_from_filename(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return _MIME_BY_EXTENSION.get(PurePosixPath(value).suffix.lower(), "")


def _largest_photo_file_id(message: dict[str, Any]) -> str:
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        largest = photos[-1]
        if isinstance(largest, dict):
            return str(largest.get("file_id") or "")
    document = message.get("document") or {}
    if isinstance(document, dict):
        mime = str(document.get("mime_type") or "").split(";", 1)[0].strip().lower()
        if mime in _SUPPORTED_IMAGE_MIME_TYPES or mime.startswith("image/"):
            if not is_telegram_audio_document(document):
                return str(document.get("file_id") or "")
    return ""


def is_telegram_audio_document(document: object) -> bool:
    """Only route an audio document to STT; ordinary documents remain documents."""
    if not isinstance(document, dict):
        return False
    declared = document.get("mime_type")
    try:
        normalize_telegram_audio_mime(declared)
        return True
    except TelegramMediaError:
        if isinstance(declared, str) and declared.split(";", 1)[0].strip().lower().startswith(
            "audio/"
        ):
            # It is an audio document even when its codec is outside Mia's allowlist.
            # Route it to the normal validation path so the owner receives the fixed,
            # truthful retry explanation rather than seeing it disappear.
            return True
        return bool(_mime_from_filename(document.get("file_name")))


def resolve_telegram_audio_mime(
    *,
    cdn_content_type: object,
    declared_mime_type: object = "",
    file_path: object = "",
    declared_filename: object = "",
) -> str:
    """Prefer an explicit CDN audio type, with narrow Telegram metadata fallback.

    Telegram's file CDN can return ``application/octet-stream`` for an otherwise valid
    Voice/Audio upload.  That generic header is not evidence that the audio is invalid;
    use the Bot API's declared MIME type or the trusted getFile extension in that one
    case. A concrete non-audio CDN type still fails closed.
    """
    if isinstance(cdn_content_type, str):
        base = cdn_content_type.split(";", 1)[0].strip().lower()
    else:
        base = ""
    if base not in _GENERIC_CDN_MEDIA_TYPES:
        return normalize_telegram_audio_mime(cdn_content_type)
    try:
        return normalize_telegram_audio_mime(declared_mime_type)
    except TelegramMediaError:
        from_path = _mime_from_filename(file_path)
        if from_path:
            return from_path
        from_declared_name = _mime_from_filename(declared_filename)
        if from_declared_name:
            return from_declared_name
        raise TelegramMediaError("Telegram media content type is unsupported") from None


def transcription_filename(*, mime_type: str, file_path: object = "") -> str:
    """Give OpenAI a safe extension consistent with the validated media type."""
    extension = _EXTENSION_BY_MIME.get(mime_type)
    if not extension:
        raise TelegramMediaError("Telegram media type has no transcription extension")
    candidate = PurePosixPath(str(file_path)).name if isinstance(file_path, str) else ""
    if candidate and candidate.lower().endswith(extension):
        return candidate
    return f"telegram-audio{extension}"


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
        """Send one owner message, splitting past the documented 4096-character limit.

        `reply_parameters` and `link_preview_options` replaced `reply_to_message_id` and
        `disable_web_page_preview` in Bot API 7.0. The old names still work server-side but
        are no longer in the docs, so the current form is used.

        Only the first chunk carries the reply and the keyboard: repeating a keyboard on
        every chunk would give Assaf the same buttons several times.
        """
        chunks = split_message(message.text) or [message.text]
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": message.conversation_id,
                "text": chunk,
                "link_preview_options": {"is_disabled": True},
            }
            if message.parse_mode:
                payload["parse_mode"] = message.parse_mode
            if index == 0:
                if message.reply_to_id and message.reply_to_id.isdigit():
                    payload["reply_parameters"] = {
                        "message_id": int(message.reply_to_id),
                        "allow_sending_without_reply": True,
                    }
                if message.reply_markup:
                    payload["reply_markup"] = message.reply_markup
            await self._call("sendMessage", payload)

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str = ""
    ) -> None:
        """Required after every button press.

        Telegram clients show a spinner until this lands, and the platform warns bots that
        under-answer callback queries. Call it before doing the real work, not after.
        """
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        await self._call("answerCallbackQuery", payload)

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: str,
        text: str,
        parse_mode: str | None = None,
        clear_markup: bool = True,
    ) -> None:
        """Rewrite a message after a decision. Editing beats send-and-delete per the docs."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id) if str(message_id).isdigit() else message_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if clear_markup:
            payload["reply_markup"] = {"inline_keyboard": []}
        await self._call("editMessageText", payload)

    async def set_webhook(self, url: str, *, secret_token: str) -> None:
        """Register the webhook with `allowed_updates` stated explicitly.

        `allowed_updates` is sticky server-side state on the bot token: if it was ever set
        to `["message"]`, `callback_query` is dropped silently and forever on every later
        call that omits the parameter. Buttons would spin with nothing in the logs. Always
        send it, and verify with `get_webhook_info`.
        """
        await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": list(ALLOWED_UPDATES),
            },
        )

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._call("getWebhookInfo")

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        try:
            try:
                if self._client is not None:
                    response = await self._client.post(
                        self._url(method), json=payload or {}, headers=headers
                    )
                else:
                    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                        response = await client.post(
                            self._url(method), json=payload or {}, headers=headers
                        )
            except httpx.HTTPError as exc:
                raise AdapterHttpError(None) from exc
            if response.status_code >= 400:
                raise AdapterHttpError(response.status_code)
        except AdapterHttpError as exc:
            _reraise_classified(TelegramSendError, f"Telegram {method} failed", exc)
        try:
            body = response.json()
        except ValueError:
            return {}
        return body.get("result", {}) if isinstance(body, dict) else {}

    async def download_voice(
        self, file_id: str, *, declared_mime_type: str = "", declared_filename: str = ""
    ) -> tuple[bytes, str, str]:
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
            return await self._download_voice_file(
                file_url,
                declared_mime_type=declared_mime_type,
                declared_filename=declared_filename,
                file_path=file_path,
            )
        except AdapterHttpError as exc:
            _reraise_classified(TelegramMediaError, "Telegram media download failed", exc)

    async def download_photo(self, file_id: str) -> tuple[bytes, str]:
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
            if self._client is not None:
                media = await self._client.get(file_url)
            else:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    media = await client.get(file_url)
            if media.status_code >= 400:
                raise AdapterHttpError(media.status_code)
            mime = str(media.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            suffix = PurePosixPath(file_path).suffix.lower()
            if mime not in _SUPPORTED_IMAGE_MIME_TYPES:
                mime = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".gif": "image/gif",
                }.get(suffix, "")
            if mime not in _SUPPORTED_IMAGE_MIME_TYPES:
                raise TelegramMediaError("Telegram photo content type is unsupported")
            payload = media.content
            if not payload or len(payload) > _MAX_PHOTO_BYTES:
                raise TelegramMediaError("Telegram photo is empty or too large")
            return payload, mime
        except AdapterHttpError as exc:
            _reraise_classified(TelegramMediaError, "Telegram photo download failed", exc)

    async def _download_voice_file(
        self,
        file_url: str,
        *,
        declared_mime_type: str = "",
        declared_filename: str = "",
        file_path: str = "",
    ) -> tuple[bytes, str, str]:
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
        mime_type = resolve_telegram_audio_mime(
            cdn_content_type=media.headers.get("content-type"),
            declared_mime_type=declared_mime_type,
            file_path=file_path,
            declared_filename=declared_filename,
        )
        audio, mime_type = validate_telegram_voice_media(media.content, mime_type)
        filename_evidence = (
            file_path if _mime_from_filename(file_path) == mime_type else declared_filename
        )
        return audio, mime_type, transcription_filename(
            mime_type=mime_type, file_path=filename_evidence
        )


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
    native_voice = message.get("voice")
    voice = native_voice or message.get("audio") or {}
    if not voice and is_telegram_audio_document(message.get("document")):
        voice = message.get("document") or {}
    file_id = ""
    # Telegram Voice is always OGG/Opus. Audio/document uploads need their own declared
    # MIME or provider filename; fabricating OGG would mislabel an MP3 with a generic CDN header.
    mime_type = "audio/ogg" if native_voice else ""
    if isinstance(voice, dict):
        file_id = str(voice.get("file_id") or "")
        mime_type = str(voice.get("mime_type") or mime_type) or mime_type
    photo_file_id = _largest_photo_file_id(message)
    message_id = str(message.get("message_id") or "")
    return {
        "id": str(update_id),
        "from": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "file_id": file_id,
        "photo_file_id": photo_file_id,
        "mime_type": mime_type,
        "file_name": str(voice.get("file_name") or "") if isinstance(voice, dict) else "",
    }


def parse_telegram_callback(payload: dict[str, Any]) -> dict[str, str] | None:
    """Parse a `callback_query` update into the fields the owner path needs.

    An Update carries at most one of its optional fields, so `message` and `callback_query`
    are a clean either/or. `from` is a Python keyword, so it is read by key, never
    attribute. The docs warn a callback can be replayed against a message that no longer
    carries that button, so the handler must stay idempotent.
    """
    update_id = payload.get("update_id")
    query = payload.get("callback_query")
    if update_id is None or not isinstance(query, dict):
        return None
    user = query.get("from") or {}
    user_id = str(user.get("id") or "")
    query_id = str(query.get("id") or "")
    if not user_id or not query_id:
        return None
    message = query.get("message") or {}
    chat = message.get("chat") or {} if isinstance(message, dict) else {}
    chat_id = str(chat.get("id") or "") or user_id
    message_id = (
        str(message.get("message_id") or "") if isinstance(message, dict) else ""
    )
    return {
        "id": str(update_id),
        "callback_query_id": query_id,
        "from": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "data": str(query.get("data") or ""),
    }


def build_telegram_port(settings: Settings) -> MessagePort:
    if settings.telegram_bot_token.strip():
        return TelegramPort(bot_token=settings.telegram_bot_token)
    return DisabledMessagePort()
