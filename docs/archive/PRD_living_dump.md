> Converted from `Mia_AI_Growth_Sales_Operator_PRD_Build_Bible_v1.1.docx` on 2026-08-21.
> If this file disagrees with the `.docx`, the `.docx` wins until Assaf accepts this markdown.

# MIA

AI Growth & Sales Operator for AssafWeb

Full Product Requirements Document & Engineering Build Bible

| Version | 1.1 — Governed adaptive baseline |
| --- | --- |
| Validated | 21 August 2026 |
| Primary owner | Assaf |
| Primary build environment | Cursor, file-by-file with review gates |
| Core stack | Python + FastAPI + LangGraph + AWS + Composio-first adapters or better options |

Product principle: Mia must feel like a high-performing digital sales operator, not a chatbot.

Build Bible revision: v1.1 — Collaborative Architecture & Better-Way Protocol added.

# How to Use This Document

- This document is the approved product and architecture baseline before implementation begins. It gives the default direction and constraints, but it is not dogma: better approaches may replace a documented choice after explicit discussion, evidence, Assaf approval and an ADR/update.
- The system is intentionally production-oriented: security, idempotency, observability, evals, model cost, permissions and failure recovery are designed before feature code.
- Mia may learn approved preferences and instructions from Assaf, but production Mia never rewrites her own code, graph or prompts.
- The development loop for prompts, routing and graph changes runs locally in the Mia Graph Lab and is promoted only after evaluation and human review.
- Where external provider functionality is beta, changing, or restricted by channel policy, the PRD uses an adapter boundary and feature flag rather than making the core product depend on a fragile provider behavior.
## Collaborative Build & Better-Way Protocol

The implementation agent (Cursor/Codex or any future builder) is expected to collaborate with Assaf like a senior engineering partner. The Bible defines the current vision, constraints and default architecture; it must guide the build without preventing better engineering decisions discovered during implementation.

- Conversation-first collaboration. For significant product, architecture, security, data, model, integration or workflow decisions, the builder explains what it understood, discusses the decision with Assaf, and keeps the interaction conversational rather than silently executing a large design change.
- Challenge the baseline when justified. If the builder finds a safer, simpler, faster, cheaper, more maintainable or higher-performing approach, it should say so clearly instead of implementing an inferior documented approach.
- No silent architecture drift. The builder may not deviate from an approved Bible decision in a material way without first surfacing the alternative and receiving approval.
- Required better-way proposal. A proposed deviation must state: current Bible direction; proposed alternative; why it appears better; evidence or technical basis; benefits; tradeoffs; security/privacy implications; performance and reliability implications; cost implications; migration/lock-in implications; files/components affected; and a clear recommendation.
- Decision gate. Assaf chooses KEEP BASELINE, ADOPT ALTERNATIVE, TEST BOTH, or DEFER. Until that decision is made, material implementation follows the last approved direction.
- Test before ideology. When two approaches are plausibly good and the choice depends on real behavior (for example model routing, AWS runtime, provider adapter, prompt, graph path or retrieval strategy), prefer a small benchmark, spike or eval over argument by preference.
- Minor implementation freedom. The builder may make local code-quality improvements that do not change product behavior, security boundaries, data contracts, external interfaces, cost model or architecture. These should still be visible in the diff/review.
- Capture accepted evolution. Any approved material deviation becomes an ADR in docs/DECISIONS.md and, when it changes the product/architecture contract, updates this Bible to the next version. The Bible is therefore a living governed baseline, not an immutable historical artifact.
- Preserve Assaf control. The builder must not use “better architecture” as justification for scope expansion. New features, new providers, new autonomous permissions or meaningful infrastructure complexity still require explicit approval.
- Prefer understanding over obedience. If an instruction from Assaf conflicts with an established safety/security constraint or appears likely to damage the product, the builder should explain the concern and propose a safer path instead of blindly following it.
### Better-Way Discussion Template

1. What the Bible currently says
1. What I discovered during implementation
1. Proposed alternative
1. Why it may be better
1. Security / privacy impact
1. Reliability / performance impact
1. Cost / vendor-lock-in impact
1. Migration and affected files
1. How we can test it
1. My recommendation
1. Assaf decision: KEEP / ADOPT / TEST BOTH / DEFER
## Research validation note

