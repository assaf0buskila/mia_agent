# DECISIONS

Architectural Decision Records for Mia. One decision per record. Nygard fields: context, decision, consequences.

Bible layout shows both `docs/ADR/` and `docs/DECISIONS.md`. This file is the log for Phase 0. If the log grows past about ten accepted records, split into `docs/adr/NNNN-title.md` and keep this file as the index. Do not create that folder until then.

## Status words

| Status | Meaning |
| --- | --- |
| proposed | Written; Assaf has not chosen yet |
| accepted | Assaf chose KEEP or ADOPT |
| superseded | Replaced by a later ADR |
| rejected | Assaf chose not to take this path |

Proposed is not accepted. Build may follow a proposed default only when `AGENTS.md` / `BUILD_STATUS.md` say to.

## Index

| ID | Title | Status |
| --- | --- | --- |
| ADR-000 | Bible v1.1 is the product baseline | accepted |
| ADR-001 | Repo root is this workspace | proposed |
| ADR-002 | Phase 0 uses `AGENTS.md` only, no `.mdc` rules | proposed |
| ADR-003 | Finish Phase 0 control docs before `pyproject.toml` | proposed |
| ADR-004 | Keep PRD living; sync provider facts while building | accepted |
| ADR-005 | uv + pinned FastAPI/LangGraph for the toolchain | accepted |
| ADR-006 | WhatsApp ingress stays Meta webhook; Composio is not the WhatsApp brain | accepted |
| ADR-007 | Pick the best adapter per job; do not default to Composio | accepted |
| ADR-008 | Today-vs-baseline = previous 7 completed local days' daily average | accepted |
| ADR-009 | Composio LinkedIn profile + direct member post analytics | accepted |
| ADR-010 | Explicit company domain for meeting research | accepted |
| ADR-011 | Calendar create after explicit slot confirmation | accepted |
| ADR-012 | Meeting availability policy (Sun–Thu IL business hours) | accepted |
| ADR-013 | Automatic confirmed reschedule; cancellation request for Assaf | accepted |
| ADR-014 | First AWS production: Fargate + RDS + Secrets Manager box | accepted |
| ADR-015 | Production adapter map (Composio vs Meta vs Firecrawl) | accepted |
| ADR-016 | WhatsApp inbound stays Meta; Composio may own send | accepted |
| ADR-017 | v1 communication operating model | accepted |
| ADR-018 | Website offers WhatsApp after first real friction | accepted |
| ADR-019 | Selected Region is eu-north-1 | accepted |
| ADR-021 | Documentation core set; ManyChat not a v1 runtime channel | superseded (docs set → ADR-031; ManyChat still unmounted) |
| ADR-022 | Production live sales test: leave shadow, keep gated writes off | accepted |
| ADR-023 | Model routing: deterministic decisions, model paraphrases | proposed |
| ADR-024 | WhatsApp stays human until official Cloud API inbound | accepted |
| ADR-026 | Mia's brain: long-term memory, knowledge, and an owner tool loop | accepted |
| ADR-027 | Opt-in Composio discovery for GSC/GA4/Meta; Sheets and LinkedIn stay explicit | accepted |
| ADR-028 | Visitor knowledge, answer-then-ask, meeting as default website exit | accepted |
| ADR-029 | Website conversion funnel, engine truth line, multi-owner notification | accepted |
| ADR-030 | Owner Telegram: free conversation, typed Gmail reads, lead by name | accepted |
| ADR-031 | Owner intent: same agent, no sub-agents | accepted |
| ADR-032 | Owner reads: wider tool loop, live dates, agenda, deterministic query normalization | accepted |
| ADR-033 | reserved — Gmail send after Approve (in flight on `claude/mia-adr033-wip`, not yet merged) | proposed |
| ADR-034 | LinkedIn v1 is Composio profile; member-analytics token is optional | accepted |
| ADR-035 | Apify google-search-scraper behind ResearchPort | accepted |
| ADR-036 | VNext two graphs + canonical docs | accepted |
| ADR-037 | Delete ManyChat from the product | accepted |
| ADR-038 | Graphs own retrieve and conversation complete | accepted |
| ADR-039 | Drop Meta ads, LinkedIn post analytics, campaigns, pacing and prelaunch | accepted |
| ADR-040 | Prospect tone awareness in the website sales prompt | accepted |
| ADR-041 | The permission principal is derived from the request | accepted |
| ADR-042 | Authorized Sheets updates and normalized AssafWeb KPI reads | accepted |
| ADR-043 | Owner-only on-demand Composio tool breadth | accepted |
| ADR-044 | Repair strict provider contracts and remove lazy-user handoff friction | accepted |
| ADR-045 | Complete-work owner actions and Mia-managed CRM workspace | accepted |
| ADR-046 | Official Composio destructive slugs stay denied; WhatsApp-move ping is a summary | accepted |
| ADR-047 | Owner-requested Gmail send stays; unsolicited send and delete-forever stay denied | accepted |

## Template

Copy for a new ADR. Do not skip fields.

```markdown
### ADR-NNN Title

- **Status:** proposed | accepted | superseded | rejected
- **Date:** YYYY-MM-DD
- **Assaf:** KEEP / ADOPT / TEST BOTH / DEFER / unset

**Context**
What forces the choice.

**Decision**
What we will do.

**Consequences**
What becomes easier, harder, or off-limits.

**Alternatives considered**
What we did not pick, and why.
```

## Records

### ADR-000 Bible v1.1 is the product baseline

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** KEEP (source file provided)

**Context**
Mia must be built from an approved product and architecture baseline, not from improvised agent preference.

**Decision**
`Mia_AI_Growth_Sales_Operator_PRD_Build_Bible_v1.1.docx` is the approved baseline. Material change uses the Better-Way protocol, then an ADR, then a Bible/PRD update if the contract changed.

**Consequences**
Agents follow the Bible until Assaf adopts an alternative. `_mia_prd_extract.txt` is a scratch extract, not the canonical spec. Canonical markdown form will be `docs/PRD.md` when that file is written.

**Alternatives considered**
Ignore the Bible and design from scratch — rejected. Treat the scratch extract as source of truth — rejected.

### ADR-001 Repo root is this workspace

- **Status:** proposed
- **Date:** 2026-08-21
- **Assaf:** unset

**Context**
The Bible shows a `mia/` directory as the repo tree. The Cursor workspace is already `assaf_agent`.

**Decision**
Use this workspace as the project root. Do not create a nested `mia/` folder.

**Consequences**
`AGENTS.md`, `app/`, `docs/`, and `tests/` live at the workspace root. Paths in the Bible that start with `mia/` map to this root. Import and deploy paths stay one level shallower.

**Alternatives considered**
Create `mia/` inside the workspace — matches the Bible diagram, adds a useless extra directory for every path and tool.

### ADR-002 Phase 0 uses AGENTS.md only

- **Status:** proposed
- **Date:** 2026-08-21
- **Assaf:** unset

**Context**
Cursor supports `AGENTS.md` and glob-scoped `.cursor/rules/*.mdc`. Official Cursor docs: `AGENTS.md` is the simple root instruction file; `.mdc` is for scoped rules.

**Decision**
Phase 0 ships one root `AGENTS.md`. Do not add `.cursor/rules/*.mdc` until a repeated, file-pattern-specific failure justifies a scoped rule.

**Consequences**
One instruction surface. Less drift. Later Python/test/graph rules can be added as `.mdc` without rewriting this ADR; that would be a new ADR.

**Alternatives considered**
Create `.mdc` rules on day one — more Cursor control, splits instructions before the repo has files to scope.

### ADR-003 Finish Phase 0 control docs before pyproject.toml

- **Status:** proposed
- **Date:** 2026-08-21
- **Assaf:** unset

**Context**
Bible §45.2 lists `pyproject.toml` immediately after `AGENTS.md` and `BUILD_STATUS.md`. Bible §45.0 requires `DECISIONS.md`, `docs/PRD.md`, and a provider-capability matrix before feature coding.

**Decision**
Remaining Phase 0 order: this file, then `docs/PRD.md`, then the provider-capability matrix, then `pyproject.toml`.

**Consequences**
Toolchain comes after the spec is in-repo and decisions have a home. Slightly slower to first `pip install`. Less chance of coding against an unreadable `.docx` and unrecorded defaults.

**Alternatives considered**
Follow §45.2 literally and write `pyproject.toml` next — faster scaffolding, control docs still missing while the toolchain appears.

### ADR-004 Keep PRD living while building

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** KEEP (chat: always update PRD while working; wire features so they are alive)

**Context**
Provider facts drift (Composio catalog size, changelog URL). A frozen markdown copy of the Bible goes stale in hours. Assaf also asked that features be wired and alive, not documented then left disconnected.

**Decision**
`docs/PRD.md` is updated in the same turn as any contract, provider-fact, or capability-status change. Runtime wiring status lives in `app/core/capabilities.py` and is mirrored in the PRD table. A capability is not done until a test proves the path.

**Consequences**
PRD and code cannot diverge silently. `.docx` remains historical baseline; markdown PRD is the working spec. More frequent PRD diffs.

**Alternatives considered**
Wait for Assaf KEEP before every appendix patch — rejected by this instruction.

### ADR-005 uv with pinned FastAPI and LangGraph

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** unset (implementation default; Assaf may change)

**Context**
Official LangGraph install shows pip and uv. Official FastAPI docs require pinning the FastAPI minor version and not pinning Starlette. Local Python is 3.14.3. PyPI on 21 Aug 2026: FastAPI 0.141.1, LangGraph 1.2.11, pydantic-settings 2.15.0.

**Decision**
Use uv. `requires-python = ">=3.12"`. Pin `fastapi[standard]>=0.141.1,<0.142.0`, `langgraph>=1.2.11,<2`, `pydantic-settings>=2.15.0,<3`. Do not add Composio, AWS, or channel SDKs until that adapter is built.

**Consequences**
Reproducible installs via `uv.lock`. Channel packages land with their adapter, not on day one.

**Alternatives considered**
Poetry / pip-tools — extra tool. Unpinned FastAPI — official docs warn against it. Dumping every integration into pyproject now — dead weight.

### ADR-006 WhatsApp ingress stays Meta webhook; Composio is not the WhatsApp brain

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** KEEP (chat: always pick the best API/tool/workflow)

**Context**
Bible §17.1 and §23 allow Cloud API *or* Composio behind typed adapters. Mia already has a live WhatsApp path: Meta HMAC webhook, idempotent claim, sales graph, R2-gated send, in-memory media download, GPT Transcribe. Assaf asked to use Composio for WhatsApp Business. Official Composio toolkit `WHATSAPP` (version `20260815_00`) has 57 tools, managed app **Yes**, and **one trigger**. That trigger is `WHATSAPP_MESSAGE_STATUS_UPDATED_TRIGGER` (poll). Composio documents that WhatsApp has no native status-poll API, so the trigger returns empty; inbound messages are **not** a Composio trigger. Status/inbound still require a Meta webhook. `WHATSAPP_CONFIGURE_CONVERSATIONAL_AUTOMATION` would install Meta away/welcome bots and dual-send against Mia.

