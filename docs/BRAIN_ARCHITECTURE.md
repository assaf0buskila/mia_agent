# Mia's brain — architecture

**Status:** implemented 2026-08-23. **WhatsApp is out of scope for this slice.**
Plan and research trail: `docs/archive/BRAIN_PLAN.md`. Decisions: ADR-026 in `docs/DECISIONS.md`.

Mia was a keyword switchboard: a phrase table picked one of ~25 task types, a 700-line
`if` chain ran one Python function, and the model only rephrased the result. Off-list
phrasing fell through to a generic digest, two requests in one message were impossible,
and nothing survived the conversation. This document describes what replaced that.

---

## 1. The layers

```
Telegram (voice or text)
  └─ transcription (gpt-transcribe, he/en)
     └─ owner allowlist (numeric ids)
        └─ deterministic classifier  ── write/approval intents ──▶ unchanged handlers
           └─ brain context assembly (profile + memory + knowledge)
              └─ owner agent loop (read tools, N steps)
                 └─ HTML renderer ──▶ Telegram
        └─ after the reply: extraction ──▶ consolidation ──▶ memory
```

Every layer degrades independently. No model keys → the deterministic classifier answers,
exactly as before. No embeddings → retrieval falls back to BM25. Model outage → the canned
result is phrased and sent. Kill switch blocks high-risk writes; it does not 503 owner talk or site chat.

---

## 2. Memory model

Four kinds in one table, discriminated by `kind` (`app/brain/schemas.py`):

| Kind | Holds | Lifetime |
|---|---|---|
| `episodic` | things that happened: decisions, discussions, events | decays; superseded by newer episodes |
| `semantic` | stable facts: who Assaf is, businesses, skills, relationships | long-lived, updated in place |
| `working` | current projects, open tasks, active goals | short; closed when resolved |
| `preference` | how he wants Mia to work and communicate | long-lived, newest wins |

Orthogonal to `kind` is `category` (identity, background, business, project, skill, goal,
preference, communication, workflow, decision, task, relationship, event), which drives the
always-on profile and filtered retrieval.

### Tables (`migrations/20260823_brain_memory.sql`)

| Table | Purpose |
|---|---|
| `brain_memories` | the memory store: text, kind, category, importance, confidence, status, provenance, embedding |
| `brain_knowledge_sources` | one row per ingested URL with content hash — makes re-ingest idempotent |
| `brain_knowledge_chunks` | retrievable website/business knowledge, categorized |
| `brain_entities` | people, companies, products, projects, technologies |
| `brain_memory_entities` | many-to-many links between memories and entities |
| `brain_knowledge_gaps` | what Mia wants to learn, whether she asked, when it was answered |

Portable types only — `String`/`Text`/`Integer`/`Float` and ISO-8601 timestamp strings. No
`SERIAL`, no `JSONB`, no `NOW()` defaults, no vector type. The same DDL creates identically
on SQLite (the 2000-test suite) and RDS PostgreSQL (production), with no `POSTGRES_ONLY`
entry and no dialect branching.

### Why not pgvector

`pgvector`'s SQLAlchemy type is PostgreSQL-dialect-only, so adopting it would mean the test
suite exercises a different retrieval path than production — exactly the failure the
SQLite-parity constraint exists to prevent. pgvector also documents that exact search
"provides perfect recall" and that an index trades recall for speed; its own IVFFlat sizing
rule (`rows/1000`) yields `lists = 3` at this corpus size, i.e. a degenerate index. At one
owner and a few thousand rows there is no speed problem to trade recall for.

Vectors are therefore stored as base64 float32 in a `TEXT` column, L2-normalized on write so
cosine similarity is a plain dot product (`app/brain/vectors.py`). Measured: **3000 × 1536
exact search is ~170 ms in pure stdlib**, against an LLM call that costs seconds. No numpy
dependency was added.

