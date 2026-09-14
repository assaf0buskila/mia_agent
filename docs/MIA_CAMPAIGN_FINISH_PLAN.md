# Mia — Campaign-Ready Finishing Plan

**Status:** Proposed implementation contract. Not implemented, tested end to end, or deployed by this review.
**Prepared:** 2026-09-14.
**Project:** `assaf0buskila/mia_agent`; Leo remains separate and unchanged.
**Source baseline:** GitHub `master` was rechecked at `4b80f31d731cf1b358ce4d6f1c2a77c86a46f6ce`.
**Purpose:** Finish the existing Mia as a premium, useful business-operator demonstration before Assaf promotes it. This is not a rebuild or permission to launch a campaign.

## 1. Product decision

Mia has two experiences with shared infrastructure, not shared permissions:

- **Public website:** answer accurately, demonstrate a relevant business use case, understand the visitor, obtain voluntary contact/consent, and pass useful context to Assaf.
- **Private owner Telegram:** understand Assaf's requests; read authorized Gmail, CRM, Calendar and analytics; create strategies and local drafts; prepare exact supported actions for approval; report actual outcomes.

The product promise is: **understand → prepare useful work → obtain approval when required → execute reliably → show the result.**

“Premium” means correct information, excellent Hebrew, clear hierarchy, quick feedback, useful follow-ups and reliable completion. It does not mean a dashboard, more agents, more integrations, or a stronger model on every request.

Retain PostgreSQL, existing Composio/provider adapters, the current bounded owner loop, visitor isolation, delivery receipts/outbox, exact approvals and ambiguous-outcome safeguards. Do not transplant Leo's planner/writer pipeline into Mia merely because it exists in another project.

## 2. What was actually checked

### Sources and limits

- Rechecked GitHub's master SHA. Reviewed the existing owner/website/notification/approval paths from the previous audit and inspected additional CRM, Calendar, analytics and effect-routing code at the same SHA.
- Read the prior `MIA_TELEGRAM_UPGRADE_PLAN.md`, which is a proposal, not evidence of implementation.
- Opened the configured CRM workbook through the Google Drive/Sheets connection, read metadata and **only** `Contacts!A1:P3` and `Activity!A1:H6`.
- Consulted official Claude Code, Telegram, Gmail, Sheets and Calendar documentation for implementation constraints.
- No repository changes, provider writes, mail sends, Calendar changes, Sheet edits, deployment or full test run were performed in this review.
- The public Mia health endpoint could not be retrieved here. This is not evidence of an outage. Deployed SHA and production configuration remain to be verified.
- The Sheets connection used for this inspection is ChatGPT's connection. Its success does not prove Mia's separate Composio connection works.
- Social API/account eligibility and publishing permissions have not been verified live.

### Carry-forward findings from the prior audit

Reproduce on the actual working/deployed revision before changing them:

1. Owner plain prose is escaped for HTML; Markdown is not rendered. Existing Telegram formatting helpers are underused.
2. `_looks_silent` can classify useful `היי`/`hey`-prefixed answers as empty and replace them with raw tool reports.
3. `owner_status` advertises a technical digest as a greeting. Inventory should not run a provider-wide health audit.
4. Website `business_context` can latch onto a greeting. The lead summary repeats raw fragments rather than useful facts.
5. First notification is assembled before same-turn model metadata is absorbed. Later metadata does not automatically repair an already-issued brief.
6. V2 capture and legacy lead/reporting paths must be reconciled before relying on counts.
7. Exact approvals exist, but their displayed cards are not guaranteed to show their complete stored envelope; multiple actions and pending views are awkward.
8. Callback response can report `sent: true` after a Telegram edit failure.
9. `gmail_summary` means stored-thread summary, not a generic live-inbox brief.
10. Generic `פוסט` can outrank explicit LinkedIn in toolkit hints.
11. Short-result emptiness heuristics and failed-turn usage propagation need correction.
12. Dead-code lists and older deployment docs are hypotheses until checked against the current code and persisted records.

### New finishing blockers / checks

**CRM valid-input path:** `app/tools/owner/crm.py:_crm_upsert` performs intent/contact checks and then falls through before `_crm_record_activity`, without creating a proposal or returning a `ToolResult`. The registry binds this handler and returns its result directly. This is a source-level defect on the inspected SHA; production impact is not yet measured. Add a regression that exercises the actual registry and valid-input path, not a mocked replacement handler.