**Decision**
KEEP the live Meta webhook + direct Graph `MessagePort` / media port as WhatsApp ingress and session replies. Do not give the model the 57-tool catalog. Composio WhatsApp, if used later, is a second implementation of the **same** typed ports, pinned to a short allowlist (`WHATSAPP_SEND_MESSAGE`, `WHATSAPP_GET_MEDIA_INFO`, `WHATSAPP_SEND_TEMPLATE_MESSAGE` under approval). First Composio adapters should be Gmail / Calendar / Sheets, where OAuth is the actual pain. Production WhatsApp credentials stay Assaf-owned Meta tokens, not Composio’s managed app.

**Consequences**
WhatsApp latency and HMAC stay in our process. Template/admin tools can be added without rewriting sales. Extra hop and schema-pin work if we later wrap send in Composio. Cannot use Composio as the inbound event bus for WhatsApp.

**Alternatives considered**
Replace webhook with Composio triggers — rejected; the only WhatsApp trigger is a documented no-op poll for status. Point LangGraph at all 57 tools — rejected; violates pin-schema, risk policy, and dual-send rules. Wati/Spoki/1msg Composio toolkits — rejected extra vendors.

### ADR-007 Pick the best adapter per job; do not default to Composio

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT (chat: always pick the best API/tool/workflow)

**Context**
Bible §23 says Composio-first behind typed ports. That is a supplier preference, not a quality function. Asking KEEP/ADOPT on every toolkit slows the build and still needs a rubric.

**Decision**
For each capability, choose the adapter that wins on: (1) safety and one-sender, (2) official completeness for that job, (3) latency and failure mode we control, (4) OAuth/token-refresh burden, (5) do not rip out an alive path for fashion, (6) pin schemas — never dump a catalog into the model, (7) cost and lock-in. Composio wins when it removes OAuth/token pain (Gmail, Calendar, Sheets, LinkedIn read). Direct official APIs win for Meta webhooks we already own (WhatsApp, Instagram DMs) and for STT (`gpt-transcribe`). LangGraph stays the orchestrator; Composio Tool Router does not. Models stay eval-driven: Luna for sales, Grok 4.6 for deep research, gpt-transcribe for voice input. No TTS.

**Consequences**
Faster adapter choices. Assaf is asked only when the pick changes safety, permissions, or dual-send. A “best” pick can still be reversed with a Better-Way + ADR if evidence changes. Production supplier map is **ADR-015**.

**Alternatives considered**
Composio for every channel — rejected; WhatsApp trigger gap and dual-send risk. Direct Google APIs first — higher OAuth cost for the same typed ports. Re-ask Assaf per toolkit — rejected by this instruction.

### ADR-008 Today-vs-baseline = previous 7 completed local days' daily average

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT

**Context**
Bible §20.2 requires comparing today (partial day) to a baseline. Partial-day spend/impressions/clicks must not trigger anomaly investigation because full-day baselines would produce false positives.

**Decision**
Baseline = previous seven **completed** local-calendar days (since=D-7, until=D-1 inclusive Meta date strings). Display is read-only: `date_preset="today"` vs baseline `time_range` from `baseline_7d_time_range`. Additive metrics (spend, impressions, clicks) show 7-day total ÷ 7; CTR compares aggregate ratios without dividing by 7. Missing paired metrics omitted. This comparison does not create an anomaly, change recommendation priority, or persist `CampaignRecommendation`.

**Consequences**
Owner analytics ack may append one informational Hebrew line after the recommendation. Two extra Meta reads when settings exist. No Meta writes. `FakeMetaAdsPort` uses explicit `time_range_snapshots` for baseline range distinct from previous-7d compare range.

**Alternatives considered**
Rolling 7d including today — rejected; partial day skews average. Same window as `previous_7d_time_range` — rejected; that window is D-14..D-8 for 7d-vs-previous-7d anomaly compare, not today baseline. Treat today-under-baseline as anomaly — rejected; false positives on partial days.

### ADR-009 Composio LinkedIn profile + direct member post analytics

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

### ADR-010 Explicit company domain for meeting research

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

### ADR-011 Calendar create after explicit slot confirmation

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT

**Context**
Bible §12.2 / §18.2 require calendar events only after prospect confirmation and conflict checks. Prior slice offered slots (read-only `CalendarPort`) but never created events. Assaf selected **calendar event creation with explicit prospect confirmation** — numbered slot selection only; no implicit booking from “yes”, natural-language dates, or meeting intent alone.

**Decision**
Add separate typed `CalendarBookingPort` (`app/integrations/calendar_booking.py`) for Composio pins `GOOGLECALENDAR_EVENTS_LIST` + `GOOGLECALENDAR_CREATE_EVENT` (toolkit `20260812_00`). Keep `CalendarPort` read-only. R2 `calendar_create` with `in_approved_scope=True` only after valid selection of a stored numbered offer; policy AUTO, no owner approval. Idempotency via `privateExtendedProperty` `mia_booking_key=sha256(lead_id|start|end)` lookup before create. Conflict recheck with `find_free_slots` on exact 30m window before create (CREATE has no conflict check). Persist `meetings.status=booked`, canonical `MEETING_BOOKED` (`{status, scheduled_at UTC}` only). No attendees, description, htmlLink, or PII in provider args.

**Consequences**
- **Security/permissions:** R2 auto only in code-defined scope (exact `1`/`2`/`3`, `slot N`, `option N`, Hebrew ordinals). Kill switch denies before recheck/lookup/create. Meet links stored/returned only when host is exactly `https://meet.google.com`. Live Composio create needs Calendar **write** OAuth scope on connected account — operator action; code alive by fake/mock.
- **Reliability:** Lookup returns typed `found|not_found|error`; pagination exhaustion with remaining token => error. Lookup runs before conflict recheck; found event skips recheck (crash recovery). Conflict recheck requires returned free slot to fully cover selected interval. `mark_meeting_booked` revalidates all fields; false persist => RETRY, no `MEETING_BOOKED`. Booked leads never re-offer; unrelated follow-up skips calendar ports.
- **Cost/lock-in:** Up to 3 Composio executes per booking (recheck read + lookup + create); idempotent retry skips create when lookup hits.
- **Migration/files:** `meetings.offered_slots_json`, `meetings.meet_link`, `calendar_event_id` → `String(1024)`; `app/domain/meeting_slots.py`, `app/domain/calendar_booking.py`, `app/api/inbound.py`, `app/api/website.py`, tools `calendar_booking_lookup`/`calendar_create`, tests `test_calendar_booking.py`.
- **Tests:** 968 passed (2026-08-21); parser, offer persistence, R2/kill switch, conflict, idempotency, Composio args, E2E inbound+website.

**Alternatives considered**
Owner approval after slot pick — rejected (Assaf: R2 auto in approved scope). Natural-language date parsing — rejected. Single combined calendar port — rejected (read vs write separation). Attendee invite on create — rejected (out of scope).

### ADR-012 Meeting availability policy (Sun–Thu IL business hours)

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT

**Context**
Final Mile Playbook Gate 2 requires real availability, correct timezone, and no invented slots. Assaf locked business hours for intro-call booking: **Sunday–Thursday, 09:00–17:00 Asia/Jerusalem, minimum 24 hours notice**. No env bypass this slice.

**Decision**
Deterministic policy in `app/domain/meeting_availability.py`. Workdays Python weekday Sunday=6, Monday=0 … Thursday=3; reject Fri/Sat. Local window `[09:00,17:00)`; 30-minute slots aligned to `:00`/`:30` local boundaries; meetings must end ≤17:00. Start ≥ `now + 24h` exactly. Carve provider free gaps into max 3 policy-valid slots in `prepare_meeting_offer`; persist same slots in `offered_slots_json`. Re-evaluate policy at confirmation with threaded `now`; stale slot => conflict, no create. Post-create **verify** via second `find_by_booking_key` (`calendar_booking_verify` audit tool); no local booked state or success reply before verify ok. Persist `meeting_type=intro_call`, `booked_at` UTC ISO on book. Customer copy follows Human Voice Standard (native concise Hebrew).

**Consequences**
- **Security/permissions:** Policy not configurable this slice. Verify mismatch / not_found after create => RETRY, no booked row.
- **Reliability:** Create timeout + verify found => persist from verified event (no duplicate retry). Preflight lookup (crash recovery) unchanged; no extra verify.
- **Cost/lock-in:** Up to 4 Composio executes per new booking (recheck + preflight lookup + create + verify lookup).
- **Migration/files:** add `meetings.meeting_type VARCHAR(32) DEFAULT 'intro_call'`, `meetings.booked_at VARCHAR(32) DEFAULT ''`; `app/domain/meeting_availability.py`, `app/domain/booking_voice.py`, `calendar_booking_verify` in tool allowlist; tests `test_meeting_availability.py`, extended `test_calendar_booking.py`.
- **Tests:** policy boundaries, verify-after-create, metadata. At ADR-012 acceptance, Gate 2 still needed reschedule/cancel plus staging OAuth; ADR-013 now closes the safe code boundary, while live staging OAuth remains open.

**Alternatives considered**
Configurable hours via env — rejected (Assaf: no bypass). Trust create response without verify — rejected (Final Mile write verification). Offer arbitrary gap times (e.g. 12:17) — rejected.

### ADR-013 Automatic confirmed reschedule; cancellation request for Assaf

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT (automatic confirmed reschedule; cancellation request for Assaf)

**Context**
Final Mile Gate 2 requires safe reschedule and cancellation behavior. A reschedule is reversible through another PATCH and can be constrained to an exact stored event, exact numbered slot, current availability, and post-write verification. Deleting or cancelling the provider event is destructive R5 behavior and is denied by the higher-priority safety policy.

**Decision**
For a booked meeting with a validated stored provider event ID, accept only exact whole-message reschedule phrases. Read current availability under ADR-012, store up to three options separately, and accept only a numbered stored option. R2 `calendar_reschedule` is AUTO only in that code-defined scope. Pre-read the exact event with `GOOGLECALENDAR_EVENTS_GET`, revalidate policy and conflict, PATCH only event ID, start, end, timezone, calendar `primary`, and `send_updates=none` through `GOOGLECALENDAR_PATCH_EVENT`, then GET again. Local state changes only when event ID and exact UTC interval verify. If the pre-read already matches the target, recover locally without PATCH.

For exact cancellation phrases, write only local status `cancellation_requested`, timestamp it, clear pending reschedule offers, and tell the customer that Assaf will update the calendar. Do not call Calendar ports and do not claim the provider event was cancelled. Repeated requests are idempotent. Provider delete/cancel remains denied.

**Consequences**
- **Security/privacy:** No attendee, summary, description, conference data, extended properties, event ID, Meet link, or PII enters PATCH audit payloads or canonical reschedule/cancellation events. Calendar deletion remains unavailable.
- **Reliability:** GET uncertainty blocks PATCH. PATCH timeout still proceeds to mandatory GET verification. Verified provider state is authoritative. The stored event ID, Meet link, booking timestamp, and meeting type remain unchanged.
- **Follow-up:** Verified booking or booking crash recovery closes a pending meeting-offered follow-up with reason `meeting_booked`; stale pending rows are never send-ready once meeting status is booked or cancellation-requested. Reschedule offers and cancellation requests do not create or reopen follow-up.
- **Cost/lock-in:** New live reschedule uses up to four Calendar calls after selection: GET, exact free-slot read, PATCH, GET. Same Composio toolkit pin `20260812_00`; provider remains behind typed ports.
- **Migration/files:** Add `meetings.reschedule_slots_json`, `meetings.rescheduled_at`, and `meetings.cancellation_requested_at`; allow `offered|booked|cancellation_requested`. Migration: `migrations/20260821_adr013_calendar_gate2.sql`.
- **Acceptance:** Create and reschedule are alive by fake. Real staging OAuth CREATE/PATCH/GET remains an operator acceptance action. Cancellation is a manual request by safety design, so Gate 2 production acceptance is not complete until that live test and Assaf's manual calendar update path are verified.

