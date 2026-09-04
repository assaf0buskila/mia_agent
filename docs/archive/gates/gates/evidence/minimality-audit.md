# Minimality audit — leaf 1.3.2

Audit date: 2026-08-28. Scope is the current `app/` and `scripts/` tree after the
ADR-039 and ADR-042 source work stabilized and after the bounded refactor below.
Only the files listed in the implementation evidence were changed by this leaf.

## Architecture boundary

This audit does **not** recommend a production swarm. ADR-031 and ADR-032 require
one bounded owner agent; ADR-036/038 require exactly OwnerGraph and ClientGraph.
Splitting a Python module or extracting a deterministic helper changes source
ownership only. It must not add a model call, runtime agent, graph, router model,
database, ambient `GraphName`, or supplier.

Large-file size is only a discovery signal. Declarative schemas, explicit safety
contracts, and typed provider parsers are often clearer kept together than hidden
behind indirection.

## Measured inventory

Measurement command counted physical lines in every `*.py` under `app/` and
`scripts/` using `System.IO.File.ReadAllLines`. Snapshot: 176 `app/` Python files,
41,390 lines; 22 files are at least 500 lines, totaling 19,303 lines (46.6%).
There were no `scripts/` files at or above the threshold.

| Lines | File | Disposition | Concrete evidence and dependency impact |
| ---: | --- | --- | --- |
| 2,957 | `app/db/store.py` | **SPLIT, last** | `LeadStore` spans nearly the full file and is imported across virtually every API, worker, domain service, graph, and test. Split persistence domains behind the same `LeadStore` public facade only after smaller call sites are stable; changing signatures would have repo-wide impact. Do not replace Postgres or transactions. |
| 1,336 | `app/integrations/sheets.py` | **SPLIT** | It combines owner range reads/writes, provider implementations (`SheetsPort`/Disabled/Composio/Fake), mirror shaping, and the now-shared `mirror_sales_turn` (1091–1214). APIs, owner tools, capabilities, and many tests import it. Keep attribution/source mirror fields and Postgres-as-SoR behavior. The duplicated sales orchestration is removed; a later transport/policy split is optional. |
| 1,317 | `app/tools/registries/owner_tools.py` | **KEEP** | Explicit handlers and explicit tool descriptions are the server-side allowlist (`execute_tool`, currently near the file end). `app/graph/owner_agent.py` consumes the registry and owner-tool tests exercise it. ADR-032/042 deliberately require detailed per-tool purpose/input/output/limits. Splitting descriptions from handlers would add navigation without deleting behavior; it would still be one owner agent, not sub-agents. |
| 1,018 | `app/evals/harness.py` | **SPLIT, low priority** | Nine independent eval families are loaded/run in one file (`run_sales_eval` 470, `run_gold_eval` 510, `run_buyer_eval` 557, `run_website_handoff_eval` 612, extract/objection/calendar/safety/routing/writing 728–1018). Runtime serving code does not import it; callers are `app/evals/__init__.py`, `scripts/eval_diff.py`, and eval tests. Split by eval family only when a family changes; no production complexity reduction follows now. |
| 965 | `app/domain/owner_tasks.py` | **KEEP** | Phrase tables plus `classify_owner_task` (718–765), fallback promotion (830–854), and deterministic acknowledgement (864–965) implement the availability fallback ADR-032 explicitly preserves. Callers include `app/api/owner.py`, `app/api/inbound_common.py`, `app/domain/commitments.py`, the eval harness, and extensive HE/EN regression tests. Removing it would trade fallback availability for tidiness. |
| 964 | `app/api/owner.py` | **SPLIT after active owner work** | `process_owner_item` is one 732-line transport/task/agent lifecycle function (133–864); `process_owner_texts` is the thin batch entry (867–964). Extract deterministic task execution and port construction without changing the single `run_owner_turn` call. Impact: Telegram, legacy mixed-inbound compatibility, owner tool outcomes, learning, and many owner tests. Do not rewrite the behavior wholesale. |
| 835 | `app/api/website.py` | **SPLIT** | DTO/endpoints are small, while `process_website_message` is now 266 AST lines (323–588) after the shared sales-mirror extraction. Keep the router and response contracts stable; a later service split is optional. Impact: widget endpoints plus website, voice, finalization, and client-isolation tests. |
| 893 | `app/domain/events.py` | **KEEP** | The file is an explicit canonical-event contract: builders and sanitizers for attribution, qualification, meetings, business value, handoff, behavior, deals, follow-ups, approvals, briefs, tool outcomes, transcripts, and messages (lines 136–893). It is imported throughout APIs/domains and heavily tested. These allowlists, idempotency keys, attribution fields, and sanitizers are generic safety infrastructure, not accidental duplication. |
| 800 | `app/integrations/calendar_booking.py` | **KEEP** | Typed booking models/port (50–164), disabled/fake/live adapters (167–546), and provider-response parsers (590–800) form one write-boundary contract. Domain booking and meeting-change code plus focused Gate-2 tests depend on exact lookup/create/patch/verify semantics. File length is justified by fail-closed parsing. |
| 791 | `app/integrations/calendar.py` | **SPLIT** | Availability occupies lines 56–533; the independently typed agenda read begins with `CalendarEvent`/`CalendarAgendaPort` at 545 and continues through line 791. `calendar_booking.py` imports only the toolkit version while owner tools need agenda. Moving agenda to `calendar_agenda.py` has a clear seam but saves no lines, so it ranks below deletion/deduplication. |
| 780 | `app/domain/meeting_changes.py` | **KEEP** | Reschedule and cancellation are one accepted state machine: explicit parsers (145–150), verified persistence (195–371), offer/attempt (374–709), and `resolve_booked_meeting_change` (712–780). Booking/cancellation idempotency and verification tests cover it. Splitting the state machine risks separating validation from side effects. |
| 622 | `app/api/inbound.py` | **SPLIT, preserve strangler** | `process_inbound_texts` is now 494 AST lines (129–622), but it is live: `app/api/whatsapp.py`, `app/api/instagram.py`, and `app/api/composio.py` import it, as do many end-to-end/unit tests. It handles prospect transport compatibility and delegates graph reasoning to ClientGraph; it is not a third graph. Do not delete the entry point until those callers are replaced and tested. |
| 697 | `app/db/models.py` | **KEEP** | Declarative SQLAlchemy rows from `CustomerRow` (17) through `KnowledgeGapRow` (681) describe the single Postgres system of record. `LeadStore`, `BrainStore`, and tests import the models. One schema file is easier to audit than a package of tiny model files; removed ADR-039 campaign rows no longer appear. |
| 663 | `app/domain/owner_brain.py` | **SPLIT, later** | Model-chain construction, owner answer, retrieval shaping, one OwnerGraph invocation, and post-turn learning are separate seams. A split must retain exactly one `run_owner_turn`/owner agent and one final answer; it cannot introduce planner/rewrite/sub-agent hops. Owner graph/retrieval/voice tests are the impact surface. |
| 671 | `app/domain/approvals.py` | **SPLIT** | Generic identity/hash/expiry helpers occupy 82–229, website-edit parsing/validation/application occupies 242–429, and owner approval decision/ack/policy occupies 432–671. Extracting website-edit approval behind the same public imports is a clean seam. Preserve hashes, expiry, resource binding, approval IDs, and generic safety behavior. |
| 657 | `app/domain/calendar_booking.py` | **KEEP** | Booking result helpers, `attempt_meeting_booking` (227–491), canonical-event persistence (494–525), and `resolve_meeting_reply` (528–657) are one verified booking transaction. It depends on both typed calendar ports and is covered by booking/idempotency tests. Do not split policy from verify-before-success. |
| 656 | `app/domain/extract.py` | **KEEP** | Most lines are explicit HE/EN signal lexicons; the executable surface is six cohesive functions at 488–656, ending in `extract_sales_signals`. The orchestrator, emotion policy, eval harness, probes, and extraction/domain tests use it. Moving literals elsewhere would not make behavior smaller or easier to verify. |
| 606 | `app/brain/store.py` | **KEEP** | `BrainStore` (100–606) is the transaction-bound persistence facade for memory, knowledge, entities, links, and gaps. Brain context/extraction/capabilities/workers and brain tests depend on it. Splitting into runtime objects would increase coordination and transaction ambiguity without deleting behavior. |
| 538 | `app/integrations/gmail.py` | **KEEP** | One typed Gmail port with disabled/live/fake adapters (67–271), inbound/result formatting (282–382), and provider mappers/date parsing (385–538). Owner tools, Gmail ingest, drafts, summaries, and focused timestamp/tool tests consume it. Cohesive read/draft adapter; no dead branch found. |
| 516 | `app/integrations/sales_reply.py` | **KEEP** | Prompt/typed reply context, canned/fake/live implementations, `build_user_content` (304–377), and port construction (503–516) comprise one model-paraphrase boundary. ClientGraph/API and sales-reply tests depend on the prompt contract. Deterministic NBA remains elsewhere; do not add a rewrite model. |
| 513 | `app/core/capabilities.py` | **KEEP** | Mostly declarative `CapabilityId`/`Capability` entries, with only `capability_map` and `require_alive` executable at 506–513. Health, policy, integration wiring, and capability tests depend on its explicit status map. A data-driven loader would hide rather than remove truth. |
| 508 | `app/integrations/instagram_insights.py` | **KEEP** | Typed port plus direct/Composio/fake adapters (45–318), sanitized metric mapping (321–386), enrichment/outcomes (389–476), and builder (479–508) are one read-only capability. Owner/inbound/capability callers and freshness tests cover it. No campaign-ad or outbound behavior is present. |

