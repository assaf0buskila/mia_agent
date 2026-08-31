# Phase 1.6 local release evidence

Date: 2026-08-31 (Asia/Jerusalem)

Scope: frozen current-tree verification before commit, push, deployment, or live owner acceptance. This artifact contains no credential, account id, ARN, provider payload, transcript, KPI value, or Sheet cell content.

## Mechanical and behavioral gates

- Full pytest: **2,577 passed**.
- Repository Ruff (`app tests scripts`): **passed**.
- Widget JavaScript syntax: **passed**.
- Public origin-bind assertion: **passed**.
- Deterministic evals: **273/273 passed** across all ten families, including calendar and routing 20/20.
- Git diff check: **passed**; only Windows line-ending notices were emitted.

## Independent release review

The final fresh HEAVY reviewer returned **RELEASE PASS** with unresolved **P0=0, P1=0, P2=0**. Its direct evidence included:

- GA4 adversarial schema matrix **10/10**: current four-metric data requires both exact semantic header sequences; absent, partial, reordered, extra, or mismatched headers fail closed. Exact four-metric, historical headerless two-metric, and legitimate typed rowless shapes remain accepted.
- Telegram voice failure suite **44 passed**: a definite failure to send the fixed content-free voice-error reply remains retryable; a later retry sends once and the durable outcome stays deduplicated.
- Notification-focused suite **128 passed** plus exact blocker selections **9 passed**: graph hot handoff and WhatsApp CTA share one per-recipient delivery key across both orders and a forced database race; legacy claims are conservative; only rejected owners retry; pre-send commit failure sends nothing; a first rejection-release commit failure recovers durably.
- Combined Phase 1.6 suite **360 passed** and the complete **2,577-test** tree passed in the reviewer session.

## Explicit non-claims

- This is not deployment evidence.
- No live Telegram voice note exercised this SHA.
- No live website CTA click exercised this SHA or proved receipt in Assaf's Telegram client.
- No post-deploy Composio/Sheets/GSC/GA4 adapter probe exercised this SHA.
- No Google Sheet write was made by this local gate.
