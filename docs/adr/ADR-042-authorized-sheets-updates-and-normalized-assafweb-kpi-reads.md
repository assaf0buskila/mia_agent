# ADR-042 Authorized Sheets updates and normalized AssafWeb KPI reads

- **Status:** accepted
- **Date:** 2026-08-28
- **Assaf:** ADOPT (chat)

**Context**
Assaf needs Mia to maintain a small set of explicitly authorized Google Sheets and to
answer AssafWeb KPI questions from Search Console and GA4. The prior mirror-only wording
correctly protected Postgres as the system of record, but it incorrectly excluded an
owner-requested, bounded Sheets update. Broad Drive discovery or a model-selected spreadsheet
would create an unacceptable wrong-document write risk.

**Decision**
Mia may read, and may make bounded value updates or appends to, only spreadsheet IDs explicitly
configured or allowlisted by Assaf. There is no arbitrary Drive discovery. Sheets reads are
`READ`; an update or append is a low-risk, policy-controlled operation only after an explicit,
authenticated owner request. Every action is a named capability authorized with the request
`Principal`; kill switch and idempotency apply. This slice excludes create, delete, clear,
formatting, and formula generation.

GSC and GA4 remain API-backed owner reads, never browser automation. Their owner tools normalize
AssafWeb KPIs before answering: GA4 traffic, users, sessions, conversions, and pages; GSC clicks,
impressions, CTR, position, and queries. LinkedIn remains profile-only.

**Consequences**
Sheets is an authorized operational surface, not Mia's internal database: Postgres remains the
system of record and Mia never reads Sheets back as truth for state, decisions, or recovery.
Website visitors do not inherit these capabilities. Implementations must add a named capability,
policy allowance, typed adapter, owner allowlist, and tests before any live Sheets action exists.

**Alternatives considered**
Use Sheets as the system of record or read it back into Mia state — rejected; it loses the
transactional, tenant-safe Postgres boundary. Let the model search Drive or choose by name —
rejected; explicit IDs are the authorization boundary. Use browser automation for Google metrics
— rejected; stable API responses are the required integration surface. Allow general sheet editing
or formula/format generation — deferred; this decision permits only bounded values updates/appends.