**Live workbook observations:** the workbook titled `Mia — AssafWeb operating mirror` exists with Contacts and Activity, frozen headers, Hebrew locale, and timezone `Etc/GMT`. The sampled Contacts header includes the Mia ID and a schema marker. One sampled phone is stored as a number; one legacy sampled row lacks a Mia ID; a release-test row is present. The sampled Activity rows use generic action/result text and do not show a field-level before/after trail. This is a small sample, not a full-data audit.

**Gmail distinction:** `gmail.create_draft` is executed by the typed approval adapter. `GMAIL_SEND_DRAFT` has an explicit snapshot/approval route in `composio_effects.py`. Direct generic send/reply/forward is unavailable there. Finish a coherent owner UX around verified paths; do not claim draft creation equals sending.

**Calendar scope:** typed owner tools support agenda/availability, create and reschedule proposals. The inspected create contract has title/time/duration/location, not a complete invitation/recurrence management contract. Do not promise all Calendar operations.

**Social scope:** own LinkedIn profile and Instagram Insights readers exist. LinkedIn proposal paths exist but live success is unverified. Instagram mutations are explicitly denied in the effect policy; “manage Instagram” cannot silently enable publishing. LinkedIn profile access is not proof of post/impression analytics access.

## 3. Campaign scope and exclusions

### Required campaign stories

A. Website prospect → useful conversation → consented capture → grounded Telegram lead brief → correct CRM record and current reporting.
B. Assaf requests an email daily brief → Mia prioritizes current messages → prepares a reply → exact send approval → confirmed provider outcome.
C. Assaf requests today's schedule → Mia shows current events and next event → proposes one safe create/move → exact approval → verified result.
D. Assaf asks what changed in CRM → precise human-readable answer and record reference; approved update → database/activity → correct Sheet sync outcome.
E. Assaf asks for LinkedIn/Instagram strategy → grounded, channel-specific recommendations and editable drafts/plan, with missing analytics labeled honestly.

Actual social publication is a separate capability gate. Advertise it only for the account, platform and content type tested end to end. Instagram publication is an explicit policy/feature extension, not cleanup. Without that extension, describe the release as strategy/drafts/analytics, not automated Instagram management.

### Not part of finishing by default

- Mia/Leo merger, a new agent framework, runtime multi-agent orchestration or a generic integration platform rewrite.
- Bulk email, cold DMs, autonomous social posting, ad-spend/campaign mutations, destructive operations or production self-editing.
- New automatic morning notifications. Daily briefs are **on demand** in this release.
- Full social scheduling infrastructure, media generation, recurring-event editing, arbitrary calendars or broader scopes without an explicit product decision.
- A new dashboard or a new CRM workbook. Use the existing Sheet and Telegram.
- Silent deletion of legacy data/test rows or rewriting old numbered migrations.

## 4. The message design contract

### Voice

Natural professional Hebrew, warm but not overfamiliar, concise and useful. Match English when requested. Avoid habitual “אחי”, robotic menus, unnecessary apologies, raw tool names, raw IDs, `**`/`###` artifacts, and repeated security disclaimers.

Answer first. Ask at most one necessary question by default. Use specific uncertainty: missing data, failed read and empty result are different. Longer explanations are appropriate when requested; brevity must never hide recipients, attachments, scope or consequences of an approval.

### Three presentation types

1. **Conversation:** short prose; a greeting is not a status report and needs no operational tool.
2. **Brief/card:** title, key information, implications/next step, bounded evidence/coverage, optional relevant controls.
3. **Approval/result:** backend-authored from the exact stored action, with complete reviewable parameters and truthful status.

Reuse `telegram_format.py`. Either keep prose plain or parse a small allowlisted Markdown subset safely. Do not trust arbitrary LLM/provider HTML. Escape once; preserve literal code when requested; only server-approved references generate action links.

Split semantic blocks before serialization or maintain valid independent markup per chunk. Account for Telegram's rendered-length and callback constraints. Never resend an accepted chunk because a later chunk failed. Keep controls bound to a reviewable complete proposal, not an arbitrary first chunk of a truncated preview.

Use normal Telegram messages, native buttons and expandable detail where appropriate; no dependency on a new rich-message API. Verify mixed Hebrew/Latin direction on Assaf's real phone. A HTML/browser fixture is helpful, but not a substitute for a Telegram client check.

