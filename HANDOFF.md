# Mia handoff — 2026-09-16

Section 0 is the campaign-finish state for the next session. Sections 1 onward are the
2026-09-11 handoff and still hold (deploy procedure and gotchas especially).

## 0. Campaign finish — where it stands and how to continue

Plan: `docs/MIA_CAMPAIGN_FINISH_PLAN.md`. Chunk briefs: `docs/MIA_CLAUDE_CODE_PROMPTS.md`
(the "Ready briefs" section at the end is copy-paste ready). Session rules: `CLAUDE.md`.

**Correction (2026-09-16): production is deployed and current, not unchanged.** This line
previously said `/health` last reported `4b80f31` (2026-09-14) and nothing was deployed. That is
now stale: `110ada6` is live on ECS task definition `mia:65`, verified via `/health`, with a real
website lead delivered end to end to Assaf's Telegram. Every item below is `LOCAL_TESTED` +
CI-green + independently reviewed at most — deployment status for anything past `110ada6` (i.e.
this session's C7b work) is unaffected by this correction.

### C8 — RDS credential rotation took production down for ~9 hours (2026-09-16)

**Root cause.** RDS has `ManageMasterUserPassword` enabled and rotates the master password
into its own AWS-managed secret (`rds!db-d7c051e7-2f6a-4711-826d-2bf7d243a2f8-...`, key
`password`) every 7 days; last rotation 2026-09-12. The app's own secret `mia/prod` held a
second, independent copy of the password embedded in `MIA_DATABASE_URL`. When RDS rotated,
the already-running task kept working on its already-open connection, so nothing alarmed —
the two copies had silently diverged with no signal. The next task restart (a routine
deploy) tried to authenticate fresh, failed against the rotated password, and production
was down until the `mia/prod` copy was hand-updated: roughly nine hours.

**Fix, take 1 (in-app override) — designed, reviewed, then dropped.** A first design added
an optional `MIA_DATABASE_PASSWORD` setting sourced from the RDS-managed secret as its own
container secret, overriding just the password in `database_url` at one choke point
(`Settings.effective_database_url()` / `app.db.session.get_engine()`). Review confirmed the
substitution logic itself was correct under adversarial testing (23 passwords round-tripped
exactly through SQLAlchemy's own parser, including `%40`, `%2F`, 512 chars, Hebrew, emoji;
last-`@` anchoring correct; choke point confirmed sole). It was still dropped: the ECS
execution role `miaTaskExecutionRole`'s inline policy `ReadMiaProdBoxOnly` is scoped to
`secret:mia/prod*` only, so it cannot read `secret:rds!db-...` — deploying the RDS-managed
secret as a container secret as designed would have prevented every task from starting.
Assaf chose not to widen that policy.

**Fix, take 2 (auto-sync) — the chosen approach, chunk C9, `CODE_CHECKED` + `LOCAL_TESTED`,
not deployed.** Instead of the running container reading the rotated password directly, an
EventBridge rule on the RDS-managed secret's rotation event
(`deploy/eventbridge-db-password-rotation-rule.example.json` +
`-targets.example.json`, `deploy/iam-lambda-invoke-permission.example.json`) triggers a
Lambda (`scripts/lambda_sync_db_password.py`, `sync_database_password`) that (a) reads the
new password from the RDS-managed secret, (b) rewrites only the password inside `mia/prod`'s
`MIA_DATABASE_URL` — the rest of the URL string preserved byte-for-byte, every other key
preserved JSON-equivalent (`json.dumps`'s default `ensure_ascii=True` escapes non-ASCII
values as `\uXXXX`; semantically identical, not literally byte-identical) — and
(c) forces a new ECS deployment (`ecs:UpdateService` with `forceNewDeployment`) so the
already-running task actually picks up the change instead of holding the old password until
something else restarts it (which is exactly today's failure mode; the code comment says so
explicitly). The percent-encoding + last-`@`-anchoring substitution is carried over verbatim
from take 1's already-adversarially-reviewed `_with_overridden_dsn_password`, not rewritten.
Idempotent (re-running against an already-current secret is a no-op on the value, still
forces a deployment). Guards a UTF-8 BOM on read (one already made this exact secret invalid
JSON) and never writes one.

The Lambda's IAM role (`deploy/iam-lambda-db-password-sync.example.json` +
`-trust.example.json`) is scoped to: `secretsmanager:GetSecretValue` on the RDS-managed
secret; `secretsmanager:GetSecretValue` **and** `PutSecretValue` on `mia/prod`;
`ecs:UpdateService` (only — see below) on the `mia` service; the three
`logs:CreateLogGroup`/`CreateLogStream`/`PutLogEvents` actions scoped to this Lambda's own
log group; `sns:Publish` on its DLQ/alerts topic. The extra `GetSecretValue` on `mia/prod`
(beyond the originally-specified `PutSecretValue`-only) is necessary — the handler cannot
preserve every other key without reading the current secret first — and is called out here
rather than silently added. No resource is `*`. `mia/prod`'s embedded password stays
load-bearing under this design (it is what the container actually reads); it does not
become vestigial the way take 1 would have made it.

