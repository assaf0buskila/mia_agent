# ADR-055 Telegram delivery, bounded reasoning, and voice context

- **Status:** accepted
- **Date:** 2026-09-06
- **Assaf:** ADOPT ("implement all of your recomand upgrades, test and deploy")

**Context**
The website could treat a reserved notification as delivered without a Telegram
acceptance. Owner briefs omitted useful conversation context. The owner model's
tool calls disabled reasoning, while a model request could consume the entire
outer timeout and continue using a database session after cancellation. Successful
voice input lost its provenance on the active owner path.

**Decision**
Keep one owner agent and the deterministic website surface. Use the existing
per-recipient delivery ledger to distinguish accepted delivery from pending or
ambiguous claims. Retry only explicitly rejected, retryable delivery attempts within
a bounded operation; ambiguous outcomes retain duplicate protection. Telegram
notification delivery must not depend on Google Sheets completing first.

Commit website request state before independent delivery or CRM transactions begin.
The installed FastAPI request-scoped yield dependency commits after background tasks;
relying on that cleanup while a background task updates the same row through another
connection can block the effects and leave the contact state uncommitted.
Each request reads committed website state; process-local cache entries are not
authoritative across workers. After network effects, merge only delivery or CRM
completion flags against freshly locked state so newer conversation details survive.

Send a bounded factual lead brief containing contact details, known business and
friction, relevant conversation context, questions requiring follow-up, and a next
step. Unknown information stays unknown; visitor text remains untrusted data.

Enable configurable, bounded reasoning for the OpenAI owner loop through the
Responses API while preserving other callers and providers. Keep tool authorization,
write approvals, numeric owner authorization, and the existing single-agent design.
Propagate a turn deadline, stop starting work when its budget expires, and keep
resources alive until work using them has stopped. Show typing progress while work
is active without turning it into extra conversational messages.

Persist successful voice transcript provenance on the active Telegram path and
provide an audio-origin marker to the owner agent. Clarify ambiguous critical
details rather than inventing names, numbers, or dates. Use Hebrew/English vocabulary
guidance for transcription; increased reasoning is not proof of improved speech
recognition. No voice output is introduced.

**Consequences**
Delivery truth, duplicate handling, timeout behavior, voice metadata, and model
tool continuation require behavioral tests. Production acceptance requires the
tested commit, live health, a real model request, and a controlled Telegram delivery
check. Physical-device transcription accuracy remains a separate acceptance lane.

**Alternatives considered**
Increasing the model step budget alone does not fix reasoning or timeouts. Blindly
resending ambiguous notifications risks duplicates. Adding another agent or a new
queue service would expand scope without being necessary for these corrections.
