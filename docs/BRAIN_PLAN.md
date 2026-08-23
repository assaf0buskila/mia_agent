# Mia Brain — implementation plan

**Date:** 2026-08-23
**Scope:** owner-facing brain, memory, knowledge, voice. **WhatsApp is out of scope.**
**Baseline:** commit `ce4d039`, live image `mia:15`, `uv run pytest` = 2 pre-existing failures
(`test_story_calendar_no_double_book`, `test_website_post_message_enriches_seeded_offer_meeting` —
both date-dependent calendar fixtures, unrelated to this slice).

Research sources for every provider decision are cited in `docs/BRAIN_ARCHITECTURE.md`.
Nothing here was taken from memory or from an example older than the current docs.

---

## 0. Governing constraint — do not break the product

The existing product is 2000+ passing tests, a live Fargate service, and a deterministic
safety architecture (`app/core/risk.py`, approvals, kill switch, allowlists). The brain is
**additive**:

- Every new path is **feature-gated on configuration**. With no model keys set — which is
  exactly how the test suite runs — the new agent loop, memory extraction and embeddings are
  all disabled and the existing deterministic classifier path runs unchanged.
- `select_next_action`, `classify_owner_task`, risk policy, approval binding, idempotency and
  the Telegram numeric allowlist are **not** replaced. The agent loop sits *in front of* them
  and still routes every write through them.
- New tables only. No column is dropped or retyped. Migrations are additive `.sql` in the
  existing filename-ordered runner, and must apply on **both** SQLite (tests) and Postgres (prod).

---

## 1. Findings that change the design

### 1.1 Voice is broken in production right now

`app/integrations/transcribe.py` posts `response_format=verbose_json` with model `gpt-transcribe`.

- `gpt-transcribe` is a real, current model id (released 2026-07-28) — that part is correct.
- **`verbose_json` is not supported by it.** Per the OpenAPI spec, `verbose_json` is a
  whisper-1 format; for the GPT transcribe family the supported format is `json`.
- The parser reads `payload["language"]`, `payload["duration"]` and `payload["confidence"]`.
  `gpt-transcribe` returns `languages` (an **array** of `{code}`), duration only via
  `usage.seconds`, and **`confidence` does not exist in any documented OpenAI response.**
- `_transcribe_telegram_voice` swallows `AdapterHttpError` and returns empty text, which renders
  as `לא תפסתי את ההקלטה`. A 400 from the bad parameter is therefore **invisible**.

Fix, not rewrite: pick `response_format` by model family, parse both response shapes, send
`languages=["he","en"]` (never together with `language`), Hebrew `prompt`, and set the multipart
filename/content-type explicitly.

### 1.2 Telegram is using two parameters that no longer exist in the docs

`reply_to_message_id` and `disable_web_page_preview` were replaced in Bot API 7.0 by
`reply_parameters` and `link_preview_options`. They still work server-side, but the modern form
is required for the rest of this work.

**The one that will bite:** `setWebhook`'s `allowed_updates` is **sticky server-side state on the
bot token**. If it was ever set to `["message"]`, `callback_query` is dropped silently, forever,
with no error — buttons would spin and nothing would reach the app. Every `setWebhook` call must
pass `allowed_updates` explicitly and be verified with `getWebhookInfo`.

**Formatting: HTML, not MarkdownV2.** MarkdownV2 requires escaping 18 characters under three
different context-dependent rules. Every value this bot interpolates — `lead_abc123` (hyphen/underscore),
`a.b@x.co.il` (dots), decimals, parentheses — is a MarkdownV2 landmine. HTML needs exactly three
characters escaped (`<`, `>`, `&`) under one uniform rule, supports every entity MarkdownV2 does,
and `html.escape` is stdlib. Hebrew is above U+007F and unaffected either way.

Also usable: `<blockquote expandable>` (collapse detail behind a tap — the best "scannable"
primitive available), `<code>` (monospace **and tap-to-copy** for ids), and `style: "success"`/`"danger"`
on inline buttons for native green/red.

`callback_data` is limited to **64 bytes**, and Hebrew is 2 bytes/char in UTF-8 — callback payloads
stay ASCII and opaque.

### 1.3 The website already publishes a structured, agent-oriented corpus

`www.assafweb.com` serves `llms.txt`, `llms-full.txt`, `pricing.md`, `index.md`, `sitemap.xml` and
`.well-known/agent.json`. `llms-full.txt` is 8 KB of clean Hebrew covering identity, 8 services, the
6-step process, FAQ, testimonials, projects (MYstudio / Mochi / Cafe Ana), tech stack and contact.

So the primary ingestion path is **fetch these files directly** — zero Firecrawl credits,
deterministic, no scraping noise, and it is the source the owner already maintains. Firecrawl
(`/v2/map` + `/v2/scrape` with a JSON schema) stays as the **fallback** for pages not covered, behind
the existing `MIA_FIRECRAWL_API_KEY`.

### 1.4 Models

Production model ids stay in config, never hard-coded (AGENTS.md). Recommended values documented
in `.env.example`:

| Job | Model | Why |
|---|---|---|
| Owner agent loop | `gpt-5.6-terra` | frontier tool-calling at half the cost of Sol |
| Memory extraction / sales signals | `gpt-5.6-luna` | $0.20/1M in, full strict structured outputs |
| Transcription | `gpt-transcribe` | $0.0045/min, `languages` + `keywords` Hebrew levers |
| Embeddings | see §2, pending final research |

Tool calling on Chat Completions nests under `function` (`{"type":"function","function":{...}}`) —
different from the Responses API. `strict: true` on every function. The allowlist is enforced
**server-side on the returned tool name**, because `allowed_tools` is unverified on Chat Completions.

---

## 2. Memory architecture

*(Final embedding model + storage decision pending the fourth research report; the table shape below
is storage-agnostic and holds either way.)*

Four memory kinds, one table, discriminated by `kind`:

| Kind | Holds | Lifetime |
|---|---|---|
| `episodic` | things that happened: decisions, discussions, events, tasks | decays; superseded by newer episodes |
| `semantic` | stable facts: who Assaf is, businesses, skills, relationships | long-lived; updated in place |
| `working` | current projects, open tasks, active goals, pending decisions | short; closed when resolved |
| `preference` | how he wants Mia to work and communicate | long-lived; newest wins |

Tables (all new, all additive):

1. `memories` — text, `kind`, `category`, `importance`, `confidence`, source + provenance,
   `occurred_at`/`created_at`/`last_used_at`, `use_count`, `status` (`active|superseded|archived`),
   `superseded_by`, embedding + model + dim.
2. `knowledge_chunks` — website/business corpus. `category` ∈ personal, project, service, product,
   experience, skill, portfolio, business, current_work, contact. Carries `url`, `content_hash`,
   `fetched_at` for idempotent re-ingest and provenance.
3. `knowledge_entities` — people, companies, products, projects, technologies. Name + aliases +
   summary + attributes.
4. `memory_entity_links` — many-to-many between memories/chunks and entities.
5. `knowledge_gaps` — what Mia wants to learn, whether she asked, when it was answered.
6. `knowledge_sources` — one row per ingested source URL with content hash and last fetch, so
   re-ingest is idempotent and "what changed" is answerable.

**Retrieval score** (Generative-Agents shape): `w_sim·similarity + w_rec·recency_decay +
w_imp·importance`, then dedup, then a token budget. Weights are constants in code, tunable by eval.

**Write policy — what is worth remembering.** A cheap structured-extraction call over each closed
owner exchange returns candidate memories with kind, category, importance and an explicit
`supersedes` hint. Candidates below an importance floor are dropped. Before insert, the candidate is
matched against existing memories by embedding + entity; a near-duplicate **updates** the existing
row and bumps `confidence`, a contradiction **supersedes** it (old row → `superseded`, pointer set).
Nothing is written on every message.

**Hallucination prevention.** Retrieved context is injected with explicit provenance, and the owner
prompt keeps the existing rule that RESULT is phrased, never invented. A fact with no supporting
memory or chunk must produce "I don't know that yet" plus, at most, one question.

---

## 3. Work order

**A1 — Owner agent loop** (the "less gated" fix)
Typed read-tool registry over existing domain functions → Chat Completions tool loop →
allowlist enforcement → writes still to the approval queue. Falls back to today's classifier when
no model is configured.

**A2 — Brain**
Schemas → embedding port → memory store → retrieval/ranking → extraction/consolidation →
context assembly → gap questions. Wired into the owner loop as the always-on context layer.

**A3 — Knowledge ingestion**
`llms.txt` / `llms-full.txt` / `pricing.md` fetch → typed chunks by category → embeddings →
`knowledge_sources` provenance. CLI entry point for re-ingest. Firecrawl fallback.

**A4 — Telegram polish**
HTML formatter + escaping + section/date builders → `reply_parameters` / `link_preview_options` →
inline keyboards with `style` → `callback_query` handling + `answerCallbackQuery` +
`editMessageText` → `allowed_updates` verification → 4096-char chunking → 429 `retry_after`.

**A5 — Voice**
Transcription adapter fix per §1.1 → config validation surfaced on `/health` → `.env.example`
and runbook updates.

**C2 — Website handoff card** (from the earlier plan; kept, WhatsApp *sending* remains out of scope)
In-widget handoff card instead of a full-page redirect to a raw `wa.me` URL.

**A6 — Evals + docs**
Realistic memory/retrieval scenarios per the brief, then `docs/BRAIN_ARCHITECTURE.md`.

---

## 4. Risks

| Risk | Mitigation |
|---|---|
| Agent loop picks a privileged tool from untrusted text | Registry is read-only; writes go to the approval queue; tool name validated against the allowlist server-side; owner text stays data |
| Model outage breaks the owner console | Every layer degrades to the existing deterministic path |
| Memory poisoning from a prospect conversation | Only owner-channel turns feed owner memory; website chat cannot write owner semantic memory |
| SQLite/Postgres divergence on the new schema | Portable column types only; both engines exercised by the suite |
| Cost blowup from embedding every message | Extraction gated on importance; embeddings only on stored memories and knowledge chunks |
| Silent `callback_query` drop | Explicit `allowed_updates` + `getWebhookInfo` assertion + a startup check |
