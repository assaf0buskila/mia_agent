"""STT adapter contracts: request families are explicit and provider errors are safe."""

import httpx
import pytest
from app.integrations.transcribe import (
    OpenAITranscribePort,
    TranscriptionError,
    transcription_request_fields,
)


@pytest.mark.parametrize(
    ("model", "expected", "forbidden"),
    [
        (
            "whisper-1",
            {"response_format": "verbose_json", "language": "he"},
            {"languages[]", "keywords[]"},
        ),
        (
            "gpt-transcribe",
            {
                "response_format": "json",
                "languages[]": ["he", "en"],
                "keywords[]": ["Mia"],
            },
            {"language"},
        ),
        (
            "gpt-4o-mini-transcribe",
            {"response_format": "json", "language": "he"},
            {"languages[]", "keywords[]"},
        ),
    ],
)
def test_transcription_request_fields_are_limited_to_each_model_family(
    model: str, expected: dict[str, object], forbidden: set[str]
) -> None:
    fields = transcription_request_fields(
        model=model, prompt="prompt", languages=("he", "en"), keywords=("Mia",)
    )
    for key, value in expected.items():
        assert fields[key] == value
    assert fields["prompt"] == "prompt"
    assert forbidden.isdisjoint(fields)


def test_transcription_rejects_unknown_model_before_request() -> None:
    with pytest.raises(TranscriptionError, match="model is unsupported"):
        transcription_request_fields(
            model="unreviewed-model", prompt="", languages=("he",), keywords=()
        )


async def test_transcription_malformed_provider_body_is_visible_but_safe() -> None:
    secret = "test-stt-secret"
    audio_marker = b"private-audio-marker"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))
    )
    port = OpenAITranscribePort(api_key=secret, model="gpt-transcribe", client=client)
    try:
        with pytest.raises(TranscriptionError, match="invalid response") as raised:
            await port.transcribe(audio=audio_marker, mime_type="audio/ogg")
    finally:
        await client.aclose()
    detail = str(raised.value)
    assert secret not in detail
    assert audio_marker.decode() not in detail