**Alternatives considered**
Direct provider delete/cancel — rejected as R5 destructive. Owner approval before every exact reschedule — rejected because the accepted R2 scope is deterministic and verified. Natural-language date parsing or embedded intent — rejected as ambiguous. Full event PUT — rejected because it could overwrite attendees or event content.

### Gemini sales fallback (Assaf 2026-08-22)

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** add Google supplier fallback for sales paraphrase

**Decision**
Keep OpenAI as the primary sales Chat Completions path. After OpenAI primary + optional OpenAI fallback model, try Gemini AI Studio once via the official OpenAI-compat URL `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` (`MIA_GEMINI_API_KEY` + `MIA_SALES_GEMINI_MODEL`). Same Human Voice lint; fail → canned. Not Vertex. Not a Gmail/Calendar path. Model ids stay env/eval (ADR-007).

**Alternatives considered**
Vertex AI OpenAI-compat — rejected for local operator setup (GCP project + ADC). Native Gemini generateContent — rejected this slice; Chat Completions JSON already matches the OpenAI adapter.

### ADR-014 First AWS production: Fargate + RDS + Secrets Manager box

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (AWS production; keys in a box, not pasted into Mia)

**Context**
Bible §29 wants AWS for ingress, secrets, and runtime. Assaf wants production on AWS and must not put provider keys in git, chat, or a host `.env` file. A custom Lambda that “holds the keys” still has to give those keys to whoever calls OpenAI/Composio. LangGraph on Lambda-only is rejected in the Bible.

**Decision**
1. **Key box** is AWS Secrets Manager secret `mia/prod` (KMS). Assaf creates the JSON keys (`MIA_*` SECRET fields). Never commit values. Never paste values in chat.
2. **ECS Fargate** injects those JSON keys as container environment variables at task start ([Pass Secrets Manager secrets through Amazon ECS environment variables](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html), platform 1.4.0+). Mia’s adapters keep reading `Settings()` from env. Mia’s **source code and git never contain keys**.
3. **RDS PostgreSQL** is the system of record. App uses `psycopg` (`postgresql+psycopg://`).
4. **HTTPS** is ALB + ACM on `https://mia.assafweb.com`. Landing-page `NEXT_PUBLIC_MIA_BASE_URL` is that origin.
5. **Lambda** is **not** the sales graph and **not** the key box. Lambda webhook ingress (fast ACK → SQS FIFO → Fargate) stays the next AWS slice after first live is healthy. AgentCore stays a later benchmark (ADR not written until measurements exist).

**Consequences**
- **Security:** Task execution role may `secretsmanager:GetSecretValue` on `mia/prod` only. Logs still redact. GraphState still forbids secrets. Rotating the secret requires a new Fargate deployment (ECS does not hot-reload env secrets).
- **Reliability:** One FastAPI process still verifies webhooks and runs LangGraph until the Lambda/SQS split ships.
- **Cost/lock-in:** Always-on Fargate, not Lambda-per-request for conversations. Provider-neutral domain layer unchanged.
- **Migration/files:** `deploy/Dockerfile`, `deploy/ecs-task-definition.example.json`, `psycopg[binary]`, `app/db/session.py` DSN pin. `CapabilityId.AWS_RUNTIME` stays **specified** until ALB+RDS are actually running.

**Alternatives considered**
Keys in laptop `.env` copied onto a VPS — rejected for production. Custom Lambda as the only place keys live, Mia calling Lambda per OpenAI request — rejected this slice (duplicates Secrets Manager; becomes a tool gateway). AgentCore now — rejected until a frozen runtime benchmark exists (historical notes in `docs/archive/RUNTIME_DECISION_PLAN.md`). Vercel/Cloudflare Workers for the graph — rejected.

### ADR-015 Production adapter map (Composio vs Meta vs Firecrawl)

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat/goal: lock production suppliers; clean unused)

**Context**
Assaf locked a production supplier map. ADR-007 still forbids dumping catalogs into the model. Official Composio Instagram tools cover send, list, and insights — **no inbound DM trigger**. Composio `LINKEDIN_GET_SHARE_STATS` is organization-page analytics only (ADR-009). Composio WhatsApp inbound is still a no-op poll (ADR-006).

**Decision**

| Job | Production adapter |
| --- | --- |
| Brain | LangGraph |
| Data (SoR) | Postgres |
| WhatsApp inbound | Meta Cloud API webhook (HMAC) — ADR-016 |
| WhatsApp send | `MIA_WHATSAPP_SENDER=direct` (Graph) or `composio` (`WHATSAPP_SEND_MESSAGE`); never both |
| Instagram **inbound** | Meta webhook HMAC (same reason as WhatsApp) |
| Instagram **send + organic insights** | Composio Instagram (pins when adapters land; Graph tokens stay until then) |
| Gmail ingest | Composio |
| Calendar | Composio |
| Sheets mirror | Composio |
| LinkedIn profile | Composio |
| LinkedIn personal member post analytics | Direct REST + `MIA_LINKEDIN_ACCESS_TOKEN` (ADR-009). Composio has org share-stats only. |
| Meta Ads **read** | Composio |
| Research | Firecrawl primary; Apify `google-search-scraper` behind the same `ResearchPort` when Firecrawl is unset (ADR-030) |
| ManyChat | Not mounted in v1 (ADR-021). Leftover AWS secret name stays in the box; app ignores it. |
| Composio WhatsApp toolkit | Send pin `WHATSAPP_SEND_MESSAGE` only when sender=`composio`. No inbound trigger. Template send not wired. |

One Instagram sender per conversation (`direct` or `composio`). Never dual-send Graph + Composio. ManyChat is unmounted (ADR-021).

**Consequences**
Env/docs/JSON list Meta tokens for WhatsApp and for Instagram **webhook verify**. Composio key+user covers Gmail/Calendar/Sheets/GSC/GA4/LinkedIn profile/Meta ads/future IG send+insights. Member analytics stays Direct REST (ADR-009); Composio has org share-stats only. GSC/GA4/Meta ads resource ids are leftover env, optional only when `MIA_COMPOSIO_DISCOVERY=true` (ADR-027). Sheets id stays explicit. Apify token is `MIA_APIFY_TOKEN` (ADR-030). Do not rip live Graph IG send until the Composio port is tested.

**Alternatives considered**
Composio for WhatsApp or Instagram inbound — rejected; no usable inbound trigger. Composio `LINKEDIN_GET_SHARE_STATS` for personal analytics — rejected; org URN only (ADR-027). Default-everything-Composio including catalogs in the model — rejected (ADR-007). WhatsApp send via Composio is ADR-016.

### ADR-016 WhatsApp inbound stays Meta; Composio may own send

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat: migrate WhatsApp to Composio if the complete production flow exists)

**Context**
Assaf asked to make Composio the preferred WhatsApp Business layer. Re-check on 2026-08-22: official toolkit `WHATSAPP` version `20260815_00`, managed app **Yes**, 57 tools, **one** trigger `WHATSAPP_MESSAGE_STATUS_UPDATED_TRIGGER` (delivery status poll; Composio documents WhatsApp has no native status-poll API so the trigger is empty). Live MCP `COMPOSIO_GET_TOOL_SCHEMAS` + `docs.composio.dev/tools/whatsapp` match. `WHATSAPP_GET_MESSAGE_HISTORY` returns delivery-status audit rows (`id`, `message_id`, `events.delivery_status`) — no customer text, media, sender, or inbound/outbound body. Connected Composio account had **no** active WhatsApp connection. Third-party toolkits (Waboxapp, Mocean, Kapso) are extra vendors, not the official Cloud API toolkit. ADR-006 still holds for ingress.

**Decision**
Do **not** fake an inbound-message trigger. Do **not** poll `WHATSAPP_GET_MESSAGE_HISTORY` as an inbox. Keep Meta webhook as the thin inbound transport (`POST /v1/whatsapp/webhook`, HMAC, message ids, STT media via Graph). Composio webhook continues to ignore WhatsApp slugs (status updates are not customer messages). Outbound: one sender via `MIA_WHATSAPP_SENDER` (`direct` default | `composio`). Composio pin `WHATSAPP_SEND_MESSAGE` toolkit `20260815_00`; requires `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` + `MIA_WHATSAPP_PHONE_NUMBER_ID`. `WHATSAPP_SEND_TEMPLATE_MESSAGE` is not wired (no mass outreach). Dual Meta+Composio send is forbidden. Meta verify/app-secret/access-token stay until inbound (and Graph media) no longer need them. Shadow, owner acks, idempotency, and LangGraph unchanged.

**Consequences**
Near-real-time inbound stays Meta. Composio can own send auth/token refresh after Assaf connects WhatsApp on the existing `MIA_COMPOSIO_USER_ID`. Health reports `whatsapp_ingest` only for a working Meta inbound path, not because a Composio API key exists. Phone number id remains required for Composio send.

**Alternatives considered**
Composio as sole inbound+outbound — rejected; no incoming-message trigger. History polling worker — rejected; tool is delivery receipts, not an inbox. Waboxapp/Mocean/Kapso inbound — rejected extra vendors (ADR-006). Rip Meta webhook now — rejected; would lose customer messages.

### ADR-017 v1 communication operating model

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat: finalize Mia v1 communication operating model)

**Context**
Assaf locked four answers: private owner talk is Telegram; customers talk with Mia on AssafWeb; personal WhatsApp stays human-only; hot leads stop selling and hand to Assaf on Telegram. WhatsApp is not an open inbox. Email may read/draft; send stays approval-gated. One Mia brain. Composio still has no WhatsApp inbound-message trigger (ADR-016).

**Decision**
Telegram is the owner control channel (numeric user-id allowlist, existing owner brain). Website is the primary autonomous sales channel. WhatsApp is a controlled continuation of a **verified website handoff** (`MIA_BUSINESS`); unknown/personal/`DO_NOT_AUTOMATE` contacts get no reply, no lead, no follow-up, no STT. Production `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=true`. Transport stays ADR-016 (Meta inbound, one outbound sender). Instagram is not a v1 autonomous conversation expansion. Hot `NextAction.HANDOFF` sets `HUMAN_TAKEOVER_REQUIRED`, cancels follow-ups, notifies Telegram when the owner bot is configured. Manual provider echo detection is **not** claimed (requires Meta coexistence + `smb_message_echoes`; not subscribed or parsed).

**Consequences**
Assaf talks to Mia in Telegram. Customers start on the website. A click-to-chat token continues the same lead on WhatsApp. Friends who message the Business number hear silence. Unit tests that still drive sales over WhatsApp as a transport set `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=false` in `tests/conftest.py`; communication-model tests turn the gate on.

**Alternatives considered**
Composio as sole WhatsApp provider — rejected (ADR-016). Classify arbitrary WhatsApp threads as leads from business-like language — rejected. Dual Meta+Composio send — forbidden. TTS — out of v1. Instagram as a fourth sales inbox — deferred.

### ADR-018 Website offers WhatsApp after first real friction

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat: move qualified/engaged website prospects to WhatsApp earlier)

