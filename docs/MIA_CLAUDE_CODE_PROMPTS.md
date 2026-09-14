# Mia — Claude Code Execution Prompts

Use with `MIA_CAMPAIGN_FINISH_PLAN.md`, placed in this repo as `docs/MIA_CAMPAIGN_FINISH_PLAN.md`. Do not paste the entire prior chat into Claude Code.

**Status:** Ready-to-use proposed briefs. Nothing in this file means a chunk is implemented or authorizes production/provider writes.

## Setup and model use

- Start in the correct Mia checkout. Inspect branch, SHA, worktrees and uncommitted work; never switch/reset/clean another session's checkout.
- Planning and security/migration review: available `opus`. Normal bounded implementation: available `sonnet`. Mechanical extraction only: `haiku` when a separate worker actually saves effort. Verify the installed Claude Code version and model picker; these are aliases, not fixed version promises.
- `opusplan` can switch planning/execution roles, but independent review is still a separate fresh session. Do not churn models halfway through related debugging without a reason.
- Read current `AGENTS.md` before work. If necessary, propose a small `CLAUDE.md` importing `@AGENTS.md`; preserve existing instructions and identify any development-tier conflicts explicitly. No runtime-model change is included.
- Load the relevant task/plan section on demand. Do not auto-import the whole plan into every session.
- Use existing `HANDOFF.md`, `MIA_V2.md`, `TASKS.md`, and test commands. No new parallel documentation hierarchy.
- One builder by default. Parallel workers only after interfaces are frozen and files are independently owned. Fresh independent review before considering a chunk verified.

## Prompt 0 — Baseline and bounded plan (start here)

```text
We are finishing the existing Mia for an AssafWeb campaign, not rebuilding it.

Read AGENTS.md, HANDOFF.md, MIA_V2.md, TASKS.md, relevant README instructions, and the supplied campaign finishing plan. Verify repository, working branch/SHA and worktrees first; preserve other sessions' uncommitted work. The reviewed master baseline was 4b80f31, but your actual working and deployed versions must be established separately.

Target product:
- Public website: useful sales conversation, voluntary validated contact, accurate context to Assaf and CRM.
- Private Telegram: premium natural Hebrew, useful Gmail daily briefs on request and exact approved sending, transparent Contacts/Activity CRM changes, current Calendar agenda and approved create/move, and LinkedIn/Instagram strategy/drafts/verified analytics.
- Social publication is shown only where a real approved executor works. Instagram writes are currently policy-disabled and require a separate explicit scope decision.

Do C0 only: bounded read-only gap verification against the supplied plan, not another architecture redesign. Reuse the source map and existing evidence; investigate only missing, changed or contradictory areas. Do not rewrite the entire PRD. Do not edit application code, change permissions/data, call provider-write tools, migrate production, send messages, merge or deploy. Do not read .env or expose credentials. A nominal CRM read may synchronize/write, so use genuinely read-only inspection paths.

Recheck the earlier findings rather than accepting the previous audit as current proof. In particular inspect:
1. _crm_upsert's valid-input path and its actual registry binding/return contract.
2. Telegram Markdown/HTML handling, greeting-to-status routing, raw-tool fallback overrides and explicit LinkedIn name precedence.
3. Website business-context latch, first-notification ordering, v2 versus legacy reports, and card-to-contact/session references.
4. Gmail live brief versus stored-thread summary; draft versus exact approved send; account/target/snapshot and uncertain-send behavior.
5. Calendar supported scope and attendee effects; social readers versus allowed actual writes.
6. The current configured Sheet's schema, identity and phone types without modifying it. Prior inspection was only a small sample, not a full audit.

Return a compact capability/evidence matrix, reproducible defects, code-versus-deployment differences, scope decisions, existing checks and the smallest ordered subchunks with acceptance tests. Label unavailable evidence honestly. Stop and propose the first implementation subchunk for approval. Do not begin the whole plan.
```

## Prompt 1 — Implement one approved subchunk