The escape hatch, if the corpus ever grows: a `POSTGRES_ONLY` migration adding
`CREATE EXTENSION vector`, a shadow `vector(1536)` column, and an HNSW index — while the
portable column stays the source of truth so SQLite tests keep passing.

---

## 3. Retrieval

Two stages, both in Python so one code path serves both engines (`app/brain/retrieval.py`).

**Stage 1 — candidate generation.** An exact-cosine list and a BM25 keyword list over the
same rows, fused with Reciprocal Rank Fusion at `k = 60`. RRF fuses on *rank*, not score,
because cosine sits in [-1, 1] while BM25 is unbounded — averaging them directly would let
BM25 dominate. Keyword ranking runs in-process rather than via Postgres `ts_rank` or SQLite
`bm25()`: that is the only way to keep one implementation across both engines, and over a
few thousand short documents it costs microseconds.

**Stage 2 — memory re-rank.** The Generative Agents score, each component min-max
normalized across the candidate set:

```
score = w_relevance · relevance + w_recency · recency + w_importance · importance

relevance  = normalized RRF score
recency    = 0.995 ** hours_since(last_used_at)
importance = stored 1..10 poignancy / 10
```

Defaults `1.0 / 0.5 / 0.3`, tunable via `MIA_MEMORY_WEIGHT_*`. Relevance leads because Mia
answers questions; the original paper weights all three at 1.0 because its agents simulate a
world where recency *is* the task.

Applied to memories only. Knowledge chunks stop at stage 1 — recency and poignancy are
meaningless for a web page.

**On retrieval, `last_used_at` and `use_count` are bumped** for everything actually placed in
the prompt. The decay is defined over *last access*, not creation; skipping this makes the
recency term meaningless.

