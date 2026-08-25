# Mia VNext — Clean Rebuild Specification

## 0. Mission

You are working inside the **correct Mia project**.

The directory containing this file is the project you must work on.

Do not switch to another repository, worktree, branch, or master copy because another version appears cleaner or easier to understand.

Mia already exists and parts of the product work well, but the repository has accumulated too much code, documentation, overlapping architecture, experiments, and historical decisions.

We are going to **rebuild Mia cleanly inside this project**.

This is not a requirement to preserve the old internal architecture.

It IS a requirement to preserve valuable working product behavior, production contracts, data, and UX where appropriate.

The priorities are:

1. Understand what actually works today.
2. Preserve the good user-facing behavior.
3. Remove unnecessary documentation and code.
4. Establish a much smaller canonical architecture.
5. Rebuild Mia around LangGraph.
6. Create two clearly separated agent experiences:

   * Owner Mia on Telegram
   * Client Mia on the AssafWeb website
7. Use Composio behind a controlled capability layer.
8. Support voice input on both Telegram and the website.
9. Automatically summarize completed website conversations and notify the owner on Telegram.
10. Make future tools easy to add without making Mia messy again.

The final result should be substantially easier for a new engineer or coding agent to understand.

---

# 1. Product Definition

Mia is the AI operator for AssafWeb.

She has **two different users and two different trust levels**.

```text
                    MIA
                     │
            ┌────────┴────────┐
            │                 │
       OWNER SIDE         CLIENT SIDE
        Telegram            Website
            │                 │
       Owner Graph        Client Graph
            │                 │
            └────────┬────────┘
                     │
               Shared Services
                     │
          ┌──────────┼───────────┐
          │          │           │
        Memory     Voice     Capabilities
                               │
                             Policy
                               │
                           Composio
                               │
                      External Services
```

This must remain **one application and one coherent architecture**, not two unrelated agents copied into different folders.

However, the owner and client must have different graphs, prompts, permissions, tools, and state boundaries.

---

# 2. Owner Mia — Telegram

Telegram is Mia's private interface with the AssafWeb owner.

The experience should feel like talking naturally with a capable digital employee.

Mia should not behave like a command-line bot where every request must follow predefined syntax.

The owner should be able to write things such as:

* "What's important today?"
* "Check my emails."
* "Did anyone contact us from the website?"
* "What happened with that lead?"
* "Find the email from X."
* "What meetings do I have tomorrow?"
* "Schedule this."
* "Summarize what happened today."
* "Look into this customer for me."

Mia should understand the request, decide whether tools are needed, use the correct capabilities, and respond naturally.

## Telegram inputs

Support:

### Text messages

Normal Telegram messages.

### Voice notes

Pipeline:

```text
Telegram voice note
        ↓
download audio
        ↓
speech-to-text
        ↓
normalized text
        ↓
Owner LangGraph
        ↓
normal Mia response
```

Voice is an **input modality**.

Do not build a separate "voice agent brain."

Voice and text must enter the same Owner Graph after transcription.

Do not add text-to-speech unless an existing requirement explicitly needs it.

---

# 3. Client Mia — Website

The website contains the client-facing version of Mia.

This is a sales/business conversation experience.

The existing website conversation experience currently has valuable behavior and presentation.

Inspect it carefully before replacing anything.

**Preserve the parts that already look and feel good.**

Do not redesign the website conversation UI merely because the backend is being rebuilt.

The objective is to improve the architecture underneath it.

Client Mia should be able to:

* naturally talk with visitors
* understand what their business does
* understand their problem
* explain relevant AssafWeb services
* ask useful follow-up questions
* answer approved questions about AssafWeb
* understand voice input
* maintain conversation context
* identify useful lead information naturally
* recommend an appropriate next step
* support meeting-first behavior where applicable
* hand off appropriately
* avoid inventing facts, prices, promises, capabilities, or delivery commitments

The client conversation should feel like a conversation with a knowledgeable AssafWeb employee, not a qualification form disguised as a chatbot.

---

# 4. Website Voice

