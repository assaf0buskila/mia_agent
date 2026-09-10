# Owner tools audit — 2026-09-10

## Scope and result

This is a current-tree audit of all 42 tools advertised by
`app/tools/registries/owner_tools.py`. It covers registration, the effective
authorization boundary, side-effect posture, focused unit evidence, and what still
needs a credentialed read-only verification. It did not inspect secrets, send a
message, execute a provider write, approve an action, deploy, or claim production
acceptance.

Every advertised tool is present in `OWNER_HOUSE_TOOLS`, and none of the four
explicitly forbidden owner tools is registered. The new mechanical guard in
`tests/unit/test_owner_tool_registry_inventory.py` freezes both invariants.

The owner runtime has two relevant outer boundaries: Telegram mints an owner
`Principal` only after numeric allowlist authentication, and `answer_owner` exits
while the kill switch is active. `execute_tool` then enforces the owner/visitor state
gate before resolving the registered handler. The handler notes below distinguish a
missing defense-in-depth check from a demonstrated reachable owner-turn bypass.

## Inventory

Legend: **R** is a read, **LW** is a local/database write, **PW** is a bounded
provider write, **P** creates an approval proposal without executing the approved
provider action, and **D** is denied. “Named policy” means `execute_capability` or
`authorize` checks a registered capability with the request-derived principal.

