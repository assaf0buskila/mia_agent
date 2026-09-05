# ADR-021 Documentation core set; ManyChat not a v1 runtime channel

- **Status:** superseded
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: `/goal` simplify repository without breaking v1)
- **Superseded by:** ADR-031 (living agent docs). ManyChat remains unmounted.

**Context**
The repo accumulated overlapping MD files (PRD dump, HANDOFF, playbooks, gap reports) that burned agent context. ManyChat was an optional Instagram sidecar, not part of the ADR-017 v1 channel set.

**Decision**
Living docs are: `AGENTS.md`, `README.md`, `docs/PROJECT_MAP.md`, `docs/ARCHITECTURE.md`, `docs/PRD.md` (short), `docs/BUILD_STATUS.md`, `docs/RUNBOOK.md`, `docs/DECISIONS.md`, plus operator `docs/PRODUCTION_BUILD.md`. Historical material lives in `docs/archive/`. ManyChat HTTP ingest is unmounted. Instagram inbound webhook and insights stay (ADR-015); Instagram is still not a v1 sales inbox. Unused `MIA_MANYCHAT_INGEST_TOKEN` in AWS Secrets Manager is documented, not deleted.

**Consequences**
Agents load the map first. Do not grow `docs/PRD.md` back into a Bible dump. Do not remount ManyChat without a new ADR.

**Alternatives considered**
Delete Instagram inbound entirely — rejected; analytics/insights and ADR-015 inbound HMAC remain locked. Delete the ManyChat secret from AWS — rejected; document unused secrets only.
