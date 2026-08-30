# Phase 1.5.2 evidence: domain and brain function-file audit

Date: 2026-08-28
Scope: the current on-disk worktree under `app/domain` and `app/brain`
Mode: read-only audit; no application code or tests changed

## Audit contract and method

The inventory was independently rebuilt with `rg -l "^\s*(async\s+)?def\s+" app/domain app/brain -g "*.py"` and sorted. It contains exactly **73** files: **8** under `app/brain`, **65** under `app/domain`, and **15,217** current lines by `Get-Content | Measure-Object -Line`. Every file below was opened in full. Direct module callers and tests were mapped with exact `app.<module>` searches, then concrete symbols were checked with repository-wide word searches.

The four required passes were performed:

1. Complete inventory: reproduced 73/73 with no overlap or omission.
2. Expert reread: checked each file against PRODUCT, ARCHITECTURE, accepted ADRs, Postgres ownership, principal/policy boundaries, idempotency, and customer/owner behavior.
3. Defect and duplication hunt: traced approval ingress, calendar write recovery, deterministic lexicons, dead public symbols, and repeated helpers into direct callers/tests.
4. Free polish: separated changes with measurable safety/correctness value from cosmetic line-count reductions.

Disposition meanings are file-level: `SIMPLIFY` means a bounded behavior-preserving cleanup or a narrowly scoped correctness hardening is justified; it does not authorize implementation. No whole file qualifies for `MERGE` or `REMOVE`.

## Exact inventory and per-file disposition

