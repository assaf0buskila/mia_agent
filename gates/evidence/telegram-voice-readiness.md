# Telegram voice readiness evidence

Date: 2026-08-28 (local, no live calls made)

Required setting names:

- `MIA_TELEGRAM_BOT_TOKEN` — Telegram adapter enablement.
- `MIA_TELEGRAM_WEBHOOK_SECRET` — webhook authentication.
- `MIA_TELEGRAM_OWNER_USER_IDS` — numeric owner allowlist.
- `MIA_OPENAI_API_KEY` — OpenAI transcription adapter credential.
- `MIA_OPENAI_TRANSCRIBE_MODEL` — reviewed transcription model family selector.
- `MIA_OPENAI_TRANSCRIBE_FALLBACK_MODEL` — optional reviewed fallback selector.

Adapter state from source inspection (not a live readiness assertion): `build_telegram_port`
uses the live Telegram adapter only when the bot-token setting is non-empty; otherwise it
uses the disabled message port. `build_transcription_port` uses OpenAI only when its API
key and at least one model setting are present; otherwise it uses the disabled
transcription port. No values are recorded in this evidence.
