# Pre-production gap report

**Date:** 2026-08-21 (audit snapshot). First AWS live host later accepted as ADR-014 (2026-08-22) — see Adjustment C; this file is not rewritten as a new audit.  
**Control file:** `MIA_PRE_PRODUCTION_ARCHITECTURE_ADJUSTMENTS.md` v1.0  
**Related:** `AGENTS.md`, `MIA_FINAL_MILE_PLAYBOOK.md`, `docs/PRD.md`, `docs/BUILD_STATUS.md`, `docs/DECISIONS.md`, `docs/PROVIDER_MATRIX.md`, `docs/HANDOFF.md`  
**Suite at inspection:** `uv run pytest` **1196 passed** (parent-verified earlier this day)  
**Code changes in this turn:** none. This is Phase 0 audit only.

## How to read

| Label | Meaning |
| --- | --- |
| Complete | Required behavior exists in code and a test proves the path |
| Partially complete | Real code exists, but the adjustment’s contract is incomplete |
| Missing | No implementation, or only a specified stub |
| Unclear | Evidence is too weak to claim either way |
| Blocked by external setup | Code cannot finish this without operator/OAuth/Meta/AWS action |

Do not treat this report as permission to implement. Assaf must approve it first.

## Repo layout (not a `mia/` nest)

`AGENTS.md` says this workspace root **is** the project. Control files now live here:

- `AGENTS.md`
- `MIA_FINAL_MILE_PLAYBOOK.md` (copied into the repo this turn)
- `MIA_PRE_PRODUCTION_ARCHITECTURE_ADJUSTMENTS.md` (copied into the repo this turn)
- `Mia_AI_Growth_Sales_Operator_PRD_Build_Bible_v1.1.docx` (workspace root, not `docs/`)
- `app/`
- `docs/`

A nested `mia/` directory was **not** created.

---

# Adjustment A — One owner for every capability

**Status: Partially complete**

Written matrix now exists at `docs/CAPABILITY_OWNERSHIP.md` (Phase 1 unit 1; no application code). It records **current** execution owners and flags intent-vs-code where the adjustment wants ManyChat as default IG entry. Registry and adapter inventory remain:

- Runtime registry: `app/core/capabilities.py`
- Adapter inventory: `docs/PROVIDER_MATRIX.md`
- ADRs: `docs/DECISIONS.md`

| Required row | Status | Evidence |
| --- | --- | --- |
| Instagram entry trigger | Partially complete | Direct Graph webhook `app/api/instagram.py`; ManyChat ingest `app/api/manychat.py`. No Meta conversation-routing owner doc. |
| Instagram conversation routing | Missing / Blocked by external setup | Config `MIA_INSTAGRAM_SENDER` (`direct` default) in `app/core/config.py`. No stored routing-owner state. Dual-send is a policy rule, not a Meta API check. |
| Sales reasoning | Complete | `app/graph/orchestrator.py`, `app/domain/sales.py`. ManyChat/Make are not the brain. Tests: `tests/unit/test_domain.py`, `tests/unit/test_objections.py`. |
| Lead state | Complete | Postgres/SQLite models `app/db/models.py`; Sheets is mirror `app/integrations/sheets.py`. |
| Calendar truth | Partially complete | Google Calendar via typed ports (`app/integrations/calendar.py`, `app/integrations/calendar_booking.py`). Create/reschedule alive by fake; live OAuth blocked. Local cancellation is request-only (ADR-013). |
| Campaign truth | Partially complete | Meta insights read `app/integrations/meta_ads.py`. Writes gated. Cache freshness labels not modeled. |
| Campaign write | Missing | R4 stays approval in `app/core/risk.py`. No write adapter. |
| Tool authentication | Partially complete | Per-adapter env keys in `app/core/config.py`. Written auth-owner matrix in `docs/CAPABILITY_OWNERSHIP.md` (actor identity + ingress verify + per-job credentials). No `app/auth` service. Production box is AWS Secrets Manager `mia/prod` (ADR-014); ECS injects env. Shared Composio/OpenAI/IG tokens are credential pools, not extra execution owners. |
| Owner instructions | Partially complete | Propose-only `app/domain/learning.py`. Never active. Activation gated. |
| Business report view | Complete | Sheets 01–10 mirror; Postgres SoR. Tests: `tests/unit/test_sheets.py`. |
| Runtime | Partially complete | First live **host** chosen (ADR-014: Fargate + RDS + SM). `CapabilityId.AWS_RUNTIME` still **specified**; port `app.infra` does not exist. Tests assert it is not alive (`tests/unit/test_wiring.py`). AgentCore / Lambda-graph still blocked on benchmark. |
| Evals | Partially complete | Local Graph Lab `app/evals/`. No LangSmith. Production cannot self-edit graph. |

Acceptance extras: GraphState forbids SDK objects (`app/graph/state.py`) — Complete. Duplicate automated IG replies — Partially complete (one-sender config, no Meta routing proof).

---

# Adjustment B — Instagram routing and ManyChat

**Status: Partially complete** (items 1–4 blocked by external Meta/ManyChat console)

| Required item | Status | Evidence |
| --- | --- | --- |
| Professional account / Meta routing / default owner / disable competing apps | Blocked by external setup | Not in repo. Operator Meta + ManyChat Conversation Routing. |
| Store ManyChat contact and conversation IDs | Partially complete | `parse_manychat_item` sets `subscriber_id`, `conversation_id`, and `thread_id` on inbound item (`app/integrations/manychat.py`); `CanonicalEvent.conversation_id` uses ManyChat conversation id when present. **Persist (2026-08-21):** `channel_identities.manychat_subscriber_id` + `manychat_conversation_id` via `LeadStore.stamp_manychat_identity` on ManyChat prospect path (`provider=manychat` only; first-write-wins; sanitized `[A-Za-z0-9._-]+`; Graph IG inbound does not stamp); identity key remains channel+external_id; migration `migrations/20260821_manychat_identity_ids.sql`. |
| Map ManyChat event to internal envelope | Partially complete | Parsed into inbound item `{id, from, text, subscriber_id, conversation_id, thread_id}` then `CanonicalEvent` via `process_inbound_texts`. Not the suggested `InboundEvent` model. |
| Idempotency key from provider event/message ID | Partially complete | Requires non-empty `event_id`; payloads without it are rejected (no synthesized fallback). Claim: `LeadStore.claim_webhook`. Tests: `tests/unit/test_manychat.py`. |
| Human takeover state + stop outbound | Partially complete | `leads.human_takeover` via owner phrases + `apply_owner_human_takeover` (`app/domain/takeover.py`); prospect MessagePort skip; distinct from `conversation_killed` (sales `stop`); owner resume phrases + `apply_owner_human_resume` clear flag; website HTTP unchanged |
| Contract tests with sanitized real payloads | Partially complete | Unit tests with synthetic bodies plus sanitized fixtures: `external_request_ad.json`, `external_request_story.json`, `external_request_comment.json` (`tests/fixtures/manychat/`). |