No whole live file has sufficient evidence for **REMOVE**. The proven removals in
this campaign are the ADR-039 campaign-specific rows, store methods, events,
approvals, brief fields, and Sheets mirrors owned by leaf 1.1.2. Deleting any
additional full module from line count alone would be architecture churn.

## Duplicated and dead-code findings

### 1. Proven exact-shape duplication: sales-turn Sheets mirror

Before this leaf, `app/api/inbound.py:525-627` and
`app/api/website.py:503-605` each contained a 103-line block that:

1. claims the `sales` mirror idempotency key;
2. builds and writes lead, follow-up, deal, meeting, activity, and weekly-KPI rows;
3. persists the same `sheets_mirror_outcome`; and
4. completes the same claim.

Searches for `claim_sheets_mirror`, `activity_mirror_row_from_persisted`,
`maybe_mirror_weekly_kpi`, and `complete_sheets_mirror` show the two sales blocks
and a separate, intentionally different website-session block. The sales blocks
differ only in channel/provider identifiers and local variable names. Keeping two
copies made a new field or safety check easy to add to one channel and omit from
the other. Both now call `mirror_sales_turn`; the duplicated blocks are gone.

### 2. ADR-039 dead surface is assigned, not a second refactor target

The stable source no longer declares the four campaign ORM rows, campaign
store methods, campaign recommendation event, or campaign budget/performance
mirror functions. During the audit, two leftover disabled-port methods referencing
the removed campaign row types were found by searching for `upsert_budget` and
`upsert_performance`; the campaign owner removed them. Stale campaign tests were
still being converted when this report was written. Attribution names such as
`utm_campaign` and generic untrusted-text safety examples are **not** campaign
runtime and must remain.