| Owner tool | Posture and authorization | Focused unit evidence | Live verification still needed |
| --- | --- | --- | --- |
| `search_memory` | R via `memory.search`; owner named policy. Touches local retrieval metadata after a hit. | `test_brain_agent.py`, `test_vnext_owner_capability.py` | No; local test evidence is sufficient. |
| `search_knowledge` | R via `knowledge.search`; named policy, owner registry exposure. | `test_brain_agent.py`, `test_vnext_owner_capability.py`, `test_two_state_tools.py` | No; refresh/ingestion currency is a separate operational check. |
| `remember` | LW to owner memory; state gate plus `memory_write_enabled`, no named capability. | `test_brain_agent.py`, `test_prompt_safety_rules.py` | No provider check. A kill-switch defense-in-depth test is missing. |
| `list_known_entities` | R from the local brain store; state gate only. | Entity extraction/listing is covered in `test_brain_memory.py` and `test_brain_end_to_end.py`; no direct registry-handler test. | No. |
| `daily_brief` | R plus optional idempotent LW of the dated brief; R1 policy inside the domain write. | `test_owner_briefs.py`, owner inbound coverage. | No. |
| `weekly_brief` | R plus optional idempotent LW of the dated brief; R1 policy inside the domain write. | `test_owner_weeklies.py`, owner inbound coverage. | No. |
| `hot_leads` | R via `leads.get_recent`; owner named policy. | `test_vnext_owner_capability.py`, agent chaining tests. | No. |
| `pending_approvals` | R from Postgres; state gate only. It cannot decide an approval. | Agent chaining and owner reply tests. | No. |
| `website_conversations` | R from Postgres; state gate only. | Owner reply coverage; no dedicated direct-handler test. | No. |
| `operator_snapshot` | R aggregate from Postgres; state gate only. | `test_owner_snapshot.py`; no direct registry-handler test. | No. |
| `owner_status` | R aggregate; state gate, with hot-lead reads delegated through named policy. | `test_owner_distinct_replies.py`, `test_owner_tasks.py`. | No. |
| `sheets_list_tabs` | R via `sheets.list_tabs`; owner named policy and spreadsheet allowlist. | `test_owner_sheets.py`; no direct `execute_tool` test. | One read-only call against the configured workbook. |
| `owner_system_audit` | R aggregate. Calls bounded named/provider and local reads; deliberately does not consume booked-meeting notifications. | Four direct tests in `test_owner_live_tools.py` plus agent budget coverage. | Run the prepared read-only probe; it is representative by integration, not exhaustive by Composio slug. |
| `lead_review` | R plus optional R1 LW of a review projection; state gate and domain risk check for the write. | `test_lead_reviews.py`. | No. |
| `find_leads` | R from Postgres; state gate only. | `test_lead_find.py`. | No. |
| `meeting_brief` | R from Postgres; state gate only. | `test_briefs.py`. | No. |
| `calendar_availability` | R via `calendar.get_schedule`; owner named policy. | `test_owner_calendar.py`, `test_vnext_owner_capability.py`. | One current free-slot read. |
| `calendar_agenda` | R through `CalendarAgendaPort`; state gate but no named capability call in the handler. | Direct fake-port, invalid-range, registration, and disconnected tests in `test_owner_calendar_agenda.py`. | One current agenda read; add kill-switch/principal defense-in-depth coverage if this direct port remains. |
| `calendar_create_meeting` | P only. Tel Aviv/business-hours/empty-slot gate persists an exact R3 approval; provider create is outside the tool and occurs only after approval. | `test_two_state_tools.py`, `test_owner_calendar_writes.py`; exact returned approval ID added in this audit. | No provider write. A read-only availability preflight can be verified separately. |
| `booked_meetings` | R plus LW marking returned owner notifications seen; R1 domain policy protects the mark. | `test_owner_notify.py`; aggregate audit non-consumption regression in `test_owner_live_tools.py`. | No. |
| `content_ideas` | R plus optional R1 LW of the daily idea projection; never publishes. | `test_content_ideas.py`. | No. |
| `gmail_summary` | R from ingested Postgres data; state gate only, not a live Gmail read. | `test_gmail_summaries.py`. | No; this tool must not be used as evidence of current inbox state. |
| `gmail_inbox` | R via `mail.search`; owner named policy. | Direct fake-port, disconnected, visitor denial, timeout, and house-binding tests. | One current inbox-list read. |
| `gmail_search` | R via `mail.search`; owner named policy and deterministic query normalization. | Direct fake-port tests plus Gmail query tests. | One bounded current search. |
| `gmail_read` | R via `mail.read`; owner named policy. Email body remains untrusted data. | Direct fake-port, visitor/kill denial, and capability tests. | One explicitly selected current message read if needed. |
| `gmail_create_draft` | PW creates a Gmail draft, then P persists R3 send approval. It never sends; exact approval ID now returns with the tool result. | `test_owner_gmail_console.py`, including the new exact-ID/no-send regression. | Do not live-test in the read-only probe because draft creation is a provider write. |
| `whatsapp_draft_assaf` | Local text-only draft, no provider call and no lead send. | `test_two_state_tools.py`. | No. |
| `seo_snapshot` | R aggregate across GSC, GA4, and technical SEO adapters; named policies apply to GSC/GA reads. | `test_owner_live_tools.py`, timeout and prompt-distinction tests. | One representative read-only snapshot. |
| `website_kpis` | R via `analytics.get_traffic` and `search_console.query`; owner named policy, fixed completed 28-day window. | Success and partial-failure direct tests in `test_owner_live_tools.py`. | One current API-backed read; verify dates in output. |
| `linkedin_snapshot` | R via `linkedin.get_profile`; owner named policy. | Fake-port, disconnected, client denial, house-binding, and capability tests. | One current profile read. Full-profile completeness remains provider-schema dependent. |
| `crm_search` | R from the locked Contacts/Activity workbook; state gate and fixed workbook boundary, no named capability call. | `test_dude_clone.py`, `test_tool_outcome_truth.py`, Sheets alias prefetch tests. | One bounded read of Contacts and Activity. |
| `crm_upsert` | PW to locked Contacts plus Activity with contact-key validation and idempotency claim; no named capability or handler-local kill-switch check. | `test_crm_upsert_handler.py`, `test_dude_clone.py`. | Do not live-test in this audit. Add a direct kill-switch defense-in-depth regression; the outer owner turn currently exits when killed. |
| `sheets_read` | R via `sheets.read`; owner named policy, allowlisted workbook and bounded range. | `test_owner_sheets.py`, adapter-error coverage. | One bounded configured-workbook read. |
| `sheets_update` | PW via `sheets.update`; owner named policy, exact current-message ID/range/value binding, allowlist, and idempotency. | Extensive positive and adversarial coverage in `test_owner_live_tools.py` and adapter policy tests. | Do not execute during the read-only probe. |
| `sheets_append` | PW via `sheets.append`; same binding and idempotency controls as update. | Extensive positive and adversarial coverage in `test_owner_live_tools.py` and adapter policy tests. | Do not execute during the read-only probe. |
| `instagram_insights` | R through the analytics-only adapter; state gate plus domain kill-switch handling, no publish surface. | House binding, identity/format, freshness, and disconnected tests. | One current bounded insights read; verify post/account identity or explicit provider omission. |
| `research_search` | R via `research.search`; owner named policy and explicit query requirement. | `test_research.py`, freshness, disconnected, and capability tests. | One bounded public search. |
| `composio_search_tools` | R via `composio.catalog_search`; owner named policy, ACTIVE connected toolkits only, bounded cached listing. | `test_composio_owner_catalog.py`, including cache, transient failure, client and kill-switch tests. | Search each active toolkit in the prepared probe to inventory, not execute, available tools. |
| `composio_get_tool_schema` | R via `composio.tool_schema`; owner named policy and bounded closed schema. | Schema size/shape/detail mismatch and policy tests in `test_composio_owner_catalog.py`. | Fetch one representative current schema per active toolkit. |
| `composio_execute_tool` | Dynamic R executes only classifier-approved R0 reads after ACTIVE-toolkit/schema validation. Non-R0 routes to P or is refused; sends and bounded Sheets writes never auto-execute. | Read, schema, provider-failure, destructive/send/publish and policy tests in `test_composio_owner_catalog.py`. | Execute representative read slugs only. This does not prove every active Composio slug. |
| `composio_propose_linkedin_action` | P via named low write; exact schema/arguments persisted for approval. Direct messages and destructive LinkedIn actions are denied; no provider action at proposal time. | LinkedIn proposal, deny, payload-bound and exact-turn approval tests. | No provider write. Verify only schema discovery if needed. |
| `composio_propose_action` | P via named low write for eligible non-destructive actions. R5, Gmail send, Instagram publish, and generic Sheets writes are denied; LinkedIn writes route to the named LinkedIn approval path. | Generic proposal/callback, excluded-class, legacy-R5, and policy-first tests. | No provider write. |