**Review round 2 findings, fixed:**
- The EventBridge rule's `detail-type` was wrong (`"AWS API Call via CloudTrail"`, an
  API-call pattern) for a **service-emitted** event — Secrets Manager's rotation-succeeded
  event surfaces as `"AWS Service Event via CloudTrail"`. As written, the rule could never
  match, so the Lambda would never run — a safety net that silently doesn't exist, worse
  than none, because nobody would do the manual check believing it was automated. Fixed the
  detail-type and dropped the `requestParameters.secretId` narrowing (the id likely lives
  under `additionalEventData` for a service event, not `requestParameters`; the handler
  already hardcodes the RDS secret ARN, so a broader match costs nothing).
  **Still true, and must stay true until proven otherwise: this event pattern is UNVERIFIED.
  It has never been fired by a real rotation. Do not treat it as working, do not deploy the
  rule on the assumption it fires, until one real rotation cycle in staging proves it does.**
- `ecs.describe_services` was called after every successful write+redeploy purely to
  populate a status field nothing reads (EventBridge discards the return value). If that
  call throttled or was denied, it raised *after* the secret write and the redeploy had
  already succeeded — Lambda's automatic retries would then repeat both, forcing up to
  three rolling production restarts for a run that had already worked. Removed the call,
  the `describe_services` Protocol method, the `service_status` return key, and
  `ecs:DescribeServices` from the IAM policy.
- The Lambda ran with no log-group grant, no documented base execution permissions, and no
  DLQ — a failed sync would have been invisible, the exact silent-divergence failure mode
  this chunk exists to close. Added the CloudWatch Logs statement, `DeadLetterConfig`
  pointing at an SNS topic (`deploy/lambda-db-password-sync.example.json`), and a
  CloudWatch `Errors` alarm on that same topic (`deploy/cloudwatch-db-password-sync-errors.example.json`).
- Three load-bearing properties had no test pinning them (all passed the full suite
  unmutated): `quote(safe="")` vs. an accidental `safe="%"` (a password literally
  containing `%40` decodes to the wrong character under the mutant); the write-then-redeploy
  *order* (if the secret write raises, the redeploy must never fire); and `rpartition("@")`
  vs. `partition("@")` when the *current* password already contains a literal `@` (plausible
  here — the current password came from a hand-written secret during the incident). All
  three now have a dedicated test in `tests/unit/test_lambda_sync_db_password.py`.
- A runtime `assert not new_secret_string.startswith(BOM)` could never fire — `json.dumps`
  cannot produce a leading BOM — so it proved nothing and was removed; the "never write a
  BOM" property is a structural fact about `json.dumps`, checked instead by
  `test_sync_never_writes_a_bom`.

Not deployed: creating the Lambda, its role, or the EventBridge rule is a separate approval.
Deploying before the event pattern is verified against one real rotation would recreate the
exact silent-divergence failure this chunk exists to close, just one layer up the stack.

**Pre-existing gap, flagged by review then fixed on Assaf's call (separate commit):**
`app/core/logging.py`'s `RedactingFilter` used to guard `record.msg` with
`isinstance(..., str)` — a non-str `msg` skipped scrubbing entirely and reached the log with
any secret in its `__str__` intact once `record.getMessage()` stringified it later, the same
class of bug as the httpx/Telegram leak above. It also never touched `exc_info`/`exc_text`,
so an exception whose own message embedded a token or password (exactly how a provider
client error would carry one) reached the log unscrubbed. Both closed: `record.msg` now
always passes through `redact()` regardless of type; `exc_info` is rendered via the standard
traceback formatter and scrubbed into `record.exc_text` at the record level (the exception
object itself is never mutated, since other code up the stack may still hold and inspect it)
before any handler formats it, so `Formatter.format()`'s own cache-if-empty check picks up
the already-redacted text instead of recomputing an unredacted one. Traceback structure and
frames are untouched — only matched secret substrings are substituted — so real stack traces
stay fully readable. `record.stack_info` (a separate, unrelated `stack_info=True` mechanism)
is deliberately left untouched.

