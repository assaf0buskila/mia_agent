# Handoff — 2026-09-11

Read this first. Everything here was checked against code, AWS and GitHub today.

## What Mia is (from the code)

- **Telegram owner loop**: `app/api/telegram.py` → `app/workers/telegram_owner.py` →
  `app/surfaces/owner.py` → `app/domain/owner/brain.py:answer_owner` →
  `app/graph/owner_agent.py:run_owner_agent` (tools from `app/tools/registries/owner_tools.py`).
  Writes become approval proposals; buttons resolve in `app/api/telegram.py:_handle_callback`.
- **Website sales loop**: `app/api/website.py` → `app/surfaces/site_v2.py:run_site_v2_turn`.
  The widget is `app/web/ask_mia.js`, served at `/v1/website/widget.js`.
- **CRM**: `app/services/crm_v2.py` (database is the record) plus the delivery worker
  `app/workers/crm_delivery.py`, started in-process by `app/workers/crm_runtime.py`
  (5s poll: Sheet import → Telegram lead brief → Contacts/Activity sync).

## 1. P0 — fix merged, NOT deployed (do this first)

Production (ECS `mia:58`, image `mia@sha256:7dbe3759…`, commit `e0ae94e`) fails every
CRM delivery cycle. CloudWatch `/ecs/mia` logged 5,574 "CRM delivery cycle failed"
warnings between 2026-09-10 23:18 UTC and 09:03 UTC, still ongoing. **Website leads have
not reached Telegram since the v2 rollout.** The jobs are durable and will drain after
deploy.

Root cause: `app/db/session.py` sets `autoflush=False`, but CRM tests ran with autoflush
on. Fixed in PR #55 (merged; master `a7a1b5b` has the same tree as `f2ed981`), with CI green.

Deploy (Assaf is starting Docker Desktop; its WSL engine was stuck):
1. `SHA=$(git rev-parse origin/master)`; confirm `gh run list --branch master` is green for it.
2. `git archive $SHA | docker build -f deploy/Dockerfile --build-arg MIA_BUILD_SHA=$SHA -t 535252061205.dkr.ecr.eu-north-1.amazonaws.com/mia:v2-$SHA -`
3. ECR login (`aws ecr get-login-password --region eu-north-1 | docker login …`), push,
   then read the pushed digest.
4. `uv run python scripts/deploy_ecs_revision.py --v2-release --image-uri <repo>@sha256:<digest> --sha $SHA`
   registers `mia:59`. It verifies the image label/env against the SHA.
5. `aws ecs update-service --cluster mia --service mia --task-definition mia:59`, then wait
   until stable. There are no migrations.
6. Re-pin scheduler `mia-due-scan` (currently pinned to `mia:58`) to `mia:59`. Keep
   `mia-reconcile` DISABLED.
7. Verify: `/health` `deployment.commit_sha == $SHA`; "CRM delivery cycle failed" stops
   (new log lines include `error=<Class>`); Telegram lead briefs arrive.

Rollback: `mia:58`.

Tip: in Git Bash, prefix AWS log commands with `MSYS_NO_PATHCONV=1` or `/ecs/mia` gets
mangled into a Windows path.

## 2. Cleanup — approved "full cleanup", in progress

Branch `claude/mia-v2-cleanup` (WIP checkpoint commit, **tests not yet green**). Done so far:
- Deleted the dead OwnerGraph layer: `app/agents/`, `app/channels/`, `tests/unit/test_vnext_graph_functions.py`
  and `test_vnext_owner_voice.py`. `run_owner_turn` had no callers.
- `brain.py`: removed `run_owner_turn`, `retrieve_owner_context`, the `graph_state` parameter
  and the stale docstring.
- Owner prompt grammar fix in `app/graph/owner_agent.py` ("…or a different account").

Still to do (all verified dead by three Sonnet investigators plus my grep):
- Fix tests that used the graph: `test_telegram_owner_graph.py` (keep `test_telegram_owner_entry_is_deferred_worker`
  and `test_preclaimed_owner_event_requires_the_exact_received_webhook`), `test_vnext_owner_text.py`
  (all graph, so delete), and `test_vnext_owner_retrieval.py` (port "explicit no-history skips retrieval"
  and "one turn retrieves once" onto `answer_owner`).
- Remove `langgraph` from `pyproject.toml` and run `uv lock`. Drop the `LANGGRAPH` entry in
  `app/core/capabilities.py:138`, `app/domain/policies/execution_policy.py:95` and
  `tests/unit/test_execution_policy.py:23`.
- Dead modules: `app/surfaces/published_facts.py` (plus the `two_state` VISITOR bits),
  `app/integrations/llm_compose.py`, `app/domain/owner/followups.py`,
  `app/domain/meetings/{booking,changes,copy}.py` (tests and `conftest.freeze_mia_clock` reference
  them), `app/domain/policies/{decision,failure_policy}.py`,
  `services/notifications.py:{render_conversation_summary,send_owner_telegram}`.
- Dead `LeadStore` methods (zero callers, even in tests): instruction/correction/reconciliation/v1
  website-session/WhatsApp-identity groups. Keep the ORM tables and migrations.
- Dead config flags: `knowledge_min_similarity`, `max_completion_tokens_site`.
- `app/surfaces/owner.py`: drop the unused `crm`/`gmail_port`/`talk` params. The worker builds
  Sheets/CRM/Gmail ports every turn for nothing.
- Keep `app/evals/predeploy/` (a manual pre-deploy gate).
- Then: full suite, ruff, PR, CI, deploy the same way as section 1.

## 3. Website widget — Assaf wants Mia as a BOX on the page

Mia is not on assafweb.com. Website repo `assaf landing page`, commit `3cce2b9`
("feat: add Leo WhatsApp website entry"), removed `<AskMiaWidget />` and mounted the Leo
WhatsApp widget. Assaf says he asked for Mia as an **embedded box on the website, not a
floating side/bottom launcher**. Leo was not mentioned, so leave Leo.

Plan:
- Add an inline mode to `app/web/ask_mia.js` that mounts into a page container (for example
  `<div data-mia-inline>`), always open, with no launcher.
- In the website, render that container. The hero `components/site/AiHeroChat.tsx` is a
  static scripted demo chat and is the obvious slot. **Ask Assaf** whether Mia replaces that
  demo or sits beside it.
- Browser-check desktop and mobile.

The widget itself works: tested at `https://mia.assafweb.com/v1/website/preview`, it gave a
good answer and asked one useful question.

## 4. Conversion-flow findings not yet fixed

- `site_v2.py`: `submit_lead` tool arguments (`name`, `next_step`) are ignored, and
  `state.next_step` is never set, so every lead brief says the default next step. A name given
  in free text is never captured. `business_context` is simply the visitor's first message.
- The website prompt (`_system_prompt`) has no guidance on when to invite contact; conversion
  relies on the model alone.
- Owner surface: when the model is unavailable, a greeting ("פה. מה צריך?") can reach the owner.

## 5. Docs

Assaf asked for simple docs based on the code. Rewrite README.md, MIA_V2.md, TASKS.md and
AGENTS.md short once the cleanup lands. TASKS.md still claims "no deployment has occurred",
which is false: v2 has been live on `mia:58` since 2026-09-11 02:20 +03.

## Watch out

- Another session has an uncommitted `crm_v2.py` flush fix plus a test in the main checkout
  (`assaf_agent/`, on master). PR #55 supersedes it; tell that session or discard it.
- Never read `.env`. Production secrets live in Secrets Manager.
