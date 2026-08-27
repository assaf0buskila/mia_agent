# BUILD_STATUS

**Last updated:** 2026-08-27  
**Line:** file-tree / wiring PR on `cursor/manage-file-tree-wiring-31a2` vs master `39d1ef3`.  
**This PR is not a live cut.** File cleanup is necessary and not sufficient for production. Remaining live cut is operator `.env` on the laptop + `scripts/deploy_ecs_revision.py` after a green CI SHA that includes PR #4. Do not copy `.env` onto Fargate. Do not merge or deploy from this agent.

## This slice

Inventory + wiring map + two real duplicate wires, no extra features.

- Map: `docs/WIRING.md` (who calls website vs telegram vs owner vs whatsapp vs graph vs store).
- Dead: almost every `app/` file has a caller or is a `pyproject` / CI / Docker entry point. Dated PR #4 slice notes moved to `docs/archive/OWNER_TELEGRAM_HANDOFF_SLICE.md`. Probe scripts and deploy examples stay (ops). Tree count `app+tests+scripts+deploy+docs`: 408 → 410 (map + archived slice). `app/` Python files unchanged at 172.
- Wired: live `gmail_read` now goes through `mail.read` + `execute_capability`. Owner turns build state via `app/channels/telegram.py` (was test-only).

Origin-bind fail-closed, website kill-switch HTTP 503, owner Telegram NOTE fallback to the sales model, and HANDOFF owner ping `ok:true` before transfer copy stay proven in tests.

## Not 100% production-ready

- Live ECS revision / service update of PR #4
- Composio tools returning real data on the live host
- Telegram voice note end-to-end on the live bot
- Website meeting CREATE against live Calendar OAuth
- WhatsApp stays a human inbox (ADR-024)
