"""Shared speech-to-text. Voice is transport, not a separate brain."""

from app.integrations.transcribe import (
    TranscriptionPort,
    TranscriptResult,
    build_transcription_port,
)

__all__ = ["TranscriptionPort", "TranscriptResult", "build_transcription_port"]