Required tests mapping:

| Test | Status | File |
| --- | --- | --- |
| Story reply enters once | Complete | `tests/fixtures/manychat/external_request_story.json`; `tests/unit/test_manychat.py` (parse + webhook duplicate; `ig_trigger_source=STORY`; media dropped). |
| Comment trigger enters once | Complete | `tests/fixtures/manychat/external_request_comment.json`; generic text + `event_id` (no invented DM trigger); parse + webhook duplicate. |
| Ad trigger preserves campaign/ad ids | Partially complete | IG attribution keys on Graph inbound (`app/domain/attribution.py`, `tests/unit/test_instagram.py`, `tests/unit/test_attribution.py`). ManyChat parse **Complete** (sanitized ad/campaign ids via `parse_manychat_item` → `sanitize_instagram_attribution`; names/media dropped). Dual-send / Meta routing still blocked. |
| Duplicate event → no duplicate response | Partially complete | Webhook claim + inbound duplicates. ManyChat synthetic id can collide or miss. |
| Human takeover stops automation | Partially complete | Prospect WhatsApp/IG MessagePort skip when `human_takeover`; graph still runs; owner resume phrases clear flag |
| Resume only via explicit state change | Complete | Owner exclusive resume phrases + `lead_*` via `apply_owner_human_resume` (`app/domain/takeover.py`); R1 `human_takeover_persist` with `enabled=false`; prospect MessagePort allowed again (subject to SHADOW/kill switch); `conversation_killed` unchanged |
| Second connected app cannot duplicate reply | Blocked by external setup | Policy only (`MIA_INSTAGRAM_SENDER`) |
| Same internal identity continues | Partially complete | Channel identity reuse; cross-channel only via handoff token (`app/domain/identity.py`, `tests/unit/test_handoff.py`) |

---

# Adjustment C — Runtime decision

**Status: Partially complete** (first AWS host ADR-014) / **Missing** (AgentCore benchmark) / **Blocked by external setup** (ALB+RDS must actually run)

- First live host (accepted): `docs/DECISIONS.md` ADR-014 — ECS Fargate + RDS + Secrets Manager box. Not AgentCore. Not Lambda as graph or key box.
- AgentCore vs Fargate graph (not ADR yet): `docs/RUNTIME_DECISION_PLAN.md`. Do not write `docs/adr/ADR_RUNTIME_SELECTION.md` until measurements exist.
- Current process: FastAPI + uvicorn (`app/main.py`). Production that process runs on Fargate once Assaf provisions the stack. Laptop uses `.env`.
- `docs/adr/ADR_RUNTIME_SELECTION.md` does not exist.
- `app/infra` does not exist.
- Bible/PRD: AWS specified; first host chosen; `AWS_RUNTIME` not alive until `/health` on the ALB is green.

Do not implement AgentCore, SQS, WAF, or `app.infra` before benchmark + Assaf ADR. Matches this adjustment and `AGENTS.md` hard stop on dumping AWS catalogs into AGENTS.md.

---

# Adjustment D — Ingress, queue, event envelope

**Status: Partially complete**

| Required | Status | Evidence |
| --- | --- | --- |
| `InboundEvent` contract (business_id, correlation_id, raw_event_id, payload_version) | Partially complete | KEEP extending `CanonicalEvent` (no parallel type). `correlation_id` stamped at ingress. `payload_version` allowlist `"1"` stamped in `save_canonical_event` (`canonical_events.payload_version`; migration `migrations/20260821_canonical_payload_version.sql`; not in payload). `provider_event_id` is the provider raw id (string, not UUID). No `business_id` tenant. No SQS. |
| Verify provider | Partially complete | Meta HMAC `app/core/webhooks.py`; Composio HMAC; ManyChat bearer (no request HMAC — documented in `manychat.py`). Website session API is origin/CORS, not HMAC. |
| Store sanitized raw event | Partially complete | `webhook_events` stores provider+id+status+claimed_at plus allowlisted `channel` + `envelope_kind` (`text`\|`audio`\|`empty`\|`referral`) on `claim_webhook` (`webhook_envelope_kind`; first-write-wins on reclaim; never body/`from`/email; migration `migrations/20260821_webhook_envelope.sql`). Canonical payload is truncated/allowlisted. No raw JSON. No `webhook_events.correlation_id`. |
| Idempotency then ack | Partially complete | Claim then process **in the same HTTP request**. No SQS. Ack is the HTTP response after work. |
| Enqueue / process async | Missing | No queue. |
| Correlation ID | Partially complete | `CanonicalEvent.correlation_id` stamped at ingress (prospect reuses sales `run_id`; owner `cor_*`; graph QUALIFICATION/HANDOFF/MEETING_OFFERED share inbound `run_id`); **`tool_runs.correlation_id`** same sanitized value on `persist_tool_outcome` (IG ownership + Gmail `gmail_fetch` share inbound; due-scan / website session-create sheets_mirror empty); migrations `migrations/20260821_canonical_correlation_id.sql` + `migrations/20260821_tool_run_correlation_id.sql`. Envelope `payload_version="1"` on persist (`migrations/20260821_canonical_payload_version.sql`). No SQS envelope. No `webhook_events.correlation_id`. |
| Operation ID on every external write | Partially complete | Tool runs keyed `{inbound_id}:tool:{tool}` (`app/domain/events.py`). Not a general operation-id store. |
| Failed event visible final state | Partially complete | Webhook status + `mia-reconcile` (`app/domain/reconciliation.py`). `--inspect` lists open `reconciliation_findings` (kind + subject_key + sanitized webhook `channel`/`envelope_kind`, cap 50). Replay still Missing. |
| Dead-letter inspect/replay | Partially complete | Read-only inspect: `mia-reconcile --inspect` + `inspect_open_findings` (SoR open rows; webhook findings overlay `channel`/`envelope_kind`; no PII/raw tokens). **Replay still Missing** (never repairs). |

---

# Adjustment E — Idempotency

**Status: Partially complete**

Application-level `IdempotencyStore` Protocol: **Partially complete** (`app/domain/idempotency.py`; `LeadStore.claim_webhook` reclaims stale in-flight `received` via `is_stale_received`/`STALE_AFTER_SECONDS` (300s) from `app/domain/reconciliation.py`; `failed`→`received` retry unchanged; `processed`/`sent` stay unique; + `claim_operation`/`complete_operation`/`fail_operation`/`get_operation_result`; in-flight TTL + completed result store on `claim_operation`; tests `tests/unit/test_idempotency.py`; capability `fde_idempotency` ALIVE; R1 `claim_operation` writes `idempotency_records`; Powertools **DEFER**).

