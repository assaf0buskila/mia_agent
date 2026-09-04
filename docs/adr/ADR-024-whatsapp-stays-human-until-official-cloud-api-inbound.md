# ADR-024 WhatsApp stays human until official Cloud API inbound

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: Composio cannot receive WhatsApp; handle the person himself until official API; Telegram gets the briefing)

**Context**
Click-to-chat pointed at Assaf's personal number. Meta Cloud API never delivered a customer message to Mia in 48h of production logs. Composio has no WhatsApp inbound-message trigger (ADR-016). Assaf tested from a second number and still got silence. He will handle WhatsApp himself until official Cloud API inbound exists.

**Decision**
`MIA_WHATSAPP_HANDOFF_SEND` stays false and now gates WhatsApp prospect send in every automation mode, including `auto_approved`. Website still talks. When the visitor clicks through, wa.me opens Assaf with a human Hebrew prefill (no `mia1_` token in the customer message) and Telegram receives a one-time briefing of the website conversation plus a paste-ready first WhatsApp line. Mia does not reply on WhatsApp. Flip the flag only after Cloud API inbound is proven.

**Consequences**
Customers are not ghosted: Assaf sees them. The ugly token is gone from the compose box. Identity binding via token is deferred until inbound works. ADR-022's "Mia may reply on verified handoff" is paused, not deleted.

**Alternatives considered**
Keep chasing Cloud API / Composio inbound — rejected; Composio cannot ingest WhatsApp messages. Put the token back in wa.me so a future webhook can bind — rejected for now; it made the handoff look broken. Global shadow — unnecessary; website and Telegram already send.