**Context**
Live website widget looped the opening “יום רגיל בעסק” question. Full MEDDPICC discovery on the site is not how AssafWeb sells. Website is for starting the relationship, basic workflow, and first friction. WhatsApp is the continuation channel, not only the close.

**Decision**
`NextAction.OFFER_WHATSAPP` is a website continuation offer, distinct from owner `HANDOFF`. Website graph passes `channel="website"` into `select_next_action`. After workflow is known and pain is P2+ (identifiable friction), Mia offers WhatsApp in short conversational Hebrew and persists `whatsapp_handoff_offered`. Greeting / one vague sentence does not offer. Token issue stays `POST .../handoff`. Context must survive. Graph Lab `website_handoff_v1` is the shoe-store regression. `buyers_v1` stays unchannelled.

**Consequences**
Clinic “miss calls all day” on the website offers WhatsApp before reflect. Same transcript on Graph Lab / inbound WhatsApp still reflects. Demo scripted funnel offers WhatsApp first, then continues to meeting if the visitor keeps talking on the site.

**Alternatives considered**
Reuse `HANDOFF` — rejected; that is Assaf takeover + Telegram notify. Hard-code “after N messages” — rejected; measure empirically. Wait until every discovery field is complete — rejected; that was the live loop.

### ADR-019 Selected Region is eu-north-1

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat: Bible `il-central-1` is old; live project Region is `eu-north-1`)

**Context**
The new AWS experience pins one selected Region from the contact address. This project can create Regional resources only in `eu-north-1`. The Bible still said `il-central-1`. Live Fargate, RDS, ALB, ACM, and Secrets Manager already run in `eu-north-1`.

**Decision**
Selected Region for Mia is **`eu-north-1`**. Do not create Lambda, RDS, ECS, or other Regional resources elsewhere. CloudFront remains global. A later Bible-file cleanup pass is a separate Assaf request — this ADR does not delete historical reports.

**Consequences**
Operator scripts and `docs/PRODUCTION_BUILD.md` pin `eu-north-1`. `CapabilityId.AWS_RUNTIME` stays specified until `app.infra` exists. Rekognition/Textract/Personalize/App Runner reduced availability in this Region does not affect current Mia ports.

**Alternatives considered**
Move the project to an account that can use `il-central-1` — rejected; live host already works in `eu-north-1`. Multi-Region — excluded by the new AWS experience.

### ADR-021 Documentation core set; ManyChat not a v1 runtime channel

- **Status:** superseded
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: `/goal` simplify repository without breaking v1)
- **Superseded by:** ADR-031 (living agent docs). ManyChat remains unmounted.

**Context**
The repo accumulated overlapping MD files (PRD dump, HANDOFF, playbooks, gap reports) that burned agent context. ManyChat was an optional Instagram sidecar, not part of the ADR-017 v1 channel set.

**Decision**
Living docs are: `AGENTS.md`, `README.md`, `docs/PROJECT_MAP.md`, `docs/ARCHITECTURE.md`, `docs/PRD.md` (short), `docs/BUILD_STATUS.md`, `docs/RUNBOOK.md`, `docs/DECISIONS.md`, plus operator `docs/PRODUCTION_BUILD.md`. Historical material lives in `docs/archive/`. ManyChat HTTP ingest is unmounted. Instagram inbound webhook and insights stay (ADR-015); Instagram is still not a v1 sales inbox. Unused `MIA_MANYCHAT_INGEST_TOKEN` in AWS Secrets Manager is documented, not deleted.

**Consequences**
Agents load the map first. Do not grow `docs/PRD.md` back into a Bible dump. Do not remount ManyChat without a new ADR.

**Alternatives considered**
Delete Instagram inbound entirely — rejected; analytics/insights and ADR-015 inbound HMAC remain locked. Delete the ManyChat secret from AWS — rejected; document unused secrets only.

### ADR-022 Production live sales test: leave shadow, keep gated writes off

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: leave shadow; give Mia full v1 sales capability; check safety before new image)

**Context**
Production `mia:9` was `MIA_AUTOMATION_MODE=shadow`. Website already replied. Verified WhatsApp handoff did not send. Assaf started live testing and rejected the silent WhatsApp continuation. Literal “unlock every flag” would open Gmail send, Meta writes, and Instagram as a sales inbox.

**Decision**
Production may run `MIA_AUTOMATION_MODE=auto_approved` so verified website→WhatsApp continuation can send. `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=true` stays. Instagram prospect send stays off unless `MIA_AUTO_REPLY_INSTAGRAM=true` (code gate, not shadow). Gmail send, Meta writes, follow-up send, browser automation, dynamic tool discovery, R4 auto, and R5 stay denied. Kill switch still stops all gated actions.

**Consequences**
Friends / unknown WhatsApp numbers still get silence. A valid `mia1_` handoff token continues the website lead and Mia may reply. `/health` shows `automation_mode=auto_approved` and `auto_reply_instagram=false`. Roll back prospect DMs with `MIA_AUTOMATION_MODE=shadow` plus a new task revision.

**Alternatives considered**
Keep shadow and only flip `MIA_WHATSAPP_HANDOFF_SEND` — safer, but Assaf asked to leave shadow. Flip every write flag — rejected. `auto_approved` without an Instagram send gate — rejected; that would make Instagram a v1 sales inbox.

### ADR-023 Model routing: deterministic decisions, model paraphrases

- **Status:** proposed
- **Date:** 2026-08-23
- **Assaf:** unset

**Context**
Phase 6/7 asked which model runs which task, chosen on measured quality rather than brand. Reading the code found three model call sites (sales reply paraphrase, Gmail thread summary, transcription) and no runtime task-to-model router. Everything that decides, permits, scores or routes is pure Python. The eval harness is fully deterministic: it never calls a real model and measures no latency, tokens or cost, so no per-model scores exist to select on.

**Decision**
Keep the current split and write it down: deterministic tasks never call a model; the sales reply is a paraphrase of deterministic canonical copy, never a free author; each model port keeps its own primary/fallback chain with no cross-task router. Full reasoning and the explicit list of what has not been measured are in `docs/MODEL_ROUTING_DECISION.md`. Do not claim a model comparison until a live-port harness mode with latency, token and cost capture exists. Owner Telegram phrasing was later added as the same paraphrase pattern (ADR-025); classification and tools stay in Python.

**Consequences**
A model outage degrades phrasing, never routing or permissions. Sales judgment is testable for free, which is why the eval datasets are worth trusting. A new owner intent costs a phrase list and a test rather than a prompt. The cost of this shape is that `ai_runs.cost_usd` stays a placeholder until a price table exists. Selecting or changing a production model id remains an operator action with no scoring behind it.

**Alternatives considered**
Let a model choose the next sales action — rejected; it moves the deal-losing decision into the untestable layer. Add a model composer for Telegram owner replies — deferred at the time; taken in ADR-025 after Assaf asked for conversation reasoning. Build the task-class router now — rejected; routing configuration with no scoring data behind it is invented surface. Publish candidate model scores from the existing harness — rejected; the harness scores canned copy, so those numbers would describe nothing.

### ADR-024 WhatsApp stays human until official Cloud API inbound

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: Composio cannot receive WhatsApp; handle the person himself until official API; Telegram gets the briefing)

**Context**
Click-to-chat pointed at Assaf's personal number. Meta Cloud API never delivered a customer message to Mia in 48h of production logs. Composio has no WhatsApp inbound-message trigger (ADR-016). Assaf tested from a second number and still got silence. He will handle WhatsApp himself until official Cloud API inbound exists.

**Decision**
`MIA_WHATSAPP_HANDOFF_SEND` stays false and now gates WhatsApp prospect send in every automation mode, including `auto_approved`. Website still talks. When the visitor clicks through, wa.me opens Assaf with a human Hebrew prefill (no `mia1_` token in the customer message) and Telegram receives a one-time briefing of the website conversation plus a paste-ready first WhatsApp line. Mia does not reply on WhatsApp. Flip the flag only after Cloud API inbound is proven.

**Consequences**
Customers are not ghosted: Assaf sees them. The ugly token is gone from the compose box. Identity binding via token is deferred until inbound works. ADR-022's "Mia may reply on verified handoff" is paused, not deleted.

**Alternatives considered**
Keep chasing Cloud API / Composio inbound — rejected; Composio cannot ingest WhatsApp messages. Put the token back in wa.me so a future webhook can bind — rejected for now; it made the handoff look broken. Global shadow — unnecessary; website and Telegram already send.

### ADR-025 Conversation reasoning on website and Telegram paraphrasers

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: give Mia reasoning on conversion, both website and Telegram)

**Context**
Website sales already paraphrased canned copy, but the prompt did not require a think-then-speak step, so the model could parrot or restart. Telegram owner replies were still canned templates. Assaf asked for conversation reasoning on both channels. Dumping the Composio catalog into the Telegram model would let untrusted text pick privileged tools.

**Decision**
Keep next-action, owner-task classification, approvals and Composio calls in Python. Upgrade `sales_reply_v5` so the website model reasons silently about what the prospect said, what is known, and the one conversion move that serves INTENT, then writes only the customer message. Wire `owner_telegram_v2` as a paraphraser over the typed RESULT. If the owner paraphrase drops a lead id or email, looks like a tool call, or the kill switch is on, send the canned RESULT.

**Consequences**
Both channels can sound like a conversation without the model choosing strategy or tools. A model outage still degrades phrasing only. Owner list accuracy is protected by the fact-preservation fallback. No new env knobs; owner phrasing reuses the sales model chain.

**Alternatives considered**
Let the Telegram model pick Composio tools — rejected; catalog dump and untrusted-text tool choice. Free-author sales replies without INTENT — rejected; NBA stays testable in code. Keep Telegram canned — rejected; Assaf asked for reasoning on that channel too.



### ADR-026 Mia's brain: long-term memory, knowledge, and an owner tool loop

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: A1 full agentic loop, B-now website brain, C2 handoff card; make her know me, remember, learn from the website and Telegram, understand voice. WhatsApp explicitly out of scope.)

**Context**
The owner console was a keyword switchboard: a phrase table picked one of ~25 task types, a 700-line `if` chain ran one Python function, and the model only rephrased the result. Off-list phrasing fell through to a generic digest, two requests in one message were impossible, and nothing survived the conversation. Mia had no long-term memory at all — `app/domain/memory.py` was a 24-turn transcript read model over `canonical_events`. Separately, owner voice notes were failing silently in production: the transcription adapter sent `response_format=verbose_json` with `gpt-transcribe`, a whisper-1-only format, and the caller swallowed the error into "לא תפסתי את ההקלטה".

**Decision**
Add `app/brain/`: a memory store with four kinds (episodic, semantic, working, preference), website/business knowledge ingestion, hybrid retrieval, and extraction/consolidation. Add an owner tool-calling loop over an allowlisted read-only registry. Keep every write and approval intent on the existing deterministic path (`DETERMINISTIC_TASK_TYPES`). Store embeddings as base64 float32 in a portable `TEXT` column and do exact cosine in Python — not pgvector, whose SQLAlchemy type is PostgreSQL-only and would make the test suite exercise a different retrieval path than production. Fix transcription to pick parameters per model family. Move Telegram to `parse_mode=HTML` with inline approve/reject buttons and `callback_query` handling.