| # | File | Disposition | Concrete symbols, caller and test evidence | Behavior risk and expected benefit |
|---:|---|---|---|---|
| 1 | `app/brain/context.py` | KEEP | `assemble_owner_context`, `assemble_visitor_context`, `render_context_block`; called by owner/client graph and capability paths; covered by `test_brain_end_to_end.py`, `test_visitor_knowledge.py`, `test_vnext_owner_retrieval.py`. | Owner-memory isolation and one bounded retrieval context are safety contracts. Retain the explicit visitor knowledge-only path. |
| 2 | `app/brain/embeddings.py` | KEEP | `EmbeddingPort`, `OpenAIEmbeddingPort`, `build_embedding_port`; used by context, extraction, ingest, and capability registries; exercised by brain and visitor-knowledge tests. | Explicit provider selection prevents cross-model vector corruption; disabled fallback preserves keyword retrieval. |
| 3 | `app/brain/extraction.py` | KEEP | `extract_candidates`, `reconcile_candidate`, `consolidate`; called by `owner_brain.learn_from_exchange`; covered by `test_brain_memory.py`. | Owner-only durable-memory writes, supersession, and fail-closed parsing are deliberate. |
| 4 | `app/brain/knowledge.py` | KEEP | `build_chunks`, `ingest_source`, `ingest_website`; called by `workers/ingest_knowledge.py`; covered by `test_brain_memory.py` and `test_brain_voice_knowledge.py`. | Hash-idempotent Postgres ingestion and untrusted-text treatment should remain explicit. |
| 5 | `app/brain/retrieval.py` | KEEP | `bm25_scores`, `rank_memories`, `rank_knowledge`, `fit_to_budget`; called by context/extraction/capabilities; covered by brain retrieval tests. | Python parity across SQLite/Postgres and deterministic ranking are intentional, not accidental duplication. |
| 6 | `app/brain/schemas.py` | KEEP | `MemoryCandidate`, `MemoryRecord`, `KnowledgeChunk`, `BrainContext`; shared by all brain modules and owner tools; covered across six brain suites. | Typed frozen/validated contracts protect graph serialization and storage boundaries. |
| 7 | `app/brain/store.py` | KEEP | `BrainStore.save_memory`, `replace_knowledge_chunks`, `link_entity`, `open_gap`; called by capabilities, graphs, owner API, main, and ingest worker; broad brain tests. | This is the Postgres-backed brain repository. Splitting or replacing it would add ownership ambiguity with no demonstrated gain. |
| 8 | `app/brain/vectors.py` | SIMPLIFY | `rank_by_similarity`, `encode_vector`, `decode_vector` are live via brain modules and `test_brain_memory.py`; repository-wide exact search finds `cosine_similarity` only at its definition. | Remove only dead `cosine_similarity`; retain portable TEXT encoding and exact dot-product search. Benefit: one false public surface removed without changing retrieval. |
| 9 | `app/domain/ai_runs.py` | KEEP | `sales_model_label`, sanitizers, `persist_ai_run`; called by inbound/API/integration paths and store; covered by `test_ai_runs.py` and Sheets tests. | Metadata-only audit trail and canned-vs-model truth must remain stable. |
| 10 | `app/domain/approvals.py` | SIMPLIFY | `resource_hash_matches`, `_validate_pending_row`, `_validate_website_pending_row`, `apply_owner_approval_decision`; 15 production importers and approval/website tests. `apply_website_edit_approval_policy` truncates serialized JSON at 255 characters after hashing the untruncated before/after values. | Preserve approval binding but reject or safely bound oversized website proposals instead of storing invalid JSON. Also expose one reusable pending-row validator for Gmail/callback paths. Benefit: no permanently unapprovable queued row and less policy drift. |
| 11 | `app/domain/attribution.py` | KEEP | `sanitize_url_value`, `sanitize_attribution`, `sanitize_instagram_attribution`; used by website/inbound/events/Sheets; covered by attribution and website tests. | PII/URL allowlists are deliberate boundary code. Keep module-local explicit sanitizers. |
| 12 | `app/domain/behavior.py` | KEEP | `sanitize_client_behavior`, `behavior_provider_event_id`; called by website API, store, and event builder; indirectly covered by website/events suites. | The small overlap with attribution sanitization is policy-specific; a generic sanitizer would blur accepted fields. |
| 13 | `app/domain/briefs.py` | KEEP | `apply_meeting_brief_policy`, `persist_booked_meeting_brief`, `format_owner_meeting_brief`; used by API, booking/change flows, tools; covered by briefs and adversarial-research tests. | Redacted storage/event split and one-shot research cache protect customer data and Postgres ownership. |
| 14 | `app/domain/calendar_booking.py` | KEEP | `attempt_meeting_booking`, `_persist_booked_from_provider`, `resolve_meeting_reply`; API callers; nine calendar/idempotency/value test modules. | Lookup-before-create and verify-after-create are accepted write guarantees; size follows explicit recovery states, not accidental complexity. |
| 15 | `app/domain/commitments.py` | KEEP | `parse_due_at`, `scan_due_owner_tasks`, `plan_owner_commitment`; worker/store/owner API callers; commitment, due-scan, and owner-task tests. | Deterministic date/condition parsing keeps scheduled owner tasks non-autonomous. |
| 16 | `app/domain/company.py` | KEEP | `sanitize_company_domain`, `extract_explicit_company_domain`; used by extract/brief/store; covered by `test_company_meeting_research.py`. | Explicit domain only is ADR-010 safety policy; do not broaden inference. |
| 17 | `app/domain/content_ideas.py` | KEEP | `compute_content_idea_snapshot`, `apply_owner_content_ideas`; owner API/tool/capability callers; `test_content_ideas.py`. | Ranked stored insights remain read/persist-only and bounded. |
| 18 | `app/domain/content_insights.py` | KEEP | `ContentInsight`, `is_allowlisted_media_id`, `apply_content_insight_policy`; store/Instagram/Sheets callers; Instagram insight/freshness tests. | Media-id allowlisting and Postgres persistence are explicit security boundaries. |
| 19 | `app/domain/conversation_kill.py` | KEEP | `opt_out_status_outcome`, `apply_conversation_kill_policy`; website/inbound/capability callers; kill/takeover/freshness tests. | Opt-out and kill-state persistence must stay a separate deterministic shield. |
| 20 | `app/domain/conversation_scope.py` | KEEP | `whatsapp_sales_allowed`, `prepare_whatsapp_inbound`, `apply_owner_scope_mark`; WhatsApp/inbound/owner/store callers; communication and handoff tests. | This owns one-sender/business-scope behavior; merging with takeover would conflate distinct states. |
| 21 | `app/domain/deals.py` | KEEP | `confidence_from_attribution`, `apply_deal_policy`; API/store/Sheets/lead-review callers; deals/debrief/attribution tests. | Monotonic stage persistence and sanitized confidence are small and coherent. |
| 22 | `app/domain/debriefs.py` | KEEP | `parse_debrief_outcome`, `apply_owner_meeting_debrief`, `ack_for_debrief_result`; owner API/capability/store callers; `test_debriefs.py`. | Deterministic allowlisted outcome/next-step persistence prevents model-selected writes. |
| 23 | `app/domain/emotion.py` | KEEP | `detect_emotional_cues`, `infer_emotional_cues`; orchestrator/sales-reply callers; `test_emotion.py`. | ADR-040 explicitly accepts this deterministic bilingual lexicon for phrasing only. Do not replace it with a model or merge it into sales extraction. |
| 24 | `app/domain/engine_health.py` | KEEP | `compute_engine_health`, `format_engine_health`; daily brief/snapshot callers; indirectly exercised by owner brief/snapshot tests. | The all-time limitation is documented and truthful; exact day scope requires a future additive DB migration, not cleanup sleight of hand. |
| 25 | `app/domain/events.py` | KEEP | `CanonicalEvent`, typed `build_*_event` functions, `persist_tool_outcome`; 28 production importers and broad event/idempotency suites. | Explicit per-event payload allowlists are safer than a generic builder. Preserve canonical-event ownership and redaction. |
| 26 | `app/domain/extract.py` | KEEP | `is_substantive_answer`, `extract_sales_signals`; graph/eval/emotion/task-class callers; seven sales/objection suites. | The large bilingual token tables are deterministic sales policy, not dead duplication. |
| 27 | `app/domain/feedback.py` | KEEP | `classify_correction_scope`, `persist_owner_correction`; owner API/capability callers; `test_feedback.py`. | Persist-only correction logging intentionally does not activate prompts. |
| 28 | `app/domain/followup_voice.py` | KEEP | `compose_follow_up_draft`; called by follow-up scan and eval harness; follow-up/humanity/due-scan tests. | A six-line customer-copy boundary is explicit and testable; merging it for line count provides no functional benefit. |
| 29 | `app/domain/followups.py` | KEEP | `evaluate_follow_up_send`, `scan_due_follow_ups`, `apply_follow_up_policy`; worker/API/store/capability callers; fourteen follow-up, takeover, value, and due-scan suites. | Frequency caps, opt-out/takeover checks, and Postgres task state are critical deterministic shields. |
| 30 | `app/domain/funnel.py` | KEEP | `compute_website_funnel`, `format_website_funnel`; daily brief and operator snapshot callers; indirect owner brief/snapshot coverage. | Mixed-window limitations are explicitly disclosed; do not pretend sampled engagement is day-scoped. |
| 31 | `app/domain/gmail_drafts.py` | SIMPLIFY | `apply_owner_gmail_draft`, `apply_gmail_send_decision`, `execute_approved_gmail_send`; owner API and classifier callers; `test_owner_gmail_console.py` covers draft creation/flag-off but no expiry/hash rejection. | Reuse the approval module's expiry, payload-hash, action, risk, resource-type, and resource-id validation before deciding/sending. Benefit: expired or corrupted pending rows cannot authorize a commercial email send. |
| 32 | `app/domain/gmail_query.py` | KEEP | `normalize_gmail_query` and its Hebrew-clitic/relative-time helpers; owner tool registry caller; `test_gmail_query.py`. | ADR-032 deliberately puts deterministic query normalization here rather than a second model hop. |
| 33 | `app/domain/gmail_summaries.py` | KEEP | `build_gmail_summary_snapshot`, `apply_owner_gmail_summary`; capability/API/tool/classifier callers; `test_gmail_summaries.py`. | Thread/lead resolution and read-only formatting are coherent and bounded. |
| 34 | `app/domain/handoff.py` | KEEP | `generate_handoff_token`, `hash_handoff_token`, `click_to_chat_url`; API/store/scope callers; handoff/website tests. | Token hashing and click-to-chat normalization preserve the current human-inbox contract. |
| 35 | `app/domain/hot_handoff.py` | KEEP | `notify_owners`, `apply_hot_handoff`, `format_hot_leads_ack`; client graph/inbound/owner/tool callers; hot-handoff/finalization tests. | Multi-owner best-effort fan-out and per-recipient isolation are accepted behavior. |
| 36 | `app/domain/humanity.py` | KEEP | `lint_customer_reply`, `customer_reply_or_canned`; follow-up/sales-reply/eval callers; humanity/follow-up tests. | Customer one-question, typography, and canned-fallback rules are deterministic output safety. |
| 37 | `app/domain/idempotency.py` | KEEP | `sanitize_operation_result`, `IdempotencyStore`; store/capability callers; idempotency suites. | This is the shared typed protocol and redacted result sanitizer; keep it small and stable. |
| 38 | `app/domain/identity.py` | KEEP | `IdentityIndex`, `persist_verified_identity_link`; inbound/store/capability callers; domain/handoff tests. | Verified identity links must remain Postgres-owned and deterministic. |
| 39 | `app/domain/kpis.py` | KEEP | `week_bounds_utc_iso`, `compute_weekly_kpi`; store/Sheets/daily/weekly callers; KPI and brief tests. | Local-calendar KPI windows are shared domain truth. |
| 40 | `app/domain/language.py` | KEEP | `language_of`, `reply_language`; orchestrator caller; indirectly covered by bilingual sales/website tests. | Cheap script counting is intentional deterministic routing and avoids an LLM call. |
| 41 | `app/domain/lead_label.py` | SIMPLIFY | `sanitize_label`, `derive_headline`, `lead_display`, `derive_display_name` are live in graph/services/owner views and `test_lead_label.py`; exact search finds `short_lead_id` only at its definition. | Remove only dead `short_lead_id`; full IDs are an explicit owner-console contract. Benefit: removes a misleading helper that contradicts the documented full-id behavior. |
| 42 | `app/domain/lead_reviews.py` | KEEP | `build_lead_review_snapshot`, `apply_owner_lead_review`, `format_lead_matches`; owner API/tools/store/capability callers; lead-review/find/calendar tests. | Grounded Postgres snapshot and no-guess name search are accepted owner behavior. |
| 43 | `app/domain/learning.py` | SIMPLIFY | `InstructionKind`, `classify_instruction_kind`, `propose_owner_instruction` are live via owner API/feedback/tasks and learning tests; exact search finds `ProposedInstruction` only at its declaration. | Remove dead `ProposedInstruction` and now-unused Pydantic imports. Preserve propose-only persistence. |
| 44 | `app/domain/meeting_availability.py` | KEEP | `slot_is_bookable`, `carve_policy_slots`; booking/calendar/integration callers; five calendar-policy suites. | Sun-Thu, local hours, 24-hour notice, and exact 30-minute slots are accepted deterministic policy. |
| 45 | `app/domain/meeting_changes.py` | SIMPLIFY | `_request_cancellation`, claim/complete helpers, `_attempt_reschedule`, `resolve_booked_meeting_change`; booking caller and six calendar/idempotency suites. `_request_cancellation` completes the inbound claim in `finally`, including the branch that returns `RETRY` because `mark_meeting_cancellation_requested` failed. | Complete only after successful durable cancellation-request persistence; mark failed/reclaimable otherwise. Benefit: a retryable local failure cannot be recorded as completed and permanently suppress the same inbound retry. Preserve no-provider-delete behavior. |
| 46 | `app/domain/meeting_slots.py` | KEEP | `OfferedSlot`, `parse_slot_selection`, `compute_booking_key`, `sanitize_meet_link`; booking/change/store/integration callers; calendar gate/idempotency tests. | Exact selection grammar, UTC normalization, Meet-link allowlist, and booking key are security/recovery contracts. |
| 47 | `app/domain/meetings.py` | KEEP | `apply_meeting_policy`; used by API/store/booking/change/Sheets/capabilities; eight meeting suites. | Monotonic offered/booked/cancellation state is concise and correctly separate from provider writes. |
| 48 | `app/domain/memory.py` | SIMPLIFY | `ConversationTurn`, `counterpart_turns`, `render_transcript`, `repeats_previous_mia_turn` have 14 production importers and six tests; exact search finds `human_turn_count` only at its definition. | Remove only dead `human_turn_count`; retain canonical-event-derived conversation memory. |
| 49 | `app/domain/owner_brain.py` | KEEP | `answer_owner`, `retrieve_owner_context`, `run_owner_turn`, `learn_from_exchange`; owner API caller; nine owner graph/agent tests. | The apparent context assembly overlap is deliberate: graph retrieval goes through capability policy, while direct callers retain one fallback retrieval. Do not collapse it without proving policy/audit parity. |
| 50 | `app/domain/owner_briefs.py` | KEEP | `compute_daily_brief`, `apply_owner_brief`; API/capability/tools/snapshot callers; `test_owner_briefs.py`. | Postgres counts, funnel truth, and engine truth are explicit owner read models. |
| 51 | `app/domain/owner_calendar.py` | KEEP | `apply_owner_calendar`, `resolve_agenda_window`, `format_calendar_agenda`; owner API/tools/capability callers; owner-calendar and freshness suites. | Availability and agenda are distinct reads; untrusted invite text remains labeled data. |
| 52 | `app/domain/owner_callbacks.py` | SIMPLIFY | `approval_token`, `resolve_owner_callback`; Telegram API/inbound-common callers; `test_telegram_format.py` and `test_telegram_owner_outbound.py`. The live callback test seeds `payload_hash="c" * 64` and still expects approval, proving callbacks bypass hash validation. | Route callback decisions through the same expiry/hash/action/risk/resource validator as text decisions. Benefit: expired, corrupted, or misbound R3 approvals fail closed instead of being approved by button. |
| 53 | `app/domain/owner_followups.py` | KEEP | `needs_data_anchor`, `resolve_owner_reference`, `routed_owner_text`; owner API caller; `test_owner_distinct_replies.py`. | Deterministic pronoun resolution is constrained to Mia's prior lead IDs and explicitly excludes approvals/personal-scope changes. |
| 54 | `app/domain/owner_notify.py` | KEEP | `persist_owner_notify`, `persist_meeting_booked_owner_notify`, `apply_owner_notify`; booking/change/owner/tool callers; `test_owner_notify.py`. | Postgres inbox persistence and redacted owner notifications are cohesive. |
| 55 | `app/domain/owner_reads.py` | KEEP | `discovery_depth`, `top_website_lead_id`, `format_website_conversations_ack`; owner API/tools/funnel/snapshot callers; owner distinct-reply tests. | Shared discovery depth prevents funnel/read drift; full lead IDs remain required. |
| 56 | `app/domain/owner_snapshot.py` | KEEP | `format_operator_snapshot_ack`; owner API/tool callers; `test_owner_snapshot.py`. | This is the explicit multi-read aggregation surface, distinct from greeting status. |
| 57 | `app/domain/owner_status.py` | KEEP | `format_owner_status_ack`; owner tool registry caller; owner snapshot tests. | Greeting/status copy intentionally includes menu and differs from grounded snapshot output. |
| 58 | `app/domain/owner_tasks.py` | SIMPLIFY | `classify_owner_task`, `_dedicated_matches`, `promote_unclassified_text_to_status`, `ack_for_owner_task`; five production callers and 32 test modules. Eight private helpers (`_review_phrase_in_text` through `_human_takeover_phrase_in_text`) have identical ASCII/Hebrew substring bodies. | Collapse only the identical phrase helpers into one private function; keep lexicons, precedence, read-combine rules, and deterministic write routing unchanged. Benefit: one matching primitive instead of eight drift points. |
| 59 | `app/domain/owner_weeklies.py` | KEEP | `compute_weekly_brief`, `apply_owner_weekly`; API/capability/snapshot/tool callers; `test_owner_weeklies.py`. | Weekly window/persistence is separate domain output, not needless duplication of daily counts. |
| 60 | `app/domain/ownership_freshness.py` | KEEP | `conversation_ownership_outcome`, `owner_permissions_outcome`; inbound/owner/Instagram callers; ownership freshness tests. | Two explicit config-backed facts map to distinct allowlisted tools/pins; generic abstraction would save little. |
| 61 | `app/domain/policies/decision.py` | KEEP | `AgentDecision`, `route_decision`, `decision_from_sales`; execution-policy/ai-run callers; `test_decision_policy.py`. | Deterministic route from typed decision to policy is a core safety boundary. |
| 62 | `app/domain/policies/execution_policy.py` | KEEP | `ActionPolicy`, registry pins, `policy_for`; capability/core/decision/shadow callers; seven policy/risk suites. | Fail-closed unknown capability and explicit risk pins must remain code-owned. |
| 63 | `app/domain/policies/failure_policy.py` | KEEP | `NodeFailurePolicy`, `failure_policy_for`; core and policy exports; `test_failure_policy.py`. | Per-tool fail-closed fallback registry is deliberate deterministic policy. |
| 64 | `app/domain/policies/freshness.py` | KEEP | `FreshnessPin`, `freshness_pin`, `stamp_freshness`, `overlay_stale`; 12 integration/domain callers; freshness suites. | Explicit source/TTL/version pins prevent invented freshness; registry repetition is declarative policy. |
| 65 | `app/domain/policies/task_classes.py` | KEEP | `TaskClassPin`, `task_class_pin`; core capability caller; owner-reply/task-class tests. | Model-brand-free owner registry is a documented lookup contract, not runtime routing. |
| 66 | `app/domain/reconciliation.py` | KEEP | `evaluate_reconciliation`, `inspect_open_findings`, `run_reconciliation`; worker/store/capability callers; reconciliation suites. | Flag-only reconciliation deliberately does not repair or send; keep current ownership. |
| 67 | `app/domain/sales.py` | KEEP | `SalesState`, `website_whatsapp_continuation_ready`, `select_next_action`, `mark_action_delivered`; 25 production importers and 43 test modules. | Deterministic NBA and accepted meeting-first/customer behavior are core product policy; no refactor justified by size. |
| 68 | `app/domain/seo.py` | KEEP | `apply_seo_recommendation_policy`, `enrich_seo_ack`; owner API/tool callers; SEO/adversarial tests. | Read-only recommendation enrichment stays behind typed adapters and redaction. |
| 69 | `app/domain/shadow.py` | KEEP | `should_skip_prospect_send`, `persist_shadow_decision`; outbound/inbound/capability callers; shadow/handoff tests. | Shadow/no-send and one-sender WhatsApp rules are production safety controls. |
| 70 | `app/domain/takeover.py` | KEEP | `apply_owner_human_takeover`, `apply_owner_human_resume`; owner API/capability callers; communication/takeover suites. | Human takeover is distinct from opt-out and conversation scope. The public inbound path enforces kill switch before this formatter/persister runs. |
| 71 | `app/domain/tools.py` | KEEP | `AdapterHttpError` hierarchy, `tool_status_from_http`, `ToolOutcome`; 32 production importers and 28 test modules. | Central allowlists and sanitized tool outcomes are shared audit vocabulary. |
| 72 | `app/domain/value.py` | KEEP | `persist_business_value`, `count_business_value`; graph/booking/follow-up/capability callers; `test_value.py`. | Canonical-event-backed value counts avoid a second source of truth. |
| 73 | `app/domain/website_handoff_brief.py` | KEEP | `format_website_whatsapp_brief`, `format_website_human_handoff_brief`, `apply_website_whatsapp_handoff_brief`; website/client graph/finalization callers; `test_website_handoff_brief.py`. | Allowlisted topic copy, escaped transcript, one-per-lead notification, and no WhatsApp-send claim are accepted behavior. |

