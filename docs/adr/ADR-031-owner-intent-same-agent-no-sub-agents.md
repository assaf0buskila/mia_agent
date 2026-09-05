# ADR-031 Owner intent: same agent, no sub-agents

- **Status:** accepted
- **Date:** 2026-08-24
- **Assaf:** chat — more phrasing understanding HE/EN; asked about transform/plan/execute/sub-agents

**Context**
mia:19 treated any unmatched text of three words or fewer as a greeting. `תבדקי את המייל` and `check my inbox` never reached the owner agent. The prompt also forced `search_memory` before live reads, so even longer paraphrases burned the four-step budget. Assaf asked for query transform, plan-and-execute, and sub-agents.

**Decision**
Keep **one** owner agent (`owner_agent_v2`). No sub-agents, no extra model hop. Greetings/acks/status pings stay an exact-set hello. Every other unmatched phrase, including three-word requests, stays `NOTE` so the loop can plan and call pinned tools. The prompt restates Hebrew/English paraphrases as a tool plan and does live reads (inbox, calendar, leads) before memory. Writes stay on the Python path.

**Consequences**
`תבדקי את המייל` / `can you look at my emails` reach `gmail_inbox`. Cost and safety stay one bounded loop. Shipped image **mia:20**, task **mia:22**. Rollback: image `mia:19` / task `mia:21`.

**Alternatives considered**
Sub-agent swarm or a separate rewrite model — rejected (AGENTS.md: subgraphs over swarms; extra latency and a second untrusted planner). Growing the keyword list for every Hebrew/English paraphrase — rejected; that is what failed. Dumping the Composio catalog — already rejected ADR-007 / ADR-030.