Website visitors must also be able to speak to Mia.

Pipeline:

```text
Browser microphone
       ↓
audio upload / stream
       ↓
speech-to-text service
       ↓
normalized text
       ↓
Client LangGraph
       ↓
normal client response
```

Again:

**Voice and text use the same Client Graph.**

Do not duplicate reasoning logic for voice.

Make the STT provider replaceable behind a small interface.

Conceptually:

```python
class SpeechToText:
    async def transcribe(audio: bytes, metadata: ...) -> Transcript:
        ...
```

Reuse a reliable existing provider if Mia already has one.

Otherwise choose the simplest supported implementation based on official documentation and the providers already used by the project.

Do not hardcode credentials.

Document required environment-variable NAMES in `.env.example`.

Never expose secret values.

---

# 5. Website Conversation Completion → Telegram Ping

This is a core workflow.

When a meaningful website conversation ends, Mia should send the owner a concise Telegram notification.

Architecture:

```text
Client conversation
        ↓
conversation finalized
        ↓
deterministic finalization event
        ↓
conversation summarizer
        ↓
Lead / Conversation Summary
        ↓
Telegram notification
        ↓
Owner
```

The notification should be useful immediately.

Example information when available:

```text
New website conversation

Name: ...
Business: ...
What they need: ...
Main problem: ...
Service they appear interested in: ...
Timeline: ...
Budget: ...        # only if actually discussed
Qualification: ...
Meeting: ...
Recommended next step: ...

Conversation ID: ...
```

Do NOT fabricate missing fields.

Do not aggressively interrogate clients merely to fill this schema.

A good natural conversation is more important than completing every field.

## Finalization

Create an explicit conversation finalization mechanism.

Possible triggers can include:

* visitor explicitly finishes
* meeting/handoff completes the conversation
* browser/session explicitly closes when detectable
* configurable inactivity threshold
* another existing deterministic business rule

Do not scatter finalization logic through multiple handlers.

Have one clear finalization service/workflow.

### Idempotency

The same conversation must NOT generate repeated "conversation finished" Telegram notifications because of retries.

Use a persistent idempotency mechanism based on the conversation identifier.

For example conceptually:

```text
conversation_id + final_summary_version
```

---

# 6. Two Graphs, Shared Infrastructure

Do not create one giant ReAct agent that handles every user and permission level.

Use two clear LangGraph entry points:

```text
OwnerGraph
ClientGraph
```

They may share:

* model/provider configuration
* memory infrastructure
* state utilities
* structured output models
* speech-to-text
* logging
* tracing
* integration service
* capability models
* policy engine
* summarization utilities

They must NOT share permissions blindly.

---

# 7. Owner Graph

Conceptual flow:

```text
START
  ↓
load owner context
  ↓
understand request
  ↓
route / plan
  ↓
need external capability?
  ├── no
  │    ↓
  │  respond
  │
  └── yes
       ↓
   select capability
       ↓
   policy check
       ↓
   read / safe?
   ├── yes → execute
   │
   └── no
        ↓
      approval if required
        ↓
      execute
        ↓
   normalize result
        ↓
   validate
        ↓
   update useful state/memory
        ↓
      respond
```

Do not create LLM calls for deterministic work.

Use Python routing/policy code where possible.

---

# 8. Client Graph

Conceptual flow:

```text
START
  ↓
load conversation
  ↓
load approved AssafWeb context
  ↓
understand visitor
  ↓
conversation / qualification
  ↓
answer / ask useful next question
  ↓
optional safe capability
  ↓
meeting / handoff / continue
  ↓
conversation finalized?
       ↓
      yes
       ↓
summarize
       ↓
notify Telegram
       ↓
END
```

Do not let the Client Graph execute owner capabilities.

This must be enforced in code, not just in the prompt.

---

# 9. Composio's Role

Composio is NOT Mia.

LangGraph is responsible for:

* reasoning
* orchestration
* state
* workflow
* business rules
* approval
* policy
* tool-routing decisions
* memory decisions
* conversation behavior

Composio is responsible primarily for:

* external integrations
* authentication
* connected accounts
* external tool execution
* integration schemas
* session-scoped access

Target:

```text
LangGraph
    ↓
Business Capability
    ↓
Policy
    ↓
Integration Adapter
    ↓
Composio
    ↓
External API
```

Do NOT build:

```text
LLM
 ↓
all Composio tools
 ↓
hope it chooses correctly
```

---

# 10. Capability Layer

LangGraph should reason in terms of capabilities useful to Mia.

Examples:

```python
mail.search(...)
mail.read(...)
mail.create_draft(...)

calendar.get_schedule(...)
calendar.find_availability(...)
calendar.create_event(...)

leads.get_recent(...)
leads.update(...)

business.get_information(...)
```

Use capabilities that actually exist in Mia.

Do not add Gmail, Shopify, Sheets, CRM, Calendar, or another integration merely because Composio supports it.

First determine what Mia genuinely needs.

The important separation is:

```text
Mia business capability
          ↓
provider adapter
```

not:

```text
Mia graph
          ↓
COMPOSIO_RANDOM_TOOL_NAME
```

The graph should not depend throughout the codebase on raw Composio SDK objects.

---

# 11. Manageable Composio Sessions

Use the current official Composio session architecture.

Do NOT blindly follow old examples found in historical Mia code.

Check current official Composio documentation before implementation.

A Composio session represents the runtime integration environment associated with the appropriate application user.

Persist/reuse session identifiers where appropriate rather than creating a random new session for every message.

The owner side should map to a stable owner identity.

For example conceptually:

```text
mia_owner_id
      ↓
Composio session
      ↓
owner connected accounts
```

Never allow website clients to inherit the owner's unrestricted Composio session.

---

# 12. Restrict Composio

Never create unrestricted tool access merely because it is easier.

For each graph, define exactly which capabilities are available.

Conceptually:

```python
OWNER_CAPABILITIES = {
    ...
}

CLIENT_CAPABILITIES = {
    ...
}
```

Client capabilities should be dramatically narrower.

Where Composio provides toolkit/tool/tag filtering, use it.

Prefer allowlists.

Use read/write/destructive metadata where helpful.

Do not load hundreds of schemas into model context.

For a small stable set of common capabilities, explicit/preloaded tools may be appropriate.

For uncommon capabilities, dynamic discovery can be used where it provides real benefit.

Keep model context small.

---

# 13. Read vs Write

Every external capability must have policy metadata.

At minimum:

```text
READ
WRITE
SENSITIVE_WRITE
DESTRUCTIVE
```

Examples:

### READ

* read calendar
* search email
* retrieve business data
* read lead information

### WRITE

* create draft
* create normal record
* update a non-sensitive field

### SENSITIVE_WRITE

* send external email/message
* schedule externally visible event
* modify important customer state

### DESTRUCTIVE

* delete data
* cancel
* refund
* irreversible action

Policy should determine:

* which graph can perform it
* whether confirmation is required
* whether it can retry
* whether idempotency is required

Do not rely on:

> "Mia, please don't do anything dangerous."

Enforce it in code.

---

# 14. Website Capability Safety

The website client never gets direct access to private owner data.

A website visitor must NEVER be able to ask:

* "show me Assaf's emails"
* "what meetings does Assaf have today?"
* "show me other clients"
* "give me the latest lead list"
* "send this message from Assaf's email"

and cause the Owner capabilities to execute.

Even prompt injection must not bypass this.

The permission boundary exists below the prompt.

Potential website capabilities may include things such as:

* approved business knowledge
* approved availability lookup
* meeting scheduling
* lead creation
* safe customer-facing lookup

but ONLY when needed and authorized by the product requirements.

---

# 15. Conversation State

Owner and client state must be clearly separated.

Possible identifiers:

```text
Owner:
owner_id
telegram_chat_id
thread_id

Client:
conversation_id
anonymous/session visitor id
known lead id if applicable
```

Do not mix these.

Do not use one global LangGraph thread for multiple clients.

Do not let one website visitor see another visitor's history.