Disposition totals: **64 KEEP, 9 SIMPLIFY, 0 MERGE, 0 REMOVE = 73**.

## Ranked actionable findings

### 1. High: all approval ingress paths do not enforce the same binding contract

- `app/domain/approvals.py::apply_owner_approval_decision` validates expiry and payload/resource binding for proposal and website text decisions.
- `app/domain/gmail_drafts.py::apply_gmail_send_decision` checks decision state and action, but not expiry, payload hash, risk, resource type, or resource-id binding before the caller may invoke `execute_approved_gmail_send` when the write flag is enabled.
- `app/domain/owner_callbacks.py::resolve_owner_callback` performs none of those validation checks. `tests/unit/test_telegram_owner_outbound.py::test_approval_keyboard_callback_applies_the_decision` deliberately seeds an invalid `payload_hash="c" * 64` and expects the row to become approved.

Action: introduce one reusable pending-approval validator in `approvals.py` and require text, Telegram callback, and Gmail decision paths to use it. Add expiry, tampered-hash, wrong-action, wrong-risk, wrong-resource-type, and wrong-resource-id tests for each ingress. Preserve numeric Telegram owner authorization and Gmail's separate write flag.

Expected benefit: one fail-closed approval invariant; stale or corrupted rows cannot authorize an R3 decision or email send.