**Separate, incidental finding fixed and kept regardless of which take: Telegram bot token
leaking into CloudWatch.** httpx's own request logger (`logging.getLogger("httpx")`) logs
`request.url` as an `httpx.URL` object, not a pre-formatted string. `app/core/redact.py`'s
`redact()` only pattern-matched `str`/`dict`/`list` values, so this one non-string `%`-style
log argument fell through every branch untouched, and the Telegram bot token embedded in
the URL path (`api.telegram.org/bot<TOKEN>/sendMessage`) reached `/ecs/mia` in plaintext.
Assaf already revoked the exposed token; the replacement would have leaked identically.
Fixed by extending `redact()` to stringify and pattern-check any non-str/dict/list value,
substituting the scrubbed string only when a token is actually present. Independent of the
credential-rotation question either way; merged on its own.

### Merged to master (each: failing test → fix → fresh opus review → fixes → green CI)

| PR | Chunk | What it fixed | Known limits recorded |
|---|---|---|---|
| #61 | C0 | Plan, prompts, `CLAUDE.md`, audit checklist | — |
| #62 | C1a | `crm_upsert` returned None on valid input → owner turn crashed; now an exact `crm.upsert` proposal; `execute_tool` rejects non-ToolResult; source/summary defaults only for new contacts | no test for a contact created by someone else between proposal and approval (code rejects it) |
| #63 | C2a | Explicit LinkedIn beats generic "פוסט"; owner replies render a safe Markdown subset; `split_message` never splits a tag, entity, `<b>`/`<code>`/`<pre>` span | pre-existing needle collisions: "ig " in "big"/"config", "excel" in "excellent", "LinkedIn post about crm" → sheets |
| #64 | C5 | Reschedule no longer conflicts with itself (events-list check when the destination overlaps the event's own span, fail-closed); a failed agenda read is not a free day; scope text | legacy `calendar_writes.py:426` still plain free/busy (refuses self-overlap, never double-books) |
| #65 | C1b | Greeting-prefixed useful replies kept; "no data" detection by exact/prefix formatter constants (not length, not substring); failed turns persist usage marked `owner_reply_failed` | a turn failing before any model call records 0 tokens (NOT NULL columns; no migration) |
| #66 | C3a | Website `business_context` never latches a greeting; deterministic de-duplicated lead brief; same-turn `submit_lead` next step/name refresh the still-pending brief and re-enqueue the contacts job at the new revision; greeting regex atomic (was ReDoS) | Activity Sheet job keeps the capture-time summary (lags one turn); PostgreSQL test doesn't exercise the refresh path |
| #67 | C5h | Reschedule safety check fails closed on all-day items, `nextPageToken`, unparseable items; agenda port bound to the approved connection | any other all-day event refuses a move (conservative) |
| #70 | C3b | Owner daily brief / website conversations / hot leads include v2 captures ("leads today" = distinct contacts; list one row per contact; hot judged per capture's conversation); Sheet `נוצר`/`עודכן` filled from row timestamps, system-owned (owner edits overwritten, never a conflict or a wedged projection); `crm_search`/`crm_conflicts` refuse instead of creating tabs when the workspace is missing | header drift can still be repaired by a read; hot replies show raw `crm_…` ids; `crm_v2.py` timezone default hardcoded `Asia/Jerusalem`; broad `except` in `tools/owner/crm.py` workspace check |
| #69 | C4 | `gmail_brief`: bounded (25) owner-local daily email data, thread-deduped, true `partial`, marketing only from Gmail category labels, a failed Composio read is `ok=False` (also fixes `gmail_search`); `owner_uncertain_writes`: read-only list of `pending_review` writes with plain-words targets | uncertain-writes query unindexed (fine at limit 10) |
| #71 | C6a | Social capability truth: real per-tool capability clauses, a `social_capabilities` tool, a social-writing rule that separates observed data / inference / recommendation; routing needle-collision fixes (content-word plurals, Hebrew clitics, reels); the audit method itself replaced (a differential sweep against master instead of audit-by-inspection) | — |
| #72 | C2b | Approval cards render from the stored envelope, one Telegram message per proposal with its own keyboard; a real edit-failure fallback send; no button ever delivered on a partially-sent card; callback `sent` reflects actual delivery, not intent; the pending-approvals digest is always sent as its own message, never combined with a card | see C7b below — a card-succeeds-while-digest-fails variant of the same "no button on a partial send" property left the webhook mark and the outbound canonical event both keyed on the digest alone |

### C7b (this session, committed locally, not pushed / no PR yet)

