# ADR-003 Finish Phase 0 control docs before pyproject.toml

- **Status:** proposed
- **Date:** 2026-08-21
- **Assaf:** unset

**Context**
Bible §45.2 lists `pyproject.toml` immediately after `AGENTS.md` and `BUILD_STATUS.md`. Bible §45.0 requires `DECISIONS.md`, `docs/PRD.md`, and a provider-capability matrix before feature coding.

**Decision**
Remaining Phase 0 order: this file, then `docs/PRD.md`, then the provider-capability matrix, then `pyproject.toml`.

**Consequences**
Toolchain comes after the spec is in-repo and decisions have a home. Slightly slower to first `pip install`. Less chance of coding against an unreadable `.docx` and unrecorded defaults.

**Alternatives considered**
Follow §45.2 literally and write `pyproject.toml` next — faster scaffolding, control docs still missing while the toolchain appears.
