# Phase 1.6 sanitized live pre-deployment evidence

Date: 2026-08-31 (Asia/Jerusalem)

Scope: read-only production diagnosis performed before the Phase 1.6 code release.
This artifact records only resource names, booleans, result counts, and exit status.
It contains no credential value, account id, ARN, token, transcript, email content, KPI
value, or Sheet cell content.

## Reproducible boundaries

- Region was pinned to the project-selected `eu-north-1`.
- Secret inspection compared configured field names and equality/status booleans in
  memory. Secret values were never printed or written to the repository.
- Provider probes used one-off ECS tasks based on the active `mia` task definition and
  printed only `<CAPABILITY>_OK`, result counts, adapter error class, and exit code.
- Composio account diagnosis used the v3 connected-accounts and direct tool-execution
  endpoints with the production API credential held in process memory. Responses were
  reduced to toolkit names, active-state counts, HTTP status, success boolean, and row
  count before display.

## Observed evidence

- The configured `MIA_SHEETS_SPREADSHEET_ID` exactly matched
  `1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI`.
- The workbook title was `Mia — AssafWeb operating mirror`; eleven tabs were visible.
  The bounded read target `10 Mia Activity!Z236` existed, was blank, and had no data
  validation. No write was attempted.
- A production-shaped one-off task using the current secret and Sheet adapter read that
  exact target with exit code 0, `READ_OK=true`, and zero returned rows.
- Eight Composio connected accounts were ACTIVE for one actual Composio user:
  Gmail, Google Analytics, Google Search Console, Google Calendar, Google Sheets,
  Instagram, LinkedIn, and WhatsApp. The previously configured user matched none of
  those accounts and reproduced the provider's no-connected-account 404.
- `mia/prod` was rebound to the sole user owning all eight ACTIVE accounts. The update
  changed only `MIA_COMPOSIO_USER_ID`; hashes for the other 31 fields were unchanged,
  and the previous secret version remained retained for recovery.
- After the rebind, one-off adapter probes returned Gmail 1 result, Calendar 1 result,
  LinkedIn 1 result, and the exact Sheet read above. A direct completed-28-day provider
  probe returned Search Console success with 3 rows and GA4 success with 1 row.
- Short-window successful provider responses contained no rows. Before the tracked
  Phase 1.6 parser repair, the adapters classified those legitimate shapes as schema
  errors; therefore the repaired no-data behavior is local-only until the new exact SHA
  is deployed and re-probed.
- The then-active service was stable on task definition `mia:29`, desired/running 1/1,
  with deployment circuit breaker rollback enabled. Public live/ready/aggregate health
  returned HTTP 200 for the previous release.

## Explicit non-claims

- No Google Sheet write has occurred. It still requires a fresh authenticated owner
  Telegram request naming the exact spreadsheet, bounded range, and literal value.
- No live Telegram owner voice note has exercised the new code.
- No live website WhatsApp CTA click has exercised the new one-card Telegram path.
- The Phase 1.6 code in the working tree was not deployed when this evidence was
  recorded. Post-deployment health and provider probes remain required.
