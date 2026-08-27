# BUILD_STATUS

**Last updated:** 2026-08-26  
**Line:** live-bugfix PR on `cursor/fix-owner-telegram-and-handoff-8c1c` vs master.  
**This PR is not a live cut.** Remaining live cut is operator `.env` on the laptop + `scripts/deploy_ecs_revision.py` after green CI. Do not copy `.env` onto Fargate. Do not merge or deploy from this agent.

## Origin bind (already on master)

`POST /v1/website/sessions`, `.../messages`, `.../voice`, `.../handoff`, and `.../end` fail closed unless `Origin` is on `MIA_CORS_ORIGINS` (plus the public host). Rate-limited per IP and per session. `scripts/assert_origin_bind.py` and CI refuse an image if that code is missing.

## This slice

Two live defects, one PR.

**Owner Telegram NOTE fallback.** Greetings stayed deterministic. Real questions classified as NOTE, the agent ran, and Assaf got `הבדיקה לא עברה כרגע`. The owner-agent chain now advances on empty 200s and on 400-with-tools, appends the live sales models after the dedicated owner ids, and the failure line names a class (`שגיאת ספק`) without model ids or secrets. `/health` `owner_agent.ready` is true when the sales model can run even if `MIA_OWNER_AGENT_MODEL` is blank.

**Website fake transfer.** Ask Mia told a visitor the conversation was handed to Assaf. Assaf got nothing on Telegram. HANDOFF now notifies every allowlisted owner with the transcript, counts only Telegram `ok: true` as delivered, fails closed without transfer-claim copy, and releases a failed claim so the next turn can retry. `MIA_WHATSAPP_HANDOFF_SEND=false` still gates visitor WhatsApp send; it does not gate the owner ping. The widget shows the WhatsApp CTA on `handoff` as well as `offer_whatsapp`.

Origin-bind, kill-switch HTTP 503, and async owner paraphraser HTTP stay.

## Not this PR

- Live ECS revision / service update
- Playwright in CI (widget guarantees are static source tests)
- Phase L rewrite
- Flipping `MIA_WHATSAPP_HANDOFF_SEND` (visitor WhatsApp can stay gated)
