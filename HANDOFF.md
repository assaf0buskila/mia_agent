# Mia v2 release handoff

Updated 2026-09-11. Implementation, cleanup and local acceptance complete.
Read AGENTS.md, MIA_V2.md and TASKS.md for current rules, product and evidence.

All 1,991 tests passed with zero failures/errors/skips. Ruff, widget behavior,
browser appearance and actual API integration, PostgreSQL migrations/repeat,
nonroot container startup and fresh independent HEAVY reviews passed.
All reproduced production and test-port findings are closed.

The user explicitly requested deployment now. No more agent review is needed.
Continue exact-SHA CI, build/scan immutable ECR image, pause the unrevisioned
schedulers before registering, migrate, deploy and verify. Reconcile stays disabled;
due scan resumes pinned to the accepted revision. User live acceptance follows.

Production rollback before rollout: mia:57, old image mia:31, digest
sha256:78f25971a5a891f05ef745d5b0055cb8d723cc2fc14a5561cbcfad153894ef54,
commit 58b40b341c992508349e23cdc55d3cc5f456c41b. Account 535252061205, eu-north-1.
Credentials work outside sandbox; do not read .env or expose secrets.

Evidence .cache/mia-v2-release/. Initial 93-path baseline .cache/mia-v2-baseline/
is immutable. Cleanup 179 deletions, of which 66 preexisting and 113 implementation.
Use MIA_ENV=test, UV_CACHE_DIR=.cache/uv, unique pytest basetemp. Local isolated PG
runs at localhost:51403/mia_v2. Never alter the old excluded temporary directory ACL.
