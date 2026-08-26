# BUILD_STATUS

**Last updated:** 2026-08-26  
**Region:** eu-north-1 (ADR-019). Live host `https://mia.assafweb.com`. Live image **mia:16** (task `mia:18`). `mia:15` remains in ECR for rollback.

## Master CI test job (2026-08-26)

Not a product change. Master `test` job on SHA `0b6369d` (PR #1 merge) failed 3 tests. Fixes: freeze ADR-012 clocks on website booking + owner calendar freshness (wall date had moved past the Aug 20/21 fixtures); document `MIA_OWNER_AGENT_GEMINI_MODEL` in `.env.example`.

## Public Ask Mia origin bind + rate limit (2026-08-26)

Not deployed. Working tree / PR only.

`POST /v1/website/sessions`, `.../messages`, `.../voice`, and `.../handoff` now fail closed unless `Origin` is on `MIA_CORS_ORIGINS` (plus the public host so same-origin preview still works). Allowed origins are also capped per IP and per session. Browser CORS is unchanged; the widget still uses `credentials: 'omit'`. Config, widget.js, preview, and `/events` are not in this bind.

## Alive (v1)

Website sales + WhatsApp handoff tokens + Telegram owner (status digest on unclassified text) + Gmail ingest/summary + Calendar read/gated write + Sheets mirror + Meta/LinkedIn/research reads + STT + approvals + takeover + Postgres events + Graph Lab evals + Fargate host.

## Brain live (2026-08-24)

Shipped on image **mia:16**, task **mia:18**. `mia-migrate` ran on `mia:17` (exit 0) before the service moved. Knowledge ingest and Telegram webhook re-register ran as one-offs on `mia:18` (both exit 0).

ECS plaintext env (next to `MIA_SALES_MODEL`): `MIA_OWNER_AGENT_MODEL=gpt-5.6-terra`, `MIA_EXTRACTION_MODEL=gpt-5.6-luna`, `MIA_EMBEDDING_MODEL=text-embedding-3-small`. `/health` only checks that those names are set, not that OpenAI accepts them.

Live `GET https://mia.assafweb.com/health` (24 Aug): `status=ok`, `kill_switch=false`, `whatsapp_handoff_send=false`. `brain.owner_agent` / `embeddings` / `memory_extraction` all `{ready: true, missing: []}`. `brain.corpus.knowledge_chunks=31`, `memories=0`. Telegram webhook `allowed_updates` is `message`, `edited_message`, `callback_query`; `pending_update_count=0`. Owner conversation as Assaf is still the real proof that the model ids exist.

Telegram owner agent can call pinned **reads**: `gmail_summary`, `seo_snapshot`, `linkedin_snapshot`, `instagram_insights`, `research_search`, `ads_snapshot`, plus the existing briefs / leads / calendar / memory tools. Writes stay on the Python path.

`owner_integrations` is config readiness, not a live Composio ping. Live missing list: `MIA_LINKEDIN_ACCESS_TOKEN`, `MIA_META_ADS_ACCOUNT_ID`. LinkedIn member analytics stays Direct REST. Sheets write target stays `MIA_SHEETS_SPREADSHEET_ID`. Firecrawl is not Composio.

Live widget on `https://www.assafweb.com/` (23 Aug, Cursor browser — this repo has no Playwright): clinic path, three distinct discovery replies, no opener restart. WhatsApp offer and numbered meeting slots were not reached by turn three.

Verified: `uv run pytest tests/unit/test_owner_live_tools.py tests/unit/test_health.py tests/unit/test_brain_agent.py` **29 passed**.

Do not enable: WhatsApp send, Gmail send, Meta writes, IG auto-reply, TTS, Apify, Lambda, ManyChat.

## Website voice input (2026-08-23)

Ask Mia records from a mic **inside the open composer** (`#ask-mia-mic` next to Send). The 56px `שאלו את מיה` pill is unchanged. No TTS. `POST /v1/website/sessions/{session_id}/voice` transcribes via `TranscriptionPort` (16MB cap, same as Telegram) then `process_website_message`, so NBA/sales is unchanged. Kill switch fails closed before STT. Stored `askMia.sessionId` is reused; a stale session 404s once, the widget creates a new session and retries without wiping the visible transcript. Live on **mia:14**.

## This cleanup (2026-08-23)

Docs consolidated to `PROJECT_MAP` / `ARCHITECTURE` / short `PRD` / this file / `RUNBOOK` / `DECISIONS`. Historical MD moved to `docs/archive/`. ManyChat HTTP route unmounted (not a v1 channel). Instagram inbound+insights **kept** (ADR-015; not a v1 sales inbox). AWS secret names left in the box — unused ManyChat token documented, not deleted.

`Mia_AI_Growth_Sales_Operator_PRD_Build_Bible_v1.1.docx` was consolidated in place and synchronized with ADR-014–021. Production is now **mia:10** (ADR-022). Its 48 numbered sections remain stable for existing references.

Verified: `uv run pytest` **1914 passed**. `uv run ruff check app tests` **all checks passed**.

## Do not enable

Gmail send, Instagram prospect send (`MIA_AUTO_REPLY_INSTAGRAM`), Meta writes, instruction activation, R5. No unused env knobs for follow-up send, browser crawl, or tool discovery.

## This slice (2026-08-23)

Removed unused write knobs `MIA_AUTO_FOLLOWUP`, `MIA_BROWSER_AUTOMATION`, `MIA_DYNAMIC_TOOL_DISCOVERY`. Follow-up persist + due-scan stay. Research stays search-only. Tools stay pinned. No new image required — leftover ECS env names are ignored.

## Sales + owner conversation slice (2026-08-23)

Defect A (website discovery loop) and Defect B (one generic Telegram acknowledgment) are fixed in code and covered by tests. Shipped in **mia:11** — see Deployment below.

**Website sales.** `manual_step_established` no longer infers a manual step from a confirmed cost, so "we miss calls all day" now asks what is done by hand instead of reflecting work the prospect never described. `explicit_buying_intent` is a new persisted `SalesState` field: stated intent ("I want a website") reaches the WhatsApp offer without a workflow ladder, everyone else needs business + one manual step + real friction + `MIN_DISCOVERY_TURNS_FOR_WHATSAPP`. A direct meeting request now goes to `offer_meeting` after one qualifying question rather than requiring `fit == GOOD`. New `ObjectionKind.PRICE_QUESTION` answers "how much is it?" with scope instead of "what feels expensive?".

**Telegram owner.** `promote_unclassified_text_to_status` no longer collapses every unmatched sentence into the status digest; only greetings, status pings and text of three words or fewer. Real requests keep the Understanding Check. Two new read task types answer with data: `PENDING_APPROVALS` lists what is waiting (`app/domain/owner_reads.py`) and `WEBSITE_CONVERSATIONS` ranks website conversations by discovery depth. `app/domain/owner_followups.py` resolves follow-up references ("what's most interesting?", "check with him") against the lead id in Mia's own previous reply — deterministic, and approvals plus conversation-scope marking are explicitly never resolved from memory.

**Evals.** `buyers_v1` rebuilt to 19 multi-turn personas (shoe store, clinic, e-commerce, restaurant, real estate, one-word answers, Hebrew slang, mixed language, price question HE/EN, price objection, skeptic, privacy, vendor, committee, human request, ready to book, student, opt-out). `website_handoff_v1` adds greeting-only and human-request cases that must never reach the WhatsApp offer. The writing eval now scores English cases against English copy instead of Hebrew. Dataset size assertions became floors so coverage can grow but never shrink.

Verified: `uv run pytest` **1951 passed**. `uv run ruff check app tests scripts` **all checks passed**.

## Handoff send + owner memory + model routing slice (2026-08-23)

Shipped in **mia:11** — see Deployment below.

**WhatsApp handoff send (Phase 4).** No behavior change was needed: `MIA_WHATSAPP_HANDOFF_SEND` already defaults false and `should_skip_prospect_send` already required both the WhatsApp channel and a verified `MIA_BUSINESS` scope. What was missing was proof, so `tests/unit/test_whatsapp_handoff_send.py` now pins the exemption's narrowness: off by default, wrong channel stays staged, unverified scope stays staged, a verified handoff introduces Mia exactly once, an unknown contact stays silent, provider failure is not reported as sent, and the kill switch still aborts. The kill-switch backstop inside `process_inbound_texts` raises `PolicyDenied` on purpose — it aborts before commit so owner side effects do not persist — and the WhatsApp route returns before reaching it, so a live kill switch is a 200, not a 500. Sending stays disabled.

**Telegram owner memory (Phase 5b).** The daily brief now reports what the website produced (`format_website_headline`) while staying lead-id free. `top_website_lead_id` gives a drill-down question an anchor when no id was ever named, so "מה קרה היום?" → "מה הכי מעניין?" opens a real conversation; a pronoun ("check with him") may **not** use that anchor and still needs Mia to have named the lead. New `OwnerTaskType.LEAD_OUTREACH` answers "תבדקי איתו את זה" by confirming the lead and stating it will not send without approval, instead of the unclassified fallback. It is matched last, so it cannot shadow an approval, a takeover or a scope change, and it performs no write.

**Model routing (Phase 6/7).** `docs/MODEL_ROUTING_DECISION.md` and ADR-023 record the shape found in the code: three model call sites (sales paraphrase, Gmail summary, transcription), no cross-task router, and everything that decides or permits staying deterministic. The doc states plainly that no per-model benchmark exists, because the eval harness never calls a real model and measures no latency, tokens or cost. Fixed one real audit defect: `sales_model_label` reported `canned` for a Gemini-only deployment that was in fact paraphrasing. `run_gold_eval` and `load_gold_dataset` are now exported from `app.evals`.

**Widget.** Open-on-click was already correct and already pinned by `tests/unit/test_ask_mia_widget.py`: the panel is created hidden, there is no auto-open timer, scroll or exit-intent trigger, and the session is created only on first open.

Palette alignment is now done, from the live site rather than a guess. `www.assafweb.com` was scraped and its `:root` tokens read off the shipped CSS bundle: `--ink #061b35`, `--navy #2f5f93`, `--action #2563eb`, `--steel #7ba7d3`, `--mist #d9eeff`, `--paper #f8fbff`, `--line #2f5f9321`, font `Assistant`, `theme-color #F8FBFF`, light-only. Four widget values were off-token and were replaced: body text `#03101f` → `#061b35`, two `1px solid #c9e4f7` borders and one `#05265917` border → `#2f5f9321`, send button `#0c2440` → `#2f5f93`. `test_widget_uses_only_assafweb_palette_colors` now fails on any hex outside that token set (plus WhatsApp green `#25d366` and error red `#b00`), so the widget cannot silently drift from the site again.

No raster assets were added, and none are needed. The brand mark is an inline SVG built by `miaMarkSvg()` that mirrors the site's own `icon.svg` — ink field, white glyph, `#2563eb` accent bar — with a `'מ'` text fallback if `createElementNS` is unavailable. Standalone `app/web/assets/mia-mark.svg` and `mia-icon.svg` already existed and are pinned by `test_standalone_svg_assets_match_brand`.

**Rejection review (Phase 10).** Executed, not just read. Added the two cases that matter under the mode production actually runs (`auto_approved`, ADR-022) rather than only under shadow: an unknown WhatsApp number stays silent, and a `do_not_automate` scope set by Assaf overrides an already-verified business handoff. Confirmed the Telegram allowlist is numeric-only and rejects before the store is even constructed (`app/api/telegram.py:91`), and that `NextAction.HANDOFF` stops Mia, cancels the pending follow-up and pushes a Telegram brief from both the website and inbound paths. Open medium finding: `_notify_telegram` sends only to `sorted(owner_ids)[0]`, so a second owner id would never be notified. Single-owner today, so not fixed.

Verified: `uv run pytest` **1972 passed**. `uv run ruff check app tests scripts` **all checks passed**.

## Deployment (2026-08-23)

Deployed in two revisions. **mia:11** carried the sales/owner slice; `mia-migrate` ran as a one-off Fargate task on that revision first and applied `20260823_lead_sales_state_discovery_ledger.sql` (the five discovery-ledger columns, including `explicit_buying_intent`) with exit code 0 before the service was switched. **mia:12** carried the widget palette alignment. Both were verified by digest: the running task's `imageDigest` matches the locally built image, so the live code is provably the tested code.

`scripts/deploy_ecs_revision.py` registers a new task revision by swapping only the image tag on the active definition; it never touches the service. `scripts/run_ecs_migration.py` runs `mia-migrate` as a one-off task reusing the service's own network configuration and polls to a terminal exit code. `scripts/probe_live_website.py` replays the Defect A transcript against a live deployment.

Live verification on **mia:12**: `/health`, `/health/live`, `/health/ready` all 200; target group healthy; `kill_switch=false`, `whatsapp_handoff_send=false`, `email_send_policy=approval`, `R4=approval`, `R5=deny`. The Defect A transcript over the real API produced four distinct replies with zero opening-question restarts, progressing `deepen_pain → quantify → offer_whatsapp → reflect`, and offered WhatsApp on the third exchange. A handoff token was issued with a one-hour expiry and a `wa.me` URL. `widget.js` serves `Cache-Control: no-cache` with the aligned palette and zero off-token colours.

Telegram was verified behaviourally against the same code by probe, not against the live bot: the six Defect B messages produce six different useful replies, and the three-turn thread (brief → drill-down → instruction) carries one subject and gates the instruction on approval. Sending as Assaf's own Telegram account was not attempted, since it needs the owner's credentials; the running digest proves the same code is live.

## Human WhatsApp + owner prompts (2026-08-23)

Assaf ADOPT: do not chase WhatsApp Cloud API / Composio inbound. Composio cannot receive WhatsApp messages. Until official inbound exists, Mia stays silent on WhatsApp in every automation mode unless `MIA_WHATSAPP_HANDOFF_SEND` is explicitly true. Website click-to-chat opens Assaf with a human Hebrew prefill (no `mia1_` token in the customer box). Telegram gets a one-time briefing of the site conversation plus **השורה שלך** — one deterministic Hebrew first WhatsApp line Assaf can paste (no LLM; no prices/ROI/urgency/token/phone). Ask Mia launcher is a labeled 56px pill (`שאלו את מיה`) at the true bottom, closed until click; the competing AssafWeb WhatsApp FAB is removed. WhatsApp button inside the panel appears only after she offers the handoff.

## Conversation reasoning (2026-08-23)

Website sales prompt is `sales_reply_v7`: reason about the conversion turn, then write; customer Hebrew is mixed-gender plural, native, and dash-free. Mia answers only published facts, matches language, one question, hands off on money/promise/complaint/human request, and does not chase after a day of silence unless the opener is shop-approved. Assaf continues on WhatsApp, not Mia. Telegram owner phrasing is `owner_telegram_v2`, wired as a live paraphraser over typed owner results. Python still owns classification, tools, and approvals. The model does not receive a Composio catalog. Kill switch and missing keys stay canned. Live on **mia:14**.

The always-open chat on assafweb.com is the marketing `ai-hero-chat` in the Vercel hero, not a second bottom inbox. Clients see one bottom control: Ask Mia.

## Ask Mia bottom pill (2026-08-23)

Launcher is a site-matched pill (navy→action gradient, 56px, Assistant 800, visible `שאלו את מיה`) at `bottom: max(1.1rem, env(safe-area-inset-bottom))`. Panel still opens only on click. Transcript uses ChatBubble-style rows: Mia avatar + muted bubble, visitor avatar + navy sent bubble, bouncing dots while she replies. Widget CSS also hides leftover `.whatsapp-fab` on the host page. WhatsApp remains in header/footer CTAs. The embed stays vanilla JS — no React/shadcn inside `widget.js`. Live on **mia:14**. AssafWeb main + `rebrand/ai-solutions-studio` dropped the competing WhatsApp FAB.

## Mixed-gender customer Hebrew (2026-08-23)

Customer-facing Hebrew now uses 2nd-person plural / impersonal (`שאלו את מיה`, `ספרו`, `אתם`, `כתבו`, `לחצו`, `אפשר גם להקליט`, `בואו נמשיך`) so it addresses men and women. Owner Telegram stays masculine. Sales prompt pin is `sales_reply_v7`. Booking/reschedule retries use `נסו` / `השיבו` / `בחרו`. Customer Hebrew has no hyphen, en dash, em dash, or double hyphen. Canned sales lines, widget status, local preview, and WhatsApp paste lines were rewritten off English calques (by hand, off the table, in the picture, around inventory, defined WhatsApp). Mia answers only published facts, matches the customer's language, asks one question, hands off on money/promise/complaint/human request, and does not chase after a day of silence unless the opener is shop-approved. Live on **mia:15** (task `mia:16`).

## Website→WhatsApp owner brief (2026-08-23)

Telegram briefing is no longer a transcript dump. `format_website_whatsapp_brief` still names the lead, says Mia will not answer, lists known facts, and includes a short transcript. It now adds **השורה שלך:** with one paste-ready line from sales flags + last prospect turns (allowlisted topics only; prospect text is never copied). Kill switch / demo skip and once-per-lead persist stay. WhatsApp send flags stay off. Live on **mia:14**.

Verified: `uv run pytest` **2031 passed**. `uv run ruff check app tests scripts` **all checks passed**.

## Operator snapshot + website voice (2026-08-23)

Shipped in **mia:14**. No new RDS migration.

**Telegram.** Transcribed owner voice uses the same routing as typed text. Empty audio replies `לא תפסתי את ההקלטה. אני לא מבצעת כלום.` and does not dump a command menu. Two or more read intents, or a long unmatched sentence, return one `OPERATOR_SNAPSHOT` (daily counts, pending approvals, website conversations, hot leads) and end with `לא כתבתי כלום.` Greetings / ≤3 words / status pings stay `OWNER_STATUS`.

**Website mic.** `#ask-mia-mic` sits next to Send inside the open composer. Hint: `אפשר גם להקליט. זה יותר קל מלכתוב.` The 56px `שאלו את מיה` pill is unchanged. No TTS. Stale `askMia.sessionId` 404s once, then the widget creates a session and retries.

**Handoff line.** Telegram brief includes **השורה שלך** (deterministic, allowlisted topics only). First website→WhatsApp click notifies Assaf again; `has_owner_notification` is a real boolean.

Live verification: `/health` 200; `kill_switch=false`, `whatsapp_handoff_send=false`, R4 approval, R5 deny. `widget.js` has the composer mic, the shipped pill, and no `speechSynthesis`. Defect A probe: four distinct replies, WhatsApp on the third exchange, zero opening restarts. Rollback = image `mia:13`.

## Next slice

`AWS_RUNTIME` stays specified. Open medium finding unchanged: `_notify_telegram` notifies only `sorted(owner_ids)[0]`.

## Brain slice — memory, knowledge, owner agent, voice fix (2026-08-23)

Live on **mia:16** / task **mia:18** (see Brain live above). **WhatsApp was explicitly out of scope.** Full architecture:
`docs/BRAIN_ARCHITECTURE.md`. Decision: ADR-026.

**New package `app/brain/`.** `vectors` (portable base64 float32, exact cosine in stdlib —
3000×1536 in ~170 ms, no numpy dependency), `schemas`, `embeddings` (OpenAI/Gemini/Disabled/
Fake), `store` (`BrainStore`, separate from the 2900-line `LeadStore`), `retrieval` (BM25 +
cosine fused with RRF at k=60, then the Generative Agents relevance/recency/importance
re-rank), `extraction` (importance floor, then ADD/UPDATE/DELETE/NOOP reconciliation),
`knowledge` (heading-split ingestion with ancestry-aware categorisation), `context` (the
unified layer), plus `app/domain/owner_brain.py` as the wiring.

**Six new tables**, all additive, portable types only, one migration
(`20260823_brain_memory.sql`) that applies identically on SQLite and Postgres and is
idempotent. No `POSTGRES_ONLY` entry needed. pgvector deliberately not used — its SQLAlchemy
type is PostgreSQL-only and would make the suite exercise a different path than production.

**Owner agent loop** (`app/graph/owner_agent.py`, 16 tools in
`app/tools/registries/owner_tools.py`). All reads plus one owner-scoped memory write. The
allowlist is enforced server-side on the returned tool name. `DETERMINISTIC_TASK_TYPES`
keeps approvals, takeover, conversation scope, preferences, outreach and debriefs on the
original handlers. Empty `MIA_OWNER_AGENT_MODEL` → the old classifier answers, which is how
the test suite runs.

**Voice bug fixed.** The adapter sent `response_format=verbose_json` with `gpt-transcribe` —
a whisper-1-only format — and the caller swallowed the error into "לא תפסתי את ההקלטה", so
**every owner voice note was failing silently in production**. Parameters are now chosen per
model family (`gpt-transcribe` → `json` + plural `languages[]` + `keywords[]`; `whisper-1` →
`verbose_json` + singular `language`). Response parsing reads both shapes; the `confidence`
field, which exists in no documented OpenAI response, is now optional.

**Telegram.** `parse_mode=HTML` (3 escape chars under one rule, versus MarkdownV2's 18 under
three context-dependent rules — every lead id, email and decimal is a landmine there).
`reply_parameters` / `link_preview_options` replace the params removed in Bot API 7.0.
Inline approve/reject buttons with native styles, `callback_query` handling,
`answerCallbackQuery` first, message edited afterwards. `ALLOWED_UPDATES` is now always sent
explicitly — it is sticky server-side state, and a stale `["message"]` silently drops every
button press forever.

**Website handoff card** (C2). The widget no longer jumps the whole page to a raw `wa.me`
URL; it renders a card saying what Assaf will already know and opens WhatsApp in a new tab.

**Knowledge.** `uv run mia-ingest-knowledge` pulls `llms-full.txt`, `llms.txt` and
`pricing.md` — the agent-oriented corpus the site already publishes — and chunks them by
heading into 31 typed chunks across 9 categories. Idempotent on content hash.

**Config validation.** `GET /health` gained a `brain` block naming the exact env vars each
feature is still missing, plus live corpus counts.

Verified: `uv run ruff check app tests` clean. `uv run pytest` — 112 new tests
(`test_brain_memory` 30, `test_brain_agent` 14, `test_brain_voice_knowledge` 32,
`test_telegram_format` 36) all passing; no new failures against the pre-slice baseline.

**Known open:** `.env.example` could not be edited (the workspace blocks `.env*`), so
`test_deploy_secret_box.py::test_env_example_documents_settings_and_adapter_map` fails until
`docs/brain.env.example` is appended to it. The two calendar failures
(`test_story_calendar_no_double_book`,
`test_website_post_message_enriches_seeded_offer_meeting`) are date-dependent and
pre-existing — they fail on the baseline commit too.

## Sales-ladder fix + Composio resource discovery (2026-08-24)

Not deployed. Working tree only.

**Defect found by probing the live sales flow and fixed.** After Mia offered the WhatsApp
handoff, the ladder fell back to an unmet discovery rung — so the prospect said "כן נוח לי"
and got a reflection question instead of the handoff. `website_whatsapp_continuation_ready`
returns false once `whatsapp_handoff_offered` is set, and nothing carried the offer forward.
Accepting the offer now sets `owner_required`, which routes to `HANDOFF` with no schema
change. Three guards keep the broad affirmative tokens honest: a detected objection, a
conjunction ("but", "אבל"), and confirmation idioms ("that's right", "נכון") — the last
because "ok that's right" answers a *reflection*, and reading its "ok" as consent derailed
the clinic funnel one rung before the meeting offer. `website_handoff_v1` turn 4 expected
`reflect` and was corrected to `handoff`: it had encoded the defect as the expectation.

**Investigated and NOT a defect:** a price question sets `owner_required` and hands off, so
`ObjectionKind.PRICE_QUESTION` is unreachable on the website path. That is deliberate —
there is no public price list and Mia must never quote a number — and it is pinned by
`test_money_complaint_human_and_promise_hand_off`. Now also pinned from the other side in
`test_sales_ladder_defects.py` so nobody "fixes" it. BUILD_STATUS previously implied the
PRICE_QUESTION copy was live on this path; it is not.

**Composio resource discovery (ADR-027).** Opt-in, default false. When
`MIA_COMPOSIO_DISCOVERY=true` and leftover env is blank, GSC / GA4 / Meta ads ids
resolve from `GOOGLE_SEARCH_CONSOLE_LIST_SITES`, `GOOGLE_ANALYTICS_LIST_ACCOUNT_SUMMARIES`,
`METAADS_GET_AD_ACCOUNTS` (cached once per process). Explicit env always wins. Sheets
stays `MIA_SHEETS_SPREADSHEET_ID` — `GOOGLESHEETS_SEARCH_SPREADSHEETS` is not used as a
write-target picker. LinkedIn member analytics stays Direct REST + `MIA_LINKEDIN_ACCESS_TOKEN`.
Do not fake personal analytics with org share-stats. Probe first:
`uv run python scripts/probe_composio_discovery.py`. Live probe on 24 Aug listed 8
Active connections (`google_search_console`, `google_analytics`, `googlesheets`,
`instagram`, `linkedin`, `googlecalendar`, `whatsapp`, `gmail`) then all three
list executes returned **HTTP 404** (no `{data, error, successful}` payload).
Resolved: GSC no / GA4 no / Meta ads no. Leave `MIA_COMPOSIO_DISCOVERY=false`
until the execute slug/version 404 is diagnosed. Firecrawl stays required.

**Go-live that remains true:** ship a new image; run `mia-migrate` if brain tables are not
on prod RDS; one-off `uv run mia-ingest-knowledge`; `GET /health` and read `brain` counts
plus `owner_integrations.missing`. Assaf still sets Sheets id, LinkedIn token, Firecrawl.
GSC / GA4 / Meta ads ids stay until discovery is flipped after a clean probe.

Verified this slice: focused pytest on discovery / health / LinkedIn / GSC / GA4 / Sheets
/ owner live tools; `uv run ruff check` on touched files.

## Reconciliation noise: `handoff_expired` no longer counted as a failure (2026-08-24)

Live `/health` reported `ops.integration_failures: 10`. Traced from the code, not guessed:

`compose_handoff_text` deliberately keeps the token out of the customer's wa.me prefill
(ADR-024), so the customer never sends it back and `consume_handoff_token` is never
reached. `HANDOFF_TTL_MINUTES = 60`, and `list_expired_unconsumed_handoffs` has **no
retention window** — so every website→WhatsApp click became a permanent open
`handoff_expired` finding an hour later, and no later scan could ever close it. The counter
only grows, and it buries the finding that actually matters: `webhook_received` means an
inbound message was claimed and then dropped.

`evaluate_reconciliation` now takes `handoff_send_enabled`; the worker passes
`settings.whatsapp_handoff_send`. While that flag is false the `handoff_expired` scan is
skipped, and because `apply_reconciliation_policy` closes findings absent from the current
scan, the next `mia-reconcile` run **closes the stale rows and drops the live count**.
`webhook_received` and `sent_without_out` are untouched. When official Cloud API inbound
lands and the flag flips true, the scan resumes.

To see the live breakdown before/after (read-only, never repairs):
`mia-reconcile --inspect` as a one-off Fargate task — it prints each open finding's kind,
subject_key, channel and envelope_kind.

Verified: `uv run ruff check app tests scripts` clean. `uv run pytest` **2236 total, 2233
passed**; the 3 failures are the date-dependent calendar fixtures.
