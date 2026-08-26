# Plan: Close Mia from all angles

Depth: tree 3   Mode: orchestrated (research leaves; implementation waits for Assaf ADOPT on the safer reshape)
Budget note: one sitting of audit + Composio permission map + inventory. Code deletion only after the list is accepted.

## Contract

- Live code is this worktree (`claude/mia-product-feedback-0bfc90`). Master is stale.
- Do not inspect `.env`. Do not execute Composio writes (send, create event, sheet upsert, Meta write).
- Do not dump Composio catalogs into Mia's runtime. Cursor Composio MCP may be listed for Assaf; that list is operator research, not a production tool dump.
- Safety stays in code: `risk.py`, write flags, numeric Telegram allowlist, untrusted-text-is-data.
- Prompt may name risk examples and the pinned tool *names*. It is not the execute gate.
- Leaves do not share files. Research leaves write only their gates evidence (and the driver writes the canvas).
- Website sales graph is in scope for the inventory, out of scope for deletion this sitting.

Interfaces:
- Owner tools: names from `tool_definitions()` in `app/tools/registries/owner_tools.py`
- Composio pins: `PRELOADED_TOOLS` in `app/tools/registries/mia_preloaded_tools.py`
- Capabilities: `app/core/capabilities.py`

Data ownership:
- Leaf 1.1.1: app/graph, app/tools, app/domain/owner_* (read)
- Leaf 1.1.2: app/integrations, app/api, composio pins (read)
- Leaf 1.1.3: website sales graph, WhatsApp, Instagram, inbound (read)
- Leaf 1.1.4: safety, flags, tests that prove writes cannot fire from the model (read)
- Driver: GATES.md, PLAN.md, canvas

## Tree

- 1 Close Mia
  - 1.1 Research .......... gates/node-1.1.md
    - 1.1.1 Telegram owner loop
    - 1.1.2 Composio ports and pins
    - 1.1.3 Customer channels (website / WA / IG)
    - 1.1.4 Safety and dual-path mess

## Status log

- 2026-08-25 plan written, contract fixed; research leaves dispatched
- 2026-08-25 inventory canvas written; catalog dump refused; permission list is pinned reads + propose_write
- 2026-08-25 four research leaves verified: Telegram 27 tools / 9 deterministic; Composio 20 pins / 7 never-on-agent; website graph zero Composio (post-graph Calendar+Sheets); safety four write flags default false, four approval actions
