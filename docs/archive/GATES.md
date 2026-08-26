# Gates: Mia VNext rebuild (MIA_REBUILD.MD)

Scope: Rebuild Mia inside this repo through phases A–L: two graphs (Owner Telegram, Client website), shared voice/capability/policy/notifications, preserved website UX, website→Telegram finalization ping, one proven Composio READ capability, old path removed, canonical docs only.

- [x] G1: Phase A inventory exists as PLAN.md contract + rebuild-map classifying KEEP/REUSE/REPLACE/DELETE/ARCHIVE from code, not old docs
  CHECK: uv run python -c "from pathlib import Path; p=Path('PLAN.md'); t=p.read_text(encoding='utf-8'); assert 'KEEP' in t and 'REPLACE' in t and 'OwnerGraph' in t and 'ClientGraph' in t; print('PLAN_OK', p.stat().st_size)"
  EXPECT: PLAN_OK
  EVIDENCE: PLAN_OK 6060

- [x] G2: Canonical docs exist and AGENTS.md points only at PRODUCT/ARCHITECTURE/DECISIONS
  CHECK: uv run python -c "from pathlib import Path; files=['AGENTS.md','docs/PRODUCT.md','docs/ARCHITECTURE.md','docs/DECISIONS.md']; missing=[f for f in files if not Path(f).exists()]; print('CANONICAL_OK' if not missing else 'MISSING '+','.join(missing)); a=Path('AGENTS.md').read_text(encoding='utf-8'); assert 'docs/PRODUCT.md' in a and 'docs/ARCHITECTURE.md' in a"
  EXPECT: CANONICAL_OK
  EVIDENCE: CANONICAL_OK

- [x] G3: OwnerGraph and ClientGraph are distinct compiled graphs, not one shared ReAct loop
  CHECK: uv run python -c "from app.agents.owner.graph import compile_owner_graph; from app.agents.client.graph import compile_client_graph; o=compile_owner_graph(); c=compile_client_graph(); assert o is not c; print('TWO_GRAPHS_OK')"
  EXPECT: TWO_GRAPHS_OK
  EVIDENCE: TWO_GRAPHS_OK

- [x] G4: Telegram text reaches OwnerGraph and returns a response (rebuild §31 Owner text)
  CHECK: uv run pytest tests/unit/test_vnext_owner_text.py tests/unit/test_telegram_owner_graph.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ...                                                                      [100%]

- [x] G5: Telegram voice transcribes then enters the same OwnerGraph (rebuild §31 Owner voice)
  CHECK: uv run pytest tests/unit/test_vnext_owner_voice.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: .                                                                        [100%]

- [x] G6: Website text reaches ClientGraph (rebuild §31 Client text)
  CHECK: uv run pytest tests/unit/test_vnext_client_text.py tests/unit/test_website_client_graph.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ..                                                                       [100%]

- [x] G7: Website audio transcribes then enters the same ClientGraph (rebuild §31 Client voice)
  CHECK: uv run pytest tests/unit/test_vnext_client_voice.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: .                                                                        [100%]

- [x] G8: Website clients cannot execute owner capabilities even under prompt injection
  CHECK: uv run pytest tests/unit/test_vnext_isolation.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ..                                                                       [100%]

- [x] G9: Conversation A cannot read conversation B state
  CHECK: uv run pytest tests/unit/test_vnext_client_isolation.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: .                                                                        [100%]

- [x] G10: Allowed owner READ capability executes through capability → policy → Composio mock (no live accounts)
  CHECK: uv run pytest tests/unit/test_vnext_owner_capability.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: .                                                                        [100%]

- [x] G11: Unavailable capability is rejected in code, not prompt
  CHECK: uv run pytest tests/unit/test_vnext_tool_restrictions.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ...                                                                      [100%]

- [x] G12: Safe reads skip confirmation; sensitive writes require policy/approval
  CHECK: uv run pytest tests/unit/test_vnext_policy.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ....                                                                     [100%]

- [x] G13: Finished website conversation produces structured summary and one Telegram ping; retries are idempotent
  CHECK: uv run pytest tests/unit/test_vnext_finalization.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ..                                                                       [100%]

