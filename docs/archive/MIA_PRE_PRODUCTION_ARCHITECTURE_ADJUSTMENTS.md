# MIA PRE-PRODUCTION ARCHITECTURE ADJUSTMENTS
## Cursor Implementation Control File

**Document status:** Required pre-production adjustment plan  
**Version:** 1.0  
**Review date:** 2026-08-21  
**Project:** Mia, AssafWeb AI Growth and Sales Operator  
**Primary user:** Assaf  
**Execution environment:** Cursor agent  
**Related documents:**

1. `Mia_AI_Growth_Sales_Operator_PRD_Build_Bible_v1.1.docx`
2. `MIA_FINAL_MILE_PLAYBOOK.md`
3. Repository `AGENTS.md`
4. Existing architecture decisions, tests and implementation

---

# 0. Purpose

This file tells Cursor how to complete the final architecture adjustments before Mia is treated as a finished production project.

This is **not a request to rebuild Mia**.

Most of the project already exists. Cursor must:

1. inspect what is already implemented
2. compare it with this adjustment plan
3. produce a factual gap report
4. preserve working code
5. implement only missing or weak areas
6. test every change
7. review every change independently
8. keep all high-risk behavior behind feature flags
9. stop before each major architecture migration and discuss it with Assaf
10. complete a final production acceptance run

The target is:

> Managed channels and infrastructure around a custom, evaluated LangGraph sales brain.

---

# 1. Cursor operating rule

Before changing code, Cursor must respond with:

```text
WHAT I UNDERSTOOD

The goal:
...

What already appears complete:
...

What remains:
...

What I recommend changing:
...

What I will not change:
...

First implementation unit:
...

Risks:
...
```

For a material architecture change, Cursor must use this discussion format:

```text
CURRENT BIBLE DIRECTION
...

NEW ALTERNATIVE
...

WHY IT MAY BE BETTER
...

QUALITY AND PERFORMANCE
...

SECURITY AND PRIVACY
...

RELIABILITY
...

COST
...

VENDOR LOCK-IN
...

FILES AFFECTED
...

TEST PLAN
...

RECOMMENDATION
KEEP / ADOPT / TEST BOTH / DEFER
```

Do not materially change architecture until Assaf decides.

---

# 2. Architecture verdict

The current Mia direction is correct for the intended product.

Do not replace the custom brain with:

- Base44
- Make AI Agents
- n8n Agents
- ManyChat AI
- ElevenLabs Agents
- a generic multi-agent framework
- a single giant model prompt

Those platforms may be useful at the edges, but Mia’s proprietary value remains custom.

Keep custom:

- workflow-first sales discovery
- sales state
- pain-depth state
- qualification logic
- next-best-question policy
- next-best-action policy
- lead identity
- cross-channel memory
- owner instructions
- attribution
- model routing
- approval logic
- evaluation datasets
- Graph Lab
- human writing policy

Buy or configure where appropriate:

- channel triggers
- OAuth
- commodity integrations
- transcription
- telephony if added later
- cloud infrastructure
- email and calendar connectivity
- scraping and browser infrastructure

---

# 3. Final target architecture

```text
CHANNELS

Instagram
  -> ManyChat trigger and conversation routing

WhatsApp
  -> One selected WhatsApp channel adapter

Website
  -> AssafWeb event and chat integration

Gmail and Calendar
  -> Scoped Google integration

Owner voice notes
  -> Transcription provider
  -> Text-only Mia response


INGRESS

CloudFront where useful
  -> AWS WAF
  -> API Gateway
  -> Lambda webhook receiver
  -> raw event persistence
  -> idempotency
  -> SQS


AGENT RUNTIME

Selected production runtime
  -> LangGraph
  -> customer sales subgraph
  -> owner operations subgraph
  -> calendar subgraph
  -> campaign analysis subgraph
  -> approval subgraph
  -> follow-up subgraph


DATA

Postgres or Supabase Postgres
  -> identities
  -> leads
  -> conversations
  -> messages
  -> sales state
  -> tasks
  -> approvals
  -> owner instructions
  -> campaign attribution
  -> model runs
  -> tool runs
  -> audit records


TOOLS

Direct controlled adapters
  -> database
  -> outbound messaging
  -> calendar writes
  -> Meta campaign writes
  -> identity and permissions
  -> website events

Composio preloaded tools
  -> Gmail
  -> Google Sheets
  -> Drive
  -> LinkedIn
  -> approved secondary integrations

Dynamic Composio discovery
  -> owner-only open-ended tasks
  -> never the default customer hot path


OBSERVABILITY

CloudWatch
  + agent tracing
  + LangSmith or Langfuse
  + structured audit logs
  + local Graph Lab
```

