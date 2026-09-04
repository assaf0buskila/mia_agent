# ADR-046 Official Composio destructive slugs stay denied; WhatsApp-move ping is a summary

- **Status:** accepted
- **Date:** 2026-09-01
- **Assaf:** ADOPT (chat)

**Context**
The on-demand Composio classifier was word-based and over-guardrailed bounded Sheets
upsert/update/append as unknown/commercial, while official destructive slugs (there is no
`delete-lead` tool) needed an explicit pin. Website WhatsApp-move already POSTs `/handoff`
and pings Telegram via `sendMessage`; the card dumped recent visitor turns.

**Decision**
Pin official destructive slugs as R5 deny: `GOOGLESHEETS_DELETE_DIMENSION` (delete-lead-row),
Sheets clear/delete-sheet/chart/`EXECUTE_SQL`, `INSTAGRAM_DELETE_COMMENT`,
`INSTAGRAM_DELETE_MESSAGGER_PROFILE`, `LINKEDIN_DELETE_POST`, `LINKEDIN_DELETE_UGC_POST`,
`LINKEDIN_DELETE_LINKED_IN_POST`. Adapter-pinned Sheets writes already in this repo
(`GOOGLESHEETS_UPSERT_ROWS`, `GOOGLESHEETS_VALUES_UPDATE`,
`GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND`) are R1 and stay on named allowlisted
`sheets.update` / `sheets.append` / CRM upsert — generic catalog execute does not run them.
Instagram/LinkedIn reads stay R0; publish/post slugs never auto-fire. LinkedIn non-delete
writes keep the existing one-tap approval path. WhatsApp-click owner ping keeps the paste
line and adds a flag-only summary (workflow, stage, next action, WhatsApp offered) plus
"someone moved to you"; it does not dump the transcript. Delivery remains
`POST https://api.telegram.org/bot<token>/sendMessage` to stored numeric owner chat ids.

**Consequences**
Assaf gets a usable heads-up when a visitor taps WhatsApp. Bounded Sheets writes are not
classified as deletes. Official row/sheet/social deletes stay denied. No new Composio app
is connected. No silent Instagram/LinkedIn publish.

**Alternatives considered**
Invent a `delete-lead` slug — rejected; official catalog has none. Generic-execute Sheets
writes from the catalog — rejected; that would skip the spreadsheet allowlist. Auto-publish
IG/LinkedIn because the tools exist — rejected. New Telegram webhook for the ping — rejected;
outbound `sendMessage` already exists.