## Findings and disposition

1. **Fixed: exact approval IDs were dropped by two registered proposal paths.**
   `calendar_create_meeting` and `gmail_create_draft` created durable approvals but
   returned only text, so `AgentOutcome.approval_ids` could not bind Telegram buttons
   to the approval created by that exact tool call. Both handlers now resolve the
   exact resource created in the same call and return its stored approval ID. They do
   not query the latest pending approval. Focused tests prove the ID resolves to the
   matching calendar resource or Gmail draft, and prove Gmail did not send.

2. **Fixed in the same working tree: generic proposal authorization order.**
   `composio_propose_action` previously constructed the catalog before its named
   authorization check. The runtime visitor state gate prevented visitor reachability,
   but the handler lacked the policy-first defense used by the other meta-tools. The
   handler now authorizes before catalog construction, and the “all meta tools” test
   includes both proposal tools.

3. **Fixed: the generic approval path no longer turns denial classes into executable
   approvals.** R5/destructive slugs, Gmail sends, Instagram publishing, and generic
   Sheets writes are refused before approval persistence. The executor rechecks the
   stored row and current provider tool contract, and callbacks refuse legacy R5 rows
   without changing their decision. Safe non-R5 proposals remain approval-bound;
   LinkedIn writes route to the existing named LinkedIn approval workflow.

4. **Open defense-in-depth gap: registry write markers are incomplete.**
   `ToolSpec.writes_memory` identifies only `remember`; it is not a general side-effect
   classification. Several tools persist local projections or notification state, and
   CRM/Sheets/Gmail draft tools write to providers. The marker must not be used as an
   audit claim that every other registered tool is read-only.

5. **Open defense-in-depth gap: `remember` and `crm_upsert` do not perform their own
   named capability/kill-switch check.** The authenticated owner turn currently exits
   under the kill switch, and visitors fail the state gate, so this audit did not show
   a reachable production owner-turn bypass. Direct handler execution nevertheless has
   less defense than Sheets writes and named reads. A central side-effect policy or
   explicit handler checks would make the invariant testable without relying on call
   topology.

6. **Open consistency gap: some direct read adapters bypass the named capability
   registry.** `calendar_agenda` and `crm_search` rely on the owner state gate and their
   bounded adapters rather than `execute_capability`; Instagram uses its older domain
   risk path. This is not evidence of visitor reachability, but it weakens the single
   auditable capability map promised by the architecture.

7. **No static no-tool gap was found for current house reads.** Gmail, Calendar,
   LinkedIn, Instagram, GSC/GA4, Sheets, CRM, research, local operating data, memory,
   and knowledge all have named tools. Other ACTIVE Composio reads are intentionally
   discovered through search → schema → dynamic read execution. A catalog inventory
   proves discovery breadth only; it cannot prove all returned slugs work without
   executing them, and this audit does not make that claim.

## Verification record

- Focused Composio catalog, health, and production-smoke suites after the R5 repair:
  **102 passed**. The earlier Gmail, Calendar, registry, and Composio slice passed
  **82 tests**.
- The registry drift guard adds **2 tests** and covers all **42 registered names** plus
  all **4 forbidden names**.
- Relevant read-only suites should be run with
  `UV_CACHE_DIR=.uv-cache` and `--basetemp=.codex-pytest-tmp/tools-audit` before merge.
- Credentialed verification remains a separate read-only operational step. Its report
  must state which integrations and representative slugs were actually called, retain
  provider failures/empty results, and avoid the phrase “all Composio tools verified.”