---

# 16. Memory

Mia should feel like she knows the owner and AssafWeb, but memory must remain deliberate.

Separate:

```text
conversation state
business knowledge
owner memory
client/lead state
tool results
```

Not every tool output should become long-term memory.

Do not save giant raw API payloads as memory.

Store durable information only when it has future value.

Reuse the project's existing persistence layer if it is appropriate and healthy.

Do not introduce another database merely because the rebuild gives an opportunity to do so.

---

# 17. Website Knowledge

Client Mia needs a reliable source of approved AssafWeb knowledge.

Audit where this information currently comes from.

Possible sources may include:

* current AssafWeb website
* curated business profile
* current service catalog
* approved product/business knowledge
* existing structured Mia knowledge

Do not make the prompt itself the database for every business fact.

Separate stable persona/instructions from business data.

---

# 18. Prompts

Keep prompts small.

Avoid huge system prompts containing:

* architecture documentation
* duplicated business information
* deployment notes
* old decisions
* unused tool documentation

Recommended separation:

```text
Owner system behavior
Client system behavior
Business context
Task-specific context
```

Only inject information relevant to the current graph/run.

---

# 19. Repository Rebuild Philosophy

We are intentionally using this rebuild to simplify Mia.

Before adding new architecture, inspect the repository.

Identify:

```text
KEEP
REUSE
REPLACE
DELETE
ARCHIVE
UNKNOWN
```

Do not preserve old internal modules merely because they exist.

But do not delete production functionality until its replacement works.

Use a strangler/incremental replacement strategy where necessary:

```text
old path
    ↓
new path implemented
    ↓
tests
    ↓
traffic/caller moved
    ↓
old path removed
```

Do not keep permanent compatibility layers without a real consumer.

---

# 20. Documentation Cleanup

The current Markdown situation is part of the problem.

Do not require future coding agents to read a large collection of overlapping MD files.

During the rebuild, create a small canonical set.

Target:

```text
AGENTS.md

docs/
    PRODUCT.md
    ARCHITECTURE.md
    DECISIONS.md
    archive/
```

## AGENTS.md

Very short.

Contains:

* how to work in the repository
* required canonical documents
* important commands
* test expectations
* secret-handling rules
* production safety rules

It must NOT contain another complete description of Mia.

## PRODUCT.md

Canonical product truth:

* what Mia is
* Owner Mia
* Client Mia
* current capabilities
* product behavior
* voice
* conversation finalization
* Telegram notifications
* current vs future scope

## ARCHITECTURE.md

Canonical technical truth:

* two graphs
* shared infrastructure
* state
* memory
* channels
* voice
* capability layer
* Composio
* policy
* persistence
* deployment/runtime shape

## DECISIONS.md

Only architectural decisions that still constrain the active system.

Historical documents should move into `docs/archive/` only if worth keeping.

Archived docs are NOT normal required reading.

Delete low-value temporary docs when safe.

Do not create more MD files merely to explain every implementation task.

---

# 21. This File's Role

`MIA_REBUILD.md` is the rebuild specification.

During the rebuild, treat it as the authoritative description of the desired VNext direction.

Old documents are evidence about current/previous behavior, not automatically the desired architecture.

Where old documentation conflicts:

1. identify the conflict
2. validate actual behavior against code
3. preserve important product behavior
4. follow this rebuild direction for the new internal architecture

When the rebuild is complete and the canonical documentation accurately describes production Mia, this document no longer needs to be normal required reading.

It may be archived.

---

# 22. Do Not Load Every Old MD File Every Turn

One of the explicit goals is reducing context pollution.

At the start:

1. inventory documentation filenames
2. inspect the most relevant current docs
3. inspect code
4. identify conflicts
5. consolidate

Do not repeatedly inject the entire documentation archive into context.

After canonical documentation exists, normal work should require approximately:

```text
AGENTS.md
docs/PRODUCT.md
docs/ARCHITECTURE.md
docs/DECISIONS.md
```

and then only the code relevant to the task.

---

# 23. Code Structure

