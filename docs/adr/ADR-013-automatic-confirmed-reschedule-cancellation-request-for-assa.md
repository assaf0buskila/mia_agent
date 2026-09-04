# ADR-013 Automatic confirmed reschedule; cancellation request for Assaf

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT (automatic confirmed reschedule; cancellation request for Assaf)

**Context**
Final Mile Gate 2 requires safe reschedule and cancellation behavior. A reschedule is reversible through another PATCH and can be constrained to an exact stored event, exact numbered slot, current availability, and post-write verification. Deleting or cancelling the provider event is destructive R5 behavior and is denied by the higher-priority safety policy.

**Decision**
For a booked meeting with a validated stored provider event ID, accept only exact whole-message reschedule phrases. Read current availability under ADR-012, store up to three options separately, and accept only a numbered stored option. R2 `calendar_reschedule` is AUTO only in that code-defined scope. Pre-read the exact event with `GOOGLECALENDAR_EVENTS_GET`, revalidate policy and conflict, PATCH only event ID, start, end, timezone, calendar `primary`, and `send_updates=none` through `GOOGLECALENDAR_PATCH_EVENT`, then GET again. Local state changes only when event ID and exact UTC interval verify. If the pre-read already matches the target, recover locally without PATCH.

For exact cancellation phrases, write only local status `cancellation_requested`, timestamp it, clear pending reschedule offers, and tell the customer that Assaf will update the calendar. Do not call Calendar ports and do not claim the provider event was cancelled. Repeated requests are idempotent. Provider delete/cancel remains denied.

**Consequences**
- **Security/privacy:** No attendee, summary, description, conference data, extended properties, event ID, Meet link, or PII enters PATCH audit payloads or canonical reschedule/cancellation events. Calendar deletion remains unavailable.
- **Reliability:** GET uncertainty blocks PATCH. PATCH timeout still proceeds to mandatory GET verification. Verified provider state is authoritative. The stored event ID, Meet link, booking timestamp, and meeting type remain unchanged.
- **Follow-up:** Verified booking or booking crash recovery closes a pending meeting-offered follow-up with reason `meeting_booked`; stale pending rows are never send-ready once meeting status is booked or cancellation-requested. Reschedule offers and cancellation requests do not create or reopen follow-up.
- **Cost/lock-in:** New live reschedule uses up to four Calendar calls after selection: GET, exact free-slot read, PATCH, GET. Same Composio toolkit pin `20260812_00`; provider remains behind typed ports.
- **Migration/files:** Add `meetings.reschedule_slots_json`, `meetings.rescheduled_at`, and `meetings.cancellation_requested_at`; allow `offered|booked|cancellation_requested`. Migration: `migrations/20260821_adr013_calendar_gate2.sql`.
- **Acceptance:** Create and reschedule are alive by fake. Real staging OAuth CREATE/PATCH/GET remains an operator acceptance action. Cancellation is a manual request by safety design, so Gate 2 production acceptance is not complete until that live test and Assaf's manual calendar update path are verified.

**Alternatives considered**
Direct provider delete/cancel — rejected as R5 destructive. Owner approval before every exact reschedule — rejected because the accepted R2 scope is deterministic and verified. Natural-language date parsing or embedded intent — rejected as ambiguous. Full event PUT — rejected because it could overwrite attendees or event content.