**Consequences**
Mia answers freely, chains several reads in one turn, and remembers across conversations. Nothing about the safety architecture moved: the allowlist is enforced server-side on the returned tool name, writes still go through `app/core/risk.py` and `app/domain/approvals.py`, and the model never sees a Composio catalog. Every layer degrades independently — no model keys means the old classifier answers, which is how the 2000-test suite runs. New tables are additive and create identically on SQLite and Postgres. Gated actions finally have a completion path (one-tap approval) instead of a dead end. `MIA_OWNER_AGENT_MODEL` is the on switch; it ships empty.

**Alternatives considered**
Hybrid router keeping the classifier as a fast path — rejected; Assaf picked A1, and the classifier's single-task ceiling was the actual complaint. pgvector with an HNSW index — rejected at this scale; pgvector documents exact search as perfect-recall, and its own IVFFlat sizing rule yields a degenerate `lists=3` here. Routing raw audio to an audio-in chat model instead of transcription — rejected; roughly an order of magnitude more expensive per minute and it loses the documented Hebrew language/keyword hints. MarkdownV2 for Telegram — rejected; 18 escape characters under three context-dependent rules, and every id, email and decimal Mia interpolates is a landmine there. Firecrawl crawl as the primary knowledge source — rejected; the site already publishes `llms.txt`/`llms-full.txt`/`pricing.md`, which is cleaner, free, and owner-maintained. Firecrawl stays as the fallback.

### ADR-027 Opt-in Composio discovery for GSC/GA4/Meta; Sheets and LinkedIn stay explicit

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

### ADR-034 LinkedIn v1 is Composio profile; member-analytics token is optional

> Renumbered from ADR-028 when the VNext rebuild was merged with the shipped
> `mia:20` branch, which had already used 028–032 for different decisions.
> Production's ids win because they are cited in shipped code.

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** ADOPT (deploy: LinkedIn through Composio, no access-token key)

**Context**
ADR-009 still describes how member post analytics would work (direct REST + `MIA_LINKEDIN_ACCESS_TOKEN`). Composio still has no member analytics tool. Assaf's live LinkedIn connection is the profile toolkit. Requiring the leftover token listed it on `/health` as missing even though profile reads already work.

**Decision**
v1 owner LinkedIn is Composio `LINKEDIN_GET_MY_INFO`. Do not add `MIA_LINKEDIN_ACCESS_TOKEN` to go live. `/health` `owner_integrations.missing` does not list that token. `linkedin_analytics` stays false until a real member token exists. Do not fake member stats with org share-stats.

**Consequences**
Telegram can answer LinkedIn profile questions via the existing Composio port. Member analytics stays dark unless Assaf later supplies the leftover token. ADR-009 remains the analytics adapter if that token appears.

**Alternatives considered**
Keep listing the token as missing — rejected; it blocked a clean health picture for a capability Assaf is not shipping. Wire `LINKEDIN_GET_SHARE_STATS` as analytics — rejected; org URN, wrong job.

### ADR-035 Apify google-search-scraper behind ResearchPort

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

### ADR-036 VNext two graphs + canonical docs

> Renumbered from ADR-031 in the `mia:20` merge (see ADR-034).

- **Status:** accepted
- **Date:** 2026-08-25
- **Assaf:** ADOPT (chat: `/goal` rebuild per `MIA_REBUILD.MD`)

**Context**
The live app is one FastAPI process with a one-node sales LangGraph and a custom owner tool loop inside `process_inbound_texts`. Documentation required agents to load PROJECT_MAP, PRD, BUILD_STATUS, HANDOFF, and more. Brain code is on the HTTP path but semantic memory is gated on empty default model ids. There is no conversation-finalization → Telegram summary (only WhatsApp-click briefing).

**Decision**
1. Two compiled LangGraph entry points: `OwnerGraph` (Telegram) and `ClientGraph` (website). Shared core; separate state, prompts, tools, permissions.
2. Channels stay thin adapters. Capability layer + Python policy sit in front of Composio. Website visitors cannot execute owner capabilities even under prompt injection.
3. Canonical agent reading: `AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`. Operator files `docs/RUNBOOK.md` and `docs/PRODUCTION_BUILD.md` remain for humans/tests, not required agent reading. ADR-021’s living-doc list is superseded; ManyChat stays unmounted.
4. Reuse `app/brain/`, STT, Postgres, risk policy. Strangle `inbound.py` — do not delete until the new path is tested.
5. Add explicit website conversation finalization with idempotent Telegram notify.
6. Do not auto-deploy. Do not add pgvector. Do not declare brain embeddings/extraction ALIVE when model ids are empty.

**Consequences**
Owner and client reasoning no longer share one inbound mega-handler. Prospect Meta/Gmail inbound compiles ClientGraph (same NBA, not a third graph). New integrations plug in as capability + policy + adapter + graph allowlist. Old classifier remains fallback until OwnerGraph is proven. Production still needs `MIA_OWNER_AGENT_MODEL` / `MIA_EXTRACTION_MODEL` / `MIA_EMBEDDING_MODEL` plus a one-off `mia-ingest-knowledge` for a non-empty corpus.

**Alternatives considered**
Keep one ReAct loop for both users — rejected (rebuild §36). Rewrite brain onto pgvector — rejected (ADR-026). Delete WhatsApp/Gmail webhooks in this slice — rejected; preserve production contracts until replaced.

### ADR-037 Delete ManyChat from the product

> Renumbered from ADR-032 in the `mia:20` merge (see ADR-034).

- **Status:** accepted
- **Date:** 2026-08-25
- **Assaf:** ADOPT (chat: delete all ManyChat integration)

**Context**
ManyChat was never mounted in v1 (ADR-021). Assaf asked to remove it from the product, not leave it as a deferred sidecar. The app still declared `manychat` specified, injected `MIA_MANYCHAT_INGEST_TOKEN` in the ECS example, and documented a leftover AWS secret.

**Decision**
Remove ManyChat from runtime code, capability map, health, `.env.example`, and ECS/secret examples. Instagram senders stay `direct` | `composio`. Unused `channel_identities` columns remain in Postgres so existing databases do not need a drop migration. This repo does not read or delete a leftover AWS secret name.

**Consequences**
`POST /v1/manychat/external-request` stays gone (404). New task revisions must not inject `MIA_MANYCHAT_INGEST_TOKEN`. Do not remount ManyChat.

**Alternatives considered**
Keep the unused AWS secret documented forever — rejected for new deploys; the live box is not touched from this repo. Drop leftover identity columns now — rejected; avoid a production schema change for empty columns.

### ADR-028 Visitor knowledge on the website path, answer-then-ask, and the meeting as default exit

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** ADOPT (chat: go ambitious, it is our product)

**Context**
Three findings from a product review of the live system. First, `app/api/website.py` imported nothing from `app/brain/`, and `ReplyContext` carried turns, known facts and asked actions but no retrieved knowledge — so the 31 ingested knowledge chunks (services, portfolio, FAQ, pricing, testimonials) were reachable only from Assaf's Telegram owner agent. The person with questions could not reach the knowledge; the person who already knows everything could. Second, Mia ran a cold-call discovery script on a warm inbound surface: she took before she gave, so a visitor asking whether we build online stores got the next ladder rung instead of an answer. Third, `MIA_CALENDAR_WRITE=true` was already live and booking was deterministic and measurable, yet the website's top exit was `OFFER_WHATSAPP` into a human inbox, and ADR-024 deliberately keeps the token out of the `wa.me` prefill — so nothing linked a website conversation to what happened next and the funnel was structurally unclosable.

**Decision**
Add `assemble_visitor_context` to `app/brain/context.py`: knowledge-only retrieval for the website path, with a hard invariant that it never calls `retrieve_memories`, `build_profile_block`, `list_memories`, `memory_vectors`, `touch_memories` or `list_open_gaps`. Owner memory is owner-only in both directions — the PRD already said a visitor can never write it, and the read side of that boundary is now enforced in code and pinned by a test using a store whose memory methods raise. Add `knowledge` to `ReplyContext` and a PUBLISHED ASSAFWEB FACTS section to the turn prompt. Move the reply shape to answer-then-ask at `sales_reply_v8`: when the visitor asks something the published facts cover, answer it in one sentence, then serve INTENT with its one question; when they do not cover it, say so plainly. `select_next_action` stays fully deterministic — this is a paraphrase-layer change only. Separately, add `meeting_first` to `select_next_action` (default **false**, so every non-website caller is unchanged) and `MIA_WEBSITE_MEETING_FIRST` (default **true**): when the existing WhatsApp continuation gate passes and the meeting exit was not already offered, the website offers the booked meeting. WhatsApp stays the fallback, reachable once the meeting was offered and not taken, or on an explicit request. New persisted `SalesState.meeting_exit_offered` plus migration `20260824_lead_sales_state_meeting_exit.sql`.

**Consequences**
The visitor can finally get an answer from the published corpus, and the measurable exit is now the default one. `build_graph`'s `knowledge_lookup` defaults to `None`, so every existing caller is byte-identical. The lookup is skipped entirely while the kill switch is set (no embedding call on a killed turn) and any exception it raises is swallowed as no knowledge — a brain outage degrades phrasing, it never 500s a customer. The canned/no-model path is unchanged. **A meeting can now be offered before the `OFFER_HYPOTHESIS` rung is reached**, so a meeting brief may carry `hypothesis_offered: false`; the pre-ADR-028 WhatsApp offer had exactly this same property, since it fired at the same gate. Price questions still hand off and still quote no number — pinned from both sides. `MIA_WEBSITE_MEETING_FIRST=false` reproduces the old behavior without a deploy.

**Alternatives considered**
Give the website path the full `assemble_owner_context` — rejected outright; it would expose owner memory to anonymous visitors, which is the one thing this design must never allow. Make answer-then-ask a new `NextAction` — rejected; the next-best-action must stay deterministic and testable in code, and this is a phrasing concern. Loosen `lint_customer_reply` to allow a second question mark — rejected; the answer half is a statement, so the one-question rule still holds, and weakening the linter to fit a prompt is backwards. Drop the WhatsApp exit entirely now that the meeting is default — rejected; WhatsApp is still the right escape hatch for someone who wants a human, and removing it would strand that visitor. Put the handoff token back in the `wa.me` prefill to close attribution — rejected here, that is ADR-024's call and it made the handoff look broken; the meeting exit closes the loop without reopening it.

### ADR-029 Website conversion funnel, engine truth line, and multi-owner notification

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** ADOPT (chat: go ambitious, it is our product)

**Context**
Mia is a sales operator that could not report whether she converts. `AiRunRow` was written on every turn with latency and tokens, and the only read anywhere in the codebase was `get_ai_run(run_id)` — one row at a time, never aggregated. `owner_reads.py` ranked conversations by discovery depth but counted almost nothing. The single aggregate that ever reached `/health` was `ops.integration_failures`, which turned out to be noise from expired handoff tokens. Separately, `/health` reported `owner_agent: ready` because `owner_agent_ready()` only checked that a model string was non-empty; the owner agent silently ran the pre-brain keyword classifier in production for a full day and only Assaf's gut caught it. And `_notify_telegram` sent to `sorted(owner_ids)[0]` only, so a second owner id was never notified — a listed open finding, and a blocker on selling Mia as a product.