Existing first-write-wins unique keys (not Powertools):

| Action | Mechanism | File |
| --- | --- | --- |
| Webhook | `claim_webhook` unique `(provider, provider_event_id)` | `app/db/store.py` |
| Generic operations | `claim_operation` unique `(scope, key)` on `idempotency_records` | `app/db/store.py` / `app/domain/idempotency.py` |
| Calendar booked persist | `claim_operation(scope=calendar_create, key={lead_id}:booked)` before canonical | `app/domain/calendar_booking.py` |
| Calendar reschedule persist | `claim_operation(scope=calendar_reschedule, key={lead_id}:rescheduled:{target_key})` before canonical | `app/domain/meeting_changes.py` |
| Approval persist | `claim_operation(scope=approval, key={lead_id\|campaign_id}:approval:{action})` before APPROVAL_REQUIRED canonical | `app/domain/approvals.py` |
| Owner task persist | `claim_operation(scope=owner_task, key={provider}:{provider_event_id})` before `save_owner_task` | `app/api/inbound.py` |
| Canonical events | unique `(provider, provider_event_id)` | `CanonicalEventRow` |
| Outbound message | `{inbound_id}:out` | `app/domain/events.py` |
| Calendar create | private booking key + event list lookup | `app/integrations/calendar_booking.py` |
| Reschedule | first-write canonical per target key | `app/domain/events.py` |
| Cancellation request | `claim_operation(scope=calendar_cancellation, key={inbound_id}:cancellation)` then first-write canonical `{lead_id}:cancellation_requested` | `app/domain/meeting_changes.py` |
| Sheets upsert | `claim_operation(scope=sheets_mirror, key={inbound_id}:sheets:{sales\|session\|campaign\|content})` then Composio upsert by tab key | `app/integrations/sheets.py` / inbound + website |
| Follow-up | `claim_operation(scope=follow_up, key={inbound_id}:followup)` then one row per lead | `app/domain/followups.py` / inbound + website |
| Approval | unique lead+action | `ApprovalRow` |

Missing concurrency cases: queue redelivery (no queue), campaign write execute (no Meta writes), email send (no send). Generic `claim_operation` allowlist scopes: `calendar_create`, `calendar_reschedule`, `canonical`, `approval`, `owner_task`, `sheets_mirror`, `follow_up`, `calendar_cancellation` — **`approval` wired** on APPROVAL_REQUIRED persist (`proposal_handoff` + `campaign_write`; `complete_operation` `{"ok": true}`); **`owner_task` wired** on owner inbound `save_owner_task` persist (`{provider}:{provider_event_id}` → `complete_operation` `{"ok": true}`; ack/analytics/takeover unchanged; duplicate claim skips second persist); **`sheets_mirror` wired** on Sheets persist (`{inbound_id}:sheets:{sales|session|campaign|content}` → `complete_operation` `{"ok": true}`; inbound key, not lead; later inbound still refreshes snapshot); **`follow_up` wired** on `apply_follow_up_policy` persist (`{inbound_id}:followup` → `complete_operation` `{"ok": true}`; inbound key, not lead; later inbound still cancels/recovers/creates; due-scan and booking cancel unclaimed; **send still gated**); **`calendar_cancellation` wired** on local cancellation persist (`{inbound_id}:cancellation` → `complete_operation` `{"ok": true}`; inbound key, not lead; already-requested short-circuits before claim; empty inbound_id keeps today’s write; **provider delete still gated**); **execute still gated** (no due-scan execute, no Meta/Gmail/follow-up send). In-flight TTL + completed result store on `claim_operation` (`status`/`expires_at`/`result_json`; migration `migrations/20260821_idempotency_inflight.sql`); lost-response reread via `get_operation_result`. Webhook in-flight TTL reclaim on `claim_webhook` for stale `received` only (same 300s as reconciliation flag scan; reconciliation stays flag-only).

`claim_webhook` returns False for fresh in-flight `received`; stale `received` (empty/unparseable `claimed_at` or >300s) reclaims with refreshed `claimed_at`; `processed`/`sent` always False.

Acceptance “no test can produce duplicates”: **Complete** for listed persist actions in `tests/unit/test_idempotency_persist_paths.py` (12 paths; `LIVE_CLAIM_SCOPES` ⊆ allowlist). Queue redelivery, campaign-write execute, and email send still missing (gated; no SQS / no Meta writes / no Gmail send). Powertools DEFER.

---

# Adjustment F — Critical vs convenient tools

**Status: Partially complete**

Tier 1 typed ports exist (not Composio catalogs dumped into the model):

- DB: `app/db/store.py`
- Messaging: `app/integrations/whatsapp.py`, `app/integrations/instagram.py`, `app/integrations/base.py`
- Calendar writes: `CalendarBookingPort`
- Website events: `app/api/website.py`
- Identity merge: in-memory + handoff link only; unmerge R5
- Meta campaign writes: **Missing** (intentional gate)
- Approval execution: persist-only, no execute

Timeout/retry/verify-after-write: calendar create/reschedule have verify; most other adapters fail-closed without a shared retry policy object.

Tier 2 versioned registry `app/tools/registries/mia_preloaded_tools.py`: **Partially complete** (frozen `PreloadedTool` catalog re-exports adapter pin constants; `preloaded_tool()` lookup only; capability `preloaded_tools` ALIVE; customer graph still has **zero** Composio tools). Pins:

`GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`, `GOOGLECALENDAR_FIND_FREE_SLOTS`, `GOOGLECALENDAR_EVENTS_LIST`, `GOOGLECALENDAR_CREATE_EVENT`, `GOOGLECALENDAR_EVENTS_GET`, `GOOGLECALENDAR_PATCH_EVENT`, `GOOGLESHEETS_UPSERT_ROWS`, `METAADS_GET_INSIGHTS`, `LINKEDIN_GET_MY_INFO`, plus Gmail trigger `GMAIL_NEW_GMAIL_MESSAGE` and direct LinkedIn `member_post_analytics`.

No Drive. No Gmail draft/send. LinkedIn writes not pinned.

Tier 3 dynamic discovery: **Missing** (and must stay off for customer path). Customer graph has **zero** Composio tools — NBA is code, reply is `SalesReplyPort`. That already satisfies “model cannot discover a dangerous write tool during a normal lead conversation.”

---

# Adjustment G — Adapter boundaries

**Status: Partially complete**

