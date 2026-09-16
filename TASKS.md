# TASKS.md

Updated 2026-09-16 (end of campaign coding). **Production is deployed and current**: `110ada6` is live on ECS task
definition `mia:65`, verified via `/health`, with a real website lead delivered end to end to
Assaf's Telegram. This supersedes every earlier "nothing is deployed" note in this file and in
`HANDOFF.md` (both said `4b80f31`/#60, which is now stale). Detail and next-session instructions
in `HANDOFF.md` section 0.

## Campaign finish (plan: `docs/MIA_CAMPAIGN_FINISH_PLAN.md`)

Merge policy (Assaf): each chunk merges when independent review is PASS and CI is green.
Deploy is a separate go. Max 2 agents at once (session usage limit).

- [x] C0 — audit + plan + rules (#61)
- [x] C1a — `crm_upsert` crash → exact proposal; non-ToolResult guard (#62)
- [x] C2a — explicit platform routing; safe owner Markdown + span-safe split (#63)
- [x] C5 — reschedule self-conflict; failed agenda read ≠ free day (#64)
- [x] C1b — greeting-prefixed replies kept; exact "no data" markers; usage on failed turns (#65)
- [x] C3a — website lead brief: greeting latch, de-duplicated layout, same-turn refresh (#66)
- [x] C5h — reschedule safety check fails closed (#67)
- [x] C3b — owner reports count v2 leads; Sheet created/updated system-owned; CRM reads stop
      creating tabs (#70)
- [x] C4 — `gmail_brief` daily email data; `owner_uncertain_writes` (#69)
- [x] C2b — approval cards from the stored envelope; callback `sent` truth (#72)
- [x] C6a — social capability truth + routing collisions (#71)
- [x] C7b — proven-dead code, v1 hot-lead auto-freeze (`apply_hot_handoff`) retired while its
      read path (`list_hot_lead_ids`/`hot_ids`/`set_takeover_state`) stays live, reviewed
      follow-ups, docs. A round-2 review caught a false premise in the first pass (production
      has a live takeover-state row), fixed at `2311e5b` (#73).
- [x] C11 — Activity Sheet cells readable again: Hebrew who/action and a short outcome, with
      the stored enum unchanged so `store.py`'s `contact_captured` queries still match (#75).
- [x] C8 — Telegram bot token no longer leaks into CloudWatch via httpx request logs
      (`app/core/redact.py`). The in-app `MIA_DATABASE_PASSWORD` override design was
      dropped after review (IAM boundary); see HANDOFF section 0.
- [x] C8 follow-up — `RedactingFilter` (`app/core/logging.py`) now scrubs a non-str
      `record.msg` and `exc_info`/`exc_text` too, not just a str `msg`; own commit.
- [x] C9 — Hebrew presentation standard: `owner_text()` egress normaliser, bidi isolation,
      no em-dashes, no raw ids (#76). LOCAL_TESTED only: no test in this repo can render bidi
      the way a Telegram client does, so this has a permanent ceiling until Assaf reads his phone.
- [ ] C10 — RDS to mia/prod password auto-sync (`scripts/lambda_sync_db_password.py`; the
      commits label it C9, renamed here to avoid colliding with the Hebrew chunk).
      `CODE_CHECKED` + `LOCAL_TESTED` only, NOT deployed. Round-2 review fixed: EventBridge
      detail-type (was `AwsApiCall`, which could never match a service-emitted rotation event
      and is still UNVERIFIED against a real rotation), removed `ecs:DescribeServices` (it
      turned a successful run into a failure under throttling), added log group, DLQ and an
      `Errors` alarm. Before deploy: create the Lambda, role and EventBridge rule, and verify
      the event pattern against one real rotation.
- [x] C12 — live knowledge: hourly ingest instead of weekly (safe: `content_hash` already
      skips unchanged sources); `/health`'s `brain.knowledge_freshness` reports per-source
      recency + hash prefix + site-vs-files staleness; owner brief gets one Hebrew line only
      when a source is stale. Committed locally on `claude/mia-c12-live-knowledge`, not
      pushed/PR'd yet; see `HANDOFF.md` section 0 for detail. The live EventBridge schedule is
      NOT re-pointed by this commit — no script does that, same manual step as `mia-due-scan`.
- [x] Social pass — `social_capabilities.py` answered in English while every other owner surface
      answers in Hebrew; now Hebrew with no capability claim changed (#78). The model-facing
      `ToolSpec.description`s and `SOCIAL_WRITING_RULE` stay English on purpose. Still open from
      the same pass and NOT code: verify C6a's routing live (production already runs C6a), and
      the one controlled live LinkedIn post.
- [x] Prompt 4 — release readiness, go/no-go. **Run at `96a0f3c`. Verdict: GO-WITH-CONDITIONS on
      deploying, NO-GO on calling the campaign ready to launch.** Full report delivered to Assaf;
      the load-bearing findings are in `HANDOFF.md` section 0 under "Release readiness". The
      conditions are: run the migration before `update-service`, re-pin `mia-due-scan` after, and
      pass the post-deploy smoke tests.
- [ ] Prompt 5 — approved deploy + phone acceptance. **Blocked on Assaf's go.** Twenty-six
      controlled live tests are named individually in the readiness report, ordered read-only
      first; each needs its own approval.
- [ ] **Before ~2026-09-19** — create the C10 Lambda/IAM/EventBridge/DLQ and deliberately trigger
      an RDS rotation to prove the event pattern fires. It never has. The only deadline item.
- [ ] **Read-only Composio catalog listing for LinkedIn**, before scheduling the live post. The
      publish route requires the literal word `CREATE` in the live slug; if the real slug differs
      Mia refuses, and the release demo does not exist.

## Hardening campaign (plan: `docs/MIA_HARDENING_PLAN.md`)

Goal: every one of the 13 inspection sectors above 8.5, i.e. at 9 or 10. The 9-10 band needs all
three of: an unbypassable choke point, an adversarial test, **and** an observable log when the
guard trips. Mia is strong on the first, weak on the second, near-absent on the third — the whole
application emits 26 log statements across 8 files. That is why H1 gates everything.

Baseline: the 2026-09-16 agent inspection graded 6/10 weighted at `8ed912e`
(S1 6, S2 7, S3 6, S4 7, S5 8, S6 6, S7 7, S8 6, S9 7, S10 6, S11 8, S12 6, S13 7).
Master is now `ef8ff78`, twelve commits later, so those grades are stale until H0 re-measures.
Scorecard: https://claude.ai/artifact/DUuZeGmxa98EaiM5fg2Ft1

Decisions (Assaf, 2026-09-17): evidence bar is `LOCAL_TESTED` everywhere plus a real-model eval
for S2/S3/S6; thematic chunks, observability spine first; **deploy at the end**; the ~09-19 RDS
rotation gets a dated manual runbook step rather than an exception deploy.

Order: H0 → H1 → (H2, H3, H4 parallel) → H5 → H6 → H7 → H8 → H10 → H9.

- [x] H0 — re-baseline at `ef8ff78` + cleanup inventory. 19 agents, 36/36 claims challenged:
      **27 confirmed and still present, 9 refuted, 1 already fixed by C12.** 37 cleanup
      candidates, 5 proven dead, 2 proven alive.

### P0 — the contact veto silently loses leads, in production, today

`app/surfaces/site_v2.py:594` vetoes contact capture whenever `_CONTACT_NEGATION` (`:133`) or
`_CONTACT_EXAMPLE` (`:143`) matches anywhere in the visitor's message. Reproduced by executing
the regexes against realistic lead text: **8 of 11 messages carrying a real phone or email are
vetoed**, including the highest-intent phrasing there is —

- `Can I get a quote for a landing page? email me at dana@x.com` → vetoed on the bare word
  **`quote`** (meant to catch `quoted`, as in quoting a third party; it catches *price quote*)
- `I dont have a landline, my mobile is 052-7654321` → `dont`
- `never mind the email, call me at 052-7654321` → `never`
- `no need to rush, email me at dana@x.com` → `no need to`
- `send me a sample, my email is dana@x.com` → `sample`
- Hebrew: `בלי מייל בבקשה, תתקשרו 052-7654321` → `בלי מייל`

The veto returns `{}` **before** the semantic classifier runs and **before** `state.pending_contact`
is populated (that only happens in the `else` branch at `:638-642`), so the documented
next-turn-confirmation recovery does not exist — the lead is simply gone, leaving one
undifferentiated warning line. `grep` across `tests/` for `_CONTACT_EXAMPLE`,
`_CONTACT_NEGATION` and `negation_or_example_veto` returns **0**. Byte-identical in production
`110ada6` (`git show 110ada6:app/surfaces/site_v2.py` line 594).

It also contradicts the design rule written 20 lines below it at `:616-618`: *"Phrase detectors
must not gate semantically valid wording."* So the fix is to delete the pre-classifier veto and
let the consent classifier decide — which makes it **HEAVY to design and HEAVY to review** under
`AGENTS.md`, not a regex tweak. **Decision needed: hotfix now, or lead H2 with it.**

### Grades at `ef8ff78` (overall 5, down from 6)

| S1 | S2 | S3 | S4 | S5 | S6 | S7 | S8 | S9 | S10 | S11 | S12 | S13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5↓ | 6↓ | **4↓↓** | 7 | 7↓ | 6 | 6↓ | 6 | 6↓ | 6 | 8 | 5↓ | 7 |

Six sectors fell. Nothing rose: C12 is good work, but it gates nothing on the visitor path.
Two refutations cleared guards that were being blamed unfairly — approval expiry **is** enforced
at every callback path, and the Hebrew edit-truncation defect is unreachable behind a working
fallback.

### What H0 changed about the plan

- **H2 is now the most urgent chunk, not H1**, and leads with the veto plus the captured-latch
  fix — both lose a lead silently on the same path and share a test harness.
- **H3 must be re-scoped raiser-first before it starts, or it will do harm.** The
  `except RuntimeError` sweep was planned on one reported instance. There are five call sites and
  only `app/core/outbound.py:45-49` is dead; three of the others catch a raiser that genuinely
  raises bare `RuntimeError`. A call-site grep would "fix" three correct handlers and miss the
  inverse class — a `MiaError` raised where only `RuntimeError` is caught.
- **H4 gains the client half** — the localStorage credential and transcript in `ask_mia.js`.
- **H1 shrinks and gets concrete**: baseline to beat is 28 log statements across 8 files.
- **`app/domain/meetings/*` (1471 lines + ~2800 lines of tests) is escalated to Assaf as a
  product question, not cleanup.** Retire it or re-wire it — H10 must not touch it either way.
- [ ] H1 — observability spine. Reason-code registry, one `guard_tripped` emitter, a durable trip
      row on `/health`, `log_comm` called with `success=True` on every channel, `persist_ai_run`
      wired into site v2. Precondition for all 13 sectors.
- [ ] H2 — the confirmed defects, each with the test that would have caught it.
- [ ] H3 — class fixes, not instance fixes: an undiscardable typed outcome, the
      `except RuntimeError` sweep, empty-result markers as constants, the Chat/Gemini truncation
      contract.
- [ ] H4 — provenance and session lifecycle: provenance carried with a parked contact, session
      TTL, origin bind, email-grants-a-write.
- [ ] H5 — the privileged loop: untrusted-data frame on owner tool results, visitor free text
      reaching the owner model.
- [ ] H6 — resource safety: connection pool config and `pool_pre_ping`, transaction scope vs model
      latency, aggregate spend ceiling, rate-limiter topology.
- [ ] H7 — the gate becomes real: flip the 7 permissive fakes to strict, `VisitorWorld` + website
      scenarios, `hard_safety_ids()` as the runner's authority, predeploy in CI.
- [ ] H8 — real-model evidence and a sandbox tier for Sheets/Calendar/Gmail. First
      `INTEGRATION_TESTED` evidence in the system.
- [ ] H10 — cleanup, acting on H0's ledger. Nothing deleted without a liveness proof.
- [ ] H9 — re-inspect at the final SHA, sign off, then deploy, rotate the Telegram token,
      re-register the webhook, phone acceptance. **Stop for Assaf.**

Ceilings recorded rather than graded around: S9 is capped until the H9 deploy (production has
leaked the bot token into CloudWatch for the whole life of `110ada6`; master fixes it); S13 caps
near 8 until a real rotation proves the EventBridge pattern; integrations beyond
Sheets/Calendar/Gmail stay faked; S6 is capped by Vercel, below.

External, Assaf's not the repo's:

- [ ] Regenerate `llms.txt` / `llms-full.txt` / `pricing.md` on Vercel at build time. All three are
      dated 09-14 and did not move with the 09-16 site change. They are Mia's only knowledge
      sources, so C12's hourly ingest runs against files that never change.
- [ ] Merge `assaf-landingPage#28`. Until it does, assafweb.com serves the scripted fake chat, so
      H9 would ship a working widget to a site that does not embed it.
- [ ] 09-19 rotation runbook step: confirm the task's `startedAt` is later than the secret's
      `LastChangedDate`, hand-sync `mia/prod` if they diverged.

## Gaps the readiness audit found in the gates themselves

Recorded so nobody reads a green suite as completeness.

- [ ] `tests/unit/test_due_scan_worker.py::test_due_reminder_claim_survives_outer_rollback_after_accepted_send`
      **runs nowhere** — it needs a Postgres DSN, so it skips locally and in CI's `checks` job, and
      CI's `postgres` job runs only three named files, none of them this one.
- [ ] `node tests/unit/widget_behavior.test.js` is **not in CI**. CI runs `node --check` on the
      widget, a syntax parse. `AGENTS.md` requires the behavioural file; that gate is human-only.
- [ ] The real-model eval suite (`app/evals/predeploy/`) is invoked by **no CI job** and cannot run
      without credentials, so `owner_forbidden_write` — marked `hard_safety`, must pass 3/3 — has
      no evidence at any SHA. The suite also defines **zero website scenarios**, so the consent
      classifier and narrative validator would stay unevidenced even with credentials.
- [ ] There is **no `INTEGRATION_TESTED` evidence anywhere**: Sheets, Calendar, Gmail, Composio,
      Telegram and STT are faked in every test.
- [ ] Local runs use CPython 3.14.3; CI pins 3.12. Neither result is evidence about the other.
- [ ] `state.pending_contact` is never cleared on a refusal, so a refused number can still be
      captured by a later readback plus "כן". A consent bug, one-line fix plus a test.
- [ ] The widget's post-capture screen branches on `ask_contact`/`confirm_contact`/`handoff`,
      which `site_v2.py` can never emit, and the tests assert that dead branch stays.


## Done today

- [x] P0 — CRM delivery worker failing every cycle since the v2 rollout. Fixed (#55),
      deployed, failure loop confirmed stopped.
- [x] v2 dead-code cleanup merged (#56). 1967 passed / 7 skipped, ruff clean.
- [x] Widget inline mode for embedding Mia as a box (#56), serving on production.
- [x] Mia now invites contact details; `submit_lead` `name`/`next_step` are no longer
      discarded (#56).
- [x] Capture: confirmation-turn dead end and byte-equal span check fixed (#56, #57).
- [x] Reason-code logging on every consent failure path — #58 green, not yet merged.

## Open

- [x] **Website lead capture — root cause found, fixed and merged.** The consent classifier ran
      on the Responses API with reasoning enabled and a 180-token `max_output_tokens`; reasoning
      consumed the budget, no tool call was emitted, and the adapter reported that truncation as a
      normal response, so every verdict silently became "ambiguous" and the chain never fell back.
      Budget raised to 600, truncation logged, regression tests guard the requested budget. This is
      **deployed** — it is in `110ada6` — and one real website lead has reached Telegram since.
      The line above that called this "needs deploy" was stale and contradicted the header.
- [x] Contact-turn reply blanking to `"איך אפשר לעזור?"` — most likely the same cause on
      the post-tool completion (500-token cap). Reply budget raised to 900; truncation and
      the empty-reply fallback are now logged so the validator path stays visible.
- [x] Voice: a dictated number arrives as words ("zero five two…"). Now normalised to
      digits (English and Hebrew) before extraction; the consent span check accepts the
      spoken form too.
- [x] Owner surface returned the greeting `"פה. מה צריך?"` when the brain failed. Now
      returns `OWNER_UNAVAILABLE`, unless the brain composed a specific failure note.
- [ ] Website: merge assaf-landingPage#28 (Mia inline in the hero, replacing the scripted
      demo) once capture is proven. Merging auto-deploys to Vercel.
- [ ] Product decision: is meeting booking (`app/domain/meetings`) retired, or waiting to
      be re-wired? It has no callers and ~100 tests.
- [x] `build_contacts_crm` (with `log_contact`/`resolved_spreadsheet_id`/`now_israel`) and
      the `scripts/calibrate_knowledge_floor.py` phantom-setting docstring — both fixed
      in C7b.
- [ ] The main checkout still has stale uncommitted `crm_v2.py` edits superseded by #55
      (unverified from this worktree).

## Live acceptance (Assaf)

Telegram: natural conversation, Hebrew voice and image, explicit memory, approve /
reject / expired-target flows. Website: exploration → pricing → volunteer contact →
brief arrives in Telegram, Contacts/Activity rows, Sheet edits and conflict handling.
