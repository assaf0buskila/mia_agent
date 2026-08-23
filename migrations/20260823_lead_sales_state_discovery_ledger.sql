ALTER TABLE lead_sales_state ADD COLUMN manual_step_known BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE lead_sales_state ADD COLUMN data_source_known BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE lead_sales_state ADD COLUMN discovery_turns INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE lead_sales_state ADD COLUMN asked_actions TEXT DEFAULT '[]' NOT NULL;
ALTER TABLE lead_sales_state ADD COLUMN explicit_buying_intent BOOLEAN DEFAULT FALSE NOT NULL;
