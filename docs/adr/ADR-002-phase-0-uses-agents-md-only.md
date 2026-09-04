# ADR-002 Phase 0 uses AGENTS.md only

- **Status:** proposed
- **Date:** 2026-08-21
- **Assaf:** unset

**Context**
Cursor supports `AGENTS.md` and glob-scoped `.cursor/rules/*.mdc`. Official Cursor docs: `AGENTS.md` is the simple root instruction file; `.mdc` is for scoped rules.

**Decision**
Phase 0 ships one root `AGENTS.md`. Do not add `.cursor/rules/*.mdc` until a repeated, file-pattern-specific failure justifies a scoped rule.

**Consequences**
One instruction surface. Less drift. Later Python/test/graph rules can be added as `.mdc` without rewriting this ADR; that would be a new ADR.

**Alternatives considered**
Create `.mdc` rules on day one — more Cursor control, splits instructions before the repo has files to scope.
