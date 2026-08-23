import math
import re
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.core.errors import MiaError
from app.core.models import model_chain
from app.domain.tools import AdapterHttpError

_OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
_ASSAFWEB_STT_PROMPT = (
    "AssafWeb digital employee sales conversations. "
    "Hebrew and English business terms."
)

_STT_PROVIDER_ALLOWLIST = frozenset({"openai", "fake"})
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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._models = model_chain(model, fallback_model)
        self._prompt = prompt
        self._client = client

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
        files = {"file": (filename, audio, mime_type)}
        data = {
            "model": model,
            "prompt": self._prompt,
            "response_format": "verbose_json",
        }
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
                async with httpx.AsyncClient(timeout=20.0) as client:
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
        payload = response.json()
        text = payload.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("OpenAI transcription returned empty text")
        language = ""
        if "language" in payload:
            language = sanitize_language(payload.get("language"))
        duration_ms = 0
        if "duration" in payload:
            duration_ms = duration_ms_from_seconds(payload.get("duration"))
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
    chain = model_chain(
        settings.openai_transcribe_model, settings.openai_transcribe_fallback_model
    )
    if settings.openai_api_key and chain:
        return OpenAITranscribePort(
            api_key=settings.openai_api_key,
            model=chain[0],
            fallback_model=chain[1] if len(chain) > 1 else "",
        )
    return DisabledTranscriptionPort()
