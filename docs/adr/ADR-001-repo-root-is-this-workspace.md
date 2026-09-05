# ADR-001 Repo root is this workspace

- **Status:** proposed
- **Date:** 2026-08-21
- **Assaf:** unset

**Context**
The Bible shows a `mia/` directory as the repo tree. The Cursor workspace is already `assaf_agent`.

**Decision**
Use this workspace as the project root. Do not create a nested `mia/` folder.

**Consequences**
`AGENTS.md`, `app/`, `docs/`, and `tests/` live at the workspace root. Paths in the Bible that start with `mia/` map to this root. Import and deploy paths stay one level shallower.

**Alternatives considered**
Create `mia/` inside the workspace — matches the Bible diagram, adds a useless extra directory for every path and tool.