### Illustrative copy (fictional; not current account data)

Greeting:
> היי אסף 👋 מה תרצה לקדם?

Mail brief:
> **המיילים להיום**
> נבדקו [מספר] הודעות בטווח [הטווח בפועל].
> **דורש תגובה:** [פנייה עסקית וסיבת העדיפות].
> **לידיעה:** [עדכון חשוב].
> **שיווק ועדכונים:** [סיכום מרוכז].
> [הכן תשובה] [הצג מייל]

Lead:
> **פנייה חדשה מהאתר**
> **העסק:** [רק אם נאמר].
> **הצורך:** [הבעיה שהפונה תיאר].
> **מה כבר הוסבר:** [נקודה רלוונטית, לא כל התמליל].
> **השלב הבא המומלץ:** [המלצה, לא הבטחה של הלקוח].
> **יצירת קשר:** [ערך שאומת בשרת].
> [הצג שיחה] [הכן תשובה] [פתח ב-CRM]

Approval:
> **לאישור: שליחת מייל**
> **מאת / אל:** [החשבון והנמענים המדויקים, כולל CC/BCC כשיש].
> **נושא:** [מדויק].
> [כל התוכן והקבצים לבדיקה].
> המייל עדיין לא נשלח.
> [אשר שליחה] [ערוך] [ביטול]

Read failure:
> לא הצלחתי לקרוא כרגע את היומן. אין לי תמונת מצב עדכנית.

Uncertain write:
> הפעולה התחילה, אבל לא התקבל אישור לתוצאה. לא אבצע אותה שוב לפני בדיקה.

## 5. Capability contracts

### 5.1 Gmail: brief → useful response → approved send

**On demand:** interpret “סיכום מייל”, “מה חשוב במייל היום?” and English equivalents as a bounded live-mail task, not a request for a legacy thread ID. Resolve the local day/time window using the configured owner timezone. State the inspected range, count and whether coverage is partial. Fetch body/thread content when needed for claims; a subject/snippet alone is not proof of detailed instructions or deadlines.

Group by needs action, information and marketing. Deduplicate threads. Do not mark read, archive, delete, unsubscribe, forward or send as a side effect of summarizing. No request to approve ordinary authorized reads.

**Reply composition:** a local editable draft is not a Gmail draft. Resolve the exact message/thread and reply target; do not confuse display names, Message-ID and thread IDs. Changing recipient/content/attachments invalidates prior approval.

**Send UX:** the desired experience is reviewed content → one clearly scoped send approval → actual outcome. First verify the existing draft/snapshot/send path end to end. Reuse it rather than adding a parallel sender. A new-email implementation may use an explicitly approved prepare-and-send operation only if its complete effects are presented and each provider step has durable recovery state; otherwise keep truthful separate save-draft/send steps. Never silently add an unapproved provider-draft write just to achieve one button.

Preserve account binding, full recipient list, thread semantics, body and attachment manifest; reject unsupported fields instead of dropping them. Gmail threading requires the provider's proper thread/header mapping. Re-read/hash a provider draft immediately before an approved send; a user-edited draft requires fresh approval. After an uncertain send, reconcile, do not blindly recreate/resend. Provider acceptance is not proof of inbox delivery/read.

**Acceptance:** live brief routes correctly; injected mail instructions cannot authorize actions; a tested controlled message sends once after approval; altered/expired drafts do not send; failed Telegram presentation never repeats the email.

### 5.2 CRM and the existing Google Sheet

Retain Contacts/Activity, database as source of truth and editable owner projection. Do not recreate, clear, reorder or rename the workbook. Discover the configured runtime target first; the inspected code default is not proof of production's override.

First conduct a read-only full schema/identity/format audit. Do not invoke a nominal “read” helper that calls `ensure_crm_workspace` or imports/writes during that audit. Inspect provider metadata and database through read-only routes. Permission to inspect is not permission to change headers or repair data.

**Mia may:** capture consented website inquiries under the already-approved product flow, log system outcomes, synchronize authorized fields, and propose owner-requested contact/activity changes.

**Mia may not silently:** overwrite manual edits, alter protected IDs/headers/schema markers, change permissions, delete rows, merge identities, invent contact details, or use raw Sheets writes to bypass CRM revision rules.