---

# 4. Adjustment A: One owner for every capability

The project must contain a written ownership matrix.

Create or update:

`docs/CAPABILITY_OWNERSHIP.md`

Required format:

| Capability | System of record | Execution owner | Backup or fallback | Must not own |
|---|---|---|---|---|
| Instagram entry trigger | Meta or ManyChat | ManyChat | manual inbox | LangGraph trigger polling |
| Instagram conversation routing | Meta routing | ManyChat | human takeover | two apps replying together |
| Sales reasoning | Mia state | LangGraph | safe handoff | ManyChat or Make |
| Lead state | Postgres | domain service | restore from event log | Google Sheets |
| Calendar truth | Google Calendar | calendar adapter | draft slots only | model memory |
| Campaign truth | Meta | Meta read adapter | cached marked stale | Google Sheet |
| Campaign write | Meta | controlled direct adapter | manual Ads Manager | free-form tool discovery |
| Tool authentication | selected adapter | Composio or direct | provider reconnect | multiple owners |
| Owner instructions | Postgres | instruction service | disabled instruction | system prompt only |
| Business report view | Postgres-derived | Google Sheets sync | dashboard | Sheet as source of truth |
| Runtime | chosen AWS runtime | one deployment owner | documented rollback | duplicate workers |
| Evals | versioned dataset | Graph Lab and CI | manual review | production self-editing |

## Acceptance criteria

- Every major capability has one execution owner.
- No important state is split across several uncontrolled platforms.
- No channel can produce duplicate automated replies.
- Google Sheets is not a source of truth.
- ManyChat does not own sales reasoning.
- Composio does not own business state.
- LangGraph does not store provider SDK objects in state.

---

# 5. Adjustment B: Instagram routing and ManyChat

ManyChat may be used for:

- Instagram comment triggers
- story replies
- story mentions
- Instagram ad entry
- ref URL entry
- initial routing
- live human takeover
- simple deterministic opt-in steps

Mia owns:

- lead memory
- sales reasoning
- business discovery
- qualification
- research
- recommendations
- meeting progression
- next-best action
- final customer response policy

## Required implementation

1. Confirm Instagram professional account requirements.
2. Confirm Meta conversation routing configuration.
3. Set one default routing owner.
4. Disable competing applications from taking control unexpectedly.
5. Store ManyChat contact and conversation IDs in the identity table.
6. Map every ManyChat event to a stable internal event envelope.
7. Add idempotency key based on provider event or message ID.
8. Add human takeover state.
9. Stop Mia outbound automation when human takeover is active.
10. Add contract tests using sanitized real payloads.

## Required tests

- story reply enters Mia once
- comment trigger enters Mia once
- ad trigger preserves campaign and ad identifiers where available
- duplicate event does not produce duplicate response
- human takeover stops automation
- automation resumes only through explicit state change
- second connected app cannot produce a duplicate automated response
- lead continues with the same internal identity

---

# 6. Adjustment C: Runtime decision

Do not assume Lambda is the best long-running agent runtime.

Lambda remains preferred for:

- webhook receipt
- signature verification
- fast acknowledgement
- lightweight normalization
- scheduled triggers
- queue producers
- short deterministic tasks

Benchmark the long-running LangGraph worker using at least two viable candidates:

## Candidate 1: Current implementation

Could be:

- Lambda
- ECS or Fargate
- App Runner
- another existing runtime

## Candidate 2: Amazon Bedrock AgentCore Runtime

AgentCore currently supports custom agents and frameworks including LangGraph, uses versioned runtime deployments, supports multiple model providers and can expose MCP or A2A interfaces.

Do not migrate only because AgentCore is newer.

## Benchmark workload

Use the same frozen test scenario:

1. receive a qualified lead message
2. load lead and sales state
3. perform one database read
4. call the default conversation model
5. perform one mock or staging tool read
6. persist result
7. return response

Also test:

- owner daily brief
- calendar availability check
- campaign analysis request
- one asynchronous research request

## Measure

- cold-start latency
- warm latency
- P50
- P95
- error rate
- timeout behavior
- deployment complexity
- rollback quality
- session isolation
- trace quality
- secret handling
- networking complexity
- estimated monthly cost
- operational burden
- vendor lock-in

