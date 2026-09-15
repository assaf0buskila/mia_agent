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

## Ready briefs (handoff 2026-09-15)

Copy one brief into an Agent call (`general-purpose`, `model: sonnet`, background). Each builder
works in its own worktree off the latest `origin/master` and never pushes. After it returns: the
main session pushes and opens the PR, then runs the reviewer template below with `model: opus`.

### Common builder rules (prepend to every brief)

```text
SETUP: git -C "<repo>" fetch origin; git -C "<repo>" worktree add "<repo>/.claude/worktrees/mia-<chunk>" -b claude/mia-<chunk>-<slug> origin/master. Work only there (absolute paths). uv sync --frozen --group dev. Read AGENTS.md, CLAUDE.md and only the named plan section. Never bare git stash.
Run EVERY command in the foreground with output redirected to a file — no background loops.
HARD RULES: never read .env; no network/provider calls (fake ports); no push/merge/deploy; don't weaken or delete tests; no runtime model change; no unrelated cleanup; don't edit TASKS.md/HANDOFF.md or files named as owned by another open chunk. Two failed attempts → stop with a diagnosis.
VERIFY: focused tests; MIA_ENV=test uv run pytest --basetemp=.cache/<chunk>-full -p no:cacheprovider > .cache/<chunk>-full.txt 2>&1 (read the summary line); uv run ruff check app tests; node tests/unit/widget_behavior.test.js.
COMMIT (no push) via git commit -F <message file outside the repo>, repo prose style, ending "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>".
RETURN (≤400 words): worktree, SHA, changed files and why, tests added, exact summary lines, remaining risk, clean tree.
```

### C2b — exact approval cards (plan §4 "Three presentation types", §9)

```text
Presentation + callback flag only: do not change envelope format, proposal ids, hashing, expiry, actor checks or execution. Preserve legacy outstanding approvals. Keep C1b's failed-turn ai_run persistence in app/surfaces/owner.py and C2a's split_message.
DEFECTS: (1) cards are model prose — owner tools return e.g. "Prepared an exact Gmail draft proposal." and the turn attaches buttons to that prose (app/surfaces/owner.py → owner_telegram_reply_markup in app/api/inbound_common.py). (2) app/api/telegram.py _handle_callback swallows edit_message_text failures and returns "sent": True.
DESIGN: new app/domain/owner/proposal_cards.py render_owner_proposal_card(envelope) → escaped Telegram HTML from read_owner_action's stored envelope; Hebrew; "לאישור: …" title; explicit "עדיין לא בוצע". Per kind: gmail.create_draft (to, subject, full body, "תיווצר טיוטה — לא יישלח מייל"; no invented CC/BCC); composio.write (GMAIL_SEND_DRAFT → target.resource fields + "המייל יישלח"; LINKEDIN → full post text/visibility; others bounded args; never connection ids/account hashes); calendar.create (title, local date + time, duration, location, "לא נשלחות הזמנות"); calendar.reschedule (old → new local); crm.upsert (new vs existing, before → after only for changed non-empty fields); crm.activity; crm.resolve_conflict; sheets.update/append (range + bounded rows); unknown kind → generic safe card, never raw JSON.
owner.py: prose WITHOUT keyboard, then one card message per turn approval id with its own approval_keyboard; long card → split_message, keyboard only on the last chunk; card send failure logged (reason code), proposal stays pending. Pending-approvals view: a card per pending proposal, newest first, max 5 + "ועוד N". _handle_callback: sent reflects edit success; on edit failure one fallback sendMessage with the result text; never re-run the action.
TESTS: snapshot per kind (Hebrew, escaping, no secrets); two proposals → prose + two cards with their own tokens; long body split with keyboard last; pending view 7 → 5 + "ועוד 2"; edit fails → fallback, sent true/false correctly, action executed exactly once; legacy rows render and resolve.
```

### C6a — social capability truth (plan §5.4) — start after C4 merges

