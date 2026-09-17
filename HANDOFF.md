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

### CURRENT STATE — read this before anything else (2026-09-16, end of session)

- **The code queue is closed.** Master is `96a0f3c`. No campaign PR is open. The only open PRs are
  #46 (Hebrew RTL docs, 2026-09-09) and #8 (stale cursor draft), neither part of this effort.
- **Production runs `110ada6` on task definition `mia:65`. SEVEN merged PRs are undeployed:**
  #73 (C7b cleanup), #74 (C8 token leak + rotation Lambda code), #75 (C11 Activity Sheet),
  #76 (C9 Hebrew presentation), #77 (handoff refresh), #78 (social pass: capability text in
  Hebrew), #79 (C12 live knowledge). Two of them fix exactly what Assaf judges the release on:
  today his Activity tab still shows `contact_captured` with the whole brief in one cell, and his
  approval cards still render monospaced emails and ids with no LTR protection.
- **Prompt 4 has been run. Verdict: GO-WITH-CONDITIONS on deploying `96a0f3c`, NO-GO on calling
  the campaign ready to launch.** Those are different questions with different answers. The deploy
  is low-risk and strictly improves what Assaf complained about; the campaign rests on capabilities
  whose evidence is mocked, unreachable or absent. The full report went to Assaf as a file; the
  load-bearing findings are recorded below so they survive this session.
- **The production database was deliberately wiped** (2026-09-16). Restore point: RDS snapshot
  **`mia-pre-wipe-20260916-112916`**. Consequence for evidence: no production observation older
  than 2026-09-16 still holds. The single post-wipe live data point for the whole lead pipeline is
  one website lead reaching Telegram on `110ada6`, which predates C9 and C11 entirely.
- **The Telegram bot token was revoked and replaced.** ECS injects secrets at container start, so
  changing a secret does nothing until the task is replaced; and revoking a token drops the
  webhook, which must be re-registered via the `mia-telegram-webhook` entrypoint as an ECS one-off.
  A correct token with no webhook looks exactly like a wrong token: silence, no errors.
- **`aws ecs update-service --force-new-deployment` silently produced no new deployment twice.**
  Confirm a new deployment `createdAt` and that the task's `startedAt` is later than the secret's
  `LastChangedDate`; otherwise stop the task directly.

### Deploy ordering — verified 2026-09-16, and NOT in README

