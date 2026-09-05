# ADR-050 Two-state tools and Tel Aviv calendar write gate

- **Status:** accepted
- **Date:** 2026-09-02
- **Assaf:** ADOPT (chat: two states + tools, Tel Aviv calendar gate)

**Context**
Owner Telegram and the website were sharing one product posture. Assaf asked for
two states: Dude + full house Composio on Telegram, seller + few tools on the
site. Identity must gate ping, not product answers. Calendar writes were able
to accept any titled event after approval, including weather.

**Decision**
Keep two loops. Owner tools are the house Composio set: say the tool before
numbers; Instagram names post and account; GSC/GA include dates; Sheets stay
Contacts+Activity; Gmail read/draft with `gmail_send` false; LinkedIn read no
post; WhatsApp drafts to Assaf never fire at a lead; parallel tool calls;
timeout says `still checking`. Visitor tools are published facts and
`search_knowledge` only. Calendar write is allowed only when the event is a
meeting near Tel Aviv, 09:00–17:00 Asia/Jerusalem, and the slot is empty.
Otherwise ask Assaf. Weather never becomes a meeting.

**Consequences**
Site answers published product facts without a phone. Ping/CRM/WhatsApp still
need identity. Owner calendar grammar that omits Tel Aviv or names weather
asks Assaf and does not create an approval. Visitor principals cannot execute
owner tools.

**Alternatives considered**
Identify-then-sell before any product answer — rejected; Assaf said identity
before ping, not before product answers. Auto-write any approved calendar
title — rejected; weather chats must not become meetings.
