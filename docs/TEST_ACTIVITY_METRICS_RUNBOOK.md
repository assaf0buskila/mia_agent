# Exclude reviewed Learn QA activities from lead metrics

Two reviewed production form submissions belong to QA, but their shared CRM contact,
activities and immutable intake receipts must remain intact. This change adds an
`is_test` flag to CRM activities and excludes flagged capture activities from the
owner's website-lead list and count.

This document prepares the exact operation. It does not authorize a migration,
deployment or production data update.

## Approved immutable mapping

| Source ID | Activity ID | Contact ID |
|---|---|---|
| `c9da672f-04d2-4a17-9981-e28db82e9225` | `activity_f39575e7a53249759c1bcd89bbda44be` | `crm_9777d0dac2614da6a320a71ee46d8b4a` |
| `b6254929-4bac-4591-880f-88d2d166932e` | `activity_d04df494b3f34f9eafef9516b6d7e446` | `crm_9777d0dac2614da6a320a71ee46d8b4a` |

The command refuses the operation unless exactly two distinct pairs are supplied and
each intake receipt and activity agrees on the source, activity and contact. Each
activity must also be a `form` / `contact_captured` row with the expected immutable
`source_ref`. A mismatch fails before any commit.

## Release and operation order

1. Back up PostgreSQL and archive-validate the backup using the normal GCP release
   procedure.
2. Apply `migrations/20261016_crm_activity_is_test.sql` before starting code that reads
   `crm_activities.is_test`. Confirm the migration runner reports no failure.
3. Deploy the exact reviewed, CI-green code revision. Do not run the marking command
   against an older application revision.
4. Run the command below without `--apply`. Verify the JSON reports `dry_run: true`,
   `matched: 2`, `changed: 0`, and only the exact IDs above.
5. With separate authorization for the production write, repeat the exact command with
   both `--apply` and the confirmation phrase. Verify `dry_run: false`, `matched: 2`,
   and `changed: 2`. Repeating an already completed operation is safe and reports
   `changed: 0`, but should only be done to reconcile known state.
6. Read back the two activities, two intake receipts and shared contact. Confirm only
   the activities have `is_test = true`; confirm the receipts and contact remain and
   the owner lead metric excludes the QA-only contact.

Read-only dry run:

```powershell
uv run mia-mark-test-activities `
  --source-activity c9da672f-04d2-4a17-9981-e28db82e9225=activity_f39575e7a53249759c1bcd89bbda44be `
  --source-activity b6254929-4bac-4591-880f-88d2d166932e=activity_d04df494b3f34f9eafef9516b6d7e446 `
  --contact-id crm_9777d0dac2614da6a320a71ee46d8b4a
```

Guarded write, only after separate production authorization:

```powershell
uv run mia-mark-test-activities `
  --source-activity c9da672f-04d2-4a17-9981-e28db82e9225=activity_f39575e7a53249759c1bcd89bbda44be `
  --source-activity b6254929-4bac-4591-880f-88d2d166932e=activity_d04df494b3f34f9eafef9516b6d7e446 `
  --contact-id crm_9777d0dac2614da6a320a71ee46d8b4a `
  --apply `
  --confirm mark-reviewed-test-activities
```

No command in this runbook deletes or rewrites contacts, activities or receipts. A
rollback of the classification sets `is_test = false` on these exact two activities
after the same receipt/activity/contact evidence is revalidated; do not delete rows.