### 2. High: oversized website edit proposals can be queued but never approved

`apply_website_edit_approval_policy` hashes before/after values capped independently at 255 characters, serializes both, then truncates the serialized JSON to 255 characters. When the combined JSON exceeds 255 characters, truncation can make it invalid. `_website_proposed_parts` then returns empty values and `website_resource_hash_matches` fails forever. Existing website approval tests use only very short values.

Action: either reject an oversized proposal before persisting or bound the two fields so the complete canonical JSON fits the storage column. Never slice serialized JSON. Add a boundary test at and above the storage limit.

Expected benefit: every reported `queued` approval is structurally decidable; no misleading owner acknowledgment.

### 3. Medium: cancellation retry can be permanently consumed as completed

`meeting_changes._request_cancellation` claims the inbound id, but `complete_cancellation_persist` runs in `finally`. The branch where `mark_meeting_cancellation_requested` returns false reports `MeetingChangeKind.RETRY` and still marks the idempotency row completed. The store already supports `fail_operation`, but this path does not use it.

Action: complete only after the durable status/event write succeeds; on retryable failure, fail the claim (or leave it reclaimable under the established TTL contract). Add a test that injects a false/failed mark, asserts non-completed claim state, then proves a retry succeeds exactly once.

Expected benefit: idempotency still prevents duplicates without converting transient persistence failure into permanent suppression.

