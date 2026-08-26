# Mia wiring table

**Generated 2026-08-24** from the live host (`GET https://mia.assafweb.com/health`) plus
`app/core/capabilities.py`. Regenerate any time with the **`mia-status`** skill.

Live: 68 capabilities · `kill_switch=false` · `ops.integration_failures=0`.

## Wired and working

| Capability | Evidence |
|---|---|
| Website Ask Mia + sales discovery | Live widget; multi-turn probe gives distinct replies, no restart |
| Website voice input (STT) | `brain.voice_in.ready=true`; mic in composer |
| Composio connection | `owner_integrations.composio=true` |
| Gmail read / classify / summarize | `gmail_read=true` |
| Calendar read **and** gated write | `calendar_read=true`, `calendar_write=true` |
| Sheets mirror | `sheets_mirror=true` |
| LinkedIn **profile** read | `linkedin_profile=true` |
| Instagram insights | `instagram_insights=true` |
| Search Console | `search_console=true` |
| GA4 | `ga4=true` |
| Research (Firecrawl) | `research_firecrawl=true` |
| Brain: memory + embeddings + extraction | all `ready=true`; corpus 31 knowledge chunks |
| Telegram owner channel + voice in | `telegram_owner=true`, `voice_in.ready=true` |
| Postgres SoR, risk policy, kill switch | `postgres=true`, R4 approval / R5 deny |

## NOT wired — missing configuration

| Capability | Missing | Effect |
|---|---|---|
| LinkedIn **member post analytics** | `MIA_LINKEDIN_ACCESS_TOKEN` | `linkedin_analytics=false`. Composio cannot substitute — its toolkit has org page stats only (ADR-015) |
| Meta ads insights | `MIA_META_ADS_ACCOUNT_ID` | `ads_snapshot` returns "not connected" |

## Gated OFF on purpose — leave them

| Flag | State | Why |
|---|---|---|
| `whatsapp_handoff_send` | false | Assaf handles WhatsApp himself until Cloud API inbound is proven (ADR-024) |
| `gmail_send` | false on live mia:20; true after ADR-033 deploy | Draft + Approve still required. Tests keep Settings default false |
| `auto_reply_instagram` | false | Cold IG DM spam is a hard never |
| `meta_write` | false | R4 approval, not an env knob |
| `MIA_COMPOSIO_DISCOVERY` | false | Turn on only after the discovery probe passes |
| TTS / voice output | absent | Hard never |
| ManyChat | unmounted | Out (ADR-033). Leftover DB columns / AWS secret wait for Assaf delete list |
| Apify | `research_apify` follows `MIA_APIFY_API_TOKEN` | Pinned `apify/google-search-scraper` behind `ResearchPort`. Firecrawl first |

## Unproven — configured but never demonstrated live

| Thing | Why it is unproven |
|---|---|
| **Telegram owner agent actually answering** | `/health` only checks the model string is non-empty. Live evidence showed the pre-brain classifier answering every turn. Prove it with the `owner_agent used=True` log line |
| Composio tools returning real data | Cannot be tested until the agent runs |
| Telegram voice end-to-end | Code fixed, `ready=true`, but no live voice note seen |
| Website meeting booking | Live OAuth CREATE never exercised |
| Composio resource discovery | All three list tools 404'd; root cause now understood and fixed (see below) |
