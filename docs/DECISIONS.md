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
| ADR-009 | Composio LinkedIn profile + direct member post analytics | superseded |
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
| ADR-021 | Documentation core set; ManyChat not a v1 runtime channel | accepted |
| ADR-022 | Production live sales test: leave shadow, keep gated writes off | accepted |
| ADR-023 | Model routing: deterministic decisions, model paraphrases | proposed |
| ADR-024 | WhatsApp stays human until official Cloud API inbound | accepted |
| ADR-026 | Mia's brain: long-term memory, knowledge, and an owner tool loop | accepted |
| ADR-027 | Composio owns Google/LinkedIn connections; no extra IDs | accepted |

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
| LinkedIn personal member post analytics | Direct REST (Composio cannot do this job) |
| Meta Ads **read** | Composio |
| Research | Firecrawl now; Apify later behind the same `ResearchPort` |
| ManyChat | Not mounted in v1 (ADR-021). Leftover AWS secret name stays in the box; app ignores it. |
| Composio WhatsApp toolkit | Send pin `WHATSAPP_SEND_MESSAGE` only when sender=`composio`. No inbound trigger. Template send not wired. |

One Instagram sender per conversation (`direct` or `composio`). Never dual-send Graph + Composio. ManyChat is unmounted (ADR-021).

**Consequences**
Env/docs/JSON list Meta tokens for WhatsApp and for Instagram **webhook verify**. Composio key+user covers Gmail/Calendar/Sheets/LinkedIn profile/Meta ads/future IG send+insights. `MIA_LINKEDIN_ACCESS_TOKEN` stays for member analytics. Do not add Apify keys until that adapter exists. Do not rip live Graph IG send until the Composio port is tested.

**Alternatives considered**
Composio for WhatsApp or Instagram inbound — rejected; no usable inbound trigger. Composio for LinkedIn member analytics — rejected; org URN tool only. Default-everything-Composio including catalogs in the model — rejected (ADR-007). WhatsApp send via Composio is ADR-016.

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

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: `/goal` simplify repository without breaking v1)

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

### ADR-027 Composio resolves read resource ids instead of env vars

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** ADOPT (chat: "i want more thing be handle by composio no need env")

**Context**
Composio already holds an authenticated connection per toolkit, but the *resource id* — which Search Console property, which GA4 property, which ad account — was still pasted into `mia/prod` by hand. That is the piece most likely to be wrong or stale, and `owner_integrations` on `/health` reported exactly these as missing. Research against the Composio docs confirmed a zero-argument list action for each: `GOOGLE_SEARCH_CONSOLE_LIST_SITES`, `GOOGLE_ANALYTICS_LIST_ACCOUNT_SUMMARIES`, `METAADS_GET_AD_ACCOUNTS`. Composio publishes only the `{data, error, successful}` envelope; the inner provider payload is undocumented.

**Decision**
Add `app/integrations/composio_discovery.py`. When the env var is blank and `MIA_COMPOSIO_DISCOVERY=true`, resolve the id from Composio, cached once per process. An explicit env var always wins. Parsing is shape-tolerant: documented provider field names first, then a recursive scan for values matching the id pattern, because the inner shape is unverified. `scripts/probe_composio_discovery.py` prints what a live account returns so the parsers can be tightened without exposing the API key.

**Consequences**
Three env vars become optional. Discovery is opt-in and defaults false, so nothing changes for the running deployment and no network call enters the per-request port-construction path unless enabled. Ambiguity is never resolved by guessing: one candidate resolves, several leave the port disabled — except Search Console, where http/https/www/domain variants of the *configured* website collapse to the domain property, since that is one site rather than a real choice. Failures are swallowed to empty so discovery can never break port construction.

**Alternatives considered**
Read the resource id from the Connected Accounts API — rejected; that record carries auth material and scopes, never the user's chosen property, so it can only serve as an `ACTIVE` pre-flight gate (implemented as `connected_toolkits()`). Auto-discover the Sheets spreadsheet — rejected; it is a write target and `GOOGLESHEETS_SEARCH_SPREADSHEETS` matches by name, so a near-miss writes to the wrong document. Drop `MIA_LINKEDIN_ACCESS_TOKEN` — impossible; the Composio LinkedIn toolkit exposes organization stats only, and ADR-015 made member analytics direct REST for exactly that reason. Drop `MIA_FIRECRAWL_API_KEY` by adopting Composio's `FIRECRAWL_*` toolkit — pointless, it takes your own key; the `NO_AUTH` `COMPOSIO_SEARCH_*` toolkit would remove the key but changes crawl capability, so it stays a separate product decision.
