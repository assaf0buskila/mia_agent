# ADR-049 SITE Mia answers first; contact only for Assaf or Sheet

- **Status:** accepted
- **Date:** 2026-09-02
- **Assaf:** ADOPT (ten site demo upgrades)

**Context**
The live site loop asked for a phone as soon as a visitor stated a need. That
blocked answers, invented nothing useful on price, and hung the widget on
voice failure. Assaf asked for ten SITE-only demo upgrades: off-topic joke
then AssafWeb hook, language match, answer first, confirm a number already in
session, complaint stop-sell, published prices only, honest bot line, voice
fail stays in chat, burst stitch, and honest tool naming.

**Decision**
`run_site_turn` uses `app/surfaces/site_policy.py`. Phone or email is asked
only when the next step is Assaf or the Sheet. Prices are quoted only from
assafweb.com published facts. No weather API. No invented JSON-LD or Search
Console. Voice STT failure returns a chat reply and keeps the session open.
Rapid widget sends debounce into one thought; the server also stitches a
short burst window.

**Consequences**
Need statements get an answer, not an immediate contact ask. CRM and Telegram
ping still require phone or email. A seen visitor turn always gets a visible
reply. Toolkit questions are answered before weather or a sell. Missing
metrics are said missing, never invented. Leftover NBA tests that expected
`ask_contact` after a need no longer describe the live path.

**Alternatives considered**
Keep identify-then-ask-contact on the first need — rejected; Assaf asked to
answer first. Call a weather or GSC API from the widget — rejected; that
invents or over-scopes SITE Mia.