- GraphState comment and fields: no SDK objects — Complete (`app/graph/state.py`).
- Domain consumes typed records (slots, insights, snippets) — Complete in adapters.
- Suggested Protocol names (`CalendarGateway`, `MessagingGateway`, `CampaignGateway`) — Partially complete as `CalendarPort` / `CalendarBookingPort` / `MessagePort` / `MetaAdsPort`. Campaign **apply_change** does not exist.
- Result classification success/partial/stale/retryable/unauthorized/rate_limited/malformed — Partially complete. `ToolOutcome.status` allowlist is `ok|denied|empty|error|unauthorized|rate_limited|malformed|retryable|partial|stale` (`app/domain/tools.py`); `ok` kept as success token (no `success`). `AdapterHttpError` + `tool_status_from_http` map HTTP codes; wired on Meta insights (`ComposioMetaAdsPort.get_insights`), LinkedIn analytics (`DirectLinkedInAnalyticsPort.get_member_analytics`), LinkedIn profile (`ComposioLinkedInPort.get_my_profile`), Calendar free/busy (`ComposioCalendarPort.find_free_slots`), Calendar booking (`ComposioCalendarBookingPort` list/create/GET/PATCH; domain stamps classified status and keeps Hebrew retry; create/PATCH HTTP still verifies), Gmail fetch (`ComposioGmailPort.fetch_message`), Instagram media-list (`GraphInstagramInsightsPort`, per-media 400 still skips), Sheets upsert (`ComposioSheetsPort`; `mirror_*` catch and skip), and research (`FirecrawlSearchPort.search`); enrich/hydrate/booking paths stamp classified status without changing customer/owner acks. WhatsApp send / Instagram send / OpenAI STT / WhatsApp media download raise AdapterHttpError then wrap MiaError 502 so claim rollback stays; send_inbound_reply still only catches RuntimeError; HTTP 200 empty STT stays TranscriptionError; unsupported IG host and media missing-url/host/size are not HTTP. OpenAI sales-reply _complete raises AdapterHttpError on HTTP/transport; compose catches then fallback then canned (never 502); HTTP 200 empty stays None. OpenAI thread-summary _complete raises AdapterHttpError on HTTP/transport; summarize catches then fallback then canned unclear (never 502); HTTP 200 empty stays None. `partial`/`stale` allowlisted; **`partial` wired** on LinkedIn member analytics when some metrics populate and others are omitted (`enrich_linkedin_analytics_ack`; ack line unchanged). `stale` unused (no cache clock on the live path). No `CampaignGateway.apply_change`.

---

# Adjustment H — Visual automation platforms

**Status: Complete** (by absence, which is the required default)

Make and n8n are not in the live sales path. ManyChat is ingest/sidecar only (`app/api/manychat.py`). No Make/n8n workflows in repo. Do not add both.

---

# Adjustment I — Latency and performance budget

**Status: Partially complete** (targets written; graph.invoke + port-call + all claimed Sheets-tab tool latency measured; tokens from OpenAI usage on successful compose; cost_usd still 0)

- `docs/PERFORMANCE_BUDGET.md` exists. Targets copied from Adjustment I. Honest note: webhook ack is not <500 ms because work runs in-process.
- Hot-path already avoids browser, multi-agent debate, dynamic tool search, RAG dump — Partially complete by architecture.
- Instrumentation (node/queue/model/tool/db latency, tokens, cost, retries, cache hit) — Partially complete: `ai_runs.latency_ms` measured around `graph.invoke` on website + prospect inbound (`elapsed_ms` in `app/domain/ai_runs.py`); `tokens_in`/`tokens_out` parsed from OpenAI `usage.prompt_tokens` / `usage.completion_tokens` when live compose succeeds and lint passes (`ComposeResult` on `SalesReplyPort`; canned/fallback/kill-switch paths stay 0); `cost_usd` remains **0** (no invented pricing). `tool_runs.latency_ms` measured on port wall-clock for research/meta/linkedin/calendar enrich + STT, **and** sales-tab / session-tab / campaign-tab / content-tab Sheets upserts after claim (`sheets_mirror_outcome` / `sheets_tab_mirror_outcome`; kill-switch denied still measured). Denied-before-call enrich stays 0. Campaign/content use distinct tools so they do not collide with `sheets_mirror`. No node timers.

---

# Adjustment J — Model router

**Status: Partially complete**

- Runtime model ids are env/eval config, not hard-coded brains (`app/core/models.py`, `MIA_SALES_MODEL`). Cursor build-time policy is in `AGENTS.md`.
- Typed task classes (route/extract/transcribe/sales/reframe/objection/campaign/research/summary/humanity/safety) as a **lookup catalog**: **Complete** — `app/domain/policies/task_classes.py`; `TaskClass` StrEnum + frozen `TaskClassPin`; `task_class_pin()` lookup only; capability `model_task_classes` ALIVE; **not wired** into inbound/graph.
- Live typed model router (runtime selection by task class): **Missing**.
- Frozen benchmark sizes (20 routing, 30 extract, 50 sales turns, …): **Partial** — `routing_v1` (20 isolated owner classify cases, `run_routing_eval`, 20/20), `extract_v1` (30 isolated extract cases, `run_extract_eval`, 30/30), `sales_v1` (50 one-shot NBA+reply cases, `run_sales_eval`, 50/50), `objection_v1` (20 extract→NBA→reply cases, `run_objection_eval`, 20/20), `calendar_v1` (20 ADR-012 `carve_policy_slots` cases, `run_calendar_eval`, 20/20), `campaign_v1` (20 `analyze_insights` + `format_recommendation_line` cases, `run_campaign_eval`, 20/20), and `safety_v1` (20 adversarial sales extract→NBA→reply + snippet sanitizer cases, `run_safety_eval`, 20/20) scored in Graph Lab; transcript still **Missing**. Plan: `docs/MODEL_BENCHMARK_PLAN.md`.
- Safety frozen set (`safety_v1`, 20 cases): **Complete** — 12 untrusted-text-as-data sales cases (extract→NBA→canned reply+lint+forbidden) + 8 `sanitize_snippets` URL/title cases; no LLM/judge/inbound.
- `docs/MODEL_ROUTING_DECISION.md`: **Missing** (correct — write after scoring).

---

# Adjustment K — Transcription

**Status: Partially complete**

- Text-only Mia, no voice-agent runtime — Complete.
- Provider: GPT Transcribe `app/integrations/transcribe.py`; in-memory audio; optional fallback model. Tests: `tests/unit/test_whatsapp_stt.py`.
- Second-provider benchmark (Hebrew/English/mixed/noisy): **Missing**. Blocked by real sanitized notes if we need live scoring.
- Stored transcript row: Postgres `voice_transcripts` (`VoiceTranscriptRow`) with text + channel provider event ids. **Complete** on row: `stt_provider`, `stt_model`, `language`, `duration_ms`, `confidence` (provider JSON key only when present), `cost_usd` (always 0), `retention_status` (`text_only` on save; audio discarded). Migration `migrations/20260821_voice_transcript_stt_meta.sql` + `migrations/20260821_voice_transcript_retention.sql`.

---

# Adjustment L — Identity and permissions

**Status: Partially complete**

