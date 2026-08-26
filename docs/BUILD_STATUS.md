# BUILD_STATUS

**Last updated:** 2026-08-26  
**Line:** `claude/mia-vnext-rebuild` rebased/merged onto master (origin-bind + rate-limit from PR #1 is in SHA `d2c4e387` and this follow-up).  
**This PR is not a live cut.** Remaining live cut is operator `.env` on the laptop + `scripts/deploy_ecs_revision.py` after green CI. Do not copy `.env` onto Fargate.

## Origin bind (already on the branch)

`POST /v1/website/sessions`, `.../messages`, `.../voice`, `.../handoff`, and `.../end` fail closed unless `Origin` is on `MIA_CORS_ORIGINS` (plus the public host). Rate-limited per IP and per session. `scripts/assert_origin_bind.py` and CI refuse an image if that code is missing.

## This slice

Kill switch stops website **chat** the same way it stops voice (HTTP 503, no graph). Owner Telegram paraphraser HTTP is async. Due-scan can send one unprompted owner Telegram due reminder per local day. Approval keyboards stay attached on pending-approval reads and callbacks apply the decision. Client isolation tests two real website sessions and a capability deny. ALB 5xx example alarm includes SNS `AlarmActions` (`MIA_ALB_5XX_SNS_TOPIC_ARN`). CD-from-green-CI exists as `workflow_dispatch` after test+image; it does not update ECS.

## Cleanup (this PR)

Removed leftover workaround `docs/brain.env.example` (names already live in `.env.example`). Moved dated `docs/DEPLOY_SLICE_REPORT.md` into `docs/archive/`. Removed Cursor AWS starter rule `.cursor/rules/aws-starter-rules.mdc` (not product). Origin-bind, kill-switch 503, async owner HTTP, due reminder, two-tenant isolation, and `scripts/assert_origin_bind.py` stay.

## Not this PR

- Live ECS revision / service update
- Playwright in CI (widget guarantees are static source tests)
- Phase L rewrite
