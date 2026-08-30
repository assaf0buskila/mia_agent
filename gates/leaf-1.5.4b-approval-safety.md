# Gates: Approval and cancellation idempotency cleanup

Scope: Make every approval ingress enforce one binding contract, keep website proposal payloads structurally decidable, and avoid completing retryable cancellation failures.

- [x] G1: Text, Telegram callback, and Gmail approval decisions all enforce expiry, payload hash, action, risk, resource type, and resource-id binding before approval/send.
  EVIDENCE: `validate_pending_approval_binding` is the one shared expiry/hash/action/risk/resource validator. Text calls it for explicit and singleton lead rows; callbacks dispatch only canonical proposal/Gmail/website bindings; Gmail calls it before decision and before `send_draft`. Focused regressions mutate every binding field and retain the pending row.
- [x] G2: Website-edit proposals never persist truncated invalid JSON; accepted boundary and rejected oversize behavior are tested.
  EVIDENCE: `apply_website_edit_approval_policy` returns false before persistence when compact before/after JSON exceeds the 255-column contract; `test_website_approval_rejects_oversize_json_before_persist` proves no row exists.
- [x] G3: Cancellation claims complete only after durable success; confirmed retryable failures remain reclaimable and retry exactly once.
  EVIDENCE: cancellation now completes only after durable meeting persistence; any failed branch marks the operation failed. `test_cancellation_failed_persist_is_reclaimable_once` proves one failed claim is reclaimed and persisted on exactly the next attempt.
- [x] G4: Focused approval, Gmail, callback, meeting-change, and idempotency regressions pass with Ruff and diff-check.
  EVIDENCE: four passes recorded in `gates/evidence/function-cleanup-approval-safety.md`.
