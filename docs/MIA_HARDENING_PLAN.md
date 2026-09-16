# MIA_HARDENING_PLAN.md — every sector to 9+

Successor campaign to `MIA_CAMPAIGN_FINISH_PLAN.md`. That plan finished Mia; this one hardens
her. Chunk IDs are `H0`–`H10` so the ledger stays distinct from the finish campaign's `C` IDs
(`C6b`, `C6c`, `C7a` are still reserved there). State stays in `TASKS.md` and `HANDOFF.md`.
Briefs live in `MIA_CLAUDE_CODE_PROMPTS.md` under `## Hardening briefs`.

## 1 Goal and the bar

The 2026-09-16 agent inspection graded Mia **6/10 weighted** across 13 sectors at `8ed912e`.
The goal is **every sector above 8.5**, which means every sector at **9 or 10**.

The 9–10 band requires three things at once:

> Enforced at a single choke point that cannot be bypassed · adversarially tested · **observable
> in logs when it trips**.

Mia is strong on the first, weak on the second, and close to absent on the third. The whole
application emits **26 log statements across 8 files**; `app/workers/crm_delivery.py` is 702
lines with 0 log calls, 12 bare excepts and 48 returns. That third leg is why this is a campaign
and not a fix list, and it is why `H1` gates everything.

Grades are re-measured by re-running the inspection, not asserted. Evidence states are
`AGENTS.md`'s, never a percentage.

## 2 Sectors

| ID | Sector | At `8ed912e` |
|---|---|---|
| S1 | Visitor / owner isolation | 6 |
| S2 | Truthfulness — no false claims | 7 |
| S3 | Consent and contact capture | 6 |
| S4 | Approval and write gating | 7 |
| S5 | Tool layer quality | 8 |
| S6 | Grounding, memory and freshness | 6 |
| S7 | Model I/O robustness | 7 |
| S8 | Delivery and data integrity | 6 |
| S9 | Log privacy and observability | 7 |
| S10 | Degradation under failure | 6 |
| S11 | Hebrew presentation and egress | 8 |
| S12 | Test and eval honesty | 6 |
| S13 | Operational readiness | 7 |

Grades at `8ed912e` are superseded by `H0`'s re-baseline at `ef8ff78`.

## 3 Decisions (Assaf, 2026-09-17)

| Decision | Choice |
|---|---|
| Evidence bar for 9+ | `LOCAL_TESTED` everywhere, plus a **real-model eval** for S2, S3, S6 |
| Sequencing | Thematic chunks, **observability spine first** |
| Deploy | **At the end**, after sign-off |
| RDS rotation (~09-19) | **Dated manual runbook step**, no exception deploy |
| Integration evidence | Real-model eval, then a scoped sandbox tier (Sheets, Calendar, Gmail) |
| Cleanup | Its own chunk (`H10`), fed by `H0`'s inventory, evidence-first |

## 4 Work packages

| ID | Deliverable | Suggested atomic subchunks | Exit evidence |
|---|---|---|---|
| H0 | Re-baseline at `ef8ff78` + cleanup inventory | delta-grade the 12 new commits incl. the never-inspected `app/brain/site_freshness.py`; challenge the 36 deferred claims; the two completeness critics that never ran; cleanup inventory with liveness proofs | current grade per sector, a confirmed-defect ledger, a cleanup ledger separating dead from reserved. No code. |
| H1 | Observability spine | reason-code registry; one `guard_tripped` emitter; durable trip row on `/health`; `log_comm` with `success=True` on every channel; `persist_ai_run` wired into site v2; the silent zones | every choke point emits one distinguishable reason code, with a `caplog` test per code |
| H2 | Confirmed defects | telegram predecessor gate; `pending_contact` on `quoted`/`refused`; phone-shape check; calendar `ToolOutcome` | each fix ships with the test that would have caught it |
| H3 | Class fixes, not instance fixes | undiscardable typed outcome; `except RuntimeError` sweep; empty-result markers as constants; Chat/Gemini truncation contract | the shape cannot recur — a new instance fails to compile or fails a test |
| H4 | Provenance and session lifecycle | provenance carried with a parked contact; session TTL and sweep; origin bind; email-grants-a-write | a laundered `quoted` value and a resurrected session both have failing tests |
| H5 | The privileged loop | untrusted-data frame on owner tool results; visitor free text reaching the owner model | an injected instruction in a tool result does not change the loop's next tool call, proven by test |
| H6 | Resource safety | connection pool config + `pool_pre_ping`; transaction scope vs model latency; aggregate spend ceiling; rate-limiter topology | 16 concurrent visitors do not exhaust the pool; a daily ceiling exists and trips observably |
| H7 | The gate becomes real | flip the 7 permissive fakes to strict; `VisitorWorld` + website scenarios; `hard_safety_ids()` as the runner's authority; predeploy in CI; the tests that run nowhere | the gate cannot emit green with a hard-safety scenario unrun; both surfaces covered |
| H8 | Real-model evidence + sandbox tier | predeploy against the real model, Pass@1 / Pass^3; sandbox ports for Sheets, Calendar, Gmail | the first `INTEGRATION_TESTED` evidence in the system |
| H9 | Re-inspect, sign off, deploy | full re-inspection at the final SHA; deploy; token rotation + webhook; phone acceptance | every sector ≥9 with `file:line` evidence, then `DEPLOYED` + `PHONE_VERIFIED` |
| H10 | Cleanup | act on `H0`'s ledger: dead code, dead tests, stale docs, repo hygiene | nothing deleted without a liveness proof; the suite stays green; retired behaviour named in the commit |

