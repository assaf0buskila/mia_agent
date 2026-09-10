# ADR-056 Complete LinkedIn profile reads and approval routing

- **Status:** accepted
- **Date:** 2026-09-10
- **Assaf:** ADOPT (chat: fix full-profile tool behavior, check tools, and deploy)

**Context**
The pinned LinkedIn adapter reduced provider data to name and headline. A full-profile
request could stop after that summary or reuse history. The generic Composio tool advertised
automatic approval preparation but refused two LinkedIn publishing slugs before routing.

**Decision**
Preserve bounded, explicitly supported profile sections through the adapter and capability.
Explicit full LinkedIn profile requests perform a fresh profile read and bounded profile-tool
discovery before the model answers. Missing sections remain unavailable rather than filled
from memory. Additional tools still require active connection, schema validation and read policy.
Route known LinkedIn publishing slugs through the existing LinkedIn approval preparation path.
No provider write occurs at preparation; execution still needs the exact Telegram approval.
Centralize generic proposal eligibility and check it again before approval execution.
Destructive actions remain forbidden, and named Gmail/Sheets write paths and Instagram's
analytics-only boundary cannot be bypassed through direct generic proposals.

**Consequences**
Full-profile requests cost two bounded preliminary reads and retain the owner deadline.
OAuth scope or provider limitations remain visible. This does not guarantee that every
Composio tool exists or that every profile section is exposed by the connected API.
Instagram publishing, customer messaging and existing authorization boundaries remain intact.

**Alternatives considered**
Prompt-only encouragement leaves both gaps in execution. Returning raw provider JSON risks
unbounded or sensitive fields. Automatic publishing bypasses the approval contract.