```text
No new write capability; Instagram deny (composio_effects.py) and LinkedIn approval/execution (linkedin_writes.py) unchanged; no scheduling/DMs/ads; no new LLM call.
STATE: linkedin_snapshot = own profile only; instagram_insights = per-post metrics, missing → "unavailable" (_metric_value); composio_propose_linkedin_action = exact approval, live posting unverified; content_ideas = categories, not drafts. No guidance separating data/inference/recommendation.
IMPLEMENT: (1) read-only social_capabilities tool computed from settings/readiness (no provider calls): LinkedIn profile read, post/comment via approval ("not yet verified live"), no analytics; Instagram insights read, publishing not available (policy), no DMs/ads; drafts are not scheduled or published. (2) One-clause "what this is NOT" in the descriptions of linkedin_snapshot, instagram_insights, content_ideas, composio_propose_linkedin_action. (3) A social-writing rule constant injected in owner_agent.py build_messages only for linkedin/instagram/content turns: label observed data vs inference vs recommendation; no reach/performance/follower/best-time claims without tool data; a draft is not published; at most one question. Do not touch _looks_empty/_looks_silent/markers/usage. (4) Routing (two_state.py; keep C4's additions and all existing tests): whole-word matching for Latin needles so "a big meeting tomorrow", "update the config file", "excellent work" don't route to instagram/sheets; "LinkedIn post about crm" → linkedin; keep C2a sentences (install/instant/instance/instability → base, "I linked in the doc, make a פוסט" → instagram).
TESTS: capabilities for configured/unconfigured; description clauses; rule injected only on social turns; every routing sentence above.
```

### C7b — cleanup, reviewed follow-ups, docs (plan §11) — last

```text
Never delete app/domain/meetings/*, approval/reporting modules in bulk, or migrations. A "dead" symbol is a hypothesis: grep imports, dotted strings (monkeypatch.setattr("a.b.c")), capability port="…", pyproject entrypoints, workers/schedules, deploy/*.json, tests — and persisted approvals/jobs that could still route to it.
PART 1 hypotheses → table symbol / evidence / delete-or-keep: app/domain/owner/composio_writes.py execute_approved_composio_write (propose has no callers — check outstanding composio_approval rows and callback dispatch); app/surfaces/crm.py log_contact / ActivityRecord (test-only) and build_contacts_crm; app/domain/handoff/hot.py apply_hot_handoff; LeadStore.open_channel_lead/_save_lead_created (only if C3b no longer needs them); scripts/calibrate_knowledge_floor.py (documents a removed setting).
PART 1b reviewed follow-ups with tests: calendar.py window_free_excluding_self — getattr(agenda, "list_events_strict", None), fail closed when missing; owner_agent.py exact "no data" markers — import C4's gmail_brief and uncertain-writes empty-state constants.
PART 2 docs (code is truth): TASKS.md checklist with PR numbers; HANDOFF.md section 0 current; MIA_V2.md/README.md only where behaviour changed; AGENTS.md only if a statement is false. No new doc files. Two commits: code, docs.
```

### Reviewer template (opus, read-only)

```text
Independent READ-ONLY review (you are not the author). Worktree <path>, branch <branch>, review `git diff origin/master...<sha>` (three dots). Do not edit, stash, switch or push; no network; never read .env.
Goal and claims: <chunk goal + the author's claimed fixes, as claims to verify>.
Check: correctness against the stated defects; invariants (server-only contact validation, visitor isolation, exact immutable approvals, no duplicate/uncertain side effects, no visitor text in logs); callers and persisted-data compatibility; adversarial inputs with throwaway `uv run python -c` probes (nothing left in the repo); tests exercise the real code and fail on revert (mutate a scratch copy outside the repo); nothing weakened.
Run the focused test files to .cache/<chunk>-review.txt and read it.
Return: findings (file:line, severity P0–P3, failing scenario, smallest fix), verified checks, verdict PASS / CHANGES_REQUIRED. ≤500 words.
```

### Resume a stopped agent

```text
Resume <chunk> after the interruption: you were <last step>. Continue in <worktree> from its current state (check git status --short / git diff --stat first). <remaining items>. Foreground commands only. Same verify/commit/return rules.
```
