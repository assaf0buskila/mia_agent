# Owner latency investigation — 2026-09-10

## Observed failure

At 15:00 Israel time the owner asked, in Hebrew, what tools Mia has and not to use history. CloudWatch at 12:00:48.987 UTC records task=note, steps=2, tools=owner_system_audit, no failed tool, completion=provider_error. The screenshot shows the timeout reply. Production was on 43f1112, so this was not a stale deployment.

The active Telegram entry is app/surfaces/owner.py. It retrieved history, entered answer_owner, retrieved context, and allowed the model to select a complete integration audit. That audit performs multiple sequential provider reads. A tool inventory needs none of those calls.

## Applied scope

- Route explicit tool-inventory questions to a current registry-derived reply before CRM work, history, embeddings, model calls, or integration probes. Do not intercept execution, audit, compound-action, or missing-context requests.
- Honor explicit no-history requests in the active surface and answer_owner context assembly. Retrieval tools for current external data remain available.
- Skip post-reply learning for pure inventory questions, avoiding another model call that cannot add useful durable facts.
- Verify the real Telegram surface and call counts, rather than a legacy inbound helper alone.

## Agent latency terms and next priorities

1. Critical path / round trips: remove unnecessary model-tool-model cycles before changing model quality. Inventory target is zero model or provider round trips.
2. Context prefill: omit history/retrieval when explicitly excluded; keep normal context behavior for other questions. Stable prompt prefixes already benefit from provider prompt caching.
3. Fan-out: the broad system audit is sequential. A later bounded parallel read implementation must isolate sessions and return partial results honestly; sharing a SQLAlchemy session between tool threads is not acceptable.
4. Deadline budget: reserve time to deliver useful results instead of spending the whole turn on a last model rewrite. No increase to the timeout is claimed as a speed fix.
5. Perceived vs actual latency: typing indicators already exist. Measure server time separately from Telegram transport and physical-device receipt; indicators do not make completed work faster.
6. Model choice / reasoning: retain the configured model for now. Compare lower reasoning only with a fixed quality/effect-safety evaluation; do not trade correct actions for faster guesses.

Reference: https://developers.openai.com/api/docs/guides/latency-optimization (fewer requests, fewer generated tokens, parallelize independent work, avoid unnecessary LLM calls).

## Verification

Independent review passed with 18 focused tests. The full-suite run caught an obsolete expectation that a generic reply should attach any pending approval; its replacement seeds an approval and proves that only an explicit pending request receives its keyboard. Final full-suite and candidate-container timing are release gates. The runtime probe uses the active surface with real database rollback and a recording transport; it does not measure Telegram transport or user-device receipt.

## Assessment of the pasted agentic roadmap

The Level1-4 ladder is a heuristic, not a standard or an acceptance test. Useful findings are sequential execution, limited recent context, and lack of durable post-approval continuation. Several claims need correction:

- Owner reasoning is configured (medium by default) and sent through the Responses API. A public thought/scratchpad argument is unnecessary and adds output rather than proving better decisions.
- Requesting parallel tool calls does not make the Python executor concurrent. Only independent reads can run together safely; discovery, schema loading and execution depend on each other. Claimed 3-4x/50% savings require a benchmark.
- Not every write waits for a card: explicitly authorized bounded CRM/Sheets and local memory paths exist. Payload hashes bind approved parameters; identity/authorization/idempotency enforce the actual boundary.
- Due-scan includes owner reminders; website lead delivery also produces owner notifications. Zero proactivity is incorrect, though a daily cross-service briefing is not established by those paths.
- Website state is context-sensitive, not a fixed question list. It intentionally has no owner tools. Determinism is not a claim of zero prompt-injection risk.
- Tool errors already re-enter the agent loop. A bounded correction policy is an improvement, not creation of the first recovery mechanism. Ambiguous writes must not be retried automatically.
- A durable workflow checkpoint must precede approval continuation; never promote provider output to a trusted synthetic system instruction or treat one approved step as approval of all future sends.

Suggested order: remove unnecessary calls; instrument stage latency; add isolated parallel reads; bounded schema recovery; durable task/approval continuation; then measured rolling summaries. Preserve the website/owner capability boundary.
