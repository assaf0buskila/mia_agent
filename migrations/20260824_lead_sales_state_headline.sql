-- Short human label for a lead, derived from the prospect's own words.
-- Owner-facing only: the console listed leads as opaque ids plus state flags, so
-- "who is the watches guy?" was unanswerable even though the conversation said so.
-- Additive, portable, idempotent-safe via the runner's duplicate-column skip.

ALTER TABLE lead_sales_state ADD COLUMN headline VARCHAR(120) NOT NULL DEFAULT '';
