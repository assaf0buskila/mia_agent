# ADR-045 Complete-work owner actions and Mia-managed CRM workspace

- **Status:** accepted
- **Date:** 2026-08-31
- **Assaf:** ADOPT (chat: make Mia my number-one assistant; Mia owns her spreadsheet)

**Context**
Live owner checks exposed three product gaps. Mia stopped broad audits after partial results and
described an invented provider-call limit. Gmail and Calendar could read but their desired writes
were not complete approval workflows. The configured Google Sheet responded, yet an empty first
tab made Mia treat her own CRM workspace as an unknown external document. LinkedIn and Instagram
also exposed less of their active, useful surface than Assaf authorized. Website WhatsApp-click
requests reached the server, but a historical lead-wide notification claim could hide a new
conversation's Telegram alert.

**Decision**
Broad owner health requests use one bounded aggregate audit tool and return a factual status for
every defined surface; Mia never invents a "two calls" or generic provider-limit explanation.
Gmail draft send and Calendar create/reschedule are exact, hash-bound, expiring, idempotent
Telegram approvals. LinkedIn exposes all active connected reads and may propose schema-validated
non-destructive side effects for exact Telegram approval; delete/remove/revoke and direct-message
tools stay denied. Instagram remains analytics-only and falls back to individual metric requests
when a mixed metric call is unsupported.

`MIA_SHEETS_SPREADSHEET_ID` is Mia's managed CRM workspace. The adapter may add the fixed CRM tabs,
repair their headers, and continuously upsert leads, sources, follow-ups, meetings, deals, content
performance, weekly KPIs, and the Mia activity log. This authority applies to that one configured
spreadsheet only. There is no Drive discovery, spreadsheet create/delete, business-row clearing,
or formula generation. Postgres stays canonical and can rebuild the projections. Website
handoff-notification idempotency is conversation/session scoped, with aggregate delivery outcome
logging and no visitor content.

**Consequences**
Assaf does not maintain Mia's CRM workbook, and a blank or partially structured workbook is
repaired by Mia's background maintenance worker. Visitor requests never wait for this repair.
Mia can complete approved Gmail, Calendar, and LinkedIn actions
without opening a generic write proxy. Provider/API failures are reported per surface and remain
distinguishable from empty data. Returning leads may alert Assaf in a new conversation without
duplicating graph and click alerts inside the same session. The configured workbook remains an
operational view, not a second brain or recovery database.

**Alternatives considered**
Let Sheets become the system of record — rejected because manual edits and provider failures would
corrupt business state. Grant generic provider writes from OAuth alone — rejected because OAuth
does not supply Mia's approval, hash binding, expiry, kill-switch, or idempotency contracts. Ask
Assaf to name every tab/range forever — rejected because this is Mia's own managed workspace. Raise
the model step budget and keep many separate checks — rejected because it remains incomplete and
expensive; the aggregate audit is deterministic and gives one complete result.
