# ADR-052 Delete the dead Sheets mirror and the unread meeting-first flag

- **Status:** accepted
- **Date:** 2026-09-04
- **Assaf:** ADOPT (delete what is connected to nothing)

**Context**
`build_sheets_port` only ever returns `ComposioSheetsPort` or `DisabledSheetsPort`,
and on both every mirror upsert was literally `del row`. `FakeSheetsPort` was the
only implementation that stored a row, so the mirror wrote nothing in production
and was exercised solely by its own tests. `mirror_sales_turn` still ran on every
WhatsApp and Instagram turn: one claim write, roughly five reads, six no-op calls,
then a tool outcome and a claim completion. `/health` reported `sheets_mirror`
ALIVE. Separately `MIA_WEBSITE_MEETING_FIRST` (default true, ADR-028) had exactly
one reference in the repo, its own definition; nothing ever read it.

**Decision**
Delete the mirror stack: the eight `*MirrorRow` models, the eight `mirror_*`
writers, `mirror_sales_turn`, `maybe_mirror_weekly_kpi`,
`maybe_mirror_content_insights`, the claim helpers, the row sanitizers, the
archive tab constants, the `upsert_*` port methods on all four ports, both call
sites, the `sheets_mirror` capability, and the `sheets_mirror_content` tool name.
Delete the `website_meeting_first` setting. Keep `write_locked_contact`,
`append_locked_activity`, `read_locked_contacts` and `ensure_crm_workspace`
untouched: the locked Contacts and Activity workbook is the live CRM.

**Consequences**
`app/integrations/sheets.py` drops from 1556 to roughly 740 lines and every
inbound turn loses a claim write, five reads and a tool outcome that produced
nothing. `/health` stops advertising a writer that never wrote. The `meeting_first`
parameter on `select_next_action` and its `meeting_exit_offered` column stay, so
the ADR-028 logic is still readable; only the setting nothing read is gone. That
logic was already unreachable and remains so: the branch requires
`channel == "website"`, and the website runs `site_policy`, never the client
graph. Offering a meeting before WhatsApp is therefore a fresh decision to build
in the site surface, not a flag to flip.

**Alternatives considered**
Keep the mirror and only drop the call site — rejected; the cruft and the false
ALIVE status survive. Wire `website_meeting_first` into `empty_client_state`
instead — rejected; it would still never fire, because inbound passes
`channel="whatsapp"` and the website never reaches `select_next_action`.