### 3. Legacy inbound is intentional compatibility, not dead code

`process_inbound_texts` has live importers in `app/api/whatsapp.py`,
`app/api/instagram.py`, and `app/api/composio.py`, plus broad tests. It now invokes
ClientGraph for prospect reasoning. Deleting it because website has a newer API
would remove Meta/Gmail compatibility and violate ADR-036's strangler rule.

### 4. Deterministic owner classifier is intentional fallback, not a second agent

`classify_owner_task` and `OwnerTaskType` are called from `app/api/owner.py`,
`app/api/inbound_common.py`, `app/domain/commitments.py`, and the eval harness.
ADR-032 explicitly preserves these classifiers when the owner model is
unconfigured or down. They are not a competing production agent and are not dead.

### 5. No supported claim of additional dead production behavior

Every large module has runtime callers or, for the eval harness, explicit
operator/test callers. Static imports cannot prove dynamic reachability, so this
audit does not label a symbol dead merely because it lacks a direct import. A
future removal needs an exact caller search, route/worker proof, and a test proving
the capability is not part of an accepted ADR.

## Ranked bounded refactor candidates

1. **Completed: extract the duplicated sales-turn Sheets mirror helper.** Net
   production-source reduction of 70 lines with no architecture change; existing
   fake/disabled/live ports remain.
