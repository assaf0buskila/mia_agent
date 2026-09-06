# ADR-054 Website converts from known context

- **Status:** accepted
- **Date:** 2026-09-06
- **Assaf:** ADOPT (requested implementation of the supplied Website Conversion Rework Plan)

**Context**
The website's turn-number ladder could ask for business or repeated work that the
visitor had already explained. Internal action labels could pass an eval while the
actual replies repeated discovery. Contact confirmation could return a WhatsApp URL
that the widget did not display.

**Decision**
Supersede the website turn-count interpretation of ADR-053. Keep its WhatsApp/Baileys
decision intact. Website remains the deterministic site loop, separate from ClientGraph.

Store business, friction, value, contact and question-topic state in the existing
website session JSON. A substantive answer closes its question topic. Ask each core
discovery topic once, with a hard ceiling of three discovery questions. Known business
and friction move directly to a conditional value hypothesis and contact capture.
Product questions still receive published facts first. Discovery alone does not retrieve.

Python owns discovery questions and the contact CTA. The shared reply port has a
website-only value intent; the model may phrase that value but may not replace the CTA
or reopen discovery. Preserve the shared default behavior for other channels.

Use the existing messages API for inline structured phone-or-email capture; name is
optional. Confirmation shows a validated WhatsApp CTA immediately. Contact remains
required before CRM writes, owner notifications and WhatsApp handoff. Safe acquisition
context persists under the anonymous session, without creating a lead or storing URL
query credentials. Active sessions resume; completed or stale conversations start fresh.

The recorder first requests periodic chunks on all browsers, with a guarded fallback.
Container sniffing, truthful MIME, size caps, distinct errors and no TTS remain binding.

**Consequences**
Conversion advances from visitor context instead of message count. The eval records
every reply and checks visible value, closed topics, CTA visibility and accepted model
usage. The flagship nail-business conversation requires three successful runs. Mocked
checks do not establish paid-model quality or physical iPhone recording; release
evidence is tracked in `docs/WEBSITE_CONVERSION_REWORK.md`.

**Alternatives considered**
Another prompt-only fix would leave the policy and widget contracts inconsistent.
Moving to ClientGraph or introducing a runtime agent would widen scope without fixing
the missing state. Requiring both phone and email adds unnecessary contact friction.