Fix `_crm_upsert` so valid input resolves identity, reads current revision, creates an exact proposal and returns a typed result. The model must not select an arbitrary row number as durable identity.

Make “what did Mia touch?” inspectable: action/operation ID, actor, source channel, stable contact ID, changed fields with before/after values, approval reference or authorized system-trigger reference, timestamp, database outcome and Sheet-sync outcome. Use the existing Activity view; keep compact readable summaries there and richer detail in the database. Any additional columns require a compatible explicit migration, not opportunistic header editing.

Display `saved to CRM`, `Sheet sync pending`, `synced` and `conflict` distinctly. An owner read may show the last confirmed database state with a freshness warning when Sheet sync fails; a write needing current target evidence must wait. Never treat a read failure as “no contacts”.

Phones/IDs must be text. Use RAW string values for system-written contact fields and text formatting for manual-entry columns. Do not reconstruct a missing leading zero or missing identity by guessing; reconcile against authoritative stored input and require review when ambiguous. Confirm timezone handling before changing the workbook's current GMT setting. Preserve ISO offsets.

Proposed cosmetic finishing: readable RTL, wrapped bounded summaries, filters, stable frozen headers, and clear status/conflict formatting, **without changing data or layout contracts**. No styling change is authorized by this plan alone.

### 5.3 Calendar: know the day and execute deliberate changes

Mia should read today's/tomorrow's/week's configured calendar scope, show local times, all-day items, next event, conflicts and genuinely free windows. Distinguish “no events returned”, “no access” and “partial calendar scope”. Do not pretend a primary-calendar read covers every calendar.

For “move that meeting”, resolve the exact event from current source/card context. Present old → new time, date/timezone, duration, target calendar and attendee-notification effects. Revalidate before execution. Availability checks for rescheduling must not falsely count the event being moved as an unrelated conflict. Handle daylight-saving/timezone boundaries and events changed after preview.

Owner create/reschedule are first-class campaign operations; invitation options, cancellation, recurrence-series scope and multiple calendar writes require separately supported exact contracts. Never silently update an entire series or add guests. Provider `sendUpdates` behavior is part of the approved consequence, not a hidden adapter default.

**Acceptance:** grounded daily view; clarified ambiguous event; explicit approval; changed target rejected; one actual controlled create/move; correct follow-up result.

### 5.4 Social: strategic helper first; supported actions second

**Core:** channel-specific goals, content pillars, short plans, hooks/captions/CTAs, repurposing, profile suggestions, and post-performance recommendations where real metrics exist. Clearly separate observation, inference and recommendation. A plan is not a schedule, and a draft is not a published post.

Use current available owner facts/approved business knowledge. Invoke live account reads only when needed. Ask one goal/audience question only when the answer materially changes the plan. Do not run Instagram, LinkedIn, GSC and GA4 on every social question.

LinkedIn own-profile data does not imply reach/follower/post analytics. Instagram Insights only supports what the connected account and adapter actually return. Missing metrics stay unavailable, not zero; never invent “best posting time” or performance improvements from unsupported data.

**Publication gate:** resolve account + platform + full text + exact media + audience + timing; preview; bind approval to that content and destination; use a verified executor; store resulting post reference/uncertain outcome. Edits revoke the prior proposal. No autonomous DMs or ad-spend operations.

Instagram publishing is currently policy-denied. Before implementation, Assaf must explicitly approve expanding the policy for exact, manually approved publishing. Then verify official account eligibility, scopes, chosen media types and provider support. Do not just remove a deny-list entry. Scheduled posting remains excluded until durable scheduling, cancellation, expiry and changed-content approval semantics are separately designed.

**Acceptance:** explicit LinkedIn requests route to LinkedIn; both platforms receive appropriate plans; suggestions cite available inputs; unsupported publication is transparent; campaign claims list only verified writes.

### 5.5 Website → Assaf: context-rich conversion

Keep the existing public surface and session isolation. Match the finished AssafWeb design rather than independently rebranding Mia. Brief intro, useful starter prompts, readable RTL, mobile keyboard handling, honest loading/error states, accessible controls, and persisted thread continuity should be checked in the actual landing-page repository. Read-only HTML inspection alone cannot validate the JavaScript widget.

