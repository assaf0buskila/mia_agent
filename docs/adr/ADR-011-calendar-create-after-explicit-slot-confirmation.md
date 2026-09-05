# ADR-011 Calendar create after explicit slot confirmation

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT

**Context**
Bible §12.2 / §18.2 require calendar events only after prospect confirmation and conflict checks. Prior slice offered slots (read-only `CalendarPort`) but never created events. Assaf selected **calendar event creation with explicit prospect confirmation** — numbered slot selection only; no implicit booking from “yes”, natural-language dates, or meeting intent alone.

**Decision**
Add separate typed `CalendarBookingPort` (`app/integrations/calendar_booking.py`) for Composio pins `GOOGLECALENDAR_EVENTS_LIST` + `GOOGLECALENDAR_CREATE_EVENT` (toolkit `20260812_00`). Keep `CalendarPort` read-only. R2 `calendar_create` with `in_approved_scope=True` only after valid selection of a stored numbered offer; policy AUTO, no owner approval. Idempotency via `privateExtendedProperty` `mia_booking_key=sha256(lead_id|start|end)` lookup before create. Conflict recheck with `find_free_slots` on exact 30m window before create (CREATE has no conflict check). Persist `meetings.status=booked`, canonical `MEETING_BOOKED` (`{status, scheduled_at UTC}` only). No attendees, description, htmlLink, or PII in provider args.

**Consequences**
- **Security/permissions:** R2 auto only in code-defined scope (exact `1`/`2`/`3`, `slot N`, `option N`, Hebrew ordinals). Kill switch denies before recheck/lookup/create. Meet links stored/returned only when host is exactly `https://meet.google.com`. Live Composio create needs Calendar **write** OAuth scope on connected account — operator action; code alive by fake/mock.
- **Reliability:** Lookup returns typed `found|not_found|error`; pagination exhaustion with remaining token => error. Lookup runs before conflict recheck; found event skips recheck (crash recovery). Conflict recheck requires returned free slot to fully cover selected interval. `mark_meeting_booked` revalidates all fields; false persist => RETRY, no `MEETING_BOOKED`. Booked leads never re-offer; unrelated follow-up skips calendar ports.
- **Cost/lock-in:** Up to 3 Composio executes per booking (recheck read + lookup + create); idempotent retry skips create when lookup hits.
- **Migration/files:** `meetings.offered_slots_json`, `meetings.meet_link`, `calendar_event_id` → `String(1024)`; `app/domain/meeting_slots.py`, `app/domain/calendar_booking.py`, `app/api/inbound.py`, `app/api/website.py`, tools `calendar_booking_lookup`/`calendar_create`, tests `test_calendar_booking.py`.
- **Tests:** 968 passed (2026-08-21); parser, offer persistence, R2/kill switch, conflict, idempotency, Composio args, E2E inbound+website.

**Alternatives considered**
Owner approval after slot pick — rejected (Assaf: R2 auto in approved scope). Natural-language date parsing — rejected. Single combined calendar port — rejected (read vs write separation). Attendee invite on create — rejected (out of scope).
