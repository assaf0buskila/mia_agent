# ADR-053 Site Mia sells on a ladder; WhatsApp waits on Baileys

- **Status:** accepted
- **Date:** 2026-09-04
- **Assaf:** ADOPT (value fast, ask by turn 4; Baileys on a spare number first)

**Context**
A real prospect described a gel-nail business, answered six discovery questions in a
row, was never told what AssafWeb would build for her, was never asked for a phone
number, and typed "נכשלת". Every substantive turn mapped to `answer` ->
UNDERSTAND_WORKFLOW, so turn 1 and turn 20 were identical. Separately, WhatsApp and
Instagram ran the full LLM turn and dropped the reply: `whatsapp_require_business_scope`
is on, `whatsapp_handoff_send` is off, `auto_reply_instagram` is off. Full cost, zero
delivery.

**Decision**
The site gets a ladder: learn, then name what we would take off their hands and ask if
they want to hear how, then one sharp question, then ask for contact and ping Assaf.
Frustration short-circuits it to the offer, because the one thing never to do when
someone says the conversation is failing is ask them another question. Contact is
gated on an expressed business need, so a student on a school project is never asked
for a phone number.

Instagram inbound is deleted; Assaf reaches Instagram through the Composio tools on
Telegram. WhatsApp gets a Baileys sidecar, built and not deployed, pointed at a spare
number first because the ban risk lands on the linked number.

Business claims left the sales prompt (`sales_reply_v11`): "there is no public price
list" became false the moment pricing.md was ingested, and "every launch includes a
month of guidance" is a commercial promise that belongs in published facts. Rules 17
to 19 cover an abusive visitor, regulated advice, and someone claiming to be Assaf.

**Consequences**
Site Mia reaches an offer by turn 4 instead of never. Instagram stops costing money.
The website still has no follow-up and no tone detection, and its session state still
dies on deploy, so she can re-ask for a phone number after a restart; a durable claim
was attempted twice and backed out both times because opening a second DB session mid
request breaks the outer transaction. That needs website session state written inside
the request's own transaction.

**Alternatives considered**
Ask for contact purely on turn count — rejected; the existing kill-switch test caught
that it would ask a student for a phone number. Swallow the WhatsApp send error to
stop duplicate deliveries — rejected; nothing else ever retries, so that customer
would go permanently unanswered, and a per-item commit fixes the duplicate without
losing at-least-once. Wire `MIA_WEBSITE_MEETING_FIRST` instead of deleting it —
rejected in ADR-052 and still true: the branch needs `channel == "website"`, and the
website does not run the client graph.