| Required | Status | Evidence |
| --- | --- | --- |
| Who / channel / verified / role before write | Partially complete | Owner = `MIA_WHATSAPP_OWNER_PHONES` exact set (`app/core/config.py`, `app/api/inbound.py`). Risk `assert_allowed` before writes (`app/core/risk.py`). Auth-owner matrix in `docs/CAPABILITY_OWNERSHIP.md` names actor / ingress verify / per-job credentials. No `business_id` multi-tenant. Authorized operator vs service account still not modeled. |
| Do not infer owner from name/text/style | Complete | Phone allowlist; `tests/unit/test_adversarial_identity.py` (I am Assaf, forwarded owner command, Gmail display-name spoof). |
| Adversarial tests listed in the control file | Complete (webpage-scrape suite) | Adjustment L cases in `tests/unit/test_adversarial_identity.py` plus full webpage-scrape adversarial suite in `tests/unit/test_webpage_scrape_adversarial.py` (http/javascript/data URLs dropped; https path/query not in ack; excerpt injection not in ack/TOOL_RESULT; one research task; meeting brief title+host only). Meta write replay still missing (writes gated). Existing: `tests/unit/test_gmail.py::test_gmail_email_body_is_data_not_instructions`. |

---

# Adjustment M — Approval object

**Status: Partially complete** (object fields persist-only; execute still gated)

`ApprovalRow` (`app/db/models.py`): `lead_id` (nullable for campaign rows), `channel`, `action`, `risk`, `payload_hash`, `decision`, `approver` (always `""` on decide), `resource_type` (`lead` | `campaign`), `resource_id` (bound lead_id or Meta campaign id), `expires_at` (UTC ISO; 24h TTL on persist), `approval_id` (`apr_` + 12 hex; unique; first-write-wins), `business_id`/`actor_id` (reserved `""` — no tenant, no PII), `proposed_parameters` (compact JSON of payload identity keys; capped 255 chars; fail-closed `""` if over), `approved_at` (UTC ISO on approve only), `executed_at`/`execution_operation_id`/`result` (reserved `""` — execute not wired).

Campaign-bound exact approval: **Partially complete** (persist-only R4 `campaign_write`; owner WhatsApp request/decide phrases; hash + expiry + unbound/expired fail-closed; **never calls Meta**; `named_write_may_auto` R4 stays False; `MIA_META_WRITE` unwired). Lead `proposal_handoff` (R3) unchanged. Generic WhatsApp approve/reject is persist-only for both resource types (`app/domain/approvals.py`, `tests/unit/test_approvals.py`); stale or unbound pending rows cannot be decided (`expired` / `unbound` status; fail-closed empty `expires_at`). Canonical `APPROVAL_REQUIRED` stays `pending` after decide (intentional first-write-wins). No re-read-before-execute because execute is gated.

Operator: `migrations/20260821_approval_object_fields.sql` on existing Postgres/file sqlite DBs (prior: `migrations/20260821_approval_binding.sql`, `migrations/20260821_approval_campaign_resource.sql`).

---

# Adjustment N — Data truth and freshness

**Status: Partially complete** (`campaign_metrics` + `instagram_content_metrics` + `linkedin_content_metrics` + `linkedin_profile` + `research_snippets` + `calendar_availability` + `gmail_results` + `opt_out_status` + `campaign_budget_status` + `conversation_ownership` + `owner_permissions` + `lead_recent_messages` + `website_session_events` stamps wired; versioned knowledge RAG missing)

Freshness policy type (`source`, `fetched_at`, `version`, `freshness status`) on retrieved facts: **Partially complete** — lookup+stamp catalog exists (`app/domain/policies/freshness.py`; `FreshnessClass`/`FreshnessStatus`; frozen `FreshnessPin`/`FreshnessStamp`; `freshness_pin()` + `stamp_freshness()`; capability `freshness_policy` ALIVE). **`campaign_metrics` stamp wired** on Meta insights enrich (`enrich_analytics_ack` in `app/integrations/meta_ads.py`); **`instagram_content_metrics` stamp wired** on organic IG insights enrich (`enrich_content_insights_ack` in `app/integrations/instagram_insights.py`; tool `instagram_insights`; audit `tool_runs.freshness` only; ack unchanged); **`campaign_budget_status` stamp wired** on Meta `this_month` pacing fetch (`meta_ads_pacing` via `campaign_budget_outcome`; audit `tool_runs.freshness` only; does not overwrite `campaign_metrics` on `meta_ads_insights`); **`calendar_availability` stamp wired** on offer + owner read (`prepare_meeting_offer`, `apply_owner_calendar` via `calendar_availability_outcome` in `app/integrations/calendar.py`); **`gmail_results` stamp wired** on Composio empty-body hydrate only (`gmail_results_outcome` in `app/integrations/gmail.py`; tool `gmail_fetch`; non-empty trigger body skips port + stamp); **`opt_out_status` stamp wired** when `leads.conversation_killed` changes (`opt_out_status_outcome` + `apply_conversation_kill_policy` in `app/domain/conversation_kill.py`; tool `opt_out_status`; no flood on unchanged turns); **`conversation_ownership` stamp wired** on prospect Instagram inbound once per lead (`conversation_ownership_outcome` in `app/domain/ownership_freshness.py`; reads `settings.instagram_sender`; `{lead_id}:ownership` first-write-wins); **`owner_permissions` stamp wired** on owner inbound once per owner external id (`owner_permissions_outcome`; `owner:{from}` first-write-wins); **`lead_recent_messages` stamp wired** when due-scan counts `message_out` (`lead_recent_messages_outcome` in `app/domain/followups.py`; `{lead_id}:followup-scan:{today}`); **`website_session_events` stamp wired** when analytics enrich runs `count_behavior_events` on WATCH funnel path (`website_session_events_outcome` in `app/integrations/meta_ads.py`; separate tool from `meta_ads_insights`); **`research_snippets` stamp wired** on owner public search enrich (`enrich_research_ack` in `app/integrations/research.py`; tool `research_search`; audit `tool_runs.freshness` only; ack unchanged; no URLs/excerpts on the row); all persisted on `tool_runs.freshness` (audit only; canonical TOOL_RESULT payload unchanged; customer ack unchanged). Versioned knowledge RAG still **Missing**.

Live calendar availability is read at offer/reschedule time (not cached as truth). Campaign metrics and calendar slot reads stamp freshness (`cached`/`live`/`unverified`) on `tool_runs` only — customer ack text unchanged. Opt-out is follow-up cancel + conversation_killed, not a live provider opt-out read.

Mia “cannot verify” copy when live truth missing: calendar degrades to static/canned; insights omit missing metrics (never zero-fill). Stamp helper exists; all live/short-cache catalog facts now stamp `tool_runs.freshness`. Versioned knowledge pins stay `unverified` until RAG exists.

