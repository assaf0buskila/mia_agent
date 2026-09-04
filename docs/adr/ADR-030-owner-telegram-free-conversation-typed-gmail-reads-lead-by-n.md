# ADR-030 Owner Telegram: free conversation, typed Gmail reads, lead by name

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** ADOPT (chat: go implement)

**Context**
After mia:18 the Telegram console still dumped funnel/engine/daily on greetings and on real requests like "תבדקי את המייל". Unmatched long text was coerced to `OPERATOR_SNAPSHOT`. Gmail reads could not list the inbox (`GmailPort` only fetched by message id). Leads showed as hashes. Assaf rejected dumping the Composio catalog into the model; he asked for a typed allowlist, inbox list/search/read, send as draft+Approve, and lead lookup by person name when they said it.

**Decision**
Greetings and ≤3-word chatter stay a one-line hello (`היי אסף, אני כאן.`) and never hit the agent or the digest. Long unmatched text stays `NOTE` so the owner agent answers. Snapshot/funnel/engine only on an explicit brief. Extend `GmailPort` with `list_recent` / `search` / `create_draft` / `send_draft`, pinning `GMAIL_FETCH_EMAILS`, `GMAIL_CREATE_EMAIL_DRAFT`, `GMAIL_SEND_DRAFT` on toolkit `20260817_00`. Agent tools: `gmail_inbox`, `gmail_search`, `gmail_read`, `find_leads`. No send/delete on the agent registry. Draft is a deterministic `gmail_draft` task; send runs only after Approve and `MIA_GMAIL_SEND=true` (stays false). Persist `SalesState.display_name` only from explicit name phrases (`שמי X`, `my name is`); never guess. `find_leads` matches name, headline, or full `lead_…`.

**Consequences**
"תבדקי את המייל" can reach inbox tools instead of a canned dump. Send cannot happen from the model. Empty names stay empty until stated; headlines still work. Shipped image **mia:19**, task **mia:21** after migrate exit 0. Rollback: image `mia:18` / task `mia:20`, or blank `MIA_OWNER_AGENT_MODEL`.

**Alternatives considered**
Expose all Composio Gmail tools to the model — rejected (ADR-007). Auto-send after draft — rejected; Approve plus the existing send flag. Infer names from headlines — rejected; Assaf chose stated names only.
