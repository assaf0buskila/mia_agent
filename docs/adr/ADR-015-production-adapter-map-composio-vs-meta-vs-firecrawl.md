# ADR-015 Production adapter map (Composio vs Meta vs Firecrawl)

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat/goal: lock production suppliers; clean unused)

**Context**
Assaf locked a production supplier map. ADR-007 still forbids dumping catalogs into the model. Official Composio Instagram tools cover send, list, and insights — **no inbound DM trigger**. Composio `LINKEDIN_GET_SHARE_STATS` is organization-page analytics only (ADR-009). Composio WhatsApp inbound is still a no-op poll (ADR-006).

**Decision**

| Job | Production adapter |
| --- | --- |
| Brain | LangGraph |
| Data (SoR) | Postgres |
| WhatsApp inbound | Meta Cloud API webhook (HMAC) — ADR-016 |
| WhatsApp send | `MIA_WHATSAPP_SENDER=direct` (Graph) or `composio` (`WHATSAPP_SEND_MESSAGE`); never both |
| Instagram **inbound** | Meta webhook HMAC (same reason as WhatsApp) |
| Instagram **send + organic insights** | Composio Instagram (pins when adapters land; Graph tokens stay until then) |
| Gmail ingest | Composio |
| Calendar | Composio |
| Sheets mirror | Composio |
| LinkedIn profile | Composio |
| LinkedIn personal member post analytics | Direct REST + `MIA_LINKEDIN_ACCESS_TOKEN` (ADR-009). Composio has org share-stats only. |
| Meta Ads **read** | Composio |
| Research | Firecrawl primary; Apify `google-search-scraper` behind the same `ResearchPort` when Firecrawl is unset (ADR-030) |
| ManyChat | Not mounted in v1 (ADR-021). Leftover AWS secret name stays in the box; app ignores it. |
| Composio WhatsApp toolkit | Send pin `WHATSAPP_SEND_MESSAGE` only when sender=`composio`. No inbound trigger. Template send not wired. |

One Instagram sender per conversation (`direct` or `composio`). Never dual-send Graph + Composio. ManyChat is unmounted (ADR-021).

**Consequences**
Env/docs/JSON list Meta tokens for WhatsApp and for Instagram **webhook verify**. Composio key+user covers Gmail/Calendar/Sheets/GSC/GA4/LinkedIn profile/Meta ads/future IG send+insights. Member analytics stays Direct REST (ADR-009); Composio has org share-stats only. GSC/GA4/Meta ads resource ids are leftover env, optional only when `MIA_COMPOSIO_DISCOVERY=true` (ADR-027). Sheets id stays explicit. Apify token is `MIA_APIFY_TOKEN` (ADR-030). Do not rip live Graph IG send until the Composio port is tested.

**Alternatives considered**
Composio for WhatsApp or Instagram inbound — rejected; no usable inbound trigger. Composio `LINKEDIN_GET_SHARE_STATS` for personal analytics — rejected; org URN only (ADR-027). Default-everything-Composio including catalogs in the model — rejected (ADR-007). WhatsApp send via Composio is ADR-016.