Answer before qualifying; reuse known facts; explain one concrete relevant use case; collect contact voluntarily. Never expose owner inbox, Calendar, private CRM, memory, tool inventory or approval controls to visitors.

Maintain a bounded `LeadSnapshot` concept (use existing schema seams where practical): source session/turn, stated name/business/problem/current process, service/desired outcome, important objection, validated contact, consent evidence and proposed next step. Recommendations are separate from customer facts. Attribution is untrusted analytics, never consent/qualification evidence.

Finalize available fields before constructing the first notification intent. An enrichment/model failure must not lose a valid contact: persist a truthful partial snapshot and the existing durable intent. Do not reorder capture wholesale or silently invalidate consent/status/receipt invariants. A sent notification is an immutable snapshot; latest conversation detail may be read separately. No extra ping per message.

Bind Telegram card IDs to owner-authorized contact/session IDs. “הכן לו תשובה” resolves from the referenced card, never from the most recent unrelated person. If context is ambiguous, ask. A phone-only visitor does not authorize Gmail; opening WhatsApp is not sending a message.

Ensure current website leads, owner reports and Sheet records refer to the same source/session. Do not fabricate legacy qualification/delivery when backfilling. Separate contact captured, notification accepted by Telegram, Assaf reviewed/responded, meeting and commercial conversion.

## 6. Other existing capabilities worth retaining

Friendly capability view: Gmail, Calendar, CRM, website leads, LinkedIn profile, Instagram Insights, website performance/SEO, public research, public business knowledge and explicit owner memory. Show registered, configured, last verified, approval-required, unavailable and not implemented separately.

Keep public knowledge/owner memory behind their existing boundaries. Remember durable owner preferences only on explicit request; an explicitly requested local work draft is workflow state, not permission to memorize everything.

GSC/GA4 reports must label their supported window. Do not describe a fixed 28-completed-day report as today's campaign results. A broad integration audit is an explicit longer task with bounded checks and partial results, not the response to “what can you do?”.

No added connector just for its logo. Every showcased tool must have a real user journey, permission contract and test evidence.

## 7. Execution in Claude Code

Use the current repo's existing source-of-truth files: `AGENTS.md`, `HANDOFF.md`, `MIA_V2.md`, `TASKS.md` and the relevant README instructions. Do not create a competing documentation system or import the whole conversation at startup.

Add a small `CLAUDE.md` importing `@AGENTS.md` only if it is absent; inspect existing/nested instruction files first. Reference this plan for on-demand reading rather than auto-importing it. Handle the existing development-tier mapping as an explicit Claude Code session configuration decision, not a production-model migration.

Recommended coding roles: HEAVY = available Opus for planning/security/migrations/fresh review; MID = available Sonnet for normal bounded implementation; CHEAP = available Haiku for mechanical work only when delegation saves total effort. `opusplan` is an official hybrid alias, but does not provide independent review automatically. Confirm availability and actual selected model locally; do not use version names from older unverified chat claims.

Default to one builder. Use parallel workers only on frozen interfaces and disjoint ownership. Keep related debugging context when helpful; start a fresh session at a real boundary after a durable handoff, not blindly after every command. A reviewer must receive requirements, changed code and test evidence, not rely on the author's narrative. After fixes, review the final diff again.

Scope each work item to one provable change. Tests/acceptance first, focused tests during iteration, affected integration gates at chunk end, full existing checks before release. Do not add strict whole-repo tooling solely for this upgrade unless already configured. Never weaken a test to make the build pass.

No more than two unsuccessful repair cycles without a diagnosis and smaller repro/escalation. Track files/context read, model/tool calls, elapsed work, rework and verification—not only token price. Do not launch a subagent for a small mechanical edit.

## 8. Approval-gated work packages

These are workstreams. Split the listed subchunks into separate sessions; do not interpret one row as permission to implement everything at once.