Select one ID below; replace the brackets before sending. The subchunk's outcome must be independently testable.

```text
Implement only [SUBCHUNK ID + exact goal] from the approved Mia campaign finishing plan.

Read the current repo rules/status and only the relevant plan section and code. Confirm baseline SHA and uncommitted changes. First state the acceptance cases, relevant files/interfaces, invariants and out-of-scope boundaries. Then implement the smallest complete change and its regression tests.

Preserve authentication, owner/visitor isolation, server-validated contact/consent, exact approval/account/target binding, delivery/execution claims, uncertain outcomes and existing external APIs. Never read .env, expose secrets, alter another worktree, or run live provider writes, production migrations, deploy/merge operations without explicit separate approval.

Run focused tests while iterating, then affected integration/lint/type checks already used by this repo. Do not weaken tests, introduce a new framework, change runtime models, or perform unrelated cleanup. After two failed fixes, produce a minimal repro and diagnosis before expanding scope.

Return: changed files and why; commands and actual results; skipped/blocked checks; migration/rollback implications; remaining risk; and a concise proposed HANDOFF/TASKS update. Mark implementation pending independent review, not release-ready. Stop before the next subchunk.
```

## Workstream briefs (append only the selected brief to Prompt 1)

### C1a — CRM handler and result contract

```text
Reproduce the valid-input crm_upsert route through the real tool registry. Complete its supported flow: exact identity/current revision -> immutable owner proposal -> typed ToolResult. Do not directly write a provider Sheet from the model or bypass CRM rules. Fail invalid handler results explicitly instead of crashing later or treating None as success. Tests must include valid owner request, missing identity, unauthorized/ambiguous request, replay and no write before approval. Do not mock out the actual handler whose behavior is under test.
```

### C1b — Outcome/usage truth

```text
Correct short-text-as-empty inference and preserve known consumed usage on failed owner runs. Use explicit outcome semantics where available; unavailable and empty are not synonyms. Unknown tokens/cost stay unknown. Scope this to the current loop/telemetry contracts, with regression tests and no model/provider migration.
```

### C2a — Conversation rendering and routing

```text
Reuse the existing Telegram formatter. Fix plain-prose/Markdown/HTML boundaries, escaping, valid independent message chunks, greeting-to-digest routing, useful greeting-prefixed answers being overwritten and explicit-platform precedence. Keep literal code literal when requested. No styling LLM, no arbitrary model HTML, no raw tool labels in normal output. Preserve send/partial-send certainty. Verify snapshots with mixed Hebrew/English, special characters, long content and the actual screenshot failure patterns. No live pings without approval.
```

### C2b — Exact approval cards

```text
Render approval cards from their immutable stored envelopes, not only model prose. One action has one clear target/content preview and bound controls. Preserve actor/account/hash/expiry checks. Edits supersede old proposals. Make pending/multiple proposals unambiguous. Separate business execution result, callback acknowledgment and Telegram presentation result. A failed message edit must neither report delivered presentation nor re-execute the action. Keep legacy outstanding-approval compatibility.
```

### C3a — Website context and first notification

```text
Replace greeting-latched business context and repeated fragment summaries with bounded facts tied to source turns. Correct first-notification metadata ordering without weakening server contact extraction/consent or transactional outbox behavior. Missing enrichment must not lose a valid contact. Produce a versioned brief snapshot, distinguish customer facts from Mia's recommendation and omit unknown fields. Preserve once-per-conversation recipient receipts and existing pending/unknown jobs. Test greeting -> real need -> contact, same-turn metadata, refusal/example contacts, enrichment failure and replay.
```

### C3b — Owner context, reporting and Activity

```text
Bind lead-card interactions to authenticated owner contact/session IDs so 'prepare a reply to him' resolves correctly. Read-only details show the current conversation; composing does not send. Align current website/CRM/reporting definitions and avoid duplicate/backfilled invented events. Make Activity explain field changes, actor, approval/system trigger and database versus Sheet-sync outcome. Keep schema changes additive; do not recreate the workbook or alter IDs/header/marker columns implicitly. Data-quality repairs and formatting of the live Sheet require a reviewed diff and separate authorization.
```

