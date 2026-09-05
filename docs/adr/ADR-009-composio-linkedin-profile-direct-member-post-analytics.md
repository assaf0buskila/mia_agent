# ADR-009 Composio LinkedIn profile + direct member post analytics

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT

**Context**
§21A requires personal post/share performance for Assaf's LinkedIn presence. Composio `LINKEDIN_GET_MY_INFO` covers own-profile read via managed OAuth. Composio `LINKEDIN_GET_SHARE_STATS` requires an organization URN and is organization-page analytics only — wrong adapter for Assaf's personal member profile. Microsoft Learn documents personal member analytics at `GET /rest/memberCreatorPostAnalytics` with OAuth scope `r_member_postAnalytics` (3-legged member OAuth; application approval required).

**Decision**
Keep typed `LinkedInPort` on Composio `LINKEDIN_GET_MY_INFO` (`20260724_00`) when `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` set. Add a **separate** typed `LinkedInAnalyticsPort` in `app/integrations/linkedin_analytics.py` using LinkedIn's direct official REST API when `MIA_LINKEDIN_ACCESS_TOKEN` is set. Pin `LINKEDIN_API_VERSION = "202608"`. One GET per allowlisted metric (`IMPRESSION`, `MEMBERS_REACHED`, `REACTION`, `COMMENT`, `RESHARE`, `LINK_CLICKS`) with `q=me`, `aggregation=TOTAL`, and previous 30 completed local-calendar days (`start=D-30`, `end=D` exclusive). Do not combine credentials/clients into one class. Do not use Composio share stats for personal analytics.

**Consequences**
- **Security:** Member token in env/Secrets Manager only; never in code/git/logs/ack/canonical events. R0 `linkedin_analytics_read`. Kill switch denies before HTTP. 401/403 fail-closed (no six denied calls). No post content, post URLs, member IDs, or raw API response in ack or `TOOL_RESULT`.
- **Reliability:** Per-metric fail-closed; partial metric errors leave field `None`; all missing → no stats line. No retries this slice. Separate port from profile read — profile failure does not block analytics and vice versa.
- **Cost:** Up to six read calls per owner linkedin ack when token set. Composio profile read unchanged (one call).
- **Migration:** New env `MIA_LINKEDIN_ACCESS_TOKEN`; new tool `linkedin_analytics` in allowlist; owner linkedin path persists two `TOOL_RESULT` rows (`linkedin_profile`, `linkedin_analytics`). Live adapter requires LinkedIn app approval for `r_member_postAnalytics` + operator OAuth — code is alive via mocks; production HTTP needs approved token.
- **Test:** `tests/unit/test_linkedin_analytics.py` + updated owner linkedin inbound test.

**Alternatives considered**
Composio `LINKEDIN_GET_SHARE_STATS` — rejected; organization URN required, not personal member analytics. Single combined LinkedIn client — rejected; splits credentials, failure modes, and ADR-007 adapter choice per job. Scraping personal profile — rejected; boundary violation.
