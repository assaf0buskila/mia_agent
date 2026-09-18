# Mia handoff — 2026-09-17

Section 0 is the current state for the next session. The sections after it are older handoffs and
still hold — the deploy gotchas especially.

## 0b. Telegram owner timeouts (2026-09-19) -- committed, NOT merged, NOT deployed

Branch `claude/telegram-owner-timeouts-1645f0`, head `d4bdbde`, 11 commits, branched from
`31a91c9`. Full detail in `TASKS.md`; this is the state a next session needs.

Assaf reported owner turns timing out on both transports, including for a bare `?`. Fixed
four root causes plus one found while fixing. The headline number: the owner execution
deadline used to start before media download, STT, transcript persistence and the coalesce
wait, and STT alone can take 60s (two OpenAI rungs plus Gemini at 20s each) out of a 45s
budget. It now starts once the final owner utterance exists. `MIA_OWNER_TURN_TIMEOUT_SECONDS`
stays 45 by his decision -- the fix is the boundary and the per-child caps, not a bigger
number.

**Two things block a merge, one of them only Assaf can do:**

1. `.env.example` must document four new `MIA_OWNER_*` names (listed in `TASKS.md`).
   `test_env_example_documents_settings_and_adapter_map` fails until then, so CI cannot go
   green. **Every agent session is denied that path by permission settings** -- do not waste a
   session rediscovering that. Same class as the `*secret*` deny already recorded.
2. No PR is open. Assaf said commit only, explicitly no deploy, and he had already observed
   Mia working well at the time. Do not open one, merge, or deploy without a fresh go.

Evidence state **LOCAL_TESTED** and no higher: 2666 unit tests pass and three independent
opus reviews ran, but every provider and STT call is mocked. Per `AGENTS.md` that proves
nothing about live behaviour, so a real Telegram turn is still required -- voice note, bare
`?`, the capability sentence, one tool-using question -- then read the new `timeout_stage`
log lines.

Three durable gotchas this chunk established:

- **`tests/conftest.py` zeroes `COALESCE_WAIT_S` suite-wide.** That is why no existing test
  could see preprocessing eating the reasoning budget. Any test about turn timing must set a
  non-zero value locally.
- **`HANG_REPLY` and the brain's timeout line are byte-identical.** Proved by execution. A
  screenshot cannot tell you which layer ran out of budget; only `timeout_stage=` can.
- **A `\b` written through a Bash heredoc becomes a literal 0x08.** The same bug class the
  2026-09-17 handoff recorded for non-raw Python strings, now via the tooling. Use the
  Edit/Write tools for any content containing backslashes.

## 0. Where this stands (2026-09-17, end of session)

### Production is CURRENT and verified

`6369126` is live on ECS task definition **`mia:67`**. Verified, not assumed:

- `/health` `deployment.commit_sha` matches `6369126`, every field non-null, `/health/ready` 200.
- The migration step ran against `mia:67` **before** `update-service`: `applied: []`,
  `skipped: []`, `failed: ''`, 46 already. Correct — the only new migration shipped in the
  previous deploy. Nothing was *skipped*, which is the failure this step exists to catch.
- Scheduler `mia-due-scan` re-pinned to `mia:67`; it was still on `:66`. Nothing does this
  automatically and forgetting it fails silently. `mia-reconcile` left DISABLED.
- Live smoke on production afterwards: a website turn saying *"Can I get a quote for a landing
  page? email me at ..."* returns `contact_saved`; a POST with a forged
  `Sec-Fetch-Site: same-origin` and no Origin returns 403; a POST with no Origin returns 403.

**Nine PRs merged today**, taking production from `110ada6` to `6369126`:

| PR | What |
|---|---|
| #81 | **P0** — the contact veto discarded ordinary leads before the classifier ran |
| #82 | Deploy scripts survive the non-UTF-8 text ECS returns in service events |
| #83 | **Security** — a public writer cannot rewrite a contact row it does not own |
| #84 | A total owner send failure is `failed` (retryable), not `processed` (terminal) |
| #85 | The reconcile pass survives a poison job and stops starving |
| #86 | A truncation stops masquerading as a dead model, on both transports |
| #87 | Hardening campaign plan, ledger, H0 re-baseline |
| #88 | Wave-1 routed follow-ups recorded |
| #89 | Origin bind stops accepting a header no browser can send cross-origin |

### The P0, because it is why today happened

`app/surfaces/site_v2.py` vetoed contact capture whenever a negation **or** an example regex
matched anywhere in the visitor's message — on bare words ordinary buyers use. Measured by
executing the regexes: **8 of 11 realistic lead messages carrying a real phone or email were
discarded**, including `Can I get a quote for a landing page? email me at dana@x.com`, killed by
the bare word `quote`. It ran *before* the classifier and *before* the value was parked, so the
documented next-turn recovery did not apply — the lead was gone, leaving one warning line.
Neither regex had a single test. Live in `110ada6`; now fixed, deployed and re-proven in
production.

It took **four review rounds**. Rounds 1–3 failed on the same class: every widening of the guard
to catch a refusal started swallowing customers. Round 3 failed because the pattern allowed a
possessive, so `don't call my office line, call my mobile 0501234567` — a lead redirecting a
channel — read as a refusal. A literal `0x08` backspace also shipped inside the pattern because
`\b` was written through a non-raw string: it compiled fine and matched nothing.

### IN FLIGHT — three branches fixed but NOT re-reviewed

The wave-2 round-2 **fixers finished and committed; the re-reviewers died on a session usage
limit before running.** Do not merge any of these on the author's word.

| Branch | Head | State |
|---|---|---|
| `claude/mia-h0a-column-width-guard` | `edb405c` | all findings addressed, including a HIGH where truncation failed open on multi-row INSERT while logging success |
| `claude/mia-h5b-sanitise-untrusted` | `560eeaf` | **`all_addressed: false`** — read its return before trusting it |
| `claude/mia-h2d-widget-vocabulary` | `2724157` | all findings addressed |

To resume: re-review each at its head against `origin/master...<sha>`, then merge on PASS **and**
CI green. The review workflow is persisted under
`workflows/scripts/mia-hardening-wave1-round2-*.js` and takes a findings file as an arg.

### The campaign: 5 of 23 chunks merged

Plan: `docs/MIA_HARDENING_PLAN.md`. Ledger: the hardening section of `TASKS.md`. Briefs:
`docs/MIA_CLAUDE_CODE_PROMPTS.md`.

Merged: H1a, H3a, H3b, H4a, H4b. In flight: H0a, H5b, H2d. Remaining: 15.

**The pattern that repeated in every chunk of both waves, without exception:** every chunk needed
at least two review rounds, and every round found something real. Three shapes recur, and they
belong in every future brief — they are already in the wave-2 workflow script verbatim:

1. **Generalising is not free.** An author hoisted a shared exception tuple so two call sites
   could not drift apart, and the sharing silently flipped an outcome from `unknown` to `failed`
   on a path where that causes a duplicate message to the owner. Prove by execution that shared
   semantics are identical at both sites, or write the difference out at each with a comment.
2. **A guard that cannot fail is not a guard.** A drift test compared handled exception *names*
   and passed cleanly on the buggy head, because the bug was in the outcome each name mapped to.
   A column-width guard failed open on the shape that mattered while logging success. Run every
   guard against the broken code and watch it fail before relying on it.
3. **Stay inside your owned files.** An author found a real bug of the same class in another
   chunk's file and correctly refused to fix it, routing it out instead. Both reviewers confirmed
   the refusal was right. Report it; do not reach for it.

### Decisions Assaf made today

- **Evidence bar** for 9+: LOCAL_TESTED everywhere plus a real-model eval for the
  model-dependent sectors. **Sequencing**: thematic chunks, observability spine first.
  **Deploy**: at the end of a wave, not per chunk.