## Decision gate

Create:

`docs/adr/ADR_RUNTIME_SELECTION.md`

Allowed outcomes:

- keep current runtime
- migrate to AgentCore
- use Lambda plus AgentCore
- use Lambda plus container runtime
- defer migration until volume justifies it

Do not change runtime before the benchmark and ADR are reviewed.

---

# 7. Adjustment D: Ingress, queue and event envelope

Every external event must be normalized into one internal contract.

Suggested model:

```python
class InboundEvent(BaseModel):
    event_id: str
    provider: Provider
    channel: Channel
    event_type: str
    business_id: UUID
    external_identity_id: str | None
    conversation_external_id: str | None
    occurred_at: datetime
    received_at: datetime
    correlation_id: str
    idempotency_key: str
    payload_version: str
    raw_event_id: UUID
```

## Required behavior

```text
Receive event
  -> verify provider
  -> store sanitized raw event
  -> derive idempotency key
  -> reject or reuse duplicate result
  -> enqueue
  -> acknowledge quickly
  -> process asynchronously
```

## Rules

- Raw provider payloads must not flow directly through the entire domain.
- External payload parsing belongs in provider adapters.
- Domain services consume normalized internal models.
- Queue consumers must be safe to retry.
- Every event gets a correlation ID.
- Every external write gets an operation ID.
- Every failed event has a visible final state.
- Dead-letter events must be inspectable and replayable through an authorized tool.

---

# 8. Adjustment E: Idempotency

Idempotency is required for every consequential action.

Use stable keys for:

- webhook event
- outbound message
- calendar event creation
- reschedule
- cancellation
- task creation
- Google Sheet row or update
- CRM state transition
- Meta campaign change
- email send
- owner approval execution
- follow-up scheduling

AWS Lambda Powertools offers an idempotency utility that can persist request state and safely return a previous result on repeated calls. It may be used where appropriate.

The domain must not depend on the Powertools decorator alone.

Create an application-level interface:

```python
class IdempotencyStore(Protocol):
    async def begin(self, key: str, ttl_seconds: int) -> BeginResult: ...
    async def complete(self, key: str, result: dict) -> None: ...
    async def fail(self, key: str, error_code: str) -> None: ...
    async def get(self, key: str) -> IdempotencyRecord | None: ...
```

## Required concurrency behavior

Handle:

- duplicate arrival after completion
- duplicate arrival while first request is running
- first worker timeout
- write succeeds but response is lost
- queue redelivery
- provider webhook retry
- user repeats the same owner command

## Acceptance criteria

No test can produce:

- duplicate customer message
- duplicate calendar event
- duplicate campaign write
- duplicate task
- duplicate lead
- duplicate email

---

# 9. Adjustment F: Critical tools versus convenient tools

Do not expose all Composio tools to every graph.

## Tier 1: Direct controlled adapters

Use direct, narrowly typed adapters for:

- database writes
- outbound customer messaging
- owner authorization
- calendar booking writes
- Meta campaign writes
- website event ingestion
- approval execution
- identity merge or split
- destructive actions

These adapters need:

- exact typed input
- allowlist
- timeout
- retry policy
- audit
- idempotency
- read-before-write when needed
- verify-after-write
- permission check before execution

## Tier 2: Composio preloaded tools

Use a small known set for recurring tasks such as:

- Gmail search and draft
- Google Sheets update
- Google Drive lookup
- approved LinkedIn operations
- secondary CRM operations

Composio currently recommends keeping preloaded tools small, generally fewer than 20, when the needed tool set is known.

Create a versioned registry:

`app/tools/registries/mia_preloaded_tools.py`

The registry must define:

- logical capability
- provider toolkit
- exact action
- read or write
- allowed actor classes
- approval requirement
- timeout
- result validator
- fallback
- version

## Tier 3: Dynamic discovery

Allowed only for:

- authenticated owner tasks
- open-ended research
- low-risk app discovery
- non-customer hot-path operations

Not allowed for:

- automatic customer message send
- campaign write
- calendar write
- identity action
- deletion
- permission changes
- financial action

## Acceptance criteria

- Customer graph receives only the tools it needs.
- Tool schemas do not create unnecessary context bloat.
- A model cannot discover a dangerous write tool during a normal lead conversation.
- Every write tool is permission checked outside the model.

