# ADR-043 Owner-only on-demand Composio tool breadth

- **Status:** accepted
- **Date:** 2026-08-31
- **Assaf:** ADOPT (chat)

**Context**
Assaf enables OAuth/toolkit guardrails in Composio and wants Owner Mia to use enabled tools
without maintaining a handwritten list. A raw catalog in a model prompt would be slow, stale,
and would let untrusted retrieved text steer a broad provider surface. Treating every provider
tool as a low-risk read would bypass Mia's kill switch, risk policy, approval, idempotency, and
audit contracts.

**Decision**
OwnerGraph exposes three on-demand meta-tools: search only ACTIVE toolkits connected to
`MIA_COMPOSIO_USER_ID`, fetch one selected tool's bounded current input schema, then execute a
locally schema-preflighted read recognized by the conservative classifier. Listings and schemas
are process-cached; unfamiliar actions and oversized schemas fail closed, and the catalog is
never attached to every model call. The request `Principal` gates every meta-tool and ClientGraph
receives none. Python classifies selected slugs: destructive operations are R5 and denied;
send/write/post/marketing/unknown operations do not execute generically. They require a named
workflow with explicit approval, idempotency, and audit before enablement. Kill switch checks run
before catalog operations and provider execution.

**Consequences**
Mia can dynamically use the complete authorized read surface of every active Composio toolkit,
including future toolkits, without prompt-catalog maintenance. Broad side-effect execution remains
intentionally open: OAuth alone cannot bind provider writes to Mia's approval and idempotency
records. Existing named reads and approved bounded Sheets updates remain unchanged.

**Alternatives considered**
Expose every tool definition to the model — rejected for prompt size, drift, and authority
confusion. Let the model label a tool read/write — rejected because model text is not policy.
Generic approval followed by generic execute — rejected because it cannot bind the provider action
to existing named approval, idempotency, and audit records. Proxy execution — rejected because it
bypasses tool schemas and modifiers.
