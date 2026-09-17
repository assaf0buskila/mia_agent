# MIA_V2.md — what Mia is, from the code

Updated 2026-09-11. Production (`/health`, 2026-09-14): commit `4b80f31`, `eu-north-1`.

## Two loops, one app

`app/main.py` is a FastAPI app with two routers and one background thread.

**Owner loop** — `/v1/telegram`. Numeric owner-ID auth → deferred worker
(`app/workers/telegram_owner.py`) → `app/surfaces/owner.py:run_owner_loop`
→ `app/domain/owner/brain.py:answer_owner` → the tool loop in `app/graph/owner_agent.py`.
Reads run directly. Writes (Gmail, Sheets, Calendar through Composio) become proposals —
distinct ID, immutable parameters, target snapshot, 24-hour expiry — with inline buttons
resolved in `app/api/telegram.py:_handle_callback`, which re-validates the target before
executing. Conversation history is stored automatically; lasting memory only on explicit
request. `MIA_KILL_SWITCH` pauses delivery; it does not stop the owner tool loop.

**Website loop** — `/v1/website`. The widget (`app/web/ask_mia.js`) creates a session
with a hashed credential (`X-Mia-Session-Credential` header) and posts messages to
`app/surfaces/site_v2.py:run_site_v2_turn`. The model sees public knowledge
(`app/brain`; sources are `llms.txt`, `llms-full.txt`, `pricing.md`) and the session —
nothing else. Its only tool is `submit_lead`. `next_action` is `answer` or
`contact_saved`. Every reply passes a narrative validator that fails closed on any claim
about saving or delivery; there is one rewrite attempt, after which the reply is dropped.

## Contact capture — what turns a chat into a lead

1. Server regexes (`_PHONE`, `_EMAIL`) find a phone or email in the visitor's own message.
2. Negation and example wording veto it.
3. A consent classifier — a separate model call with `tool_choice=required` — must return
   `affirmative` with verbatim evidence and a span that covers the server's value
   (digit-tolerant for phone, case-insensitive for email). If it does not, the value is
   kept as `pending_contact` and can be consumed by a later explicit confirmation, but
   only when Mia quoted that exact value back on the previous turn.
4. `CrmService.capture_site_lead` writes the contact, business context, summary and next
   step, plus one durable outbox job per destination, in the same transaction.

The model's `submit_lead` arguments contribute only `next_step`, and a `name` that appears
verbatim in the visitor's text. Capture runs **before** the model turn, so a next step
recovered from the tool call lands on the following brief, not the current one.

Budgets matter here: the site client is the Responses API with reasoning on, and
`max_output_tokens` bounds reasoning and visible output together. The classifier requests
600 (`_CONSENT_MAX_OUTPUT_TOKENS`) and replies 900; at 180 the classifier never got to
emit its tool call, which the adapter reports as a truncation, not an error. A dictated
number ("zero five two…", "אפס חמש שתיים…") is normalised to digits before extraction.

## Delivery and CRM

`app/services/crm_v2.py` owns contacts, identities, activity, sync snapshots and outbox
rows. `app/workers/crm_delivery.py` polls every 5 seconds under DB leases: Sheet import →
Telegram lead brief → Contacts/Activity sync. Google Sheets is an editable owner view:
three-way merge per field; a same-field conflict pauses for the owner. The worker logs
only failures, by design — success is silent, so proof of delivery is the Telegram message.

## Widget

Floating launcher by default. Inline mode: add `<div data-mia-inline>` to the page (or
`data-mia-mount="<selector>"` on the script tag) and the widget mounts there — always
open, no launcher, sized to the host, Escape and close inert. Selectors are ID-based, so
one instance per page. Preview harness: `/v1/website/preview`. There is no contact form:
free text through the consent classifier is the only capture path. The widget reads exactly
two `next_action` values, `answer` and `contact_saved` (`SITE_V2_ACTIONS`), and the server
cannot put any other name on the wire — `ask_contact`/`confirm_contact`/`handoff` and the
inline form that branched on them were retired in h2d.

## Providers

OpenAI first, Gemini fallback per purpose (`MIA_SALES_MODEL`, `MIA_GEMINI_*`). Voice in
through transcription; no TTS. One shared per-turn model deadline
(`llm_request_timeout_seconds`) covers consent, reply, validation and rewrite.

## Known gaps (2026-09-11)

- Website capture fix (reasoning consumed the classifier's output budget) is on
  `claude/mia-consent-observability`; until deployed, production still creates no lead
  from free text.
- The contact-turn reply can still blank to the greeting if the narrative validator
  rejects both the reply and its rewrite; that path is now logged.
- `/health` `integration_failures` can only decrease — its only writer was removed.
- `app/domain/meetings` exists with no callers; product decision pending.