**Correction (2026-09-16, round 2 review):** the first version of this section claimed
`human_takeover` (boolean) and `takeover_state` (string) are independent columns with different
writers, and used that to justify deleting `list_hot_lead_ids`/`set_takeover_state`/the `hot_ids`
field entirely. **That claim was false and the deletion was wrong.** `store.set_takeover_state`
writes *both* columns on the same row (`row.takeover_state = state; row.human_takeover =
human_takeover_flag(state)` — true for `HUMAN_TAKEOVER_REQUIRED`), so they are not independent.
Production corroborated it directly: Assaf's owner console showed `ליד חם: lead_887149792f1c` —
a `lead_` + 12-hex id, which only `_new_id("lead")` for `LeadRow` ever mints (v2 mints `crm_` +
32-hex and never passes a `contact_id` into `capture_site_lead`), so that id could only have come
from the v1 `hot_ids` path this session had just deleted. The write path (`apply_hot_handoff`)
being unreachable does not make a column it already wrote historically unreadable — reads outlive
writers. Fixed below; do not repeat the "unreachable writer implies dead reader" mistake on
persisted state.

Branch `claude/mia-c7b-cleanup-docs` off `origin/master` = `110ada6`. Two code commits (kept
separate because the second is materially riskier than everything else in the chunk), a docs
commit, then a round-2 review fix commit (`2311e5b`) plus this correction to the docs:

