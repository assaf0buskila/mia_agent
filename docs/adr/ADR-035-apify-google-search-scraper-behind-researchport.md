# ADR-035 Apify google-search-scraper behind ResearchPort

> Renumbered from ADR-030 in the `mia:20` merge (see ADR-034).

- **Status:** accepted
- **Date:** 2026-08-25
- **Assaf:** ADOPT (chat: wire the Apify token as a research supplier)

**Context**
ADR-015 left Apify for later behind the same typed `ResearchPort`. Assaf added an Apify API token. Dumping the Actor Store or arbitrary runs into the owner model would violate ADR-007. `apify/rag-web-browser` is Playwright page crawl, not search snippets. `apify-client.call()` waits indefinitely by default.

**Decision**
Pin **`apify/google-search-scraper`** only. Call `POST /v2/actors/apify~google-search-scraper/run-sync-get-dataset-items` with httpx (`timeout=60`, `maxTotalChargeUsd=0.02`, client timeout 70s). Adapter-owned input: one query, one SERP page, add-ons off. Map `organicResults` to `{title, url, excerpt}` (cap 2). Firecrawl stays primary when `MIA_FIRECRAWL_API_KEY` is set; `MIA_APIFY_TOKEN` selects this adapter only when Firecrawl is empty. Do not retry HTTP 408 (the run keeps billing). No Actor catalog, no `apify-client`, no Composio Apify toolkit.

**Consequences**
`research_search` and meeting-brief research stay one port. `/health` `research_apify` is true only when Apify is the selected adapter. Production `mia/prod` must include `MIA_APIFY_TOKEN` (empty until Assaf pastes the token) before an ECS revision that injects it. SEO audit scrape stays Firecrawl.

**Env var name is settled: `MIA_APIFY_TOKEN`.**
A parallel, abandoned implementation of this same decision exists on branch
`claude/mia-adr033-wip` (`e21a29c`) using `MIA_APIFY_API_TOKEN` and the legacy
`/v2/acts/` path. Assaf chose `MIA_APIFY_TOKEN` (2026-08-26, chat: "for apify
choose one name and apply it"). That name is the one wired through
`app/core/config.py` (`apify_token`), `app/core/redact.py`, `app/main.py`,
`.env.example`, `deploy/ecs-task-definition.example.json`,
`deploy/mia-prod.secret.example.json` and `tests/conftest.py`. Do not
reintroduce `MIA_APIFY_API_TOKEN`; if that WIP branch is ever revived, rename
it on the way in. The AWS secret has not been set yet — no migration cost.

**Alternatives considered**
`MIA_APIFY_API_TOKEN` (the `claude/mia-adr033-wip` spelling) — rejected; the shorter name was already wired and tested across eight files, and Apify's own "API token" wording does not justify churn on a key that is not yet in Secrets Manager. `apify/rag-web-browser` — rejected; full-page crawl/browser, not SERP snippets. `apify-client` — rejected; extra package, unbounded wait. Firecrawl replacement while its key is set — rejected; live production search stays Firecrawl. Model-owned actor id or input knobs — rejected (ADR-007).