---

# 10. Adjustment G: Adapter boundaries

Provider objects must not leak into domain or LangGraph state.

Bad:

```python
state["gmail_message"] = composio_response
state["calendar_client"] = google_sdk_client
```

Good:

```python
class EmailSummary(BaseModel):
    message_id: str
    sender: str
    subject: str
    received_at: datetime
    summary: str
```

Create interfaces such as:

```python
class CalendarGateway(Protocol):
    async def get_availability(self, request: AvailabilityRequest) -> AvailabilityResult: ...
    async def create_booking(self, request: CreateBookingRequest) -> BookingResult: ...
    async def cancel_booking(self, request: CancelBookingRequest) -> BookingResult: ...

class MessagingGateway(Protocol):
    async def send(self, request: OutboundMessageRequest) -> MessageDeliveryResult: ...

class CampaignGateway(Protocol):
    async def read_metrics(self, request: CampaignMetricsRequest) -> CampaignMetricsResult: ...
    async def apply_change(self, request: ApprovedCampaignChange) -> CampaignChangeResult: ...
```

## Required result validation

Every provider result must be classified as:

- success
- partial
- stale
- retryable failure
- permanent failure
- unauthorized
- rate limited
- malformed

The model must never treat an unvalidated provider response as truth.

---

# 11. Adjustment H: Visual automation platforms

Do not add Make or n8n to Mia’s live sales reasoning path unless a specific measured benefit justifies it.

They may be used as optional sidecars for:

- daily scheduled report delivery
- internal notifications
- simple file movement
- non-critical CRM sync
- manual operations dashboard
- one-off client integration
- temporary proof of concept

They must not own:

- sales state
- next-best-question
- qualification
- identity
- critical outbound policy
- approvals
- campaign write policy
- core memory
- source of truth

If introduced, document:

- exact workflow owner
- source of truth
- retry owner
- error visibility
- cost
- exit or migration path

Do not use both Make and n8n in Mia.

---

# 12. Adjustment I: Latency and performance budget

Create:

`docs/PERFORMANCE_BUDGET.md`

Initial targets:

| Workflow | Target |
|---|---:|
| Webhook acknowledgement | under 500 ms |
| Simple message route | P95 under 4 seconds |
| Message with one business lookup | P95 under 6 seconds |
| Calendar slot proposal | P95 under 7 seconds |
| Owner daily summary from prepared metrics | P95 under 8 seconds |
| Complex campaign analysis | asynchronous if over 10 seconds |
| Deep research | asynchronous with task state |
| External browser task | asynchronous |

## Hot-path rules

Normal customer sales path must avoid:

- broad dynamic tool search
- browser automation
- several research providers
- multi-agent debates
- unnecessary verifier model
- several sequential LLM calls
- large full-history prompts
- loading all business knowledge
- loading all tools

Use:

- compact state summary
- targeted retrieval
- known tool registry
- deterministic scoring
- model router
- cached stable business facts with freshness labels
- asynchronous enrichment when possible

## Instrumentation

Every graph run must capture:

- total latency
- node latency
- queue delay
- model latency
- tool latency
- database latency
- tokens
- cost
- retries
- cache hit
- selected model
- selected tool

---

# 13. Adjustment J: Model router

Do not hard-code one model for all tasks.

Create typed task classes:

- route
- extract
- transcribe
- normal sales conversation
- sales reframe
- objection handling
- campaign interpretation
- deep research
- summarization
- message humanity review
- safety verification

## Routing inputs

- task type
- complexity
- risk
- latency budget
- context size
- required tools
- preferred language
- cost limit
- fallback status

## Required benchmark

Build a frozen Mia benchmark with Hebrew and English cases.

At minimum:

- 20 routing cases
- 30 extraction cases
- 50 sales conversation turns
- 20 objection cases
- 20 calendar task cases
- 20 campaign interpretation cases
- 20 owner voice-note transcripts
- 20 humanity-linter cases
- 20 safety cases

Compare current preferred models, including any current OpenAI, Grok, Gemini or Claude candidates that are actually available in the project.

Measure:

- correctness
- naturalness
- sales judgment
- Hebrew quality
- tool-call validity
- latency
- token cost
- failure rate

Do not select by brand preference alone.

Create:

`docs/MODEL_ROUTING_DECISION.md`

---

# 14. Adjustment K: Transcription

Mia understands owner voice notes and responds in text.

