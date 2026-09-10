import math
import re
from base64 import b64encode
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.core.errors import MiaError
from app.core.models import model_chain
from app.domain.tools import AdapterHttpError

_OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
_GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_MAX_GEMINI_INLINE_AUDIO_BYTES = 14_000_000
# The prompt should match the audio language, so the Hebrew-primary owner channel gets a
# Hebrew prompt. Product names go in `keywords` for models that support it.
_ASSAFWEB_STT_PROMPT = (
    "שיחות עסקיות עם Mia, העוזרת של AssafWeb. עברית ואנגלית יכולות "
    "להופיע באותו משפט. יש לתמלל במדויק שמות אנשים וחברות, כתובות אימייל, "
    "מספרי טלפון, סכומים, שעות ותאריכים; לא להשלים פרט שלא נשמע בבירור. "
    "נושאים נפוצים: עובדים דיגיטליים, אוטומציות, סוכני AI, CRM, אימייל, "
    "יומן, פגישות, Google Sheets, GA4, Search Console, Instagram ו-LinkedIn."
)
_ASSAFWEB_STT_KEYWORDS: tuple[str, ...] = (
    "AssafWeb",
    "אסף",
    "Mia",
    "מיה",
    "עובד דיגיטלי",
    "אוטומציה",
    "ליד",
    "וואטסאפ",
    "CRM",
    "Contacts",
    "Google Sheets",
    "Gmail",
    "Google Calendar",
    "GA4",
    "Google Search Console",
    "Instagram",
    "LinkedIn",
)
_DEFAULT_LANGUAGES: tuple[str, ...] = ("he", "en")


def transcription_request_fields(
    *,
    model: str,
    prompt: str,
    languages: tuple[str, ...],
    keywords: tuple[str, ...],
) -> dict[str, object]:
    """Build the multipart fields for one model, per its documented capabilities.

    Three families with genuinely different contracts:

    - `whisper-1` is the only model that supports `verbose_json`, and it takes a singular
      `language`. It is also the only one returning `language` / `duration` inline.
    - `gpt-transcribe` takes a plural `languages` array plus `keywords`, and documents
      `json` and `text` only. Sending `language` alongside `languages` is explicitly
      called out as wrong: "don't send both fields".
    - `gpt-4o-*-transcribe` takes singular `language`, and `json` is the ONLY supported
      response format.

    The previous implementation sent `verbose_json` to `gpt-transcribe` unconditionally,
    which is unsupported and made every owner voice note fail silently.
    """
    name = model.strip().lower()
    fields: dict[str, object] = {"model": model}
    if prompt and not name.endswith("-diarize"):
        fields["prompt"] = prompt
    if name.startswith("whisper"):
        fields["response_format"] = "verbose_json"
        if languages:
            fields["language"] = languages[0]
        return fields
    fields["response_format"] = "json"
    if name.startswith("gpt-transcribe") or name.startswith("gpt-live-transcribe"):
        if languages:
            fields["languages[]"] = list(languages)
        if keywords:
            fields["keywords[]"] = list(keywords)
        return fields
    if name.startswith("gpt-4o-") and name.endswith("-transcribe"):
        if languages:
            fields["language"] = languages[0]
        return fields
    raise TranscriptionError("OpenAI transcription model is unsupported")


_STT_PROVIDER_ALLOWLIST = frozenset({"openai", "gemini", "fake"})
_STT_MODEL_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
_MAX_DURATION_MS = 86_400_000


class TranscriptionError(MiaError):
    code = "transcription_failed"
    http_status = 502


class TranscriptResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    stt_provider: str = ""
    stt_model: str = ""
    language: str = ""
    duration_ms: int = 0
    confidence: str = ""


def sanitize_stt_provider(value: object) -> str:
    if isinstance(value, str) and value in _STT_PROVIDER_ALLOWLIST:
        return value
    return ""


def sanitize_stt_model(value: object) -> str:
    if isinstance(value, str) and _STT_MODEL_RE.fullmatch(value):
        return value
    return ""


def sanitize_language(value: object) -> str:
    if isinstance(value, str) and _LANGUAGE_RE.fullmatch(value):
        return value
    return ""