`README.md` steps 1-7 contain **no migration step**. C12 (#79) adds
`migrations/20260916_brain_knowledge_site_freshness.sql`, which has never been applied to
production. Insert between README steps 4 and 5:

```bash
uv run python scripts/run_ecs_migration.py --task-definition mia:N
```

- It applies the `.sql` files baked into **that image**. Run against `mia:65` it finds no new file,
  applies nothing and **exits 0** — a silent no-op that looks like success. Confirm the printed
  summary lists the file under `applied`, not `already` or `skipped`.
- **Deploying the code first is the dangerous order, and nothing catches it.** `schema_ready()`
  compares the running image's ORM columns against the DB, so the new image against the old schema
  makes `database_ready()` false: `/health` goes all-null, `/health/ready` returns 503, and every
  hourly ingest dies at the first ORM read. Both the ALB target group and the ECS container check
  probe **`/health/live`**, which returns 200 without touching the database, so ECS reports a
  healthy, stable deployment throughout. `/health/ready` has no consumer.
- **Migration-first is safe in the other direction**: `schema_ready()` only checks model ⊆ DB, so
  the four extra columns are invisible to `110ada6`. They are `NOT NULL` with **literal defaults**,
  so old-image inserts omit them and the server fills them — which is also why rollback stays
  clean, and why a `NOT NULL` without a default would have broken every old-image write during the
  rolling window.
- **Re-pin `mia-due-scan` to the new revision afterwards.** No script does it; it fails silently.
- Rollback: `update-service` back to `mia:65`, and re-pin `mia-due-scan` to 65 as well. No
  down-migration exists or is needed. Rolling back also restores the mangled Hebrew rendering and
  the English Activity cells, so it is cheap technically and immediately visible to Assaf.

### Release readiness — findings that must not be rediscovered (Prompt 4, at `96a0f3c`)

Gates: `2420 passed, 7 skipped`, ruff clean, widget checks pass. **A green suite is the weakest
evidence in this report**, for reasons the audit made concrete:

- **All 7 skips are PostgreSQL-DSN gaps, none benign.** CI's `postgres` job covers six of them on
  real PG 18. It does **not** cover
  `tests/unit/test_due_scan_worker.py::test_due_reminder_claim_survives_outer_rollback_after_accepted_send`,
  which is in none of that job's three named files and skips in `checks` too. **That test runs
  nowhere, ever** — the durable due-reminder claim on real Postgres is `NOT_INCLUDED`.
- **`node tests/unit/widget_behavior.test.js` is not in CI.** CI runs `node --check app/web/ask_mia.js`,
  a syntax parse only. `AGENTS.md` requires the behavioural file, so that gate exists only when a
  human runs it locally.
- **Real-model evals cannot run, and CI never invokes them.** `app/evals/predeploy/` needs
  credentials; `report.py` states plainly that a skip is not a pass. Pass@1/Pass^3 on all ten owner
  scenarios, including the hard-safety `owner_forbidden_write`, has **no evidence at this SHA**.
  The suite also defines **zero website scenarios**, so even with credentials the consent
  classifier and the narrative validator stay unevidenced.
- **There is no `INTEGRATION_TESTED` evidence anywhere in this system.** Sheets, Calendar, Gmail,
  Composio, Telegram and STT are faked in every test.
- **Local green is not CI green.** This machine runs CPython 3.14.3; CI pins 3.12.

Top blockers, in the audit's order (the full list is in the delivered report):

1. **C10 password auto-sync does not exist in AWS, and the next RDS rotation is ~2026-09-19.** The
   only blocker with a deadline. Either create the Lambda/IAM/EventBridge/DLQ and prove the event
   pattern with a deliberate rotation, or put a dated manual runbook step on the calendar.
2. **LinkedIn publishing may be unreachable, and it is the release demo.** The only route requires
   the literal word `CREATE` in the live Composio slug; if the real slug is `LINKEDIN_POST_UPDATE`
   or similar, Mia refuses. A read-only catalog listing settles it, and nothing else can be
   scheduled before it runs.
3. ~~**The widget's post-capture screen has zero real coverage, and its tests pin a contract the
   server cannot emit.**~~ **Closed by H2D** (below). `ask_mia.js` no longer branches on
   `ask_contact`/`confirm_contact`/`handoff`, the inline contact form they gated is retired, and
   the path a converting visitor actually hits is what the node suite now exercises.
4. **`state.pending_contact` is never cleared on refusal.** A number given, then refused, then
   quoted back by Mia, plus a later "כן", can capture someone who explicitly said no. One-line
   fix plus a test.
5. **The standing memory note "MIA_KILL_SWITCH does not stop the live owner tool loop" does not
   match `96a0f3c`** — here the worker path stops before every adapter. It was never checked
   against `110ada6`, so it may still be true of what production runs. Resolve before anyone
   relies on either reading.

### H2D — widget/server action vocabulary (branch `claude/mia-h2d-widget-vocabulary`)

Closes blocker 3 above. Base `b336158` ("retire the widget contact form and the dead action
vocabulary") plus one review-fix commit on the same branch. Not merged, not deployed.

- **What changed.** `app/web/ask_mia.js` no longer branches on `ask_contact`, `confirm_contact`
  or `handoff`, and the inline contact form those branches gated is gone — it could never open
  (only `ask_contact` opened it) and its submit handler read `contact_saved` as a *failed*
  capture. Free text through the consent classifier is now the single capture path.
- **Both wire paths are guarded, not just one.** `SiteV2Reply.__post_init__` refuses to
  construct an action outside `SITE_V2_ACTIONS`. That covers direct construction only, so
  `_wire_action` guards the two paths that actually reach the client — the value
  `run_site_v2_turn` computes, and the value `begin_site_message` replays out of
  `response_json` (which goes to the wire as a bare dict; `MessageOut.next_action` is a bare
  `str`). On those two it **degrades to `answer` and logs
  `site reply rejected reason=next_action_not_in_vocabulary`** rather than raising: the turn
  picks its label as its last statement, inside the API's transaction, after the contact save
  and the canonical events, so raising there would roll back a capture to punish a wrong label.
- **Commands.** `MIA_ENV=test uv run pytest` (full suite),
  `node tests/unit/widget_behavior.test.js`, `uv run ruff check app tests`. Results are in the
  branch's commit messages; evidence state is `LOCAL_TESTED`.

**H4b blocker — the consent-classifier bypass is still open, and now has no legitimate user.**
`app/surfaces/site_v2.py` (`exact_form_operation`) treats a structured `phone`/`email` plus the
exact literal `רוצה להמשיך עם אסף` as consent and fabricates an affirmative verdict, so the
classifier is never consulted. The widget that produced that shape is now retired, so the only
remaining callers are `tests/unit/test_public_website_guard.py:163,210`, which use the literal
as a deliberate bypass — that file belongs to H4b, which is why this was routed out rather than
reached for. Reproduced by execution on this branch: one POST to
`/v1/website/sessions/<id>/messages` with `{"text": "רוצה להמשיך עם אסף", "phone": "<any valid
number>", "client_message_id": "x"}` returns 200 with `next_action=contact_saved`,
`delivery_status=saved`, and **zero** calls into the consent classifier. `MessageIn`
`name`/`phone`/`email` (`app/api/website.py:87-89`) are likewise now client-only fields.

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

### C7b — merged as #73

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

### C12 — live knowledge (merged as #79, with review fixes)

Two independent staleness gaps, both previously invisible, per the CURRENT STATE note above.

1. **Mia lags the *files*.** `mia-ingest-knowledge` ran weekly
   (`cron(20 4 ? * MON *)`), so a Tuesday site edit sat unseen until the next Monday.
   `deploy/eventbridge-ingest-knowledge.example.json` now says `rate(1 hour)`. Confirmed
   safe first: `ingest_source` (`app/brain/knowledge.py:254`) already skips re-embedding on
   an unchanged content hash — `tests/unit/test_brain_voice_knowledge.py`'s
   `test_ingest_is_idempotent_on_content_hash` and
   `test_changed_content_retires_old_chunks_and_writes_new` already existed and pin exactly
   this (an unchanged source costs one GET and zero embedding spend; a real change still
   re-ingests) — nothing here weakens or duplicates them.

   **The live AWS schedule is NOT updated by this commit — no script does it.** The only
   automation in this repo for an EventBridge Scheduler resource is none: re-pointing
   `mia-due-scan` after a deploy is a manual `aws` CLI step (`README.md` step 6), and
   `mia-ingest-knowledge` has the identical gap. Until someone manually updates (or
   recreates) the live `mia-ingest-knowledge` schedule to match the new `rate(1 hour)`
   expression, production keeps running the old weekly cron regardless of what this file
   says. Do not assume the schedule change is live just because the example JSON changed.

2. **The *files* lag the *site*.** New module `app/brain/site_freshness.py`. During the
   scheduled ingest only (never per visitor request, never from `/health`), one `HEAD`
   against the site root and one per configured source compares `Last-Modified` headers
   (`compare_staleness`). Either header missing/unparsable, or the request failing, is
   always `"unknown"` — never a false `"fresh"`. `"stale"` only when the site is more than
   `MATERIAL_STALENESS` (6h) newer than a source's own header. Wired into
   `app/workers/ingest_knowledge.py`'s `main()` right after the real ingest, in the same
   transaction, best-effort (a freshness-check failure never aborts or rolls back a
   successful ingest). Persisted per source on `brain_knowledge_sources`
   (`migrations/20260916_brain_knowledge_site_freshness.sql`, additive): `site_last_modified`,
   `source_last_modified`, `site_checked_at`, `site_stale`.

   Surfaced in two places:
   - `/health`'s new `brain.knowledge_freshness`: one entry per *configured* source
     (`source_id`, `ingested`, `last_ingested_at`, `content_hash_prefix` — 12 hex chars, not
     the full sha256 — `site_stale`), including a source never ingested at all. DB read
     only, same cost as the existing `brain.corpus` counts — no network call from `/health`.
   - The owner daily brief: `app/domain/owner/briefs.py::apply_owner_brief` takes new
     optional `brain`/`knowledge_sources` params (every existing caller/test is unaffected),
     wired from `app/tools/owner/operations.py::_daily_brief` via `ToolContext.brain` +
     `ctx.settings.knowledge_source_list()`. Appends one Hebrew line — `owner_text()`-safe,
     no dash, filename(s) and date isolated automatically — **only** when at least one
     configured source is `"stale"`; silent when fresh **and** silent when merely
     `"unknown"` (a missing header is not evidence of staleness). Never more than one line
     regardless of how many sources are stale.

Branch `claude/mia-c12-live-knowledge` off `origin/master` (rebased onto `011ee92` after the
handoff-refresh session pushed ahead of this chunk's original `8ed912e` base — docs-only,
zero file overlap with this chunk's diff, confirmed by `git diff --stat 8ed912e origin/master`
before merging).

Verify: `MIA_ENV=test uv run pytest` → **2411 passed, 7 skipped** (16 new tests across
`test_brain_site_freshness.py` (new), `test_brain_voice_knowledge.py`,
`test_health.py`, `test_owner_briefs.py`). `uv run ruff check app tests` → clean.
`node tests/unit/widget_behavior.test.js` → passed (untouched this chunk).

One real bug caught by the test suite itself, not by review: the migration file's own
top comment originally contained a semicolon inside a `--` line, which
`app/db/migrate.py::_split_statements`' naive `sql.split(";")` does not treat as a comment
boundary — it silently mangled the migration into invalid SQL and failed every migration
test (`test_migrate.py`, `test_release_readiness.py`) with `OperationalError`. Fixed by
rewording the comment; `test_migration_sql_comments_do_not_contain_semicolons` exists
specifically to catch this class of bug and does.

Left deliberately alone, with reasons:
- The comparison window (`MATERIAL_STALENESS = 6h`) is a judgement call, not a measured
  constant — chosen because the ingest itself now runs hourly, so ordinary clock/CDN skew
  is minutes, not hours. Revisit if it proves noisy or insensitive in production.
- No crawl of page content anywhere, and no fetched body or header value is ever logged —
  only the verdict (`fresh`/`stale`/`unknown`) reaches stdout, matching the reason-codes-only
  logging rule. `/health` and the brief only ever read what the scheduled ingest already
  persisted.
- `knowledge_refresh.py`'s owner-triggered manual refresh tool is untouched: it still calls
  `ingest_website` directly, without the site-freshness check. That check is deliberately
  scoped to the *scheduled* run per the brief; the owner tool already gets an explicit,
  on-demand human decision, which is a different problem than an unattended weekly gap.
- Regenerating `llms.txt`/`llms-full.txt`/`pricing.md` on the site's own build (Vercel) so
  their `Last-Modified` tracks real content changes is Assaf's side, not this chunk's — this
  chunk only makes the drift *visible*, per the brief's explicit scope.

### In flight at handoff

**Nothing.** The code queue is closed: C12 merged as #79 and the social pass as #78, and no
campaign PR is open. Worktrees still on disk are `.claude/worktrees/mia-c12`, `mia-social` and
`mia-docs`; all three are merged and can be removed with `git worktree remove` once confirmed
clean. A stale locked directory may exist at `.claude/worktrees/mia-c6a` from an earlier
interrupted session — ignore it.

### Remaining queue

Everything below needs Assaf, not another coding session. The next session's job is to execute an
approved deploy (Prompt 5), not to write more code.

1. **Deploy `96a0f3c`** — brings all seven merged-but-undeployed PRs live. Prompt 4 says
   GO-WITH-CONDITIONS. Follow the "Deploy ordering" section above exactly: the migration step
   between README steps 4 and 5, and the `mia-due-scan` re-pin afterwards. Neither is in README.
2. **Before ~2026-09-19: the RDS rotation.** Create the Lambda / IAM role / EventBridge rule /
   DLQ / alarm, then **deliberately trigger a rotation** to prove the event pattern fires. It has
   never fired and cannot be verified any other way. A safety net that silently does not exist is
   worse than none, because it suppresses the manual check. The fallback, if the infrastructure is
   not created in time, is a dated manual runbook step: update `mia/prod` **and replace the task**.
3. **Read-only Composio catalog listing for LinkedIn**, before scheduling anything else. The
   publish route requires the literal word `CREATE` in the live slug; if the real slug differs,
   Mia refuses and the release demo does not exist. The same listing checks the forbidden-action
   word lists against real slug names.
4. **The approved live tests** (Prompt 5). Twenty-six of them are named individually in the
   delivered readiness report, ordered read-only first. Each needs its own yes.
5. **Two code fixes the audit surfaced that are worth doing before the campaign**, neither
   started: clear `state.pending_contact` on a refusal (a consent bug — a refused number can still
   be captured via a later readback plus "כן"), and reconcile the widget's post-capture contract
   with what `site_v2.py` can actually emit (the tests currently pin a dead branch).

### Start the next session with

```text
Read CLAUDE.md, HANDOFF.md section 0 (CURRENT STATE, then Deploy ordering) and TASKS.md.
Do not re-audit merged work. Prompt 4 has already been run - do not re-run it.

Verify: git fetch; origin/master SHA; gh pr list; git worktree list; and
curl -s https://mia.assafweb.com/health | jq '.deployment.commit_sha, .ops, .brain.corpus'

The code queue is closed. Master is 96a0f3c; production is 110ada6 on mia:65.
Do not start new chunks. The remaining work needs Assaf's approval, not more code.

If Assaf approves the deploy, execute Prompt 5 against 96a0f3c using the "Deploy ordering"
section: build+push, register mia:N, RUN THE MIGRATION (scripts/run_ecs_migration.py
--task-definition mia:N, confirming the file lands in `applied`), only then update-service,
then re-pin mia-due-scan, then verify /health and /health/ready - never /health/live.

Otherwise stop and ask. Do not deploy or create AWS resources without an explicit go.
```

Waiting on Assaf, not on the session: regenerate `llms.txt` / `llms-full.txt` / `pricing.md` on
Vercel (they are dated 09-14 and did not update with his 09-16 site change, so Mia cannot see it
however often she ingests); merge `assaf-landingPage#28` so the site stops serving the scripted
fake chat box (until it lands, the deploy ships a working widget to a site that does not embed
it); clean the Sheet rows; one controlled live LinkedIn post; decide whether `content_ideas`
should produce drafts instead of categories.

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

## Deploy gotchas — do not rediscover these

Carried forward from the 2026-09-11 handoff, whose narrative sections (the P0 CRM outage,
the website-capture root cause, the cleanup, the inline-widget PR and its "still open" list)
were deleted on 2026-09-16 as superseded. These are the only part of it still load-bearing.
Its "Housekeeping" section went too: the uncommitted `crm_v2.py` edits it warned about are
gone (the main checkout is clean), and its `.env` rule is already in `AGENTS.md`.

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
