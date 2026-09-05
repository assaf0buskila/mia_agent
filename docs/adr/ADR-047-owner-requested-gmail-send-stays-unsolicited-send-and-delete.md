# ADR-047 Owner-requested Gmail send stays; unsolicited send and delete-forever stay denied

- **Status:** accepted
- **Date:** 2026-09-01
- **Assaf:** ADOPT (chat via Dude)

**Context**
PR 10 looked like it might deny every Gmail send slug. Assaf's mail policy is not
"never send": Mia must not send on her own, but if the owner asks on Telegram to
write and send mail she must draft and send. Official Composio Gmail send slugs
are `GMAIL_SEND_EMAIL`, `GMAIL_SEND_DRAFT`, `GMAIL_REPLY_TO_THREAD`, and
`GMAIL_FORWARD_MESSAGE`. There is no `GMAIL_SEND`. Delete-forever class is
`GMAIL_DELETE_MESSAGE`, `GMAIL_BATCH_DELETE_MESSAGES`, `GMAIL_DELETE_THREAD`,
plus `GMAIL_DELETE_DRAFT` / `GMAIL_DELETE_FILTER` / `GMAIL_DELETE_LABEL`.
Trash (`GMAIL_MOVE_TO_TRASH`, `GMAIL_MOVE_THREAD_TO_TRASH`) is recoverable.
Google Analytics has no DELETE slug. Google Search on Composio is Search Console;
the only delete is `GOOGLE_SEARCH_CONSOLE_DELETE_SITE`.

**Decision**
Keep the named owner Telegram path: draft (`GMAIL_CREATE_EMAIL_DRAFT`) then
Approve then `GMAIL_SEND_DRAFT` when `MIA_GMAIL_SEND` is on. Pin those proven
adapter slugs. Do not put Gmail send slugs on the destructive denylist. Generic
`composio_execute_tool`, cron, website visitors, and marketing blasts never
auto-fire send slugs. The owner LLM registry still has no `gmail_send` tool —
the model never sends; Python does after the owner asked and approved.
Deny the official Gmail delete-forever class and `GOOGLE_SEARCH_CONSOLE_DELETE_SITE`.
Pin already-wired Gmail reads/draft/send-draft, GA4 reads including
`GOOGLE_ANALYTICS_LIST_ACCOUNT_SUMMARIES`, and existing Search Console reads.
Do not invent 63/69 pins. Do not pin `GMAIL_SEND_EMAIL` until an adapter uses
it. `GOOGLE_ANALYTICS_SEND_EVENTS` stays unpinned and never auto-fires.
Trash is not delete-forever. `GOOGLE_SEARCH_CONSOLE_ADD_SITE` /
`SUBMIT_SITEMAP` are owner writes, not visitor, not auto, not pinned until an
adapter exists. No `GOOGLE_SEARCH` / SERPAPI toolkit. GA
`ARCHIVE_CUSTOM_DIMENSION` is archive, not denied as delete.

**Consequences**
Owner-requested mail on Telegram can send. Visitors and unsolicited paths cannot.
Gmail was not removed; Calendar, Sheets, LinkedIn, GA, and Search Console stay.
No new Composio app. No silent marketing mail.

**Alternatives considered**
Deny every Gmail send slug — rejected; that would break owner-requested send.
Give the LLM a `gmail_send` tool — rejected; the model must not send.
Flip `MIA_GMAIL_SEND` default to true in this PR — deferred; production flag
stays an ops choice. The code path is kept.