- [x] G14: Existing website session/message/voice API contracts still pass
  CHECK: uv run pytest tests/unit/test_ask_mia_widget.py tests/unit/test_website.py tests/unit/test_website_voice.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: .......................................................                  [100%]

- [x] G15: Focused suite green during strangler (excludes two pre-existing date-dependent calendar tests)
  CHECK: uv run pytest tests/unit/test_vnext_owner_text.py tests/unit/test_telegram_owner_graph.py tests/unit/test_vnext_owner_voice.py tests/unit/test_vnext_client_text.py tests/unit/test_website_client_graph.py tests/unit/test_vnext_client_voice.py tests/unit/test_vnext_isolation.py tests/unit/test_vnext_client_isolation.py tests/unit/test_vnext_owner_capability.py tests/unit/test_vnext_tool_restrictions.py tests/unit/test_vnext_policy.py tests/unit/test_vnext_finalization.py tests/unit/test_website.py tests/unit/test_website_voice.py tests/unit/test_ask_mia_widget.py tests/unit/test_health.py tests/unit/test_wiring.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ........................................................................ [ 73%] | ..........................                                               [100%]

- [x] G16: Ruff clean on app and tests
  CHECK: uv run ruff check app tests
  EXPECT: All checks passed
  EVIDENCE: [1;32mAll checks passed![0m

- [x] G17: Claude audit addressed: embeddings/extraction/owner-agent are not declared ALIVE when model ids are empty; knowledge ingest is documented as CLI unless scheduled
  CHECK: uv run python -c "from app.core.config import Settings; from app.core.capabilities import CapabilityId, WiringStatus, CAPABILITIES; s=Settings(); print('emb', s.embeddings_ready()); print('ext', s.extraction_ready()); print('agent', s.owner_agent_ready()); print('embeddings_status', next(c.status.value for c in CAPABILITIES if c.id is CapabilityId.EMBEDDINGS))"
  EXPECT: emb False
  EVIDENCE: agent False | embeddings_status wired

- [x] G18: Telegram HTTP adapter does not contain sales/owner reasoning
  CHECK: uv run python -c "from pathlib import Path; t=Path('app/api/telegram.py').read_text(encoding='utf-8'); banned=['select_next_action','OWNER_CAPABILITIES','assemble_owner_context']; hits=[b for b in banned if b in t]; print('ADAPTER_THIN' if not hits else 'FAT '+','.join(hits))"
  EXPECT: ADAPTER_THIN
  EVIDENCE: ADAPTER_THIN

- [x] G19: No auto-deploy performed (no new ECS task revision from this rebuild unless Assaf asked)
  EVIDENCE: No aws ecs / deploy_ecs_revision.py / docker push ran in this session. Rebuild stayed local.

- [x] G20: Phase K calendar.get_schedule and leads.get_recent execute through capability → policy (CalendarPort / Postgres mocks, no live accounts)
  CHECK: uv run pytest tests/unit/test_vnext_owner_capability.py tests/unit/test_vnext_isolation.py tests/unit/test_owner_calendar.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ....................................                                     [100%]

- [x] G21: Owner turn lives in app/api/owner.py; Telegram does not call process_inbound_texts
  CHECK: uv run python -c "from pathlib import Path; t=Path('app/api/telegram.py').read_text(encoding='utf-8'); o=Path('app/api/owner.py').read_text(encoding='utf-8'); i=Path('app/api/inbound.py').read_text(encoding='utf-8'); assert 'process_inbound_texts' not in t; assert 'async def process_owner_item' in o; assert 'process_owner_item' in i; print('OWNER_PEEL_OK')"
  EXPECT: OWNER_PEEL_OK
  EVIDENCE: OWNER_PEEL_OK

- [x] G22: Prospect inbound compiles ClientGraph; does not import build_graph
  CHECK: uv run pytest tests/unit/test_vnext_inbound_client.py tests/unit/test_health.py -q --tb=no --disable-warnings
  EXPECT: [100%]
  EVIDENCE: ....................                                                     [100%]

ABANDON: Duplicate owner tool registries (`owner_tools` still wraps capabilities for the LLM loop; `mia_preloaded_tools` still pins Composio slugs). Inner sales `build_graph` still runs inside ClientGraph. inbound.py remains the Meta webhook prospect path (ADR-024; not a third graph).
