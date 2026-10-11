# Owner OS authentication threat model — local implementation, 2026-10-11

## Trust and identity

This integrates the existing frozen AssafWeb OS UI with Mia's canonical PostgreSQL.
The standalone PWA receives no Mini App initData. The chosen identity proof is the
official Telegram Login callback, validated by the existing verify_owner_login:
HMAC over the original signed fields, bot-token-derived key, numeric owner allowlist,
600-second age limit and existing clock-skew rule. A username grants nothing.
See https://core.telegram.org/widgets/login-legacy.

Mia creates a separate opaque, expiring owner session after proof verification.
It does not extend or modify the Telegram proof. Defaults: 30-minute absolute expiry,
server-granted read scopes, workspace binding, no refresh token.
Only SHA-256 hashes of session/challenge credentials and canonical proof digests
are stored. Raw proof, names, browser credential and bot token are never persisted
in session tables. Logout revokes the current session; revoke-all advances a
monotonic owner revocation watermark under an owner advisory lock.

The Node BFF serves the six frozen frontend assets and forwards an opaque browser
cookie. It owns no session map, database, provider token or client secret.
The browser sees only public Telegram bot ID/name. All privileged DSNs and the
existing bot token remain on Mia's server.

## Adversaries and controls

| Threat | Control | Remaining boundary |
|---|---|---|
| Forged Telegram login, nonowner, username impersonation | Existing official HMAC verifier, numeric allowlist, strict bounded JSON; allowlist checked on every authenticated request | A compromised allowlisted Telegram account is an authenticated owner |
| Login CSRF, session fixation | Expiring prelogin challenge plus CSRF value, exact configured HTTPS Origin, atomic single-use challenge and proof consumption; cookie rotation | Official Telegram callback and BotFather domain require real browser acceptance |
| Session theft or replay | Host-only Secure HttpOnly SameSite=Strict cookie, hashed server records, absolute expiry, current allowlist/workspace/scope checks | Same-origin XSS can act as the user; CSP helps but cannot make a compromised origin safe |
| Cross-site requests | Exact Origin for every Mia endpoint; CSRF required on mutations; BFF checks Host and browser Origin/Fetch Metadata before forwarding configured Origin | TLS proxy must preserve headers and route only this app; no trust in arbitrary X-Forwarded-* |
| Duplicate/shadow cookies and proof in URLs | Duplicate owner-cookie rejection; auth querystrings rejected; callback POST only, RAM-only CSRF/proof | Infrastructure access logs must never record bodies or Cookie/CSRF headers |
| Reuse of consumed proof, concurrent login/revoke | Durable proof digest uniqueness, single-use challenge, transaction and owner lock; monotonic revocation epoch | Global auth-rate budgets can be exhausted for denial of service |
| Customer PII across unrelated sessions | Scope dependencies before data access; no public-session credentials accepted; reminder and receipt actor checks | CRM is one shared AssafWeb workspace for all numeric allowlisted owners, not a tenant-per-owner database |
| Broad read credentials, SQL/provider side effects on GET | Dedicated SELECT-only PostgreSQL role; effective ACL checks including PUBLIC, column rights, memberships, ownership, functions; REPEATABLE READ READ ONLY, no autoflush, rollback | Existing schemas may require ACL hardening before this strict posture is usable |
| Cache/log leakage | private,no-store on private pages/API/errors; no service worker, localStorage or persistent BFF cache; allowlisted DTO fields; sanitized error codes | Reverse proxy/CDN/APM and real Safari cache behavior still require release testing |
| Unknown source data masquerading as a zero or business fact | Explicit unknown/partial/unavailable coverage, stored-PG-only scope, unknown source watermarks and unverified dates; no counts or financial inference | PostgreSQL coverage does not establish Sheet/website/Leo ingestion completeness |
| Unapproved or duplicate actions | Independent default-off action gate; BFF blocks writes; existing immutable expiring Quick Lead approval, target revalidation and durable receipt; revision guards for reminders | No action enablement is authorized here |

## Quick Lead proof boundary

The session can outlive the 10-minute Telegram proof, but proposal and approval
still require the exact original signed proof bound to that session, freshly
reverified by the unchanged existing verifier. CSRF, numeric ID, scope and action
gate also apply. Substituting a newer proof into an older session fails; relogin
creates a fresh bound grant. Receipt recovery is authenticated and read-only, and
does not require repeating an expired approval. The frozen UI does not retain or
forward proof for actions, so action activation requires an explicit future design.

## Evidence and acceptance

Synthetic HMAC proofs exercise the server protocol; they do not prove Telegram
account consent, official popup behavior, BotFather registration or iPhone Safari.
Disposable PostgreSQL tests exercise actual role privileges, locks, transactions,
atomic proof consumption, revision conflicts, committed-receipt recovery and
the frozen Node client/DTOs through the BFF. Independent HEAVY review is required
after final fixes; its outcome is recorded in OWNER_OS_LOCAL_INTEGRATION.md.

No production deployment, live migration, customer/provider call, DNS change,
feature activation or merge is part of this work.

