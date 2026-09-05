# ADR-018 Website offers WhatsApp after first real friction

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat: move qualified/engaged website prospects to WhatsApp earlier)

**Context**
Live website widget looped the opening “יום רגיל בעסק” question. Full MEDDPICC discovery on the site is not how AssafWeb sells. Website is for starting the relationship, basic workflow, and first friction. WhatsApp is the continuation channel, not only the close.

**Decision**
`NextAction.OFFER_WHATSAPP` is a website continuation offer, distinct from owner `HANDOFF`. Website graph passes `channel="website"` into `select_next_action`. After workflow is known and pain is P2+ (identifiable friction), Mia offers WhatsApp in short conversational Hebrew and persists `whatsapp_handoff_offered`. Greeting / one vague sentence does not offer. Token issue stays `POST .../handoff`. Context must survive. Graph Lab `website_handoff_v1` is the shoe-store regression. `buyers_v1` stays unchannelled.

**Consequences**
Clinic “miss calls all day” on the website offers WhatsApp before reflect. Same transcript on Graph Lab / inbound WhatsApp still reflects. Demo scripted funnel offers WhatsApp first, then continues to meeting if the visitor keeps talking on the site.

**Alternatives considered**
Reuse `HANDOFF` — rejected; that is Assaf takeover + Telegram notify. Hard-code “after N messages” — rejected; measure empirically. Wait until every discovery field is complete — rejected; that was the live loop.
