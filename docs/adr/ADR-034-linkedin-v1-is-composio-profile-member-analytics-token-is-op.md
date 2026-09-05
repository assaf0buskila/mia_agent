# ADR-034 LinkedIn v1 is Composio profile; member-analytics token is optional

> Renumbered from ADR-028 when the VNext rebuild was merged with the shipped
> `mia:20` branch, which had already used 028–032 for different decisions.
> Production's ids win because they are cited in shipped code.

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** ADOPT (deploy: LinkedIn through Composio, no access-token key)

**Context**
ADR-009 still describes how member post analytics would work (direct REST + `MIA_LINKEDIN_ACCESS_TOKEN`). Composio still has no member analytics tool. Assaf's live LinkedIn connection is the profile toolkit. Requiring the leftover token listed it on `/health` as missing even though profile reads already work.

**Decision**
v1 owner LinkedIn is Composio `LINKEDIN_GET_MY_INFO`. Do not add `MIA_LINKEDIN_ACCESS_TOKEN` to go live. `/health` `owner_integrations.missing` does not list that token. `linkedin_analytics` stays false until a real member token exists. Do not fake member stats with org share-stats.

**Consequences**
Telegram can answer LinkedIn profile questions via the existing Composio port. Member analytics stays dark unless Assaf later supplies the leftover token. ADR-009 remains the analytics adapter if that token appears.

**Alternatives considered**
Keep listing the token as missing — rejected; it blocked a clean health picture for a capability Assaf is not shipping. Wire `LINKEDIN_GET_SHARE_STATS` as analytics — rejected; org URN, wrong job.
