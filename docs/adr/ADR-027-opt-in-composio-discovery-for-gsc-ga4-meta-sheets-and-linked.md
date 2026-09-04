# ADR-027 Opt-in Composio discovery for GSC/GA4/Meta; Sheets and LinkedIn stay explicit

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (completed Claude slice)

**Context**
Assaf already has Active Composio connections: Gmail, Calendar, LinkedIn (Assaf Buskila, PRIVATE), Instagram, GSC (siteOwner assafweb.com), GA4, Sheets, GitHub. Three leftover env vars (`MIA_GSC_SITE_URL`, `MIA_GA4_PROPERTY_ID`, `MIA_META_ADS_ACCOUNT_ID`) are ids those connections can list. Sheets is a write target. LinkedIn member post analytics has no Composio tool — `LINKEDIN_GET_SHARE_STATS` needs an organization URN. Firecrawl is not a Composio app. Ports are constructed per request, so a default-on list call would add network to every health/owner turn.

**Decision**
`MIA_COMPOSIO_DISCOVERY` is opt-in and defaults **false**. When true, and only when the matching leftover env is blank, resolve GSC via `GOOGLE_SEARCH_CONSOLE_LIST_SITES`, GA4 via `GOOGLE_ANALYTICS_LIST_ACCOUNT_SUMMARIES`, and Meta ads via `METAADS_GET_AD_ACCOUNTS`. Explicit env always wins. Never guess between unrelated candidates. Cache once per process — no list call inside per-request port construction when the flag is off. `GOOGLESHEETS_SEARCH_SPREADSHEETS` exists but is **not** used to pick a write target; `MIA_SHEETS_SPREADSHEET_ID` stays required. LinkedIn member analytics stays Direct REST + `MIA_LINKEDIN_ACCESS_TOKEN` (ADR-009). Do not fake personal analytics with org share-stats. Firecrawl stays required for `ResearchPort`.

**Consequences**
`GET /health` `owner_integrations.missing` is honest: Sheets id and LinkedIn token stay listed when blank. GSC / GA4 / Meta ads ids are listed only while discovery is off. Parsers in `app/integrations/composio_discovery.py` are shape-tolerant and unverified against Assaf’s live `{data, error, successful}` envelope until `uv run python scripts/probe_composio_discovery.py` is run. Production stays dark for those three ids until Assaf sets the leftover env or flips the flag after a clean probe.

**Alternatives considered**
Fake personal analytics with `LINKEDIN_GET_SHARE_STATS` — rejected; org URN, wrong job. Auto-pick a Sheets write target by name — rejected; near-miss writes the wrong document. Always-on request-time LIST_* — rejected; discovery must not fire on every port build. Treat LinkedIn member token as a go-live requirement — superseded by ADR-034: v1 LinkedIn is Composio profile; the analytics token is optional leftover. Replace Firecrawl with a Composio search toolkit — rejected; ResearchPort stays Firecrawl as primary (ADR-035 adds Apify only as the fallback when Firecrawl is unset).