Do not force an elaborate directory structure merely because it appears clean on paper.

Prefer a small number of meaningful boundaries.

Conceptually, something like this is enough:

```text
app/
    agents/
        owner/
        client/
        shared/

    channels/
        telegram/
        website/

    capabilities/
        registry
        policy
        composio

    services/
        voice
        notifications

    domain/
        models
```

Adapt to the existing application.

If an existing directory already represents one of these concepts cleanly, reuse it.

Avoid creating:

```text
ToolService
CapabilityService
IntegrationService
ConnectorService
ProviderService
ActionService
```

all wrapping one another.

Every layer must earn its existence.

---

# 24. Telegram Adapter

Telegram-specific code should handle Telegram.

For example:

* receiving update/webhook
* extracting text
* downloading voice note
* returning response
* sending proactive notification

It should NOT contain Mia's reasoning/business logic.

Target:

```text
Telegram
   ↓
Telegram adapter
   ↓
normalized message
   ↓
OwnerGraph
```

---

# 25. Website Adapter

Website-specific code should handle:

* HTTP/WebSocket/SSE transport as currently appropriate
* conversation identifier
* text input
* audio upload
* streaming response if currently supported
* UI integration

It should NOT contain the core sales-agent logic.

Target:

```text
Website
   ↓
Website adapter
   ↓
normalized message
   ↓
ClientGraph
```

Preserve the current good frontend UX unless there is a concrete bug.

---

# 26. Cross-Graph Communication

Do not make the Client Graph directly start pretending to be Owner Mia.

When a client conversation creates an owner-relevant event:

```text
ClientGraph
    ↓
domain event
    ↓
notification service
    ↓
Telegram
```

For example:

```text
WebsiteConversationCompleted
LeadNeedsHuman
MeetingBooked
HighIntentLeadDetected
```

The event should carry structured data.

The notification renderer turns it into the Telegram message.

This keeps the two agents separated.

---

# 27. Conversation Summary Model

Use structured output internally.

Example concept:

```python
class ConversationSummary(BaseModel):
    conversation_id: str
    name: str | None
    contact: str | None
    business: str | None
    need: str | None
    pain_points: list[str]
    relevant_service: str | None
    timeline: str | None
    budget: str | None
    qualification: str | None
    meeting_status: str | None
    recommended_next_step: str | None
```

Adapt the fields to Mia's actual product.

Do not force the model to invent values.

Unknown remains `None`.

---

# 28. Observability

A rebuild is not successful if it becomes impossible to debug.

Use structured logging/tracing for:

```text
request_id
conversation_id / thread_id
graph
node
capability
tool/provider
tool duration
success/failure
approval
voice transcription status
notification status
```

Do not log:

* API keys
* OAuth tokens
* raw secrets
* unnecessary sensitive client information

Tool calls should be inspectable.

---

# 29. Errors

Normalize external errors.

Examples:

```text
AuthenticationRequired
PermissionDenied
CapabilityUnavailable
InvalidArguments
RateLimited
ProviderUnavailable
ExternalServiceError
```

The graph should not depend on random Composio/provider error strings.

Give Mia appropriate conversational fallbacks.

---

# 30. Idempotency

Important external writes must be safe under retries.

Especially:

* Telegram final-summary notification
* email sends
* calendar event creation
* lead creation
* any customer/CRM mutation

Design idempotency explicitly.

Do not assume LangGraph or the provider guarantees it for every operation.

---

# 31. Testing

The rebuilt architecture must have meaningful tests.

At minimum:

## Owner — text

Telegram text reaches OwnerGraph and produces a response.

## Owner — voice

Telegram voice:

```text
audio → STT → same OwnerGraph
```

## Client — text

Website text reaches ClientGraph.

## Client — voice

Website audio:

```text
audio → STT → same ClientGraph
```

## Isolation

Website clients cannot execute Owner capabilities.

## Client isolation

Conversation A cannot access conversation B state.

## Owner tools

An allowed owner capability can be executed through the capability/Composio boundary.

## Tool restrictions

