# Website + SEO gap report (pre-AWS)

**Date:** 2026-08-22  
**Status:** partial — Mia SEO read ports **alive** (Disabled when env empty). AssafWeb widget `form_started` **alive**. Cross-repo Playwright still missing.  
**AssafWeb repo:** `../assaf landing page` (Next.js, Vercel, https://www.assafweb.com)  
**Mia repo:** this workspace  
**Bible:** §7 funnel + minimization; §8 attribution; §21 Firecrawl search (no crawl this slice)  
**Do not start:** RDS, ECS, ECR, ALB, production Secrets Manager

GA4 and Search Console are **measurement sources**. They do not edit the site. Approved SEO edits happen in the AssafWeb codebase. Mia must never autonomously rewrite the website.

## Verdict

Mia already owns the widget, funnel events, UTM attribution, WhatsApp handoff, and CORS. AssafWeb already embeds the widget script and marks sections/CTAs/form. **Missing** is everything Google + SEO analysis + approval-gated site edits + cross-repo staging E2E.

**Tally (12 areas):** 3 Alive (UTM, CORS, form_started) · 5 Partial (widget, behavior, handoff, conversion, SEO read ports) · 4 Missing/staging (live Composio GA4/GSC connections, cross-repo E2E, applied site edits, gtag in Next).

Do not redesign AssafWeb. Do not dump Composio catalogs into the sales graph (`session.tools()` / LangChain provider). Pin tools behind owner-only ports, same as Gmail/Calendar today.

## 1. Twelve-area matrix

| Area | Mia | AssafWeb | Status |
| --- | --- | --- | --- |
| Widget / chat | `GET /v1/website/widget.js`; session + messages + LangGraph | `AskMiaWidget` + `lib/mia.ts`; homepage inject | **Partial live.** Script is in the site. `NEXT_PUBLIC_MIA_BASE_URL` must be HTTPS and not localhost — production widget is off until a public Mia origin exists. Local Next cannot load loopback Mia. |
| Behavioral events | Allowlist in `app/domain/behavior.py`; POST `/v1/website/sessions/{id}/events`; widget queues until open | `data-mia-section`, `data-mia-cta`, `form[data-mia-form]` on homepage | **Alive** including `form_started`. Client: `page_viewed`, `section_viewed`, `cta_click`, `form_started`, `form_abandoned`. Server: `mia_opened`, `conversation_started`, `whatsapp_handoff`. |
| Website → WhatsApp handoff | Opaque token, 60m TTL, same lead, `identity_links` | Widget WA button uses Mia handoff. Contact form + FAB open raw `wa.me` **without** the token | **Partial.** Widget path preserves lead. Form/FAB bypass Mia. |
| UTM / attribution | Session create query: `utm_source/medium/campaign/content`, landing, referrer; canonical ATTRIBUTION first-write | Widget copies UTMs from the page URL into session create | **Alive** on Mia session. Not joined to GA4. |
| GA4 | Typed `Ga4Port` (`app/integrations/ga4.py`); owner SEO enrich; Disabled when empty | No `gtag`, no Measurement ID, no `NEXT_PUBLIC` analytics key | **Partial** — Mia read port alive; live Composio connection + `MIA_GA4_PROPERTY_ID` operator action |
| Search Console | Typed `SearchConsolePort` (`app/integrations/search_console.py`); owner SEO enrich | robots/sitemap exist; no client GSC | **Partial** — Mia read port alive; live OAuth + `MIA_GSC_SITE_URL` operator action |
| SEO crawl / audit | `SeoAuditPort` Firecrawl scrape allowlisted to assafweb.com | Source has title, description, canonical, JSON-LD | **Partial** — homepage audit alive when `MIA_FIRECRAWL_API_KEY` set |
| SEO recommendations | `app/domain/seo.py` + Postgres `seo_recommendations` | — | **Alive** (read + recommend; no auto-edit) |
| SEO edit / approval | `website_edit` approval persist (`app/domain/approvals.py`) | No Mia-driven patch path | **Partial** — persist-only; apply in AssafWeb via Cursor after approve |
| Conversion tracking | Behavior + sales graph + meetings/deals in Postgres; `website_funnel_drop` on owner analytics | Many competing CTAs (hero, contact, FAB, form, Ask Mia) | **Partial.** Mia funnel exists. GA conversions do not. Form submit ≠ Mia handoff. |
| Security / CORS | `MIA_CORS_ORIGINS`; prod example is assafweb only; defaults also allow localhost for laptop | Only public env is `NEXT_PUBLIC_MIA_BASE_URL`; ElevenLabs keys are server-only | **Alive** on policy. Staging CORS for `localhost:3000` must stay laptop-only. |
| Staging E2E | In-process website unit tests + §23 stories (fakes). No Playwright against Next | `npm run lint` = `tsc --noEmit`. No test suite | **Missing** cross-repo. Tests 1–6 of the prompt are unproven on the real site. |

## 2. Event contract (keep)

Required vs code:

| Event | Where | Status |
| --- | --- | --- |
| `page_viewed` | Widget client | Alive |
| `section_viewed` | Widget + `data-mia-section` | Alive |
| `cta_click` | Widget + `data-mia-cta` | Alive |
| `form_started` | Widget client | **Alive** |
| `form_abandoned` | Widget + `data-mia-form` | Alive (kind only; no field values) |
| `mia_opened` | Server on launcher/session | Alive |
| `conversation_started` | Server on first message | Alive |
| `whatsapp_handoff` | Server on widget handoff | Alive |

Do not read `innerText` or form field values into Mia.

## 3. SEO / analytics architecture (proposed)

```
AssafWeb source + live https://www.assafweb.com
  → git inspection + Firecrawl scrape (allowlisted host only)

Google Search Console  → Composio pin, owner-only
Google Analytics 4     → Composio pin, owner-only (READ reports, not Measurement Protocol send)

Mia SEO analysis
  → prioritized recommendation (Problem / Evidence / Why / Change / Metric)
  → proposed exact before/after
  → Assaf approval
  → Cursor edit in AssafWeb repo
  → lint/build
  → show diff
  → deploy only after Assaf says so
  → re-measure in GSC/GA4 when data exists
```

Postgres stays operational SoR. Missing Google rows → omit, never invent rank/traffic.

### Composio pins (minimum)

Official toolkits exist; Cursor Composio connections for GA4/GSC were **inactive** at inspect time.

**Search Console (`GOOGLE_SEARCH_CONSOLE`):**

- `GOOGLE_SEARCH_CONSOLE_SEARCH_ANALYTICS_QUERY` (query/page, clicks, impressions, CTR, position)
- `GOOGLE_SEARCH_CONSOLE_INSPECT_URL`
- `GOOGLE_SEARCH_CONSOLE_LIST_SITES`

Sitemap listing tool was not in the first search hit — do not invent one. Use AssafWeb `public/sitemap.xml` + inspect URL.

**GA4 (`GOOGLE_ANALYTICS`):**

- `GOOGLE_ANALYTICS_RUN_PIVOT_REPORT` (landing, source, engagement)
- `GOOGLE_ANALYTICS_LIST_CONVERSION_EVENTS`
- Property list tools as needed to resolve `properties/{id}`

**Do not pin:** `GOOGLE_ANALYTICS_SEND_EVENTS` (Measurement Protocol **write**). Do not put Measurement Protocol secrets in Next. Do not use [LangChain `session.tools()`](https://docs.composio.dev/docs/providers/langchain) for this.

Firecrawl: add a **scrape** path behind `ResearchPort` (or a dedicated `SeoAuditPort`) restricted to `https://www.assafweb.com` / `https://assafweb.com`. Treat HTML as untrusted data. Current `FirecrawlSearchPort` is search-only.

## 4. AssafWeb facts (do not redesign)

- One H1 on the homepage. JSON-LD in `LandingPage.tsx`. Metadata in `app/layout.tsx`. `public/robots.txt` + `public/sitemap.xml` + `public/schemamap.xml`.
- Competing conversion paths: Ask Mia, hero/contact WhatsApp, FAB `wa.me`, contact form that opens WhatsApp with PII in the query string (bypasses Mia handoff).
- Minimization is already specified in PRD §7.1 (compress services, merge about/process, keep 4 FAQ). **No deletes without Assaf approval.** First output is a separate recommendation report, not a redesign PR.
- No GA snippet today — adding gtag is a product decision (client measurement ID is public by design; still no API secrets in `NEXT_PUBLIC_`).

## 5. Tests the phase must prove (after approval)

Mia: `uv run ruff check app tests` and `uv run pytest`.  
AssafWeb: `npm run lint` and `npm run build`.

| # | Requirement | Today |
| --- | --- | --- |
| 1 | AssafWeb loads | Manual / Vercel. No automated test |
| 2 | Widget loads | Blocked locally by HTTPS-only `miaWidgetSrc()` |
| 3 | Chat session starts | Mia unit tests; not against Next |
| 4 | Events reach Mia once | Mia unit tests |
| 5 | UTM persists | Mia unit tests |
| 6 | Handoff keeps same lead | Mia unit tests; form/FAB bypass |
| 7–9 | GA4 / GSC / Firecrawl audit | **Alive** in Mia unit tests (Fake/Disabled); live Google still operator |
| 10 | Missing Google data ≠ fake SEO | **Alive** (`tests/unit/test_seo.py`) |
| 11–13 | Proposed edit + approval + git diff + build | **Partial** — approval persist alive; AssafWeb apply manual |
| 14 | No secrets in browser payloads | Policy OK; needs a staging check |
| 15 | Existing Mia tests still pass | 1800 passed at last suite |

## 6. Implementation order (after Assaf ADOPT)

1. **Staging widget path** without AWS — pick one: (A) allow `http://localhost:8000` in `lib/mia.ts` only when `NODE_ENV=development`, plus Mia CORS localhost; or (B) TryCloudflare HTTPS in front of local uvicorn and set `NEXT_PUBLIC_MIA_BASE_URL` to that origin for the session. Do not put trycloudflare in Vercel production.
2. **`form_started`** in widget + `behavior.py` (form already has `data-mia-form`).
3. **Document / optionally unify** form+FAB vs widget handoff (recommendation first; do not silently remove WhatsApp CTAs).
4. Typed **GSC port** + **GA4 read port**; pin tools; owner WhatsApp SEO classify; Disabled ports when empty; never invent metrics.
5. **Firecrawl scrape audit** allowlisted to AssafWeb; persist recommendation rows (not page HTML dumps).
6. Owner SEO answers: check SEO / which pages / keywords / weak CTR / traffic without conversion — template: Problem, Evidence, Why, Change, Metric. No “will rank #1”.
7. **Approval-gated proposed edit** (`resource_type=website`, exact before/after). Apply in AssafWeb via Cursor after Assaf yes. Mia does not commit.
8. **Minimization report** (redundant copy, competing CTAs) — no deletes.
9. Staging E2E for items 1–15. Then Assaf approves AWS.

## 7. Out of this phase

- RDS / ECS / ECR / ALB / production Secrets Manager
- Composio Instagram send (separate ADR-015 slice; inbound stays Meta webhook)
- LangChain Composio provider / catalog-in-graph
- Autonomous website rewrite
- Make GA/GSC the CRM

## 8. Decision gate

Assaf chose **ADOPT** for pre-AWS Mia SEO slice (2026-08-22). Implemented: GSC/GA4 read ports, Firecrawl homepage audit, owner SEO classify, recommendations persist, `website_edit` approval persist-only. **Remaining:** connect Composio GA4/GSC OAuth, set `MIA_GA4_PROPERTY_ID` / `MIA_GSC_SITE_URL`, cross-repo Playwright, form/FAB handoff unification (see `docs/WEBSITE_MINIMIZATION_REPORT.md`). AWS still gated.