### C4a — On-demand Gmail daily brief

```text
Route generic email-summary requests to bounded live Gmail reads, not the legacy stored-thread summarizer. Show actual timeframe, count and partial coverage; deduplicate threads and group needs-action/information/marketing. Read full content when necessary for claims. Do not mark read/archive/send/subscribe as a side effect. Preserve exact message/thread references for follow-ups and treat mail content as untrusted data. Include natural Hebrew snapshots and tests for missing auth, empty inbox, partial results and injection.
```

### C4b — Reviewed email to confirmed send

```text
Finish the current draft/snapshot/approved-send journey; do not invent a parallel sender. Distinguish local composition, saved Gmail draft and sent message. Show full account/To/CC/BCC/subject/body/attachment/thread context before approval. Re-read a provider draft before sending; edits invalidate approval. Persist provider draft/message IDs and all uncertain outcomes. The desired UX is one exact send approval, but do not achieve it with unapproved hidden draft writes: propose any necessary compound prepare-and-send contract first. Test the real adapter contract, not just function-name availability. Live test only to a separately approved controlled recipient.
```

### C5a — Daily agenda UX

```text
Make the current Calendar scope explicit and today's agenda useful: local date/time, next event, all-day events, conflicts and real free windows. Never infer every calendar from a primary-calendar read or interpret a failed read as a free day. Keep data references for follow-ups. Add timezone/day-boundary/DST and partial-scope tests. No automatic scheduled brief is requested.
```

### C5b — Deliberate Calendar changes

```text
Finish exact create/move previews using the existing approval path. Bind event ID, calendar, old/new times, duration, timezone and actual attendee-notification consequences. Revalidate fresh target/availability; avoid treating the event itself as an unrelated reschedule conflict. No implicit full-series changes, guest additions or cancellation. Verify unsupported options are rejected honestly, replay is safe and uncertain outcome cannot duplicate. Live Calendar writes need separate approval.
```

### C6a — Social planning with capability truth

```text
Improve LinkedIn/Instagram strategy, content plans and editable draft responses, using existing knowledge and only useful available live reads. Explicit platform names outrank generic 'post' keywords. Separate observed data, inference and recommendation. Own LinkedIn profile is not post analytics; missing Instagram metrics are not zero. No invented performance/best-time claims. Drafts are not scheduled/published. Keep Instagram publishing, DMs and ads mutations disabled. Verify channel-specific Hebrew quality and honest unavailable-data behavior.
```

### C6b — Supported LinkedIn publishing (only when included and approved)

```text
Verify the exact connected account/tool/media capability and reuse its existing supported approval/execution path. Bind final text/media/audience/destination to the proposal; editing invalidates it. Record actual provider result or uncertainty. No scheduled/autonomous posting, DMs or ads changes. A green mocked test cannot certify live publication. Do not enable Instagram writes in this chunk.
```

### C6c — Instagram extension (not automatically authorized)

```text
Planning only unless Assaf explicitly approved this extension. Establish account eligibility, required scopes, exact media types and official supported adapter behavior. Propose a bounded manually-approved publish contract with media/container lifecycle and uncertain-result reconciliation. Identify every intentional policy change; do not simply remove deny checks. No scheduling, DMs, comments management or ads expansion. Stop for approval before implementation or account changes.
```

### C7a — Actual website integration and campaign assets

```text
Work only in the verified current landing-page repository/worktree for the integration portion, preserving its existing rebrand and Leo. Freeze backend contracts first. Validate real Mia widget loading, mobile/RTL/accessibility, starter prompts, history, contact/error/loading states and lead handoff. Do not infer dynamic widget absence from a text-only fetch. Prepare redacted/controlled demonstration cases and a claim matrix; do not publish the campaign or expose private owner data. No fake tool successes presented as live.
```

