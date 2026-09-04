# ADR-004 Keep PRD living while building

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** KEEP (chat: always update PRD while working; wire features so they are alive)

**Context**
Provider facts drift (Composio catalog size, changelog URL). A frozen markdown copy of the Bible goes stale in hours. Assaf also asked that features be wired and alive, not documented then left disconnected.

**Decision**
`docs/PRD.md` is updated in the same turn as any contract, provider-fact, or capability-status change. Runtime wiring status lives in `app/core/capabilities.py` and is mirrored in the PRD table. A capability is not done until a test proves the path.

**Consequences**
PRD and code cannot diverge silently. `.docx` remains historical baseline; markdown PRD is the working spec. More frequent PRD diffs.

**Alternatives considered**
Wait for Assaf KEEP before every appendix patch — rejected by this instruction.
