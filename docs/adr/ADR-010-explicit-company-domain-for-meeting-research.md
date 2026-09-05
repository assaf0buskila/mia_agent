# ADR-010 Explicit company domain for meeting research

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT

**Context**
Bible §12.2 requires company research before meetings. Inferring company identity from message text, UTM, referrer, email domain, profile URL, or business-type tokens is unsafe and unreliable.

**Decision**
Meeting research identity is an **explicit** `company_domain` on `SalesState`, collected via conservative extract (`app/domain/company.py`) or a short Hebrew domain question appended to `OFFER_MEETING` when missing. Domain does not block meeting eligibility, qualification, NBA, or canonical events. Pre-meeting research uses the existing typed `ResearchPort` (Firecrawl search / disabled / fake) with query = validated domain only; stores at most two title+host sources in Postgres brief row only; canonical `MEETING_BRIEF` stays SalesState snapshot keys. Cache: same domain + `research_attempted=true` never re-calls research.

**Consequences**
- **Security/privacy:** No inference from untrusted text; domain is owner-brief data only — excluded from qualification events, tool log payloads beyond allowlisted tool/status/result_count, Sheets, lead review, and graph return. Snippets are data; no excerpt/URL/path in storage.
- **Reliability/performance:** One search per lead/domain; fail-closed on error with base brief still persisted; kill switch denies research and skips brief write.
- **Cost/lock-in:** Reuses existing Firecrawl search port; no crawl/browser/LLM; no new provider.
- **Migration/files:** `company_domain` column on `lead_sales_state` (`String(253)`, default empty); `meeting_research` in `ALLOWLISTED_TOOLS`; `app/domain/company.py`, `app/domain/briefs.py`, `app/domain/extract.py`, `app/graph/replies.py`, `app/api/inbound.py`, `app/api/website.py`, tests.
- **Tests:** `tests/unit/test_company_meeting_research.py` + existing brief tests unchanged when no domain.

**Alternatives considered**
Infer company from email/UTM/message — rejected (Assaf ADOPT explicit domain). Company-name search without domain — rejected. Separate research table — rejected (brief row enrichment sufficient). LLM summarization — rejected (out of scope).