Then: dedup by token overlap (a fact shown twice inflates the model's confidence), and a
character budget. The always-on profile is capped at 40% of the budget so a small
`max_chars` still leaves room for anything retrieved for the actual question.

---

## 4. What gets remembered

`app/brain/extraction.py`, run **after** the reply is composed so it never adds latency.

1. **Extract.** One strict-schema call returns candidate facts, each rated 1–10 on the
   poignancy scale. Anything below 3 is dropped and never written — not writing it down is
   the cheapest and most effective forgetting mechanism there is.
2. **Reconcile.** For each survivor: embed it, retrieve the 5 nearest existing memories, and
   ask for one of four operations — `ADD` / `UPDATE` / `DELETE` / `NOOP`. Two shortcuts skip
   the model entirely: no neighbours → `ADD`, a ≥0.9 token-overlap restatement → `NOOP`.
3. **Apply.**
   - `ADD` → insert.
   - `UPDATE` → rewrite in place and re-embed (update the document, don't duplicate it).
   - `DELETE` → **never a SQL delete.** The old row gets `status='superseded'` and a
     `superseded_by` pointer to its replacement. Supersede, don't remove — that is what keeps
     "why does Mia believe X?" answerable a year later.
   - `NOOP` → bump `last_used_at` only.

**Only owner-channel text reaches this pipeline.** A website visitor cannot write owner
memory; that boundary is enforced by where `learn_from_exchange` is called, not by a prompt.

---

## 5. Website knowledge

`www.assafweb.com` publishes `llms.txt`, `llms-full.txt` and `pricing.md` — clean,
structured, Hebrew-first markdown that Assaf already maintains for agents. That is the
primary source: no crawler, no scraping noise, no Firecrawl credits, and a content hash
makes re-ingest a no-op when nothing changed.

Content is split on markdown **headings**, not a fixed character window, so each chunk is a
coherent topic that reads on its own in a prompt. Heading ancestry is tracked, so a `###`
question under `## שאלות נפוצות` classifies as FAQ instead of falling through to OTHER.

Taxonomy: personal, project, service, product, experience, skill, portfolio, business,
current_work, contact, process, faq, testimonial, pricing.

Against the live corpus this yields 31 chunks across 9 categories, with 7 remaining "other"
(site metadata: MCP, Agent Instructions, document titles) — correctly uncategorized.

```bash
uv run mia-ingest-knowledge --dry-run
```

`--force` re-embeds even when unchanged. One failing source never aborts the others.

---

## 6. The owner agent loop

`app/graph/owner_agent.py`, registry in `app/tools/registries/owner_tools.py`.

16 tools: `search_memory`, `search_knowledge`, `remember`, `list_known_entities`,
`daily_brief`, `weekly_brief`, `hot_leads`, `pending_approvals`, `website_conversations`,
`operator_snapshot`, `owner_status`, `lead_review`, `meeting_brief`,
`calendar_availability`, `booked_meetings`, `content_ideas`.

**All reads, plus one owner-scoped memory write.** Nothing in the registry sends, books,
approves, spends, publishes or deletes.

Safety properties, unchanged from before the loop existed:

- **The allowlist is enforced on the tool name the model returns**, server-side. Not by
  asking the API to restrict itself — `allowed_tools` is undocumented on Chat Completions,
  and the Gemini compatibility layer silently ignores parameters it does not support.
- **Write and approval intents never reach the model at all.** `DETERMINISTIC_TASK_TYPES` in
  `app/domain/owner_brain.py` routes approvals, takeover, conversation scope, stored
  preferences, outreach drafts and meeting debriefs to the original handlers.
- Assaf's message is data. It cannot add a tool, raise permissions or bypass a gate.
- The model never sees a Composio catalog — only the pinned registry.
- The loop is bounded by `MIA_OWNER_AGENT_MAX_STEPS`; on the final step tools are withheld so
  the model must answer instead of requesting another call it will never get.

Loop discipline that matters: `finish_reason == "length"` is checked *before* parsing JSON
(a truncated tool-argument string otherwise looks like a malformed-JSON bug); the whole
assistant message including its `tool_calls` array is appended before the results; and every
tool call gets exactly one `role: "tool"` reply keyed by its `tool_call_id`, including calls
refused over the per-step cap — a missing id breaks the next request.

### Asking questions

Mia asks only after checking memory, knowledge and tool results, at most one question, at
the end. Open gaps are surfaced in the context block, recorded once per topic in
`brain_knowledge_gaps`, and marked asked so she does not interview him.

---

## 7. Voice

**A real production bug was found and fixed here.** The adapter sent
`response_format=verbose_json` with model `gpt-transcribe`. That format is whisper-1 only;
for the GPT transcribe family the supported format is `json`. The caller swallowed the
resulting error into "לא תפסתי את ההקלטה", so **every owner voice note was failing
silently.**

`transcription_request_fields` now picks parameters per model family:

| Model | `response_format` | Language parameter | Keywords |
|---|---|---|---|
| `gpt-transcribe` | `json` | `languages[]` (plural array) | yes |
| `whisper-1` | `verbose_json` | `language` (singular) | no |
| `gpt-4o-*-transcribe` | `json` | `language` (singular) | no |

`gpt-transcribe` takes `languages` **instead of** `language` — the docs say plainly "don't
send both fields". Hebrew and English are sent together so code-switching is handled.

Response parsing reads both shapes: `languages[0].code` (current) and `language` (verbose),
duration from `duration` or `usage.seconds`. The `confidence` field was being read but **does
not exist in any documented OpenAI transcription response**; it is now optional rather than
assumed.

The multipart filename and content type are always explicit, because the spec requires
"enough format metadata for the file to be identified".

**Architecture choice:** transcription stays primary rather than routing raw audio through an
audio-in chat model. It is roughly an order of magnitude cheaper per minute, it is the only
path with documented multi-language hints for Hebrew, and Telegram's 20 MB `getFile` cap sits
comfortably under the API's 25 MB limit.

---

## 8. Telegram rendering

`app/integrations/telegram_format.py`. Formatting is built in code, not left to the model, so
output is consistent instead of however the LLM felt that turn.

**parse_mode is HTML, not MarkdownV2.** MarkdownV2 requires escaping 18 characters under
three different context-dependent rules, and every value this bot interpolates is a landmine
there: `lead_ab12` (underscore), `a.b@x.co.il` (dots), decimals, parentheses, hyphens. HTML
needs exactly three characters escaped (`<`, `>`, `&`) under one uniform rule, supports every
entity MarkdownV2 does, and `html.escape` is stdlib. Hebrew is above U+007F and unaffected
either way — so MarkdownV2 would buy nothing on the Hebrew side while costing everything on
the data side.

Also fixed: `reply_to_message_id` and `disable_web_page_preview` were replaced by
`reply_parameters` and `link_preview_options` in Bot API 7.0. Both still work server-side but
are no longer documented.

**The trap worth knowing:** `setWebhook`'s `allowed_updates` is sticky server-side state on
the bot token. If it was ever set to `["message"]`, `callback_query` is dropped silently and
forever on every later call that omits it — buttons spin with nothing in the logs. It is now
always sent explicitly (`ALLOWED_UPDATES`) and can be verified with `get_webhook_info`.

Primitives: `<blockquote expandable>` to collapse detail behind a tap (the best scannable
element Telegram offers), `<code>` for ids (monospace **and** tap-to-copy), `style: success`/
`danger` for native green/red buttons, Hebrew date phrasing (`23 באוגוסט 2026`,
`יום ראשון, 23 באוגוסט, 14:30`, `היום`/`מחר`), bidi isolation for LTR runs inside Hebrew,
and 4096-character chunking (overflow behaviour is undocumented, so never rely on the
server to truncate).

`callback_data` is capped at **64 bytes** and Hebrew is 2 bytes/char, so payloads stay ASCII
and opaque (`ok:<approval_id>`), with the Hebrew in the visible label.

### One-tap approvals

The console previously had no completion path: a gated action logged "נרשם כמשימה. לא ביצעתי
אותה." and there was no way to say yes. Now `answerCallbackQuery` fires first and
unconditionally (the client spins until it lands), the owner allowlist is re-checked,
the decision is applied, and the message is rewritten with its buttons cleared. Replays
against a button-less message are idempotent.

---

## 9. Configuration

All model ids are configuration, never hard-coded (AGENTS.md build-time model policy).

| Variable | Default | Purpose |
|---|---|---|
| `MIA_OWNER_AGENT_MODEL` | *(empty)* | preferred tool-calling model; blank may use the configured sales chain, otherwise deterministic classifier |
| `MIA_OWNER_AGENT_FALLBACK_MODEL` | *(empty)* | secondary model id |
| `MIA_OWNER_AGENT_MAX_STEPS` | `8` | tool-loop step budget (ADR-032) |
| `MIA_EXTRACTION_MODEL` | *(empty)* | memory extraction + reconciliation |
| `MIA_EMBEDDING_PROVIDER` | `openai` | `openai` or `gemini`. Never auto-failover |
| `MIA_EMBEDDING_MODEL` | *(empty)* | e.g. `text-embedding-3-small` |
| `MIA_EMBEDDING_DIM` | `1536` | must match the model |
| `MIA_MEMORY_ENABLED` | `true` | master switch for retrieval |
| `MIA_MEMORY_WRITE_ENABLED` | `true` | master switch for learning |
| `MIA_MEMORY_MAX_CONTEXT_CHARS` | `4000` | context budget per turn |
| `MIA_MEMORY_WEIGHT_RELEVANCE` | `1.0` | retrieval weight |
| `MIA_MEMORY_WEIGHT_RECENCY` | `0.5` | retrieval weight |
| `MIA_MEMORY_WEIGHT_IMPORTANCE` | `0.3` | retrieval weight |
| `MIA_KNOWLEDGE_SOURCES` | `llms-full.txt,llms.txt,pricing.md` | files fetched from `MIA_WEBSITE_URL` |

Reused: `MIA_OPENAI_API_KEY`, `MIA_GEMINI_API_KEY`, `MIA_OPENAI_TRANSCRIBE_MODEL`,
`MIA_TELEGRAM_BOT_TOKEN`, `MIA_WEBSITE_URL`, `MIA_CALENDAR_TIMEZONE`.

Secrets live in AWS Secrets Manager `mia/prod` and are injected as `MIA_*` by ECS. Nothing is
hard-coded and no secret is ever returned by an endpoint.

**Validation:** `GET /health` returns a `brain` block naming the exact variables each feature
is still missing, plus live corpus counts:

```json
"brain": {
  "owner_agent":      {"ready": false, "missing": ["MIA_OWNER_AGENT_MODEL"], "max_steps": 8},
  "embeddings":       {"ready": false, "missing": ["MIA_EMBEDDING_MODEL"], "provider": "openai", "dim": 1536},
  "memory_extraction":{"ready": false, "missing": ["MIA_EXTRACTION_MODEL"]},
  "voice_in":         {"ready": false, "missing": ["MIA_TELEGRAM_BOT_TOKEN"]},
  "corpus":           {"memories": 0, "knowledge_chunks": 0}
}
```

When an owner sales-chain or owner-Gemini fallback is configured and ready, the
owner-agent `missing` list is empty even if `MIA_OWNER_AGENT_MODEL` is blank.
The readiness block reports the active fallback contract, not a preferred-but-unused
configuration path.

---

## 10. Debugging retrieval

- **`/health` → `brain.corpus`** — is anything ingested at all?
- **`uv run mia-ingest-knowledge --dry-run`** — what the chunker produces per source, with
  categories, without writing.
- **A memory that should surface but doesn't** — check `status` (superseded rows are excluded
  by design) and `embedding_model` (a model change means the vector is incomparable and the
  corpus needs a backfill; keyword search still works meanwhile).
- **Everything ranks equally** — usually no embeddings configured. `BrainContext.degraded` is
  `true` and retrieval is keyword-only.
- **Wrong thing ranked first** — `RetrievedItem` carries `similarity`, `recency` and
  `importance` separately, so the component responsible is visible. Tune
  `MIA_MEMORY_WEIGHT_*`.
- **The agent answered without a tool** — `AgentOutcome.steps` records every call with its
  ok/error; `tools_used` records only the ones that succeeded.

---

## 11. Adding a knowledge source later

1. Add the filename to `MIA_KNOWLEDGE_SOURCES` if it lives under `MIA_WEBSITE_URL`.
2. For anything else, implement the `DocumentFetcher` protocol (one `fetch(url) -> str`) and
   pass it to `ingest_source`. Firecrawl `/v2/scrape` fits here behind the existing
   `MIA_FIRECRAWL_API_KEY`.
3. Extend `_CATEGORY_RULES` in `app/brain/knowledge.py` if the new source uses headings the
   taxonomy does not recognize.

Adding a **tool** is one `_register(...)` call plus a handler. Under `strict: true` every
property must be listed in `required`, `additionalProperties` must be `false`, and optional
arguments are a `["type","null"]` union.

---

## 12. Tests

`tests/unit/test_brain_memory.py` (30), `test_brain_agent.py` (14),
`test_brain_voice_knowledge.py` (32), `test_telegram_format.py` (36).

They assert behaviour, not API success: which memory comes back for "what projects am I
working on?", that a changed preference supersedes the old one and the old one stops
surfacing, that profile facts are not duplicated into the retrieved section, that retrieval
degrades to keyword-only without embeddings, that the budget is never exceeded, that a
corrupt vector row is skipped rather than fatal, that an unknown tool name is refused, that
`gpt-transcribe` never receives `verbose_json`, and that Hebrew never reaches `callback_data`.
