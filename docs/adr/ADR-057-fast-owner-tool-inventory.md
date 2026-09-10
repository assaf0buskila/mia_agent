# ADR-057 Fast owner tool inventory and explicit context exclusion

- **Status:** accepted
- **Date:** 2026-09-10
- **Assaf:** ADOPT (chat: make Mia faster following a Telegram tool-list timeout)

**Context**
The live Telegram surface sent a simple capabilities question through retrieval,
the model, and a full multi-provider audit. The next model call failed and the user
received a timeout. A current tool inventory is already available in the registry.

**Decision**
Recognize explicit tool-inventory-only requests after existing owner authorization and
kill checks. Render the current registered tools directly, without asserting that every
connection was tested live. Keep execution, audits and compound requests in the normal
owner flow. Pure inventory turns skip history retrieval, model calls and learning.
Honor explicit no-history requests when preparing the current message and assembling
owner context. Preserve the configured model and existing tool/write policy.

The active Telegram surface must retain the current turn's exact approval IDs; unrelated
pending approvals must not be substituted into arbitrary replies. This completes the
existing ADR-056 delivery contract on the live surface.

**Consequences**
Inventory latency no longer depends on model or provider response times. Other requests
retain context and reasoning unless explicitly excluded. Tests must call the active
surface, prove zero expensive calls for inventory, and keep execution and compound
requests out of the fast path. Broader parallel audits remain future work.
