# Mia — product

AssafWeb’s AI Growth & Sales Operator. Not a generic chatbot.

Two users, two trust levels, one application:

```text
                    MIA
                     │
            ┌────────┴────────┐
            │                 │
       OWNER SIDE         CLIENT SIDE
        Telegram            Website
            │                 │
       Owner Graph        Client Graph
```

## Owner Mia (Telegram)

Private digital employee for Assaf. Numeric Telegram user-id allowlist only. Username never grants access.

Natural conversation, not a command bot. Text and voice notes in; text out. No TTS.

Owner Mia may search mail, calendar, leads, knowledge, memory, explicitly authorized Google
Sheets, Search Console, and GA4 through an allowlisted capability layer. The exact configured
Mia spreadsheet is her managed CRM workspace: Mia creates or repairs its fixed CRM tabs and
headers and continuously projects leads, sources, follow-ups, meetings, deals, content metrics,
weekly KPIs, and her activity log into it. Assaf does not maintain this workbook. Postgres remains
the recoverable source of truth, so a damaged or edited Sheet cannot erase Mia's business memory.

Other allowlisted spreadsheets retain the narrow owner-request contract: an authenticated owner
may request a bounded value update or append, with no Drive discovery, delete/clear, formula work,
or Sheets read-back as Mia's internal truth. Other writes, approvals, sends, Meta changes, and
deletions stay on the deterministic policy path — never because a prompt asked nicely.

For reads, Assaf may paste an exact `docs.google.com/spreadsheets/d/...` URL. Mia extracts the
spreadsheet ID locally, verifies the same allowlist, and uses a bounded first-tab preview when no
A1 range was supplied. A link is convenience, not authorization.

Across every ACTIVE Composio toolkit connected to `MIA_COMPOSIO_USER_ID`, Owner Mia may search
for a tool on demand, fetch that exact tool's bounded current input schema, and execute a
preflighted read recognized by the conservative classifier. The full catalog is never inserted
into a prompt. Unfamiliar actions fail closed. Python classifies recognized side effects:
destructive tools are denied; writes, sends, posts, marketing actions, and unknown actions need
a named approval, idempotency, and audit workflow before they can execute. Client Mia never has
this surface. LinkedIn is the first additional named write surface: all active reads are available,
while an exact non-destructive post, comment, upload, or other side effect runs only after schema
validation and one-tap Telegram approval. Delete/remove/revoke and direct-message tools stay denied.

## Client Mia (Website)

Sales conversation on AssafWeb via the Ask Mia widget. Natural discovery, not a qualification form.

She explains published AssafWeb facts, asks useful follow-ups, identifies lead information when it appears, and recommends a next step. She does not invent prices, promises, capabilities, or delivery dates.

Customer Hebrew is 2nd-person plural / impersonal, native, and dash-free. Mix language with the visitor. One question at a time. Handoff on money / promise / complaint / human request.

## Voice

Voice is an input modality on both surfaces. Same graph as text after speech-to-text.

- Telegram: voice note → download → STT → OwnerGraph
- Website: mic in the open composer → upload → STT → ClientGraph

No separate voice brain. No speech synthesis.

## Website conversation → Telegram ping

When a meaningful website conversation is finalized, Mia sends Assaf one concise Telegram summary (name, business, need, problem, interest, timeline, budget only if discussed, qualification, meeting, recommended next step, conversation id). Missing fields stay empty. Do not interrogate the visitor just to fill the card.

Finalization is one service. Triggers now: visitor closes the widget or leaves the page (after at least one message), configurable inactivity (`MIA_WEBSITE_INACTIVITY_MINUTES`, default 30, via `mia-due-scan`), and meeting/handoff completing the thread. Idempotent on `conversation_id + final_summary_version`. Empty opens are not pinged.

The existing WhatsApp-click briefing stays: a paste-ready first line for Assaf,
plus a flag-only summary (workflow known, lead stage, next action, whether WhatsApp
was offered) and a heads-up that the visitor moved to him. It is not a transcript
or a PII dump. Notification
deduplication is scoped to the website conversation/session: graph handoff and click in the same
session do not double-ping, while a returning lead in a new conversation can alert Assaf again.

When the website graph selects HANDOFF, Assaf gets a Telegram ping with the conversation. That ping is independent of `MIA_WHATSAPP_HANDOFF_SEND` (visitor WhatsApp send stays gated). When a valid configured click-to-chat destination exists, the widget always exposes a one-tap WhatsApp action after session creation; reply handoff cards remain one tap as well. A tap opens WhatsApp immediately and sends the server-side handoff notification best-effort. Model-generated phone text is never converted into a destination. Visitor copy may claim a completed transfer only after Telegram accepted the ping. If the bot token, owner ids, or send fail, the widget says the transfer could not be completed — it does not invent a handoff.

## WhatsApp, Gmail, and the rest

WhatsApp is Assaf’s human inbox until official Cloud API inbound exists. Website may offer WhatsApp after real buying context. Mia does not reply on WhatsApp.

Gmail: read / draft; an exact draft sends only after one-tap Telegram approval and when the
production send flag is enabled.
Calendar: free/busy; exact create and reschedule actions require one-tap Telegram approval.
Search Console and GA4 answer AssafWeb KPI questions from APIs: traffic, users, sessions,
conversions, pages, clicks, impressions, CTR, position, and queries. LinkedIn reads use the active
Composio surface and approved non-destructive writes use the bound approval path. Instagram is
analytics-only: profile/media/insight reads are allowed; posting, messaging, and deletion are not.

## Hard never

Voice output. Auto-publish social. Self-edit production graph or prompts. Autonomous Meta budget/launch/pause. Sheets as system of record. ManyChat/Make as the brain. Cold Instagram DMs. Fake urgency or unsupported claims. Website visitors executing owner tools.

## Current vs later

**Now:** two graphs, shared core, capability/policy, voice on both, conversation finalization
ping, API-backed GSC/GA4 owner reads, Mia-managed CRM projections in the exact configured Sheet,
and explicitly authorized low-risk reads/updates for other allowlisted Sheets. Ask Mia UX is
preserved. Knowledge retrieve and conversation complete live on the graph nodes.

**Not this product:** dumping the Composio catalog into the model, generic side-effect execution
without a named approval contract, a third graph for WhatsApp, TTS, auto-deploy.