### 4. Low: four provably unreferenced public symbols remain

Repository-wide exact searches across `app`, `tests`, and `scripts` find only their own definitions:

- `app/brain/vectors.py::cosine_similarity`
- `app/domain/lead_label.py::short_lead_id`
- `app/domain/learning.py::ProposedInstruction`
- `app/domain/memory.py::human_turn_count`

Action: remove these symbols and imports made unused by their removal; run their owning focused suites plus Ruff.

Expected benefit: smaller truthful public surface, especially removal of `short_lead_id`, which conflicts with the documented full-id owner-console contract.

### 5. Low: owner task phrase matching has eight identical helper implementations

`_review_phrase_in_text`, `_content_idea_phrase_in_text`, `_gmail_summary_phrase_in_text`, `_seo_phrase_in_text`, `_calendar_phrase_in_text`, `_owner_notify_phrase_in_text`, `_meeting_brief_phrase_in_text`, and `_human_takeover_phrase_in_text` all implement the same ASCII-casefold/Hebrew-substring rule. Their lexicons and routing order are deliberate; the duplicated primitive is not.

Action: replace those eight helper bodies with one private `_phrase_in_text`, leaving every lexicon, match order, clarification decision, and read-combine rule unchanged. Run `test_owner_tasks.py` plus the 32 importing suites selected by the driver.