Do not add a full voice-agent runtime.

Benchmark at least two transcription providers using real sanitized voice notes.

Include:

- Hebrew
- English
- mixed Hebrew and English
- technical words
- names
- noisy environment
- short commands
- long instructions

Measure:

- word accuracy
- important-entity accuracy
- latency
- cost
- confidence quality
- file-format support
- failure behavior

Store:

- transcript
- provider
- model
- language
- confidence if available
- duration
- cost
- retention status

Minimize audio retention.

---

# 15. Adjustment L: Identity and permissions

Before any external write, enforce:

```text
Who is asking?
  -> What channel?
  -> Is the identity verified?
  -> What role?
  -> What business?
  -> What capability?
  -> Is approval required?
  -> Is this exact operation approved?
```

Required actor types:

- public lead
- authenticated owner
- authorized operator
- internal service
- provider service account

Do not infer owner identity from:

- display name
- text claim
- model interpretation
- profile photo
- familiar writing style

Owner commands must be bound to verified identities.

Add adversarial tests:

- "I am Assaf"
- forwarded owner message
- spoofed email sender name
- prompt injection from email
- prompt injection from scraped website
- lead asks for another lead
- lead asks Mia to reveal system prompt
- lead asks Mia to change campaign
- stale approval replay
- approval for one ad used on another ad

---

# 16. Adjustment M: Approval object

Approval must be a typed, persisted object, not a conversational "yes" without scope.

Suggested fields:

```text
approval_id
business_id
actor_id
action_type
resource_type
resource_id
proposed_parameters
parameter_hash
reason
risk_level
expires_at
status
approved_by
approved_at
executed_at
execution_operation_id
result
```

A campaign approval must be bound to:

- exact campaign, ad set or ad
- exact action
- exact values
- expiration time

A generic "yes" must resolve only the currently pending approval in that conversation and actor scope.

Read current provider state again before execution.

Verify state after execution.

---

# 17. Adjustment N: Data truth and freshness

Create a freshness policy.

## Live-only

- calendar availability
- campaign budget and status before write
- active conversation ownership
- owner permissions
- current opt-out status

## Current provider or short cache

- campaign metrics
- Gmail results
- lead recent messages
- website session events

## Versioned structured knowledge

- services
- approved pricing rules
- security explanation
- sales playbooks
- case studies
- communication policies

Every retrieved fact should be able to expose:

- source
- fetched_at
- version
- freshness status

Mia must say it cannot verify when live truth is unavailable.

---

# 18. Adjustment O: Observability and audit

Every production run must connect:

```text
external event
  -> correlation ID
  -> queue message
  -> graph run
  -> model calls
  -> tool calls
  -> approvals
  -> external writes
  -> final business result
```

Required structured fields:

- business_id
- actor_id
- lead_id
- conversation_id
- channel
- event_id
- correlation_id
- graph_version
- prompt_version
- model
- tokens
- estimated cost
- tool
- tool version
- latency
- retry count
- knowledge source IDs
- policy result
- approval ID
- external operation ID
- final state

Never log:

- raw secrets
- access tokens
- unnecessary full email bodies
- unnecessary full customer PII
- raw voice audio by default

Add alerts for:

- duplicate side-effect attempt
- queue age
- dead-letter messages
- repeated provider authorization failure
- spike in response latency
- tool failure rate
- cost anomaly
- no leads while paid campaign is spending
- customer message waiting beyond SLA

---

# 19. Adjustment P: Graph structure

Keep one orchestrator with specialized subgraphs.

Do not create many autonomous agents only for visual complexity.

Recommended:

```text
Main orchestrator
  -> identify actor and channel
  -> load state
  -> classify task
  -> route

Customer sales subgraph
Owner operations subgraph
Calendar subgraph
Campaign analysis subgraph
Follow-up subgraph
Approval subgraph
Research worker subgraph
```

## Graph rules

- State is typed.
- Nodes have one responsibility.
- Deterministic business rules remain code.
- Tools are injected by capability.
- Long research is asynchronous.
- Human approval uses persisted interrupt or approval state.
- Graph version is recorded on every run.
- Production graph cannot rewrite itself.
- Graph Lab changes require offline evaluation and human promotion.

---

# 20. Adjustment Q: Sales quality must not regress

Architecture hardening must not make Mia sound robotic or slow.

Preserve:

- workflow-first discovery
- one meaningful question at a time
- natural Hebrew
- natural English
- no corporate AI language
- no decorative slash
- no backslash in normal prose
- no em dash
- no fake enthusiasm
- no early solution push
- no unsupported ROI
- no forced meeting for poor-fit leads
- clear human handoff

Run `MIA_FINAL_MILE_PLAYBOOK.md` humanity tests after any prompt, model or graph change.

---

# 21. Adjustment R: Feature flags

No high-risk capability goes live directly.

Required feature flags:

- `MIA_AUTO_REPLY_INSTAGRAM`
- `MIA_AUTO_REPLY_WHATSAPP`
- `MIA_AUTO_FOLLOWUP`
- `MIA_CALENDAR_WRITE`
- `MIA_GMAIL_READ`
- `MIA_GMAIL_DRAFT`
- `MIA_GMAIL_SEND`
- `MIA_META_WRITE`
- `MIA_LINKEDIN_WRITE`
- `MIA_DYNAMIC_TOOL_DISCOVERY`
- `MIA_OWNER_VOICE_INPUT`
- `MIA_BROWSER_AUTOMATION`

Each flag must support:

- disabled
- staging only
- allowlisted users
- percentage or controlled rollout where useful
- immediate kill switch

---

# 22. Cursor implementation phases

## Phase 0: Repository gap audit

Do not code.

Inspect:

- repository tree
- current branch
- uncommitted work
- current tests
- deployment files
- graph files
- adapter files
- tool registry
- runtime
- channel integrations
- feature flags
- audit logs
- ADRs

Produce:

`docs/PRE_PRODUCTION_GAP_REPORT.md`

For every adjustment in this file, label:

- complete
- partially complete
- missing
- unclear
- blocked by external setup

Include exact file evidence.

## Phase 1: Documentation and ownership

Implement:

- capability ownership matrix
- runtime decision plan
- performance budget
- model benchmark plan
- external integration readiness checklist

No runtime migration yet.

## Phase 2: Security-critical foundations

Close:

- identity
- roles
- permission enforcement
- approval binding
- idempotency
- event envelope
- human takeover
- kill switch

Do not continue to campaign writes until this phase passes.

## Phase 3: Tool boundary

Close:

- direct critical adapters
- provider result validation
- Composio preloaded registry
- dynamic discovery restrictions
- no vendor objects in state

## Phase 4: Channel reliability

Close:

- ManyChat conversation routing
- WhatsApp ownership
- website event identity
- Gmail and Calendar scopes
- LinkedIn limitations and setup
- duplicate webhook tests

## Phase 5: Runtime benchmark

Implement benchmark harness.

Compare current runtime with AgentCore or selected alternative.

Write ADR.

Migrate only after approval.

## Phase 6: Performance and model routing

Implement:

- latency instrumentation
- task classes
- model benchmark
- routing decision
- context minimization
- asynchronous research path

## Phase 7: Production write operations

Enable progressively:

1. Calendar write
2. low-risk outbound follow-up
3. Gmail draft
4. controlled Gmail send if approved
5. LinkedIn write if intentionally enabled
6. Meta campaign write last

Every write requires E2E, idempotency and rollback testing.

## Phase 8: Full acceptance

Run all production stories.

Do not declare complete until release gates pass.

---

# 23. Mandatory end-to-end tests

## Scenario 1: Instagram lead

```text
Instagram ad or content
  -> ManyChat trigger
  -> internal identity
  -> Mia sales conversation
  -> lead state
  -> meeting
  -> Calendar
  -> confirmation
  -> Google Sheet
  -> owner notification
  -> full trace
```

## Scenario 2: WhatsApp lead

```text
WhatsApp message
  -> verified webhook
  -> dedupe
  -> sales state
  -> follow-up
  -> human takeover
  -> no duplicate send
```

## Scenario 3: Owner voice task

```text
Assaf voice note
  -> identity verification
  -> transcription
  -> understanding check
  -> task creation
  -> text response
  -> audit
```

## Scenario 4: Calendar race

```text
slot offered
  -> another event takes slot
  -> re-check detects conflict
  -> alternative offered
  -> no double booking
```

## Scenario 5: Campaign change

```text
campaign analysis
  -> exact recommendation
  -> approval object
  -> read current state
  -> one write
  -> verify
  -> audit
```

## Scenario 6: Duplicate provider event

```text
same event delivered twice
  -> one business result
  -> one message
  -> one task
```

## Scenario 7: Provider failure

