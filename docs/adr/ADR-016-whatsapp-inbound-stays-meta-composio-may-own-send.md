# ADR-016 WhatsApp inbound stays Meta; Composio may own send

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat: migrate WhatsApp to Composio if the complete production flow exists)

**Context**
Assaf asked to make Composio the preferred WhatsApp Business layer. Re-check on 2026-08-22: official toolkit `WHATSAPP` version `20260815_00`, managed app **Yes**, 57 tools, **one** trigger `WHATSAPP_MESSAGE_STATUS_UPDATED_TRIGGER` (delivery status poll; Composio documents WhatsApp has no native status-poll API so the trigger is empty). Live MCP `COMPOSIO_GET_TOOL_SCHEMAS` + `docs.composio.dev/tools/whatsapp` match. `WHATSAPP_GET_MESSAGE_HISTORY` returns delivery-status audit rows (`id`, `message_id`, `events.delivery_status`) — no customer text, media, sender, or inbound/outbound body. Connected Composio account had **no** active WhatsApp connection. Third-party toolkits (Waboxapp, Mocean, Kapso) are extra vendors, not the official Cloud API toolkit. ADR-006 still holds for ingress.

**Decision**
Do **not** fake an inbound-message trigger. Do **not** poll `WHATSAPP_GET_MESSAGE_HISTORY` as an inbox. Keep Meta webhook as the thin inbound transport (`POST /v1/whatsapp/webhook`, HMAC, message ids, STT media via Graph). Composio webhook continues to ignore WhatsApp slugs (status updates are not customer messages). Outbound: one sender via `MIA_WHATSAPP_SENDER` (`direct` default | `composio`). Composio pin `WHATSAPP_SEND_MESSAGE` toolkit `20260815_00`; requires `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` + `MIA_WHATSAPP_PHONE_NUMBER_ID`. `WHATSAPP_SEND_TEMPLATE_MESSAGE` is not wired (no mass outreach). Dual Meta+Composio send is forbidden. Meta verify/app-secret/access-token stay until inbound (and Graph media) no longer need them. Shadow, owner acks, idempotency, and LangGraph unchanged.

**Consequences**
Near-real-time inbound stays Meta. Composio can own send auth/token refresh after Assaf connects WhatsApp on the existing `MIA_COMPOSIO_USER_ID`. Health reports `whatsapp_ingest` only for a working Meta inbound path, not because a Composio API key exists. Phone number id remains required for Composio send.

**Alternatives considered**
Composio as sole inbound+outbound — rejected; no incoming-message trigger. History polling worker — rejected; tool is delivery receipts, not an inbox. Waboxapp/Mocean/Kapso inbound — rejected extra vendors (ADR-006). Rip Meta webhook now — rejected; would lose customer messages.
