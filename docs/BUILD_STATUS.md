# BUILD_STATUS

**Last updated:** 2026-08-28
**Line:** final local code/documentation alignment; this workspace is not a live cut.
**Production remains an operator action:** use a green CI SHA and `scripts/deploy_ecs_revision.py`; production configuration comes only from Secrets Manager `mia/prod`. Do not copy `.env` onto Fargate or deploy from this agent.

## This slice

The living wiring map and operations contracts describe the current two-graph product, with one bounded owner agent (ADR-031/032), no production swarm, origin-bound website writes, and ADR-042's explicitly authorized Sheets/KPI owner surfaces.

- Map: `docs/WIRING.md` (who calls website vs telegram vs owner vs whatsapp vs graph vs store).
- Probe scripts and deploy examples are retained as operator tooling. Website probes include the allowed browser Origin, so their POSTs exercise the same fail-closed boundary as the widget.
- Owner reads stay behind named capabilities and policy; owner turns build state via `app/channels/telegram.py`. Local focused evidence covers Telegram voice (3 webhook-to-reply tests plus 5 transcription-contract tests) and Sheets/GSC/GA4/LinkedIn (146 focused tests), without live provider calls.

Origin-bind fail-closed, website kill-switch HTTP 503, owner Telegram NOTE fallback to the sales model, HANDOFF owner ping `ok:true` before transfer copy, and the bounded Sheets/KPI policy path are local-test evidence only.

## Not 100% production-ready

- Current ECS image/task revision and service update
- Read-only live data from Sheets, GSC, GA4, and LinkedIn
- Telegram voice note end-to-end on the live bot
- Website meeting CREATE against live Calendar OAuth
- WhatsApp stays a human inbox (ADR-024)

No deploy, live API call, or production credential inspection was performed for this status.
