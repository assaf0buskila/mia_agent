# Website conversation release — 2026-10-13

This change is based on the currently deployed revision `345d0a705d990b3ec6ea582febce43203fec907e`, preserving its owner OS, Telegram, CRM and reminder code. Master is older than production. The website feature branch is reviewed relative to that deployed revision; unrelated later OS publication is outside this release.

## Intended behavior

- Mia identifies herself consistently as Assaf's AI assistant.
- Ordinary answers are brief and answer the need before one optional question or invitation. Detail remains available on request.
- A greeting or acknowledgement continues the existing session rather than restarting discovery. Invitations and refusals are remembered in durable session JSON.
- Existing consent, contact provenance, narrative validation, delivery authority, idempotency and outbox contracts remain in force.
- Pure greetings, acknowledgements and status queries skip generation and retrieval. Mixed messages retain the full business/contact path.
- Website model calls reuse a website-only HTTP pool with request-local model/fallback state and per-request authentication and timeout.
- Text coalescing is shorter while keeping sequential bursts and immutable retry IDs.
- Stage timing logs contain durations, counts and bounded reasons; no visitor text or provider bodies.

## Verification limits

Tests must cover multi-turn context, mixed intents, refusal, contact/availability enrichment, narrative rejection, replay, transport isolation and burst ordering. Independent review and the exact feature revision's CI are release gates.

A shorter configured browser delay and fewer calls on pure-message paths are mechanically verifiable. Live response-time distributions and generative conversation quality require fresh runtime evidence. The existing Chrome transcript is historical observation, not proof of the new behavior.

A production synthetic conversation is separately permission-gated because it creates stored session/message rows. It must contain no contact details and must not generate an owner notification. No test may use real customer information.

## Release and rollback

Use a clean checkout at the independently reviewed, exact CI-green feature SHA. Immediately before release, confirm that production still matches the expected base; if another release advanced it, reconcile and reverify rather than overwriting it.

Follow the GCP operational steps: record current job state, pause jobs, create and verify a database backup, deploy the exact SHA, verify `/health` and `/health/ready`, then restore the recorded job state. No runtime model/feature setting change or new migration is part of this website change.

Keep the original `mia:345d0a705d99` source/image as the rollback candidate. Redeploy that retained exact revision if the website release fails acceptance; do not restore the database for this code-only change.