Versioned knowledge RAG (services, pricing, playbooks): **Missing** (pins mark `versioned_knowledge` → `unverified`; no knowledge SoR).

Operator: run `migrations/20260821_tool_run_freshness.sql` on existing Postgres/file sqlite DBs.

---

# Adjustment O — Observability and audit

**Status: Partially complete**

Present: structured-enough logs with redaction (`app/core/logging.py`, `app/core/redact.py`); `ai_runs`; `tool_runs` (including `correlation_id` join to ingress); canonical events; reconciliation CLI.

Present also: `tool_runs.correlation_id` joins tool audit to the ingress canonical / `ai_runs.run_id` trace (no queue). Missing: correlation ID spanning a queue (no SQS); CloudWatch; LangSmith/Langfuse; alerts (duplicate side-effect, queue age, DLQ, auth failure spike, latency SLA, cost anomaly, spend-without-leads alert as ops — analysis exists as owner-ack text, not an alert); dashboards. Operator runbook: `docs/RUNBOOK.md` (kill switch, named flags, due-scan, reconcile, rollback).

Tokens on `ai_runs` come from OpenAI usage when live compose succeeds; `cost_usd` remains 0. Allowlisted `automation_mode`, frozen `prompt_version=sales_reply_v1`, and `decision_confidence="1.0"` (deterministic NBA pin; no LLM self-score) are stamped on website + prospect inbound runs (audit; HYBRID send still unwired; prompt body not stored).

---

# Adjustment P — Graph structure

**Status: Partially complete**

Recommended subgraphs vs actual:

| Recommended | Actual |
| --- | --- |
| Main orchestrator | FastAPI routers + `process_inbound_texts` (`app/api/inbound.py`) |
| Customer sales subgraph | Single node `sales_next_action` in `app/graph/orchestrator.py` |
| Owner operations | Deterministic classify in `app/domain/owner_tasks.py`, not a graph |
| Calendar / campaign / follow-up / approval / research | Domain modules called from inbound/website, not LangGraph subgraphs |

Graph rules mostly hold: typed serializable state; deterministic NBA in code; no production self-edit; Graph Lab local; graph_version recorded as `sales_v1`. Long research is sync Firecrawl on owner/brief paths, not an async worker subgraph. Approval interrupt in LangGraph: **Missing** (persist-only row instead).

Premature subgraph extraction is **not** required to preserve the sales brain.

---

# Adjustment Q — Sales quality must not regress

**Status: Partially complete**

Preserved in code: workflow-first NBA (`app/domain/sales.py`), one-question qualify, Hebrew canned (`app/graph/replies.py`), humanity linter checks 3/4/6/9 (`app/domain/humanity.py`), no forced meeting for poor fit (NBA), handoff when `owner_required`.

Judgment checks 1/2/5/7/8/10 (rewrite) still later. Playbook writing suite exists (`writing_v1`) but is smaller than the model-router benchmark table. Humanity tests: `tests/unit/test_humanity.py`.

---

# Adjustment R — Feature flags

**Status: Partially complete** (named flags exist; calendar create + reschedule PATCH gated; others fail-closed unused)

Existing runtime knobs: `MIA_KILL_SWITCH`, `MIA_DEMO_MODE`, plus credential emptiness (disabled ports). R4/R5 are **not** env-overridable (`app/core/risk.py`).

Named flags in `app/core/config.py` + `app/core/write_flags.py` (all default **false**; tests set `MIA_CALENDAR_WRITE=true` in `tests/conftest.py`):

| Flag | Wired this slice |
| --- | --- |
| `MIA_CALENDAR_WRITE` | **Yes** — gates `calendar_create` and `calendar_patch_event` (reschedule); reads not gated |
| `MIA_AUTO_FOLLOWUP` | No — fail-closed; send still unwired |
| `MIA_GMAIL_SEND` | No |
| `MIA_META_WRITE` | No — cannot override R4 |
| `MIA_DYNAMIC_TOOL_DISCOVERY` | No |
| `MIA_BROWSER_AUTOMATION` | No |
| `MIA_AUTO_REPLY_INSTAGRAM` | No — SHADOW owns prospect send |

`named_write_may_auto` returns False for R4/R5 always. Flags do not override kill switch.

Rollout modes (staging only / allowlist / percentage): **Missing**. Kill switch is global, not per capability. Operator flag table + rollback: `docs/RUNBOOK.md` §4 and §9.

---

# Implementation phases (control file §22)

| Phase | Status | Note |
| --- | --- | --- |
| 0 Repository gap audit | Complete (this file) | No code |
| 1 Documentation and ownership | Complete (docs) | `docs/CAPABILITY_OWNERSHIP.md`, `docs/PERFORMANCE_BUDGET.md`, `docs/RUNTIME_DECISION_PLAN.md`, `docs/MODEL_BENCHMARK_PLAN.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md`, `docs/PRODUCTION_BUILD.md`. No runtime migration. Application-code phases still wait on Assaf. |
| 2 Security-critical foundations | Partially complete | Identity/risk/kill switch exist; approval binding, envelope, human takeover, IdempotencyStore incomplete |
| 3 Tool boundary | Partially complete | Typed adapters + pins; no registry file; no discovery (good) |
| 4 Channel reliability | Partially complete | WA/IG/Gmail/Calendar/LinkedIn live-or-fake; ManyChat routing external; duplicate tests incomplete |
| 5 Runtime benchmark | Missing | |
| 6 Performance and model routing | Partially complete | Eval harness + `docs/PERFORMANCE_BUDGET.md` + `docs/MODEL_BENCHMARK_PLAN.md`; P95 unmeasured; `MODEL_ROUTING_DECISION.md` not written |
| 7 Production write operations | Missing / gated | Calendar write alive by fake; follow-up/Gmail/LinkedIn/Meta writes not enabled |
| 8 Full acceptance | Partially complete | In-process §23 stories in `tests/e2e/test_preprod_stories.py`; live staging OAuth/Meta writes still blocked |

---

# Mandatory end-to-end scenarios (§23)