| ID | Deliverable | Suggested atomic subchunks | Exit evidence |
|---|---|---|---|
| C0 | Baseline and release contract | repo/production mapping; read-only capability/Sheet audit; regressions and policy decisions | actual versions, observed defects, blocked capabilities, approved next chunk |
| C1 | Typed-tool correctness | CRM valid-path fix; invalid-handler-result protection; relevant usage/outcome fixes | actual registry valid-input regression, no unauthorized writes |
| C2 | Message and approval presentation | prose renderer; safe splitting; greeting/override/hint fixes; exact proposal cards | snapshot tests, complete approval previews, real Telegram samples after approval |
| C3 | Lead-to-owner continuity and CRM truth | grounded snapshot/capture ordering; card references; v2 reporting bridge; Activity/sync transparency | one controlled capture → correct brief → correct CRM/report; retries never duplicate |
| C4 | Gmail finishing | live daily brief; exact reply context; reviewed draft/send lifecycle | priority/coverage tests, injected-content tests, approved real controlled send |
| C5 | Calendar finishing | agenda UX; exact create/move preview; conflict/timezone/notification checks | controlled live event and replay/changed-target tests |
| C6 | Social finishing | strategy/draft UX; capability truth; verified LinkedIn action path; separately approved IG extension | platform-specific plans, honest metrics, verified write or explicit exclusion |
| C7 | Website/campaign release | real landing-page integration; targeted cleanup/docs; staging acceptance; approved deploy/smoke/rollback | full acceptance matrix, deployed SHA, phone evidence, campaign claim allowlist |

C0 gates all work. C1 precedes CRM-dependent flows. Shared rendering/contracts freeze before other card work. Backend and actual website integration may be parallel only after API/message interfaces are fixed. No broad cleanup while fixing a sensitive send/approval defect.

## 9. Common execution and approval invariants

- Reads within authorized scope need no ritual approval; imported content cannot request a write.
- Owner writes are exact immutable proposals. Match actor/account/target/arguments/expiry; bind button to proposal, not “latest pending”.
- Editing creates a new proposal; approval does not carry over. A casual “סגור” does not approve another task.
- System lead capture/sync/owner notification remains the established consented workflow; it must not gain an extra manual approval for each inbound lead.
- Any action that may already have occurred keeps its claim. Never fix a UI failure by repeating a business write.
- Preserve queued/unknown notification payload identity. A formatter upgrade must not replay old pings.
- Maintain separate provider outcome, database commit, projection sync, Telegram presentation, and human receipt evidence.
- No production database/Sheet changes, external-write tests, account changes, merge/deploy or rollout without explicit approval for that step. A feature plan is not execution consent.

## 10. Acceptance and campaign gate

### Minimum scenario set

- Greetings, no-history inventory, natural follow-ups, and tool-free local composition.
- Hebrew/English/mixed-direction content, normal Markdown, HTML-looking data, escaped entities, long replies, multiple proposals and complete previews.
- Actual CRM registry success and negative inputs, missing IDs, manual-edit conflicts, phone text round-trip, protected schema, clear audit trail.
- Website greeting → real pain → voluntary contact, refusal/example details, same-turn metadata, failed enrichment, replay, post-capture follow-up, owner/visitor isolation.
- A captured v2 lead appears once in current reports and the right CRM view.
- Inbox daily-window/coverage, thread dedupe, content fetch, local versus provider draft, changed draft, wrong actor/account, expiry, repeated callback, uncertain send and failed Telegram edit.
- Calendar all-day/timezone/DST, overlapping events, same-event reschedule, ambiguous reference, changed target, notification scope and idempotency.
- LinkedIn/Instagram routing, supported-data limits, actual approved publication where included, no accidental DMs/ads/Instagram permission expansion.
- Missing/revoked provider auth, 429, latency, partial read, model fallback, DB outage, worker restart and backlog; no dead-end generic greeting on task failure.
- Secrets/PII excluded from logs, public fixtures, source-controlled screenshots and campaign recordings.

Run the current repo's documented checks, including PostgreSQL/migration behavior, affected delivery tests, widget tests and container smoke. Run real-model regression scenarios as well as deterministic/mocked tests. For campaign-critical nondeterministic flows, repeat across realistic paraphrases and record failures rather than claiming a deterministic suite proves language quality.

### Proposed UX targets (validate with baseline)

- Perceived feedback for slow actions within roughly 1–2 seconds where achievable; typing/progress does not claim completion.
- Greeting/inventory: no avoidable operational/model round trip.
- Common single-source read: target p95 under 15 seconds; do not promise this before measurement. Broad audits use a different budget and clearly bounded scope.
- Capture persists even if enrichment fails; notification delay measured separately from contact acceptance and Sheet sync.
- Zero known authorization, wrong-recipient or duplicate-side-effect defects in the release acceptance set. Passing a set is not proof that all possible failures are impossible.