The architecture was checked against current vendor documentation and current AssafWeb content through 21 August 2026. Key provider decisions are referenced as [R#] and the full source list appears in the final appendix. Provider pricing, quotas, API policies, permissions and beta features are dynamic configuration and must be revalidated immediately before production rollout.

# Document Map

1. Executive Summary

2. Product Definition, Goals and Non-Goals

3. Users and Operating Modes

4. Product Principles and Boundaries

5. End-to-End Customer and Owner Journeys

6. Channel Architecture

7. AssafWeb Website Funnel and Minimization Plan

8. Lead Identity, Timeline and Attribution

9. Mia Sales Engine

10. Sales State, Discovery and Qualification

11. Objections, Reframing and Closing

12. Follow-Up, Meetings and Commitments

13. Understanding Check and Learning From Assaf

14. Memory and Knowledge Architecture

15. RAG and Source-of-Truth Policy

16. Instagram and ManyChat Architecture

17. WhatsApp and Voice-Note Input

18. Gmail and Google Calendar

19. Google Sheets Business Control Layer

20. Meta Ads and Campaign Intelligence

21. Research, Firecrawl, Apify and Browser

21A. LinkedIn Professional Intelligence Channel

22. Make Integration Policy

23. Composio Tool Architecture

23.5 MCP Interoperability and Tool Trust

24. Model Router and Cost/Quality Policy

25. LangGraph Architecture

26. Graph State, Nodes, Edges and Subgraphs

27. Persistence, Interrupts and Durable Execution

28. Mia Graph Lab and Graph Engineering

29. AWS Production Architecture

30. API and Endpoint Contracts

31. Eventing, Queues, Idempotency and Reconciliation

32. Data Model

33. Permissions and Approval Matrix

34. Security and Threat Model

35. Privacy, Data Retention and Auditability

36. Observability and Operations

37. Reliability and Performance SLOs

38. Test Strategy

39. AI Evaluation and Sales Simulation

40. Model Benchmarking and Cost Controls

41. Dev/Test/Prod, CI/CD and Release Gates

42. Demo Mode

43. KPIs and Success Metrics

44. Implementation Roadmap

45. Repository Structure and Cursor Build Protocol

46. Definition of Done

47. Future Roadmap

48. Rejection Review and Resolved Architecture Risks

Appendix A. Source Register

# 1. Executive Summary

Mia is AssafWeb's AI Growth & Sales Operator: a production-grade agent designed to turn attention into qualified conversations, meetings and measurable pipeline. Mia operates primarily on the AssafWeb website (customer sales), Telegram (private owner control, ADR-017), WhatsApp as a **verified website-handoff continuation only**, Gmail (read/draft; send approval-gated), Google Calendar, LinkedIn intelligence and Meta campaign data. Instagram is not a v1 autonomous sales inbox. Assaf talks to Mia in Telegram (text and voice-note input; no TTS).

Mia does not create Assaf's social content, does not speak with a synthetic voice, does not change her own code, and does not autonomously change advertising budgets or make high-risk commercial commitments. Her differentiator is sales intelligence: understanding how a prospect's business works, discovering workflow friction naturally, helping the prospect understand the consequence of that friction, proposing a relevant automation hypothesis, qualifying fit, and guiding the right next step.

The system is channel-agnostic at the core. LangGraph owns state and orchestration; AWS owns secure ingress, eventing and production runtime boundaries; Composio is the primary tool supplier behind typed adapters **for the ADR-015 jobs** (Gmail, Calendar, Sheets, LinkedIn profile, Meta Ads read, future Instagram send/insights, WhatsApp **send** when `MIA_WHATSAPP_SENDER=composio`); WhatsApp and Instagram **inbound** stay Meta HMAC webhooks (ADR-016: Composio has no WhatsApp incoming-message trigger); ManyChat is an optional Instagram engagement/trigger sidecar (never a second sender); Make is an optional non-critical automation sidecar; Postgres is the system of record; Google Sheets is a human-readable operating mirror.

## North-star outcome

Increase qualified meetings and deals attributable to AssafWeb while reducing manual lead handling time, with a full audit trail of what Mia knew, asked, recommended, executed and cost.

## Feature wiring status (living)

Runtime source of truth: `app/core/capabilities.py`. Update this table in the same turn a capability moves. **Wired** = typed port in code. **Alive** = test proves the path runs. Auth-owner matrix (who authenticates which job): `docs/CAPABILITY_OWNERSHIP.md` — docs only; no `app/auth` service.

| Capability | PRD | Status |
| --- | --- | --- |
| HTTP API / health | §30 | alive (`GET /health` operator diagnostic including `sales_llm`, `sales_gemini`, `composio`, `composio_webhook`, `postgres`, `public_https`, `website_chat`, `telegram_owner`, `whatsapp_provider`, `whatsapp_connected`, `whatsapp_ingest`, `whatsapp_send`, `whatsapp_owner`, `email_read`, `email_send_policy`, `automation_mode`, and `ops` (`pending_approvals`, `human_takeover`, `failed_sends`, `integration_failures`) — no secrets, DSNs, or ids; `GET /health/live` process; `GET /health/ready` `SELECT 1` plus mapped-column check — 503 `not_ready`; `MIA_ENV=prod` unmounts `/docs` `/redoc` `/openapi.json`) |
| Config / env | §41 | alive (laptop `.env` from `.env.example` — ADR-015 adapter sections; production SECRET keys in AWS Secrets Manager `mia/prod`, ECS injects `MIA_*` — ADR-014; `.env` gitignored; never copy `.env` onto Fargate; R4/R5 not overridable; named write flags default false; auth-owner matrix in `docs/CAPABILITY_OWNERSHIP.md`) |
| Capability registry | this table | alive |
| Observability / log redaction | §34, §36 | alive (logs + `ai_runs`/`tool_runs`; go-live `docs/PRODUCTION_BUILD.md`; operator runbook `docs/RUNBOOK.md`; no CloudWatch/alerts) |
| Risk policy | §33, §34 | alive (R4 approval, R5 deny, kill switch env) |
| Canonical events | §8, §31 | alive (LEAD_CREATED on new lead open; ATTRIBUTION on website session create when sanitized UTM/landing/referrer present and on Instagram prospect inbound when sanitized organic/referral keys present (`{lead_id}:attribution`, first write wins; IG keys `ig_content_id`, `ig_trigger_source`, `ig_ref`, `meta_ad_id`, `meta_post_id`, `meta_campaign_id` only; no media URLs); BEHAVIOR on website funnel (allowlisted client kinds via `POST /v1/website/sessions/{session_id}/events`; server `mia_opened` on session create, `conversation_started` on first message, `whatsapp_handoff_offered` when NBA is `offer_whatsapp`, `whatsapp_handoff` on token issue; sanitized path/section/cta; no PII; idempotent per session+kind key); QUALIFICATION_UPDATED when SalesState changes after graph extract; MEETING_OFFERED and HANDOFF when graph selects those next actions; BUSINESS_VALUE count events on fit→good (`qualified`), graph handoff (`handoff`), verified meeting booked (`booked`), and follow-up recovered (`recovered`) — `{lead_id}:value:{kind}`; payload `kind`+`estimated_value_ils` always `""`; not in weekly KPI counts; MEETING_BRIEF when pre-meeting snapshot persists on `offer_meeting` (`{lead_id}:brief:offer_meeting`; allowlisted SalesState snapshot only; first write wins); MEETING_DEBRIEF when owner post-meeting debrief persists (`{lead_id}:debrief`; payload `outcome`+`next_step` only; first write wins); APPROVAL_REQUIRED when R3 handoff persists pending approval (`{lead_id}:approval:proposal_handoff`; payload `action`+`risk`+`decision` only; first write wins); CAMPAIGN_RECOMMENDATION when account-level Meta analysis persists on owner analytics ack (`meta:campaign:recommendation`; payload `kind`+`anomaly` only; first write wins); FOLLOW_UP when prospect follow-up row is created, cancelled, or recovered (`{lead_id}:followup:meeting_offered` / `:cancelled` / `:recovered`; payload `status`+`reason` only); TOOL_RESULT after calendar/sheets/meta/research/linkedin_profile/linkedin_analytics enrich and after successful WhatsApp STT (`voice_transcribe`; `{inbound_id}:tool:{tool}`; allowlisted payload `tool`/`status`/`result_count` only; no transcript text, PII, URLs, spend, or slot times); MESSAGE_IN on inbound accept and website message; MESSAGE_OUT after successful inbound send or finalized website reply; **`correlation_id`** on `CanonicalEvent` + `canonical_events` row (sanitized `[a-zA-Z0-9_-]{1,64}`; prospect/website reuse sales `run_id`; owner `cor_*` per claimed item; graph events stamped from inbound `run_id`; not in payload); **`payload_version`** `"1"` stamped on persist (`canonical_events.payload_version`; not in payload; no `business_id` tenant); Postgres SoR; idempotent via `(provider, provider_event_id)`; OUT paired as `{inbound_id}:out`; no queue/SQS this slice) |
| Identity | §8 | alive (channel identities in Postgres; in-memory `IdentityIndex` rejects weak merge; verified link history **alive** — `identity_links` row on successful handoff consume; reason `handoff_token`; R1 `identity_link_persist`; first write wins; no reverse API this slice) |
| SalesState | §10 | alive |
| Sales reply | §9 | alive (OpenAI Chat Completions when `MIA_OPENAI_API_KEY` + `MIA_SALES_MODEL` or `MIA_SALES_FALLBACK_MODEL` set; primary HTTP/empty/lint failure retries OpenAI fallback once, then Gemini AI Studio OpenAI-compat when `MIA_GEMINI_API_KEY` + `MIA_SALES_GEMINI_MODEL` set, then canned; deterministic Human Voice linter on LLM paraphrase only — fail→canned; canned Hebrew customer replies per §9.3 proven by tests; owner/scorecard/tool payloads not linted; `QUANTIFY` asks frequency only — cost/value on `QUALIFY` metric; bilingual extract tokens; website buying-intent tokens (`לבנות אתר` / `לפתוח עסק` / leads-as-goal) raise pain to P2 so NBA can `offer_whatsapp`; `בי תודה` stops; `select_next_action` in code; prompt `sales_reply_v2` pins AssafWeb facts and forbids typical-day questions for pre-launch; no instruction activation) |
| Humanity linter | §9 / playbook §25 | alive (deterministic checks 3/4/6/9 in `app/domain/humanity.py` — AI phrases, typography, question count, unsupported-claim block; typography is em/en dash, backslash, decorative ` / ` and ` - `, double hyphen `--`, and letters spaced with `-` / `--` / `'` / `\\`; not `//`; wired on `OpenAISalesReplyPort` paraphrase; fail→canned; no LLM rewrite; owner path not linted) |
| LangGraph orchestrator | §25–27 | alive (policy node, no LLM) |
| Website Mia | §7, §30 | alive (session + optional UTM/landing/referrer attribution on create; behavior tracking **alive** — widget posts sanitized `page_viewed`, `section_viewed`, `cta_click`, `form_started`, and `form_abandoned` (queued until first launcher open creates session; no innerText/PII); client POST for allowlisted funnel kinds; server emits `mia_opened`/`conversation_started`/`whatsapp_handoff_offered`/`whatsapp_handoff`; website NBA may select `offer_whatsapp` after workflow + identifiable friction (P2+) **or buying intent** (pre-launch site/leads) without finishing MEDDPICC — not owner `HANDOFF`; Ask Mia widget chrome matches AssafWeb navy/ice (`#0c2440` launcher + header, ice `מ` monogram, `#F8FBFF` panel, `#03101f` text), opens upward, resumes same-browser `web_*` session from `localStorage`; GA4/GSC/SEO audit read ports **alive** (`Ga4Port`, `SearchConsolePort`, `SeoAuditPort`; Disabled when env empty; owner WhatsApp SEO classify; never Measurement Protocol or GSC writes; `website_edit` approval persist-only); see `docs/WEBSITE_SEO_GAP_REPORT.md`; Mia never autonomously rewrites AssafWeb) |
| WhatsApp + voice input | §17 / ADR-017 | alive (inbound Meta HMAC; production `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=true` — unknown/personal/`DO_NOT_AUTOMATE` contacts get no reply, no lead, no follow-up, no STT; verified website handoff → `MIA_BUSINESS` + one-time intro; outbound Composio `WHATSAPP_SEND_MESSAGE` when `MIA_WHATSAPP_SENDER=composio` — never both with Graph, ADR-016; Graph access token still empty so WhatsApp media STT is not live; owner text+audio task router classify+persist; no execute; no TTS) |
| Telegram owner | ADR-017 | alive (`POST /v1/telegram/webhook`; numeric `MIA_TELEGRAM_OWNER_USER_IDS` allowlist; secret header; unauthorized → HTTP 200 ignored; outbound `sendMessage` uses chat `message_id` as `reply_to_message_id` (never `update_id`); voice download+STT then existing owner brain; unclassified **text** (empty `matched_types`) is promoted to `owner_status` — Hebrew operator digest (today’s counts, pending approvals, hot leads, command menu), not the Understanding Check loop; audio and 2+ keyword matches stay Understanding Check; no sales graph, no execute; hot-lead notify; takeover/resume/scope commands; capability `telegram` ALIVE) |
| voice_stt | §17.3 | alive (GPT Transcribe port; primary fail retries fallback model once; in-memory media download; TOOL_RESULT `voice_transcribe` after successful save — payload tool/status/result_count only; no TTS) |
| Instagram + ManyChat | §16 | alive (IG inbound Meta HMAC webhook; DMs via Graph when `MIA_INSTAGRAM_SENDER=direct`; Composio `INSTAGRAM_SEND_TEXT_MESSAGE` when sender=`composio` and Composio key+user set — default stays `direct`; toolkit `20260819_00`; organic/referral attribution on inbound prospect path including referral-only webhooks without `message.mid` via synthetic `igref:{sender}:{stable}`; organic content insights read on owner analytics ack via Graph when sender=`direct` and tokens set, else Composio `GET_IG_USER_MEDIA` + `GET_IG_MEDIA_INSIGHTS` (fields `id,media_type` only); Postgres `content_insights` + Sheets tab `07 Content Performance`; no media URLs/captions; ManyChat sidecar **alive** — External Request ingest, bearer auth, requires `event_id`, subscriber+conversation ids on parsed item, sanitized ad/campaign/post/content/ref/trigger attribution via same IG sanitizer (names/media dropped), no Composio ManyChat toolkit; Dynamic Block send only when sender=manychat; no dual-send; no publish; no cold DM) |
| Gmail | §18 | alive (Composio `GMAIL_NEW_GMAIL_MESSAGE` trigger ingest; toolkit `20260817_00`; optional `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID` hydrate when trigger body empty; canonical `conversation_id`=Gmail `thread_id`; lead identity=sender email; no send; no delete; body is data) |
| Gmail thread summary | §18.1 | alive (persist-only `gmail_thread_summaries`; owner WhatsApp `summarize email` / `סיכום מייל` + `lead_*` or `thread:<id>` loads Gmail `message_in` from Postgres; typed `ThreadSummaryPort` summarizes with email as data not instructions; Hebrew ack; R1 `gmail_summary_persist`; kill switch format-only; demo skips persist; no send/delete/MIME/sales graph) |
| Calendar | §18.2 | alive (free/busy read via `CalendarPort`; **create and reschedule alive by fake** via separate `CalendarBookingPort`; exact numbered confirmation only; ADR-012 Sun–Thu 09:00–17:00 `Asia/Jerusalem`, 24h notice; R2 `calendar_create` and `calendar_reschedule` AUTO only in approved scope; create idempotency + verify; ADR-013 reschedule exact event GET, conflict recheck, narrow PATCH, mandatory GET verification; Composio pins `GOOGLECALENDAR_FIND_FREE_SLOTS`, `GOOGLECALENDAR_EVENTS_LIST`, `GOOGLECALENDAR_CREATE_EVENT`, `GOOGLECALENDAR_EVENTS_GET`, and `GOOGLECALENDAR_PATCH_EVENT` on toolkit `20260812_00`; cancellation is a local request for Assaf with no provider mutation; provider delete remains R5 denied; real staging OAuth CREATE/PATCH/GET remains operator acceptance) |
| Google Sheets mirror | §19 | alive (`01 Leads` + `02 Campaign Budget` + `03 Campaign Performance` + `04 Meetings` + `05 Deals` + `06 Lead Sources` + `07 Content Performance` + `08 Follow-ups` + `09 Weekly KPI` + `10 Mia Activity` upsert via `SheetsPort`; production workbook created 2026-08-22 titled `Mia — AssafWeb operating mirror` and `MIA_SHEETS_SPREADSHEET_ID` stored in `mia/prod`; content row on owner analytics insights path from Postgres `content_insights`; campaign budget/performance from Postgres `campaign_pacing`/`campaign_performance` when `MIA_CAMPAIGN_MONTHLY_BUDGET` set (never infer budget from spend); meeting row after graph mirrors offered with empty time/event fields, or booked/cancellation-requested with existing time and event ID; never Meet link; deal row after graph when a deal exists; source row on website session create from ATTRIBUTION; weekly KPI from canonical events + pending follow-up snapshot; live Composio execute when configured; pin `GOOGLESHEETS_UPSERT_ROWS` toolkit `20260813_00`; Postgres SoR; no sheet→CRM read; no debrief transcript mirror; no create-spreadsheet from the port) |
| Meta Ads intelligence | §20 | alive (insights read on owner analytics ack; live Composio HTTP when `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` + `MIA_META_ADS_ACCOUNT_ID` set; pin `METAADS_GET_INSIGHTS` toolkit `20260731_00`; Assaf-owned credentials; no writes; missing metrics not zero-filled) |
| Content performance | §16 / §19 | alive (Instagram organic insights read on owner analytics ack via typed `InstagramInsightsPort` — Graph when sender=`direct` and `MIA_INSTAGRAM_ACCESS_TOKEN` + `MIA_INSTAGRAM_ACCOUNT_ID` set; Composio when sender=`composio` or Graph tokens empty and Composio key+user set; `DisabledInstagramInsightsPort` otherwise; pins `INSTAGRAM_GET_IG_USER_MEDIA` + `INSTAGRAM_GET_IG_MEDIA_INSIGHTS` toolkit `20260819_00`; recent media + per-media insights only; Postgres `content_insights` with lead_signals from canonical ATTRIBUTION `ig_content_id`; Sheets tab `07 Content Performance` mirror on owner path; R0 read + R1 persist; no publish/comments/media URLs) |
| Content ideas | §2.2 | alive (persist-only `content_ideas`; owner WhatsApp `content ideas` / `רעיונות לתוכן` ranks allowlisted kinds from Postgres `content_insights` by lead_signals then views; one row per local `idea_date`; Hebrew ideas-only ack; R1 `content_idea_persist`; kill switch format-only; demo skips persist; no LLM/captions/publish/Graph/execute/Sheets) |
| Campaign analysis | §20.2 | alive (deterministic `analyze_insights` on owner analytics ack `last_7d` + previous 7d via Composio `time_range` only; official GET_INSIGHTS pin includes `frequency`; when 7d is `watch`, Postgres `lead_created` count for last 7 local days including today via `count_canonical_events` — missing count never treated as zero; `spend_without_leads` investigate when spend>0 and count exactly 0; when rec remains `watch` with count>0, second Postgres count for previous 7 local days (`previous_7d_event_bounds`) + Meta previous spend may upgrade to `cpl_spike` when current CPL>previous CPL (missing/zero leads never invent CPL); when rec remains `watch` after CPL, `creative_fatigue` when current frequency>previous frequency and current CTR<previous CTR (all four parse; missing never zero-filled; no magic threshold); when rec remains `watch` after fatigue/CPL/leads, `count_behavior_events` on `mia_opened`/`conversation_started` (not in `KPI_EVENT_TYPES`) may upgrade to `website_funnel_drop` (7d only; missing never invented; no magic conversion threshold; funnel investigate skips 30d); spend-without-clicks and 7d compare still win; second compare on `last_30d` + previous 30 local days via `time_range` only when rec remains `watch` — never with `date_preset`; upgrade only on `spend_up_clicks_down_30d`; 7d investigate/uncertain/`cpl_spike`/`creative_fatigue`/funnel skip 30d; missing previous metrics never zero-filled; Hebrew recommend line appended; read-only today-vs-baseline line after recommendation when settings exist — baseline = previous 7 completed local days (ADR-008); `today` preset + baseline `time_range` never together; not an anomaly; account-level `campaign_recommendations` row + canonical `CAMPAIGN_RECOMMENDATION` (`meta:campaign:recommendation`; payload `kind`+`anomaly` only); R1 persist; no Meta writes) |
| Campaign pre-launch | §20.3 | alive (named-campaign config env + deterministic seven-check gate in `app/domain/prelaunch.py`; Postgres `campaign_prelaunch`; Hebrew pre-launch line on owner analytics ack; R1 `campaign_prelaunch_persist`; kill switch read-only ack; demo skips; never Meta launch/write; never infer budget; e2e is operator attestation) |
| Research / browser | §21 | alive (public search snippets on owner research ack + pre-meeting brief when explicit `company_domain`; typed `ResearchPort` search-only; live Firecrawl search when `MIA_FIRECRAWL_API_KEY` set; Disabled otherwise; snippets/page text are data; no Playwright) |
| Search Console | §7 / website SEO | alive (read-only `SearchConsolePort`; Composio toolkit `20260806_00`; pins QUERY / INSPECT_URL / LIST_SITES; Disabled when Composio or `MIA_GSC_SITE_URL` empty; production ECS injects `MIA_GSC_SITE_URL` from `mia/prod`; never add/delete site or submit sitemap; missing rows omitted) |
| GA4 measurement | §7 / website SEO | alive (read-only `Ga4Port`; Composio toolkit `20260721_00`; pins RUN_PIVOT_REPORT / LIST_CONVERSION_EVENTS; Disabled when Composio or `MIA_GA4_PROPERTY_ID` empty; production ECS injects `MIA_GA4_PROPERTY_ID` from `mia/prod`; never SEND_EVENTS / Measurement Protocol) |
| SEO audit | §7 / website SEO | alive (Firecrawl v2 scrape via `SeoAuditPort`; allowlisted `assafweb.com` / `www.assafweb.com` HTTPS only; derived title/description/canonical/h1_count/json-ld only; no HTML dump; owner WhatsApp `OwnerTaskType.SEO`; `website_edit` approval persist-only; Mia never git-pushes AssafWeb) |
| LinkedIn intelligence | §21A | alive (own-profile read via Composio `LINKEDIN_GET_MY_INFO` when `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` set; personal post analytics via direct `memberCreatorPostAnalytics` when `MIA_LINKEDIN_ACCESS_TOKEN` set; previous 30 completed local days; six metrics only; R0 read; no post content/raw IDs/writes; toolkit pin `20260724_00` profile only; API version `202608` analytics; Composio share stats not used) |
| Owner learning | §13 | alive (propose-only: `preference` / `behavior_rule` / `correction` classified on propose; `status=proposed`; never active; not appended to prompts; `fact` not this slice) |
| FDE owner correction | FDE operating layer | alive (persist-only `owner_corrections` via `app/domain/feedback.py`; R1 `owner_correction_persist`; scope `this_turn`/`remember` from phrase classify; status always `logged`; first write wins; owner PREFERENCE inbound with `InstructionKind.CORRECTION` also persists correction while still proposing instruction; no remember-ask; no activation; no prompt rewrite) |
| FDE business value | FDE operating layer | alive (persist-only `business_value` canonical events via `app/domain/value.py`; kinds `qualified`/`booked`/`recovered`/`handoff`; idempotency `{lead_id}:value:{kind}`; payload `kind`+`estimated_value_ils` always `""`; R1 `business_value_persist`; wired on fit→good, handoff, meeting booked, follow-up recovered; `count_business_value` requires lead_id; **not** in `COUNTABLE_EVENT_TYPES`; no deal won/minutes/ILS inference) |
| Owner daily brief | §2.2 / §17 | alive (persist-only `owner_briefs`; owner WhatsApp `daily_brief` / `סיכום יומי` computes today's local-calendar counts from canonical events + pending follow-ups due today, including `meeting_booked` and `meeting_cancellation_requested` counts (no PII; not in Sheets tab 09); pacing/prelaunch status labels only; Hebrew scorecard ack; R1 `owner_brief_persist`; kill switch read-only ack; demo skips persist and scorecard; no execute/send/Meta/Sheets/LLM) |
| Owner weekly brief | §2.2 / §17 | alive (persist-only `owner_weeklies`; owner WhatsApp `weekly_brief` / `סיכום שבועי` computes ISO-week scorecard from canonical events + pending follow-ups snapshot via `compute_weekly_kpi`, plus booked-meeting and cancellation-request counts from canonical events (no PII; not in Sheets tab 09); pacing/prelaunch status labels only; Hebrew scorecard ack; R1 `owner_weekly_persist`; kill switch format-only; demo skips persist and apply returns None; no execute/send/Sheets/LLM) |
| Owner lead review | §2 / §17 | alive (persist-only `lead_reviews`; owner WhatsApp review phrases + `lead_*` id return sanitized SalesState/pipeline snapshot — no phone/email/names/message text; one row per lead upsert; Hebrew ack; R1 `lead_review_persist`; kill switch format-only; demo skips persist and apply returns None; no execute/send/sales graph/LLM) |
| Owner calendar availability | §17 / §18.2 | alive (read-only owner WhatsApp calendar phrases; typed `CalendarPort` + ADR-012 `carve_policy_slots`; numbered Hebrew slot ack via `apply_owner_calendar`; R0 `calendar_read`; TOOL_RESULT `calendar_find_free_slots`; kill switch generic logged ack + denied; demo skips port; no create/list busy events; task stays `logged`; no due_at) |
| Owner meeting notify | §12.2 / §26.2 | alive (persist-only `owner_notifications`; unique `(kind, lead_id)`; kinds `meeting_booked`, `meeting_rescheduled`, `meeting_cancellation_requested`; upsert on verified booking, verified reschedule, and first cancellation request; owner WhatsApp exclusive pull phrases (`booked meetings`, `מה נקבע`, etc.); Hebrew ack with kind-specific first line + `lead_id` + `מועד` only via `apply_owner_notify`; R1 `owner_notify_persist` / `owner_notify_deliver`; kill switch skips persist and format-only on pull; demo skips persist and apply returns None; no proactive send/MessagePort; task stays `logged`; no due_at) |
| Graph Lab | §28 | alive (local deterministic replay via `app/evals`; `sales_v1` 50 one-shot NBA+reply; `buyers_v1` multi-turn extract+mark+NBA+reply; `website_handoff_v1` shoe-store website extract+NBA(channel=website)+mark+reply — progressive discovery without looping the opening question, timely `offer_whatsapp`; `routing_v1` 20 isolated Hebrew+English owner classify cases scored via `run_routing_eval` + `classify_owner_task` (no NBA/reply/judge); `extract_v1` 30 isolated Hebrew+English extract cases scored via `run_extract_eval` + `_sales_field_matches` (no NBA/reply/judge); `objection_v1` 20 Hebrew+English extract→NBA→reply cases scored via `run_objection_eval` + `lint_customer_reply` (no judge); `calendar_v1` 20 ADR-012 `carve_policy_slots` cases scored via `run_calendar_eval` (no NBA/reply/judge); `campaign_v1` 20 `analyze_insights` + `format_recommendation_line` cases scored via `run_campaign_eval` (no NBA/reply/judge); `safety_v1` 20 adversarial sales extract→NBA→reply + snippet sanitizer cases scored via `run_safety_eval` (no judge); `writing_v1` Hebrew+English writing suite (8 categories × 2 languages + anti-pattern lint fails; follow-up draft + owner brief); `mia_sales_gold` (20 synthetic Bible-shaped cases + hidden-truth scorer: must ask workflow / must not pitch / must not invent ROI); sales/buyer/gold replies lint-gated via `lint_customer_reply`; 12 §39.1 personas; §39.3 weighted Sales Quality Score from exact-match turn results; no self-edit; no LangSmith this slice) |
| Demo mode | §42 | alive (`MIA_DEMO_MODE`; fail-closed in prod; synthetic UTMs on website sessions; Sheets skip on website + inbound; `/v1/demo/status` + `/v1/demo/scripted`; widget `(דמו)` label; free-form = existing widget) |
| Prospect follow-up | §12.1 | alive (persist-only `lead_follow_ups`; one row per lead; create on `offer_meeting`; cancel on `stop`/`disqualify`; verified booking or crash recovery cancels pending meeting-offered follow-up with reason `meeting_booked`; reschedule/cancellation request do not reopen it; send-readiness denies booked or cancellation-requested meetings even when a stale pending row exists; due tomorrow Asia/Jerusalem; frequency cap; due scan persist-only persists humanity-linted Hebrew draft on row when `send_ready` (never send; draft not in Sheets/JSON/logs); still no send) |
| Due scan worker | §12.1, §12.4 | alive (local CLI `mia-due-scan` / `python -m app.workers.due_scan`; calls `scan_due_follow_ups` + `scan_due_owner_tasks`; date-due + spend-threshold owner scans; budget from `MIA_CAMPAIGN_MONTHLY_BUDGET`, spend from `campaign_pacing.spend`; persists send_ready/due_ready; JSON counts only; no HTTP; no send; no execute; not AWS/SQS) |
| Reconciliation | §31.3 | alive (local CLI `mia-reconcile` / `python -m app.workers.reconcile`; flag-only persist via `evaluate_reconciliation` + `apply_reconciliation_policy`; checks stale received webhooks, sent-without-OUT, expired unconsumed handoff tokens; upserts `reconciliation_findings`; resolved subjects close on a later scan (`open=false`) without provider repair; R1 `reconciliation_persist`; kill switch + demo skip persist; default JSON counts only; `--inspect` lists open findings kind+subject_key plus sanitized webhook `channel`/`envelope_kind` (cap 50, no replay); never repair/send/consume/Sheets read/Meta/calendar writes; not AWS/EventBridge) |
| Schema migrate | §41 | alive (CLI `mia-migrate` / `app.db.migrate`; prod worker `init_db()` then SQL so first-boot RDS gets mapped tables; `migrations/*.sql` in filename order; non-editable image reads `/app/migrations` not site-packages; `schema_migrations` tracking; sqlite skips `20260821_approval_campaign_resource.sql` and does not record it; Postgres savepoints so duplicate-column ALTERs do not abort the transaction; SQL files must parse on Postgres — no `AUTOINCREMENT`, no `;` inside `--` comments; duplicate column/table treated as applied; JSON `applied`/`skipped`/`already`/`failed`; kill switch does not block; never send) |
| Conversation kill | §34.2 | alive (persist `leads.conversation_killed` on graph `stop` on website + prospect inbound; any other NBA clears for recovery; R1 `conversation_kill_persist` with kill_switch=False in assert; follow-up send-readiness denies `conversation_killed`; not in LangGraph state; owner/Graph Lab excluded) |
| Meeting brief | §12.2 | alive (persist-only `meeting_briefs`; one row per lead; upsert on graph `offer_meeting`; sanitized SalesState snapshot + owner-only `company_domain`/`research_sources` when explicit domain set; canonical `MEETING_BRIEF` first write wins — SalesState keys only; optional `ResearchPort` search once per domain via R0 `meeting_research_read`; `TOOL_RESULT` `meeting_research`; R1 persist; kill switch skips brief + denies research; verified booking/reschedule stamp row with `meeting_status=booked` + `scheduled_at` only; owner WhatsApp pull phrases + `lead_*` via `apply_owner_meeting_brief` (Hebrew from stored payload; read-only on kill switch; no proactive send; no crawl/Sheets/customer summary) |
| Meeting debrief | §12.3 | alive (persist-only `meeting_debriefs`; owner WhatsApp text+audio classified `meeting_debrief` with `lead_*` id in message; outcome `held`/`no_show`/`unclear` from deterministic phrases; `next_step` classified `none`/`follow_up`/`proposal` from deterministic phrases; `estimated_value`/`notes` always `""`; canonical `MEETING_DEBRIEF` first write wins (`{lead_id}:debrief`; payload `outcome`+`next_step` only); R1 `meeting_debrief_persist`; kill switch skips; Understanding Check when no lead_id; no deal value/stage change, no calendar create, no send, no follow-up upsert, no Sheets transcript mirror) |
| Meetings | §12.2 / §19 | alive (`meetings` state `offered|booked|cancellation_requested`; initial numbered booking + verify unchanged; reschedule offers stored separately in `reschedule_slots_json`; verified reschedule updates only `scheduled_at` and `rescheduled_at`, preserves event ID, Meet link, type, and `booked_at`; cancellation request sets local status/timestamp and never calls Calendar; canonical `MEETING_BOOKED`, `MEETING_RESCHEDULED`, and `MEETING_CANCELLATION_REQUESTED` are first-write and redacted; Sheets tab `04` may mirror status/time/event ID but never Meet link) |
| Approvals | §33 | alive (persist-only `approvals`; R3 lead `proposal_handoff` on graph `handoff` when `owner_required`; R4 campaign `campaign_write` on owner WhatsApp request phrases — hash+expiry+resource binding, unique `(resource_type, resource_id, action)`; campaign rows `lead_id=NULL`; stale/unbound pending cannot be decided; canonical `APPROVAL_REQUIRED` first write wins; R1 `approval_persist`/`approval_decide`; owner approve/reject **persist-only** — never Meta; kill switch skips; no execute) |
| Deals | §32 | alive (persist-only `deals`; one row per lead; upsert on graph `offer_meeting` → `meeting_offered` or `handoff` → `proposal`; forward-only stage rank; `expected_value`/`closed_value` always `""`; attribution confidence `utm` when lead has ATTRIBUTION canonical event else `unknown`; canonical `DEAL_UPDATED` first write wins per `{lead_id}:deal:{stage}`; R1 `deal_persist`; kill switch skips; no won/lost, no value inference; wired on website + prospect inbound; Sheets tab `05 Deals` mirror when row exists; owner/Graph Lab excluded) |
| AI runs audit | §32, §36, §40.2 | alive (one `ai_runs` row per sales graph invoke on website + prospect inbound; `run_id` unique; graph version + model label + next_action + kill_switch + `policy_version=fde_v1` + `prompt_version=sales_reply_v1` + allowlisted `automation_mode` + `decision_confidence="1.0"` (deterministic NBA pin; no LLM self-score) + wall-clock `latency_ms` around `graph.invoke`; `tokens_in`/`tokens_out` from OpenAI usage on successful live compose — canned/fallback/kill-switch 0; `cost_usd` 0; no prompt/reply/lead text; Graph Lab/owner excluded) |
| FDE shadow mode | FDE operating layer | alive (`MIA_AUTOMATION_MODE` default `shadow`; prospect inbound skips MessagePort under SHADOW; graph + `ai_runs` + follow-up persist still run; `shadow_decisions` stores metadata + proposed reply only; owner acks still send; website HTTP replies unchanged; HYBRID not wired; does not override R4/R5 or kill switch) |
| FDE human takeover | FDE operating layer | alive (`leads.human_takeover`; owner exclusive takeover phrases + `lead_*` via `apply_owner_human_takeover`; owner exclusive resume phrases + `lead_*` via `apply_owner_human_resume`; R1 `human_takeover_persist`; prospect MessagePort skip under takeover; graph + `ai_runs` still run; follow-up send-readiness denies `human_takeover`; distinct from `conversation_killed`; website HTTP unchanged) |
| FDE idempotency | FDE operating layer | alive (`IdempotencyStore` Protocol in `app/domain/idempotency.py`; `LeadStore.claim_webhook` reclaims stale in-flight `received` via `is_stale_received`/`STALE_AFTER_SECONDS` (300s) shared with reconciliation; `failed`→`received` retry; `processed`/`sent` stay unique + `claim_operation`/`complete_operation`/`fail_operation`/`get_operation_result` on Postgres `idempotency_records` unique `(scope, key)`; allowlisted scopes `calendar_create`, `calendar_reschedule`, `canonical`, `approval`, `owner_task`, `sheets_mirror`, `follow_up`, `calendar_cancellation`; in-flight TTL + completed result store on `claim_operation`; wired on meeting booked persist (`calendar_create` / `{lead_id}:booked` → `complete_operation` `{"ok": true}`), verified reschedule persist (`calendar_reschedule` / `{lead_id}:rescheduled:{target_key}` → `complete_operation` `{"ok": true}`), approval persist (`approval` / `{lead_id\|campaign_id}:approval:{action}` → `complete_operation` `{"ok": true}`), owner task persist (`owner_task` / `{provider}:{provider_event_id}` → `complete_operation` `{"ok": true}`; execute still gated), and Sheets mirror persist (`sheets_mirror` / `{inbound_id}:sheets:{sales\|session\|campaign\|content}` → `complete_operation` `{"ok": true}`; inbound key, not lead), and follow-up persist (`follow_up` / `{inbound_id}:followup` → `complete_operation` `{"ok": true}`; inbound key, not lead; send still gated), and cancellation persist (`calendar_cancellation` / `{inbound_id}:cancellation` → `complete_operation` `{"ok": true}`; inbound key, not lead; provider delete still gated); lost-response reread via `get_operation_result`; Powertools **DEFER**; persist-path duplicate suite `tests/unit/test_idempotency_persist_paths.py`; R1 `claim_operation`) |
| Preloaded tool pins | Pre-prod Adjustment F | alive (frozen catalog `app/tools/registries/mia_preloaded_tools.py`; versions imported from adapter constants; `preloaded_tool()` lookup; no Composio catalog discovery; `MIA_DYNAMIC_TOOL_DISCOVERY` unused/false; customer graph has zero Composio tools) |
| Model task classes | Pre-prod Adjustment J | alive (lookup-only registry `app/domain/policies/task_classes.py`; `TaskClass` StrEnum + frozen `TaskClassPin`; `task_class_pin()` pins current owner + `model_source` token; fail-closed unknown → `code`/`none`; not a live router; not wired into inbound/graph) |
| Freshness policy | Pre-prod Adjustment N | alive (lookup registry `app/domain/policies/freshness.py`; `FreshnessClass`/`FreshnessStatus` StrEnums + frozen `FreshnessPin`/`FreshnessStamp`; `freshness_pin()` + `stamp_freshness()` + `overlay_stale()`; fail-closed unknown → `live_only`/`none`; versioned knowledge → `unverified`; not a cache; **`campaign_metrics` stamp wired** on Meta insights enrich (`enrich_analytics_ack`); **`instagram_content_metrics` stamp wired** on organic IG insights enrich (`enrich_content_insights_ack`); **`linkedin_content_metrics` stamp wired** on member analytics enrich (`enrich_linkedin_analytics_ack`); **`linkedin_profile` stamp wired** on profile enrich (`enrich_linkedin_ack`); **`research_snippets` stamp wired** on public search enrich (`enrich_research_ack`); **`campaign_budget_status` stamp wired** on Meta `this_month` pacing fetch (`meta_ads_pacing` via `campaign_budget_outcome`; audit `tool_runs.freshness` only); **`calendar_availability` stamp wired** on offer + owner read (`prepare_meeting_offer`, `apply_owner_calendar`); **`gmail_results` stamp wired** on Composio empty-body hydrate (`gmail_fetch`); **`opt_out_status` stamp wired** on `conversation_killed` change; **`conversation_ownership` stamp wired** on prospect IG inbound once per lead; **`owner_permissions` stamp wired** on owner inbound once per owner id; **`lead_recent_messages` stamp wired** on due-scan message_out count; **`website_session_events` stamp wired** on analytics WATCH funnel `count_behavior_events`; persisted on `tool_runs.freshness` audit only; ack unchanged; RAG missing) |
| Tool runs audit | §32, §36 | alive (one `tool_runs` row per `persist_tool_outcome`; `provider_event_id`=`{inbound_id}:tool:{tool}` unique, first write wins; sanitized `tool_runs.correlation_id` joins canonical envelope / `ai_runs.run_id` (owner `cor_*`; Gmail hydrate reuses MESSAGE_IN; due-scan and website session-create sheets_mirror empty; not in payload; migration `migrations/20260821_tool_run_correlation_id.sql`); canonical TOOL_RESULT payload `{tool, status, result_count}` only; `ToolOutcome.freshness` allowlist `""|live|cached|stale|unverified` persisted on `tool_runs.freshness` (Meta `campaign_metrics` + `campaign_budget_status`/`meta_ads_pacing` + calendar `calendar_availability` + Gmail `gmail_fetch` + `opt_out_status` + `conversation_ownership` + `owner_permissions` + `lead_recent_messages` + `website_session_events` + Instagram `instagram_content_metrics`/`instagram_insights` + LinkedIn `linkedin_content_metrics`/`linkedin_analytics` + LinkedIn `linkedin_profile` + research `research_snippets`/`research_search` wired; versioned knowledge still unlabeled); `ToolOutcome.status` allowlist `ok|denied|empty|error|unauthorized|rate_limited|malformed|retryable|partial|stale` (`ok` = success; no `success` token); LinkedIn analytics stamps `partial` when some metrics missing; `AdapterHttpError` + `tool_status_from_http` classify HTTP on Meta insights, LinkedIn analytics, LinkedIn profile, Calendar free/busy, Calendar booking list/create/GET/PATCH, Gmail fetch, Instagram media-list, Sheets upsert, and research — customer/owner retry copy unchanged on booking HTTP; Sheets `mirror_*` skip; WhatsApp/IG send, STT, and WhatsApp media raise AdapterHttpError then wrap MiaError 502 (rollback unchanged; no send tool); OpenAI sales-reply _complete raises AdapterHttpError, compose catches then canned; OpenAI thread-summary _complete raises AdapterHttpError, summarize catches then canned unclear; `ToolOutcome.latency_ms` from port wall-clock on research/meta/linkedin/calendar enrich + STT, and sales-tab / session-tab / campaign-tab / content-tab Sheets upserts after claim (`sheets_mirror_outcome` / `sheets_tab_mirror_outcome`); explicit `persist_tool_outcome(..., latency_ms=)` wins when non-zero; `cost_usd` 0; no prompt/PII/URLs/slot times; kill-switch `denied` still persisted; migration `migrations/20260821_tool_run_freshness.sql`) |
| AWS runtime | §29 | specified (first live **host** ADR-014 proven 2026-08-22 in **eu-north-1**: ECS Fargate + RDS + Secrets Manager box `mia/prod` + ALB/ACM `https://mia.assafweb.com` — `docs/LIVE_STAGING_ACCEPTANCE_REPORT.md`; Assaf ADOPT selected Region **eu-north-1** ADR-019; `CapabilityId.AWS_RUNTIME` stays specified — no `app.infra`; Lambda/SQS/WAF/AgentCore later) |
| FDE operating layer | Assaf 2026-08-21 | alive (policy registry + decision routing: `app/domain/policies/execution_policy.py`, `app/domain/policies/decision.py`, `app/domain/policies/failure_policy.py`, `app/domain/policies/task_classes.py`, `app/domain/policies/freshness.py`; `ExecutionMode` + `ActionPolicy` lookup wraps `RiskLevel`; `NodeFailurePolicy` lookup pins per-tool fail-closed defaults (registry only; adapters unchanged); `TaskClassPin` lookup pins current owners (registry only; not a live router); `FreshnessPin`/`FreshnessStamp` lookup stamps fact provenance (`campaign_metrics` + `calendar_availability` + `gmail_fetch` + `opt_out_status` + LinkedIn/Instagram/research retrieval stamps wired → `tool_runs.freshness`; ack unchanged); `AgentDecision` + `route_decision`/`risk_gate` pure functions wrap NBA+reply; not wired into graph; shadow prospect send skip alive) |

**ADR-013 canonical update (2026-08-21):** `MEETING_RESCHEDULED` is first-write per deterministic target booking key with payload `{status:"booked", scheduled_at:<UTC ISO>}` only. `MEETING_CANCELLATION_REQUESTED` is first-write per lead with payload `{status:"cancellation_requested"}` only. Verified booking follow-up closure uses `FOLLOW_UP` payload `{status:"cancelled", reason:"meeting_booked"}`. Reschedule tool audits are `calendar_reschedule_get`, `calendar_find_free_slots`, `calendar_patch_event`, and `calendar_reschedule_verify`; all retain the standard `tool`/`status`/`result_count` payload only.

## Core experience

```
Attention (IG / Website / Meta / Email)
↓
Conversation
↓
Understand business & daily workflow
↓
Discover friction
↓
Quantify impact
↓
Reflect understanding
↓
Automation hypothesis
↓
Qualification
↓
Meeting / Assaf handoff / follow-up
↓
Outcome + attribution + learning
```

# 2. Product Definition, Goals and Non-Goals

## 2.1 Product definition

Mia is not a generic assistant. Mia is a governed sales-and-growth operating layer for AssafWeb, optimized for inbound lead engagement, owner productivity, campaign intelligence and learning from controlled human feedback.

## 2.2 Goals

- Respond to inbound leads quickly and intelligently across supported channels.
- Make sales conversations feel consultative and human rather than scripted.
- Build a unified lead record across Instagram, WhatsApp, website, Gmail and Calendar.
- Discover business workflow friction before pitching solutions.
- Convert qualified leads into meetings with minimal manual work.
- Give Assaf concise daily and weekly operating intelligence.
- Analyze Assaf's Instagram performance and external content signals to generate ideas, not finished content.

**Implementation (2026-08-21):** Owner WhatsApp `content ideas` / `רעיונות לתוכן` is **persist-only** this slice. Deterministic kinds (`more_reels`, `more_video`, `more_image`, `more_carousel_album`) rank from Postgres `content_insights` by `lead_signals` desc then numeric `views` (missing views sort last; never zero-filled). One row per local calendar day in `content_ideas` (unique `idea_date`). Hebrew ack states ideas only — no captions, URLs, media_id, metrics, or publish. R1 `content_idea_persist`; kill switch format-only; demo skips persist and apply returns None; no LLM, Graph fetch, execute, or Sheets.

**Implementation (2026-08-21):** Owner WhatsApp `weekly brief` / `סיכום שבועי` is **persist-only** this slice. ISO Monday week in `MIA_CALENDAR_TIMEZONE` via `compute_weekly_kpi` (Sheets tab 09 KPI counts unchanged: leads, meetings_offered, handoffs, messages_in, follow_ups_pending only). Owner scorecard additionally counts `meeting_booked` and `meeting_cancellation_requested` from canonical events for the same week bounds (`meetings_booked`, `cancellation_requests` on `owner_weeklies`; no PII). One row per week in `owner_weeklies` (unique `week_start`). Hebrew scorecard ack replaces generic logged ack; pacing/prelaunch status labels only (no spend digits). R1 `owner_weekly_persist`; kill switch format-only; demo skips persist and apply returns None; no LLM, execute, send, or Sheets write on this path.
- Analyze Meta campaigns beyond CPL by connecting lead quality, meetings and deal outcomes.
- Maintain a Google Sheet with campaign budget, pacing, leads, meetings, deals and references.
- Understand Assaf's voice notes and execute/clarify tasks in text.
- Learn approved preferences, rules and working habits from Assaf without self-modifying code.
- Continuously improve prompts, routing and graph behavior through a local, versioned Graph Engineering loop.
## 2.3 Non-goals

- No voice output for Mia.
- No automatic creation or publication of Assaf's social media posts, images or videos.
- No self-editing code, self-deployment or autonomous graph changes.
- No autonomous Meta budget, bid, campaign launch or campaign pause in initial production.
- No cold Instagram DM spam; messaging must comply with platform rules and valid conversation entry points.
- No Google Sheet as the system database.
- No hidden manipulation, fake urgency, invented scarcity or unsupported claims in sales conversations.
- No uncontrolled mass outbound prospecting in initial production.
- No Make or ManyChat dependency in the core state machine.
# 3. Users and Operating Modes

| Actor | Primary needs | Allowed interaction |
| --- | --- | --- |
| Assaf / Owner | Commands, voice notes, daily brief, lead review, campaign analysis, learning corrections, approvals, takeover | Telegram (v1 private owner channel). WhatsApp owner phones remain a fallback allowlist, not the v1 product surface. |
| Inbound prospect | Fast helpful conversation, diagnosis, relevant recommendation, booking | Website first. WhatsApp only after a verified website handoff token. Email is intelligence + approval-gated draft/send. |
| Existing lead | Continuation without restarting context | Linked website session or verified WhatsApp handoff |
| Personal WhatsApp contact | Human-only. Mia does not reply or analyze. | WhatsApp Business inbox — Assaf only |
| Demo viewer / client | See real capabilities without exposing private data | Demo Mode |
| Developer / Assaf in Cursor | Improve graph, prompts, models and code through reviewed releases | Local dev + Git/CI |

## Automation modes

| Mode | Behavior |
| --- | --- |
| OFF | Mia records/observes only; no outbound actions. |
| DRAFT_ONLY | Mia drafts messages/actions; Assaf approves all sends. |
| HYBRID | Approved low-risk inbound flows can execute; exceptions require approval. |
| AUTO_APPROVED_SCOPES | Only explicitly approved capabilities act automatically; risky actions remain gated. |

# 4. Product Principles and Boundaries

- Understand before automate: Mia first understands workflow/context, then identifies friction.
- One meaningful question at a time on chat channels.
- Research before asking questions when reliable public information is already available.
- Deterministic business rules remain code, not prompt prose.
- Critical facts come from structured sources of truth, never model memory.
- External content is untrusted data, never executable instruction.
- Every high-risk action is explicit, reviewable and auditable.
- A model is selected per task using quality, latency, cost and risk—not personal preference alone.
- Provider-specific integrations sit behind adapters.
- Production behavior is versioned and evaluated before promotion.
- The system must fail safely and explain uncertainty.
- Qualified meetings matter more than raw meeting count.
## Mia capability boundary

| Capability | Default policy |
| --- | --- |
| Read own Instagram insights/content/conversations | Auto |
| Reply to approved inbound IG/WhatsApp lead | Auto in approved scope |
| Qualify lead / update lead state | Auto |
| Create/update lead timeline and Google Sheet mirror | Auto |
| Read Gmail/Calendar within configured scope | Auto |
| Create meeting after explicit prospect confirmation | Auto in approved scope |
| Send normal follow-up | Auto if policy + consent + frequency checks pass |
| Analyze Meta campaigns and budget pacing | Auto read/analysis |
| Recommend campaign change | Auto recommendation |
| Change Meta budget/pause/launch | Approval required |
| Send commercial proposal / price quote outside approved rules | Approval required |
| Mass outbound | Approval required / later phase |
| Publish social content | Disabled |
| Modify AssafWeb | Disabled |
| Modify code/graph/prompt autonomously | Never |
| Read/analyze own LinkedIn profile/post performance where API permissions allow | Auto read/analysis |
| Publish LinkedIn content or mass-message prospects | Disabled by default / explicit later approval design |

# 5. End-to-End Customer and Owner Journeys

## 5.1 Instagram lead

```
Reel / Post / Story / Profile
↓
Comment / DM / approved ManyChat trigger
↓
Lead identity lookup
↓
Mia sales state
↓
Workflow-first discovery
↓
Qualification
↓
Continue in IG OR secure handoff to WhatsApp
↓
Meeting
↓
Timeline + attribution + follow-up
```

## 5.2 Website visitor

```
AssafWeb visit
↓
Behavior events + UTM captured
↓
Mia entry point (non-intrusive)
↓
"ספר לי קצת איך נראה יום רגיל בעסק. במה אתה רוב הזמן עסוק?"
↓
3–6 meaningful exchanges when possible (guideline, not a turn counter):
who / business, general workflow, first real friction, engaged answers
↓
Natural WhatsApp continuation offer + secure one-time handoff token
↓
Same conversation continues on WhatsApp (no rediscovery)
↓
Meeting when buying reality is there
```

## 5.3 Owner voice-note task

```
Assaf WhatsApp voice note
↓
Download audio
↓
Speech-to-text
↓
Task classifier
↓
New/ambiguous/high-impact?
↙                         ↘
yes                          no
Understanding Check          execute
↓
Assaf confirms/clarifies
↓
execute
↓
text response + task/audit update
```

# 6. Channel Architecture

All channels must map into a canonical conversation/event model. A channel provider may change without changing the sales engine.

| Channel | Primary adapter | Secondary / fallback | Notes |
| --- | --- | --- | --- |
| Website on vercel | Custom AssafWeb API client | None | Primary v1 customer sales channel. Own session/event tracking; streamed text. |
| Telegram | Bot API `POST /v1/telegram/webhook` | None | ADR-017 private owner control. Numeric user-id allowlist. Voice in, text out. |
| Instagram | Inbound: Meta webhook. Send/insights: Composio when ports land (Graph until then). | ManyChat ingest sidecar | Analytics/research may remain. Not a v1 autonomous sales inbox. Only one sender owns a conversation. Never dual-send. (ADR-015) |
| WhatsApp | Inbound: Meta Cloud API webhook. Send: Graph (`MIA_WHATSAPP_SENDER=direct`) or Composio `WHATSAPP_SEND_MESSAGE` (`composio`). | None for inbound | ADR-016 + ADR-017. Controlled website-handoff continuation only. Unknown/personal contacts stay human-only. Official Composio toolkit has no incoming-message trigger. Never dual-send. |
| Gmail | Composio Gmail | Direct Google API later if required | Read/summarize/draft. Send approval-gated. Least-privilege scopes. |
| Calendar | Composio Calendar | Direct Google Calendar API | Free/busy and events only as needed. |
| Google Sheets | Composio/Google Sheets API | Direct Sheets API | Mirror/control layer. |
| Meta Ads | Composio Meta Ads + direct Meta credentials | Direct Marketing API | Read/analysis first; writes gated. |
| Research | Firecrawl | Apify later behind `ResearchPort`; browser fallback | Public/authorized sources only. No Apify env until wired. |
| Browser | Playwright/managed browser sandbox | AgentCore Browser later | Allowlist and prompt-injection controls. Flag off. |
| LinkedIn | Profile: Composio. Member post analytics: direct REST (ADR-009). | Read-only research from public web where policy permits | Secondary professional/growth intelligence. No autonomous posting. |

# 7. AssafWeb Website Funnel and Minimization Plan

The current site already communicates the core promise effectively: a digital employee that answers, sells and schedules; it includes a live voice-agent proof point, live projects, service explanations, vertical scenarios, process, testimonials, FAQ and multiple CTAs [R1]. The objective is not a redesign. It is reduction, sharper hierarchy and an embedded Mia experience.

## 7.1 Keep / compress / merge

| Current section | Decision | Reason |
| --- | --- | --- |
| Hero | KEEP, shorten copy slightly | Strong promise and WhatsApp CTA. |
| Live voice-agent demo | KEEP as separate proof of voice-agent capability | Mia herself is text-only, but the demo proves service capability. |
| Live projects | KEEP | Critical proof. |
| What a digital employee can do | COMPRESS from many service cards to 4 core categories | Reduce scroll and choice overload. |
| Vertical examples | COMPRESS to 3 strongest examples + expand | Retain relevance without long repetition. |
| Statistics / 'team of AI blocks' / tech stack visualization | MERGE into compact credibility section | Interesting but currently extends page length. |
| About Assaf + process | MERGE / shorten | Maintain trust and personal story. |
| Testimonials | KEEP | High-value proof. |
| FAQ | KEEP 4 highest-friction questions | Reduce length. |
| Final CTA/form | KEEP, integrate Mia/WhatsApp continuation | Primary conversion action. |

## 7.2 Mia website placement

- Do not show an aggressive popup immediately.
- Use a persistent but subtle 'Ask Mia about your business' entry point.
- Mia receives current page, section and relevant behavioral events as context.
- The opening prompt should ask about the visitor's business/day/workflow rather than immediately ask 'what is your pain?'.
- When a website session moves to WhatsApp, use a short-lived opaque handoff token so the lead can continue context without exposing PII in the URL.
- Track: page/section viewed, CTA clicks, Mia opened, Mia conversation started, qualification reached, WhatsApp handoff, meeting offered/booked, form abandoned.

### 7.2.1 Ask Mia widget host-page contract

AssafWeb marks elements with data attributes; the widget reads attributes only (never `innerText` or field values). Events POST to `POST /v1/website/sessions/{session_id}/events` after the visitor opens the launcher (session create). Events fired before session are queued (max 10, drop oldest) and flushed after the initial `page_viewed`.

| Host attribute | Event kind | Payload field |
| --- | --- | --- |
| `[data-mia-section="slug"]` | `section_viewed` | `section` |
| `[data-mia-cta="slug"]` | `cta_click` | `cta` |
| `form[data-mia-form]` | `form_started` | kind only (first field focus; no field values) |
| `form[data-mia-form]` | `form_abandoned` | kind only (no field values) |

Slug rules (client skips invalid): trim; no spaces, `@`, or newlines; no substring `token`/`secret`/`password` (case-insensitive); charset `^[a-zA-Z0-9_\-\u0590-\u05FF]+$`; max 80 chars. Same spirit as server `_sanitize_slug` in `app/domain/behavior.py`.

Widget behavior: collapsed launcher only (no auto-popup); `IntersectionObserver` for sections (≥40% visible, once per slug); capture-phase CTA clicks (ignores `#ask-mia-root`); `form_started` on first field `focusin` (once per page load); form abandon on `pagehide`/`visibilitychange` when dirty and not submitted (once per page load); optional SPA `popstate`/`hashchange` → another `page_viewed` with `location.pathname`.
# 8. Lead Identity, Timeline and Attribution

## 8.1 Canonical lead identity

- Internal customer_id / lead_id are the only canonical identities.
- Store channel identities separately: Instagram-scoped ID, WhatsApp phone hash/normalized phone, email, website session IDs, calendar attendee email.
- Never merge two identities based on weak similarity alone. Use verified link events, explicit contact data or high-confidence deterministic matching.
- Maintain merge history and reversible identity links.

**Implementation (2026-08-21):** Verified identity link history is **alive** (persist-only): on successful website→WhatsApp handoff consume, one row in `identity_links` (unique per `channel_identities.id`; reason `handoff_token` only; `reversed_at` null until a future unmerge slice). R1 `identity_link_persist` with `kill_switch=False` in assert — history still records when business kill switch is on. First write wins; never merges two existing customers on weak similarity; consume-fail when phone bound to another customer stays unchanged. No reverse/unmerge API this slice (R5).
## 8.2 Lead timeline

Every meaningful interaction becomes a timeline event: first source, content/URL, message, question, qualification update, research result, meeting, proposal status, follow-up, campaign attribution and outcome.

## 8.3 Attribution

- Capture utm_source, utm_medium, utm_campaign, utm_content, landing page and referrer on website visits.
- Store Meta campaign/adset/ad IDs when available.
- Store Instagram content ID / trigger source for organic engagement.
- Use one-time handoff tokens to preserve source across website→WhatsApp and other supported handoffs.
- Attribution must explicitly represent unknown/partial states rather than manufacture certainty.

**Implementation (2026-08-21):** Website session create persists sanitized UTM/landing/referrer (`sanitize_attribution`; `{lead_id}:attribution`; first write wins). Instagram inbound persists sanitized organic/referral attribution on prospect path (`sanitize_instagram_attribution`; allowlisted keys `ig_content_id`, `ig_trigger_source`, `ig_ref`, `meta_ad_id`, `meta_post_id`, `meta_campaign_id` only; story reply from `message.reply_to.story.id` → `ig_content_id` + `ig_trigger_source=STORY`; referral from `referral.source`/`ref` and ads `ad_id`/`ads_context_data.post_id`; never CDN URLs, photo/video URLs, or ad titles; referral-only events without `message.mid` accepted with synthetic `igref:{sender}:{stable}` where `stable` is first sanitized `meta_ad_id`|`ig_ref`|`ig_content_id`; empty text + attribution still persists ATTRIBUTION without sales graph). ManyChat External Request ingest maps optional `ad_id`/`campaign_id`/`post_id`/content/ref/trigger fields through the same sanitizer (names and media URLs dropped); sanitized contract fixtures for ad, story (`STORY` + `ig_content_id`), and comment (generic text + `event_id`; no invented DM trigger) in `tests/fixtures/manychat/`. Deal attribution confidence: `utm` | `ig` | `meta_ad` | `unknown` via `confidence_from_attribution` (`meta_campaign_id` counts as `meta_ad`).
# 9. Mia Sales Engine

Mia's sales behavior is workflow-first. The objective is to understand how the prospect works, surface friction naturally, quantify the consequence when possible, and then offer a useful hypothesis. The sales playbook blends Sandler-style depth, Gong-style conversational discovery, MEDDPICC-lite qualification and Challenger-style commercial insight [R38-R41].

## 9.1 Core conversational pattern

```
Understand person/business
↓
Understand the normal day / process
↓
Find a point of friction
↓
Explore frequency and consequence
↓
Reflect what Mia understood
↓
Quantify only with confirmed assumptions
↓
Offer an automation hypothesis
↓
Validate fit
↓
Qualify buying reality
↓
Guide next best step
```

**Implementation (2026-08-22):** Website channel (`select_next_action(..., channel="website")`) may return `offer_whatsapp` after `workflow_known` and pain P2+ (identifiable friction), before MEDDPICC grind. That is a continuation offer, not owner `HANDOFF` (no Telegram notify, no takeover). Greeting / one vague sentence stays `understand_workflow`. Graph Lab `buyers_v1` omits `channel` so the 12 personas still run the full funnel. `website_handoff_v1` replays the shoe-store path. Persist `SalesState.whatsapp_handoff_offered` and server behavior `whatsapp_handoff_offered`. Context survives the existing handoff token.

**Implementation (2026-08-21):** Live graph marks `reflected` / `hypothesis_offered` as delivered via `mark_action_delivered` after `select_next_action` each turn. Graph Lab `sales_v1` replays one-shot NBA + reply (no extract, no mark); Graph Lab `buyers_v1` replays multi-turn extract → NBA → mark → reply. Deterministic extract sets `fit=POSSIBLE` on allowlisted business-type tokens (never from UNKNOWN to GOOD without buying reality + pain P2+ on POSSIBLE); `fit=GOOD` only after buying-reality phrase + pain P2+ when fit is already POSSIBLE — meeting offer reachable, not invented. Cold willingness to meet with unknown workflow maps to `QUALIFY`, not `understand_workflow` (§10.4). Split `QUALIFY` replies ask exactly one Hebrew question from the first `missing_fields` gap (decision maker / timeline / metric); P4 from money/cost tokens only when pain is already P3+; P5 from timeline tokens only when pain is already P3+. Buying-reality and proposal phrases set `buying_reality_known` / `owner_required`; proposal requests hand off to Assaf — **no auto-quote**. Opt-out (`willingness_to_meet is False`) and poor fit beat handoff and discovery.

## 9.2 Golden chat rule

Most Mia messages should contain: acknowledge + reflect + one useful question. Avoid multi-question forms and avoid premature pitching. **`QUANTIFY`** asks frequency only (`כמה פעמים זה קורה?`); cost/value stays on **`QUALIFY`** metric.

**Implementation (2026-08-21):** Before any LLM sales paraphrase reaches a prospect, `lint_customer_reply` in `app/domain/humanity.py` runs deterministic Human Voice checks (AI phrases, typography, question count, unsupported-claim block). Phrase match folds curly apostrophes so `Let’s` / `It’s` still fail. Typography (Assaf correction): em/en dash, backslash, decorative ` / ` and ` - `, ASCII `--`, and single letters spaced with `-` / `--` / `'` (chupchik) / `\\` — `יום-יום` and `Let's go` pass; `//` is not a typography ban. Fail → try fallback model, then canned via `OpenAISalesReplyPort`; canned copy is not re-linted at runtime. All canned customer replies in `app/graph/replies.py`, `app/domain/booking_voice.py`, and meeting-change constants are proven by unit tests. Meet-link confirmations stay unlinted (booking copy with URLs). Owner WhatsApp scorecards, tool payloads, Sheets, and logs are out of scope. Judgment checks (1/2/5/7/8/10) remain a later slice.

## 9.3 Example

Mia: ספר לי קצת איך נראה יום רגיל בעסק. במה אתה רוב הזמן עסוק?

Lead: רוב היום אני עם לקוחות, אז אני לא תמיד עונה לטלפון.

Mia: אז בזמן שאתה עם לקוחות עדיין נכנסות לא מעט פניות? מה קורה איתן בדרך כלל עד שאתה מתפנה?

Lead: חלק חוזרים אליהם אחר כך, אבל בטוח שחלק נעלמים.

Mia: אז נשמע שהבעיה היא לא להביא פניות, אלא לתפוס אותן בזמן. בערך כמה כאלה נכנסות בשבוע?

Only after the customer has described the process and consequence should Mia propose a concrete automation hypothesis.

# 10. Sales State, Discovery and Qualification

## 10.1 SalesState schema

| Dimension | Examples |
| --- | --- |
| Fit | business type, size, service, technical fit |
| Workflow | current process, who handles it, systems used |
| Pain | surface issue, root cause, frequency |
| Impact | lost time, lost leads, operational friction, financial exposure |
| Desired state | target workflow and success condition |
| Buying reality | urgency, budget signal, authority, criteria, timeline |
| Trust | engagement, skepticism, objections, sentiment |
| Evidence | source IDs and confirmed facts |
| Next best action | ask, answer, research, reframe, qualify, book, handoff, stop |

## 10.2 Pain-depth model

| Level | Meaning | Mia behavior |
| --- | --- | --- |
| P0 | No problem identified | Understand workflow. |
| P1 | Surface friction | Ask what it looks like in practice. |
| P2 | Operational consequence | Explore what happens when it occurs. |
| P3 | Frequency/scale quantified | Confirm volume and impact assumptions. |
| P4 | Business consequence | Reflect cost/time/revenue exposure. |
| P5 | Urgency/priority | Clarify timing and next-step readiness. |

**Implementation (2026-08-21):** Deterministic extract sets P4 from money/cost/revenue tokens only when pain is already P3+; P5 from timeline tokens only when pain is already P3+ (P5 may overwrite P4). Frequency/scale tokens stay on P3 via `_IMPACT`; money tokens do not bump cold leads straight to P4.

## 10.3 MEDDPICC-lite for SMB

- Pain: What is actually broken or inefficient?
- Metric: Can value/cost be reasonably quantified?
- Decision maker: Who can approve spend/change?
- Timeline: When does solving it matter?
- Criteria: What would make the solution acceptable?
- Status quo/alternatives: What happens if they do nothing or use another approach?

**Implementation (2026-08-21):** `SalesState` tracks `authority_known`, `timeline_known`, and `metric_known` (default false). `buying_reality_known` becomes true from authority or timeline tokens and is never invented from metric alone; extract does not clear a previously true buying-reality flag. `missing_fields` lists remaining gaps in order: decision maker, timeline, metric. No budget, champion, criteria, or competition interrogation for SMB.

For larger B2B opportunities, the system may expand toward fuller MEDDPICC fields, but Mia must not interrogate a small-business lead with enterprise procurement questions.

## 10.4 Question-selection policy

- Choose the next question based on the most decision-relevant missing field, not a fixed script.
- Prefer questions that elicit narrative: 'walk me through...', 'what happens when...', 'tell me more...'.
- Use research to avoid asking public facts already known.
- Do not ask budget prematurely unless buying context makes it natural.
- If the lead is already product-aware and asking to buy, do not force latent-pain discovery.
- **Implementation:** when `willingness_to_meet is True` and `workflow_known` is false, `select_next_action` returns `QUALIFY` (not `understand_workflow`); meeting offer still requires `fit=GOOD` + buying reality + pain P2+. `QUALIFY` asks exactly one Hebrew question from `QUALIFY_REPLIES` keyed by the first entry in `missing_fields` (decision maker, timeline, or metric).
# 11. Objections, Reframing and Closing

## 11.1 Objection taxonomy

| Type | Mia first move | Then |
| --- | --- | --- |
| Price | Clarify what feels expensive / compare to what | Quantify value only from confirmed data; escalate pricing if needed. |
| AI trust/safety | Clarify concern: wrong info, unwanted actions, privacy, brand tone | Explain hybrid mode, approvals, auditability and boundaries. |
| No time | Understand implementation concern | Explain pilot-first path. |
| Already have tool/vendor | Understand current workflow and gap | Do not attack competitor; compare fit. |
| Not urgent | Explore cost of status quo respectfully | No fake urgency. |
| Need partner approval | Identify decision process | Prepare concise summary / next meeting. |

**Implementation (2026-08-21):** Deterministic phrase extract sets `active_objection` on allowlisted tokens; `select_next_action` → `handle_objection` before discovery when set (poor fit, opt-out/stop, and owner handoff still win); Hebrew first-move canned copy via `reply_for`; no LLM classify; no execute; no owner-instruction activation.

## 11.2 Challenger-style reframe

Mia may offer a new perspective only after sufficient context. The reframe must be grounded in what the prospect told Mia and/or verified public/business evidence. Constructive tension is about challenging the status quo, never the person [R41].

**Implementation (2026-08-21):** When `workflow_known`, `impact_confirmed`, and `reflected` are all true, `reply_for` returns Hebrew reframe copy for `handle_objection`; otherwise first-move clarify copy (§11.1). No LLM; no invented metrics; grounded in “what you already described” phrasing only.

## 11.3 Closing policy

- The goal is the correct next step, not always a meeting.
- Meeting offer only when fit + meaningful need + willingness are sufficient.
- `fit=POSSIBLE` from allowlisted business-type tokens; `fit=GOOD` only after buying-reality phrase + pain P2+ on POSSIBLE (never UNKNOWN→GOOD); live funnel can reach `offer_meeting` without inventing fit.
- Disqualify politely when fit is poor.
- Escalate to Assaf when the lead is high-value, technically unusual, pricing-sensitive, enterprise-like, or asks for a concrete proposal.
# 12. Follow-Up, Meetings and Commitments

## 12.1 Follow-up engine

- Follow-ups are stateful tasks, not ad-hoc reminders.
- Before sending: verify consent/channel rules, previous message, lead quality, opt-out, frequency cap and whether the lead already replied elsewhere.
- Use a deterministic maximum cadence; no unlimited follow-up loop.
- Track response and recovered meeting/deal attribution.

**Implementation (2026-08-21):** Prospect follow-up is **persist-only** this slice: when the sales graph selects `offer_meeting` and `willingness_to_meet` is not false, Mia upserts one row in `lead_follow_ups` (unique per lead) with `due_at` = tomorrow in `MIA_CALENDAR_TIMEZONE` and reason `meeting_offered`; canonical `FOLLOW_UP` event on first pending create (`{lead_id}:followup:meeting_offered`), cancel (`:cancelled`), and recover (`:recovered`). Cancel on `stop` or `disqualify` (opt-out) even when kill switch is on. Recover when a later graph action is neither offer nor opt-out (lead already replied). R1 `follow_up_persist` before create; kill switch skips create only. Persist is also gated by `claim_operation(scope=follow_up, key={inbound_id}:followup)` then `complete_operation` `{"ok": true}` — failed webhook reclaim of the same inbound skips a second upsert; a later inbound still cancels/recovers/creates. Do not claim per lead. Due-scan and verified-booking cancel are unclaimed this slice. **Send-readiness** is code-only: `evaluate_follow_up_send` in `app/domain/followups.py` returns allowlisted reasons (`due_pending`, `no_row`, `not_pending`, `cancelled`, `recovered`, `not_due`, `kill_switch`, `conversation_killed`, `human_takeover`, `channel_not_sendable`, `poor_fit`, `frequency_capped`) for whatsapp/instagram pending rows — never calls MessagePort. **Frequency cap:** max one canonical `message_out` per `lead_id` per local calendar day in `MIA_CALENDAR_TIMEZONE` (`MAX_OUTBOUND_PER_LEAD_PER_DAY=1`; owner acks with `lead_id=None` excluded; invalid timezone fail-closed to `frequency_capped`); still no send. **Due scan (persist-only):** `scan_due_follow_ups` lists pending rows with `due_at <= today`, runs `evaluate_follow_up_send`, and persists `send_ready` + `block_reason` on the row (R1 `follow_up_scan` with `kill_switch=False` in assert so deny reasons still persist when the business kill switch is on; upsert resets scan fields). When `send_ready`, due scan also composes canned Hebrew follow-up copy via `compose_follow_up_draft` (`app/domain/followup_voice.py`), runs `lint_customer_reply`, and persists approved text on `lead_follow_ups.draft` only — never MessagePort send; draft never in Sheets tab 08, due-scan JSON, logs, traces, or canonical events. Missing SalesState skips that row. **Due-scan worker:** local CLI `mia-due-scan` (`app/workers/due_scan.py`) invokes `scan_due_follow_ups` + `scan_due_owner_tasks` with shared `now`; prints JSON counts only; no HTTP route. Never sends, no scheduler, no SQS. Sheets `08 Follow-ups` unchanged (result stays `meeting_offered` reason). Existing Postgres/file sqlite DBs need `send_ready`, `block_reason`, and `draft` on `lead_follow_ups`.

**ADR-013 follow-up stop (2026-08-21):** After verified initial booking or booking crash recovery, a pending `meeting_offered` follow-up becomes `cancelled` with reason `meeting_booked`, `send_ready=false`, and a redacted first-write canonical `FOLLOW_UP`. `evaluate_follow_up_send` independently denies with `meeting_booked` when the meeting is `booked` or `cancellation_requested`, including stale pending rows. Mere offer, reschedule offer, completed reschedule, and cancellation request do not create or reopen follow-up.

Owner-task relative due tokens (`today`/`היום`, `tomorrow`/`מחר`, `next week`/`בשבוע הבא`) still persist `owner_tasks.due_at` as `YYYY-MM-DD` in `MIA_CALENDAR_TIMEZONE`; Hebrew ack mentions the formatted date. Tokens match on word/letter boundaries. **No send, no schedule, no execute** for owner tasks either this slice.
## 12.2 Meeting preparation

```
Calendar event
↓
Lead timeline + IG/WhatsApp + Gmail
↓
Company research (if appropriate)
↓
Meeting brief:
- why they came
- workflow/pain
- what is known / missing
- objections
- likely solution
- questions Assaf should ask
```

**Implementation (2026-08-21):** Pre-meeting brief is **persist-only** this slice. Bible §12.2 starts from a calendar event; create is gated, so the alive trigger is graph `offer_meeting` (same turn as `MEETING_OFFERED` + follow-up persist). Mia upserts one row in `meeting_briefs` (unique per lead) with a sanitized snapshot from SalesState: channel (why they came), workflow/pain flags, known/missing MEDDPICC-lite fields, active objection, owner questions derived from `missing_fields`, and `next_action=offer_meeting`. Canonical `MEETING_BRIEF` event on first write wins (`{lead_id}:brief:offer_meeting`; **allowlisted SalesState snapshot only** — excludes `company_domain`, `research_attempted`, `research_sources`). R1 `meeting_brief_persist` before upsert; kill switch skips persist. **Booked stamp (2026-08-21):** After verified booking (including provider-already-there recovery) and verified reschedule, `persist_booked_meeting_brief` merges `meeting_status=booked` and UTC `scheduled_at` into the existing Postgres row only when an offer snapshot already exists; demo/kill switch skip; no Meet link, names, emails, phones, or new canonical event. **Owner pull (2026-08-21):** Owner WhatsApp exclusive phrases (`meeting brief`, `pre-meeting brief`, `תקציר פגישה`, `בריף פגישה`) classify as `meeting_brief` before keyword matching (after owner_notify); requires `lead_*` or Understanding Check; `apply_owner_meeting_brief` returns Hebrew from stored payload only (channel, fit, pain, next_action, gaps/questions, objection, booked status, `מועד` via `format_slot_time`, optional title+host research); kill switch format-only; demo returns None; task stays `logged`; no `due_at`; no proactive send.

**Owner meeting notify (2026-08-21):** When a meeting is verified booked, rescheduled, or a first cancellation is requested (website or prospect inbound), Mia upserts one unseen row in `owner_notifications` (unique `(kind, lead_id)`; allowlisted kinds `meeting_booked`, `meeting_rescheduled`, `meeting_cancellation_requested`; `scheduled_at` UTC ISO; `seen_at` empty; first write wins per kind — a lead may have one row per kind). R1 `owner_notify_persist`; kill switch and demo skip persist. Booked: after verified `MEETING_BOOKED`. Rescheduled: after verified local+canonical `MEETING_RESCHEDULED` with new `scheduled_at`. Cancellation: after first `mark_meeting_cancellation_requested` + canonical `MEETING_CANCELLATION_REQUESTED` using the meeting's booked `scheduled_at` (skip if unparseable). No persist on reschedule offer, conflict, retry, deny, kill-switch deny, already-cancellation early return, or `NOT_HANDLED`. Owner WhatsApp exclusive pull phrases (`booked meetings`, `what got booked`, `meeting notifications`, `מה נקבע`, `פגישות שנקבעו`, `התראות פגישות`) classify as `owner_notify` after calendar/gmail_summary and before keyword matching — bare `התראות`/`notifications`/`calendar`/`יומן` do not match. `apply_owner_notify` lists up to three unseen rows across all three kinds (ordered by `scheduled_at` then `id`), returns Hebrew blocks (kind first line + `lead_id` + `מועד` via `format_slot_time` only; no names/Meet links), marks seen on deliver (R1 `owner_notify_deliver` with business kill switch bypass in assert); kill switch on pull is format-only (no mark seen); demo returns None. Kind first lines: booked `נקבעה פגישה.`; rescheduled `פגישה עודכנה.`; cancellation `בקשת ביטול.`. Empty inbox: `אין התראות פגישות חדשות.` Extra unseen: `עוד {n} התראות.` Never proactive MessagePort send; owner inbound reply path only. Task stays `logged`; no `due_at`. Not added to `format_daily_brief` scorecard this slice.

**Meeting research (2026-08-21, ADR-010):** When `SalesState.company_domain` is set via explicit extract (`app/domain/company.py`) or prospect reply, and typed `ResearchPort` is available, R0 `meeting_research_read` runs `port.search(domain)` once per lead/domain (query = validated domain only; max two HTTPS title+host sources via `sanitize_snippets`; stored in brief row only). First `OFFER_MEETING` may append Hebrew domain question when domain missing — does **not** block the meeting. Cache: same domain with `research_attempted=true` never re-calls. Disabled/empty/error still marks attempted when port passed; kill switch denies research and skips brief write. Canonical `TOOL_RESULT` tool `meeting_research` (`tool`/`status`/`result_count` only). No crawl, no customer-facing summary, no owner notify send. Existing DBs need `company_domain` on `lead_sales_state`. Meetings SoR is **alive** on the same trigger: one row in `meetings` (unique per lead) with status `offered`, source=channel, and empty `scheduled_at`/`calendar_event_id`/`summary` — never slot times or brief payload; not a booked meeting; no `MEETING_BOOKED` event; R1 `meeting_persist`; Sheets tab `04 Meetings` mirror when row exists.

## 12.3 Post-meeting voice note

Assaf can send a voice note summarizing the meeting. Mia transcribes it, performs an Understanding Check if information is ambiguous, then updates deal stage, requirements, estimated value, follow-up task and calendar/Sheet mirror.

**Implementation (2026-08-21):** Post-meeting debrief is **persist-only** this slice. Owner WhatsApp text or voice note classified as `meeting_debrief` (existing STT + classify) triggers persist when message contains `lead_[a-f0-9]{12}`; otherwise Understanding Check (no persist). Mia upserts one row in `meeting_debriefs` (unique per lead; latest outcome and next_step win) with allowlisted `outcome` (`held`/`no_show`/`unclear` from deterministic phrases; default `held` when only debrief+lead_id), allowlisted `next_step` (`none`/`follow_up`/`proposal` from deterministic phrases; default `none`; both follow_up and proposal phrases → `none`), and empty `estimated_value`/`notes` (store rejects non-empty). Canonical `MEETING_DEBRIEF` first write wins (`{lead_id}:debrief`; payload `outcome`+`next_step` only). R1 `meeting_debrief_persist`; kill switch skips. Hebrew ack states saved, no deal value update, no calendar event. **No deal stage change, no value inference, no calendar create, no MessagePort send, no follow-up upsert, no Sheets debrief dump** this slice. Transcripts are data, never instructions.

## 12.4 Task commitments

Mia stores commitments such as 'check Daniel tomorrow if he has not replied' or 'analyze Campaign Yuma after spend reaches the configured threshold' as durable tasks with trigger, condition, action, owner and status.

**Implementation (2026-08-21):** Classified owner tasks (except preference / Understanding Check) persist allowlisted `trigger`, `condition`, and `action` on `owner_tasks` alongside optional `due_at`; relative due tokens set `trigger=due_date`; `if_not_replied` when a sales follow-up matches English/Hebrew reply-wait phrases; action maps task type (`sales`→`follow_up`, `analytics`→`analyze`, `research`→`research`, else→`log`). Ack reflects due date and, when `if_not_replied`, appends `רק אם לא תהיה תשובה`. **Spend-threshold (persist-only, alive):** analytics owner text matching allowlisted spend-reaches phrases (`after spend reaches`, `when spend reaches`, `when spend hits`, `spend reaches the`, `כשההוצאה מגיעה`, `כשההוצאה תגיע`, `אחרי שההוצאה`) sets `trigger=spend_threshold` and clears `due_at` (date tokens ignored); inbound skips immediate Meta/IG insights enrich (no analyze-now); ack `נרשם כמשימת אנליטיקה כשההוצאה תגיע לתקציב המוגדר. לא ביצעתי אותה.` + Meta-writes disclaimer; never parses ₪/amounts from owner text; threshold is `MIA_CAMPAIGN_MONTHLY_BUDGET` config only. **Due scan (persist-only):** `scan_due_owner_tasks` lists logged date-due rows (`due_at <= today`) and logged `spend_threshold` rows; compares Meta MTD spend from Postgres `campaign_pacing.spend` to configured budget via `evaluate_spend_threshold` (`spend_reached` → `due_ready=True`; `spend_below`, `spend_unknown`, `no_budget` → `due_ready=False`; missing spend never treated as zero); invalid timezone skips date rows but still scans spend-threshold rows; date-row reasons unchanged (`due_pending`, `if_not_replied`, `needs_clarification`, `not_due_trigger`); R1 `owner_task_scan` with `kill_switch=False` in assert. **Due-scan worker:** local CLI `mia-due-scan` (`app/workers/due_scan.py`) invokes both domain scans with `parse_monthly_budget(settings.campaign_monthly_budget)` + pacing spend; JSON counts only; no HTTP route. Never executes analyze, sends, Meta writes, or MessagePort; no scheduler, no SQS. Existing Postgres/file sqlite DBs need `due_ready` and `block_reason` on `owner_tasks`. **No execute**, scheduling dispatch, or outbound follow-up this slice.

# 13. Understanding Check and Learning From Assaf

## 13.1 Understanding Check

For new, ambiguous, high-impact or multi-step tasks, Mia must reply before execution with a concise interpretation: goal, intended actions, what she will not do, and at most the genuinely missing knowledge question(s). Routine known tasks execute directly.

### Example

Assaf: Analyze my Instagram this month and tell me what I should focus on next week. Do not create content.

Mia: What I understood: analyze this month's performance, identify themes/formats associated with stronger engagement or lead signals, and recommend what kind of subject to focus on next week. I will not write or generate the post itself. One thing I need to know: should I optimize primarily for reach or lead potential?

**Implementation (2026-08-22):** Ambiguous owner messages with **two or more** keyword matches, missing `lead_id` on typed commands, and unclassifiable **audio** still get a deterministic Understanding Check from `ack_for_owner_task`. Unclassifiable **text** (NOTE, empty `matched_types`) is promoted via `promote_unclassified_text_to_status` to `owner_status` and answered with `format_owner_status_ack` (read-only daily counts, pending approvals, hot leads, Hebrew command menu). Voice (`inbound_source=audio`) keeps the voice-note copy. Plain text Understanding Check does not mention a voice note. Ack copy is **Hebrew** (female voice, masculine address to Assaf); **no execute**; Telegram is not a customer sales inbox.

## 13.2 Learning system

```
Assaf correction / preference
↓
Classify:
one-time | fact | preference | behavior rule | correction
↓
If durable:
propose memory/instruction
↓
Assaf approves
↓
version + activate
```

Raw owner text must not be blindly appended to the system prompt. Durable instructions are normalized, versioned, conflict-checked and scope-limited.

**Implementation (2026-08-21):** Owner WhatsApp text and voice notes classified as durable `preference` are persisted as **proposed** rows in Postgres (`owner_instructions` via `app/domain/learning.py`); `classify_instruction_kind` assigns `preference`, `behavior_rule`, or `correction` (first-match phrase priority; `fact` not this slice). When kind is `correction`, a separate **logged** row is also persisted in `owner_corrections` (`app/domain/feedback.py`; scope `this_turn` or `remember` from phrase classify; R1 `owner_correction_persist`; first write wins; no activation). `list_active_instructions()` returns none this slice. R1 `owner_instruction_propose` before insert; kill switch skips both rows. Kind-specific ack (Hebrew) states the instruction is not active and will not change production prompts. No prompt append, no activation.

# 14. Memory and Knowledge Architecture

| Memory type | Examples | Storage / behavior |
| --- | --- | --- |
| Business facts | services, capabilities, approved pricing rules | Structured DB/KB; versioned. |
| Assaf preferences | summary style, content idea preference, sales tone | Approved instruction store. |
| Lead episodic memory | conversation history, pain, objections, next step | Lead timeline + graph thread. |
| Task memory | follow-ups, campaign watches, commitments | Task table/scheduler. |
| Observed patterns | which sources convert, recurring objections | Analytics hypothesis; not truth until validated. |
| Graph state | current multi-step run | LangGraph checkpointer. |
| Evaluation memory | failures, corrections, reference cases | Versioned eval datasets. |

## Memory write policy

- Do not store every casual statement as a durable preference.
- Facts must have provenance and freshness.
- Owner-approved preferences may affect future behavior.
- Learned correlations are recommendations until confirmed by sufficient evidence.
- Sensitive PII is minimized and subject to retention policy.
# 15. RAG and Source-of-Truth Policy

## 15.1 Source hierarchy

```
SYSTEM SAFETY / PERMISSIONS
>
APPROVED BUSINESS POLICY
>
LIVE STRUCTURED SOURCES
>
APPROVED OWNER INSTRUCTIONS
>
VERSIONED KNOWLEDGE/RAG
>
CONVERSATION CONTEXT
>
UNTRUSTED EXTERNAL RESEARCH
```

## 15.2 Critical fact rules

- Prices, campaign metrics, calendar availability, Gmail message state and lead/deal status must come from live/structured data.
- RAG is appropriate for service descriptions, case studies, FAQ, sales playbooks, security explanations, architecture knowledge and examples.
- Every retrieved chunk includes source_id, type, version, updated_at and trust level.
- If freshness or authority is insufficient, Mia says she cannot verify rather than inventing.
## 15.3 Sales knowledge library

- AssafWeb offers and capabilities.
- Case studies and proof.
- Vertical playbooks.
- Sandler-style deep discovery principles [R38].
- Gong conversation heuristics: conversational discovery and depth [R39].
- MEDDPICC-lite qualification [R40].
- Challenger-style insight/reframe [R41].
- Objection library and approved evidence.
- Security, deployment and integration explanations.
# 16. Instagram and ManyChat Architecture

## 16.1 Instagram capabilities

Instagram professional accounts can expose business/creator messaging, conversations, media, comments and insights through the official API/Composio toolkit [R4, R36]. Conversations are fundamentally user-initiated; Mia must not assume unrestricted cold outbound DM capability.

**Implementation (2026-08-22):** Organic content insights **read** is **alive** via typed `InstagramInsightsPort` (`app/integrations/instagram_insights.py`). Default sender=`direct` uses Graph when tokens are set. Sender=`composio` (or empty Graph tokens with Composio key+user) uses Composio pins `INSTAGRAM_GET_IG_USER_MEDIA` + `INSTAGRAM_GET_IG_MEDIA_INSIGHTS` toolkit `20260819_00` (fields `id,media_type` only). Owner analytics ack may fetch recent media + per-media insights (`views`, `reach`, `likes`, `comments`, `saved`; missing metrics omitted). `DisabledInstagramInsightsPort` when neither path is configured. Postgres `content_insights` (unique `media_id`) + lead_signals from canonical ATTRIBUTION `ig_content_id` exact match; Sheets tab `07 Content Performance` mirror on owner path. R0 `instagram_insights_read`; R1 `content_insight_persist`. `instagram_content_metrics` freshness stamps `tool_runs.freshness` on the `instagram_insights` outcome (audit only; ack unchanged). No publish, comments write, captions, permalinks, or media URLs in DB/sheets/logs/ack. DM send via `ComposioInstagramPort` (`INSTAGRAM_SEND_TEXT_MESSAGE`) only when `MIA_INSTAGRAM_SENDER=composio`. Inbound stays Meta HMAC — Composio has **no inbound DM trigger**.

## 16.2 ManyChat's role

ManyChat is used as an optional Instagram engagement acquisition layer—not Mia's reasoning engine. Official ManyChat capabilities include post/reel comment triggers, story mention replies, share-to-DM, keywords, a Follow-to-DM beta for eligible accounts, and external HTTPS requests [R12-R16]. **Implementation (2026-08-21):** `POST /v1/manychat/external-request` verifies `Authorization: Bearer` against `MIA_MANYCHAT_INGEST_TOKEN` (timing-safe equality; ManyChat documents no native request HMAC); ingest-only with `DisabledMessagePort`; Dynamic Block v2 text reply only when `MIA_INSTAGRAM_SENDER=manychat`. **Parse (2026-08-21):** `parse_manychat_item` requires non-empty `event_id` (rejects payloads without it); sets `subscriber_id`, `conversation_id`, and `thread_id` on inbound item so `CanonicalEvent.conversation_id` uses ManyChat conversation id when present; identity remains channel+subscriber external_id; optional `ad_id`/`campaign_id`/`post_id`/content/ref/trigger fields map through `sanitize_instagram_attribution` (names, media URLs, and ad titles dropped; same `{lead_id}:attribution` path as Graph inbound). **Identity persist (2026-08-21):** `channel_identities.manychat_subscriber_id` + `manychat_conversation_id` stamped on ManyChat prospect inbound only (`LeadStore.stamp_manychat_identity`; first-write-wins; sanitized `[A-Za-z0-9._-]+`; Graph IG inbound does not stamp); migration `migrations/20260821_manychat_identity_ids.sql`. **Contract fixtures (2026-08-21):** sanitized `external_request_ad.json`, `external_request_story.json` (`STORY` + `ig_content_id`), and `external_request_comment.json` (generic text; no invented trigger); enter-once webhook tests in `tests/unit/test_manychat.py`; Meta Conversation Routing still external; dual-send policy-only.

- ManyChat trigger collects engagement event/contact context.
- ManyChat External Request calls Mia's signed endpoint.
- Mia returns structured response/action intent.
- ManyChat renders/sends only within the configured flow, or tags/hands off based on channel ownership policy.
- ManyChat AI Step is not used as Mia's primary reasoning system because it would bypass Mia's model router, memory, evals, sales state and audit controls [R11].
- Follow-to-DM is beta/eligibility-dependent and must be feature-flagged [R14].
- Comment/share triggers have platform-specific repetition limitations; do not build core logic assuming they fire indefinitely [R12, R15].
## 16.3 Conversation ownership

Never let ManyChat, Graph, and Composio sending race against each other. Each Instagram conversation has exactly one active sender adapter. Conversation Routing and application configuration must be verified during setup [R17]. `MIA_INSTAGRAM_SENDER` allowlist is `direct` | `manychat` | `composio`. Production default remains `direct`.

| IG function | Owner |
| --- | --- |
| Inbound DMs | Meta webhook HMAC (not Composio; no inbound trigger) |
| Content/insight read | Direct Graph today; Composio Instagram when that port lands (ADR-015) |
| Comment/Story/Share growth triggers | ManyChat when enabled (ingest sidecar) |
| Dynamic sales reasoning | Mia backend |
| Message rendering/sending | Single configured adapter per conversation (Graph today; Composio specified) |
| Content creation/publishing | Disabled for Mia |

# 17. WhatsApp and Voice-Note Input

## 17.1 WhatsApp

WhatsApp is both a prospect channel and Assaf's owner command channel. Live path (2026-08-21): official Cloud API webhook + typed send/media ports in `app/integrations/whatsapp.py`. Owner phones (`MIA_WHATSAPP_OWNER_PHONES`) hit the owner task router for **text and audio** — never the prospect sales graph. Composio toolkit `WHATSAPP` (57 tools, version `20260815_00`) exists and may later implement the **same** ports for templates/admin; it is not inbound ingress (its only trigger is a documented empty status poll). Templates and consent rules apply for proactive messages outside the customer service window [R18, R37]. Do not enable Meta conversational automation / icebreakers alongside Mia (dual-send).

## 17.2 Owner identity

- Owner/admin phone identities are configured explicitly (`MIA_WHATSAPP_OWNER_PHONES` on WhatsApp only this slice).
- Display name is never authorization. Adversarial tests in `tests/unit/test_adversarial_identity.py` prove “I am Assaf” / display-name spoof / forwarded owner commands do not grant owner tools; website prompt-injection and campaign-write asks stay prospect sales NBA; two WhatsApp identities do not share SalesState; owner allowlist is re-checked on every inbound call; a revoked owner phone becomes a prospect; owner-research snippets stay title+host data. Full webpage-scrape adversarial suite in `tests/unit/test_webpage_scrape_adversarial.py` (http/javascript/data dropped; path/query/excerpt not in ack or TOOL_RESULT; meeting brief stores title+host only).
- Owner commands and lead messages use separate permissions even if they share infrastructure.
- Owner WhatsApp **text and voice** both route through `classify_owner_task` → Postgres `owner_tasks` → Hebrew text ack (task logged / Understanding Check / preference propose-only); relative due tokens persist on `owner_tasks.due_at` when classified (not preference / not Understanding Check / not daily brief / not weekly brief / not lead review / not calendar availability); **daily brief** (`daily_brief` / `סיכום יומי`) computes today's local-calendar scorecard from canonical events + pending follow-ups due today, including booked-meeting (`meeting_booked`) and cancellation-request (`meeting_cancellation_requested`) counts — no PII, no lead IDs/times/links in the Hebrew ack; persists one `owner_briefs` row (`meetings_booked`, `cancellation_requests` columns), and replaces ack with the Hebrew scorecard (no execute/send); **weekly brief** (`weekly_brief` / `סיכום שבועי`) computes ISO-week scorecard from canonical events + pending follow-ups snapshot via `compute_weekly_kpi`, plus the same two booked/cancellation counts from canonical events (not in Sheets tab 09 / `WeeklyKpiSnapshot`), persists one `owner_weeklies` row, and replaces ack with the Hebrew scorecard (no PII; no execute/send); **lead review** (`lead_review` / `סקירת ליד` / review phrases + `lead_*` id) returns a sanitized pipeline snapshot (stage, fit, pain, next_action, MEDDPICC gaps, follow-up/meeting/deal/kill flags only), persists one `lead_reviews` row per lead, and replaces ack with the Hebrew snapshot (no PII; no execute/send); **calendar availability** (`calendar availability` / `check my calendar` / `מועדים פנויים` / `מה פנוי ביומן` and related phrases) reads free/busy via typed `CalendarPort`, applies ADR-012 policy carving (Sun–Thu 09:00–17:00 `Asia/Jerusalem`, 24h notice, max 3 slots), replaces ack with numbered Hebrew times and `לא יוצרת פגישה` — never prospect `השב 1, 2 או 3`; R0 `calendar_read`; canonical `TOOL_RESULT` `calendar_find_free_slots` (`tool`/`status`/`result_count` only; no slot times in events/logs); kill switch keeps generic logged ack and persists denied; demo skips port; no create, no busy-event dump, no due_at; **no sales graph, no send, no execute** this slice. Transcripts are stored only for voice notes. Hebrew classify tokens expanded (conservative phrases for preference, sales, analytics, research, support, meeting debrief, daily brief, weekly brief, lead review, calendar availability); still deterministic keyword match — **no LLM classify, no execute**.
## 17.3 Voice notes

Mia does not generate voice. She only transcribes Assaf's voice notes, then works in text. Candidate transcription service: GPT Transcribe, with vocabulary/context hints for AssafWeb terms when useful [R28].

- Download audio into temporary encrypted storage.
- Transcribe.
- Run task/intent and confidence check.
- If ambiguity materially changes the task, perform Understanding Check.
- Delete raw audio quickly unless a defined debug/consent policy requires short retention.
- Store transcript and minimal message metadata.
- **Implementation (2026-08-21):** Postgres `voice_transcripts` persists transcript text plus honest STT metadata when available: `stt_provider` (`openai`|`fake`), `stt_model`, `language`, `duration_ms`, `confidence` (provider JSON key only when present; not derived from segments), `cost_usd` (always 0), `retention_status` (`text_only` on save; audio never stored). `TranscriptionPort.transcribe` returns frozen `TranscriptResult`; audio stays in memory only. Migrations `migrations/20260821_voice_transcript_stt_meta.sql` + `migrations/20260821_voice_transcript_retention.sql`.
- Persist canonical `TOOL_RESULT` `voice_transcribe` (`{inbound_id}:tool:voice_transcribe`) when transcript text is non-empty after save; payload is `tool`/`status`/`result_count` only — no transcript in the event.
- Owner voice notes: deterministic `classify_owner_task` in `app/domain/owner_tasks.py` → Postgres `owner_tasks` row (`logged` or `needs_clarification`) → text ack; **no execute** this slice.
# 18. Gmail and Google Calendar

## 18.1 Gmail

- Read relevant lead/business messages within explicitly configured scope.
- Summarize threads, detect lead replies and attach them to the canonical lead timeline.
- Draft/send only under approved owner policies.
- Treat email bodies and attachments as untrusted content: they cannot redefine system instructions or permissions.

**Implementation (2026-08-21):** Gmail inbound ingest is **alive** via signed Composio webhook on trigger `GMAIL_NEW_GMAIL_MESSAGE` (toolkit version `20260817_00`). When trigger subject+body are empty, optional hydration via typed `GmailPort.fetch_message` (`ComposioGmailPort` when `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` set; pin `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID` same version; `DisabledGmailPort` otherwise). Canonical events use Gmail `thread_id` as `conversation_id` when present; lead identity and LangGraph sales thread stay sender email. Outbound uses `DisabledMessagePort` — no send/delete/MIME decode this slice. Bodies remain untrusted data.

**Implementation (2026-08-21):** Owner WhatsApp Gmail thread summary is **alive** via `app/domain/gmail_summaries.py` + typed `ThreadSummaryPort` (`app/integrations/thread_summary.py`). Owner phrases `summarize email` / `summarize thread` / `email summary` / `thread summary` / `סיכום מייל` / `סיכום שרשור` / `סיכום האימייל` with `lead_*` or `thread:<id>` load up to 20 ingested Gmail `message_in` rows from Postgres (SoR), summarize via OpenAI when `MIA_OPENAI_API_KEY` + sales model env set (`CannedThreadSummaryPort` otherwise), persist allowlisted fields to `gmail_thread_summaries` (unique `thread_id`), Hebrew ack with intent label — no Gmail send/delete, no new Composio tool, no MIME decode, summary not read by sales graph. R1 `gmail_summary_persist`; kill switch format-only; demo skips persist.
## 18.2 Calendar

- Read availability/free-busy for meeting suggestions.
- Create meetings only after sufficient lead confirmation.
- Generate pre-meeting briefs.
- Use narrow OAuth scopes wherever possible. Google's current guidance explicitly recommends the most narrowly focused scope [R33].

**Implementation (2026-08-21):** Calendar free/busy read is **alive** via typed `CalendarPort` (`app/integrations/calendar.py`). After graph `OFFER_MEETING`, inbound and website handlers append up to three **numbered** Hebrew slot options from `prepare_meeting_offer` / `find_free_slots`; exact stored offers persist on `meetings.offered_slots_json`. Owner WhatsApp **calendar availability** is **alive** via `app/domain/owner_calendar.py`: exclusive classify phrases (`calendar availability`, `check my calendar`, `מועדים פנויים`, etc.) trigger read-only `apply_owner_calendar` — same `CalendarPort` + ADR-012 `carve_policy_slots`, numbered Hebrew ack with `לא יוצרת פגישה` (no prospect confirmation copy), R0 `calendar_read`, canonical `TOOL_RESULT` `calendar_find_free_slots`; kill switch keeps generic logged ack + denied; demo skips port; no create, no busy-event list, no `due_at`. Live HTTP uses Composio execute `GOOGLECALENDAR_FIND_FREE_SLOTS` (toolkit version `20260812_00`) when both `MIA_COMPOSIO_API_KEY` and `MIA_COMPOSIO_USER_ID` are set; otherwise `DisabledCalendarPort` (no HTTP). Duration filtering happens in our code (`_slot_fits_window`); Composio free lists are unfiltered. R0 `calendar_read` before port call; kill switch degrades to static copy.

**Calendar create (2026-08-21, ADR-011):** Separate typed `CalendarBookingPort` (`app/integrations/calendar_booking.py`). **Alive by fake** in tests; live Composio when same credentials set **and** `MIA_CALENDAR_WRITE=true` **and** Calendar write OAuth scope authorized on connected account (operator must verify). Before each offer fetch, inbound/website attempt booking on stored offers when prospect replies with exact `1`/`2`/`3`, `slot N`, `option N`, or Hebrew ordinals only — no “yes”, date parsing, or LLM. R2 `calendar_create` with `in_approved_scope=True` after valid selection; kill switch and `MIA_CALENDAR_WRITE=false` deny before recheck/lookup/create (`named_write_may_auto` cannot override R4/R5). Conflict recheck via read port on exact 30m window; CREATE performs no conflict check. Idempotency: `GOOGLECALENDAR_EVENTS_LIST` with `privateExtendedProperty=mia_booking_key=<sha256>` then `GOOGLECALENDAR_CREATE_EVENT` with PII-free args (generic summary, private visibility, Meet room, no attendees/description). Canonical `MEETING_BOOKED` (`{lead_id}:booked`; payload `status`+`scheduled_at` UTC only). `TOOL_RESULT` tools `calendar_booking_lookup`, `calendar_create`. Never store htmlLink; Meet link only when host is `meet.google.com`.

**Calendar reschedule and cancellation request (2026-08-21, ADR-013):** **Alive by fake.** Whole-message exact bilingual parsers only. A booked reschedule request performs R0 availability read, applies ADR-012, and stores up to three numbered slots in `meetings.reschedule_slots_json` without changing the provider. A subsequent exact numbered selection is R2 `calendar_reschedule` AUTO in approved scope only; provider PATCH requires `MIA_CALENDAR_WRITE=true` (reads not gated; flag does not override R4/R5). `CalendarBookingPort.get_event` pins `GOOGLECALENDAR_EVENTS_GET`; cancelled is `not_found`, malformed or missing start/end is `error`. If GET already matches the exact target, local state recovers without PATCH. Otherwise policy and exact conflict are rechecked, then `GOOGLECALENDAR_PATCH_EVENT` receives only `calendar_id=primary`, exact event ID, RFC3339 start/end, IANA timezone, and `send_updates=none`. Mandatory post-PATCH GET is authoritative even after timeout. Local state and canonical `MEETING_RESCHEDULED` persist only on exact ID and UTC interval match. Event ID, Meet link, meeting type, and `booked_at` are preserved. Exact cancellation phrases perform only R1 `calendar_cancellation_request`: status `cancellation_requested`, UTC timestamp, cleared reschedule slots, canonical redacted request, and honest customer copy that Assaf will update the calendar. No Calendar port call, no provider delete, and repeats are idempotent. Provider delete remains R5 denied. Existing DBs need the three columns in `migrations/20260821_adr013_calendar_gate2.sql`.
# 19. Google Sheets Business Control Layer

Postgres remains the system of record. Mia creates and maintains a Google Sheet for Assaf's visibility and campaign control. Sheets supports create/read/write/format operations via API [R34].

**Implementation (2026-08-21):** Lead, deal, meeting, source, follow-up, activity, weekly KPI, content performance, and campaign budget/performance mirrors are **alive** via typed `SheetsPort` (`app/integrations/sheets.py`). On website session create, when sanitized attribution is persisted, Mia upserts `06 Lead Sources` (Lead ID key; UTM source/medium/campaign/content, landing, referrer — no PII; no spend). After owner analytics ack with Instagram content insights, Mia upserts all rows to `07 Content Performance` (Media ID key; type + metric counts + lead_signals from Postgres `content_insights`; no media URLs or captions). After owner analytics ack when `MIA_CAMPAIGN_MONTHLY_BUDGET` is set (user-configured; never inferred from spend), Mia upserts `02 Campaign Budget` and `03 Campaign Performance` from Postgres `campaign_pacing` / `campaign_performance` (Campaign key; empty env skips 02/03). After website session create and after each sales graph turn on inbound (prospect path) and website, Mia upserts the current ISO Monday week row to `09 Weekly KPI` (Week Start key; counts from canonical events in that week plus current pending follow-up snapshot — leads, meetings offered, handoffs, messages in, follow-ups pending; no spend, no lead IDs, no PII). After each sales graph turn on inbound (prospect path) and website, Mia upserts a snapshot to `01 Leads`, current `04 Meetings`, `05 Deals`, `08 Follow-ups`, and `10 Mia Activity`. Meeting status may be `offered`, `booked`, or `cancellation_requested`; offered rows have empty time/event fields, while booked and cancellation-requested rows may mirror the existing UTC time and provider event ID. `04 Meetings` never contains a Meet link. Follow-up result may be `meeting_offered` or verified-booking closure `meeting_booked`. No email, phone, or message text. Live HTTP uses Composio execute `GOOGLESHEETS_UPSERT_ROWS` (toolkit version `20260813_00`) when configured; otherwise `DisabledSheetsPort`. Never read sheet data back into SalesState or graph state. R1 `sheets_mirror` before write; kill switch skips. Demo skips tabs 01–10. Persist is also gated by `claim_operation(scope=sheets_mirror, key={inbound_id}:sheets:{sales|session|campaign|content})` then `complete_operation` `{"ok": true}` — failed webhook reclaim of the same inbound skips a second upsert; a later inbound for the same lead still refreshes the snapshot. Do not claim per lead.

## 19.1 Required tabs

| Tab | Purpose |
| --- | --- |
| 01 Leads | Lead ID, source, stage, fit, score summary, next action. |
| 02 Campaign Budget | Campaign, monthly budget, spend, pacing, remaining, projected over/under. **Alive** (mirror of Postgres `campaign_pacing` on owner analytics path when `MIA_CAMPAIGN_MONTHLY_BUDGET` set; user-configured budget only; missing spend empty, never zero-filled; no Meta writes). |
| 03 Campaign Performance | Spend, CTR, CPC, CPL, qualified CPL, meetings, deals, revenue, ROAS. **Alive** (mirror of Postgres `campaign_performance`; Meta `this_month` insights + canonical event counts; revenue/ROAS/qualified CPL always empty this slice; no attributed revenue invented). |
| 04 Meetings | Lead, time, source, meeting status, summary. **Alive** (mirror of `meetings`; `offered` has empty time/event fields; `booked` and `cancellation_requested` may include existing UTC time and provider event ID; summary empty; no Meet link or PII). |
| 05 Deals | Stage, expected value, closed value, source, attribution confidence. **Alive** (mirror of `deals`; stage/source/confidence only; expected/closed value always empty strings; no PII). |
| 06 Lead Sources | UTM/content/campaign source performance. **Alive** (mirror of canonical ATTRIBUTION on website session create; Lead ID key; sanitized UTM/landing/referrer only; no PII; no spend). |
| 07 Content Performance | Own Instagram content insights and lead signals. **Alive** (mirror of Postgres `content_insights` on owner analytics insights path; Media ID key; metric counts + lead_signals from canonical ATTRIBUTION `ig_content_id`; no media URLs/captions). |
| 08 Follow-ups | Due date, channel, status, result. **Alive** (mirror of `lead_follow_ups`; result = reason, no PII). |
| 09 Weekly KPI | Weekly operational scorecard. **Alive** (mirror of canonical event counts for ISO Monday week in `MIA_CALENDAR_TIMEZONE` plus current pending follow-up snapshot; Week Start key; no spend, no lead IDs, no PII). |
| 10 Mia Activity | High-level audited actions, cost and exceptions. **Alive** (mirror of `ai_runs`; Run ID key; date in `MIA_CALENDAR_TIMEZONE`; includes `policy_version=fde_v1` and `prompt_version=sales_reply_v1`; `decision_confidence` stays on Postgres only this slice; no PII; no prompt/reply). |

## 19.2 Budget pacing

Campaign budget is user-configured; Mia must never infer an approved spend limit. Code computes expected spend-to-date, remaining budget and projected month-end spend. Mia interprets the result and recommends action.

**Implementation (2026-08-21):** Campaign pacing **alive** via `app/domain/pacing.py`. When `MIA_CAMPAIGN_MONTHLY_BUDGET` parses (>0, digits/decimal only), owner analytics ack fetches Meta `this_month` spend (R0 read), computes pacing (`expected = budget × elapsed_days/days_in_month`; `projected = spend/fraction`; status on_track/over/under/uncertain), persists Postgres `campaign_pacing` (R1 `campaign_pacing_persist`), mirrors Sheets tab `02`. Empty env skips 02/03 entirely. Missing spend stays empty — never zero-filled, never inferred from spend. Expected spend-to-date is still computed from the user-configured budget and calendar day even when spend is missing (status `uncertain`). Hebrew pacing line on ack (`קצב:` + status). No Meta writes, no pause/budget recommendations. `MIA_CAMPAIGN_NAME` optional sheet key (default `account`).

# 20. Meta Ads and Campaign Intelligence

Composio currently exposes Meta Ads insights and campaign/ad operations, but its managed app is not available for Meta Ads; we therefore plan for Assaf-owned Meta credentials and an adapter that can later move to direct Marketing API if needed [R5].

**Implementation (2026-08-21):** Insights **read** is alive via typed `MetaAdsPort` (`app/integrations/meta_ads.py`). Owner WhatsApp analytics acks may append a one-line `get_insights` snapshot after R0 `meta_ads_read`. Live Composio HTTP execute when `MIA_COMPOSIO_API_KEY`, `MIA_COMPOSIO_USER_ID`, and `MIA_META_ADS_ACCOUNT_ID` are set (`ComposioMetaAdsPort`); `DisabledMetaAdsPort` otherwise; `FakeMetaAdsPort` proves the path. Pin Composio `METAADS_GET_INSIGHTS` toolkit `20260731_00`. Missing metrics omitted, never zero-filled. No create/update/delete/pause. R4 writes stay approval-gated.

## 20.1 Data model

- Campaign → ad set → ad/creative.
- Spend, impressions, reach, CPM, clicks, CTR, CPC, frequency.
- Leads and qualified leads.
- Meetings and deals.
- Attributed revenue and attribution confidence.
- CPL, qualified CPL, CPA/meeting, CPA/deal, ROAS where revenue is available.
## 20.2 Mia analysis

- Compare today vs baseline, 7d vs previous 7d, 30d vs previous period.
- Detect anomalies: spend without leads, CPL spike, creative fatigue, sharp conversion deterioration, website funnel drop.
- Separate cheap leads from valuable leads.
- Explain uncertainty when attribution is incomplete.
- Recommend actions; require approval for write actions.

**Implementation (2026-08-21):** Campaign analysis **recommend + persist-only** (`app/domain/campaigns.py`). Owner analytics reads `last_7d`, the previous 7 local-calendar days through Composio `METAADS_GET_INSIGHTS` `time_range` only, and 30-day windows only while the result remains `watch`; `date_preset` and `time_range` are never sent together. The same pinned tool includes official `frequency`. Missing metrics and counts are never zero-filled.

Priority is deterministic: incomplete metrics; spend without clicks; 7d spend-up/clicks-down; Postgres spend-without-leads; CPL spike; creative fatigue (frequency up and CTR down); website funnel drop; then 30d spend-up/clicks-down. The leads/CPL path still runs when the first pass is creative fatigue so the higher-priority lead anomalies win.

Website funnel drop uses `LeadStore.count_behavior_events` for `mia_opened` and `conversation_started`. The method allowlists behavior kinds, filters `event_type=behavior` and bounded dates in SQL, then counts exact payload kinds in Python; behavior is **not** added to `KPI_EVENT_TYPES`. The single Bible-aligned anomaly is `website_funnel_drop`: current opens>0 with zero starts, or current opens>previous opens while current starts<previous starts. Counts must be known and nonnegative; there is no invented conversion threshold. Hebrew output is generic to either cause and recommends investigation only. A funnel anomaly skips 30d fetch.

Recommendations persist one account-level `campaign_recommendations` row plus canonical `CAMPAIGN_RECOMMENDATION` (`meta:campaign:recommendation`; `kind`+`anomaly` only; first write wins). R1 policy applies; kill switch skips persist. **No Meta writes**, pauses, budget changes, or launch actions. Displayed snapshot remains `last_7d`. Qualified CPL, revenue, and ROAS remain empty when unavailable.

**Today-vs-baseline (2026-08-21, ADR-008):** **alive** read-only comparison appended after recommendation when settings exist. Baseline = previous seven completed local-calendar days (D-7..D-1); fetches `date_preset="today"` and baseline `time_range` only (never together). Additive metrics show today vs 7-day daily average; CTR compares aggregate ratios without ÷7. Missing paired metrics omitted; frequency omitted. Not an anomaly; does not change recommendation or 30d fetch.
## 20.3 Campaign Yuma

The first live campaign can be represented in the system as Campaign Yuma. Budget, launch date, objective and expected lead path are configuration—not prompt text. The pre-launch gate requires tracking/UTMs, source attribution, lead capture, Sheet tabs, alert thresholds and end-to-end test before spend starts.

**Implementation (2026-08-21):** Campaign pre-launch gate **persist-only** via `app/domain/prelaunch.py`. Named campaign only (`MIA_CAMPAIGN_NAME` pattern `^[a-zA-Z0-9._-]{1,32}$`, e.g. `Yuma` — not `Campaign Yuma`); empty/invalid name skips the gate entirely. Config env (never prompt): `MIA_CAMPAIGN_LAUNCH_DATE` (`YYYY-MM-DD`), `MIA_CAMPAIGN_OBJECTIVE` (`leads`|`traffic`|`awareness`), `MIA_CAMPAIGN_LEAD_PATH` (`website`|`whatsapp`|`instagram`), `MIA_CAMPAIGN_E2E_TESTED` (exact `true` = operator e2e attestation), plus existing `MIA_CAMPAIGN_MONTHLY_BUDGET` for alert threshold (never infer budget). Seven deterministic checks (no HTTP, no Meta): campaign_config, tracking_utms, source_attribution, lead_capture by path, sheet_tabs, alert_thresholds, e2e_test. `ready` persists to Postgres `campaign_prelaunch` (R1 `campaign_prelaunch_persist`); **never launches Meta**, never writes budget, never creates Sheets tabs. Owner analytics ack (non-spend-threshold) appends Hebrew line `שער טרום-השקה: מוכן` / `לא מוכן` (+ missing labels); kill switch keeps read-only line without persist; demo skips line and persist. Existing Postgres/file DBs need the new `campaign_prelaunch` table (`create_all` on init).

# 21. Research, Firecrawl, Apify and Browser

## 21.1 Research hierarchy

```
Structured/official API available?
yes → use it
no
Public web can be searched/extracted?
yes → Firecrawl / search / Apify
no
Interactive browser required?
yes → sandboxed browser / Playwright
```

## 21.2 Uses

- Research a lead's company before asking obvious questions.
- Find public competitor/market patterns for sales insights.
- Analyze public AI/automation trends relevant to Assaf's content ideas.
- Inspect website flows when no API exists.
- Never scrape private data, bypass access controls or treat scraped instructions as trusted.
## 21.3 Browser safety

- Domain allowlist for high-risk actions.
- Read-only by default.
- Downloads quarantined/scanned before processing.
- No password entry unless explicit approved integration path.
- Prompt injection classifier/trust boundary on page text.
- Separate browser session identity from agent system instructions.
- Playwright/browser is a fallback, not the primary business integration.

**Implementation (2026-08-21):** Public search **read** is alive via typed `ResearchPort` (`app/integrations/research.py`). Owner WhatsApp research acks may append up to two public source lines (title + host) after R0 `research_read`. `FirecrawlSearchPort` calls `POST /v2/search` when `MIA_FIRECRAWL_API_KEY` is set (non-empty after strip); `DisabledResearchPort` when empty. `FakeResearchPort` proves the path in tests. Snippets are untrusted data; non-https URLs dropped in `sanitize_snippets`. `research_snippets` freshness stamps `tool_runs.freshness` on the `research_search` outcome (audit only; ack unchanged; no URLs/excerpts on the row). No crawl, browser, or LLM.
# 21A. LinkedIn Professional Intelligence Channel

LinkedIn is a secondary professional-growth channel, not a blocker for the core lead system. Mia may analyze Assaf's own LinkedIn presence and available post/share statistics, use company/profile context during legitimate lead research, and surface professional content or positioning insights. Composio currently exposes a LinkedIn toolkit with managed authentication; LinkedIn Ads is a separate credentialed surface [R42-R43].

## 21A.1 Allowed uses

- Analyze Assaf's own post/share performance and professional audience signals when available.
- Use company/public professional context to avoid asking prospects obvious questions and to improve meeting briefs.
- Suggest themes, positioning gaps and sales-relevant insights; Mia does not write/publish Assaf's final content.
- Attach LinkedIn-origin interactions or attribution to the canonical lead timeline when a reliable identifier/source is available.
## 21A.2 Boundaries

No autonomous posting, connection spam, scraping behind access controls or bulk DM outreach. If LinkedIn Ads is later enabled, it follows the same read-first, approval-gated write policy as Meta Ads.

**Implementation (2026-08-21):** Own-profile **read** is alive via typed `LinkedInPort` (`app/integrations/linkedin.py`). Owner WhatsApp linkedin acks may append one profile line (name + headline) after R0 `linkedin_read`. Live Composio HTTP execute via `ComposioLinkedInPort` when `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` set (LinkedIn connected in Composio); `DisabledLinkedInPort` otherwise; `FakeLinkedInPort` proves the path. Composio pin `LINKEDIN_GET_MY_INFO` on toolkit `20260724_00`. Personal member post analytics **read** is alive via separate typed `LinkedInAnalyticsPort` (`app/integrations/linkedin_analytics.py`; ADR-009). Owner linkedin acks may append one Hebrew stats line (six allowlisted metrics; missing omitted) after R0 `linkedin_analytics_read`. Live direct LinkedIn REST when `MIA_LINKEDIN_ACCESS_TOKEN` set (`DirectLinkedInAnalyticsPort`; pin `LINKEDIN_API_VERSION=202608`; `GET /rest/memberCreatorPostAnalytics`; `q=me`; previous 30 completed local-calendar days); `DisabledLinkedInAnalyticsPort` otherwise; `FakeLinkedInAnalyticsPort` proves the path. Composio `LINKEDIN_GET_SHARE_STATS` is organization-page only — not used for personal analytics. Separate canonical `TOOL_RESULT` tools `linkedin_profile` and `linkedin_analytics`. No post content, post URLs, member IDs, raw metrics response, post, comment, delete, DM, or upload. Live OAuth app approval for `r_member_postAnalytics` is operator action — code alive by mock; production HTTP requires approved token.

# 22. Make Integration Policy

Make AI Agent (New) was released in February 2026 and remains open beta, so it is not used as Mia's core orchestrator [R19]. Make is useful as a low-code sidecar for non-critical workflows, prototypes and operational glue.

## 22.1 Good Make uses

- Non-critical notifications.
- One-off back-office syncs.
- Prototyping a future native integration.
- Scheduled exports or mirror updates where a failure does not lose a lead.
- Connecting a niche app before building a first-class adapter.
## 22.2 Not allowed in Make

- Canonical lead state.
- Core sales reasoning.
- Critical inbound message ordering.
- Source of truth for permissions.
- Irreplaceable campaign attribution.
- High-risk automated ad changes.
## 22.3 Integration contract

Make calls Mia only through signed HTTPS endpoints. Mia calls Make only through scoped scenario webhooks/API credentials. Every Make-triggered write is idempotent. Make's webhook queues, parallel execution, rate behavior and error handlers must be considered explicitly [R20-R21].

# 23. Composio Tool Architecture

Composio is the primary tool supplier because it currently exposes 1326 toolkits and runtime meta-tools for discovery/auth/execution [R2-R3] (catalog revalidated 21 August 2026; was 1181 in Bible v1.1). It must still sit behind our interfaces.

## 23.1 Adapter rule

```
LangGraph / Domain
↓
Typed capability interface
↓
Provider adapter  ← production map ADR-015; rubric ADR-007
├─ Composio       ← Gmail, Calendar, Sheets, LinkedIn profile, Meta ads read, future IG send/insights
├─ Direct API     ← WhatsApp + IG inbound webhooks; LinkedIn member analytics; Firecrawl; STT
├─ ManyChat       ← optional IG sidecar, never the brain, never a second sender
└─ Make           ← optional non-critical glue
```

Do not load a toolkit catalog into the model. Pin production tool slugs. Production jobs follow **ADR-015**. Assaf is not asked to re-choose Composio vs direct unless the pick changes safety, permissions, or one-sender rules.

## 23.2 Version policy

Development may evaluate latest toolkit versions, but production-critical tool schemas are pinned to a tested version or compatibility contract. Composio's 2026 changelog includes consolidations/renames and latest-version behavior changes, so silent runtime schema drift is unacceptable [R10].

## 23.3 ManyChat caveat

Composio's changelog lists MANY_CHAT among deprecated toolkits with no supported actions at that time; therefore ManyChat must be integrated directly via its supported APIs/external requests rather than through Composio [R10].

> Owner note from source (not a Bible requirement): assaf has many chat premium maybe can be log to his user for assaf_playground ig

## 23.4 Custom tools

Use Composio custom/extension tools only when they preserve clean domain contracts; they are not a substitute for testable business logic.

## 23.5 MCP Interoperability and Tool Trust

MCP is an optional interoperability boundary, not a requirement for every integration. It is useful when an approved provider exposes a stable MCP server or when AgentCore Gateway is used to normalize tool access. Arbitrary MCP servers are never auto-installed or granted production permissions simply because the model discovers them [R31, R35].

### MCP policy

- Maintain an allowlist of approved MCP servers, versions and permitted tools.
- Authenticate at the gateway/adapter boundary; secrets never enter model-visible text.
- Convert MCP outputs into typed domain contracts before they enter LangGraph state.
- Run the same risk/approval policy whether a tool came from MCP, Composio or a direct API.
- Treat provider-preview MCP capabilities as feature-flagged until contract, auth, latency and failure behavior are tested.
# 24. Model Router and Cost/Quality Policy

No single model is hard-coded as Mia's permanent brain. The model router selects from an approved registry by task type, risk, latency, context length, current benchmark and cost.

## 24.1 Initial candidate set

| Task class | Initial candidate | Reason |
| --- | --- | --- |
| Normal sales conversation | GPT-5.6 Luna | Cost-sensitive/high-volume candidate with function/tool support [R27]. |
| Simple extraction/routing | GPT-5.6 Luna at low/no reasoning or cheaper approved model | Minimize cost while preserving schema accuracy. |
| Deep research / long-horizon analysis | Grok 4.6 | Positioned for long-running agentic and research work [R29]. |
| Complex campaign reasoning | Luna vs Grok 4.6 benchmark winner | Decision by eval, not preference. |
| Voice transcription | GPT Transcribe | Dedicated high-accuracy STT [R28]. |
| Critical verifier | Independent approved model/config | Avoid same-model self-grading when risk justifies. |

## 24.2 Pricing accuracy rule

Provider prices are dynamic and must live in configuration/telemetry, not the prompt or code constants. Before launch, the build process rechecks official pricing. If provider pages disagree or a new tier appears, use live provider billing telemetry and the specific API model page as the implementation source of truth.

## 24.3 Router objective

```
minimize expected cost
subject to:
quality ≥ task threshold
latency ≤ task SLO
risk policy satisfied
context supported
tool/structured-output capability supported
```

**Implementation (2026-08-21):** Live LLM paths are sales paraphrase (`OpenAISalesReplyPort`) and STT (`OpenAITranscribePort`). Sales tries OpenAI `model_chain(MIA_SALES_MODEL, MIA_SALES_FALLBACK_MODEL)` then Gemini AI Studio OpenAI-compat (`MIA_GEMINI_API_KEY` + `MIA_SALES_GEMINI_MODEL`) once; ids are not hard-coded. Sales then canned; STT then `TranscriptionError`. Kill switch skips sales HTTP. NBA/extract stay in code. No Grok/research LLM this slice.

# 25. LangGraph Architecture

LangGraph is used because Mia is stateful, multi-channel, has human approval points, durable commitments, retries and explicit subflows. Its persistence/checkpointing and interrupts directly support these requirements [R22-R24].

## 25.1 One orchestrator, specialized subgraphs

```
MIA ORCHESTRATOR
│
┌───────────────┼────────────────┐
│               │                │
SALES          OWNER OPS         ANALYTICS
│               │                │
discovery        tasks/brief       Meta/IG
qualify          Gmail/Cal         website
objections       learning          budget
│               │                │
└───────────────┼────────────────┘
│
RESEARCH
│
TOOL / POLICY LAYER
```

## 25.2 No unnecessary multi-agent swarm

Subgraphs are favored over autonomous agent-to-agent chatter. A separate agent is created only when independent context, permissions, tools or evaluation goals justify it.

# 26. Graph State, Nodes, Edges and Subgraphs

## 26.1 Canonical graph state fields

- run_id, thread_id, business_id, actor_id, actor_role.
- channel, conversation_id, lead_id, source/attribution context.
- latest_message, normalized_input, language.
- sales_state summary and missing-information map.
- retrieved_context with source/trust/freshness.
- task intent, planned actions, risk level.
- proposed tool calls, tool results and validation.
- approval state.
- response draft, response validation, final response.
- model decision metadata and cost.
- errors/retry state.
## 26.2 Core nodes

| Node | Responsibility |
| --- | --- |
| normalize_input | Channel payload → canonical input; voice transcript when needed. |
| resolve_identity | Map to canonical person/lead. |
| load_context | Lead timeline, owner instructions, channel/source context. |
| classify_task | Sales / owner command / analytics / research / support. |
| understanding_check | Pause if a new high-impact task is ambiguous. |
| research_context | Fetch public/business context only when useful. |
| sales_next_action | Choose ask/answer/reframe/qualify/book/handoff. |
| retrieve_knowledge | RAG/structured sources with trust/freshness. |
| draft_response | Generate channel-aware text. |
| validate_response | Truth, policy, tone, claim/source validation. |
| risk_policy | Determine auto/approval/deny. |
| execute_tools | Call typed adapters. |
| verify_effect | Confirm write succeeded and state matches. |
| persist_outcome | Timeline, task, lead, cost, attribution. |
| owner_notify | Escalation/brief. |

**Implementation (2026-08-21):** `owner_notify` graph node contract is **alive** as persist-only inbox pull: booking, verified reschedule, and first cancellation request persist unseen `owner_notifications` (three allowlisted kinds); owner WhatsApp exclusive phrases deliver Hebrew kind line + `lead_id` + slot time; no proactive outbound; see §12.2 owner meeting notify.
| finalize | Return/send response. |

## 26.3 Edge rules

- No tool write before risk policy.
- No customer response before critical fact validation.
- No approval interrupt after irreversible side effects unless the side effect is idempotent and intentionally placed before the interrupt.
- Research can be skipped when existing context is sufficient.
- If a tool is unavailable/stale, route to safe fallback or owner rather than fabricate.
# 27. Persistence, Interrupts and Durable Execution

Each lead/conversation uses stable thread identifiers. Checkpointing allows continuation, failure recovery and human approval; durable long-term memory lives separately from graph state [R22].

- Use a durable production checkpointer.
- Interrupt payloads contain only JSON-serializable approval context.
- Any operation before an interrupt must be idempotent because LangGraph restarts the node on resume [R23].
- Subgraphs default to per-invocation persistence unless they truly require independent cross-call memory [R24].
- Long-lived commitments are database tasks/events, not sleeping graph executions.
# 28. Mia Graph Lab and Graph Engineering

Production Mia never self-optimizes. Graph Engineering runs locally in the development environment, initially on Assaf's computer/Cursor.

```
Production traces + conversions + corrections
↓
Sanitize / label
↓
Local Mia Graph Lab
↓
Experiment:
prompt | model | route | graph edge | sales policy
↓
Replay versioned eval datasets
↓
Compare quality + cost + latency + business metrics
↓
Human review
↓
Git PR / release gate
↓
Dev → Test → Prod
```

**Implementation (2026-08-21):** Local eval harness alive at `app/evals`: `sales_v1` replays fixture SalesState through `select_next_action` + `reply_for` (one-shot); `buyers_v1` replays 12 §39.1 simulated buyers through extract → NBA → mark → reply with exact-match scoring plus §39.3 weighted Sales Quality Score; `routing_v1` replays 20 isolated Hebrew+English owner voice phrases through `classify_owner_task` with exact-match on `task_type` + `needs_clarification` only (no NBA/reply/judge); `extract_v1` replays 30 isolated Hebrew+English extract cases through `extract_sales_signals` with `_sales_field_matches` on `expect` keys only (no NBA/reply/judge); `objection_v1` replays 20 Hebrew+English cases through extract → NBA → reply with exact-match on `expected_objection`, `expected_action`, reply substring, and `lint_customer_reply` (no judge); `calendar_v1` replays 20 ADR-012 `carve_policy_slots` cases only (no NBA/reply/judge); `campaign_v1` replays 20 `analyze_insights` + `format_recommendation_line` cases only (no NBA/reply/judge); `safety_v1` replays 20 adversarial cases — 12 sales extract→NBA→reply+lint+forbidden plus 8 `sanitize_snippets` URL/title cases (no judge); `writing_v1` replays 8 playbook writing categories in Hebrew and English (discovery, short answer, technical, objection, booking, follow-up, owner report, complaint) plus deterministic anti-pattern lint-fail cases — follow-up via `compose_follow_up_draft`, owner report via `format_daily_brief` (no customer lint; forbidden owner AI phrases only); all sales/buyer canned replies must pass `lint_customer_reply`; no LangSmith, no LLM judge, no graph/prompt mutation.

## 28.1 Version every behavior

- graph_version
- prompt_version
- instruction_policy_version
- sales_playbook_version
- model_router_version
- toolkit/adapter version
- eval dataset version
# 29. AWS Production Architecture

AWS is used to create clear security, eventing and audit boundaries—not merely because Lambda exists. AgentCore is now a serious option for LangGraph-compatible agent runtime/gateway infrastructure and has current security/identity/gateway capabilities [R30-R31].

## 29.1 Recommended logical architecture

```
Internet / Providers
↓
AWS WAF
↓
API Gateway
↓
Lambda ingress
- verify signatures
- validate schema
- persist raw event
- dedupe
- fast ACK
↓
SQS FIFO (message group = conversation/lead)
↓
Agent runtime
- LangGraph
- model router
- policy engine
↓
Typed tool gateway/adapters
↓
Composio / ManyChat / Meta / Google / Research
↓
Postgres + S3
↓
CloudWatch / tracing / audit
```

## 29.2 Runtime decision gate

| Option | When to use |
| --- | --- |
| AgentCore Runtime/Gateway | Preferred candidate if pricing/limits and framework behavior pass benchmark; strong auth/tool governance [R30-R31]. |
| ECS/Fargate | Fallback for predictable containerized LangGraph runtime and full control. |
| Lambda-only agent | Only for short bounded workflows/ingress. Long conversational or research runs go through an async agent runtime; do not force LangGraph durability into Lambda when runtime limits and handoffs become worse. |
| Provider-neutral runtime contract | Required. AgentCore is a candidate, not an architectural dependency; ECS/Fargate or another compliant runtime can replace it without changing domain/graph contracts. |

## 29.2A Database decision gate

Canonical persistence is PostgreSQL. For the first production AWS deployment, choose between Aurora PostgreSQL Serverless v2/RDS PostgreSQL and another managed PostgreSQL deployment only after measuring expected connection behavior, cost, backups, latency and operational burden. Business/domain repositories must hide the infrastructure choice so the graph is not coupled to a database vendor.

## 29.3 Supporting AWS services

- Secrets Manager for provider secrets; KMS encryption and CloudTrail/CloudWatch audit [R32].
- S3 for temporary voice/media/raw event artifacts with lifecycle deletion.
- EventBridge Scheduler for recurring briefs, follow-ups and reconciliation.
- CloudWatch alarms for queue depth, errors, latency and provider failures.
- IAM least privilege for every Lambda/runtime/tool path.

**Implementation (2026-08-22, ADR-014):** First AWS live is ECS Fargate (this FastAPI process) + RDS PostgreSQL 16 + Secrets Manager secret `mia/prod` (the key box; Assaf fills JSON `MIA_*` from `deploy/mia-prod.secret.example.json`; ECS injects env at task start; never git, never chat, never a host `.env`) + ALB/ACM on `https://mia.assafweb.com`. Image includes the RDS global CA; production DSN uses `sslmode=verify-full`. Uvicorn enables `--proxy-headers` because ALB terminates TLS on 443 and forwards HTTP:8000, and `--timeout-keep-alive 130` because ALB idle timeout is 120s (uvicorn default 5s would 502 on reused connections). Prod API lifespan skips `create_all` so ALB `/health/live` is process-up; schema is `mia-migrate` only. ALB templates: internet-facing on **public** subnets; IP target group HTTP:8000 health `/health/live`; HTTPS listener `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`; HTTP:80 `HTTP_301`. ECS service grace **120s** + deployment circuit breaker enable/rollback. Operator order: ACM certificate **ISSUED** → target group → `mia-migrate` → `create-service` → CloudWatch ALB alarms without SNS (`docs/PRODUCTION_BUILD.md` §3). Persist-only EventBridge Scheduler templates exist; create them only after `/health` is green. Lambda is **not** the sales graph and **not** the key box. WAF, API Gateway, SQS FIFO, and AgentCore remain specified for later slices. `CapabilityId.AWS_RUNTIME` stays specified (no `app.infra`; Lambda/SQS/WAF/AgentCore later). Live `/health` on `https://mia.assafweb.com` showed `postgres` + `public_https` on 2026-08-22 (`docs/LIVE_STAGING_ACCEPTANCE_REPORT.md`; selected Region **eu-north-1**, ADR-019). GitHub Actions `ci.yml` runs pytest and `docker build -f deploy/Dockerfile` (no ECR push this slice).
# 30. API and Endpoint Contracts

## 30.1 Endpoint classes

| Class | Examples | Security |
| --- | --- | --- |
| Provider webhook | /webhooks/meta, /webhooks/whatsapp, /webhooks/manychat, /webhooks/composio | Provider signature/HMAC/token + replay/dedupe. |
| Website public | /api/mia/session, /api/mia/message, /api/events | Rate limit, session token, bot/abuse controls, input size validation. |
| Owner authenticated | /api/owner/* | Strong auth; explicit owner role. |
| Internal worker | /internal/* | Private network/IAM/service auth only. |
| Health | /health/live, /health/ready | No sensitive details. `GET /health/live` → `{"status":"ok"}` (no DB). `GET /health/ready` → `{"status":"ok"}` or 503 `{"status":"not_ready"}` (`database_ready`: `SELECT 1` then `schema_ready` vs mapped tables/columns; no DSN or column names in body). `GET /health` remains the operator diagnostic (env/capabilities/risk; `website_chat`, `telegram_owner`, `email_read`, `email_send_policy`, `automation_mode`, `whatsapp_*`, `ops` counts — no secrets, DSNs, user ids, or model ids). `whatsapp_ingest` is the Meta inbound path (verify+app secret), never a Composio API key. `telegram_owner` is true only when bot token, webhook secret, and numeric owner ids are all set. `MIA_ENV=prod` unmounts `/docs`, `/redoc`, `/openapi.json`. |

## 30.2 Webhook contract

- Authenticate before expensive processing.
- Store provider event ID/raw sanitized payload.
- Deduplicate before enqueue.
- Return fast acknowledgement.
- Never expose stack traces/provider secrets.
- All external payloads pass Pydantic/schema validation.
# 31. Eventing, Queues, Idempotency and Reconciliation

## 31.1 Ordering

Use per-conversation message grouping so two rapid messages from the same lead do not race and produce contradictory replies. Different leads may process in parallel.

## 31.2 Idempotency keys

- provider_event_id for inbound webhooks.
- channel_message_id for outbound message send.
- approval_id + action hash for approved writes.
- scheduled_task_id for follow-up executions.
- sheet_sync_version for mirrors.
## 31.3 Reconciliation

Do not rely exclusively on webhooks. Scheduled reconciliation checks stale transitional records (message delivery, calendar write, campaign sync, Sheet mirror) and repairs/flags mismatches.

**Implementation (2026-08-21):** Flag-only local CLI `mia-reconcile` (`app/workers/reconcile.py` / `app/domain/reconciliation.py`); three checks — stale `webhook_events` in `received` (missing/unparseable `claimed_at` or older than 300s), `sent` webhooks without canonical `{provider_event_id}:out`, expired unconsumed `handoff_tokens`; upserts open rows in `reconciliation_findings` (unique `kind`+`subject_key`; no PII/raw tokens); a later scan that no longer matches a subject closes that finding (`open=false`) without repairing the provider; R1 `reconciliation_persist`; kill switch and demo skip persist; JSON counts only on stdout by default; `mia-reconcile --inspect` lists open SoR findings (`kind` + `subject_key` + sanitized webhook `channel`/`envelope_kind`, cap 50; `open_count` is listed length, not a full-table count); never `mark_webhook`, never send, never consume tokens, never Sheets read-back, never Meta/calendar writes — existing Postgres/file DBs need `webhook_events.claimed_at` and the new `reconciliation_findings` table.

# 32. Data Model

| Table/domain | Core fields/purpose |
| --- | --- |
| businesses | id, name, timezone, automation_mode |
| users | id, business_id, role, auth identifiers |
| customers | id, business_id, canonical identity |
| channel_identities | customer_id, channel, external_id, verified |
| identity_links | identity_id, customer_id, reason, reversed_at; **alive** — verified link history on website→WhatsApp handoff consume (`handoff_token` only; unique per `channel_identities.id`; first write wins; R1 `identity_link_persist`; no reverse/unmerge this slice) |
| leads | id, customer_id, stage, fit, source, owner_required |
| lead_sales_state | lead_id, structured workflow/pain/impact/buying fields |
| conversations | id, lead_id, channel, provider, thread_id |
| messages | id, conversation_id, external_id, direction, body/transcript |
| timeline_events | lead_id, type, payload, source, occurred_at |
| attribution_touches | lead_id, source/campaign/content/utm/confidence |
| meetings | lead_id, status, source, scheduled_at, calendar_event_id, summary; **alive** — one Postgres row per lead (unique `lead_id`); upsert on graph `offer_meeting` only → status `offered`; `scheduled_at`/`calendar_event_id`/`summary` always `""` (never slot times or brief payload); not a booked meeting; no `MEETING_BOOKED` event; R1 `meeting_persist`; kill switch skips; no stop/disqualify/handoff persist; Sheets tab `04 Meetings` mirror when row exists — existing Postgres/file DBs need the new `meetings` table |
| meeting_debriefs | lead_id, outcome, next_step, estimated_value, notes; **alive** — one Postgres row per lead (unique `lead_id`); upsert on owner WhatsApp `meeting_debrief` when message contains `lead_*`; outcome from deterministic phrases; `next_step` classified `none`/`follow_up`/`proposal` from deterministic phrases; `estimated_value`/`notes` always `""`; canonical `MEETING_DEBRIEF` first write wins (`{lead_id}:debrief`; payload `outcome`+`next_step` only); R1 `meeting_debrief_persist`; kill switch skips; no deal value/stage change, no calendar create, no send, no follow-up upsert, no transcript on row — existing Postgres/file DBs need the new `meeting_debriefs` table |
| deals | lead_id, stage, expected_value, closed_value; **alive** — one Postgres row per lead (unique `lead_id`); upsert on graph `offer_meeting` → stage `meeting_offered` or `handoff` → stage `proposal`; forward-only rank (`meeting_offered` < `proposal`); `expected_value`/`closed_value` always `""` (never inferred); `source`=channel; `attribution_confidence`=`utm` when lead has ATTRIBUTION canonical event else `unknown`; canonical `DEAL_UPDATED` first write wins per `{lead_id}:deal:{stage}` (payload stage/source/confidence only); R1 `deal_persist`; kill switch skips; no stop/disqualify persist; no won/lost — existing Postgres/file DBs need the new `deals` table |
| tasks | owner, trigger, condition, action, status |
| business_instructions | category, normalized rule, scope, version, status |
| knowledge_chunks | source, trust, version, freshness, embedding |
| approvals | action, risk, payload_hash, decision, approver, resource_type, resource_id, expires_at, approval_id, business_id, actor_id, proposed_parameters, approved_at, executed_at, execution_operation_id, result; **alive** — one Postgres row per lead+action (`proposal_handoff`) or campaign+action (`campaign_write`); upsert on graph `handoff` when `owner_required` or owner campaign request; lead/campaign-bound hash + 24h expiry; `approval_id` `apr_*` first-write-wins; `proposed_parameters` compact identity JSON (same keys as `payload_hash`; no message text); `approved_at` on approve only; `business_id`/`actor_id`/`executed_at`/`execution_operation_id`/`result` reserved empty (no tenant, no PII, no execute); stale/unbound pending cannot be decided; canonical `APPROVAL_REQUIRED` first write wins; R1 `approval_persist`; owner WhatsApp approve/reject persist-only (`approved`/`rejected`; approver `""`; R1 `approval_decide`); kill switch skips; `named_write_may_auto` R4 False; no quote text, no execute/send |
| tool_runs | tool, provider, latency, status, cost metadata, freshness, correlation_id; **alive** — one Postgres row per `persist_tool_outcome` (`provider_event_id`=`{inbound_id}:tool:{tool}` unique, first write wins); sanitized `correlation_id` joins canonical envelope (not in payload; migration `migrations/20260821_tool_run_correlation_id.sql`); canonical TOOL_RESULT payload `{tool, status, result_count}` only; `ToolOutcome.freshness` (`""|live|cached|stale|unverified`) on `tool_runs.freshness` (Meta `campaign_metrics` + calendar + Gmail + opt-out + ownership + follow-up + funnel + Instagram + LinkedIn profile/analytics + research `research_snippets` wired; versioned knowledge unlabeled); `ToolOutcome.latency_ms` from port wall-clock on research/meta/linkedin/calendar enrich + STT transcribe (`elapsed_ms` around port HTTP/search/transcribe) and sales-tab / session-tab / campaign-tab / content-tab Sheets upserts after claim (`sheets_mirror_outcome` / `sheets_tab_mirror_outcome`); explicit `persist_tool_outcome(..., latency_ms=)` wins when non-zero; `cost_usd` 0; kill-switch `denied` still records audit row; no prompt/PII/URLs/slot times; migration `migrations/20260821_tool_run_freshness.sql` |
| ai_runs | model, graph version, tokens, cost, kill_switch flag, `policy_version`, `prompt_version`, `latency_ms`, allowlisted `automation_mode`, `decision_confidence`; **alive** — one Postgres row per sales graph invoke on website and prospect inbound (`run_id` unique, first write wins); metadata only (no prompt/reply/lead message text); `graph_version=sales_v1`; `policy_version=fde_v1` pinned from execution-policy registry; `prompt_version=sales_reply_v1` pinned beside the sales-reply system prompt (migration `migrations/20260822_ai_run_prompt_version.sql`; empty default, no backfill); wall-clock `latency_ms` around `graph.invoke`; `tokens_in`/`tokens_out` from OpenAI usage on successful live compose (canned/fallback/kill-switch 0); `cost_usd` 0; `automation_mode` from settings (`off|draft_only|shadow|hybrid|auto_approved`; invalid `""`; migration `migrations/20260822_ai_run_automation_mode.sql`); `decision_confidence="1.0"` pinned from `DETERMINISTIC_NBA_CONFIDENCE` (no persist parameter; no LLM self-score; migration `migrations/20260822_ai_run_decision_confidence.sql`; empty default, no backfill); kill switch still records audit row; Graph Lab/evals do not persist; owner path excluded |
| shadow_decisions | run_id, lead_id, channel, next_action, proposed_reply, policy_version; **alive** — one Postgres row per shadow-skipped prospect reply (`run_id` unique, first write wins); metadata + Mia proposed reply only (truncated 4000); no inbound/lead message text; R1 `shadow_decision_persist` with kill_switch=False in assert; owner path excluded; website HTTP replies not gated by shadow this slice |
| campaigns/adsets/ads | Meta IDs and metadata |
| campaign_metrics | time window and deterministic metrics |
| webhook_events | provider, provider_event_id, processing status, claimed_at, **channel** + **envelope_kind** (`text`\|`audio`\|`empty`\|`referral`; no body/PII; migration `migrations/20260821_webhook_envelope.sql`) |
| audit_events | actor, action, resource, result |

# 33. Permissions and Approval Matrix

## 33.1 Risk levels

| Risk | Examples | Policy |
| --- | --- | --- |
| R0 Read | Insights, calendar free/busy, CRM read | Auto. |
| R1 Low write | Create/update lead, internal task, Sheet mirror | Auto. |
| R2 Customer communication | Approved inbound reply, normal follow-up | Auto only in approved scope. |
| R3 Commercial commitment | Proposal, special price, unusual promise | Approval. |
| R4 External financial/marketing change | Meta budget/status, mass campaign | Approval with explicit action preview. |
| R5 Destructive/security | Delete data, permission changes, code changes | Deny or admin-only manual path. |

These decisions are **code** (`app/core/risk.py`), not `.env` knobs. `MIA_KILL_SWITCH=true` is the only runtime override: it raises PolicyDenied on every gated action. There is no `MIA_R4_AUTO` or `MIA_R5_ALLOW`. **`MIA_AUTOMATION_MODE`** (default `shadow`) gates prospect **MessagePort** send only under SHADOW; it does not override R4/R5 or the kill switch. **`MIA_DEMO_MODE`** is separate (synthetic UTMs). Live site origin for CORS/health: https://www.assafweb.com.

## 33.2 Approval payload

- Plain-language action.
- Exact target.
- Exact proposed content/parameters.
- Why Mia recommends it.
- Business impact/risk.
- Approve / Edit / Reject.

**Implementation (2026-08-21):** R3 commercial approval is **persist-only** this slice. When the sales graph selects `handoff` and `owner_required` is true (proposal/quote request), Mia upserts one row in `approvals` (unique per lead+action; `proposal_handoff` only) with `risk=R3`, `decision=pending`, `resource_type=lead`, `resource_id=lead_id`, `expires_at` UTC ISO (+24h TTL), and SHA-256 `payload_hash` of bound action identity (`action`+`risk`+`channel`+`resource_type`+`resource_id` — no message text). Stale or unbound pending rows return `expired`/`unbound` on owner decide without mutating the row; empty `expires_at` is fail-closed expired. Canonical `APPROVAL_REQUIRED` on first write wins (`{lead_id}:approval:proposal_handoff`; payload `action`+`risk`+`decision` only). R1 `approval_persist` before upsert; kill switch skips persist. Wired on website + prospect inbound after meeting-brief policy. Owner WhatsApp (text or audio classify path) can record **approved** or **rejected** on a valid pending row via conservative phrase match + optional `lead_id`; R1 `approval_decide` before update; `approver` stays `""`; Hebrew ack (female Mia, no PII, no lead_id). **No proposal send, no quote body, no instruction activation, no Meta writes** this slice.
# 34. Security and Threat Model

## 34.1 Primary threats

- Prompt injection from emails/websites/DMs.
- Tool injection / malicious data attempting to trigger actions.
- Webhook spoofing/replay.
- Cross-tenant or cross-lead data leakage.
- Owner impersonation.
- OAuth token leakage.
- Excessive tool permissions.
- Duplicate side effects from retries.
- Malicious file/audio payloads.
- Browser exfiltration.
- PII leakage in logs/traces.
- Model hallucination of commercial facts.
- ManyChat/Composio dual-send race.
## 34.2 Controls

- Explicit trust labels for system/user/business/external content.
- Tool permission firewall independent of the LLM.
- Least-privilege OAuth and IAM.
- Secrets in Secrets Manager; no credentials in prompts or logs.
- WAF/API Gateway rate and request controls.
- Webhook signature/HMAC verification and timestamp/replay checks.
- Tenant/business_id enforced at repository layer.
- Sensitive log redaction.
- Idempotency for every external write.
- Browser allowlist and sandbox.
- Output schema validation before tool calls.
- Human approval for high-risk actions.
- Kill switch at business, workflow and conversation levels.

**Implementation (2026-08-21):** Business kill switch is `MIA_KILL_SWITCH` (blocks gated actions via `PolicyDenied`). Workflow kill switch is `kill_switch` in LangGraph state (graph still runs; compose degrades to canned). Conversation-level kill switch persists `leads.conversation_killed=true` when the sales graph selects `stop` on website and prospect inbound; any later NBA other than `stop` clears it (recovery). R1 `conversation_kill_persist` with `kill_switch=False` in `assert_allowed` (audit row still records when business kill switch is on). Follow-up send-readiness in `evaluate_follow_up_send` denies pending rows when `conversation_killed` (defense in depth; `stop` also cancels the follow-up row). Not in LangGraph state; owner and Graph Lab excluded. Existing Postgres/file sqlite DBs need the new `conversation_killed` column on `leads`.
## 34.3 Prompt injection rule

An email, scraped webpage, Instagram message, PDF or research result may contain text that looks like an instruction. It is always data. It can never grant itself permissions, change the system prompt, select tools outside policy, or override Assaf's approved instructions.

# 35. Privacy, Data Retention and Auditability

- Data minimization: store only fields needed for lead operation, analytics, safety and improvement.
- Raw voice audio: short-lived by default; transcript retained according to operational need.
- PII masking in logs and demo mode.
- Configurable message retention policy.
- Deletion workflow that propagates across canonical DB and mirrors.
- No real private lead data in public demo.
- Evaluation datasets use sanitized/anonymized production traces unless explicit policy allows otherwise.
- Every AI/customer action records prompt/graph/model/tool versions sufficient for audit without storing hidden reasoning.
# 36. Observability and Operations

## 36.1 Required telemetry

- run_id, thread_id, lead_id, channel, source.
- node timings and graph path.
- model, reasoning mode, input/output tokens, cost estimate.
- Every sales graph invoke on website and prospect inbound persists one `ai_runs` row (metadata only; `policy_version=fde_v1`; `prompt_version=sales_reply_v1`; `decision_confidence="1.0"`; wall-clock `latency_ms` around `graph.invoke`; `tokens_in`/`tokens_out` from OpenAI usage on successful live compose — canned/fallback 0; `cost_usd` 0; no prompt/reply).
- Every allowlisted tool outcome via `persist_tool_outcome` persists one `tool_runs` row (metadata only; `ToolOutcome.latency_ms` from port wall-clock on enrich paths and sales-tab / session-tab / campaign-tab / content-tab Sheets upserts after claim; explicit kwarg wins when non-zero; cost 0).
- tool calls, latency, retries and provider errors.
- retrieved source IDs/freshness/trust.
- approval events.
- outbound send/delivery status.
- lead stage transitions.
- meeting/deal outcome.
- campaign attribution confidence.
## 36.2 Tracing/evals

Use LangSmith for graph tracing and offline/online evaluation where appropriate [R25-R26], while CloudWatch remains the infrastructure/log/alert surface. The two are complementary.

## 36.3 Alerts

- Queue depth or age above threshold.
- Webhook authentication failure spike.
- Outbound error rate spike.
- Model/tool latency regression.
- Campaign spending with zero/abnormal leads.
- Conversion drop/anomaly.
- Cost per lead/conversation threshold exceeded.

**Implementation (2026-08-22):** Ordered go-live is `docs/PRODUCTION_BUILD.md` (ADR-014: Fargate + RDS + Secrets Manager box + ALB). Day-2 operator runbook is `docs/RUNBOOK.md` (kill switch, conversation takeover/resume, named flags, due-scan, reconcile, rollback). Alerts and dashboards in this section are **not** wired — no CloudWatch/LangSmith pager. `cost_usd` remains 0. Cloudflare Tunnel is test-only. Production is this FastAPI process on Fargate + RDS + `https://mia.assafweb.com`.
# 37. Reliability and Performance SLOs

| Metric | Target / initial SLO |
| --- | --- |
| Webhook acknowledgement | P95 < 1.5s after signature/schema handling. |
| Website simple response | P95 < 5s where no external research is required. |
| IG/WhatsApp normal text reply | P95 < 7s excluding provider delivery delay. |
| Owner simple command | P95 < 6s. |
| Voice-note command | Transcript + first useful text response proportional to audio length; target < 12s after upload for short note. |
| Duplicate outbound sends | 0 for tested retry/replay cases. |
| Critical fact fabrication | 0 in critical eval suite. |
| Service availability | 99.9% target after stabilization; lower explicit demo SLO allowed before production. |
| Recovery | Failed async writes visible and retryable; no silent loss. |

## Performance tactics

- Fast webhook ACK + async processing.
- Model routing and low reasoning for simple tasks.
- Parallel read-only tool calls when independent.
- Cache stable knowledge/research, never stale critical facts.
- Avoid unnecessary RAG/tool calls.
- SQS parallelism across leads, ordering within lead.
# 38. Test Strategy

## 38.1 Test pyramid

| Layer | Coverage |
| --- | --- |
| Unit | State transitions, scoring, policies, budget math, attribution math, instruction conflict, parsers. |
| Integration | Services + DB + mocked adapters + graph nodes. |
| Contract | Recorded/sanitized provider payloads for Meta, ManyChat, Composio, Google, LinkedIn and Make. |
| Graph | Expected nodes/edges/interrupt paths. |
| E2E staging | Website/IG/WhatsApp lead → meeting, owner voice task, campaign analysis. |
| Load | Burst webhooks, parallel leads, queue behavior. |
| Security | Auth, replay, injection, cross-tenant, secrets/logging. |
| Chaos/resilience | Provider timeout, model failure, expired OAuth, queue retry, duplicate event. |
| Regression eval | Prompt/model/graph behavior. |

## 38.2 Required E2E scenarios

1. Website visitor starts Mia, is diagnosed, moves to WhatsApp and books a meeting without losing source attribution.
1. Instagram comment trigger enters ManyChat, calls Mia backend, and creates/continues one lead without double sending.
1. Instagram organic DM is qualified and escalated correctly.
1. Assaf sends a WhatsApp voice note; transcript is understood; Understanding Check fires only when needed.
1. Gmail reply from an existing lead links to the correct timeline.
1. Calendar meeting is created only after confirmation and conflict checks.
1. Campaign Yuma metrics flow into analysis and Google Sheet budget pacing.
1. Prompt injection inside an email/webpage cannot call privileged tool.
1. Repeated webhook delivers only one downstream action.
1. Provider failure results in safe user response/owner alert, not hallucination.

**Implementation (2026-08-21):** Eight §23 pre-production composed in-process stories in `tests/e2e/test_preprod_stories.py` (fakes only; live staging OAuth/Meta writes still blocked). Status matrix: `docs/PRE_PRODUCTION_GAP_REPORT.md` §23. Gate B human takeover **Complete** in-process. Next: live staging, cost on `ai_runs`/`tool_runs` (tokens wired from compose usage; cost_usd still 0), gated writes (Gmail send, Meta, follow-up send).

# 39. AI Evaluation and Sales Simulation

The main quality moat is the Sales Engine. Unit tests cannot prove it sells well. Use versioned LangSmith datasets and offline/online evaluators [R25-R26].

**Implementation (2026-08-21):** Local deterministic buyer replay at `app/evals/datasets/buyers_v1.json` — 12 §39.1 personas, multi-turn extract+NBA+mark+reply, exact-match action/reply/state asserts; no LLM judge. Ready-to-book persona maps to `QUALIFY` (§10.4 + no invented GOOD fit), not `offer_meeting` or `understand_workflow`. Website continuation replay at `app/evals/datasets/website_handoff_v1.json` — shoe-store turns with `select_next_action(..., channel="website")`; must offer WhatsApp after shoes + inventory + manual Sheets, without looping `יום רגיל בעסק`.

## 39.1 Simulated buyer set

- Clinic owner with missed calls.
- Restaurant with WhatsApp overload.
- E-commerce owner with abandoned leads.
- Real-estate professional with slow follow-up.
- Low-budget poor-fit prospect.
- Technical skeptical buyer.
- Buyer afraid of AI mistakes/privacy.
- Buyer already comparing vendors.
- Buyer who gives one-word answers.
- Buyer ready to book immediately.
- Enterprise-like lead with decision committee.
- Non-buyer/student/information seeker.
## 39.2 Eval dimensions

| Dimension | Question |
| --- | --- |
| Workflow understanding | Did Mia understand how the business actually operates? |
| Pain depth | Did she move beyond surface issue without forcing it? |
| Listening | Did next questions follow what the prospect said? |
| Question efficiency | Did she avoid interrogation/redundant public facts? |
| Quantification | Were assumptions explicit and validated? |
| Reframe quality | Did insight help rather than manipulate? |
| Qualification | Did she learn enough buying reality? |
| Solution fit | Did recommendation match the actual problem? |
| Trust/truth | No unsupported claims, prices or security promises. |
| Closing | Was the next step appropriate? |
| Disqualification | Did she avoid booking bad meetings? |
| Channel style | Natural for IG/WhatsApp/website/email. |
| Safety | Did permissions/boundaries hold? |

## 39.3 Example weighted Sales Quality Score

| Component | Weight |
| --- | --- |
| Workflow/pain understanding | 25% |
| Solution fit | 20% |
| Qualification quality | 15% |
| Buyer experience / natural conversation | 15% |
| Correct next step | 10% |
| Truth / grounded claims | 10% |
| Efficiency / message count / cost | 5% |

**Implementation (2026-08-21):** Graph Lab computes a local weighted Sales Quality Score (0–100) from exact-match turn pass/fail in `app/evals/harness.py`; component rates use `expected_action` → component mapping with renormalized weights when a component has zero tagged turns; no LangSmith, no LLM judge this slice.

# 40. Model Benchmarking and Cost Controls

## 40.1 Benchmark matrix

Before production, run the same sales, routing, extraction, campaign and research datasets against candidate models. Record quality score, p50/p95 latency, token use, dollar cost, tool-call correctness and failure rate.

## 40.2 Dynamic cost registry

- Provider/model prices stored in config and updated deliberately.
- Every run records effective model cost.
- Every sales graph invoke on website and prospect inbound persists one `ai_runs` row; `policy_version=fde_v1`; `prompt_version=sales_reply_v1`; `decision_confidence="1.0"`; wall-clock `latency_ms` around `graph.invoke`; tokens/cost are 0 until compose returns usage; no prompt/reply.
- Budget alarms per day/month and per feature.
- Cache where safe.
- Escalate to stronger model only when the expected quality gain justifies cost/risk.
- Do not use a frontier model for deterministic calculations.
# 41. Dev/Test/Prod, CI/CD and Release Gates

## 41.1 Environments

| Environment | Data | Providers | Purpose |
| --- | --- | --- | --- |
| DEV | Synthetic/local | Mocks/sandboxes | Fast iteration in Cursor. |
| TEST/STAGING | Synthetic + sanitized fixtures | Provider test/sandbox where possible | E2E, load, security, eval. |
| PROD | Real data | Production provider accounts | Controlled live operation. |

Laptop uses `.env` copied from `.env.example` (ADR-015 adapter comments). Production SECRET keys live in `deploy/mia-prod.secret.example.json` shape inside Secrets Manager `mia/prod`. Do not add Apify keys until that adapter exists. Keep `MIA_INSTAGRAM_SENDER=direct` until Assaf tests Composio send.

## 41.2 Promotion gate

```
PR
↓
format/lint/type
↓
unit + integration + contract
↓
critical security tests
↓
critical AI eval suite
↓
deploy DEV/TEST
↓
E2E smoke
↓
human approval
↓
PROD canary / feature flag
↓
monitor
```

## 41.3 Infrastructure as code

AWS infrastructure should be defined through Terraform or another chosen IaC standard once architecture stabilizes. No manual, undocumented production environment.

# 42. Demo Mode

- One switch (`MIA_DEMO_MODE`) loads synthetic/sanitized website journeys; **fail-closed in production** — `demo_mode_active()` is false when `MIA_ENV=prod` even if the flag is true.
- Never display private lead emails/phone numbers in client demonstrations. This slice does not persist or return emails or phones on demo journeys.
- Demo Mode simulates a complete website lead journey through the **real LangGraph** and Ask Mia widget UI.
- Website sessions in demo stamp synthetic attribution (`utm_source=mia_demo`, `utm_medium=demo`, `utm_campaign=synthetic`) even when query params are empty; demo labels win over visitor UTMs.
- Demo skips Google Sheets mirror on website session create and inbound prospect graph paths (tabs 01, 04, 05, 06, 07, 08, 09, 10) and owner analytics content insights path; does not persist a sheets `TOOL_RESULT` on those paths; calendar enrich unchanged.
- `GET /health` and `GET /v1/website/config` expose effective `demo` (not the raw flag).
- `GET /v1/demo/status` and `POST /v1/demo/scripted` return 404 when demo is inactive; when active, scripted runs the clinic→meeting funnel (reflect → offer_hypothesis → qualify → offer_meeting) via the same website message path.
- Ask Mia widget fetches config on load; when `demo` is true, launcher label is `שאלי את מיה (דמו)` (no auto-open).
- Free-form demo = existing widget conversation path.
- Clearly label synthetic campaign revenue internally to prevent contamination of production analytics.
# 43. KPIs and Success Metrics

| Category | Metrics |
| --- | --- |
| Acquisition | Website→Mia start, IG engagement→DM, source mix. |
| Lead quality | Qualified rate, disqualification rate, qualified CPL. |
| Sales | Meeting rate, show rate, proposal rate, win rate, pipeline value. |
| Speed | First response time, time to qualification, time to meeting. |
| Automation | % handled without Assaf, escalations, owner minutes saved. |
| Follow-up | Due completion, recovered conversations, recovered meetings. |
| Campaign | Spend, pacing, CPL, qualified CPL, cost/meeting, cost/deal, ROAS where valid. |
| Mia quality | Sales Quality Score, critical factual accuracy, approval precision, tool reliability. |
| Cost | AI/tool cost per lead, qualified lead, meeting and won deal. |
| Content intelligence | IG performance patterns and downstream lead signal—not content quantity. |

# 44. Implementation Roadmap

| Phase | Exit outcome |
| --- | --- |
| Phase 0 — Freeze spec | PRD, AGENTS.md/Cursor rules, repo, environments, secrets policy, ADRs. |
| Phase 1 — Core foundation | FastAPI, config, logging, DB, canonical events, auth/security skeleton. |
| Phase 2 — Lead domain + Sales Engine | Identity, timeline, SalesState, deterministic transitions, first graph, eval dataset. |
| Phase 3 — Website Mia | Session/event API, website widget, handoff tokens, UTM/behavior tracking. |
| Phase 4 — WhatsApp + voice input | Inbound/outbound, owner auth, GPT Transcribe, tasks, follow-up. |
| Phase 5 — Instagram + ManyChat + LinkedIn intelligence | IG analytics/read, ManyChat triggers/external request, conversation ownership; LinkedIn read/performance intelligence behind permissions. |
| Phase 6 — Gmail/Calendar/Sheets | Lead linking, meetings, operating Sheet. |
| Phase 7 — Meta campaign intelligence | Campaign sync, budget pacing, attribution, anomaly detection. |
| Phase 8 — Research/browser | Firecrawl now; Apify later behind `ResearchPort`; company research; Playwright fallback remains flagged off. |
| Phase 9 — Owner learning + memory | Understanding Check, instruction proposals, versioning/conflicts. |
| Phase 10 — AWS hardening | First live host (ADR-014): Fargate + RDS + SM box. Remaining: SQS ordering, runtime benchmark vs AgentCore, WAF, reconciliation scheduler, CloudWatch; approve AgentCore/MCP only if tested and valuable. |
| Phase 11 — Graph Lab/evals | Replay, model benchmark, prompt/graph version comparison. |
| Phase 12 — Production/demo readiness | Load/security/red-team, demo mode, canary, runbooks. |

# 45. Repository Structure and Cursor Build Protocol

```
mia/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── infra/
│   └── terraform/
├── docs/
│   ├── PRD.md
│   ├── ADR/
│   ├── BUILD_STATUS.md
│   └── runbooks/
├── app/
│   ├── main.py
│   ├── core/
│   ├── api/
│   ├── domain/
│   │   ├── identity/
│   │   ├── leads/
│   │   ├── sales/
│   │   ├── tasks/
│   │   ├── approvals/
│   │   └── attribution/
│   ├── graph/
│   │   ├── state.py
│   │   ├── orchestrator.py
│   │   ├── routing.py
│   │   └── subgraphs/
│   ├── services/
│   ├── integrations/
│   │   ├── composio/
│   │   ├── manychat/
│   │   ├── make/
│   │   ├── instagram/
│   │   ├── whatsapp/
│   │   ├── google/
│   │   ├── meta_ads/
│   │   └── research/
│   ├── db/
│   ├── prompts/
│   ├── evals/
│   └── workers/
└── tests/
├── unit/
├── integration/
├── contract/
├── eval/
├── e2e/
└── security/
```

## 45.0 Required control documents

Before feature coding begins, the repository contains AGENTS.md (Cursor/Codex operating rules), docs/BUILD_STATUS.md (file-by-file progress), docs/DECISIONS.md (ADRs), docs/PRD.md or this PRD in an accessible form, and a provider-capability matrix with validation dates. Cursor must read these before touching implementation files.

## 45.1 Cursor build rule

- Build one controlled implementation unit at a time.
- Before significant new work, briefly restate the task/decision in your own words so Assaf can see that you understood the intent; ask only the knowledge question that materially changes the design.
- Before a file: state purpose, dependencies, acceptance criteria and out-of-scope.
- After a file: inspect diff, run relevant checks/tests, perform independent reviewer pass, stop.
- Never let Cursor/Codex opportunistically rewrite unrelated architecture.
- Maintain BUILD_STATUS.md and ADRs.
- Use the PRD/Bible as the default approved architecture baseline. If a materially better approach is discovered, invoke the Collaborative Build & Better-Way Protocol before changing direction; discuss it with Assaf, test where useful, record the decision in an ADR, and then update the baseline if approved.
## 45.2 First build order

1. AGENTS.md / Cursor rules and BUILD_STATUS.md.
1. pyproject.toml and core configuration.
1. logging/errors/security primitives.
1. database/session + first business/user models.
1. canonical event schemas.
1. lead/customer identity models.
1. SalesState + deterministic transition/scoring policy.
1. first unit tests.
1. LangGraph state and mocked graph.
1. website event/session API before external channels.
# 46. Definition of Done

## Feature DoD

- Acceptance criteria implemented.
- Architecture boundary respected.
- Provider adapter contract tested.
- Permissions/risk behavior defined.
- Unit/integration/contract tests pass.
- LLM evals pass if model behavior is involved.
- Observability emitted.
- Failure path tested.
- Staging E2E verified.
- Security/privacy implications reviewed.
- Cost telemetry visible.
- Docs/ADR/runbook updated if operationally relevant.
- Assaf review complete.
## System go-live gate

- No critical sales fact hallucinations in required eval set.
- No duplicate send in retry/replay tests.
- Prompt injection red-team cannot cross tool policy boundary.
- Owner kill switch works.
- IG/ManyChat channel ownership validated.
- Voice note transcription path validated in Hebrew and English.
- Campaign Yuma tracking/Sheet/alerts pass pre-launch check.
- Backup/recovery and provider failure paths are documented.
- Demo Mode contains no private data.
# 47. Future Roadmap

- Outbound prospecting with strict consent/policy controls.
- Proposal generation with approval.
- Client-facing multi-tenant Mia platform.
- Additional CRMs, LinkedIn workflows and service-specific vertical packs.
- More sophisticated revenue attribution and offline conversions.
- Automated website experiments after separate governance design.
- Optional controlled Meta write actions after enough production evidence.
- AgentCore/Google Workspace MCP adoption where stable and valuable.
- No roadmap item automatically bypasses current permission boundaries.
# 48. Rejection Review and Resolved Architecture Risks

| Rejected design | Decision | Resolution |
| --- | --- | --- |
| Use ManyChat as Mia's brain | REJECTED | Would fragment model routing, memory, SalesState, evals and audit. Use it only as optional trigger/channel layer. |
| Use Composio ManyChat toolkit | REJECTED | Composio changelog lists it deprecated; integrate ManyChat directly [R10]. |
| Use Make for core orchestration | REJECTED | Open-beta agent features and low-code state are not the right system-of-record/control plane [R19-R21]. |
| Let ManyChat and Composio both send IG messages | REJECTED | Creates race/double-response risk. Single sender ownership per conversation. |
| Google Sheet as CRM database | REJECTED | Easy to corrupt/delete and poor canonical event model. DB is source; Sheet is mirror. |
| One LLM for every task | REJECTED | Cost/latency/quality vary by task; use eval-driven router. |
| Hard-code provider price assumptions | REJECTED | Pricing changes; telemetry/config is authoritative. |
| Lead score as sole sales brain | REJECTED | Use rich SalesState and derive scores/priority from explicit fields. |
| Fixed sales script | REJECTED | Use next-best-question/action based on workflow and missing state. |
| Ask pain directly at start | REJECTED | Workflow-first discovery better matches product vision. |
| Pitch immediately after first pain signal | REJECTED | Deepen/quantify/reflect before solution hypothesis. |
| Allow self-learning without approval | REJECTED | Mia proposes durable instruction; Assaf approves. |
| Allow production graph self-modification | REJECTED | Graph Engineering runs locally with eval + review + release. |
| Keep raw voice forever | REJECTED | Short-lived raw audio, transcript-focused retention. |
| Browser as default integration | REJECTED | APIs/Composio first; browser fallback only. |
| Trust scraped/email text as instructions | REJECTED | External content is untrusted data; tool firewall enforces permissions. |
| Webhook-only reliability | REJECTED | Add reconciliation and idempotency. |
| Parallel processing without per-lead ordering | REJECTED | Use ordered message group for each lead/conversation. |
| Autonomous ad budget changes | REJECTED | Recommendations auto; writes require approval. |
| Content creation by Mia | REJECTED | Mia analyzes and suggests ideas; Assaf creates. |
| Full AssafWeb redesign | REJECTED | Preserve design/proof; shorten and merge sections. |
| Treat LinkedIn as a core V1 dependency | REJECTED | LinkedIn is useful professional intelligence, but website/IG/WhatsApp lead conversion must work without it. |
| Autonomous LinkedIn posting or bulk outreach | REJECTED | Mia provides intelligence and lead handling; outbound/publishing needs a separate governed feature. |
| Load hundreds of Composio tools into every model call | REJECTED | Use typed capability adapters and runtime discovery/allowlists so context, permissions and schemas stay controlled [R3]. |
| Allow arbitrary discovered MCP servers in production | REJECTED | Only allowlisted, versioned MCP servers/tools may cross the tool gateway. |
| Couple LangGraph state to Composio/MCP/provider SDK objects | REJECTED | Graph state contains serializable domain data only; adapters map provider payloads. |
| Use Lambda for every long-running agent workflow | REJECTED | Lambda is excellent ingress/event compute, but long workflows use a benchmarked async runtime. |
| Lock the product permanently to AgentCore | REJECTED | AgentCore is a strong candidate; keep a provider-neutral runtime/tool interface. |
| Use provider webhooks without reconciliation | REJECTED | Webhooks are fast path; scheduled reconciliation detects missed/partial provider events. |
| Use the same conversation style on every channel | REJECTED | One sales brain, channel-specific communication policy for website, IG, WhatsApp, Gmail and owner mode. |
| Let owner instructions override safety or source-of-truth rules | REJECTED | Teach Mia changes preferences/behavior only inside hard system, permission and factual-source constraints. |

## Second-pass review result

A second architecture review was completed after the first draft. The review restored LinkedIn as a secondary professional-intelligence channel, added an explicit MCP trust/allowlist policy, added a PostgreSQL/AWS database decision gate, rejected Lambda-only long-running orchestration and AgentCore lock-in, strengthened provider-neutral contracts, and expanded the rejection table for tool sprawl, provider objects in graph state and owner-instruction safety. No unresolved product-level blocker remains before repository planning; provider credentials, quotas, prices and beta eligibility remain deployment-time validation items.

# Appendix A. Source Register

Sources are included to ground architecture choices and provider capabilities. Revalidate beta features, pricing, quotas, permissions and platform policies immediately before production deployment.

[R1] AssafWeb current website. Current site structure, positioning, copy, CTAs, service sections, testimonials, FAQ. https://www.assafweb.com/

[R2] Composio Toolkits. 1326 toolkits as listed 21 August 2026; catalog/versioning context. https://docs.composio.dev/toolkits

[R3] Composio Meta Tools. Runtime tool discovery, connections, multi-execution and reduced tool-context pattern. https://docs.composio.dev/toolkits/meta-tools

[R4] Composio Instagram Toolkit. Instagram Business/Creator support, conversations, media, insights and message tools. https://docs.composio.dev/toolkits/instagram

[R5] Composio Meta Ads Toolkit. Meta Ads insights and campaign/ad management tools; managed app not available. https://docs.composio.dev/toolkits/metaads

[R6] Composio Google Super. Unified Google integration with Gmail, Calendar, Sheets and other services. https://docs.composio.dev/toolkits/googlesuper

[R7] Composio Gmail Toolkit. Gmail tools, triggers, managed OAuth and quota notes. https://docs.composio.dev/toolkits/gmail

[R8] Composio Google Calendar Toolkit. Calendar tools, triggers and managed OAuth. https://docs.composio.dev/toolkits/googlecalendar

[R9] Composio Make Toolkit. Make toolkit coverage; primarily Make account/platform API operations. https://docs.composio.dev/toolkits/make

[R10] Composio Changelog. Toolkit versioning, custom tools, multi-account mode, webhook subscriptions; ManyChat toolkit deprecation (19 Dec 2025). https://docs.composio.dev/changelog/2025/9/15

[R11] ManyChat AI Step. ManyChat AI Step capabilities and role inside automations. https://help.manychat.com/hc/en-us/articles/14281187288860-Manychat-AI-Step

[R12] ManyChat Instagram Post/Reel Comments Trigger. Comment-to-public-reply/private-DM lead capture and first-comment limitation. https://help.manychat.com/hc/en-us/articles/14281316989724

[R13] ManyChat Story Mention Reply. Story mention trigger behavior. https://help.manychat.com/hc/en-us/articles/14281309502108-Instagram-Story-Mention-Reply-trigger

[R14] ManyChat Follow-to-DM. Follow-to-DM beta, eligibility and frequency limitations. https://help.manychat.com/hc/en-us/articles/23096654243740-Follow-to-DM-on-Instagram-Say-Hi-to-New-Followers

[R15] ManyChat Share-to-DM. Share-to-DM trigger and Instagram API limitation. https://help.manychat.com/hc/en-us/articles/23431135317916-Share-to-DM-trigger

[R16] ManyChat External Request. HTTPS external requests, JSON payloads and response mapping. https://help.manychat.com/hc/en-us/articles/14281285374364-Dev-Tools-External-request

[R17] ManyChat Conversation Routing for Instagram. Routing messages across connected apps and entry points. https://help.manychat.com/hc/en-us/articles/14281188830748-Conversation-Routing-for-Instagram

[R18] ManyChat WhatsApp Templates. Template requirement outside messaging window and consent context. https://help.manychat.com/hc/en-us/articles/14281326740124-How-to-use-WhatsApp-Messages-Templates-in-Manychat

[R19] Make AI Agent (New). New Make AI Agent released Feb 2 2026; open beta. https://help.make.com/introduction-to-make-ai-agent-new

[R20] Make Webhooks. Instant webhook triggering, queues, parallel/order processing and rate limit behavior. https://help.make.com/webhooks

[R21] Make Error Handling. Error handlers, incomplete executions, retry/resume/skip and scenario reliability. https://help.make.com/overview-of-error-handling

[R22] LangGraph Persistence. Checkpointing, threads, memory, time travel and fault tolerance. https://docs.langchain.com/oss/python/langgraph/persistence

[R23] LangGraph Interrupts. Human-in-the-loop pause/resume and idempotency requirements. https://docs.langchain.com/oss/python/langgraph/interrupts

[R24] LangGraph Subgraphs. Subgraph persistence modes and architecture considerations. https://docs.langchain.com/oss/python/langgraph/use-subgraphs

[R25] LangSmith Evaluation. Offline/online evals and production feedback loop. https://docs.langchain.com/langsmith/evaluation

[R26] LangSmith Datasets. Versioned evaluation datasets. https://docs.langchain.com/langsmith/manage-datasets

[R27] OpenAI GPT-5.6 Luna. Cost-sensitive GPT-5.6 model; current model capabilities/pricing page. https://developers.openai.com/api/docs/models/gpt-5.6-luna

[R28] OpenAI GPT Transcribe. High-accuracy speech-to-text for file/realtime transcription. https://developers.openai.com/api/docs/models/gpt-transcribe

[R29] xAI Grok 4.6 release. Grok 4.6 positioning for long-running agentic and research work. https://x.ai/news/grok-4-6

[R30] AWS AgentCore Release Notes. Current AgentCore features, security and platform updates. https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/release-notes.html

[R31] AWS AgentCore Gateway. Secure gateway for agents/tools/models, auth, tool translation and LangGraph compatibility. https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html

[R32] AWS Secrets Manager Encryption. Secret encryption and KMS monitoring. https://docs.aws.amazon.com/secretsmanager/latest/userguide/security-encryption.html

[R33] Google Calendar OAuth Scopes. Least-privilege Calendar OAuth scope selection. https://developers.google.com/workspace/calendar/api/auth

[R34] Google Sheets API Overview. Sheets REST API read/write/create capabilities. https://developers.google.com/workspace/sheets/api/guides/concepts

[R35] Google Sheets Release Notes. 2026 updates including developer-preview Sheets MCP. https://developers.google.com/workspace/sheets/release-notes

[R36] Meta Instagram API official Postman collection. Instagram professional account messaging, conversations, comments and insights; user-initiated conversation model. https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api

[R37] Meta WhatsApp Cloud API official Postman collection. Official WhatsApp Business Platform API overview. https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api

[R38] Sandler Pain Funnel. Three-level pain discovery: specificity, impact, stakes. https://go.sandler.com/stp/insights/blog/categories/sales-process/pain-funnel-questions-for-b2b-sales-the-three-le/

[R39] Gong Discovery Research. Discovery conversation research across 519k calls; depth and conversational style. https://www.gong.io/blog/deal-closing-discovery-call

[R40] MEDDPICC. Qualification framework: metrics, economic buyer, criteria/process, pain, champion, competition. https://meddpicc.com/

[R41] Challenger Sales Methodology. Teach, tailor, take control and constructive tension. https://challengerinc.com/what-is-challenger-sales-methodology/

[R42] Composio LinkedIn Toolkit. LinkedIn posting/comment/statistics/profile/network operations and managed authentication context. https://docs.composio.dev/toolkits/linkedin

[R43] Composio LinkedIn Ads Toolkit. Separate LinkedIn advertising operations/credential surface; managed app availability differs from standard LinkedIn toolkit. https://docs.composio.dev/toolkits/linkedin_ads
