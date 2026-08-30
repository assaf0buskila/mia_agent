# Phase 1.5.4b approval-safety evidence

Date: 2026-08-28

## Behavior implemented

- One reusable `validate_pending_approval_binding` checks expiry, payload hash, action,
  risk, resource type, and resource id. Text approval, Telegram callback, Gmail decision,
  and the Gmail send boundary use it. Numeric Telegram authentication and the separate
  `MIA_GMAIL_SEND` flag are unchanged.
- Website edit payload persistence rejects a compact JSON proposal larger than the 255-byte
  storage contract rather than slicing it into invalid JSON. Valid bounded proposals remain
  decidable and hash-bound.
- Cancellation idempotency claims are completed only after `mark_meeting_cancellation_requested`
  succeeds. A local retryable failure is marked failed, which the existing store reclaims.
  Calendar provider deletion remains absent (R5 denial preserved).

## Four verification passes

1. Focused regression discovery exposed the callback's formerly tampered-hash expectation and
   a stale no-Sheets prompt assertion. Both owned tests were corrected to the current safety
   contracts; no production behavior was weakened.
2. `UV_CACHE_DIR=.uv-cache uv run ruff check app/domain/approvals.py app/domain/gmail_drafts.py app/domain/owner_callbacks.py app/domain/meeting_changes.py tests/unit/test_approvals.py tests/unit/test_owner_gmail_console.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_calendar_gate2.py tests/unit/test_calendar_booking.py`
   Result: `All checks passed!`
3. `UV_CACHE_DIR=.uv-cache uv run pytest tests/unit/test_approvals.py tests/unit/test_owner_gmail_console.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_calendar_gate2.py tests/unit/test_calendar_booking.py -q -p no:cacheprovider`
   Result: 170 passed. The run emitted only existing FastAPI/pytest-asyncio deprecation warnings.
4. `git diff --check -- app/domain/approvals.py app/domain/gmail_drafts.py app/domain/owner_callbacks.py app/domain/meeting_changes.py tests/unit/test_approvals.py tests/unit/test_owner_gmail_console.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_calendar_gate2.py tests/unit/test_calendar_booking.py`
   Result: exit 0; Git printed only CRLF conversion warnings, no whitespace errors.

## Callback complexity follow-up

The strict Ruff C901 baseline was 37 findings. This leaf initially added one (`resolve_owner_callback` at complexity 12), raising the count to 38. The resolver now delegates canonical binding selection to `_callback_binding_is_valid` and persistence dispatch to `_apply_callback_decision`; `uv run ruff check app/domain/owner_callbacks.py --select C901` now reports `All checks passed!`, returning the strict count to the baseline 37. The focused 170-test suite, owned Ruff, and scoped diff-check were rerun after this refactor and passed.

## Gmail Telegram callback follow-up

`resolve_owner_callback_result` now carries a newly approved Gmail draft id structurally, while
the existing text wrapper preserves direct-render callers. The Telegram webhook invokes the same
`execute_approved_gmail_send` boundary used by owner text only for that structured, fresh Gmail
approval. It therefore retains numeric owner authorization, all approval binding validation,
the kill switch/demo/write-flag checks, and truthful rendered outcome. The send boundary claims
`approval:{draft_id}:send:gmail_send` before calling the provider, completes only on success,
and fails the claim on a known provider failure; a replay sees the already-decided approval and
does not send again.

Regression evidence: `test_gmail_approve_callback_sends_once_when_enabled` drives two owner
callback webhooks with a fake Gmail port and observes exactly one send plus the truthful first
edit and already-decided replay. `test_gmail_invalid_callback_never_sends` parameterizes
tampered-hash and expired approvals, proving neither calls Gmail; existing callback binding
regressions cover wrong action/risk/resource type/resource id. After this follow-up:

- `UV_CACHE_DIR=.uv-cache uv run pytest tests/unit/test_approvals.py tests/unit/test_owner_gmail_console.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_telegram.py tests/unit/test_calendar_gate2.py tests/unit/test_calendar_booking.py -q -p no:cacheprovider` — 176 passed (only existing deprecation warnings).
- owned Ruff and `uv run ruff check app/domain/owner_callbacks.py --select C901` — passed.
- scoped `git diff --check` — exit 0; only CRLF conversion warnings.

## Deferred-send recovery follow-up

An approved Gmail callback replay now revalidates the complete pending-approval binding before
exposing its draft to the sender. This is deliberately different from a non-Gmail
already-decided callback: an approved Gmail row can be retried after a gate or a known provider
failure, while an approved row with a changed hash, risk, action, resource type, resource id, or
expiry is rendered invalid and never reaches Gmail. `execute_approved_gmail_send` evaluates the
kill switch, demo mode, disabled Gmail port, and `MIA_GMAIL_SEND` before taking its send claim;
those deferrals therefore leave a later replay eligible. A provider `False` marks the send claim
failed, allowing a later replay to reclaim it. A completed send claim returns a truthful
already-handled response and never calls the provider again.

`test_gmail_callback_recovers_deferred_and_failed_send_once` proves a callback first records
approval while sending is off, then recovers from one provider `False`, succeeds exactly once,
and does not duplicate on a fourth replay. `test_approved_gmail_send_deferrals_remain_retryable`
proves kill switch, demo, disabled port, and disabled flag all leave the same approved draft
sendable once the gate is cleared. Final rerun:

- `UV_CACHE_DIR=.uv-cache uv run pytest tests/unit/test_approvals.py tests/unit/test_owner_gmail_console.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_telegram.py tests/unit/test_calendar_gate2.py tests/unit/test_calendar_booking.py -q -p no:cacheprovider` — 177 passed (only existing deprecation warnings).
- owned Ruff, callback C901 check, and scoped diff-check — passed (diff-check emitted only CRLF conversion warnings).

## Scope boundary

The assigned production/test files were already dirty before this leaf. This work preserves
those changes and adds only the approval/cancellation hardening described above. No deployment,
product, architecture, or provider changes were made.
