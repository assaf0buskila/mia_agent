# ADR-017 v1 communication operating model

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat: finalize Mia v1 communication operating model)

**Context**
Assaf locked four answers: private owner talk is Telegram; customers talk with Mia on AssafWeb; personal WhatsApp stays human-only; hot leads stop selling and hand to Assaf on Telegram. WhatsApp is not an open inbox. Email may read/draft; send stays approval-gated. One Mia brain. Composio still has no WhatsApp inbound-message trigger (ADR-016).

**Decision**
Telegram is the owner control channel (numeric user-id allowlist, existing owner brain). Website is the primary autonomous sales channel. WhatsApp is a controlled continuation of a **verified website handoff** (`MIA_BUSINESS`); unknown/personal/`DO_NOT_AUTOMATE` contacts get no reply, no lead, no follow-up, no STT. Production `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=true`. Transport stays ADR-016 (Meta inbound, one outbound sender). Instagram is not a v1 autonomous conversation expansion. Hot `NextAction.HANDOFF` sets `HUMAN_TAKEOVER_REQUIRED`, cancels follow-ups, notifies Telegram when the owner bot is configured. Manual provider echo detection is **not** claimed (requires Meta coexistence + `smb_message_echoes`; not subscribed or parsed).

**Consequences**
Assaf talks to Mia in Telegram. Customers start on the website. A click-to-chat token continues the same lead on WhatsApp. Friends who message the Business number hear silence. Unit tests that still drive sales over WhatsApp as a transport set `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=false` in `tests/conftest.py`; communication-model tests turn the gate on.

**Alternatives considered**
Composio as sole WhatsApp provider — rejected (ADR-016). Classify arbitrary WhatsApp threads as leads from business-like language — rejected. Dual Meta+Composio send — forbidden. TTS — out of v1. Instagram as a fourth sales inbox — deferred.
