# ADR-048 Rebuild Mia as a Dude clone with Contacts/Activity CRM

- **Status:** accepted
- **Date:** 2026-09-02
- **Assaf:** ADOPT (chat via Dude, named YES)

**Context**
Owner Telegram was buried under a task classifier, kill-switch, and ClientGraph
ceremony. The website minted `lead_` ids, wrote `01 Leads`, and showed WhatsApp
before identity existed. Assaf asked for a Dude clone: talk freely on Telegram,
log contacts to one locked Sheet, identify-then-sell on the site.

**Decision**
Replace runtime OwnerGraph/ClientGraph with two simple loops. CRM is the locked
spreadsheet `1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI`, tabs Contacts and
Activity only. Contacts headers are A1:N1 with `תאריך` after `אימייל`. No row
without phone or email. No lead IDs. No `01 Leads`. Kill-switch does not 503
owner talk or site chat. WhatsApp is offered only after phone or email. Site
pings Assaf on Telegram after capture. LangGraph stays in-repo as leftover
ceremony; the live paths do not invoke it.

**Consequences**
Telegram talk and Sheet writes work without stacked policy. Visitors cannot
open WhatsApp or create a CRM row anonymously. Old NBA/qualification tests no
longer describe the live site path. Owner Telegram uses `crm_search` /
`crm_upsert` on the locked workbook and never asks Assaf for a Sheet URL.
Empty `MIA_SHEETS_SPREADSHEET_ID` still resolves to the locked ID. House
Composio ports bind from `MIA_COMPOSIO_USER_ID`. Live workbook is Contacts + Activity only; archive tabs are gone.

**Alternatives considered**
Wait for a new schema — rejected; Assaf locked A1:N1. Keep LangGraph wrappers
— rejected as ceremony after verification. A second rebuild PR — rejected;
this branch is the rebuild.