**Decision**
Add `app/domain/funnel.py`: a read-only local-day website funnel built from counters that already existed (`mia_opened`, `conversation_started`, `meeting_offered`, `whatsapp_handoff_offered`, `whatsapp_handoff`, `meeting_booked`, `handoff`) plus derived integer-percent rates, rendered lead-id free into the owner daily brief and the operator snapshot, replacing the older one-line `format_website_headline`. Promote `_discovery_depth` to a shared public `discovery_depth` so the funnel and the owner reads use one definition rather than forking it. Add `app/domain/engine_health.py` and `LeadStore.aggregate_ai_runs`, with percentiles computed in Python so SQLite and Postgres behave identically, and surface a line reporting how many replies actually ran, the median latency, and how many were canned — a canned count near the total is the signal that the model is failing silently. Rename `_notify_telegram` to `notify_owners`, fan out to every allowlisted owner id with per-recipient failure isolation, and return only the ids actually delivered.

**Consequences**
The daily brief now answers whether the website converted today instead of only how many events fired. Two limits are deliberate and documented in the modules rather than hidden: `engaged` comes from `list_sales_snapshots`, a recency-ordered sample, because `SalesStateRow` carries no timestamp — it is directional, not a same-day count; and `aggregate_ai_runs` is **all-time**, not day-scoped, because `ai_runs` has no timestamp column either. Both need an additive migration to become exact, deliberately out of scope here. Because those counters do not share one window a numerator can exceed its denominator, so `_pct` clamps to 0..100 — without the clamp the `le=100` bound raised `ValidationError` from inside the owner's daily brief, which is a crash, not a rounding detail. Attribution past the WhatsApp click stays open: ADR-024 keeps the token out of the `wa.me` prefill, so nothing links a website conversation to the WhatsApp thread that follows. The meeting exit from ADR-028 is the path that does close.

**Alternatives considered**
Compute percentiles in SQL — rejected; PostgreSQL-only functions would make the test suite exercise a different path than production, the same reasoning that rejected pgvector in ADR-026. Add the `ai_runs` timestamp column in this slice — deferred, not refused; it is the correct fix and it is written down here so it does not get lost. Report a rate above 100 percent honestly rather than clamping — rejected; it is meaningless to a reader and it crashed the brief. Leave `_notify_telegram` single-owner because Assaf is the only owner today — rejected; Assaf's decision is that Mia is a product, and a silent single-recipient assumption is exactly the kind of thing that ships to a second customer and fails quietly.

### ADR-030 Owner Telegram: free conversation, typed Gmail reads, lead by name

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** ADOPT (chat: go implement)

**Context**
After mia:18 the Telegram console still dumped funnel/engine/daily on greetings and on real requests like "תבדקי את המייל". Unmatched long text was coerced to `OPERATOR_SNAPSHOT`. Gmail reads could not list the inbox (`GmailPort` only fetched by message id). Leads showed as hashes. Assaf rejected dumping the Composio catalog into the model; he asked for a typed allowlist, inbox list/search/read, send as draft+Approve, and lead lookup by person name when they said it.

**Decision**
Greetings and ≤3-word chatter stay a one-line hello (`היי אסף, אני כאן.`) and never hit the agent or the digest. Long unmatched text stays `NOTE` so the owner agent answers. Snapshot/funnel/engine only on an explicit brief. Extend `GmailPort` with `list_recent` / `search` / `create_draft` / `send_draft`, pinning `GMAIL_FETCH_EMAILS`, `GMAIL_CREATE_EMAIL_DRAFT`, `GMAIL_SEND_DRAFT` on toolkit `20260817_00`. Agent tools: `gmail_inbox`, `gmail_search`, `gmail_read`, `find_leads`. No send/delete on the agent registry. Draft is a deterministic `gmail_draft` task; send runs only after Approve and `MIA_GMAIL_SEND=true` (stays false). Persist `SalesState.display_name` only from explicit name phrases (`שמי X`, `my name is`); never guess. `find_leads` matches name, headline, or full `lead_…`.

**Consequences**
"תבדקי את המייל" can reach inbox tools instead of a canned dump. Send cannot happen from the model. Empty names stay empty until stated; headlines still work. Shipped image **mia:19**, task **mia:21** after migrate exit 0. Rollback: image `mia:18` / task `mia:20`, or blank `MIA_OWNER_AGENT_MODEL`.

**Alternatives considered**
Expose all Composio Gmail tools to the model — rejected (ADR-007). Auto-send after draft — rejected; Approve plus the existing send flag. Infer names from headlines — rejected; Assaf chose stated names only.

### ADR-031 Owner intent: same agent, no sub-agents

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** chat — more phrasing understanding HE/EN; asked about transform/plan/execute/sub-agents

**Context**
mia:19 treated any unmatched text of three words or fewer as a greeting. `תבדקי את המייל` and `check my inbox` never reached the owner agent. The prompt also forced `search_memory` before live reads, so even longer paraphrases burned the four-step budget. Assaf asked for query transform, plan-and-execute, and sub-agents.

**Decision**
Keep **one** owner agent (`owner_agent_v2`). No sub-agents, no extra model hop. Greetings/acks/status pings stay an exact-set hello. Every other unmatched phrase, including three-word requests, stays `NOTE` so the loop can plan and call pinned tools. The prompt restates Hebrew/English paraphrases as a tool plan and does live reads (inbox, calendar, leads) before memory. Writes stay on the Python path.

**Consequences**
`תבדקי את המייל` / `can you look at my emails` reach `gmail_inbox`. Cost and safety stay one bounded loop. Shipped image **mia:20**, task **mia:22**. Rollback: image `mia:19` / task `mia:21`.

**Alternatives considered**
Sub-agent swarm or a separate rewrite model — rejected (AGENTS.md: subgraphs over swarms; extra latency and a second untrusted planner). Growing the keyword list for every Hebrew/English paraphrase — rejected; that is what failed. Dumping the Composio catalog — already rejected ADR-007 / ADR-030.


### ADR-032 Owner reads: wider tool loop, live dates, agenda, deterministic query normalization

- **Status:** accepted
- **Date:** 2026-08-25
- **Assaf:** chat — talk to Mia like a capable human operator, no dictionary of magic phrases

**Context**
ADR-030 and ADR-031 fixed routing, and a trace of eleven real owner utterances confirmed it: `תבדקי רגע מה יש לי במייל`, `did Daniel email me?`, `מה יש לי מחר?`, `hey check my inbox` and `היי מה יש לי היום` already reached `run_owner_agent`. Only a bare `היי` was intercepted, correctly. The console still behaved like a keyword bot for five reasons downstream of routing. `max_steps=4` drops tools on its final step, so there were three tool-calling turns; `gmail_search → gmail_read → answer` spent all of them and any multi-source question forced a premature, under-informed answer. `format_inbox_rows` and `format_email_body` captured `InboxRow.timestamp` and never emitted it, so every time-scoped mail question was structurally unanswerable. No tool could list calendar events at all — `calendar_availability` returns free slots in a hard-coded seven-day window — so `מה יש לי מחר?` had no path to an answer. `gmail_search` passed the owner's raw words to Gmail, which AND-matches bare terms, so Hebrew phrasing died on its own function words. And most of the 26 pinned tool descriptions stated purpose only, with three actively wrong: `owner_status` claimed it returns a short hello while its handler dumps the brief, `meeting_brief` documented an 8-character lead id against a 12-character validator, and `search_memory` told the model to check memory first in direct contradiction of ADR-031.

**Decision**
Keep **one** owner agent. No sub-agent, no router model, no rewrite model, no second final-answer model — ADR-031 stands. Bump the prompt to `owner_agent_v3`: delete the literal trigger-keyword list and replace it with semantic guidance about what each data source is for, an internal execution plan that is never printed or narrated, explicit query-construction rules, and a standalone untrusted-content paragraph. Raise the loop to 8 steps, add a 16-call total ceiling, refuse to re-execute an identical `(tool, arguments)` call, and stop offering a tool after repeated empty results; every termination path still grants one tools-free turn so the run ends in prose. Emit absolute and relative dates in both Gmail formatters. Add a read-only `calendar_agenda` tool over the already-pinned `GOOGLECALENDAR_EVENTS_LIST` — no new Composio slug or toolkit version. Add `app/domain/gmail_query.py`, a pure deterministic Hebrew-clitic-aware normalizer that strips conversational filler, converts relative time to one `newer_than:` operator, passes existing Gmail operators through untouched, and never invents an entity or emits a `from:` for a disguised stopword. Rewrite all 27 tool descriptions to state purpose, when to use, input shape, return shape, the follow-up tool, and the limits. When the agent was allowed to run and failed on an unclassified `NOTE`, answer with an honest one-line failure instead of the classifier's "could not classify this" text. Log `steps`, `failed` and `completion` alongside the existing `used`/`tools`.

**Consequences**
`תבדקי רגע אם דניאל ענה לי ותראי גם מה יש לי מחר` is answerable end to end: seven tool-calling turns cover search, read, a second source and the answer. Mail answers can finally be time-scoped. The normalizer is a pure function, not a second conversational layer — it runs only on `gmail_search`, and when a normalized query still returns nothing the model is told so it can retry differently rather than being silently starved. Writes are untouched: `DETERMINISTIC_TASK_TYPES` still gates approvals, drafts, takeover, scope and preference away from the model, a greeting still returns exactly `היי אסף, אני כאן.` without reaching it, and `MIA_GMAIL_SEND` stays false. Three tests changed contract deliberately and were made stronger, including a regression guard asserting the trigger-keyword list cannot return; one unrelated pre-existing date-rot fixture was repaired. `MIA_OWNER_AGENT_MAX_STEPS` reverts the budget without a deploy, and blanking `MIA_OWNER_AGENT_MODEL` still falls back to the `owner_telegram_v2` classifier.

**Alternatives considered**
A rephrase sub-agent or query-rewriting model hop — rejected; ADR-031 already rejected it, and a pure function gets the same recall with no latency, no cost and no second untrusted planner. Growing the prompt's keyword list to cover more paraphrases — rejected; that is the bug being removed, in the prompt as much as in Python. Stripping the deterministic read classifiers so every read goes to the agent — rejected for now; they already act as the fallback when the model is unconfigured or down, and removing them would trade a real availability property for tidiness. Pinning a new Composio calendar slug for the agenda read — unnecessary; `GOOGLECALENDAR_EVENTS_LIST` was already pinned for booking conflict checks. Teaching the model Gmail operator syntax in the prompt instead of normalizing in code — rejected; it is deterministic work that does not belong in the untestable layer.
### ADR-038 Graphs own retrieve and conversation complete

> Renumbered from ADR-033 when the Phase L cleanup was merged: 028–032 belong to
> shipped production decisions and 033 is reserved for the in-flight Gmail-send
> slice on `claude/mia-adr033-wip`.

- **Status:** accepted
- **Date:** 2026-08-25
- **Assaf:** ADOPT (chat: graphs must include the functions Mia needs to be functional)

**Context**
OwnerGraph was `load_owner_context → respond`. ClientGraph was `load_conversation → sales_turn`. Knowledge search, hot handoff, and website finalization (widget close, inactivity, human handoff) ran in HTTP handlers and `mia-due-scan` after `graph.invoke`. Looking at the graphs did not match what Mia did, so those product functions were easy to miss or skip.

**Decision**
1. ClientGraph nodes: `load_conversation` → `retrieve_knowledge` (`knowledge.search` as `GraphName.CLIENT`) → `sales_turn` or skip on `session_end` / `inactivity` → `complete_turn` (hot handoff + website finalize).
2. Website `/end` and due-scan inactivity invoke ClientGraph with `turn_kind`, they do not call the finalization service as a side path.
3. OwnerGraph nodes: `load_owner_context` → `retrieve_owner_knowledge` (`memory.search` + `knowledge.search` as owner) → `respond`. Mail, calendar, leads, and research stay allowlisted tools inside `respond`.
4. Channels stay thin. STT, HMAC, and Sheets mirror stay outside LangGraph. Graph state stays serializable. Website visitors still cannot execute owner capabilities.