```text
tool timeout
  -> bounded retry
  -> safe fallback
  -> no invented result
  -> visible failure
```

## Scenario 8: Prompt injection

```text
malicious email or webpage
  -> treated as untrusted content
  -> cannot alter system permission
  -> no secret or private data exposure
```

---

# 24. Release gates

## Gate A: Security

- [ ] owner identity cannot be spoofed
- [ ] cross-lead data access blocked
- [ ] tool permissions checked outside model
- [ ] approval bound to exact operation
- [ ] secrets excluded from logs
- [ ] prompt injection tests pass
- [ ] channel signatures verified

## Gate B: Reliability

- [ ] duplicate events safe
- [ ] duplicate writes safe
- [ ] bounded retries
- [ ] dead-letter path
- [ ] provider timeout handled
- [ ] human takeover works
- [ ] kill switch works

## Gate C: Performance

- [ ] latency targets measured
- [ ] normal sales path avoids broad tool discovery
- [ ] asynchronous research
- [ ] graph context compact
- [ ] cost tracked by task and model

## Gate D: Sales quality

- [ ] Hebrew test set passes
- [ ] English test set passes
- [ ] humanity linter passes
- [ ] one-question policy passes
- [ ] no unsupported claims
- [ ] correct handoff behavior
- [ ] no forced meetings for poor fit

## Gate E: External writes

- [ ] Calendar staging E2E
- [ ] outbound staging E2E
- [ ] LinkedIn write intentionally approved or disabled
- [ ] Meta write exact and verified
- [ ] Google Sheet sync idempotent

## Gate F: Operations

- [ ] dashboards or alerts exist
- [ ] runbooks exist
- [ ] rollback documented
- [ ] external setup documented
- [ ] feature flags documented
- [ ] production owner knows kill switch

---

# 25. Definition of perfect enough

Do not claim the product is perfect.

Mia is ready for the final project when:

1. the main lead journey works
2. owner operations work
3. the system is safe to retry
4. one system owns every capability
5. high-risk actions require exact approval
6. the sales brain remains natural
7. provider failures are visible and safe
8. latency and cost are measured
9. the runtime decision is evidence-based
10. all write features can be disabled immediately
11. production traces can explain what happened
12. the current evaluation set passes
13. one controlled real production flow succeeds

---

# 26. Final Cursor instruction

Use this exact instruction after placing this file in the repository:

```text
Read the Mia Build Bible, AGENTS.md, MIA_FINAL_MILE_PLAYBOOK.md and MIA_PRE_PRODUCTION_ARCHITECTURE_ADJUSTMENTS.md.

Most of the project already exists. Do not rebuild completed work.

First inspect the entire repository and create docs/PRE_PRODUCTION_GAP_REPORT.md. Map every adjustment and release gate to exact existing files and tests. Label each item complete, partial, missing, unclear or blocked by external setup.

Do not write implementation code until you show me:
1. what is already complete
2. the highest-risk gaps
3. the exact build order
4. the first small implementation unit
5. any better alternative to the Bible that deserves discussion

After approval, implement one controlled unit at a time. For every unit:
plan, modify, test, independently review, adversarially test where relevant, update the gap report, and stop.

Preserve the custom LangGraph sales brain. Do not move sales reasoning into ManyChat, Make, n8n, Base44 or ElevenLabs. Use one owner per capability. Use direct controlled adapters for critical writes. Use a small preloaded Composio tool set for known workflows. Dynamic tool discovery is owner-only and low risk.

Do not enable production write access until identity, permissions, approval binding, idempotency, audit and feature flags pass their tests.
```

---

# 27. Verified current assumptions

The following assumptions were rechecked against current official documentation on 2026-08-21:

- Amazon Bedrock AgentCore Runtime supports custom agents and frameworks including LangGraph.
- AgentCore Runtime creates versioned deployments and supports multiple model providers.
- Composio sessions can preload a known small set of tools and recommends keeping that set generally below 20 to avoid context bloat.
- ManyChat documents Meta Instagram Conversation Routing to prevent conflicts and duplicate replies when several applications are connected.
- ManyChat supports current Instagram entry patterns including story replies, ad triggers and ref URLs, subject to Meta limitations.
- AWS Lambda Powertools provides idempotency utilities for retry-safe operations.

These products change quickly.

Before executing a production migration or enabling a provider write permission, Cursor must re-check the relevant official documentation.
