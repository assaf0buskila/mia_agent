-- Exact LinkedIn approval payloads are bounded in application code but exceed VARCHAR(255).
ALTER TABLE approvals ALTER COLUMN proposed_parameters TYPE TEXT;
