-- Stated person name for a lead, owner-facing only.
-- Empty until the prospect said a name. Never inferred from the headline.
-- Additive, portable, idempotent-safe via the runner's duplicate-column skip.

ALTER TABLE lead_sales_state ADD COLUMN display_name VARCHAR(80) NOT NULL DEFAULT '';