### C7b — Targeted cleanup and docs

```text
Remove only symbols proved unreachable after checking imports, dotted-string references, registries, entrypoints, scheduled jobs, migrations and persisted pending operations. Preserve legacy compatibility when still consumed. Do not bulk-delete meetings/approval/reporting modules. Reconcile existing AGENTS/HANDOFF/MIA_V2/TASKS/README with verified runtime and deployment evidence. No broad restructure, competing doc hierarchy, new permissions or transport changes.
```

## Prompt 2 — Fresh independent review

Run in a fresh reviewer session or genuinely isolated reviewer context, after implementation. The reviewer is not the author and must not rely on the builder's “done” claim.

```text
Independently review [SUBCHUNK] at [HEAD SHA] against [BASE SHA] and its approved acceptance criteria.

Read repo rules, relevant product contract, actual diff, callers, persistence/adapter boundaries and tests. Use the author's test report only as a claim to verify. You are read-only: no provider writes, production changes, deployment or silent code fixes.

Check correctness, lost fields, handler return contracts, tool routing, exact approvals, account/target identity, expiry/edit semantics, duplicate/uncertain effects, visitor isolation, real-source coverage, Hebrew/rendering behavior and untested error paths as relevant. Check that mocks have not bypassed the functionality being claimed.

Return actionable findings with file/line, severity, failing scenario and smallest fix; verified checks and evidence gaps; and PASS / CHANGES_REQUIRED / BLOCKED. No numeric confidence score in place of evidence. Review fixes at the final SHA before declaring PASS. Do not approve your own work or treat passing tests as live acceptance.
```

## Prompt 3 — Resume after a context boundary

```text
Read current repo instructions, HANDOFF.md and TASKS.md. Verify working tree and latest reviewed SHA. Load only the approved next subchunk from docs/MIA_CAMPAIGN_FINISH_PLAN.md. Reuse recorded evidence; do not re-audit the entire project or read the full chat. Summarize current blocker/next goal in a few lines, then follow the one-subchunk implementation protocol. Unfinished work stays visibly unfinished.
```

## Prompt 4 — Final release readiness (no deployment yet)

```text
Evaluate the campaign release against the approved scope and final reviewed SHA. Run the repo's full existing quality gates, PostgreSQL/migration/delivery tests, relevant real-model evals and actual website checks where available. Record failures, skipped tests and unavailable credentials honestly.

For each marketed capability distinguish code checked, locally tested, integration tested, deployed and real-phone verified. List the exact controlled live tests that still need Assaf's approval. Prepare clean-SHA rollout, compatible migration/rollback and a redacted campaign walkthrough. Do not mutate production, send external messages, publish social posts or launch the campaign.

Stop with a go/no-go recommendation, blockers and exact next approval needed. Do not call the release 100% complete because the test suite is green.
```

## Prompt 5 — Approved deployment and phone acceptance

Use only after Assaf approves a particular deployment and names/approves the controlled external test effects.

```text
Execute only the approved rollout of [EXACT CI-GREEN SHA] with [APPROVED MIGRATION/CONFIG SCOPE] using the existing documented deployment procedure. Preserve claims, queues, active sessions, credentials and rollback compatibility. Do not add unrelated changes.

Verify deployed SHA, health, worker readiness and separately approved end-to-end scenarios. Each actual send/Calendar/social write must remain inside the authorized test scope. Capture provider and real-device results without secrets or public PII. Distinguish accepted by provider, presented in Telegram and observed by the human.

If a safety/delivery regression occurs, stop and follow the approved rollback procedure; never reset claims or replay uncertain writes. Return exact results and remaining limitations. Campaign launch is a separate owner decision.
```

## Definition of done per subchunk

`requirement → regression/acceptance case → implementation → focused checks → fresh review → reviewed fixes → final evidence → status/commit checkpoint`

The aim is verified completion with controlled cost—not maximum parallelism, endless planning or a single giant “build everything” session.