**Ordering.** `H0` → `H1` → (`H2`, `H3`, `H4` may run in parallel) → `H5` → `H6` → `H7` → `H8`
→ `H10` → `H9`.

`H1` gates everything: no sector reaches 9 without the observability leg. `H7` gates `H8` — a
real-model run against permissive fakes proves nothing. `H10` runs late so that cleanup does not
churn files the other chunks are editing, and `H9` is last by Assaf's decision.

## 5 Ceilings — state them, do not grade around them

- **S9 is capped until the H9 deploy.** Production `110ada6` has leaked the Telegram bot token
  into CloudWatch for its entire life; master fixes it. Deploy-last is a decision, and this is
  its price.
- **S13 caps near 8.** Proving the rotation net needs a real rotation to fire with the Lambda
  deployed. The manual runbook was chosen, so the EventBridge pattern stays `UNVERIFIED`.
- **Integrations beyond Sheets/Calendar/Gmail stay faked** → `LOCAL_TESTED`, not
  `INTEGRATION_TESTED`, for parts of S5, S8, S10.
- **S6 depends on Vercel** (§6). The repo cannot fix a source that is stale at origin.

## 6 External dependencies — Assaf's, not the repo's

1. **Regenerate `llms.txt`, `llms-full.txt`, `pricing.md` on Vercel at build time.** All three are
   dated 2026-09-14 and did not move with the 09-16 site change. They are Mia's only knowledge
   sources, so no ingest cadence fixes this — C12 made ingest hourly against files that never
   change. Until this ships, S6 is capped regardless of H1–H8.
2. **Merge `assaf-landingPage#28`.** Until it does, assafweb.com serves the scripted fake chat, so
   H9 ships a working widget to a site that does not embed it. Merging auto-deploys to Vercel.
3. **09-19 rotation runbook step.** After the rotation, confirm the task's `startedAt` is later
   than the secret's `LastChangedDate`; hand-sync `mia/prod` if they diverged. The last time this
   was missed, production was down ~9 hours.

## 7 Invariants for every chunk

`AGENTS.md` wins on conflict; `CLAUDE.md` sets the session protocol. Additionally, for this
campaign:

- **A guard without a log is not done.** Every chunk that adds or moves a guard adds its reason
  code and a `caplog` test in the same commit.
- **A fix without the test that would have caught it is not done.**
- **Fix the class, not the instance.** If the same shape exists elsewhere, either fix it or record
  it as a named follow-up in `TASKS.md`. The inspection found three instance-fixes whose class
  reappeared one level up.
- **Nothing is deleted on a "verified dead" list alone.** Re-grep for callers *and* for
  dotted-string references (`monkeypatch.setattr("a.b.c", …)`, `patch(...)`, capability
  `port="…"`, entry points) before any removal. Several previously "verified dead" modules in this
  repo were live in production.
- **Do not weaken a test to make a change pass.** Retire a test only with the behaviour it covers,
  and say so in the commit.

## 8 Acceptance

The `H9` re-inspection is the acceptance test: all 13 sectors ≥9, each with `file:line` evidence
and a named reason code per guard, both completeness critics run, and no confirmed defect
outstanding. Anything short reopens its chunk.

Post-deploy: `/health` `deployment.commit_sha` matches the deployed SHA with all fields non-null,
`/health/ready` returns 200, and a real website lead lands in Assaf's Telegram end to end.