### Evidence states, not a completion percentage

Track each capability as: `CODE_CHECKED`, `LOCAL_TESTED`, `INTEGRATION_TESTED`, `STAGING_VERIFIED`, `DEPLOYED`, `PHONE_VERIFIED`, `BLOCKED`, or `NOT_INCLUDED`. A green CI result is not a live-tool certificate.

Campaign only advertises the intersection of implemented + permitted + tested + deployed behavior. Public visitors never receive Assaf's owner tools. Demonstrate owner functions using controlled test messages/accounts/events and anonymized records; label sample data. Do not expose real private inbox/calendar/contact data in the recording.

The flagship recording should show one complete journey: visitor need → lead notification → CRM → owner-approved follow-up → provider result. Film mail/calendar/social demonstrations separately as supporting capabilities. No mock success animation presented as a real action.

## 11. Rollout and cleanup

Use existing feature/config seams where available, clean worktrees, additive compatible migrations and a clean CI-green release SHA. Keep the prior image and a compatible schema rollback. A rollback must not reset execution or delivery claims. Before deleting any helper, check imports, dotted-string references, registries, entry points, schedules, migrations, callers outside the immediate module, and outstanding persisted proposals/jobs. An internal alias with live consumers is not dead code.

Do not delete legacy Sheet rows based on names alone. Classify fixtures explicitly for analytics/recording; retain history unless a separate deletion request is authorized.

Reconcile the existing docs after behavior is verified, not before. Store status with commands, outcomes, skipped checks, commit SHA, exact blocker and next approved subchunk. Stop at the gate instead of calling a partial feature “done”.

## 12. Source map for the coding agent

Inspect these at the actual working SHA; paths are navigation hints, not proof they are unchanged:

- Root contract/state: `AGENTS.md`, `HANDOFF.md`, `MIA_V2.md`, `TASKS.md`, `README.md`.
- Owner routing/render: `app/surfaces/owner.py`, `app/domain/owner/brain.py`, `app/graph/owner_agent.py`, `app/domain/owner/request_routing.py`, `app/domain/two_state.py`.
- Telegram: `app/api/inbound_common.py`, `app/integrations/telegram_format.py`, `app/integrations/telegram.py`, `app/api/telegram.py`.
- CRM: `app/tools/owner/crm.py`, `app/tools/registries/owner_tools.py`, `app/services/crm_v2.py`, `app/surfaces/crm.py`, `app/integrations/sheets.py`, `app/workers/crm_delivery.py`.
- Website/notifications: `app/surfaces/site_v2.py`, `app/workers/crm_runtime.py`, `app/services/notifications.py`, actual widget/landing-page integration.
- Reporting: `app/domain/owner/briefs.py`, `app/domain/owner/reads.py`, `app/domain/handoff/hot.py`.
- Gmail: `app/tools/owner/gmail.py`, `app/domain/gmail/summaries.py`, `app/services/owner_actions.py`, `app/domain/owner/composio_effects.py`, current draft/send adapters and callbacks.
- Calendar: `app/tools/owner/calendar.py`, `app/services/owner_actions.py`, agenda/booking adapters.
- Social/other: `app/tools/owner/analytics.py`, `app/tools/owner/composio.py`, `app/domain/owner/composio_effects.py`, current provider schemas/executors.
- Existing CI: `.github/workflows/ci.yml`.

Official references consulted (recheck before relying on version-specific behavior):
- Claude Code model configuration: `https://code.claude.com/docs/en/model-config`
- Claude Code best practices: `https://code.claude.com/docs/en/best-practices`
- Claude Code memory and AGENTS.md import: `https://code.claude.com/docs/en/memory`
- Telegram formatting/buttons: `https://core.telegram.org/bots/api`
- Gmail drafts: `https://developers.google.com/workspace/gmail/api/guides/drafts`
- Gmail thread mapping: `https://developers.google.com/workspace/gmail/api/guides/threads`
- Sheets RAW versus USER_ENTERED: `https://developers.google.com/workspace/sheets/api/reference/rest/v4/ValueInputOption`
- Calendar patch and sendUpdates: `https://developers.google.com/workspace/calendar/api/v3/reference/events/patch`

Official Meta/LinkedIn publishing pages were not retrievable in this review. Their current account eligibility and tool contracts remain a required capability-gate check; no secondary-source claim is substituted for that verification.
