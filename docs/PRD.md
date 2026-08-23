# Mia — living product contract

Short contract. Full Bible: `Mia_AI_Growth_Sales_Operator_PRD_Build_Bible_v1.1.docx`. Architecture: `docs/ARCHITECTURE.md`. Wiring: `app/core/capabilities.py`. Decisions: `docs/DECISIONS.md`. Model routing: `docs/MODEL_ROUTING_DECISION.md`.

When a provider fact, channel contract, or ADR changes, update **this file** in the same turn — keep it short. Do not paste Bible chapters here.

## Product

Mia is AssafWeb’s AI Growth & Sales Operator. She turns attention into qualified conversations, meetings, and pipeline. She is not a generic chatbot.

## v1 channels

| Surface | Role |
| --- | --- |
    | Website | Primary customer sales. One bottom control: visible Ask Mia pill (opens on click). Mic lives in the open composer (voice in, STT, same sales graph). No TTS. No competing WhatsApp FAB. Chat uses sent/received bubbles with avatars. Customer Hebrew is 2nd-person plural / impersonal (men and women), native, and uses no hyphen/dash. Mia answers only published shop facts, one question at a time, hands off on money/promise/complaint/human request, and does not chase after a day of silence unless the opener is approved. WhatsApp offer after real context (ADR-018), typically 3–6 meaningful exchanges. |
| Telegram | Private owner control. Numeric allowlist. Voice in, text out. No TTS. Free conversation over an allowlisted **read-only** tool loop (`owner_agent_v1`, ADR-026) with long-term memory and website knowledge; writes and approvals stay on the deterministic path. One-tap approve/reject buttons. `parse_mode=HTML`. With `MIA_OWNER_AGENT_MODEL` empty it falls back to the `owner_telegram_v2` classifier (ADR-025). |
| WhatsApp | Gated until official Cloud API inbound (ADR-024). Click-to-chat opens Assaf. Mia sends him a Telegram briefing with known facts, a short transcript, and a paste-ready first line. She does not reply on WhatsApp while `MIA_WHATSAPP_HANDOFF_SEND=false`. |
| Gmail | Read / classify / summarize / draft. Send approval-gated (`MIA_GMAIL_SEND` default false). |
| Calendar | Free/busy + gated create/reschedule (`MIA_CALENDAR_WRITE`). |
| LinkedIn | Profile via Composio (`LINKEDIN_GET_MY_INFO`). Personal post analytics is Direct REST + `MIA_LINKEDIN_ACCESS_TOKEN` (ADR-009). Composio has org share-stats only — do not fake member analytics. No posts/DMs. |
| Meta ads | Insights read. Writes need Assaf approval (R4). |
| Instagram | Analytics/insights may remain. **Not** a v1 autonomous sales inbox. |
| ManyChat | Not a v1 channel (ADR-021). Route unmounted. |

## Brain (ADR-026)

Long-term memory (episodic / semantic / working / preference), website + business knowledge,
hybrid retrieval, and extraction that decides what is worth remembering. Owner memory is
written **only** from owner-controlled surfaces; a website visitor can never write it.
Supersede, never delete: an outdated fact keeps its row with a `superseded_by` pointer.
Architecture and debugging: `docs/BRAIN_ARCHITECTURE.md`.

## Hard never

Voice output. Auto-publish social. Self-edit production graph/prompts. Autonomous Meta budget/launch/pause. Sheets as SoR. ManyChat/Make as the brain. Cold Instagram DM spam. Fake urgency.

## Feature wiring

A capability is **wired** if it has a typed port and appears in `app/core/capabilities.py`. It is **alive** only if a test proves the path. `AWS_RUNTIME` is **specified** (host live; no `app.infra`). `MANYCHAT` is **specified** (deferred, not mounted).

## Production defaults

- `MIA_ENV=prod`, `MIA_DEMO_MODE=false`, `MIA_KILL_SWITCH=false`
- `MIA_AUTOMATION_MODE=auto_approved` (ADR-022 live sales test). Instagram send still needs `MIA_AUTO_REPLY_INSTAGRAM`
- `MIA_WHATSAPP_SENDER=composio`, `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=true`
- `MIA_WHATSAPP_HANDOFF_SEND=false`. WhatsApp prospect send stays off in every automation mode until this flag is true. Official Cloud API inbound is the precondition. Until then the website click opens Assaf and Telegram gets the briefing (ADR-024).
- `MIA_CALENDAR_WRITE=true` (Assaf ADOPT). Gmail send / Meta write / IG auto-reply **false**. No env knobs for follow-up send, browser crawl, or tool discovery.
- Region `eu-north-1`. Host `https://mia.assafweb.com`. Live image `mia:15`. Working tree `/health` adds `brain` + `owner_integrations`. Those keys are not on mia:15 yet. `MIA_COMPOSIO_DISCOVERY` defaults false. GSC / GA4 / Meta ads ids are leftover env, optional only when discovery is on. `MIA_SHEETS_SPREADSHEET_ID` and `MIA_LINKEDIN_ACCESS_TOKEN` stay required. Firecrawl is not Composio.
- Widget colours are the scraped `www.assafweb.com` `:root` tokens and nothing else; `test_widget_uses_only_assafweb_palette_colors` fails on drift. The brand mark is inline SVG, not a raster asset

## Tests

`uv run pytest` and `uv run ruff check app tests`. Do not weaken tests to pass cleanup.
