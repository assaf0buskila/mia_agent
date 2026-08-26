-- ADR-028: the booked meeting is now the website's default exit. WhatsApp is the
-- fallback once the meeting has already been offered and not taken. Tracks that offer
-- the same way whatsapp_handoff_offered already tracks the WhatsApp offer.
-- Additive, portable, idempotent-safe via the runner's duplicate-column skip.
-- A comment line must never contain a statement separator, because the runner
-- splits this file on it.

ALTER TABLE lead_sales_state ADD COLUMN meeting_exit_offered BOOLEAN DEFAULT FALSE NOT NULL;
