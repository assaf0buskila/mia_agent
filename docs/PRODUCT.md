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

Owner Mia may search mail, calendar, leads, knowledge, and memory through an allowlisted capability layer. Writes, approvals, sends, Meta changes, and deletions stay on the deterministic policy path — never because a prompt asked nicely.

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

The existing WhatsApp-click briefing (paste-ready first line for Assaf) stays. Finalization does not duplicate it for the same lead if that briefing already fired.

When the website graph selects HANDOFF, Assaf gets a Telegram ping with the conversation. That ping is independent of `MIA_WHATSAPP_HANDOFF_SEND` (visitor WhatsApp send stays gated). The widget may still show a click-to-chat CTA. Visitor copy may claim a completed transfer only after Telegram accepted the ping. If the bot token, owner ids, or send fail, the widget says the transfer could not be completed — it does not invent a handoff.

## WhatsApp, Gmail, and the rest

WhatsApp is Assaf’s human inbox until official Cloud API inbound exists. Website may offer WhatsApp after real buying context. Mia does not reply on WhatsApp.

Gmail: read / draft. Send is approval-gated and off by default.
Calendar: free/busy + gated create/reschedule.
LinkedIn / Meta ads / GSC / GA4 / research: owner reads. Instagram is not a sales inbox.

## Hard never

Voice output. Auto-publish social. Self-edit production graph or prompts. Autonomous Meta budget/launch/pause. Sheets as system of record. ManyChat/Make as the brain. Cold Instagram DMs. Fake urgency or unsupported claims. Website visitors executing owner tools.

## Current vs later

**Now:** two graphs, shared core, capability/policy, voice on both, conversation finalization ping, owner Composio READs through the capability layer, Ask Mia UX preserved. Knowledge retrieve and conversation complete live on the graph nodes.

**Not this product:** dumping the Composio catalog into the model, a third graph for WhatsApp, TTS, auto-deploy.
