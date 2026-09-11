# TASKS.md

Updated 2026-09-11. Production: `mia:61`, commit `fb6cc8d`. Detail in `HANDOFF.md`.

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
