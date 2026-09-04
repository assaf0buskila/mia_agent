# Delivery and client-trust cleanup evidence

## Before and after counts

| Surface | Before | After | Evidence |
| --- | ---: | ---: | --- |
| `scan_inactive_website_conversations` definitions in `app/services/finalization.py` | 1 | 0 | ClientGraph owns inactivity traversal. |
| `knowledge_lookup` references in `app/graph/orchestrator.py` | 3 | 0 | Inner sales graph accepts serializable `knowledge_hits` only. |
| default/ambient `Principal.client` minting in ClientGraph | 1 | 0 | Graph signature requires and verifies a client `principal`. |
| direct workflow delivery callers of the shared primitive | 0 | 2 | Finalization and due-scan call `deliver_owner_telegram`. |
| durable hot-handoff delivery claims | 1 global `(kind, lead)` claim | 1 claim per `(kind, lead, numeric owner)` | `OwnerNotificationRecipientClaimRow` and its additive migration preserve successful/ambiguous recipients while freeing explicit rejections. |

## Four passes

1. Implemented one bounded multi-owner delivery result with partial-success retention, response-confirmed full-failure retry release, and ambiguous transport/malformed-2xx claim retention. A kill-switched finalization now exits before claiming, and a website handoff uses its hot-handoff result as the one notification path for the turn.
2. Re-read against ADR-038/041/042: inactivity remains graph-owned, principals are entry/caller minted, and health is configuration-only with no secret or provider call.
3. Defect hunt found the claim-release assertion must cross the transaction boundary; the regression commits before checking the released claim. It also found the package import cycle, resolved by making hot-handoff's shared-service import lazy, and closed hot-handoff's previously lost ambiguity state.
4. Free polish: removed the obsolete inner lookup hook, guarded client retrieval before any kill-switch capability call, and preserved the bool compatibility wrapper for non-workflow notification callers.
5. HEAVY rereview repair: replaced the hot-handoff's global delivery gate with a recipient claim ledger. Missing token/owners/text performs no claim, explicit recipient rejection releases only that recipient, and accepted or ambiguous recipients remain claimed. This is additive schema only (`migrations/20260828_owner_notification_recipient_claims.sql`); no backfill is safe because legacy global claims contain no recipient acceptance information.
6. Full-suite follow-up: local `OwnerNotificationRow` remains an owner-inbox record even when Telegram is unavailable. It is upserted before the no-attempt transport branch; recipient claims are still not consumed, and the real-store regression proves later valid configuration sends once.
7. Final HEAVY repair: recipient claims now include a stable notification instance key. Website finalization uses its conversation id and due reminders use their local-day key, so a returning lead's next conversation can deliver independently while accepted/ambiguous recipients remain protected and only explicit rejections retry. The unshipped additive migration was amended with `notification_key`; `test_migrate.py` proves migration enumeration/application. Hot-handoff policy now runs before every state, follow-up, inbox, claim, or transport effect.

## Commands

- `UV_CACHE_DIR=.uv-cache uv run pytest -p no:cacheprovider --basetemp .pytest-delivery-final-review` over 14 delivery/client-trust files plus `test_comm_operating_model.py` and `test_migrate.py` — 231 passed, 119 warnings.
- `UV_CACHE_DIR=.uv-cache uv run ruff check ...owned paths...` — All checks passed.
- `git diff --check` — exit 0.

## Fourth-review notification and returning-session repair

Compatibility is dual-read, with no recipient backfill. A legacy
`owner_notification_claims` row for the exact finalization `(kind, lead,
conversation)` is retained as accepted-or-ambiguous delivery and blocks every new
recipient claim and transport call. A new conversation has no matching legacy row, so
its per-recipient ledger remains independent; explicit rejections still release only
that new recipient. Due reminders need a local-day key that legacy claims did not
have: a dated legacy claim blocks only that same local day, while an older or
unparseable claim cannot suppress future daily reminders.

Website visitor-message eligibility and inactivity aggregation now bind the canonical
message to both `lead_id` and `conversation_id`, matching the session identity used by
finalization. An old messaged session therefore cannot make a new empty identity
eligible.

Evidence, 2026-08-28:

- Focused real-store finalization/due regression selection: 29 passed.
- Broad finalization/due/handoff/client/migration selection: 133 passed.
- Ruff owned paths: all checks passed; `git diff --check`: exit 0.
- Strict C901 measurement remains nonzero only for five pre-existing functions in the
  broad owned modules; this repair adds no C901 finding.
