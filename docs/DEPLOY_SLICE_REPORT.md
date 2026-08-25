# Deploy report — visitor knowledge, meeting-first exit, funnel, multi-owner

**Date:** 2026-08-24
**Branch:** `claude/mia-product-feedback-0bfc90` (worktree `mia-product-feedback-0bfc90`)
**ADRs:** ADR-028, ADR-029
**Tests:** `uv run ruff check app tests scripts` → clean. `uv run pytest` → **2287 passed, 1 failed** (the one failure is a workspace restriction, see §5).
**Live now:** `mia:16` (task `mia:18`) — has the brain, has **none** of this.

Read §1 first. It is the fix for "Mia does not use Composio tools on Telegram", and it is
**config, not code** — it ships independently of everything else here.

---

## 1. Do this first: the owner agent has never run in production

Symptom Assaf reports: on Telegram Mia will not reach Gmail or the other connected tools.

This is **not** an allowlist problem. There are already 22 registered owner tools,
including `gmail_summary`, `seo_snapshot`, `linkedin_snapshot`, `instagram_insights`,
`research_search` and `ads_snapshot`. They are unreachable because the agent loop itself
never runs: `owner_agent_ready()` only checks that the model **string** is non-empty, so a
configured-but-uncallable model is indistinguishable from a healthy one, and every turn
silently degrades to the pre-brain keyword classifier. Full analysis:
`docs/TELEGRAM_SLICE_REPORT.md` §1.

```bash
uv run python scripts/probe_owner_agent.py
```

Read-only, never prints the key. Expected: `gpt-5.6-luna` → `CALL ok`, `gpt-5.6-terra` →
`CALL FAILED http=404`. Then set on the ECS task definition:

```
MIA_OWNER_AGENT_MODEL          = gpt-5.6-luna
MIA_OWNER_AGENT_FALLBACK_MODEL = gpt-5.6-terra
```

`gpt-5.6-luna` is what the website sales path already uses successfully. Terra second means
it is picked up automatically if the project model permission is ever granted (terra costs
10x luna, which is the most likely reason it is restricted).

**Caveat, stated plainly:** OpenAI does not document what error a blocked model returns, so
project model permissions is the strongest documented hypothesis, not a verified fact. The
probe settles it in one command.

---

## 2. Migration is required before the new image serves traffic

`20260824_lead_sales_state_meeting_exit.sql` adds `lead_sales_state.meeting_exit_offered`.
Additive, defaults false, idempotent, applies identically on SQLite and Postgres.

Run it as a one-off on the NEW task revision, before switching the service:

```bash
uv run python scripts/run_ecs_migration.py --task-definition mia:<new-revision>
```

Poll to a terminal exit code and confirm exit 0, exactly as the brain slice did with
`mia:17` before moving the service to `mia:18`.

---

## 3. Deploy sequence

```bash
docker build -f deploy/Dockerfile -t mia:<tag> .
```

Push to ECR, then register the revision (this script swaps only the image tag on the active
definition and never touches the service):

```bash
uv run python scripts/deploy_ecs_revision.py --tag <tag>
```

Run the migration from §2 on that revision, then switch the service. Verify by digest: the
running task's `imageDigest` must match the locally built image, so the live code is
provably the tested code.

No `mia-ingest-knowledge` re-run is needed — the corpus is unchanged. The knowledge that
already sits in `brain.corpus.knowledge_chunks` is exactly what the website path now reads.

---

## 4. What to verify after the switch

```bash
uv run python scripts/probe_live_website.py --base https://mia.assafweb.com
```

- `GET /health` → `status=ok`, `kill_switch=false`, `whatsapp_handoff_send=false`,
  `email_send_policy=approval`, R4 approval, R5 deny.
- **Website answer-then-ask:** ask the widget a factual question the site publishes. Mia
  should answer it in one sentence and then ask one question. Before this slice she would
  have ignored the question and asked the next ladder rung.
- **Meeting-first exit:** run a discovery conversation to the continuation gate. It should
  now produce `offer_meeting` with calendar slots, not `offer_whatsapp`. Decline it and the
  next continuation-ready turn should still offer WhatsApp.
- **Owner brief:** send "מה קרה היום?" on Telegram. The brief now carries a funnel line
  (`משפך באתר ...`) and an engine line (`מנוע (מאז ההתחלה): ...`). If the engine line shows
  a canned count close to the total, the model is failing silently — that is the whole
  reason the line exists.

**Rollback:** image `mia:16`. Or, without a deploy, set `MIA_WEBSITE_MEETING_FIRST=false`
to restore the WhatsApp-first exit while keeping everything else.

---

## 5. Known gap that cannot be fixed from this workspace

`tests/unit/test_deploy_secret_box.py::test_env_example_documents_settings_and_adapter_map`
fails. This workspace denies all reads and writes on `.env*`, so `.env.example` could not be
updated. It is missing:

- `MIA_OWNER_AGENT_GEMINI_MODEL` — **pre-existing at HEAD**, from commit `c915f9f`, which
  hit the same restriction and documented it in `docs/brain.env.example` instead.
- `MIA_WEBSITE_MEETING_FIRST` — added by this slice, documented in the same place.

Both are written up in `docs/brain.env.example`. Appending that file to `.env.example`
closes the test:

```bash
cat docs/brain.env.example >> .env.example
```

---

## 6. Not in this slice

The Telegram owner tool-surface expansion — live Gmail list/search/read-thread, Sheets read,
GA4 as a first-class tool, and approval-gated Gmail drafts. Assaf approved the scope ("reads
plus gated drafts") but the work was not started.

Two things to carry into it:

- `GmailPort` has exactly one capability today, `fetch_message(message_id)`, pinned to
  `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`. There is no list and no search, so `gmail_summary`
  can only summarize threads the `GMAIL_NEW_GMAIL_MESSAGE` webhook already ingested. Mia
  genuinely cannot go and look at the inbox until a read tool is added.
- Drafting is already permitted by `docs/PRD.md` ("read / classify / summarize / draft").
  **Sending is not.** `MIA_GMAIL_SEND` stays false and Gmail send stays on the "do not
  enable" list; the approval gate is on creating the draft, never on a send. The draft write
  must also stay off the model's tool loop — it belongs in `DETERMINISTIC_TASK_TYPES` with
  the other state-changing intents, per ADR-026.

Do **not** solve this by dumping the Composio catalog into the model. The no-catalog rule is
what stops untrusted text — an email body, a scraped page — from selecting a privileged
tool. Widen the typed allowlist instead. (The 24 Aug probe also had all three Composio
discovery executes returning HTTP 404, so a catalog approach would not work today anyway.)