- `25a5c59` — proven-dead deletions (`propose_composio_write`; `log_contact` /
  `build_contacts_crm` / `resolved_spreadsheet_id` / `now_israel`;
  `scripts/calibrate_knowledge_floor.py`'s phantom-setting docstring) and the reviewed
  follow-ups (`window_free_excluding_self` fail-closed on a missing `list_events_strict`;
  `GMAIL_BRIEF_EMPTY_WINDOW`/`OWNER_UNCERTAIN_WRITES_EMPTY` now in the exact "no data" marker
  set; `settings.calendar_timezone` threaded into `CrmService` at every easily-reachable call
  site instead of its hardcoded `Asia/Jerusalem` default; the `tools/owner/crm.py` workspace
  check narrowed from `except Exception` to `except AdapterHttpError`; `run_owner_loop`'s new
  `delivered_any` flag; a pinned "zero pending rows → no keyboard" test; a clarified test name
  in `test_owner_v2_actions.py`).
- A second commit retires **only** the auto-freeze *behaviour* Assaf rejected — `apply_hot_handoff`
  and its now-unreachable siblings `OwnerNotifyAttempt`, `format_hot_brief`, `KIND_HOT_LEAD`
  (each had zero callers, production or test, the moment `apply_hot_handoff` went; `notify_owners`/
  `_deliver_owners` are untouched, still test-covered, still exported — a general Telegram
  fan-out helper, not part of the takeover mechanism). Everything that *reads* takeover state is
  restored/kept exactly as it was: `store.set_takeover_state`, `store.list_hot_lead_ids`, the
  `hot_ids` field on `leads.get_recent`, `TAKEOVER_BLOCKS_SEND`/`takeover_blocks_send`/
  `human_takeover_flag`. `format_hot_leads_ack` unions v1 (`hot_ids`, via a restored
  `execute_capability("leads.get_recent", principal=principal, …)` authorization call — a P2 from
  round-2 review had dropped it) and v2 (`list_undelivered_captured_website_leads`) again, and
  now labels *both* id shapes instead of a bare id: a v2 `crm_…` id shows its captured contact
  name, a v1 `lead_…` id shows `SalesState.headline` (the closest thing v1 has to a name — no v1
  table stores one) — either falls back to the bare id when no label exists.

Verify at `2311e5b`: `MIA_ENV=test uv run pytest` → **2270 passed, 7 skipped** (was 2266 before
this fix; +4 for the restored/new takeover-union tests — see `2311e5b`'s message for the exact
list; master itself was 2267). `uv run ruff check app tests` → clean.
`node tests/unit/widget_behavior.test.js` → passed (untouched this chunk).

Left deliberately alone, with reasons (do not treat these as missed):
- `execute_approved_composio_write` (app/domain/owner/composio_writes.py) — still on hold per
  Assaf. It is reachable from a live persisted-row path via `app/api/telegram.py`'s callback
  handler and `app/domain/owner/callbacks.py`, fails closed today, and is pinned by
  `tests/unit/test_owner_v2_actions.py:test_legacy_r5_approval_is_refused_by_callback_and_executor`
  and the composio/meta-v2-policy suites. Untouched.
- `LeadStore.open_channel_lead` / `_save_lead_created` (app/db/store.py) — kept, deliberately.
  Zero production callers, but 28 test files call `open_channel_lead` directly to seed a lead;
  deleting it would churn the suite for no runtime benefit. Same precedent as
  `app/domain/meetings`.
- `store.set_human_takeover` / `store.get_takeover_state` (app/db/store.py) — test-only (one
  caller each, in `tests/unit/test_telegram_owner_controls.py` / `test_hot_handoff.py`), not in
  this chunk's authorized deletion list either way. `set_human_takeover` is a separate,
  self-contained setter (does not call `set_takeover_state`; the two were never entangled) for
  the same live guard `is_human_takeover` reads — stays regardless of anything above.
- `app/workers/crm_delivery.py`'s 7 `CrmService(...)` calls still use the hardcoded
  `Asia/Jerusalem` default. Its constructor has no settings/timezone parameter at all; adding
  one and wiring it through the worker's instantiation site is a larger, separate change than
  the other, trivially-reachable call sites this chunk fixed.
- Sheet header drift still repaired as a side effect of a CRM read (needs an
  `app/services/owner_actions.py` change) — record only, no code touched.
- No PostgreSQL coverage for C3a's same-turn refresh — record only, no code touched.

### In flight at handoff

Nothing beyond C7b above. Before acting, run `gh pr list` and `git worktree list`: the finished
chunk worktrees (`.claude/worktrees/mia-c1b`, `mia-c2a`, `mia-c3a`, `mia-c3b`, `mia-c4`, `mia-c5`,
`mia-c5h`, `mia-c2b`, `mia-c6a`) are merged and can be removed with `git worktree remove` once
confirmed clean. A stale locked directory may exist at `.claude/worktrees/mia-c6a` from an
earlier interrupted session — ignore it. Master after the last campaign merge: `110ada6` (#72).

### Remaining queue

1. **C7b** — push, PR, fresh opus review on the three-dot diff to this session's final SHA, fix,
   merge on PASS + green CI. This chunk is unusually high-risk for its size (a production data
   point in play, and round 1 of this exact review already caught a false premise here) — the
   review should specifically re-verify the "Decisions" entry below against the code rather than
   take it on faith.
2. **Prompt 4 release readiness** (opus, read-only): full gates, capability evidence matrix,
   exact live tests needing Assaf's approval, rollout + rollback plan. **Stop for go/no-go.**
3. **Prompt 5 deploy** only after Assaf approves a specific CI-green SHA; then phone acceptance.

### Assaf's decisions (2026-09-14)

- Merge each chunk when review PASS + CI green; deploy is a separate go.
- Live Sheet audited read-only, structure only (done): Contacts 16 cols (O = Mia ID, P = marker
  `mia-contacts-v2`); 2 legacy rows lack a Mia ID and their phones lost the leading 0 (stored as
  numbers) — owner review, never a guessed repair; 5 test/fixture rows — classify, don't delete.
- `app/domain/meetings` stays as is (Calendar imports `meetings.slots`; tests patch it by dotted string).
- LinkedIn publishing is in scope: at release, one controlled live post approved by Assaf.
- Instagram writes stay denied (policy). Calendar = own events, no guests/recurrence.

### Assaf's decisions (2026-09-16, C7b)

- **`LeadStore.open_channel_lead`/`_save_lead_created`: keep, as test-only scaffolding.** Zero
  production callers; ~28 test files depend on it to seed a lead. Deleting it would churn the
  suite for no runtime benefit — same call as `app/domain/meetings`. No code change.
- **The v1 hot-lead takeover subsystem: delete the auto-freeze *behaviour* only, keep the
  *read*.** Assaf does not want Mia to auto-freeze a hot conversation and hand it over; he wants
  hot leads listed in his brief and decides himself. That is a statement about behaviour
  (`apply_hot_handoff`, which froze the conversation and could only ever be triggered by that
  now-gone auto-freeze code path), not about the report. `store.list_hot_lead_ids`/the `hot_ids`
  field/`store.set_takeover_state` all stay: **production has a live row right now**
  (`takeover_state == HUMAN_TAKEOVER_REQUIRED`, surfaced to Assaf as `ליד חם: lead_887149792f1c`
  in the owner console), and deleting the read path would have made it invisible while the
  write-gate columns kept silently protecting it. See the corrected evidence at the top of the
  C7b entry above — an earlier version of this note wrongly treated `human_takeover` and
  `takeover_state` as independent columns with different writers; `set_takeover_state` sets both
  on the same row, so they are not independent, and that error is exactly what nearly caused the
  wrong deletion. `store.is_human_takeover` (gates `app/core/outbound.py` sends and
  `app/domain/followups.py` scheduling) and `store.count_human_takeover` (`/health`) were never
  touched either way.

### Follow-ups already identified (not yet done)

- Sheet header drift is still repaired as a side effect of a CRM read (needs an
  `app/services/owner_actions.py` change).
- PostgreSQL coverage for C3a's same-turn refresh is still missing.
- `app/workers/crm_delivery.py`'s `CrmService(...)` calls still default to `Asia/Jerusalem`
  (see the C7b entry above — needs a constructor/settings-threading change, not a one-line fix).
- **Pre-existing test-order flake, now reproduced exactly (2026-09-16, record only — not fixed,
  not a release blocker, CI is unaffected):**
  `tests/unit/test_gmail_send_policy.py::test_owner_telegram_asked_then_approved_send_calls_send_draft`
  fails with `assert decision == DECISION_APPROVED` (`apply_gmail_send_decision` does not return
  the approved decision) when run directly after `tests/unit/test_telegram_owner_outbound.py`:
  ```
  MIA_ENV=test uv run pytest tests/unit/test_telegram_owner_outbound.py \
    "tests/unit/test_gmail_send_policy.py::test_owner_telegram_asked_then_approved_send_calls_send_draft"
  ```
  Mechanism: both files call `init_db()`/`get_session_factory()` against the same shared
  database; rows `test_telegram_owner_outbound.py` leaves behind make the send decision resolve
  against stale approval state instead of the draft the second test just created — a
  test-isolation gap, not a product bug. `test_owner_v2_actions.py` and
  `test_composio_owner_catalog.py` do **not** trigger it in the same pairing.
  `test_telegram_owner_outbound.py` is the file C2b (#72) expanded by ~636 lines (many
  pending-approval/card/legacy rows), so C2b likely increased the leftover state that makes this
  specific pairing trigger, though the flake is pre-existing in kind. **CI is unaffected**: the
  full suite passes in collection order — 2266 passed / 7 skipped at this session's head
  (2267 at master `110ada6`, before this chunk's net -1 test count change) — because other files
  run between these two in the normal collection order and reset the relevant state. This is
  latent test-isolation debt, not a deploy gate; it would only surface if collection order
  changed.

### Start the next session with

```text
Read CLAUDE.md, HANDOFF.md section 0 and TASKS.md "Campaign finish". Do not re-audit.
Verify in five lines: git fetch; origin/master SHA; `gh pr list` (nothing campaign-related should
be open; if #69 is not merged, finish it first); `git worktree list`.
Then continue the queue in HANDOFF section 0 using the Ready briefs in
docs/MIA_CLAUDE_CODE_PROMPTS.md: launch the C2b and C6a builders (sonnet, own worktrees off the
latest origin/master), then for each: push + PR → fresh opus review on the three-dot diff → fixes
→ merge on review PASS + green CI. Then C7b last. Max 2 agents at once.
Stop before Prompt 4's go/no-go and before any deploy.
```

### Commands the loop uses

```bash
git -C "<repo>" fetch origin
git -C "<repo>" worktree add "<repo>/.claude/worktrees/mia-<chunk>" -b claude/mia-<chunk>-<slug> origin/master
uv sync --frozen --group dev
MIA_ENV=test uv run pytest --basetemp=.cache/<chunk>-full -p no:cacheprovider > .cache/<chunk>-full.txt 2>&1
uv run ruff check app tests
node tests/unit/widget_behavior.test.js
git commit -F <message file outside the repo>
gh pr create --base master --head <branch> --title "<chunk>: …" --body "…"
gh pr checks <n> --watch --interval 30
gh pr merge <n> --merge --subject "Merge pull request #<n> from assaf0buskila/<branch>"
```

In Claude Code: `/model` to confirm the main session is on `opus`; subagents are launched with the
Agent tool (`general-purpose`, `model: sonnet` for builders, `model: opus` for reviewers,
`run_in_background: true`) and resumed with `SendMessage` to their id. Use `/compact` at a chunk
boundary rather than carrying review transcripts forward.

---

# Mia handoff — 2026-09-11

Supersedes every earlier HANDOFF.md. Verified against AWS, GitHub, the code and live
production traffic today. Where an earlier handoff was wrong, it is called out.

## 1. P0 CRM delivery outage — FIXED AND DEPLOYED

Production ran `mia:58` (commit `e0ae94e`) and failed every CRM delivery cycle from
2026-09-10 23:18 UTC — website leads did not reach Telegram for ~11.5 hours. Root cause:
`app/db/session.py` sets `autoflush=False` but the CRM tests ran with autoflush on
(PR #55, master `a7a1b5b`).

**Deployed:** `mia:59`, image `mia@sha256:62604f49…`, `MIA_BUILD_SHA=a7a1b5b…`.
`/health` confirms `deployment.commit_sha == a7a1b5b`. Scheduler `mia-due-scan` ENABLED and
re-pinned to `mia:59`; `mia-reconcile` DISABLED and deliberately unrevisioned. Rollback: `mia:58`.

The failure loop is definitively stopped (old task: one failure every ~6s; new task: zero
warnings in a 30-minute window). The delivery worker logs **only** failures by design
(`app/workers/crm_runtime.py`) and no outbox-depth counter is exposed, so positive proof that
queued briefs landed can only come from Assaf's Telegram.

## 2. Website lead capture was broken in production — FIXED, NEEDS DEPLOY

Found by driving live Mia through real conversations: **6 contact attempts across 3 sessions
produced 0 leads.** Every API response returned `lead_id: ""`, `next_action: "answer"`,
`delivery_status: "none"`.

A closed loop, not a one-off:
- `_actual_contact` only inspected the **current** message. Mia routinely reads the number back
  and asks the visitor to confirm; that confirmation carries no phone or email, so capture
  returned `{}` immediately and could never complete.
- The consent classifier returns non-affirmative on the turn that *does* contain the number,
  which is what makes Mia ask for confirmation in the first place.
- No fallback exists: the widget's structured contact form renders only on
  `next_action === 'ask_contact'` (`app/web/ask_mia.js`), and v2 only ever emits `answer` or
  `contact_saved` (`app/surfaces/site_v2.py`). Free text is the only capture path.

Worst observed case: Mia instructed the visitor to reply with an exact phrase, they did, and she
asked again. Another deflected a hot lead to the website contact form.

**Fix:** the server remembers contact it extracted but could not yet read as consent
(`state.pending_contact`) and completes the capture on a later turn — but only when Mia quoted
that exact value back on the previous turn (`_contact_readback`) **and** the classifier reads the
reply as affirmative. The value always originates from a server regex over the visitor's own
words, never from the model, so "the server alone validates contact" is unchanged. A bare
confirmation with no readback still captures nothing. Two regression tests cover both directions.

Also fixed in the same area: `submit_lead`'s `name`/`next_step` arguments were parsed and thrown
away, so `state.next_step` was never assigned and every brief ended with the default suggested
step. A name is accepted only if it appears verbatim in the visitor's message. Capture runs
before the model turn, so a recovered next step reaches the **next** brief — do not reorder
capture, four other call sites depend on that order. The system prompt also never told Mia to
invite contact details at all; it now does.

### The actual root cause — found after those two fixes both failed live

Neither fix above changed the live result, and no downgrade log ever appeared, because the
classifier was failing **before** any of the checks that log. Chain, from the code:

1. `build_site_client` is the OpenAI **Responses** API with `reasoning: {effort: "low"}`
   sent on every call (`app/integrations/llm_client.py:_responses_payload`).
2. `_classified_consent` requested `max_completion_tokens=180`, mapped to
   `max_output_tokens: 180` — which on Responses bounds **reasoning and visible output
   together**.
3. The model spent the whole 180 reasoning and emitted nothing. The adapter deliberately
   turns `status=incomplete / max_output_tokens` into `LlmResponse(text="", tool_calls=(),
   finish_reason="length")` — no exception, so `LlmModelChain` never fell back to Gemini.
4. `len(response.tool_calls) != 1` → `"ambiguous"`, silently, 100% of the time.

The narrative validator uses 1200 and works; replies use 500 and mostly work — except the
post-tool completion on the contact turn, which is why *that* turn blanked to the greeting.
Same cause, both bugs. Fix: `_CONSENT_MAX_OUTPUT_TOKENS = 600`, `_REPLY_MAX_OUTPUT_TOKENS =
900`, an explicit `reason=truncated` log, and a regression test that asserts the budget
actually requested at the call boundary (≥ 512), since a constant alone proves nothing.

Also on this branch: dictated numbers normalised to digits (`_spoken_digits_to_numerals`,
English and Hebrew; the live voice transcript was `"zero five two, one one one…"`), and
the owner surface returns `OWNER_UNAVAILABLE` instead of the greeting when the brain fails.

## 3. Cleanup — DONE

Branch `claude/mia-v2-cleanup-final`, merged here. 1957 passed / 7 skipped (the 7 are the
expected PostgreSQL skips when `MIA_TEST_POSTGRES_URL` is unset), ruff clean.
Removed: the dead OwnerGraph layer and its tests, langgraph, `llm_compose.py`, two dead config
flags, two dead notification helpers, the unused `crm`/`gmail_port`/`talk` params on
`run_owner_loop` (the Telegram worker was building CRM/Gmail ports every turn for nothing),
14 dead `LeadStore` methods, `published_facts`, and `app/domain/owner/followups.py`.

Also fixed a live bug: `app/core/capabilities.py` advertised the already-deleted
`app.agents.owner.graph` as `status=ALIVE` on `/health`.

### The old handoff's "verified dead" list was WRONG — these stayed
- `app/domain/policies/decision.py` — `app/domain/ai_runs.py` imports `DETERMINISTIC_NBA_CONFIDENCE`.
- `app/domain/policies/failure_policy.py` — bound to `CapabilityId.FDE_FAILURE_POLICY` (ALIVE).
- 6 `LeadStore` methods in the "WhatsApp-identity" / "v1 website-session" groups are live.
- `VISITOR_TOOLS` is a live permission gate; only the `"published_facts"` string was removable.
- `app/domain/meetings/{booking,changes,copy}.py` — NOT deleted. `tests/conftest.py` patches them
  by dotted string in `freeze_mia_clock`, so removing them breaks 100+ unrelated tests.
  **Open product question for Assaf: is meeting booking retired, or waiting to be re-wired?**

### Known consequences
- `/health`'s `integration_failures` can now only decrease — `upsert_reconciliation_finding` was
  its only writer. Frozen at 11.
- `scripts/calibrate_knowledge_floor.py` still documents `MIA_KNOWLEDGE_MIN_SIMILARITY`, which no
  longer exists. It is now a no-op advisor.
- `build_contacts_crm` in `app/surfaces/crm.py` is an orphaned factory with zero callers. The rest
  of that module is live — delete only that function, not the file.
- The 8 deleted followups tests were the only coverage of owner pronoun/data-anchor resolution.

## 4. Website — Mia as an inline hero box. PR OPEN

`AiHeroChat.tsx` was a fully scripted fake (5 hard-coded messages, typewriter, no input, no
network). PR **assaf-landingPage#28** (`feat/mia-inline-hero`, based on `origin/main`) replaces it
with `MiaHeroChat`, which renders a `[data-mia-inline]` container; the widget mounts inside it,
always open, no launcher, sized to the host. `AiHeroChat` is kept as a fallback when the Mia
origin is unconfigured. `npm run lint` (tsc) passes. Leo is untouched.

**Do not merge until** the widget inline mode and the capture fix are deployed — merging
auto-deploys to Vercel via the Git integration.

- `lib/mia.ts` on `main` already defaults to `https://mia.assafweb.com`, so no env var is needed.
- The widget is ID-based throughout, so only ONE instance per page — inline and floating cannot
  coexist without a class/shadow-DOM refactor.
- The other website checkout is on branch `rebrand/ai-solutions-studio`, dirty, with the Leo files
  untracked, and another agent session is editing it. `3cce2b9` is in `origin/main` but not in
  that branch. Work from a worktree off `origin/main`.
- There is no CI on the website repo; `npm run lint` is `tsc --noEmit` only.

## 5. Still open

- **Owner greeting on brain failure.** `app/surfaces/owner.py` still returns `"פה. מה צריך?"` when
  the brain throws. The comment there claims this was fixed; only a log line was added.
  `OWNER_UNAVAILABLE` already exists — gate on the failure reason, since `deterministic_intent` is
  a legitimate short ack.
- **Docs.** README.md, MIA_V2.md, TASKS.md, AGENTS.md need a short, code-based rewrite. TASKS.md
  still falsely claims "no deployment has occurred". AGENTS.md has the two graphs backwards.
- **Voice path untested.** Browser control cannot inject microphone audio; test by posting an
  audio file to `/v1/website/sessions/{id}/voice`.
- `business_context` is still a write-once latch on the visitor's first message.

## Deploy gotchas — do not rediscover these

- **OCI index trap.** `scripts/deploy_ecs_revision.py` accepts only `oci.image.manifest.v1+json` /
  `docker.distribution.manifest.v2+json`. Docker 29's default build emits an attestation manifest
  + manifest list (an index) and the provenance check rejects it. Build with
  `--provenance=false --sbom=false --platform linux/amd64`.
- **The deploy script needs NO local Docker** — it verifies provenance via the ECR API. It DOES
  require local `HEAD == --sha` and a clean tree, so never deploy from a worktree you are editing.
- **The Docker Windows credential helper is BROKEN** (`The stub received bad data`), so
  `docker login` cannot save credentials. Workaround: an isolated `DOCKER_CONFIG` dir with the ECR
  token written straight into `auths`, deleted right after the push. Still unfixed.
- **Docker Desktop here is fragile** — the WSL `docker-desktop` distro stops and the CLI control
  plane hangs. Reviving it needed the GUI.
- No Docker-free build path is wired up: GitHub Actions can build but cannot push (no AWS creds,
  and an SCP denies IAM OIDC-provider actions); CodeBuild/S3/CodePipeline do not exist.
- Git Bash mangles `/ecs/mia` — prefix AWS log commands with `MSYS_NO_PATHCONV=1`.
- `aws logs filter-log-events` returns only the first scan page, so `length(events)` is NOT a
  reliable count. Sort by timestamp instead.
- AWS credentials come from `aws login` (profile `default`) and expire mid-session.
- Piping pytest through `tail` swallows the summary line; redirect to a file instead.

## Housekeeping

- The MAIN checkout (`assaf_agent/`, master) still has uncommitted `app/services/crm_v2.py` and
  `tests/unit/test_crm_v2.py` changes that PR #55 supersedes. Tell that session or discard.
- Never read `.env`. Secrets live in Secrets Manager.
