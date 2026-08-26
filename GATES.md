# Gates: Close Mia from all angles (research + simplify inventory)

Scope: Full-project audit of Mia (channels, Composio pins, owner agent, safety, dead code) and a remove / implement / adjust list. No Composio catalog dump into the runtime model. Prompt is extra guardrail, not the only one.

- [x] G1: Inventory names every Python module under app/ (count stated, not sampled)
  CHECK: powershell -NoProfile -Command "(Get-ChildItem -Path 'app' -Recurse -Filter '*.py' | Measure-Object).Count"
  EXPECT: 149
  EVIDENCE: 149

- [x] G2: No production path dumps a Composio catalog or session.tools() into the model
  CHECK: uv run python -c "from pathlib import Path; hits=[str(p) for p in Path('app').rglob('*.py') if any(x in p.read_text(encoding='utf-8', errors='replace') for x in ('session.tools','composio_langchain','LangchainProvider'))]; print('hits', hits or 'none')"
  EXPECT: hits none
  EVIDENCE: hits none

- [x] G3: Writes still go through risk.py / approvals, not prompt-only
  CHECK: uv run python -c "from app.core.risk import decide, RiskAction, RiskLevel; print(decide(RiskAction(name='x', risk=RiskLevel.R4_FINANCIAL_MARKETING)).value)"
  EXPECT: approval
  EVIDENCE: approval

- [x] G4: Composio connected toolkits listed from live MCP (names only, no secrets)
  EVIDENCE: COMPOSIO_MANAGE_CONNECTIONS list 2026-08-25: 8 active (gmail, googlecalendar, googlesheets, linkedin, instagram, google_analytics, google_search_console, github); metaads+whatsapp initiated empty on Cursor MCP. Production GET /health: composio=true, whatsapp_connected=true.

- [x] G5: Owner registry tool names counted and listed
  CHECK: uv run python -c "from app.tools.registries.owner_tools import tool_definitions; d=tool_definitions(); print(len(d))"
  EXPECT: 27
  EVIDENCE: 27

- [x] G6: Preloaded Composio pin count stated
  CHECK: uv run python -c "from app.tools.registries.mia_preloaded_tools import PRELOADED_TOOLS; print(len(PRELOADED_TOOLS))"
  EXPECT: 20
  EVIDENCE: 20

- [x] G7: Remove / implement / adjust list delivered as a canvas with measured counts
  EVIDENCE: canvases/mia-close-inventory.canvas.tsx · counts 149 app py, 27 owner tools, 20 pins, live max_steps=4, inbound.py 1497 lines

- [x] G8: Safer Composio permission shape decided in writing: pinned allowlist + code execute-gate, not prompt-only catalog
  EVIDENCE: canvas verdict+composio tabs: grant pinned reads; propose_write for drafts; never catalog/session.tools; R4 approval measured from decide()