2. **Move agenda types/adapter/parsers from `integrations/calendar.py` to
   `integrations/calendar_agenda.py`.** Clear 247-line seam and narrower imports,
   but mostly file movement rather than deletion.
3. **Extract website-edit approval workflow from `domain/approvals.py`.** Clear
   trust/payload seam; moderate import-compatibility risk because several modules
   import shared lead-id/hash helpers.
4. **Extract deterministic owner-task execution from `api/owner.py`.** High
   readability gain, but only after owner integration/voice work is frozen; it
   affects the most security-sensitive owner path.
5. **Split `LeadStore` by persistence domain behind the existing facade.** Largest
   size reduction per file, lowest priority due its repository-wide caller surface
   and transaction coupling.

Wholesale **REWRITE** is not recommended for any file. The architecture works;
bounded extraction and deletion are safer and measurably cheaper.

## Implemented first refactor

### Objective

Replaced the two 103-line sales-mirror blocks with one deterministic helper while
preserving all rows, attribution boundaries, claim behavior, tool outcomes, kill
switch behavior, and Postgres-as-system-of-record semantics.

### Exact owned files

- `app/integrations/sheets.py`: added `mirror_sales_turn(...) -> ToolOutcome | None`.
  It owns the unchanged sales claim → six mirror attempts → tool outcome persist →
  claim completion sequence. It does not read Sheets or accept a spreadsheet ID.
- `app/api/inbound.py` and `app/api/website.py`: replaced only the duplicated sales
  blocks with calls to the helper. Caller-owned elapsed-time functions are passed
  through, preserving existing latency tests and instrumentation.
- `tests/unit/test_sheets.py`: added focused proof for all six writes and exact
  ordering, claim-collision no-op behavior, and persist-failure/incomplete-claim
  behavior. Existing website/inbound tests provide the route regressions.

### Measurable acceptance

- Consistent Python `splitlines` measurement across the three production files:
  2,863 before → 2,793 after, a net reduction of **70 lines**. Breakdown:
  `sheets.py` 1,204 → 1,336 (shared helper plus the caller-latency seam),
  `inbound.py` 725 → 622, and `website.py` 934 → 835.
- AST measurement: `process_inbound_texts` 584 → 494 lines (-90) and
  `process_website_message` 356 → 266 (-90). `mirror_sales_turn` is 124 lines.
- The helper attempts lead + optional follow-up + optional deal + optional meeting
  + activity + weekly KPI in the same order, persists the same written count, and
  completes only after outcome persistence succeeds.
- Focused gate passed **99 tests**:

  `uv run pytest tests/unit/test_sheets.py tests/unit/test_website.py tests/unit/test_vnext_inbound_client.py -p no:cacheprovider --basetemp .pytest-tmp/minimality`

- Ruff passed on all six owned Python files. `git diff --check` is recorded below.

## Evidence limits

- No `.env`, secret value, live API, database, deploy, or production log was read.
- Focused tests and owned-file Ruff were run locally; warnings were deprecation
  warnings from Starlette/httpx and pytest-asyncio, not failures.
- Current production configuration and dynamic call frequency were not proven.
- No full-suite result, live API, database, or deployment result is claimed by this
  leaf; the parent/final gate owns those broader checks.
