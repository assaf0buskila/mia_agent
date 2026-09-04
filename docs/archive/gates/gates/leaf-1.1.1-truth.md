# Gates: Config, health, docs, and probes

Scope: Correct known configuration and operational drift without changing product behavior.

- [x] G1: The example owner-agent step limit matches ADR-032 and the runtime default.
  CHECK: rg -n "MIA_OWNER_AGENT_MAX_STEPS=8" .env.example
  EXPECT: MIA_OWNER_AGENT_MAX_STEPS=8
  EVIDENCE: 2026-08-28 `rg -n "MIA_OWNER_AGENT_MAX_STEPS=8" .env.example` -> `130:MIA_OWNER_AGENT_MAX_STEPS=8`; `Settings.owner_agent_max_steps` is 8.
- [x] G2: Brain health reports missing model credentials consistently with readiness fallback logic.
  CHECK: uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_health.py
  EXPECT: /passed/
  EVIDENCE: 2026-08-28 `uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_health.py` -> 18 passed; fallback-ready owner/extraction health reports empty missing lists.
- [x] G3: Website probe scripts satisfy the current origin-binding contract and have regression coverage.
  CHECK: uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_scripts.py
  EXPECT: /passed/
  EVIDENCE: 2026-08-28 `uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_scripts.py` -> 2 passed; every probe POST carries fixed `Origin: https://www.assafweb.com`.
- [x] G4: Living operational documentation describes only current commands and capabilities.
  EVIDENCE: 2026-08-28 reviewed `docs/BUILD_STATUS.md`, `docs/RUNBOOK.md`, `docs/WIRING.md`, and `docs/BRAIN_ARCHITECTURE.md`: current two graphs/one owner loop, default 8-step limit, health fallback semantics, and fixed-origin probe commands are documented; no production swarm claimed.
