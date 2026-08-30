# Gates: Owner notification fan-out and retry semantics

Scope: Use one multi-owner Telegram acceptance primitive and release workflow claims only on confirmed non-delivery.

- [x] G1: Finalization and due-scan notifications reach every allowlisted owner with per-recipient failure isolation and no first-owner-only path.
  EVIDENCE: `deliver_owner_telegram` fans out sorted numeric owners; finalization and due-scan call it directly. `tests/unit/test_hot_handoff.py` covers all-owner and partial-failure paths.
- [x] G2: Full confirmed send failure releases the workflow claim for retry; success/partial success retains the claim and remains duplicate-safe.
  EVIDENCE: `OwnerTelegramDelivery.confirmed_failure` is true only for explicit HTTP rejection or valid `{ok: false}`. Hot handoff, ordinary finalization, and due-scan claim durably per numeric recipient and notification instance: full or partial explicit rejection frees only rejected recipients; accepted and ambiguous recipients never resend; missing configuration consumes no recipient claim. Finalization keys the ledger by conversation and due reminders by local day, preserving returning-lead cards and retrying only the missing owner.
- [x] G3: The obsolete service-level inactivity scan bypass is removed; ClientGraph remains the only inactivity finalization caller.
  EVIDENCE: `app/services/finalization.py` has no inactivity scanner; `finalize_inactive_website_conversations` invokes ClientGraph per due row.
- [x] G4: Focused finalization, hot-handoff, due-scan, multi-owner, concurrency, and idempotency tests pass with Ruff and diff-check.
  EVIDENCE: 2026-08-28 repair: real LeadStore regressions prove a legacy completed conversation claim retains every recipient as accepted-or-ambiguous (no recipient row or transport call), same-local-day legacy due claim is retained, and older due claims permit the new daily ledger. Focused finalization/due suite: 29 passed; broad finalization/due/handoff/client/migration suite: 133 passed. Ruff owned paths and `git diff --check` passed; strict C901 remains a nonzero repository measurement with five pre-existing findings in `compile_client_graph`, `complete_turn`, `upsert_lead_review`, `mark_meeting_booked`, and `claim_operation`.
