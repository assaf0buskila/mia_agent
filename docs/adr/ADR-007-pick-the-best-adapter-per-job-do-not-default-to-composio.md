# ADR-007 Pick the best adapter per job; do not default to Composio

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT (chat: always pick the best API/tool/workflow)

**Context**
Bible §23 says Composio-first behind typed ports. That is a supplier preference, not a quality function. Asking KEEP/ADOPT on every toolkit slows the build and still needs a rubric.

**Decision**
For each capability, choose the adapter that wins on: (1) safety and one-sender, (2) official completeness for that job, (3) latency and failure mode we control, (4) OAuth/token-refresh burden, (5) do not rip out an alive path for fashion, (6) pin schemas — never dump a catalog into the model, (7) cost and lock-in. Composio wins when it removes OAuth/token pain (Gmail, Calendar, Sheets, LinkedIn read). Direct official APIs win for Meta webhooks we already own (WhatsApp, Instagram DMs) and for STT (`gpt-transcribe`). LangGraph stays the orchestrator; Composio Tool Router does not. Models stay eval-driven: Luna for sales, Grok 4.6 for deep research, gpt-transcribe for voice input. No TTS.

**Consequences**
Faster adapter choices. Assaf is asked only when the pick changes safety, permissions, or dual-send. A “best” pick can still be reversed with a Better-Way + ADR if evidence changes. Production supplier map is **ADR-015**.

**Alternatives considered**
Composio for every channel — rejected; WhatsApp trigger gap and dual-send risk. Direct Google APIs first — higher OAuth cost for the same typed ports. Re-ask Assaf per toolkit — rejected by this instruction.