An unavailable capability is rejected.

## Reads

Safe reads work without unnecessary confirmation.

## Sensitive writes

Sensitive operations pass through policy/approval.

## Composio identity

The correct connected account/session is used.

## Composio mocks

Normal unit tests must not require real Gmail, Calendar, or other production accounts.

## Conversation finalization

A finished website conversation produces a structured summary.

## Telegram ping

The owner receives the final summary.

## Idempotency

Retries do not send the final Telegram summary multiple times.

## Existing website behavior

Important existing website conversation UX/API contracts continue to work.

---

# 32. Environment Configuration

Do not inspect `.env`.

Inspect how configuration is currently loaded.

Create/update `.env.example` using names only.

Potential categories:

```text
LLM provider
Composio
Telegram
Speech-to-text
Database/persistence
Application URL
Observability
```

Only introduce variables that are actually required.

Do not invent secrets.

---

# 33. Official Documentation Rule

For LangGraph, Composio, Telegram, speech-to-text providers, and any fast-changing dependency:

Use current official documentation before implementing.

Do not trust old code snippets in the repository when APIs may have changed.

Prefer the currently installed package version when it intentionally constrains the implementation.

If upgrading a major dependency is necessary, explain why and keep the upgrade narrow.

---

# 34. Initial Tool Scope

Do NOT begin by enabling everything in Composio.

Audit what the owner currently needs.

Choose a very small first tool set.

Prefer beginning with one useful, low-risk READ workflow.

For example, if Gmail currently matters:

```text
Owner asks about inbox
        ↓
OwnerGraph
        ↓
mail.search/read capability
        ↓
policy = READ
        ↓
Composio adapter
        ↓
Gmail
```

Once this works cleanly, add the next capability.

Future tools must plug into the same pattern.

---

# 35. Future Extensibility

Adding another integration later should look approximately like:

```text
1. define capability
2. define policy
3. implement/map provider adapter
4. add capability to allowed graph
5. tests
```

It should NOT require rewriting Mia.

Example future providers may change.

The architecture must remain replaceable.

---

# 36. What NOT to Build

Do not build:

### One giant agent

```text
Telegram
Website
Voice
Clients
Owner
All tools
        ↓
one enormous ReAct loop
```

### A separate agent for voice

Voice is transport/input processing.

### Unrestricted Composio

Never expose the entire catalog by default.

### Architecture duplicated by channel

Do not copy the full agent into Telegram and website directories.

### Prompt-only security

Permissions must exist below the prompt.

### An abstraction maze

Keep code understandable.

### A documentation maze

Keep canonical docs small.

### Permanent dead code

Once replacement is verified, remove obsolete code.

---

# 37. Migration / Rebuild Sequence

Perform the work in this order.

## Phase A — Discover

Inspect:

* repository tree
* running application entrypoint
* current website chat
* Telegram integration
* LangGraph implementation
* persistence
* prompts
* tools/integrations
* voice support
* deployment/runtime configuration
* tests
* Markdown inventory

Output the actual existing architecture.

Do not make assumptions based only on old docs.

---

## Phase B — Decide What Survives

For major modules classify:

```text
KEEP
REUSE
REPLACE
DELETE
ARCHIVE
UNKNOWN
```

Identify which existing user-facing behavior must survive.

---

## Phase C — Establish Canonical Docs

Create:

```text
AGENTS.md
docs/PRODUCT.md
docs/ARCHITECTURE.md
docs/DECISIONS.md
```

Reduce old documentation.

Update AGENTS.md so future agents read only the canonical files.

---

## Phase D — Establish VNext Skeleton

Build the smallest clean boundaries for:

```text
OwnerGraph
ClientGraph
Telegram adapter
Website adapter
Voice service
Capability/policy layer
Notification service
Persistence
```

Do not add integrations yet that are unnecessary for proving the architecture.

---

## Phase E — Preserve Website Experience

Connect the existing good website experience to ClientGraph.

Do not gratuitously redesign the UI.

Make text conversations work first.

---

## Phase F — Website Voice

Connect:

