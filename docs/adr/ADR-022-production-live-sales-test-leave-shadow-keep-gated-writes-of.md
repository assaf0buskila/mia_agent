# ADR-022 Production live sales test: leave shadow, keep gated writes off

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: leave shadow; give Mia full v1 sales capability; check safety before new image)

**Context**
Production `mia:9` was `MIA_AUTOMATION_MODE=shadow`. Website already replied. Verified WhatsApp handoff did not send. Assaf started live testing and rejected the silent WhatsApp continuation. Literal “unlock every flag” would open Gmail send, Meta writes, and Instagram as a sales inbox.

**Decision**
Production may run `MIA_AUTOMATION_MODE=auto_approved` so verified website→WhatsApp continuation can send. `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=true` stays. Instagram prospect send stays off unless `MIA_AUTO_REPLY_INSTAGRAM=true` (code gate, not shadow). Gmail send, Meta writes, follow-up send, browser automation, dynamic tool discovery, R4 auto, and R5 stay denied. Kill switch still stops all gated actions.

**Consequences**
Friends / unknown WhatsApp numbers still get silence. A valid `mia1_` handoff token continues the website lead and Mia may reply. `/health` shows `automation_mode=auto_approved` and `auto_reply_instagram=false`. Roll back prospect DMs with `MIA_AUTOMATION_MODE=shadow` plus a new task revision.

**Alternatives considered**
Keep shadow and only flip `MIA_WHATSAPP_HANDOFF_SEND` — safer, but Assaf asked to leave shadow. Flip every write flag — rejected. `auto_approved` without an Instagram send gate — rejected; that would make Instagram a v1 sales inbox.
