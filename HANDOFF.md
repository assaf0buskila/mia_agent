# Mia handoff — 2026-09-11, ~10:45 UTC

Supersedes every earlier HANDOFF.md in this repo. Everything below was verified against
AWS, GitHub and the code today. Where an earlier handoff was wrong, it is called out.

## 1. P0 CRM delivery outage — FIXED AND DEPLOYED

Production ran `mia:58` (commit `e0ae94e`) and failed every CRM delivery cycle from
2026-09-10 23:18 UTC. Website leads did not reach Telegram for ~11.5 hours. Root cause:
`app/db/session.py` sets `autoflush=False` but the CRM tests ran with autoflush on
(PR #55, merged as master `a7a1b5b`).

**Now live:** ECS service `mia` on `mia:59`, image
`mia@sha256:62604f496686aca00de0e544cd12af5b310be67fd8a0a570a36177cd27b6e5ff`,
`MIA_BUILD_SHA=a7a1b5b33b01a9481470e306b24f24fe4033b646`. `/health` confirms
`deployment.commit_sha == a7a1b5b`. Rollout COMPLETED, 1/1 running, task HEALTHY.
Scheduler `mia-due-scan` ENABLED, re-pinned to `mia:59`. `mia-reconcile` still DISABLED
and still unrevisioned (leave it).

**Rollback:** `mia:58`.

**Verification status — read this carefully.** The failure loop is definitively stopped:
the old task logged a failure every ~6s, the new task logged ZERO warnings/errors in a
30-minute window. The last `CRM delivery cycle failed` line (10:39:14Z) came from the OLD
task's log stream during drain. BUT the delivery worker logs only failures by design
(`app/workers/crm_runtime.py:116`, to avoid leaking credentials/visitor content) and no
outbox-depth counter is exposed on `/health`, so **positive proof that the queued briefs
landed can only come from Assaf's Telegram.** That confirmation was still outstanding.

### Deploy gotchas that cost time — do not rediscover these

- **OCI index trap.** `scripts/deploy_ecs_revision.py` accepts only
  `oci.image.manifest.v1+json` / `docker.distribution.manifest.v2+json`. Docker 29's default
  build emits an attestation manifest + manifest list (an index) and the provenance check
  rejects it. Build with `--provenance=false --sbom=false --platform linux/amd64`.
- **The deploy script needs NO local Docker** — it verifies provenance via the ECR API. But it
  DOES require local `HEAD == --sha` and a clean tree, so never deploy from a worktree you are
  editing.
- **Docker Desktop on this machine is fragile** (the WSL `docker-desktop` distro stops; the CLI
  control plane hangs). Assaf revived it by hand from the GUI.
- **The Docker Windows credential helper is BROKEN** (`The stub received bad data`), so
  `docker login` cannot save credentials. Workaround used: an isolated `DOCKER_CONFIG` dir with
  the ECR token written straight into `auths`, deleted immediately after the push. This is
  unfixed — the next deploy will hit it again.
- No Docker-free build path is wired up. GitHub Actions can build but cannot push (no AWS creds,
  and an SCP explicitly denies IAM OIDC-provider actions). CodeBuild/S3/CodePipeline do not
  exist. If Docker dies again, CodeBuild is the path (~30-60 min of first-time plumbing).
- Git Bash mangles `/ecs/mia` — prefix AWS log commands with `MSYS_NO_PATHCONV=1`.
- `aws logs filter-log-events` returns only the first scan page, so `length(events)` is NOT a
  reliable count. Sort by timestamp instead.
- AWS credentials here come from `aws login` (profile `default`) and expire mid-session.

## 2. Cleanup — IN PROGRESS

Worktree `.claude/worktrees/mia-cleanup-final`, branch `claude/mia-v2-cleanup-final`
(= cleanup WIP `3311b0b` + master `a7a1b5b` merged). Landed so far:
`5352ce9` retire the broken OwnerGraph tests; `79af27f` remove langgraph.
Phase 3 was mid-flight with uncommitted edits. Not pushed, no PR yet.

### The earlier handoff's "verified dead" list is WRONG — do not trust it

Independently re-verified. These are LIVE and must NOT be deleted:

- `app/domain/policies/decision.py` — `app/domain/ai_runs.py` imports `DETERMINISTIC_NBA_CONFIDENCE`.
- `app/domain/policies/failure_policy.py` — bound to `CapabilityId.FDE_FAILURE_POLICY` (ALIVE).
- The "WhatsApp-identity" and "v1 website-session" `LeadStore` groups — 6 methods serve
  `app/api/website.py`, `app/domain/conversation_scope.py`, `app/domain/identity.py`;
  `count_open_reconciliation` feeds `/health`.
- `VISITOR_TOOLS` in `app/domain/two_state.py` — a live permission gate
  (`app/tools/registries/owner_tools.py:987`). Only the `"published_facts"` string is removable.
- `app/domain/meetings/{booking,changes,copy}.py` — EXCLUDED by decision. `tests/conftest.py`
  patches these by dotted string in `freeze_mia_clock`, so deleting them breaks ~10 test files
  and 100+ unrelated tests. Retiring meeting booking is an unanswered PRODUCT question for Assaf.

Remaining after Phase 3: Phase 4 (delete `app/surfaces/published_facts.py` + the one
`"published_facts"` string + 1 test + 1 assertion; delete `app/domain/owner/followups.py` —
the `owner/` one, NOT the live top-level `app/domain/followups.py` — plus its 9 tests, keeping
the other 6). Then full suite + ruff + PR + CI + deploy exactly as in §1.

Also fixed along the way: `app/core/capabilities.py` was advertising the already-deleted
`app.agents.owner.graph` as `status=ALIVE` on `/health`.

**Watch:** `test_one_owner_turn_retrieves_exactly_once`, once ported to `answer_owner`, asserts
`1 == 1` and proves nothing — it was built to catch two independent retrieval paths and only one
now exists. Either re-derive it or leave it clearly marked.

## 3. Website — Mia as an inline box. DECIDED, NOT STARTED

Assaf chose: **Mia replaces the hero demo chat.** `components/site/AiHeroChat.tsx` is a fully
scripted fake (5 hard-coded Hebrew messages, typewriter effect, no input, no network).

Website repo: `C:\Users\lenovo\Desktop\assaf project\assaf landing page`
(github.com/assaf0buskila/assaf-landingPage). **Currently on branch `rebrand/ai-solutions-studio`,
dirty, with the Leo files UNTRACKED. Commit `3cce2b9` is in `origin/main` but NOT in the
checked-out branch, and another agent session is editing this tree.** Pick a base branch
deliberately. Leave Leo alone — it was not in scope.

Plan: add an inline mode to `app/web/ask_mia.js` (resolve `[data-mia-inline]`, add a
`.ask-mia-inline` class that makes `#ask-mia-root` static/full-size and hides launcher+close,
guard the `@media (max-width:480px)` block with `:not(.ask-mia-inline)`, mount into the host and
`initSession()` immediately, and early-return from `closePanel()` and the Escape handler so the
box cannot be destroyed with no way back). Change `role="dialog"`/`aria-modal` to `role="region"`.
Then render `<div data-mia-inline>` in the hero slot (`components/site/LandingPage.tsx:586`,
inside `.hero-demo-shell`, `min(100%, 620px)`).

**Constraint:** the widget is ID-based throughout, so there can be only ONE instance per page —
inline and floating cannot coexist without a class/shadow-DOM refactor.

**Trap:** `NEXT_PUBLIC_MIA_BASE_URL` must be set per Vercel environment or `lib/mia.ts` returns
`null` silently and the widget just doesn't render. There is no CI on the website repo.

The widget itself works — verified at `https://mia.assafweb.com/v1/website/preview`.

## 4. Conversion gaps — NOT STARTED (all in `app/surfaces/site_v2.py`)

- **Highest leverage, one line:** `_system_prompt` (`:429-443`) contains NO instruction about when
  to invite contact details. The server already maintains a `_CONTACT_INVITE` regex to detect that
  Mia invited — nothing ever tells her to.
- `submit_lead` declares `name`/`next_step` but `call.arguments` is never read (`:722-738`).
- `state.next_step` is never assigned, so every brief ends with the default
  `צעד מוצע: לחזור לפונה ולברר את הצורך הבא` (`:619`).
- A name given in free text is never captured — only the widget's contact form sets it.
- `business_context` is a write-once latch on the visitor's first message (`:640-641`).
- **Ordering hazard:** capture runs BEFORE the model turn, so a recovered `next_step` can only
  improve the NEXT turn's brief. Accept the one-turn lag — do NOT reorder capture; five
  interdependent sites depend on it.

Separately, `app/surfaces/owner.py`: when the brain fails, the owner still gets the greeting
`"פה. מה צריך?"` (`:297`). The code comment claims this was fixed; only a log line was added.
`OWNER_UNAVAILABLE` already exists at `:48` — gate on the failure reason, since
`deterministic_intent` is a legitimate short ack.

## 5. Docs — NOT STARTED

Rewrite README.md, MIA_V2.md, TASKS.md, AGENTS.md short and code-based after cleanup lands.
TASKS.md still falsely claims "no deployment has occurred". AGENTS.md has the two graphs
backwards. `app/workers/telegram_owner.py:3` still mentions LangGraph in a docstring.

## Housekeeping

- The MAIN checkout (`assaf_agent/`, on master) still has uncommitted `app/services/crm_v2.py`
  and `tests/unit/test_crm_v2.py` changes. PR #55 supersedes them — tell that session or discard.
- Stale worktrees: `mia-product-feedback-0d7e4e` (3311b0b) and `mia-product-feedback-0bfc90`.
- `/health` reports `integration_failures: 11` from a table whose only writer
  (`upsert_reconciliation_finding`) appears to have no callers — worth a separate look.
- Never read `.env`. Secrets live in Secrets Manager.