```text
website audio
→ STT
→ ClientGraph
```

---

## Phase G — Telegram Owner Chat

Connect Telegram text to OwnerGraph.

Mia should converse naturally.

---

## Phase H — Telegram Voice

Connect:

```text
Telegram voice note
→ STT
→ OwnerGraph
```

---

## Phase I — Website Summary → Telegram

Implement:

```text
Client conversation
→ finalization
→ summary
→ owner Telegram ping
```

with idempotency.

---

## Phase J — First Composio Capability

Implement ONE safe owner capability through:

```text
OwnerGraph
→ capability
→ policy
→ Composio
→ external service
```

Prove the architecture.

---

## Phase K — Expand Composio

Only after the first integration is stable, migrate/add additional tools one at a time.

---

## Phase L — Delete Old Architecture

Once the VNext path handles the required functionality:

* remove replaced graph code
* remove duplicate tool systems
* remove stale prompts
* remove unused adapters
* remove compatibility layers without consumers
* remove stale tests
* remove stale docs

Then run the complete validation suite.

---

# 38. Production Safety

Do not deploy automatically unless explicitly requested.

The repository may be connected to production.

Preserve:

* database compatibility unless migration is intentional
* important API routes/contracts where the current frontend depends on them
* website UX that already works well
* Telegram identity/security
* production secrets
* production data

If a schema migration is necessary, make it explicit and reversible where practical.

---

# 39. Definition of Done

Mia VNext is successful when:

### Architecture

There are two clear graphs:

```text
OwnerGraph
ClientGraph
```

with shared infrastructure.

### Telegram

The owner can talk naturally with Mia through text.

### Telegram Voice

The owner can send voice notes and Mia understands them.

### Website

Clients can have natural sales/business conversations with Mia.

### Website Voice

Clients can speak and the same ClientGraph understands the transcription.

### Handoff

Completed website conversations create useful Telegram summaries automatically.

### Composio

The owner can use external capabilities through a controlled:

```text
LangGraph
→ capability
→ policy
→ Composio
```

architecture.

### Security

Clients cannot access owner tools or data.

### Tool growth

New integrations can be added without redesigning the graph.

### Codebase

The repository is meaningfully smaller and clearer than before.

### Documentation

A coding agent normally needs only:

```text
AGENTS.md
docs/PRODUCT.md
docs/ARCHITECTURE.md
docs/DECISIONS.md
```

before inspecting task-specific code.

### No dead migration architecture

The old replaced implementation has been removed.

---

# 40. First Response Before Coding

Before making significant modifications, report:

## A. Actual current architecture

Show what really runs today.

## B. Website behavior worth preserving

Identify the current client-facing pieces that should survive.

## C. Telegram behavior worth preserving

Identify what is already functional.

## D. Voice status

Explain what exists today for website and Telegram voice.

## E. Existing tool/integration architecture

Show direct APIs, Composio, custom tools, MCP, etc.

## F. Documentation problems

Identify duplicated/conflicting/stale docs.

## G. Code complexity

Identify unnecessary modules/abstractions/duplicate implementations.

## H. VNext architecture

Show the exact proposed target architecture.

## I. Rebuild map

For major components:

```text
KEEP
REUSE
REPLACE
DELETE
ARCHIVE
```

## J. Execution sequence

List the concrete implementation slices.

Then proceed with the rebuild.

Do not stop after producing a theoretical architecture unless a genuine blocker makes implementation unsafe.

---

# 41. Core Principle

When uncertain, optimize for this:

```text
                   MIA
                    │
          ┌─────────┴──────────┐
          │                    │
     OWNER GRAPH          CLIENT GRAPH
     Telegram              Website
    text + voice          text + voice
          │                    │
          └─────────┬──────────┘
                    │
            Shared Mia Core
                    │
              Capability Layer
                    │
                 Policy
                    │
                Composio
                    │
             External Systems
```

Mia should feel sophisticated to the user because her behavior is good.

Her code should feel boring, small, explicit, and understandable to the engineer.

**Do not confuse complexity with capability.**