Expected benefit: fewer matching drift points with no product behavior change.

## Explicit no-change conclusions

- **No whole file should be removed or merged.** Every one of the 73 modules has at least one live caller; the only dead items found are four individual symbols.
- **Keep Postgres ownership.** `BrainStore`, canonical events, approval rows, meetings, follow-ups, notifications, and value counts remain Postgres-backed. No Sheets or in-memory replacement is justified.
- **Keep deterministic policy and lexicons.** `extract.py`, `emotion.py`, `gmail_query.py`, `owner_tasks.py`, availability/slot rules, sales NBA, freshness, failure, execution, and task-class registries are deliberate code-owned behavior. Their size alone is not a cleanup reason.
- **Keep safety-explicit repetition where schemas differ.** The event builders and booking/reschedule states repeat construction deliberately so each payload, audit tool, and recovery outcome is independently allowlisted and testable.
- **Keep brain provider and retrieval boundaries.** No pgvector migration, second database, extra runtime agent, provider failover between embedding families, or visitor access to owner memory.
- **Keep accepted customer and owner behavior.** No TTS, no auto-send/publish, no provider calendar cancellation, no inference of company/name/authority, no shortened owner lead IDs, no owner-tool access from the client graph, and no autonomous WhatsApp reply before official inbound.
- **Do not weaken tests to enable cleanup.** In particular, replace the callback test's invalid-hash success fixture with a valid binding and add a separate fail-closed tamper test.

## Evidence boundary

This leaf is an audit, not implementation authorization. It opened the current dirty worktree and wrote only this evidence file plus its own leaf gate. It did not run live APIs, inspect `.env`, change code/tests/product/deployment files, or claim that any finding is fixed. Existing test names show path coverage; no new full-suite pass is claimed by this audit.
