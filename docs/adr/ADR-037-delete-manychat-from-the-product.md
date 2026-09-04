# ADR-037 Delete ManyChat from the product

> Renumbered from ADR-032 in the `mia:20` merge (see ADR-034).

- **Status:** accepted
- **Date:** 2026-08-25
- **Assaf:** ADOPT (chat: delete all ManyChat integration)

**Context**
ManyChat was never mounted in v1 (ADR-021). Assaf asked to remove it from the product, not leave it as a deferred sidecar. The app still declared `manychat` specified, injected `MIA_MANYCHAT_INGEST_TOKEN` in the ECS example, and documented a leftover AWS secret.

**Decision**
Remove ManyChat from runtime code, capability map, health, `.env.example`, and ECS/secret examples. Instagram senders stay `direct` | `composio`. Unused `channel_identities` columns remain in Postgres so existing databases do not need a drop migration. This repo does not read or delete a leftover AWS secret name.

**Consequences**
`POST /v1/manychat/external-request` stays gone (404). New task revisions must not inject `MIA_MANYCHAT_INGEST_TOKEN`. Do not remount ManyChat.

**Alternatives considered**
Keep the unused AWS secret documented forever — rejected for new deploys; the live box is not touched from this repo. Drop leftover identity columns now — rejected; avoid a production schema change for empty columns.
