# ADR-038 Graphs own retrieve and conversation complete

> Renumbered from ADR-033 when the Phase L cleanup was merged: 028–032 belong to
> shipped production decisions and 033 is reserved for the in-flight Gmail-send
> slice on `claude/mia-adr033-wip`.

- **Status:** accepted
- **Date:** 2026-08-25
- **Assaf:** ADOPT (chat: graphs must include the functions Mia needs to be functional)

**Context**
OwnerGraph was `load_owner_context → respond`. ClientGraph was `load_conversation → sales_turn`. Knowledge search, hot handoff, and website finalization (widget close, inactivity, human handoff) ran in HTTP handlers and `mia-due-scan` after `graph.invoke`. Looking at the graphs did not match what Mia did, so those product functions were easy to miss or skip.

**Decision**
1. ClientGraph nodes: `load_conversation` → `retrieve_knowledge` (`knowledge.search` as `GraphName.CLIENT`) → `sales_turn` or skip on `session_end` / `inactivity` → `complete_turn` (hot handoff + website finalize).
2. Website `/end` and due-scan inactivity invoke ClientGraph with `turn_kind`, they do not call the finalization service as a side path.
3. OwnerGraph nodes: `load_owner_context` → `retrieve_owner_knowledge` (`memory.search` + `knowledge.search` as owner) → `respond`. Mail, calendar, leads, and research stay allowlisted tools inside `respond`.
4. Channels stay thin. STT, HMAC, and Sheets mirror stay outside LangGraph. Graph state stays serializable. Website visitors still cannot execute owner capabilities.

**Consequences**
Published AssafWeb facts from `knowledge.search` are passed into sales compose as labelled data. Conversation-complete pings still use the same idempotent finalization service; the graph is the caller. Do not dump the Composio catalog into extra graph nodes.

**Alternatives considered**
Leave retrieve/finalize in HTTP and due-scan — rejected; the graphs would not include the functions. One LangGraph node per Composio slug — rejected (ADR-036).
