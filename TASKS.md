# TASKS.md

Updated 2026-09-14. Production `/health` reports commit `4b80f31` (#60). Detail in `HANDOFF.md`.

## Campaign finish (plan: `docs/MIA_CAMPAIGN_FINISH_PLAN.md`)

C0 audit done 2026-09-14 at `4b80f31`: 2005 passed / 7 skipped, ruff clean, widget passed.
One chunk per stacked branch + PR, independent review each. No merge/deploy without Assaf.

Defaults taken where C0 left a fork (reverse any of them):
reports read CRM v2 tables; first-brief fix stays inside the capture transaction (no capture
reorder); Instagram writes stay excluded; LinkedIn publishing is not a campaign claim until
a live test; Calendar = own events without guests. Open: live Sheet audit (PII, needs
approval), meetings module retired or not.

- [ ] C1a — `_crm_upsert` returns None on valid input → owner turn crashes (`crm.py:77`).
      `execute_tool` must reject a non-ToolResult.
- [ ] C1b — `_looks_silent` greeting-prefix, `_looks_empty` ≤60 chars, usage lost on crash.
- [ ] C2a — LinkedIn vs `פוסט` hint order; owner prose Markdown shows literally.
- [ ] C2b — approval cards from the stored envelope (Gmail has no CC/BCC); callback
      `sent: True` after an edit failure.
- [ ] C5 — reschedule conflicts with itself; agenda `successful != True` reads as empty day.
- [ ] C3a — `business_context` latches on a greeting; summary is raw fragments; first brief
      misses same-turn `next_step`.
- [ ] C3b — v2 leads invisible to owner reports; lead card has no contact ref; nominal CRM
      reads import/write.
- [ ] C4 — live Gmail brief routing; send card fields; stuck `pending_review` has no exit.
- [ ] C6a — social planning with honest capability labels.
- [ ] C7b — remove proven-dead code, reconcile docs; then release readiness (Prompt 4).

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
- [ ] Small: `build_contacts_crm` is an orphan; `scripts/calibrate_knowledge_floor.py`
      tunes a setting that no longer exists; the main checkout still has stale uncommitted
      `crm_v2.py` edits superseded by #55.

## Live acceptance (Assaf)

Telegram: natural conversation, Hebrew voice and image, explicit memory, approve /
reject / expired-target flows. Website: exploration → pricing → volunteer contact →
brief arrives in Telegram, Contacts/Activity rows, Sheet edits and conflict handling.
