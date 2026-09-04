# ADR-012 Meeting availability policy (Sun–Thu IL business hours)

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT

**Context**
Final Mile Playbook Gate 2 requires real availability, correct timezone, and no invented slots. Assaf locked business hours for intro-call booking: **Sunday–Thursday, 09:00–17:00 Asia/Jerusalem, minimum 24 hours notice**. No env bypass this slice.

**Decision**
Deterministic policy in `app/domain/meeting_availability.py`. Workdays Python weekday Sunday=6, Monday=0 … Thursday=3; reject Fri/Sat. Local window `[09:00,17:00)`; 30-minute slots aligned to `:00`/`:30` local boundaries; meetings must end ≤17:00. Start ≥ `now + 24h` exactly. Carve provider free gaps into max 3 policy-valid slots in `prepare_meeting_offer`; persist same slots in `offered_slots_json`. Re-evaluate policy at confirmation with threaded `now`; stale slot => conflict, no create. Post-create **verify** via second `find_by_booking_key` (`calendar_booking_verify` audit tool); no local booked state or success reply before verify ok. Persist `meeting_type=intro_call`, `booked_at` UTC ISO on book. Customer copy follows Human Voice Standard (native concise Hebrew).

**Consequences**
- **Security/permissions:** Policy not configurable this slice. Verify mismatch / not_found after create => RETRY, no booked row.
- **Reliability:** Create timeout + verify found => persist from verified event (no duplicate retry). Preflight lookup (crash recovery) unchanged; no extra verify.
- **Cost/lock-in:** Up to 4 Composio executes per new booking (recheck + preflight lookup + create + verify lookup).
- **Migration/files:** add `meetings.meeting_type VARCHAR(32) DEFAULT 'intro_call'`, `meetings.booked_at VARCHAR(32) DEFAULT ''`; `app/domain/meeting_availability.py`, `app/domain/booking_voice.py`, `calendar_booking_verify` in tool allowlist; tests `test_meeting_availability.py`, extended `test_calendar_booking.py`.
- **Tests:** policy boundaries, verify-after-create, metadata. At ADR-012 acceptance, Gate 2 still needed reschedule/cancel plus staging OAuth; ADR-013 now closes the safe code boundary, while live staging OAuth remains open.

**Alternatives considered**
Configurable hours via env — rejected (Assaf: no bypass). Trust create response without verify — rejected (Final Mile write verification). Offer arbitrary gap times (e.g. 12:17) — rejected.