def sanitize_confidence(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        if value not in (0, 1):
            return ""
        num = float(value)
    elif isinstance(value, float):
        num = value
    elif isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            return ""
    else:
        return ""
    if not math.isfinite(num) or num < 0 or num > 1:
        return ""
    if num == 0:
        return "0"
    if num == 1:
        return "1"
    formatted = f"{num:.10f}".rstrip("0").rstrip(".")
    return formatted[:16]


def detected_language(payload: dict) -> str:
    """Read the detected language from either documented response shape.

    `gpt-transcribe` returns `languages`, an array of `{code}` where an empty array means
    no reliable detection. `verbose_json` (whisper-1) returns a singular `language`, and
    its documented example is a language *name* ("english") rather than a code, so it is
    sanitized and dropped if it is not an ISO code.
    """
    if not isinstance(payload, dict):
        return ""
    languages = payload.get("languages")
    if isinstance(languages, list):
        for item in languages:
            if isinstance(item, dict):
                code = sanitize_language(item.get("code"))
                if code:
                    return code
            elif isinstance(item, str):
                code = sanitize_language(item)
                if code:
                    return code
    if "language" in payload:
        return sanitize_language(payload.get("language"))
    return ""


def detected_duration_ms(payload: dict) -> int:
    """Duration from `duration` (verbose_json) or `usage.seconds` (duration billing)."""
    if not isinstance(payload, dict):
        return 0
    if "duration" in payload:
        duration = duration_ms_from_seconds(payload.get("duration"))
        if duration:
            return duration
    usage = payload.get("usage")
    if isinstance(usage, dict) and "seconds" in usage:
        return duration_ms_from_seconds(usage.get("seconds"))
    return 0


def duration_ms_from_seconds(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        seconds = float(value)
    elif isinstance(value, float):
        seconds = value
    elif isinstance(value, str):
        try:
            seconds = float(value)
        except ValueError:
            return 0
    else:
        return 0
    duration_ms = int(seconds * 1000)
    if duration_ms < 0:
        return 0
    if duration_ms > _MAX_DURATION_MS:
        return _MAX_DURATION_MS
    return duration_ms


class TranscriptionPort(Protocol):
    async def transcribe(
        self, *, audio: bytes, mime_type: str, filename: str = "note.ogg"
    ) -> TranscriptResult: ...


class OpenAITranscribePort:
    """GPT Transcribe adapter. Audio stays in memory; never logged."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        fallback_model: str = "",
        prompt: str = _ASSAFWEB_STT_PROMPT,
        languages: tuple[str, ...] = _DEFAULT_LANGUAGES,
        keywords: tuple[str, ...] = _ASSAFWEB_STT_KEYWORDS,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._models = model_chain(model, fallback_model)
        self._prompt = prompt
        self._languages = languages
        # "The API rejects the entire request" on a keyword containing < > CR or LF.
        self._keywords = tuple(
            keyword
            for keyword in keywords
            if keyword and not any(char in keyword for char in "<>\r\n")
        )
        self._client = client
        self._timeout = timeout

    async def transcribe(
        self, *, audio: bytes, mime_type: str, filename: str = "note.ogg"
    ) -> TranscriptResult:
        last_error: TranscriptionError | None = None
        for model in self._models:
            try:
                return await self._transcribe_model(
                    model=model, audio=audio, mime_type=mime_type, filename=filename
                )
            except AdapterHttpError as exc:
                detail = f": HTTP {exc.status_code}" if exc.status_code is not None else ""
                last_error = TranscriptionError(f"OpenAI transcription failed{detail}")
                last_error.__cause__ = exc
            except TranscriptionError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise TranscriptionError("OpenAI transcription returned empty text")

    async def _transcribe_model(
        self, *, model: str, audio: bytes, mime_type: str, filename: str
    ) -> TranscriptResult:
        # The spec requires "enough format metadata for the file to be identified", so the
        # filename and content type are always explicit rather than left to the client.
        files = {"file": (filename, audio, mime_type)}
        data = transcription_request_fields(
            model=model,
            prompt=self._prompt,
            languages=self._languages,
            keywords=self._keywords,
        )
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                response = await self._client.post(
                    _OPENAI_TRANSCRIPTIONS_URL,
                    files=files,
                    data=data,
                    headers=headers,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        _OPENAI_TRANSCRIPTIONS_URL,
                        files=files,
                        data=data,
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        return self._parse_response(response, model=model)

    def _parse_response(self, response: httpx.Response, *, model: str) -> TranscriptResult:
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise TranscriptionError("OpenAI transcription returned invalid response") from exc
        if not isinstance(payload, dict):
            raise TranscriptionError("OpenAI transcription returned invalid response")
        text = payload.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("OpenAI transcription returned empty text")
        language = detected_language(payload)
        duration_ms = detected_duration_ms(payload)
        # No documented OpenAI transcription response carries a `confidence` field. It is
        # still read when present so a provider that does supply one is not discarded.
        confidence = ""
        if "confidence" in payload:
            confidence = sanitize_confidence(payload.get("confidence"))
        return TranscriptResult(
            text=text.strip(),
            stt_provider="openai",
            stt_model=sanitize_stt_model(model),
            language=language,
            duration_ms=duration_ms,
            confidence=confidence,
        )


class GeminiTranscribePort:
    """Native Gemini audio adapter used only after the OpenAI STT chain fails."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        prompt: str = _ASSAFWEB_STT_PROMPT,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model.strip()
        self._prompt = prompt
        self._timeout = timeout
        self._client = client

    async def transcribe(
        self, *, audio: bytes, mime_type: str, filename: str = "note.ogg"
    ) -> TranscriptResult:
        del filename
        if not self._api_key.strip() or not self._model:
            raise TranscriptionError("Gemini transcription is not configured")
        if not audio:
            raise TranscriptionError("Gemini transcription received empty audio")
        if len(audio) > _MAX_GEMINI_INLINE_AUDIO_BYTES:
            raise TranscriptionError("Gemini transcription audio is too large")
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                f"{self._prompt}\n\nTranscribe only what is audible. "
                                "Preserve Hebrew and English code switching. Mark an "
                                "uncertain word as [לא ברור] instead of guessing."
                            )
                        },
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64encode(audio).decode("ascii"),
                            }
                        },
                    ],
                }
            ]
        }
        url = _GEMINI_GENERATE_URL.format(model=quote(self._model, safe=""))
        headers = {"x-goog-api-key": self._api_key}
        try:
            if self._client is not None:
                response = await self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TranscriptionError("Gemini transcription request failed") from exc
        if response.status_code >= 400:
            raise TranscriptionError(
                f"Gemini transcription failed: HTTP {response.status_code}"
            )
        text = self._response_text(response)
        if not text:
            raise TranscriptionError("Gemini transcription returned empty text")
        return TranscriptResult(
            text=text,
            stt_provider="gemini",
            stt_model=sanitize_stt_model(self._model),
            # Gemini audio understanding does not expose calibrated confidence here.
            language="",
            confidence="",
        )

    @staticmethod
    def _response_text(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise TranscriptionError("Gemini transcription returned invalid response") from exc
        if not isinstance(payload, dict):
            raise TranscriptionError("Gemini transcription returned invalid response")
        direct = payload.get("text") or payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            return ""
        parts: list[str] = []
        for candidate in candidates:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            raw_parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(raw_parts, list):
                continue
            for part in raw_parts:
                value = part.get("text") if isinstance(part, dict) else None
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        return "\n".join(parts).strip()


class FallbackTranscriptionPort:
    """Try provider adapters in order without exposing audio or provider bodies."""

    def __init__(self, ports: tuple[TranscriptionPort, ...]) -> None:
        self._ports = ports

    async def transcribe(
        self, *, audio: bytes, mime_type: str, filename: str = "note.ogg"
    ) -> TranscriptResult:
        last: Exception | None = None
        for port in self._ports:
            try:
                return await port.transcribe(
                    audio=audio, mime_type=mime_type, filename=filename
                )
            except (TranscriptionError, AdapterHttpError, httpx.HTTPError, RuntimeError) as exc:
                last = exc
        raise TranscriptionError("all transcription providers failed") from last


class DisabledTranscriptionPort:
    async def transcribe(
        self, *, audio: bytes, mime_type: str, filename: str = "note.ogg"
    ) -> TranscriptResult:
        raise RuntimeError("transcription port is not configured")


class FakeTranscriptionPort:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.call_count = 0

    async def transcribe(
        self, *, audio: bytes, mime_type: str, filename: str = "note.ogg"
    ) -> TranscriptResult:
        self.call_count += 1
        return TranscriptResult(
            text=self.transcript,
            stt_provider="fake",
            stt_model="fake",
        )


def build_transcription_port(settings: Settings) -> TranscriptionPort:
    chain = model_chain(settings.openai_transcribe_model, settings.openai_transcribe_fallback_model)
    ports: list[TranscriptionPort] = []
    if settings.openai_api_key and chain:
        ports.append(
            OpenAITranscribePort(
                api_key=settings.openai_api_key,
                model=chain[0],
                fallback_model=chain[1] if len(chain) > 1 else "",
                timeout=settings.transcription_timeout_seconds,
            )
        )
    if settings.gemini_api_key.strip() and settings.gemini_transcribe_model.strip():
        ports.append(
            GeminiTranscribePort(
                api_key=settings.gemini_api_key,
                model=settings.gemini_transcribe_model,
                timeout=settings.transcription_timeout_seconds,
            )
        )
    if len(ports) == 1:
        return ports[0]
    if ports:
        return FallbackTranscriptionPort(tuple(ports))
    return DisabledTranscriptionPort()