| Scenario | Status | Evidence |
| --- | --- | --- |
| 1 Instagram lead → meeting → Calendar → Sheet → owner notify → trace | Partially complete | In-process composed story: `tests/e2e/test_preprod_stories.py::test_story_instagram_lead_meeting_calendar_sheet_notify_trace` (fakes; no live OAuth). Live staging still blocked. |
| 2 WhatsApp lead → dedupe → sales → follow-up → human takeover → no duplicate send | Partially complete | In-process composed story: `tests/e2e/test_preprod_stories.py::test_story_whatsapp_dedupe_sales_followup_takeover_no_duplicate_send`. Human takeover row **Complete** in-process. Follow-up send still gated. |
| 3 Owner voice → verify → STT → understanding check → task → text → audit | Partially complete | Alive except execute and transcription metadata/benchmark. In-process story: `tests/e2e/test_preprod_stories.py::test_story_owner_voice_understanding_check`. Tests: `test_whatsapp_stt.py`, `test_owner_tasks.py`. |
| 4 Calendar race / no double book | Partially complete | In-process story: `tests/e2e/test_preprod_stories.py::test_story_calendar_no_double_book`. Conflict recheck + verify in booking/reschedule tests (`test_calendar_booking.py`, `test_calendar_gate2.py`). Not live staging. |
| 5 Campaign change approval → write → verify | Missing / gated | Test proves gate: `tests/e2e/test_preprod_stories.py::test_story_campaign_change_write_stays_gated`. Analysis alive; Meta write gated. |
| 6 Duplicate provider event | Partially complete | In-process story: `tests/e2e/test_preprod_stories.py::test_story_duplicate_provider_event`. Webhook claim tests across WA/IG/Gmail/ManyChat. |
| 7 Provider timeout → bounded retry → fallback → visible failure | Partially complete | In-process story: `tests/e2e/test_preprod_stories.py::test_story_provider_timeout_fallback_visible`. Sales reply fallback then canned; STT fallback; calendar retry copy. No shared retry budget. DLQ **inspect** via `mia-reconcile --inspect`; replay still Missing. |
| 8 Prompt injection from email/webpage | Partially complete | In-process story: `tests/e2e/test_preprod_stories.py::test_story_prompt_injection_email_or_website`. Gmail body is data (`test_gmail_email_body_is_data_not_instructions`); website prompt-injection canned-path and owner-research scrape-injection ack in `test_adversarial_identity.py`; full webpage-scrape adversarial suite in `tests/unit/test_webpage_scrape_adversarial.py` (in-process fakes); research snippets sanitized (`sanitize_snippets`, `AdapterHttpError` on Firecrawl HTTP). |

---

# Release gates (§24)

## Gate A — Security

| Item | Status |
| --- | --- |
| Owner identity cannot be spoofed | Partially complete (phone allowlist; `test_adversarial_identity.py` + full webpage-scrape suite in `test_webpage_scrape_adversarial.py`; Meta write replay still gated) |
| Cross-lead data access blocked | Partially complete (lead review requires `lead_*` in owner message; prospect `review lead_*` and two-WhatsApp SalesState isolation covered in `test_adversarial_identity.py`) |
| Tool permissions checked outside model | Complete for current writes (`assert_allowed` + typed ports) |
| Approval bound to exact operation | Partially complete (expiry + resource hash for lead R3 and campaign R4 persist-only; no Meta execute) |
| Secrets excluded from logs | Complete (`app/core/redact.py`) |
| Prompt injection tests pass | Partially complete (Gmail body-as-data + website canned-path + owner-research scrape-injection ack + full webpage-scrape suite in `test_webpage_scrape_adversarial.py`) |
| Channel signatures verified | Partially complete (Meta + Composio HMAC; ManyChat bearer; website no HMAC) |

## Gate B — Reliability

| Item | Status |
| --- | --- |
| Duplicate events safe | Partially complete |
| Duplicate writes safe | Partially complete (booking/reschedule/`sheets_mirror`/`follow_up` inbound claim) |
| Bounded retries | Partially complete |
| Dead-letter path | Partially complete (inspect via `mia-reconcile --inspect`; replay Missing) |
| Provider timeout handled | Partially complete |
| Human takeover works | Complete (in-process: `tests/e2e/test_preprod_stories.py` story 2; `tests/unit/test_takeover.py`) |
| Kill switch works | Complete (`MIA_KILL_SWITCH`, tests across inbound/website) |

## Gate C — Performance

All items Missing or Partially complete (no measured P95; hot path is already narrow; research is sync; `ai_runs` cost 0).

## Gate D — Sales quality

| Item | Status |
| --- | --- |
| Hebrew/English test sets | Partially complete (`writing_v1`, `buyers_v1`) |
| Humanity linter | Partially complete (deterministic 3/4/6/9) |
| One-question policy | Complete in NBA/qualify copy |
| No unsupported claims | Partially complete (linter block list) |
| Correct handoff | Complete persist path |
| No forced meetings for poor fit | Complete in `select_next_action` |

## Gate E — External writes

| Item | Status |
| --- | --- |
| Calendar staging E2E | Blocked by external setup (OAuth) |
| Outbound staging E2E | Partially complete (WA/IG send adapters exist; follow-up send gated) |
| LinkedIn write approved or disabled | Complete as disabled |
| Meta write exact and verified | Missing / gated |
| Sheet sync idempotent | Partially complete (`claim_operation(scope=sheets_mirror)` per inbound; Composio still upserts by tab key; living snapshot updates on a later inbound) |

## Gate F — Operations

Dashboards and automated alerts: **Partially complete** (first-live ALB `UnHealthyHostCount` + `HTTPCode_ELB_5XX_Count` alarms exist; no SNS pager). Ordered go-live: `docs/PRODUCTION_BUILD.md`. Runbook + named-flag docs + rollback steps: **Partially complete** (`docs/RUNBOOK.md`). External setup: `docs/PROVIDER_MATRIX.md` + `.env.example` + `docs/EXTERNAL_SETUP_CHECKLIST.md`. Kill switch: `MIA_KILL_SWITCH` + `GET /health`. Liveness/readiness: `GET /health/live` and `GET /health/ready`. `MIA_ENV=prod` unmounts `/docs` `/redoc` `/openapi.json`. Do not mark this gate Complete without SNS paging and a measured runtime.

---

# Highest-risk gaps (ordered)

1. **Two Instagram senders** — ManyChat Conversation Routing is not configured in-repo. Human takeover + resume exist in-process; duplicate customer replies remain the highest product/safety risk once both apps are connected.
2. **Synchronous webhook = production timeout risk** — no API Gateway/Lambda ack + SQS. Long graph/model/tool work runs inside the HTTP request (`app/api/whatsapp.py`, `app/api/inbound.py`).
3. ~~**Write features can go live without named flags**~~ — **Partially fixed:** `MIA_CALENDAR_WRITE` gates calendar create and reschedule PATCH (default false). Follow-up/Gmail/Meta/IG auto-reply flags exist but unwired.
4. **Approvals are not exact, expiring, resource-bound for execute** — lead R3 + campaign R4 persist-only rows exist (hash, expiry, resource binding, `approval_id`/`proposed_parameters`/`approved_at`); `business_id`/`actor_id` reserved empty; Meta/Gmail send execute still gated (`executed_at`/`result` stay empty).
5. **`IdempotencyStore` in-flight + lost-response** — webhook `received` in-flight TTL reclaim **Complete** (reclaim stuck `received` via `is_stale_received`; `processed`/`sent` still unique); `claim_operation` TTL + completed result store alive; Powertools decorator still DEFER; queue/campaign/email cases still missing.
6. **Identity adversarial suite partially complete** — owner spoof / cross-lead / unbound approval / duplicate-identity isolation / owner auth restart / revoked owner / owner-research scrape-injection ack covered in `tests/unit/test_adversarial_identity.py`; full webpage-scrape suite **Complete** in `tests/unit/test_webpage_scrape_adversarial.py` (in-process fakes). Meta write replay still missing (writes gated).
7. **AWS/AgentCore absent** — correct to stay specified; production acceptance cannot pass Gate F/C without a measured runtime.

