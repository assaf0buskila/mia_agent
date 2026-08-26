# Gates: 1.1.1 Telegram owner loop

Scope: How Assaf's Telegram message becomes a tool call or a canned reply.

- [x] G1: Classifier vs agent split named (DETERMINISTIC_TASK_TYPES + NOTE path)
  EVIDENCE: owner_brain.py DETERMINISTIC_TASK_TYPES = APPROVAL PREFERENCE HUMAN_TAKEOVER* CONVERSATION_SCOPE LEAD_OUTREACH MEETING_DEBRIEF GMAIL_DRAFT OWNER_STATUS; inbound.py answer_owner after canned ack

- [x] G2: Owner registry tool count stated from code, not memory
  CHECK: uv run python -c "from app.tools.registries.owner_tools import tool_definitions; print(len(tool_definitions()))"
  EXPECT: 27
  EVIDENCE: 27
