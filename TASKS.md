# TASKS.md

Updated 2026-09-16. Production `/health` last reported commit `4b80f31` (#60) — nothing from the
campaign is deployed. Detail and next-session instructions in `HANDOFF.md` section 0.

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
      has a live takeover-state row) — fixed at `2311e5b`. Committed locally on
      `claude/mia-c7b-cleanup-docs`, not yet pushed/PR'd. See `HANDOFF.md` section 0 for the
      SHAs, evidence and what was deliberately left alone.
- [ ] Prompt 4 — release readiness, go/no-go → **stop for Assaf**
- [ ] Prompt 5 — approved deploy + phone acceptance (live LinkedIn post, email send, calendar
      event, website lead — each approved individually)

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

- [ ] **Website lead capture — root cause found and fixed, needs deploy.** The consent
      classifier ran on the Responses API with reasoning enabled and a 180-token
      `max_output_tokens`. Reasoning consumed the budget, no tool call was emitted, and
      the adapter reports that truncation as a normal response — so every verdict silently
      became "ambiguous" and the chain never fell back. Fixed on
      `claude/mia-consent-observability`: budget 600, truncation logged, regression tests
      that guard the requested budget. Deploy, then prove a lead lands in Telegram.
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
