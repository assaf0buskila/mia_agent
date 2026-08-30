# Gates: Telegram voice input end to end

Scope: A Telegram voice note is authenticated, downloaded, transcribed with the configured model family, processed by OwnerGraph, and answered as text.

- [x] G1: A production-shaped test covers Telegram update -> media download -> STT -> OwnerGraph -> escaped HTML reply.
  CHECK: uv --cache-dir .uv-cache run pytest -q tests/unit/test_telegram.py -k voice -p no:cacheprovider
  EXPECT: /passed/
  EVIDENCE: 2026-08-28 local run: 3 passed. Covers numeric owner allowlist-authenticated webhook, voice download, shared TranscriptionPort, real OwnerGraph invocation, and HTML escaping.
- [x] G2: Transcription requests select only parameters supported by the configured model family and fail visibly without leaking content or credentials.
  CHECK: uv --cache-dir .uv-cache run pytest -q tests/unit/test_transcribe.py -p no:cacheprovider
  EXPECT: /passed/
  EVIDENCE: 2026-08-28 local run: 5 passed. Covers Whisper, GPT Transcribe, GPT-4o Transcribe field contracts, unknown-family rejection, and a malformed-provider response with no audio or credential in its error.
- [x] G3: Live-readiness evidence identifies every required setting by name and current adapter status without exposing values.
  EVIDENCE: gates/evidence/telegram-voice-readiness.md lists six setting names and source-derived enabled/disabled adapter states only; no values recorded and no live call made.