**Consequences**
Published AssafWeb facts from `knowledge.search` are passed into sales compose as labelled data. Conversation-complete pings still use the same idempotent finalization service; the graph is the caller. Do not dump the Composio catalog into extra graph nodes.

**Alternatives considered**
Leave retrieve/finalize in HTTP and due-scan — rejected; the graphs would not include the functions. One LangGraph node per Composio slug — rejected (ADR-036).

### ADR-039 Drop Meta ads, LinkedIn post analytics, campaigns, pacing and prelaunch

- **Status:** accepted
- **Date:** 2026-08-26
- **Assaf:** ADOPT (chat: "Drop them — accept the deletion")

**Context**
A Phase L cleanup pass removed five capability modules — `app/integrations/meta_ads.py`, `app/integrations/linkedin_analytics.py`, `app/domain/campaigns.py`, `app/domain/pacing.py`, `app/domain/prelaunch.py` — plus ~4,275 lines of their tests. They were shipped in image `mia:20` and advertised to the owner agent as the `ads_snapshot` tool and the analytics half of `linkedin_snapshot`, but they were **dormant in production**: `MIA_META_ADS_ACCOUNT_ID` and `MIA_LINKEDIN_ACCESS_TOKEN` are both on the live `/health` missing list and the campaign env vars are blank. Meta member analytics also has no Composio tool (ADR-009, ADR-034), so that half was structurally dark.

**Decision**
Drop all five, with their tests and their wiring: the `ANALYTICS` owner-task branch, the prelaunch gate, the `ads_snapshot` tool, the analytics enrichment inside `linkedin_snapshot`, the campaign Sheets mirror tab, the campaign eval in `app/evals/harness.py`, and the freshness/failure-policy pins that named them. `linkedin_snapshot` survives as a **profile-only** read and its description must stop promising post analytics. The four `app/core/capabilities.py` entries move to `SPECIFIED` with an empty port.

**Consequences**
Mia can no longer answer "how is the campaign spend" or report LinkedIn post reach. Meta Ads and LinkedIn analytics leave the product surface until they are deliberately rebuilt behind the capability layer (§35: capability → policy → adapter → allowlist → tests). Roughly 10k lines leave the repo, which is the first real movement toward §39's "meaningfully smaller". Everything is recoverable from git — the pre-deletion state is `c35d005` and the shipped state is `claude/mia-product-feedback-0bfc90` (`7433abf`).

**Explicitly NOT dropped in the same pass**
The cleanup also removed the Composio WhatsApp outbound sender (`ComposioWhatsAppPort`, `MIA_WHATSAPP_SENDER`) and rewrote `.env.example` to cite **ADR-016** as justification for Meta-only outbound — the opposite of what ADR-016 decides, and contrary to production, which runs `MIA_WHATSAPP_SENDER=composio`. That removal was **rejected and reverted**; ADR-016 stands unchanged. A cleanup pass is not the place to reverse an accepted ADR.

**Alternatives considered**
Keep everything — rejected; dormant, unconfigured integrations are exactly the dead weight §36 warns against, and their 4,275 test lines slowed every run. Keep Meta, drop LinkedIn — considered; rejected because Meta ads is the larger surface (813 lines plus campaigns and pacing) and nothing needs it until Assaf actually runs paid campaigns through Mia. Leave them dark but present — rejected; the tools stayed advertised to the model, so Mia offered a capability that could not work.

### ADR-040 Prospect tone awareness in the website sales prompt

- **Status:** accepted
- **Date:** 2026-08-26
- **Assaf:** ADOPT (chat: "keep it, make sure it wire correct and help mia use")

**Context**
The same cleanup pass introduced `app/domain/emotion.py`: 192 lines of deterministic keyword matching over Hebrew and English that infers one of eight prospect tones (frustrated, overwhelmed, stressed, skeptical, excited, tired, worried, uncertain), with carry-forward from the previous turn when the current message is a short non-substantive answer. No LLM call. It was wired into the live customer sales prompt and took the `PROMPT_VERSION` string `v8` — which production's shipped answer-then-ask contract (ADR-028) already owned. Two different `v8` prompts existed, and the frozen prompt hash pinned in `tests/unit/test_ai_runs.py` disagreed on both sides.

**Decision**
Keep the feature and wire it properly. One prompt version, **`sales_reply_v9`**, carries BOTH contracts: production's answer-then-ask over `PUBLISHED ASSAFWEB FACTS`, and a prospect-tone block. `ReplyContext` carries both `knowledge` and `emotional_cues`; `app/graph/orchestrator.py` passes both. Tone changes **delivery only** — when a visitor asks a direct question while sounding frustrated, Mia still answers the question first. The tone block is omitted entirely when no cues are detected, so neutral messages get no invented empathy. The frozen prompt hash is recomputed from the merged prompt, never copied from either side.

**Consequences**
Live Hebrew customer copy changes: Mia acknowledges tone before continuing. Because detection is deterministic keyword matching, it is testable and cheap, but it will miss paraphrases and can false-positive on quoted text — it must never be the basis of a business decision, only of phrasing. `sales_reply_v8` is retired on both lineages; anything citing `v8` is stale.

**Alternatives considered**
Drop it — rejected by Assaf; the behavior is wanted. Ship it behind a default-off flag — rejected; a flag that is never turned on is dead code, and Assaf asked for Mia to actually use it. Keep two prompt versions — impossible; one string, one prompt. Infer tone with a model call — rejected; a second untrusted inference per turn for a phrasing hint, against §7 ("do not create LLM calls for deterministic work").

### ADR-041 The permission principal is derived from the request

- **Status:** accepted
- **Date:** 2026-08-26
- **Assaf:** ADOPT (chat: take the rebuild to 9-10)

**Context**
`app/capabilities/policy.py` enforced the per-graph capability allowlist correctly, but the value it checked was a `graph=GraphName.OWNER` literal typed at each of the 8 call sites. The guarded code chose its own trust level. Owner/client isolation therefore held only because of module topology -- the owner tool registry happened to be reachable only from the Telegram route, behind the numeric allowlist. Any new code path calling an owner helper from a web-triggered path would have inherited owner trust silently, and no test could have detected it.

**Decision**
Trust is established once, at the channel entry point, and passed down as a frozen `Principal` (graph + source + actor_id). `Principal.owner()` is minted in `app/api/owner.py` only after the numeric owner allowlist has matched; `Principal.client()` is minted in `app/api/website.py` and `app/api/inbound.py`. `authorize()` and `execute_capability()` take `principal=`. No module outside `capabilities/types.py` (the constructors) and `capabilities/registry.py` (the allowlists) may name a graph. That invariant is enforced by `tests/unit/test_vnext_principal.py::test_no_module_names_its_own_trust_level`, which walks `app/` with `ast` and fails on any module that names its own trust.

**Consequences**
Adding a capability call means threading the principal you were given, not choosing one. A new web-reachable path cannot silently acquire owner rights: it would have to name a graph, and the guard test fails on that. If a genuine client-to-owner crossing ever appears (client activity needing an owner capability, not merely an owner notification), it must be introduced as a named, reasoned escalation rather than a literal -- one was written for the hot-handoff path and removed again once that path proved not to use a capability at all.

**Alternatives considered**
Keep `graph=` literals and rely on code review -- rejected; that is the status quo that produced a boundary no test could verify. A full auth framework with roles and scopes -- rejected as far more machinery than two trust levels need. Deriving trust inside the policy layer by inspecting a call stack or context var -- rejected; implicit ambient authority is harder to read and to test than an argument that must be passed.

### ADR-042 Authorized Sheets updates and normalized AssafWeb KPI reads

- **Status:** accepted
- **Date:** 2026-08-28
- **Assaf:** ADOPT (chat)

**Context**
Assaf needs Mia to maintain a small set of explicitly authorized Google Sheets and to
answer AssafWeb KPI questions from Search Console and GA4. The prior mirror-only wording
correctly protected Postgres as the system of record, but it incorrectly excluded an
owner-requested, bounded Sheets update. Broad Drive discovery or a model-selected spreadsheet
would create an unacceptable wrong-document write risk.

**Decision**
Mia may read, and may make bounded value updates or appends to, only spreadsheet IDs explicitly
configured or allowlisted by Assaf. There is no arbitrary Drive discovery. Sheets reads are
`READ`; an update or append is a low-risk, policy-controlled operation only after an explicit,
authenticated owner request. Every action is a named capability authorized with the request
`Principal`; kill switch and idempotency apply. This slice excludes create, delete, clear,
formatting, and formula generation.

GSC and GA4 remain API-backed owner reads, never browser automation. Their owner tools normalize
AssafWeb KPIs before answering: GA4 traffic, users, sessions, conversions, and pages; GSC clicks,
impressions, CTR, position, and queries. LinkedIn remains profile-only.

**Consequences**
Sheets is an authorized operational surface, not Mia's internal database: Postgres remains the
system of record and Mia never reads Sheets back as truth for state, decisions, or recovery.
Website visitors do not inherit these capabilities. Implementations must add a named capability,
policy allowance, typed adapter, owner allowlist, and tests before any live Sheets action exists.

**Alternatives considered**
Use Sheets as the system of record or read it back into Mia state — rejected; it loses the
transactional, tenant-safe Postgres boundary. Let the model search Drive or choose by name —
rejected; explicit IDs are the authorization boundary. Use browser automation for Google metrics
— rejected; stable API responses are the required integration surface. Allow general sheet editing
or formula/format generation — deferred; this decision permits only bounded values updates/appends.

### ADR-043 Owner-only on-demand Composio tool breadth

- **Status:** accepted
- **Date:** 2026-08-31
- **Assaf:** ADOPT (chat)

**Context**
Assaf enables OAuth/toolkit guardrails in Composio and wants Owner Mia to use enabled tools
without maintaining a handwritten list. A raw catalog in a model prompt would be slow, stale,
and would let untrusted retrieved text steer a broad provider surface. Treating every provider
tool as a low-risk read would bypass Mia's kill switch, risk policy, approval, idempotency, and
audit contracts.

**Decision**
OwnerGraph exposes three on-demand meta-tools: search only ACTIVE toolkits connected to
`MIA_COMPOSIO_USER_ID`, fetch one selected tool's bounded current input schema, then execute a
locally schema-preflighted read recognized by the conservative classifier. Listings and schemas
are process-cached; unfamiliar actions and oversized schemas fail closed, and the catalog is
never attached to every model call. The request `Principal` gates every meta-tool and ClientGraph
receives none. Python classifies selected slugs: destructive operations are R5 and denied;
send/write/post/marketing/unknown operations do not execute generically. They require a named
workflow with explicit approval, idempotency, and audit before enablement. Kill switch checks run
before catalog operations and provider execution.

**Consequences**
Mia can dynamically use the complete authorized read surface of every active Composio toolkit,
including future toolkits, without prompt-catalog maintenance. Broad side-effect execution remains
intentionally open: OAuth alone cannot bind provider writes to Mia's approval and idempotency
records. Existing named reads and approved bounded Sheets updates remain unchanged.

