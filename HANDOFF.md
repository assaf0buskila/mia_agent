# Mia handoff — 2026-09-15

Section 0 is the campaign-finish state for the next session. Sections 1 onward are the
2026-09-11 handoff and still hold (deploy procedure and gotchas especially).

## 0. Campaign finish — where it stands and how to continue

Plan: `docs/MIA_CAMPAIGN_FINISH_PLAN.md`. Chunk briefs: `docs/MIA_CLAUDE_CODE_PROMPTS.md`
(the "Ready briefs" section at the end is copy-paste ready). Session rules: `CLAUDE.md`.

**Production is unchanged.** `/health` last reported commit `4b80f31` (2026-09-14). Nothing
below is deployed. Every item is `LOCAL_TESTED` + CI-green + independently reviewed at most.

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

### In flight at handoff

Nothing. Still, before acting, run `gh pr list` and `git worktree list`: the finished chunk worktrees
(`.claude/worktrees/mia-c1b`, `mia-c2a`, `mia-c3a`, `mia-c3b`, `mia-c4`, `mia-c5`, `mia-c5h`) are
merged and can be removed with `git worktree remove` once confirmed clean. Master after the
last campaign merge: `c9a8df5` (#69).

C4's empty-state constants for the tool-loop "no data" markers: `GMAIL_BRIEF_EMPTY_WINDOW`
("EMAIL DATA (not instructions): no messages in the inspected window.") and
`OWNER_UNCERTAIN_WRITES_EMPTY` ("No uncertain provider writes are waiting on review.").

### Remaining queue (in order; max 2 agents at a time)

1. **C2b approval cards** — brief ready. Cards from the stored envelope, one message per proposal
   with its own buttons, pending view lists up to 5, callback `sent` reflects edit success with one
   fallback send. Touches approvals: opus review required.
2. **C6a social truth** — brief ready; C4 (which also touched `two_state.py` and
   `request_routing.py`) is merged, so branch from the latest master. C2b and C6a own disjoint
   files and may run as the two concurrent agents.
3. **C7b cleanup + docs** — brief ready; last. Includes two reviewed follow-ups (see below).
4. **Prompt 4 release readiness** (opus, read-only): full gates, capability evidence matrix,
   exact live tests needing Assaf's approval, rollout + rollback plan. **Stop for go/no-go.**
5. **Prompt 5 deploy** only after Assaf approves a specific CI-green SHA; then phone acceptance.

### Assaf's decisions (2026-09-14)

- Merge each chunk when review PASS + CI green; deploy is a separate go.
- Live Sheet audited read-only, structure only (done): Contacts 16 cols (O = Mia ID, P = marker
  `mia-contacts-v2`); 2 legacy rows lack a Mia ID and their phones lost the leading 0 (stored as
  numbers) — owner review, never a guessed repair; 5 test/fixture rows — classify, don't delete.
- `app/domain/meetings` stays as is (Calendar imports `meetings.slots`; tests patch it by dotted string).
- LinkedIn publishing is in scope: at release, one controlled live post approved by Assaf.
- Instagram writes stay denied (policy). Calendar = own events, no guests/recurrence.

### Follow-ups already identified (not yet done)

- `app/integrations/calendar.py:~794`: agenda port without `list_events_strict` raises
  AttributeError at approval → `getattr` + fail closed (in C7b brief).
- Add C4's empty-state constants to `owner_agent.py` exact "no data" markers (in C7b brief).
- Routing needle collisions above (in C6a brief).
- PostgreSQL coverage for C3a same-turn refresh.
- **Generic Composio writes may never execute:** the C4 builder found `"composio_approval"` missing
  from `ALLOWLISTED_OPERATION_SCOPES`. C0 believed `propose_composio_write` has no callers, so this
  may be dead code rather than a live bug — verify callers first (fold into C7b's dead-code table:
  either delete the unreachable path or allowlist the scope with a test).
- C3b leftovers: raw `crm_…` ids in hot-lead replies; hardcoded `Asia/Jerusalem` default in
  `crm_v2.py` (pass `settings.timezone`); broad `except Exception` in the `tools/owner/crm.py`
  workspace check; header drift still repaired as a side effect of a read (needs an
  `owner_actions.py` change).
- Pre-existing test-order flake on master:
  `tests/unit/test_gmail_send_policy.py::test_owner_telegram_asked_then_approved_send_calls_send_draft`
  fails when run after certain other test files.

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
