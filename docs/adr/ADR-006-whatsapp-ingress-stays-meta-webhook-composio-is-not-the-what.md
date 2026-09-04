# ADR-006 WhatsApp ingress stays Meta webhook; Composio is not the WhatsApp brain

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** KEEP (chat: always pick the best API/tool/workflow)

**Context**
Bible §17.1 and §23 allow Cloud API *or* Composio behind typed adapters. Mia already has a live WhatsApp path: Meta HMAC webhook, idempotent claim, sales graph, R2-gated send, in-memory media download, GPT Transcribe. Assaf asked to use Composio for WhatsApp Business. Official Composio toolkit `WHATSAPP` (version `20260815_00`) has 57 tools, managed app **Yes**, and **one trigger**. That trigger is `WHATSAPP_MESSAGE_STATUS_UPDATED_TRIGGER` (poll). Composio documents that WhatsApp has no native status-poll API, so the trigger returns empty; inbound messages are **not** a Composio trigger. Status/inbound still require a Meta webhook. `WHATSAPP_CONFIGURE_CONVERSATIONAL_AUTOMATION` would install Meta away/welcome bots and dual-send against Mia.

**Decision**
KEEP the live Meta webhook + direct Graph `MessagePort` / media port as WhatsApp ingress and session replies. Do not give the model the 57-tool catalog. Composio WhatsApp, if used later, is a second implementation of the **same** typed ports, pinned to a short allowlist (`WHATSAPP_SEND_MESSAGE`, `WHATSAPP_GET_MEDIA_INFO`, `WHATSAPP_SEND_TEMPLATE_MESSAGE` under approval). First Composio adapters should be Gmail / Calendar / Sheets, where OAuth is the actual pain. Production WhatsApp credentials stay Assaf-owned Meta tokens, not Composio’s managed app.

**Consequences**
WhatsApp latency and HMAC stay in our process. Template/admin tools can be added without rewriting sales. Extra hop and schema-pin work if we later wrap send in Composio. Cannot use Composio as the inbound event bus for WhatsApp.

**Alternatives considered**
Replace webhook with Composio triggers — rejected; the only WhatsApp trigger is a documented no-op poll for status. Point LangGraph at all 57 tools — rejected; violates pin-schema, risk policy, and dual-send rules. Wati/Spoki/1msg Composio toolkits — rejected extra vendors.