**Alternatives considered**
Expose every tool definition to the model — rejected for prompt size, drift, and authority
confusion. Let the model label a tool read/write — rejected because model text is not policy.
Generic approval followed by generic execute — rejected because it cannot bind the provider action
to existing named approval, idempotency, and audit records. Proxy execution — rejected because it
bypasses tool schemas and modifiers.

### ADR-044 Repair strict provider contracts and remove lazy-user handoff friction

- **Status:** accepted
- **Date:** 2026-08-31
- **Assaf:** ADOPT (chat: complete the package; design every flow for the lazy user)

**Context**
The first ADR-043 release advertised a dynamic Composio execute function with a nested arbitrary
object while marking every function schema strict. OpenAI validates the whole advertised tool set
before running the owner turn, so this one open object could return the owner-facing provider error
before Sheets or Composio executed. Separately, an authorized Sheets URL was unusable without
manually copying its ID and typing an A1 range. The website exposed WhatsApp only after selected
conversation actions and its footer handoff required two taps. Telegram voice discarded declared
media metadata, named every upload `note.ogg`, and ignored audio sent as a document.

**Decision**
The generic Composio execute meta-tool accepts `arguments_json` as a strict string, decodes it
locally to an object, then applies the unchanged current provider schema validation and Python risk
policy. A recursive test requires every advertised object schema to be closed. Authorized Sheets
reads accept an exact HTTPS Google Sheets URL, extract the ID locally, apply the existing allowlist,
and use only `A1:J20` on the first visible tab when no range was provided; writes still require an
explicit ID, range, and values. Website config returns only its configured `wa.me` destination and
the existing WhatsApp action becomes a one-tap open plus best-effort idempotent handoff notify after
session creation. Phone text from the model is never linked. Telegram preserves declared audio
metadata, derives the actual transcription filename, safely falls back from generic CDN MIME, and
routes audio documents through the same STT path.

**Consequences**
An unrelated malformed dynamic definition can no longer take down every OwnerGraph request.
Assaf can paste an allowlisted Sheet URL without provider-specific syntax, and a website visitor
always has the shortest configured route to WhatsApp. Incognito does not change server-side
Telegram delivery; it can still expose origin, privacy-extension, or blocked-network failures, so
production acceptance must correlate the browser request with server delivery evidence. `gid` is
not silently mapped to a tab in this slice: a link-only preview reads the first visible tab. The
current `gpt-transcribe` model remains configured because its official model page lists the file
transcriptions endpoint; no model is changed without an authenticated probe.

**Alternatives considered**
Mark the dynamic function non-strict — rejected because it would weaken the uniform schema contract.
Allow arbitrary URLs or Drive search — rejected because a link is not authorization. Automatically
link any phone number in generated text — rejected because the number can be hallucinated or
untrusted. Wait for Telegram before opening WhatsApp — rejected because transport latency should not
block the visitor's tap. Change the STT model from documentation ambiguity alone — rejected; the
current official model page explicitly supports `/v1/audio/transcriptions`, and a live authenticated
probe is the remaining proof.

### ADR-045 Complete-work owner actions and Mia-managed CRM workspace

- **Status:** accepted
- **Date:** 2026-08-31
- **Assaf:** ADOPT (chat: make Mia my number-one assistant; Mia owns her spreadsheet)

**Context**
Live owner checks exposed three product gaps. Mia stopped broad audits after partial results and
described an invented provider-call limit. Gmail and Calendar could read but their desired writes
were not complete approval workflows. The configured Google Sheet responded, yet an empty first
tab made Mia treat her own CRM workspace as an unknown external document. LinkedIn and Instagram
also exposed less of their active, useful surface than Assaf authorized. Website WhatsApp-click
requests reached the server, but a historical lead-wide notification claim could hide a new
conversation's Telegram alert.

**Decision**
Broad owner health requests use one bounded aggregate audit tool and return a factual status for
every defined surface; Mia never invents a "two calls" or generic provider-limit explanation.
Gmail draft send and Calendar create/reschedule are exact, hash-bound, expiring, idempotent
Telegram approvals. LinkedIn exposes all active connected reads and may propose schema-validated
non-destructive side effects for exact Telegram approval; delete/remove/revoke and direct-message
tools stay denied. Instagram remains analytics-only and falls back to individual metric requests
when a mixed metric call is unsupported.

`MIA_SHEETS_SPREADSHEET_ID` is Mia's managed CRM workspace. The adapter may add the fixed CRM tabs,
repair their headers, and continuously upsert leads, sources, follow-ups, meetings, deals, content
performance, weekly KPIs, and the Mia activity log. This authority applies to that one configured
spreadsheet only. There is no Drive discovery, spreadsheet create/delete, business-row clearing,
or formula generation. Postgres stays canonical and can rebuild the projections. Website
handoff-notification idempotency is conversation/session scoped, with aggregate delivery outcome
logging and no visitor content.

**Consequences**
Assaf does not maintain Mia's CRM workbook, and a blank or partially structured workbook is
repaired by Mia's background maintenance worker. Visitor requests never wait for this repair.
Mia can complete approved Gmail, Calendar, and LinkedIn actions
without opening a generic write proxy. Provider/API failures are reported per surface and remain
distinguishable from empty data. Returning leads may alert Assaf in a new conversation without
duplicating graph and click alerts inside the same session. The configured workbook remains an
operational view, not a second brain or recovery database.

**Alternatives considered**
Let Sheets become the system of record — rejected because manual edits and provider failures would
corrupt business state. Grant generic provider writes from OAuth alone — rejected because OAuth
does not supply Mia's approval, hash binding, expiry, kill-switch, or idempotency contracts. Ask
Assaf to name every tab/range forever — rejected because this is Mia's own managed workspace. Raise
the model step budget and keep many separate checks — rejected because it remains incomplete and
expensive; the aggregate audit is deterministic and gives one complete result.

### ADR-046 Official Composio destructive slugs stay denied; WhatsApp-move ping is a summary

- **Status:** accepted
- **Date:** 2026-09-01
- **Assaf:** ADOPT (chat)

**Context**
The on-demand Composio classifier was word-based and over-guardrailed bounded Sheets
upsert/update/append as unknown/commercial, while official destructive slugs (there is no
`delete-lead` tool) needed an explicit pin. Website WhatsApp-move already POSTs `/handoff`
and pings Telegram via `sendMessage`; the card dumped recent visitor turns.

**Decision**
Pin official destructive slugs as R5 deny: `GOOGLESHEETS_DELETE_DIMENSION` (delete-lead-row),
Sheets clear/delete-sheet/chart/`EXECUTE_SQL`, `INSTAGRAM_DELETE_COMMENT`,
`INSTAGRAM_DELETE_MESSAGGER_PROFILE`, `LINKEDIN_DELETE_POST`, `LINKEDIN_DELETE_UGC_POST`,
`LINKEDIN_DELETE_LINKED_IN_POST`. Adapter-pinned Sheets writes already in this repo
(`GOOGLESHEETS_UPSERT_ROWS`, `GOOGLESHEETS_VALUES_UPDATE`,
`GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND`) are R1 and stay on named allowlisted
`sheets.update` / `sheets.append` / CRM upsert — generic catalog execute does not run them.
Instagram/LinkedIn reads stay R0; publish/post slugs never auto-fire. LinkedIn non-delete
writes keep the existing one-tap approval path. WhatsApp-click owner ping keeps the paste
line and adds a flag-only summary (workflow, stage, next action, WhatsApp offered) plus
"someone moved to you"; it does not dump the transcript. Delivery remains
`POST https://api.telegram.org/bot<token>/sendMessage` to stored numeric owner chat ids.

**Consequences**
Assaf gets a usable heads-up when a visitor taps WhatsApp. Bounded Sheets writes are not
classified as deletes. Official row/sheet/social deletes stay denied. No new Composio app
is connected. No silent Instagram/LinkedIn publish.

**Alternatives considered**
Invent a `delete-lead` slug — rejected; official catalog has none. Generic-execute Sheets
writes from the catalog — rejected; that would skip the spreadsheet allowlist. Auto-publish
IG/LinkedIn because the tools exist — rejected. New Telegram webhook for the ping — rejected;
outbound `sendMessage` already exists.

### ADR-047 Owner-requested Gmail send stays; unsolicited send and delete-forever stay denied

- **Status:** accepted
- **Date:** 2026-09-01
- **Assaf:** ADOPT (chat via Dude)

**Context**
PR 10 looked like it might deny every Gmail send slug. Assaf's mail policy is not
"never send": Mia must not send on her own, but if the owner asks on Telegram to
write and send mail she must draft and send. Official Composio Gmail send slugs
are `GMAIL_SEND_EMAIL`, `GMAIL_SEND_DRAFT`, `GMAIL_REPLY_TO_THREAD`, and
`GMAIL_FORWARD_MESSAGE`. There is no `GMAIL_SEND`. Delete-forever class is
`GMAIL_DELETE_MESSAGE`, `GMAIL_BATCH_DELETE_MESSAGES`, `GMAIL_DELETE_THREAD`,
plus `GMAIL_DELETE_DRAFT` / `GMAIL_DELETE_FILTER` / `GMAIL_DELETE_LABEL`.
Trash (`GMAIL_MOVE_TO_TRASH`, `GMAIL_MOVE_THREAD_TO_TRASH`) is recoverable.
Google Analytics has no DELETE slug. Google Search on Composio is Search Console;
the only delete is `GOOGLE_SEARCH_CONSOLE_DELETE_SITE`.

**Decision**
Keep the named owner Telegram path: draft (`GMAIL_CREATE_EMAIL_DRAFT`) then
Approve then `GMAIL_SEND_DRAFT` when `MIA_GMAIL_SEND` is on. Pin those proven
adapter slugs. Do not put Gmail send slugs on the destructive denylist. Generic
`composio_execute_tool`, cron, website visitors, and marketing blasts never
auto-fire send slugs. The owner LLM registry still has no `gmail_send` tool —
the model never sends; Python does after the owner asked and approved.
Deny the official Gmail delete-forever class and `GOOGLE_SEARCH_CONSOLE_DELETE_SITE`.
Pin already-wired Gmail reads/draft/send-draft, GA4 reads including
`GOOGLE_ANALYTICS_LIST_ACCOUNT_SUMMARIES`, and existing Search Console reads.
Do not invent 63/69 pins. Do not pin `GMAIL_SEND_EMAIL` until an adapter uses
it. `GOOGLE_ANALYTICS_SEND_EVENTS` stays unpinned and never auto-fires.
Trash is not delete-forever. `GOOGLE_SEARCH_CONSOLE_ADD_SITE` /
`SUBMIT_SITEMAP` are owner writes, not visitor, not auto, not pinned until an
adapter exists. No `GOOGLE_SEARCH` / SERPAPI toolkit. GA
`ARCHIVE_CUSTOM_DIMENSION` is archive, not denied as delete.

**Consequences**
Owner-requested mail on Telegram can send. Visitors and unsolicited paths cannot.
Gmail was not removed; Calendar, Sheets, LinkedIn, GA, and Search Console stay.
No new Composio app. No silent marketing mail.

**Alternatives considered**
Deny every Gmail send slug — rejected; that would break owner-requested send.
Give the LLM a `gmail_send` tool — rejected; the model must not send.
Flip `MIA_GMAIL_SEND` default to true in this PR — deferred; production flag
stays an ops choice. The code path is kept.

