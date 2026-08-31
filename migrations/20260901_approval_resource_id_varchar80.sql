-- Calendar and LinkedIn approval ids include a prefix plus a bounded digest.
ALTER TABLE approvals ALTER COLUMN resource_id TYPE VARCHAR(80);