---

# Recommended build order (after Assaf approves this report)

Follow the control file phases. Do not skip to AgentCore or Meta writes.

1. **Phase 1 docs only:** ownership matrix, performance budget, model-benchmark plan, external-readiness checklist, runtime *plan* (not migration).
2. **Phase 2 security:** feature flags (non-overridable R4/R5 preserved), human takeover, approval object fields (expiry + resource hash) without enabling execute, `IdempotencyStore` wrapping existing unique keys, ingress `correlation_id` on `CanonicalEvent` or a thin `InboundEvent`.
3. **Phase 3 tools:** `mia_preloaded_tools.py` registry generated from current pins; result-status vocabulary; keep dynamic discovery **off**.
4. **Phase 4 channels:** ManyChat event_id required; store subscriber + conversation ids; contract fixtures; operator Meta routing checklist (blocked externally).
5. **Phase 5:** runtime benchmark harness vs current FastAPI; then ADR. Migrate only if Assaf chooses.
6. **Phase 6:** latency fields on `ai_runs` / tool_runs; router task classes; enlarge eval sets.
7. **Phase 7 writes:** calendar staging OAuth first, then follow-up send, Gmail draft, … Meta last — each behind flags.
8. **Phase 8:** eight E2E stories + release gates.

---

# First small implementation unit (proposed; do not start until Assaf approves)

**Create `docs/CAPABILITY_OWNERSHIP.md` only.** No application code.

Purpose: satisfy Adjustment A’s written matrix using evidence already in `capabilities.py` and `PROVIDER_MATRIX.md`.  
Dependencies: this report.  
Acceptance: every row in the control-file table filled with execution owner, SoR, fallback, must-not-own, and file pointers.  
Out of scope: AWS, flags, subgraphs, registry Python module.

That is Phase 1, unit 1.

---

# Better alternatives that deserve Assaf’s decision

Do not implement these until Assaf chooses KEEP / ADOPT / TEST BOTH / DEFER.

### 1. Nested `mia/` directory vs current workspace root

**CURRENT BIBLE / AGENTS DIRECTION:** Workspace root is the repo; do not nest `mia/`.  
**NEW ALTERNATIVE:** Move everything under `mia/` as in the adjustment note.  
**WHY IT MAY BE BETTER:** Matches a mental “product folder.”  
**QUALITY / RELIABILITY / COST:** Zero product benefit; breaks imports, pytest, uvicorn, Cursor workspace.  
**LOCK-IN:** None.  
**FILES AFFECTED:** Entire tree.  
**TEST PLAN:** N/A — mechanical move.  
**RECOMMENDATION: KEEP** current root. Control files are already at root.

### 2. ManyChat as default Instagram conversation owner

**CURRENT:** `MIA_INSTAGRAM_SENDER=direct`; ManyChat is ingest sidecar. ADR-015 specifies Composio for IG send/insights when ports land; inbound stays Meta webhook.  
**NEW:** ManyChat owns routing; Mia replies through Dynamic Block.  
**WHY IT MAY BE BETTER:** Official Conversation Routing; comment/story/ad entry.  
**SECURITY:** Dual-send risk if Graph send stays on.  
**RELIABILITY:** Extra hop; ManyChat outage blocks IG.  
**COST / LOCK-IN:** ManyChat subscription + Meta routing.  
**RECOMMENDATION: DEFER** until operator configures routing. Keep one sender. If Assaf needs comment/story triggers, ADOPT ManyChat as **entry only** with Graph send disabled.

### 3. New `InboundEvent` vs evolve `CanonicalEvent`

**CURRENT:** `CanonicalEvent` is the domain envelope.  
**NEW:** Parallel `InboundEvent` with correlation/raw/business fields.  
**WHY IT MAY BE BETTER:** Separates ingress from business timeline.  
**RELIABILITY:** Two envelopes invite drift.  
**RECOMMENDATION: TEST BOTH** as a design — prefer **extending** `CanonicalEvent` / webhook row over a second SoR. Ask Assaf only if we add `business_id` (multi-tenant).

### 4. Lambda Powertools idempotency vs application Protocol

**CURRENT:** SQL unique keys + `claim_webhook`.  
**NEW:** Powertools decorator + `IdempotencyStore`.  
**WHY IT MAY BE BETTER:** In-flight TTL, lost-response replay.  
**LOCK-IN:** Powertools ties domain to Lambda. Control file already forbids depending on the decorator alone.  
**RECOMMENDATION: ADOPT** the Protocol in domain; **KEEP** SQL unique keys as the first store; **DEFER** Powertools until (if) Lambda ingress exists.

### 5. AgentCore as the LangGraph runtime

**CURRENT:** FastAPI in-process graph.  
**NEW:** AgentCore versioned runtime.  
**WHY IT MAY BE BETTER:** Session isolation, versioned deploys (per AWS docs cited in the control file).  
**COST / LOCK-IN / OPS:** Higher until volume exists.  
**RECOMMENDATION: DEFER** until Phase 5 benchmark. Do not migrate because it is newer.

### 6. Split LangGraph into many subgraphs now

**CURRENT:** One sales node; owner/calendar/campaign are domain modules.  
**NEW:** Customer/owner/calendar/campaign/follow-up/approval/research subgraphs.  
**WHY IT MAY BE BETTER:** Matches Bible diagram.  
**RELIABILITY:** More checkpoint surface; little behavior change.  
**RECOMMENDATION: KEEP** until a subgraph has a distinct persistence or interrupt need (approval execute, async research worker).

### 7. Many named feature flags vs global kill switch + risk levels

**CURRENT:** `MIA_KILL_SWITCH` + non-overridable R4/R5 + empty credentials disable ports.  
**NEW:** Per-capability flags including staging/allowlist/percentage.  
**WHY IT MAY BE BETTER:** Calendar write can go live the moment OAuth is connected.  
**SECURITY:** Flags must not override R4/R5.  
**RECOMMENDATION: ADOPT** additive flags for writes and auto-reply; **KEEP** R4/R5 hard-coded.

---

# What this report does not authorize

- AWS/SQS/AgentCore implementation
- Instruction activation
- Owner task execute
- Gmail send, follow-up send, Meta/LinkedIn writes
- Provider calendar delete
- Dynamic Composio discovery
- Make/n8n on the sales path
- Rebuilding the LangGraph sales brain