- **Column guard**: RAISE on identity and key columns, TRUNCATE on prose. Implemented in H0a.
- **Widget contact form**: RETIRE it. The website now has one capture path. Implemented in H2d.
- **Untrusted text in the owner loop**: SANITISE, not merely frame — he accepted explicitly that
  owner briefs will show scrubbed text. H5b overstepped by also flattening the visitor-facing
  answer path; that was sent back and must stay owner-only.
- **Heuristic fixes**: treat them as ordinary chunks, no corpus-first process. This was flagged
  as the thing that cost four rounds on the P0; he chose it knowing that.
- **Telegram token**: NOT rotating. Recorded as an accepted risk in `TASKS.md` with its evidence
  and the full procedure if it is ever revisited. **Do not re-raise it.**

### Still Assaf's, not the repo's

1. **Regenerate `llms.txt` / `llms-full.txt` / `pricing.md` on Vercel.** Measured today:
   `llms.txt` and `pricing.md` carry `Last-Modified: 14 Sep`; `llms-full.txt` was rebuilt today
   but produced **byte-identical content** (`unchanged (0 chunks)` on a real ingest run). The
   generator is not reading his updated site. Mia's knowledge is frozen at 09-14 regardless of
   anything shipped here.
2. **Merge `assaf-landingPage#28`** — until it does, assafweb.com serves the scripted fake chat,
   so the working widget is not embedded on the live site.
3. **RDS rotation ~2026-09-19** — manual runbook step, by his decision. After it fires, confirm
   the task's `startedAt` is later than the secret's `LastChangedDate`, and hand-sync `mia/prod`
   if they diverged. The last time this was missed, production was down about nine hours.

### Two defects found today and deliberately NOT fixed

- **C12's freshness signal is structurally dead.** `check_site_freshness` anchors on the site
  root's `Last-Modified`; the Vercel-hosted root sends **no such header**, so `site_stale` is
  `unknown` for every source, always. Verified by curl and by a real ingest run.
- **LinkedIn's capability line overstates what the provider offers.**
  `app/domain/owner/social_capabilities.py:67` says profile reading is available and bounds it
  only against analytics. The read returns name and headline only — LinkedIn exposes nothing more
  at any obtainable scope, and three Composio slugs referenced in the code
  (`LINKEDIN_GET_MY_PROFILE`, `LINKEDIN_GET_PROFILE`, `LINKEDIN_GET_USER_INFO`) do not exist.
  Every "full profile works" test hand-feeds invented data through a MockTransport.

### Commands this session used

    MIA_ENV=test uv run pytest --basetemp=.cache/<name> -p no:cacheprovider > .cache/<name>.txt 2>&1
    uv run ruff check app tests
    node tests/unit/widget_behavior.test.js
    gh pr checks <n> --watch --interval 30
    gh pr merge <n> --merge
    git archive $SHA | docker build -f deploy/Dockerfile --provenance=false --sbom=false
        --platform linux/amd64 --build-arg MIA_BUILD_SHA=$SHA -t <ecr>/mia:v2-$SHA -
    uv run python scripts/deploy_ecs_revision.py --v2-release --image-uri <ecr>/mia@<digest> --sha $SHA
    uv run python scripts/run_ecs_migration.py --task-definition mia:N   # BEFORE update-service

Docker's Windows credential helper is still broken (`The stub received bad data`). The workaround
used today: an isolated `DOCKER_CONFIG` directory with the ECR token written straight into
`auths`, deleted immediately after the push.

Two invisible-character bugs bit today, both costing real time: `\b` written through a non-raw
Python string became a literal `0x08` inside a regex that still compiled and matched nothing, and
a script rewritten with Windows CRLF was rejected for containing control characters. When a
pattern or a generated file behaves impossibly, scan it for control bytes before re-reading the
logic.

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
