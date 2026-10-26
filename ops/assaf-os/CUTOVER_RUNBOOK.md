# Assaf OS v2: authority cutover runbook (V2-31)

Status: **NOT_RUN**. Written and tested on fakes and a disposable PostgreSQL only. No command here has
been run against production, and this document authorizes none of them.
Source of truth: `V2_PG_CONTRACTS.md` section 16 (the CLI, the table of preconditions, the Script fence),
section 19 (the production order, FORK-PROD-ORDER), section 5.4 (the authority state machine), and the
errata E8 (retention credential) and E21 (close loopback `trust`). Where the code and this text differ,
the code is right: fix this text.

The companion documents are [OS_REHEARSAL_PLAN.md](../../docs/runbooks/OS_REHEARSAL_PLAN.md) (the same
steps on a copy first), [OS_PHONE_ACCEPTANCE.md](../../docs/runbooks/OS_PHONE_ACCEPTANCE.md) (the phone
run and the go list), [OS_CUTOVER_GO_NO_GO.md](../../docs/runbooks/OS_CUTOVER_GO_NO_GO.md) (the record you
fill) and [APP_ROLE_DEMOTE.md](../../docs/runbooks/APP_ROLE_DEMOTE.md) (step 2 in full).

## How to read this

- **Operator** runs commands. On the VM that is `sudo mia ...`. Deploys and settings pushes run from a
  clean checkout at the approved SHA on Assaf's PC (`ops/gcp/deploy.ps1`, `ops/gcp/push-settings.ps1`).
- **Assaf** is the owner. **Every line marked `GO` needs his explicit statement in chat for that one
  step**, with the exact target and SHA. A go is per step and per session. A past approval, a passed
  rehearsal, a green preflight and this document are not a go. No go means no step.
- Each step lists its **Precondition**, **Command**, **Verify** and **Rollback**. Stop on any output you
  did not expect, and stop on a nonzero exit. Never run a step "to see what happens".
- `S` is the 40-hex approved SHA. `E` is the authority epoch that `mia os-authority status` prints.
- The CLI never touches Secret Manager, a Script Property, a workbook, a bank file or a provider. It
  checks and records, and it **prints** the steps you do by hand. Every refusal is one line on stderr,
  `os_authority_refused:<code>[,<code>...]`, exit status 1, with a JSON document on stdout. Nothing it
  prints or logs carries a DSN, a secret, a record or a driver message.
- Evidence states: `NOT_RUN`, `CODE_CHECKED`, `LOCAL_TESTED`, `REHEARSAL_VERIFIED`,
  `PRODUCTION_VERIFIED`, `OWNER_ACCEPTED`, `BLOCKED`. Record them in `OS_CUTOVER_GO_NO_GO.md`.

## 0. Rules for every production step

1. **Pause the jobs**: `sudo mia jobs pause`. Record whether they were already paused, and resume them
   only if they were running before the window (step 7 end). `mia os-import` runs while paused.
2. **Take a verified backup first.** `sudo mia backup` writes a dump under `/var/lib/mia/backups`;
   validate it with `pg_restore --list` through the postgres container and discard the listing. Also
   run the off-host status (below): it must exit 0 and its dump must be under one hour old.
3. **Exact SHA.** Deploy only from a clean checkout at `S`, with CI green on that exact head.
   After every deploy or `sudo mia settings`, `/health` must show `deployment.commit_sha == S` and
   `/health/ready` must answer 200.
4. **One state lock.** `mia os-authority`, `mia os-provision`, `mia os-ledger`, `mia settings`,
   `mia deploy` and `mia restore` share `/var/lock/mia-state.lock`; a second one refuses to start.
   Never run two at once and never work around the lock.
5. **Keys that matter**: `MIA_ASSAF_OS_FREEZE`, `MIA_ASSAF_OS_BACKEND`, `MIA_ASSAF_OS_LEDGER_ENABLED`,
   `MIA_ASSAF_OS_AUTHORITY_EPOCH` and `MIA_ASSAF_OS_MIRROR_ENABLED` change only in the step that names
   them, through `freeze-plan`, then `ops/gcp/push-settings.ps1 -PatchSecret -Set @{ KEY = "value" }`,
   then `sudo mia settings`. All start off (`sheets`, `false`, `0`, `false`, `false`). **When**
   (contract section 22 E29): T1 sets `FREEZE`, `LEDGER_ENABLED` and `AUTHORITY_EPOCH` (the import of
   T4 and T5 needs the last two), T6 sets `BACKEND` only, T7 sets `MIRROR_ENABLED`, and the `open`
   phase clears `FREEZE`.
6. **Never print a credential.** Do not `cat`, source, paste or trace `/etc/mia/*.env`. Check a file
   with `sudo stat -c '%a %U %n'` (mode 600, owner root).

## 1. The production order (FORK-PROD-ORDER, section 19)

| # | Step | Go | Rollback in one line |
|---|---|---|---|
| 1 | Default-off deploy of the integrated SHA | G01, G02, G03 | Deploy the previous tag |
| 1a | Close loopback `trust` (E21) | G04 | `pg-hba-harden.sh --rollback` |
| 2 | V2-14a: demote the main application role | G05 | `app-role-demote.sh --rollback`, recreate |
| 3 | `mia os-ledger migrate` (V2-14 + V2-14b) | G06 | Additive; restore the pre-step dump |
| 4 | Provision the ledger roles, then the validator | G07 | Re-run (idempotent); see the staged-DSN rule |
| 5 | `mia os-authority init` | G08 | Idempotent; nothing to undo before an import |
| 6 | Rehearsal (V2-35a, V2-35) on a copy | G09 | The copy is thrown away |
| 7 | Wave 6: T1 to T7, the first write, phone acceptance | G10 to G24 | Section 5 |

Every production step above pauses the jobs and takes a verified backup first (section 0). Steps 1 to
5 touch no business data: the OS stays on Sheets until T6.

### Step 1: default-off deploy of the integrated SHA

- **Go G01** exact backend deployment (including every default-off feature). **G02** exact frontend
  deployment (reader compatibility only, no new UX). **G03** exact Apps Script deployment, only if
  this release needs one. The frontend release contains the v2 reader before any backend emits v2.
- **Precondition**: CI green on `S`; the independent review passed; section 0 done.
- **Command** (PowerShell, clean checkout at `S`): `.\ops\gcp\deploy.ps1 -ProjectId mia-assafweb -Ref <S>`.
- **Verify**: `/health` shows `deployment.commit_sha == S`; `/health/ready` is 200. Nothing in
  this release is on: the five keys of section 0 are at their defaults.
- **Rollback**: deploy the previous tag the same way. No data changed.

### Step 1a: close loopback `trust` (E21)

- **Go G04** closing loopback `trust` in the VM's PostgreSQL. It runs after the deploy of the integrated
  SHA and **before** the demote, because until it is done the role separation below protects nothing
  from code running on the VM.
- **Precondition**: jobs paused, verified backup, the `--check` below says `apply_ready=true` and
  `dsn_unverified=none`.
- **Command**: see "Step 1a in detail" in `docs/runbooks/APP_ROLE_DEMOTE.md`:
  `sudo bash /opt/mia/src/<S-tag>/ops/gcp/vm/pg-hba-harden.sh --check`, then `--apply`, then
  `curl --fail http://127.0.0.1:8000/health/ready`, then `--check` again.
- **Order matters**: run it **before** step 4. The script refuses to apply while a login role that a live
  DSN uses has no password, and V2-15 creates `assaf_os_retention` as LOGIN without one (E8). If step 4
  already ran (a repeat of the window), run `sudo mia os-provision retention --approved-sha S` first: it
  gives that role its verifier even while loopback is still `trust`.
- **Verify**: `--check` reports `hardened=true`; readiness is 200.
- **Rollback**: `sudo bash <path>/pg-hba-harden.sh --rollback`, check readiness, stop.

### Step 2: V2-14a demote of the main application role

- **Go G05** V2-14a main-role demotion and the runtime DSN and posture change.
- Follow `docs/runbooks/APP_ROLE_DEMOTE.md` from its step 1 to its step 7 exactly: stage, check,
  activate (observe), recreate and migration smoke, enforce, recreate, check.
- **Verify**: `app-role-demote.sh --check` shows `active_is_app=true`, `enforce=true`, a safe catalog;
  readiness 200; `/health` SHA is `S`. This is the precondition `main_role_unsafe` of `activate` later.
- **Rollback**: `app-role-demote.sh --rollback`, recreate, readiness. Never `mia restore`.

### Step 3: the ledger schema

- **Go G06** V2-14/V2-14b schema migration.
- **Precondition**: step 2 is enforced; `/etc/mia/os-ledger-migrate.env` holds exactly
  `MIA_OS_LEDGER_MIGRATION_DATABASE_URL` (the wrapper refuses any other key).
- **Create the migration env file (once, before the first step 3).**
  - **The role:** the DSN is the ORIGINAL superuser `mia`, the pinned migration identity of the
    `mia-vm` target (LEDGER_ROLE.md, "Pinned targets": `127.0.0.1:5432/mia`, `mia`; the main
    identity is `mia_app`).
  - **The source:** step 2 already saved exactly that DSN, once, as the single
    `MIA_DATABASE_URL=` line of `/etc/mia/app-role.rollback.env`. It is a `postgresql://` DSN,
    and `app_role_demote.py` checks its form: user `mia`, host `127.0.0.1`, port `5432`,
    database `mia`, a password, no query.
  - So the file is that line with its key renamed, copied, never printed or sourced. The bare
    `postgresql://` scheme is accepted (the job normalizes it to `postgresql+psycopg://`).
  - Keep `app-role.rollback.env`: it is still the step 2 rollback.
  - Run this once, under the shared state lock, as root. It refuses if the file already exists or
    the source is not the one expected line, and prints only a reason code. Exit status 75 with no
    output means the state lock is busy (another Mia operation is running): wait, then rerun.

    ```bash
    sudo flock -n -E 75 /var/lock/mia-state.lock sh -euc '
      src=/etc/mia/app-role.rollback.env; dst=/etc/mia/os-ledger-migrate.env
      [ ! -e "$dst" ] || { echo os_ledger_env_exists >&2; exit 1; }
      [ "$(grep -c "" "$src")" = 1 ] && grep -q "^MIA_DATABASE_URL=postgresql://mia:" "$src" \
        || { echo rollback_env_unexpected >&2; exit 1; }
      umask 077
      tmp=$(mktemp /etc/mia/.os-ledger-migrate.XXXXXX); trap "rm -f $tmp" EXIT
      sed -n "s/^MIA_DATABASE_URL=/MIA_OS_LEDGER_MIGRATION_DATABASE_URL=/p" "$src" > "$tmp"
      [ "$(grep -c "" "$tmp")" = 1 ] || { echo os_ledger_env_invalid >&2; exit 1; }
      chown root:root "$tmp"; chmod 600 "$tmp"; sync "$tmp"; mv -f "$tmp" "$dst"
      echo os_ledger_env_written'
    ```

  - **Verify, without printing the value:**
    - `sudo stat -c '%U:%G %a' /etc/mia/os-ledger-migrate.env` prints `root:root 600`.
    - `sudo cut -d= -f1 /etc/mia/os-ledger-migrate.env` prints only `MIA_OS_LEDGER_MIGRATION_DATABASE_URL`.
    - This check prints `os_ledger_env_ok`: the copy equals the rollback DSN, and differs from the
      demoted main DSN.

      ```bash
      sudo sh -euc 'a=$(sed -n "s/^MIA_DATABASE_URL=//p" /etc/mia/app-role.rollback.env)
        b=$(sed -n "s/^MIA_OS_LEDGER_MIGRATION_DATABASE_URL=//p" /etc/mia/os-ledger-migrate.env)
        m=$(sed -n "s/^MIA_DATABASE_URL=//p" /etc/mia/runtime.env)
        [ -n "$b" ] && [ "$a" = "$b" ] && [ "$b" != "$m" ] && echo os_ledger_env_ok \
          || { echo os_ledger_env_mismatch; exit 1; }'
      ```

    - `sudo bash /opt/mia/src/<S-tag>/ops/gcp/vm/pg-hba-harden.sh --check` prints, without any
      value:
      - `MIA_OS_LEDGER_MIGRATION_DATABASE_URL` in `dsn_keys_checked=`;
      - `dsn_unverified=none`, `dsn_without_password=none` and `roles_without_scram=none`;
      - exit 0 on the step 1a hardened cluster.
      It confirms that the DSN's role (`mia`) has a SCRAM verifier, but it does not log in. The
      equality check above proves the copy is identical, not that it authenticates. The first real
      login is `mia os-ledger migrate` itself, which on a bad password fails closed with a SQLSTATE
      (such as 28P01) and prints no value.
  - The file is read by `mia os-ledger` (this step), `mia os-authority`, `mia os-provision` and
    `mia restore` (its ledger reapply refuses `ledger_reapply_unavailable` without the file). Each
    requires exactly this one key. `mia os-job` does not read it. **Leave the file in place after the
    cutover: deleting it breaks `mia restore`.**
  - It holds a superuser credential: root-only, mode 600, never in `mia-env`, never copied off
    the VM.
- **Command**: `sudo mia os-ledger migrate --approved-sha S`.
- **Verify**: exit 0; the job creates schema `assaf_os`, owned by `assaf_os_owner`.
- **Rollback**: the migration is additive and creates no data. To undo it, restore the dump taken
  before the step (`sudo mia restore <dump>`, which re-applies every role posture); that is a
  separately approved recovery operation.

### Step 4: the ledger roles, then the validator

- **Go G07** V2-15 role grants and provisioning, and the validator.
- **Precondition**: step 3 done (provision needs the schema and its tables). Step 1a done, so that the
  passwords below are enforced and the login check means something.
- **Command 1** (the ledger runtime role and `MIA_OWNER_OS_LEDGER_DATABASE_URL` in `runtime.env`):
  `sudo mia os-provision runtime --approved-sha S`. It runs `provision_ledger.py --mode provision
  --apply` from the deployed tag, validates the catalog before it commits, and then connects as the
  runtime role through the staged DSN.
- **Command 2** (the retention role's credential, E8): `sudo mia os-provision retention --approved-sha S`.
  It runs `ops/assaf-os/provision_retention.py`. `assaf_os_retention` is LOGIN with its section 18.2
  grants but no password; this generates one, stages
  `MIA_OS_LEDGER_RETENTION_DATABASE_URL` in the job-only `/etc/mia/os-retention.env` (VM-owned, mode 600,
  that one key, never in `mia-env`, never in `runtime.env`) **before** it commits, sets the password from a
  SCRAM verifier computed on the client, runs the section 18 validator first, and connects as the role.
  `mia-env` can never override it: the `owned` pattern in `mia settings` drops `MIA_OS_LEDGER_\w+`.
- **PUBLIC CONNECT (E8.2).** The retention role has no database ACL entry of its own (the validator pins
  that), so it reaches the database through PUBLIC CONNECT on `mia`. Command 2 therefore refuses with
  `retention_connect_blocked` if PUBLIC CONNECT was ever revoked. Do not revoke it. If the owner decides
  to, a section 18.2 erratum must first grant exactly CONNECT to the retention role and teach the
  validator; only then can this step provision it.
- **Rotation.** Remove `/etc/mia/os-retention.env` and run Command 2 again. A retry otherwise reuses the
  staged password and never rotates it.
- **A refused provision leaves its staged DSN behind (operator rule).** The DSN is staged before commit on
  purpose, so a transaction that the in-transaction audit then refuses (`ledger_provision_refused`) rolls
  the role back but keeps the line in `runtime.env`. That line now names a role that does not exist.
  V2-17's `core_authority_state` treats any set `MIA_OWNER_OS_LEDGER_DATABASE_URL` as live, so after the
  next recreate of the app every Sheets write would fail closed with `ledger_unavailable`. After a
  refused `runtime` provision, either fix the cause and re-run it until it passes (the staged password is
  reused), or delete the `MIA_OWNER_OS_LEDGER_DATABASE_URL` line before any recreate of the app.
- **Verify**: both commands print only their plan lines and exit 0;
  `sudo stat -c '%a %U %n' /etc/mia/os-retention.env` is `600 root /etc/mia/os-retention.env`; then
  `sudo mia settings` recreates the app and `/health/ready` is 200. This is the precondition
  `ledger_role_unsafe` of `activate` later.
- **Rollback**: both commands are idempotent. There is nothing to undo before step 5.
- **After a restore**: roles are cluster-global, so on the same cluster a `mia restore` keeps both
  passwords. On a **new** cluster or VM the roles come back without them (and `/etc/mia` is gone): run
  both commands again before the services start.

### Step 5: initialise the authority

- **Go G08** authority initialization and the workspace and pepper pin.
- **Precondition**: `MIA_OS_STATEMENT_PEPPER` is set in `mia-env` (at least 32 random bytes; it is write-once
  per workspace, so keep an offline copy) and `sudo mia settings` has been run.
- **Command**: `sudo mia os-authority init --approved-sha S`.
- **Verify**: `outcome: created`, `epoch: 1`, both domains `shadow`, `pepper_pinned: true`. A repeat
  prints `outcome: existing` and changes nothing. `sudo mia os-authority status --approved-sha S` shows
  `non_import_receipts: 0`.
- **Rollback**: none needed. A second owner or a different pepper is refused
  (`authority_changed:workspace_conflict`, `ledger_unavailable:statement_pepper_mismatch`).

### Step 6: rehearsal

- **Go G09** the rehearsal run, on a copy, with a different Script, workbook and mirror. Follow
  `docs/runbooks/OS_REHEARSAL_PLAN.md`; it executes this runbook's T1 to T7, the broken-precondition abort
  (exactly one of the 15 codes below, nonzero, no state change), the rollback before the first write,
  and a real `mia restore`. A relevant release change invalidates it. Production does not start until
  it passes.
- **Precondition**: steps 1 to 5 done on the copy's own pins; production untouched.
- **Verify**: every row of the rehearsal plan's table is `REHEARSAL_VERIFIED`, with the abort, the
  rollback and the forward run recorded in `OS_CUTOVER_GO_NO_GO.md`.
- **Rollback**: the copy is thrown away and its data disposition recorded; production is unchanged.

## 2. The evidence directory

The job reads and writes one VM-owned directory, `/var/lib/mia/os-authority` (the CLI's
`MIA_OS_AUTHORITY_DIR`, mounted into the container). The wrapper creates it owned by the job's user and
re-owns its contents at each run, so operator-placed files need no special mode. A symbolic link, a
non-file or a file over 1 MB is refused, and a present file the CLI cannot use fails **its** check
closed (`evidence_unreadable`). Timestamps are ISO-8601 with an offset (`2026-10-20T12:00:00Z`); a naive
time proves nothing and fails.

| File | Written by | Used by | Notes |
|---|---|---|---|
| `drain.json` | `preflight --drain --final-accept` | `sheets_pending_proposals` | Epoch, SHA and freeze bound. One drain belongs to one freeze: `freeze-plan --phase drain` renames an existing one to `drain-superseded-<UTC>.json` (kept, never deleted), and a drain taken more than 2 hours before `fence_probe_at` is refused (`drain_stale`) |
| `preflight.json` | `preflight`, and the drain once it sees the Script live | `capture_before_fence`, `sheets_pending_proposals` | Holds `fence_probe_at` and, after a lifted fence, `fence_broken_at` |
| `activation.json` | `activate` | the record | The ledger state, not this file, is the authority |
| `backup.json` | operator | `backup_too_old` | V2-10 `backup_offsite.py --status` output |
| `restore-drill.json` | operator | `restore_drill_too_old` | V2-10 `restore_drill.py --report` output |
| `fe-v2.json` | operator | `fe_v2_reader_not_live` | Cloudflare version and bundle hash |
| `journal.json` | operator | `journal_capacity` | V2-00 measure of the `_assaf_os_*` tabs |
| `manifest.json` | operator | `manifest_not_signed`, `capture_before_fence` | The import manifest and Assaf's sign-off |

The shapes below are tested against the CLI itself (`tests/ops/test_cutover_runbook.py`), with the
bracketed values replaced. Keys are exact: an extra or missing key fails the check.

`backup.json`: the output of `sudo python3 <installed path>/backup_offsite.py --status > backup.json`
on the VM (exit 0 means healthy). The CLI reads `last_success_utc`: the dump must be under one hour old
when `activate` runs, so run it again right before.

<!-- evidence:backup.json -->
```json
{"age_minutes": 7, "healthy": true, "last_object": "15min/mia-[STAMP].dump.enc", "last_success_utc": "[UTC-5M]"}
```

`restore-drill.json`: the report that `restore_drill.py --report` wrote on Assaf's PC (the drill is never
run on the VM). The CLI reads `started_utc` (within 7 days) and `passed` (true, which includes the RTO).

<!-- evidence:restore-drill.json -->
```json
{"started_utc": "[UTC-2D]", "passed": true, "rto_met": true, "checks": []}
```

`fe-v2.json`: the Cloudflare Worker version id and the sha256 of the deployed bundle that contains the
frontend v2 reader (V2-21), recorded by hand after the release.

<!-- evidence:fe-v2.json -->
```json
{"v": 1, "cloudflare_version": "[CF-VERSION]", "bundle_sha256": "[SHA256]", "recorded_at": "[UTC-1D]"}
```

`journal.json`: the data-row count of each of the five `_assaf_os_*` tabs of the archive workbook, read
in the Sheets UI (header row not counted). **The file must name exactly these five tabs, no more and no
fewer: `_assaf_os_accepted`, `_assaf_os_proposals`, `_assaf_os_receipts`, `_assaf_os_replay` and
`_assaf_os_issues`, and `max_rows` must be exactly 2000**, the Script's `ASSAF_OS_MAX_ROWS`. The CLI pins
both to `UnifiedRuntime.gs` (a test reads them back from its source), so a missing tab, an extra or
misspelled one, or another cap fails the check (detail `tabs` or `max_rows`): after a section 5 rollback
to Sheets the Script enforces that same cap, and a tab left out of the measure is a tab that can hit
`metadata_capacity_exceeded` there. The check fails above 90 percent of the cap on any tab (`at_cap`), and
a measure older than 7 days is stale.

<!-- evidence:journal.json -->
```json
{"v": 1, "measured_at": "[UTC-5H]", "max_rows": 2000, "tabs": {"_assaf_os_accepted": 0, "_assaf_os_proposals": 32, "_assaf_os_receipts": 19, "_assaf_os_replay": 51, "_assaf_os_issues": 0}}
```

`manifest.json`: **the sign-off record**. It is not a cryptographic signature: it is Assaf's explicit
statement in chat, written down by the operator, and the CLI verifies every binding in it
(`verify_signoff`): the manifest recomputes to `manifest_digest` (`app.services.os_ledger.api.digest`,
the same digest `promote` is given); the sign-off names the same digest and the same capture time; it
names Assaf's numeric Telegram owner id; it was made **after** the capture and not in the future; and
the drift proof (capture #1 against capture #2, zero changed cells) is in it. `manifest` is the object
V2-30 prints for the staged batch.

<!-- evidence:manifest.json -->
```json
{"v": 1, "manifest": {"counts": {"leads": 3, "clients": 2}}, "manifest_digest": "[DIGEST]", "xlsx_captured_at": "[UTC-90M]", "signoff": {"owner_id": "[OWNER-ID]", "signed_at": "[UTC-30M]", "manifest_digest": "[DIGEST]", "xlsx_captured_at": "[UTC-90M]", "drift_changed_cells": 0}}
```

## 3. Wave 6: T1 to T7

Preconditions for the whole of wave 6: steps 1 to 6 done; the rehearsal passed at this release; Assaf is
present throughout; jobs paused and a verified backup before **each** of T1, T3, T5 and T6.

### T1: drain (freeze)

- **Go G10** drain/freeze and final reconcile in the approved window.
- **Command**: `sudo mia os-authority freeze-plan --phase drain --approved-sha S` prints
  `MIA_ASSAF_OS_FREEZE=true`, `MIA_ASSAF_OS_LEDGER_ENABLED=true` and `MIA_ASSAF_OS_AUTHORITY_EPOCH=E`,
  with the backend still `sheets`. Set them with `push-settings.ps1`, then `sudo mia settings`. It
  names `E` from the ledger, so `init` (step 5) has run, and it refuses (`authority_state_invalid`) once
  `core` is no longer `shadow`.
  **Why the ledger keys come now, not at T6** (contract section 22 E29): the import (G24 stage at T4,
  G12 promote at T5) runs as the ledger worker, before the switch, and needs both:
  `authorize.for_worker` refuses `epoch_unset` and the ledger core refuses `ledger_disabled` without
  them. With `MIA_ASSAF_OS_BACKEND` still `sheets` nothing else changes: the backend factory returns the
  Sheets backend (only its postgres path reads the keys), the OS and Mia are served from the Sheet, and
  the ledger core admits only `import` while `core` is `shadow` (a proposal, an owner commit or a
  decision is refused `backend_not_postgres`). Proved by
  `tests/workers/test_os_e29_sheets_with_ledger_keys.py`.
  **Each freeze starts a new drain**: the command also renames an existing `drain.json` (the drain of
  an earlier freeze, for example an aborted window) to `drain-superseded-<UTC>.json` and refuses
  (`evidence_unwritable`) if it cannot. Run it once, at the start of T1: running it again after T2
  voids that T2 and T2 must be redone.
- **What the freeze does**: it refuses every **new** intent on both backends with 503 `os_frozen` (a
  proposal from the OS screen or from Mia, a lead card). Approve and reject stay allowed, so every
  pending item can settle. The OS workbook leaves Mia's Sheets allow-list. Proved path by path in
  `tests/workers/test_os_freeze_paths.py`.
- **Settle**: Assaf approves or rejects every pending proposal and open Telegram card (a card also
  expires on its own). An unknown receipt is resolved by looking up its receipt, never by replaying it.
- **Known limit while frozen**: do not tap a website-lead "prepare" card (or ask Mia to prepare a lead)
  between T1 and the `open` phase. The freeze refuses it (`os_frozen`, nothing sent), but
  `assaf_os_leads._create_final_proposal` (V2-28's file) records every refusal after its claim as an
  uncertain write, so the same lead then answers `lead_source_pending_review` after the freeze lifts and
  needs an owner repair. Leads wait until `open`.
- **Verify**: `sudo mia os-authority status --approved-sha S` shows `settings.freeze: true`,
  `settings.ledger_enabled: true`, `settings.epoch_matches: true` and `settings.backend: sheets`;
  a new proposal on the phone is refused; the OS still loads from the Sheet; `/health` SHA is `S`.
- **Rollback** (the same for an aborted T2): the keys go back with the way back to Sheets, which
  `core=shadow` allows. `sudo mia os-authority freeze-plan --phase revert --approved-sha S` prints
  `MIA_ASSAF_OS_BACKEND=sheets`, `MIA_ASSAF_OS_LEDGER_ENABLED=false`, `MIA_ASSAF_OS_AUTHORITY_EPOCH=0`
  and `MIA_ASSAF_OS_MIRROR_ENABLED=false`. Set them together with `MIA_ASSAF_OS_FREEZE=false`, then
  `sudo mia settings`. The ledger and the Sheet are
  unchanged, but **the drain is void**: once the freeze is lifted the owner can make OS-screen
  proposals again (they have no Telegram card, so no other check sees them). So the exit itself, the
  `--phase revert` plan above, **sets `drain.json` aside** (`drain-superseded-<UTC>.json`), and so
  does the retry's `freeze-plan --phase drain`. **A retry redoes T1 and T2.** A drain taken more than
  2 hours before `fence_probe_at` is refused as a backstop (`sheets_pending_proposals`, detail
  `drain_stale`). Never reuse a drain of an earlier freeze.

### T2: final reconcile

- **Command**: `sudo mia os-authority preflight --drain --final-accept --approved-sha S`.
- **Warning, disclosed**: this is the **one** approved signed snapshot, a mutating call: the Script
  accepts the pending native edits made in the Sheet. It refuses without `--final-accept`, without the
  freeze, and when the Sheet is not live (`drain_requires_live_sheet`). Run it only while the Sheet is
  still live, i.e. before T3.
- **Verify**: exit 0, and `drain.json` records `sheets_pending: 0`, `sheets_unknown_receipts: 0`,
  `cards_open: 0`, `freeze: true`. A nonzero count refuses with the matching code; settle it and run it
  again (another snapshot is allowed).
- **Rollback**: none needed; unfreeze as in T1 if you stop here. **A retry redoes T2**: `drain.json`
  belongs to the freeze it was taken under, and the retry's `freeze-plan --phase drain` (T1) sets it
  aside. Go on to T3 promptly after a good T2 (see `drain_stale` in section 7).

### T3: fence the Sheet

- **Go G11** the Script Properties fence, the archive naming and the tab warnings.
- **Do, in the Google account** (the CLI does none of this):
  1. Apps Script project settings, Script properties: set `ASSAF_OS_MODE` to `FENCED`. No redeploy.
     Any value other than exactly `TEST` or `LIVE` fences. From then on `doPost` answers `unavailable`
     unsigned for every operation, and `reconcileAssafOsEdit`, `reconcileAssafOsWorkbook` and
     `refreshAssafOsReferenceValidations` throw `unavailable` before they open the workbook or take a
     lock (`tests/assaf-os/apps-script-fence.test.js`, on the real Script source). Setting the property
     back to `LIVE` brings them all back. Apps Script may send failure notices for the edit trigger while
     it is fenced.
  2. Immediately run `sudo mia os-authority preflight --approved-sha S`. It sends one signed read-only
     `schema` call straight through the existing transport (not through the factory, which refuses
     under `postgres`). Only the exact unsigned reply a Script with no readable config gives counts as
     fenced. It records `fence_probe_at` in `preflight.json`: the earliest probe of an **unbroken** run
     of `unavailable` for this epoch and SHA. A probe that sees the Script live restarts the clock, and
     when a fence had already been recorded it also records `fence_broken_at`, the first time the Script
     was seen live again, which voids any `drain.json` taken before it. The T2 drain is such a probe: it
     sees the Script live, so it records that break itself. A probe that cannot tell (`sheets: unknown`:
     the 20 second budget ran out, a transport error, an unexpected reply) is neither a sighting nor a
     pass: it exits 1 (`sheets_not_fenced`), carries `fence_probe_at` forward unchanged and records no
     break, so it **does not void the captures**. Run `preflight` again.
  3. Put **warning-only protection on every tab** of the archive workbook, hidden tabs included
     (unhide, protect with "Show a warning when editing this range", hide again), and rename the
     workbook to exactly `ARCHIVE – edits ignored` (the dash is an en dash, U+2013). Warning protection
     is not a lock: the owner can still edit, and the edit is ignored.
- **Verify**: `preflight` prints `sheets: unavailable`; each tab's protection is read back and recorded;
  the workbook name is read back.
- **Rollback**: set `ASSAF_OS_MODE` back to `LIVE`, remove the protection, rename the workbook back,
  then the freeze rollback of T1. **Then run `sudo mia os-authority preflight --approved-sha S` while
  the Script is LIVE again, and do not go on until both of these hold: (a) its JSON prints
  `"sheets": "live"`, and (b) `sudo cat /var/lib/mia/os-authority/preflight.json` shows
  `fence_broken_at`** (a fence was recorded at T3, so a sighting of the live Script writes it). If the
  JSON prints `"sheets": "unknown"` (a timeout, a transport error) the probe saw nothing and wrote no
  break: **run it again**, as often as it takes, until (a) and (b) hold; a rollback closed on an
  `unknown` leaves the aborted attempt's `fence_probe_at` and `drain.json` usable. The command exits 1
  (`sheets_not_fenced` and, if a fence had been recorded, `sheets_pending_proposals`), which is the
  expected answer here: its job is to write `fence_broken_at`.
  The rollback lifts the fence and the freeze without touching the ledger, so the epoch and the SHA do
  not change, and without that record the CLI has nothing to tell the retry from the aborted attempt: it
  would carry the aborted attempt's `fence_probe_at` forward and accept its `drain.json` and any capture
  taken while the Sheet was live. **A retry redoes T1, T2 (a new drain, with the Sheet live and the
  freeze on), T3 and T4 with new captures taken after the new fence.** Never reuse a drain, a capture or
  a `manifest.json` of the aborted attempt. If the refusal `sheets_pending_proposals`
  (`drain_before_fence_break`) shows at T6, the remedy is that same redo of T2 and T3.
- *Naming conflict, for the coordinator*: the pasted V2-35 issue says `ARCHIVE - read only`; the frozen
  contract section 16 says `ARCHIVE – edits ignored`. This runbook follows the contract.

### T4: captures and import staging

- **Precondition**: the fence is on and `fence_probe_at` is recorded. The T1 keys are applied
  (`sudo mia os-authority status --approved-sha S`: `settings.ledger_enabled: true`,
  `settings.epoch_matches: true`, `settings.backend: sheets`); the import is refused without them.
- **Capture #1 and #2**: download the archive workbook as `.xlsx` (hidden journals included), twice,
  a few minutes apart, both **after** `fence_probe_at`. Record UTC, size and sha256 of each. Copy them
  into `/var/lib/mia/os-import` as `root:10001`, mode 640 (the import job mounts it read-only).
- **Drift proof**: `uv run --offline python scripts/os_workbook_diff.py capture1.xlsx capture2.xlsx`
  (local, no network). It prints the number of changed cells and their `(sheet, cell)` refs, never the
  values. **It must be zero.** That count is `drift_changed_cells` in the sign-off.
- **Go G24** staging the import into the production ledger. **Stage**:
  `sudo mia os-job os-import /var/lib/mia/os-import/<capture>.xlsx --workspace W --epoch E --stage`
  (`W` is the `workspace` that `sudo mia os-authority status --approved-sha S` prints), on the capture
  that will be signed. It writes staging rows to the production ledger (nothing is promoted until G12), and it runs as
  the ledger **runtime** role (no privileged DSN) from the deployed tag's `scripts/`. Unaccepted and
  id-less rows are quarantined, never dropped. Resolve every quarantined row without a bypass.
- **Verify**: zero unresolved quarantine; the manifest counts, ids, revisions, per-currency totals,
  refund chains and references equal the capture.
- **Rollback**: nothing is promoted; a staged batch is inert. Re-staging an unchanged workbook after a
  rollback is a new batch (unique per epoch).

### T5: sign the manifest, promote the import

- **Go G12** import promotion of the exact signed manifest.
- **Precondition**: the T1 keys are still applied (as for T4) and `core` is `shadow` at epoch `E`.
- **Do**: show Assaf the manifest summary and its digest. When he says so in chat, the operator writes
  `manifest.json` (section 2) with his sign-off, and the exact digest is passed to promote:
  `sudo mia os-job os-import /var/lib/mia/os-import/<capture>.xlsx --workspace W --epoch E --promote <digest>`,
  plus `--cancellations /var/lib/mia/os-import/<cancellations>.json` when the manifest lists any (the
  digest is the one `uv run --offline python scripts/os_import.py <capture>.xlsx --workspace W --epoch E`
  printed on the same workbook and cancellations). Promote runs in one transaction at `core=shadow`, writes every record with its Sheets revision, refuses orphans unless
  the manifest lists them under `cancellations`, and is idempotent within an epoch.
  `import_quarantine_unresolved`, `manifest_digest_mismatch`, `import_orphan_rows` and
  `authority_not_active` are its refusals.
- **Verify**: `sudo mia os-authority status --approved-sha S`: `states.core: shadow`, epoch `E`,
  `non_import_receipts: 0`. Promote leaves one committed `import` receipt at epoch `E`, and its
  proposal carries the signed digest: `preflight` and `activate` look for exactly that, so a skipped
  promote cannot activate (`import_not_promoted`, T6).
- **Rollback**: before activation nothing serves from the ledger. If the import is wrong, T6 does not
  happen; fix the capture and re-stage (a new batch), or section 5.

### T6: activate, then switch the backend

- **Preflight**: put the fresh `backup.json`, `restore-drill.json`, `fe-v2.json` and `journal.json` in the
  evidence directory (section 2) and run `sudo mia os-authority preflight --approved-sha S`. It must
  print `ready_to_activate: true` with all 15 checks `pass` and the one entry of `gates`
  (`import_not_promoted`) `pass`. A check whose operator evidence is not there
  yet shows `pending` here and **still refuses `activate`**; the gate shows `pending` (detail
  `no_import_receipt`) until T5's promote has run at this epoch. If this preflight prints `sheets: unknown`
  (a timeout or a transport error on the probe), run it again: an `unknown` probe carries the recorded
  `fence_probe_at` forward and does not void the T4 captures, the staging (G24) or the sign-off.
- **Go G13** authority activation at epoch `E`.
- **Command**: `sudo mia os-authority activate --approved-sha S`. It re-evaluates **every**
  precondition (section 7) and moves `core` from `shadow` to `active` only if all pass **and** the
  signed manifest digest was promoted at this epoch (`import_not_promoted`, beyond the contract's 15:
  an import receipt at epoch `E` that binds the digest in `manifest.json`; with no signed manifest
  it says nothing, `manifest_not_signed` does). It **never**
  bumps the epoch. Run again once active it answers `already_active` without checking.
- **Verify**: `outcome: activated`; `states.core: active`; `activation.json` exists. From this moment
  every Sheets write is refused `authority_moved` (409) and the Script is already fenced.
- **Go G14** the backend switch to postgres.
- **Switch**: `sudo mia os-authority freeze-plan --phase switch --approved-sha S` prints exactly
  `MIA_ASSAF_OS_BACKEND=postgres` and nothing else: `MIA_ASSAF_OS_LEDGER_ENABLED=true` and
  `MIA_ASSAF_OS_AUTHORITY_EPOCH=E` were set at T1 and are not repeated (contract section 22 E29). It
  refuses `drain_keys_not_applied` if they are not in the settings the job reads.
  **The freeze stays on.** Set the key, then `sudo mia settings` (T6 is `mia settings` and then `/health`;
  there is no task to replace on the VM).
- **Verify**: `/health` shows `deployment.commit_sha == S`; `/health/ready` is 200;
  `sudo mia os-authority status --approved-sha S` shows `settings.backend: postgres`,
  `settings.epoch_matches: true`, `settings.freeze: true`. This release's `/health` reports the SHA and
  not the epoch, so the epoch is read from `status` and from the v2 read binding of the OS screen
  (`source: postgresql`, `authority_epoch: E`). Assaf opens the OS on his phone: it loads, read-only.
- **Rollback**: before the first non-import receipt, section 5.

### T7: mirror and the third capture

- **Go G15** enable mirror to the pinned **new** mirror workbook.
- **Precondition**: the mirror workbook is new, shared only with the mirror service account, and already
  has the eight mirror tabs with their exact names: `סיכום` and the seven entity tabs
  `לידים`, `לקוחות`, `עבודות`, `תקבולים`, `שעות עבודה`, `הוצאות והשקעות`, `משימות`. There is no
  `addSheet` call, so a missing tab fails the write and the tabs must exist first (contract E11.4):
  create them with the next bullet. `MIA_OS_MIRROR_SPREADSHEET_ID` and
  `MIA_OS_MIRROR_SERVICE_ACCOUNT_JSON` are already set to it, and never to the archive workbook.
  Reference columns show raw ids in this release.
- **Create the eight tabs** (by hand, in the Google account, before the command below). The names and
  the header row of every tab come from one place in the code: `MIRROR_TABS` and `headers()` in
  `app/services/os_ledger/mirror_projection.py` (the V2-29 mirror schema). The seven entity labels are
  read from `app/prototypes/assaf_os_export_schema.json`, and the `סיכום` labels are its
  `_SUMMARY_FIELDS` (the `summary` tab of `ops/assaf-os/schema.js`). Print them, from a clean checkout at
  `S` on Assaf's PC (local, no network; `-X utf8` because the output is Hebrew, so redirect to a file if
  your console garbles it):

  `uv run --offline python -X utf8 -c "import app.services.os_ledger.mirror_projection as m; [print(t, *m.headers('summary' if t == m.SUMMARY_TAB else next(e for e, n in m.ENTITY_TABS.items() if n == t)), sep=' | ') for t in m.MIRROR_TABS]"`

  It prints eight lines, `<tab> | <header 1> | <header 2> | ...`, in the order of the tabs. In the new
  workbook add one tab per line, named exactly as printed and in that order (rename the default tab
  for the first; a leftover default tab is never read or written). **Leave them empty**: the worker
  writes every tab in full from `A1` on its first projection, the header row included, from the same
  `headers()`, and reads it back. If you type the headers anyway, they must be the printed ones. Do
  not add a settings tab (the archive workbook's is not a mirror tab). The service account needs edit
  access.
- **Command**: `sudo mia os-authority freeze-plan --phase mirror --approved-sha S` prints
  `MIA_ASSAF_OS_MIRROR_ENABLED=true`. Set it, `sudo mia settings`. The worker projects the whole import
  (an `import` event is a full projection) and reads it back.
- **Verify**: `sudo mia os-authority status --approved-sha S` shows `mirror: verified`; the OS screen shows
  "Sheet verified HH:MM (vN)". A mirror that stays `unverified` or `unknown` is not a pass.
- **Third capture**: download the **archive** workbook once more as `.xlsx` and diff it against capture
  #1 with `scripts/os_workbook_diff.py`. **It must be zero changed cells.** It detects owner edits made
  to the archive after the first two captures. Record UTC, size and sha256.
- **Rollback**: `MIA_ASSAF_OS_MIRROR_ENABLED=false` and `sudo mia settings`; the mirror is only an
  export. A nonzero diff blocks the first write: stop and use section 5.

## 4. The first write, phone acceptance, and afterwards

1. **The boundary.** `sudo mia os-authority status --approved-sha S` must show `non_import_receipts: 0`
   and `mirror: verified`. Record it.
2. **Go G16** admitting the first non-import PG write and performing the planned phone actions. Then
   `sudo mia os-authority freeze-plan --phase open --approved-sha S` prints `MIA_ASSAF_OS_FREEZE=false`
   (it refuses `switch_not_applied` until the switch is in the settings). Set it, `sudo mia settings`.
   **From the first non-import receipt, including a rejected decision, there is no simple rollback.**
3. **Go G17** phone acceptance, including the p95 and the restore RTO and RPO:
   `docs/runbooks/OS_PHONE_ACCEPTANCE.md` (twenty real actions, p95 under 4000 ms, receipts matching).
4. Resume the jobs only if they were running before: `sudo mia jobs resume`.
5. **Go G18** removing the old workbook's `reconcileAssafOsEdit` onEdit trigger, **only after
   acceptance**. Record the exact trigger id and project, and a readback of its absence. Keep the old
   workbook fenced and archived. Do not delete any other trigger.
6. One feature release at a time, each with its own go, deploy and acceptance, in this order:
   one-tap (V2-24 and V2-32), fast tasks (V2-33), statements (V2-26 and V2-27), assistant
   (V2-37, V2-38, V2-39). Those releases are not part of this runbook.

## 5. Rollback before the first non-import receipt

Allowed **only** while `non_import_receipts` is 0 at the current epoch. After that the CLI refuses
(`rollback_after_first_write`) and nothing here applies.

- **Go G19** the rollback itself.
- **Command**: `sudo mia os-authority rollback --approved-sha S`. It sets `core` to `fenced` (the ledger
  then refuses every write) and prints the steps. It is idempotent. It counts the non-import receipts
  before the fence **and again after it**: the first phone actions (G16, G17) happen exactly while a
  rollback is still allowed, and a commit can land between the check and the fence. If the second count
  is nonzero it refuses `rollback_after_first_write,core_left_fenced` (with `"core": "fenced"` in the
  JSON), leaves `core` **fenced** and prints **no steps** (no revert, no "the Sheet is as captured").
  That is the section 6 state: the ledger owns a business write and now refuses every OS write
  (`authority_not_active`), so do not return to Sheets; follow section 6. **Read the second reason**:
  a refusal of `rollback_after_first_write` alone (the first count) means the ledger is still LIVE and
  nothing was changed; with `core_left_fenced` the ledger is fenced. A repeat of the command once `core`
  is fenced answers the same two reasons.
- **Then, in this order**, as the command prints:
  1. `sudo mia os-authority freeze-plan --phase revert --approved-sha S` prints
     `MIA_ASSAF_OS_BACKEND=sheets`, `MIA_ASSAF_OS_LEDGER_ENABLED=false`,
     `MIA_ASSAF_OS_AUTHORITY_EPOCH=0`, `MIA_ASSAF_OS_MIRROR_ENABLED=false`, and sets `drain.json`
     aside (it ends the freeze that drain belongs to). It refuses
     (`authority_state_invalid`) while `core` is still `active`, so run the command above first. Keep
     `MIA_ASSAF_OS_FREEZE=true`. Set them, `sudo mia settings`, check `/health` SHA.
  2. Apps Script: set `ASSAF_OS_MODE` back to `LIVE` (no republish). Confirm the Sheet answers: the Mia OS
     page loads from Sheets. Remove the tab protection and rename the workbook back.
  3. `MIA_ASSAF_OS_FREEZE=false`, `sudo mia settings`.
- **Go G23** the re-arm. **Try again later**: `sudo mia os-authority init --rearm --approved-sha S` moves
  `core` from `fenced` to `shadow` with **one** epoch bump (idempotent; it refuses after a business
  write). It flips the authority state on production, so it needs its own go even though the rollback
  that came before it had one. Then repeat T1 to T7 from a fresh drain, a fresh fence and new captures:
  the import request id and batch key include the epoch, so the same workbook imports again at the new
  epoch. There is no `fenced` to `active`.

## 6. After the first non-import receipt

Never switch back to Sheets, never delete receipts, never replay an uncertain write to fix a latency
number.

- **Go G21**, separate containment/settings go BEFORE the fence and database restore: set
  `MIA_KILL_SWITCH=true` in the approved settings plan, retain `MIA_ASSAF_OS_FREEZE=true`,
  then `sudo mia settings`. Verify `/health` reports `kill_switch=true` from the running app;
  pause jobs (`sudo mia jobs pause`). Keep the kill switch on through
  the restore, the second fence, both authority steps and the new epoch's settings reload.
  This also stops the app's mirror daemon, which paused jobs do not control: WP1/E35 permits
  that daemon to publish under freeze on an active core. A freeze alone is insufficient.
- **Go G22** the ledger fence. `sudo mia os-authority fence --approved-sha S` fences `core` (and
  `statements` if active) and stops all ledger writes; it is idempotent. It flips the authority state
  on production, so it is **not pre-approved**: it needs Assaf's explicit statement in chat for that one
  use. If he wants it to be a standing emergency stop, he says so in chat and the operator records his
  words in `OS_CUTOVER_GO_NO_GO.md`; until then it is not one.
  Bookkeeping can never veto this containment: unreadable/unsafe evidence reports
  `evidence_written=false`, warning `restore_evidence_invalid`; an unwritable directory reports
  `evidence_written=false`, warning `evidence_unwritable`. Confirm core is fenced using status.
  Rollback uses this same incident recorder, including the `core_left_fenced` race in section 5.
- **Go G21**, a separate restore go: `sudo mia restore <dump>`. Follow
  `ops/gcp/backup/README.md` to verify/decrypt the selected dump and reapply the restored role
  posture. Record the incident epoch **E** from status BEFORE restoring; the dump must have that
  same epoch. The restore starts services again, so the kill switch must also hold in their
  loaded settings. The original `ledger-fence.json` remains outside the dump; do not replace
  its `fence_started_at` or completed `fenced_at` timestamps. A failed initial evidence write
  still fences but leaves recovery blocked. A crash after fencing retains the start; retry uses a conservative
  later completion time for RPO. For an already-fenced ledger with missing incident evidence,
  `fence` remains a no-op; `restore-begin` refuses `restore_evidence_invalid`. STOP and recover
  the original evidence with the coordinator. The fence record and restore session bind only
  workspace and epoch, so an approved fix can be deployed between fence and restore-begin.
  `restore.json` still binds the current approved SHA S.
- **STOP — first fence impossible / evidence directory lost.** If the database was unavailable
  before the first fence, or a new VM lost `/var/lib/mia/os-authority`, this needs the
  owner's explicit exception procedure. Keep containment on; the standard restore commands
  cannot authorize this exception and there is no bypass flag. Recover the original incident
  record from an independent source if possible. Otherwise the coordinator must prepare a
  separate owner-run recovery procedure documenting the unknown incident time, accepted data
  loss, restored dump/epoch/SHA and independent posture/authority validation; obtain the owner's
  explicit approval before executing that procedure. Do not fabricate timestamps or clear a flag
  to make the standard commands pass.
  Before replacing the database, `mia restore` writes `restore-attempt.json` outside the dump,
  with boolean `pre_restore_fence_record_present` (file presence, including unsafe links).
  A fence minting a record under an outstanding restore attempt without a matching prior fence sets
  `fence_record_minted_after_restore=true`. This conservative signal also catches evidence lost
  after the restore attempt began, and a stale lower-epoch record from an earlier incident.
  Successful restore-finish archives the consumed attempt as `restore-attempt-superseded-<UTC>.json`;
  a retry at E+1 repairs an archive failure, without another bump. An unreadable attempt record blocks evidence creation, never
  containment. `restore-begin` refuses `restore_evidence_invalid`; `restore-finish` also refuses
  and reports `rto_within_limit=null` and `rpo_within_limit=null` for that flagged session.
  The RTO/RPO booleans are meaningless in this case: post-restore timestamps prove neither limit.
- **Verify**: run status after `mia restore` and compare the epoch with the incident E:
  `sudo mia os-authority status --approved-sha S`. If different, STOP and select a compatible dump.
- **Go G22**, a separate post-restore fence go: `sudo mia os-authority fence --approved-sha S`.
  A dump can restore an `active` authority row. Fence it again before proceeding. This preserves
  the first fence time at E in `ledger-fence.json`; `statements` is fenced if it was active.
  Do not permit traffic or jobs between the restore and this fence. If the restored epoch differs
  from E, STOP and use a compatible dump and the coordinator's recovery plan. If a second fence
  has already run against E0 < E, it still contains writes and preserves the higher-epoch record;
  `restore-begin` refuses `restore_epoch_mismatch` rather than replacing the incident time.
- Record the restored dump's actual consistent-snapshot timestamp (backup manifest
  `created_at_utc`, NOT the download, decrypt or restore completion time) in
  `/var/lib/mia/os-authority/restore.json`, owned by UID 10001, mode 0600:
  `{"v":1,"workspace":"<configured workspace>","epoch":E,"release_sha":"S","dump_created_at":"<UTC ISO-8601>"}`.
  Replace E and S with the actual integer and approved 40-hex image SHA. This is operator evidence:
  verify it belongs to the exact dump restored using the backup manifest and hashes. The CLI
  validates its binding and timestamp; it cannot prove that attestation from database rows.
- **Go G21**, its own explicit go: `sudo mia os-authority restore-begin --expected-epoch E --approved-sha S`.
  Requires `core=fenced` and `statements=shadow|fenced`. It pins the incident time in
  `restore-session.json`, then transitions `core` to `restoring`. Repeating at E is a no-op.
- **Go G21**, another explicit go: `sudo mia os-authority restore-finish --expected-epoch E --approved-sha S`.
  Requires `core=restoring`, the host's `/etc/mia/restore-incomplete` marker absent
  (`restore_incomplete` otherwise), and the same `main_role_unsafe` / `ledger_role_unsafe` posture
  checks as preflight. It refuses missing/malformed/mismatched or future evidence with
  `restore_evidence_invalid`, a different epoch with `restore_epoch_mismatch`, and a missing E
  with `restore_epoch_required`. Wrong states refuse `authority_state_invalid`; wrong image SHA
  refuses `release_sha_mismatch`. RPO **<= 15 min** is a hard gate (`restore_rpo_exceeded`):
  missing data beyond E36's limit cannot be approved implicitly by this command. Choose a newer
  verified dump; an exception needs a separate coordinator/owner decision and procedure.
  The legal transition atomically sets core active and bumps both domains' epoch to E+1 exactly
  once. Keep passing the ORIGINAL E on retry: both commands report `already_restored` at E+1.
  Statements remain fenced or shadow; this procedure does not independently activate them.
- `sudo mia os-authority status --approved-sha S`. Check `core=active`, epoch E+1. Under its
  own settings go, set `MIA_ASSAF_OS_AUTHORITY_EPOCH=E+1`, retain `MIA_ASSAF_OS_BACKEND=postgres`,
  ledger and mirror enabled, then `sudo mia settings`; check status `settings.epoch_matches=true`
  and the approved health SHA. Under separate go, remove the kill switch, unfreeze and resume
  jobs as appropriate, after verifying those settings were loaded at E+1.
  The existing mirror/outbox worker resumes through the normal active-state/epoch gates; do not
  replay old receipts or change the mirror. Verify mirror readback before accepting recovery.
- Check E36's **RTO <= 2 h**, measured from the ORIGINAL fence start through completed activation
  and end-to-end service recovery, and **RPO <= 15 min**, dump snapshot to ORIGINAL fence.
  `restore-finish` reports only booleans/codes for these timestamps: `rpo_evidence_recorded`,
  `rpo_within_limit`, `rto_within_limit`; `restore_rto_exceeded` is a warning, because refusing a
  safe activation would prolong an already late outage. Record the full service recovery time
  separately: command activation alone does not prove RTO or live mirror/outbox delivery.

## 7. Reason codes

The 15 preconditions of `activate`, in the contract's order. `preflight` reports all of them; `activate`
refuses on any `fail` and on any `pending`.

| Code | It holds when | If it refuses |
|---|---|---|
| `release_sha_mismatch` | The image SHA equals the approved SHA (checked before anything runs) | Wrong `--approved-sha`, or a stale image: deploy `S` |
| `sheets_not_fenced` | The signed read-only Script probe returns `unavailable` | Set `ASSAF_OS_MODE=FENCED` (T3); `unknown` is not a pass |
| `freeze_off` | `settings.assaf_os_freeze` is true | The freeze must still be on at `activate` (T1) |
| `capture_before_fence` | The signed capture time is later than the unbroken `fence_probe_at` (a probe that saw the Script live restarts it; an `unknown` probe does not) | A capture before the fence: take new ones (T4) |
| `main_role_unsafe` | The main DSN passes the V2-14a posture check | Step 2 not enforced |
| `ledger_role_unsafe` | The V2-15 grant-map validator passes | Step 4 not done, or a posture drifted |
| `sheets_pending_proposals` | `drain.json`: zero pending, taken before the fence and after any lifted fence (`fence_broken_at`), at most 2 hours before `fence_probe_at`, same epoch and SHA | Settle and redo T1 and T2, then T3's probe (`no_drain_evidence`, `drain_after_fence`, `drain_before_fence_break`, `drain_stale`) |
| `sheets_unknown_receipts` | Zero Sheets receipts unknown | Resolve by receipt, never replay |
| `approval_cards_open` | Every `assaf_os.approve` card is terminal | Settle or let it expire |
| `backup_too_old` | `backup.json` shows a dump under 1 h old | Re-run `--status` after a fresh backup |
| `restore_drill_too_old` | `restore-drill.json`: passed, within 7 days | Re-run the drill |
| `mirror_not_verified` | The mirror is empty or verified | Wait for a verified mirror, or fix the conflict |
| `manifest_not_signed` | `manifest.json` verifies every binding of section 2 | Re-sign the exact digest |
| `fe_v2_reader_not_live` | `fe-v2.json` carries the Cloudflare version and bundle hash | Record the live reader |
| `journal_capacity` | `journal.json`: fresh, exactly the five tabs and `max_rows` 2000, each tab at most 90 percent of the cap | Fix the file (`tabs`, `max_rows`), or archive rows (`at_cap`) |

Other refusals of the CLI:

| Code | Meaning |
|---|---|
| `import_not_promoted` | `activate` (and `preflight`, as a `gates` entry, `pending` until T5) found no committed import receipt at this epoch that binds the digest of the signed `manifest.json`: promote was skipped, ran for another manifest, or ran at an earlier epoch. Run T5 at this epoch with the signed digest |
| `workspace_missing` | `init` has not run (step 5); `freeze-plan` needs the workspace for the epoch it prints (E29) |
| `authority_state_invalid` | The command does not apply to the current `core` state (for example `freeze-plan --phase revert` before `rollback` has fenced it, or `--phase drain` once `core` is no longer `shadow`) |
| `final_accept_flag_required` | `preflight --drain` needs `--final-accept` |
| `drain_requires_live_sheet` | The drain snapshot needs a live Sheet; run it before T3 |
| `drain_keys_not_applied` | `freeze-plan --phase switch` while `MIA_ASSAF_OS_LEDGER_ENABLED=true` and `MIA_ASSAF_OS_AUTHORITY_EPOCH=E` (set at T1) are not in the settings the job reads |
| `switch_not_applied` | `freeze-plan --phase mirror` or `open` before the switch is in the settings |
| `rollback_after_first_write`, `rearm_after_first_write` | A non-import receipt exists at this epoch. Alone, `rollback_after_first_write` means nothing was changed and `core` is still live. `rollback` also counts again after it fences `core`: if a commit slipped in between, it adds `core_left_fenced` (below) and prints nothing else (section 5, then section 6) |
| `core_left_fenced` | Always printed after `rollback_after_first_write`: `core` is **fenced** (`"core": "fenced"` in the JSON), the ledger refuses every OS write and the Sheet is NOT the record. Follow section 6; never return to Sheets |
| `authority_changed:<reason>` | Another owner or epoch, or a transition the state machine does not allow |
| `ledger_unavailable:<reason>` | The pepper does not match its pin, or the ledger cannot be read |
| `evidence_unwritable` | The evidence directory cannot be written |
| `restore_incomplete` | The host restore-incomplete marker remains; rerun/repair `mia restore` under containment before restore-finish |
| `restore_rpo_exceeded` | The dump snapshot is over 15 min before the original completed fence; select a newer verified dump or stop for a separate owner-run exception procedure |
| `restore_evidence_invalid` | Missing, unsafe, malformed or mismatched evidence, invalid timestamps, or a fence first minted after restore; STOP per section 6, recover original evidence or obtain the owner's explicit exception procedure |
| `restore_epoch_mismatch` | Status, incident record or restore session does not match the original E; compare epochs after restore and select a compatible dump |
| `restore_epoch_required` | Supply the original incident E with --expected-epoch on restore-begin/restore-finish |
| `restore_epoch_invalid` | --expected-epoch must be a positive integer; this differs from a missing E |
| `restore_rto_exceeded` | Warning on successful restore-finish (also on crash retry): activation took over 2 h from the original fence start. Keep the warning in the incident report; separately measure full service recovery |
| `dsn_missing`, `owner_ambiguous`, `workspace_invalid` | The job environment is incomplete |
| `internal_error` | An unexpected failure; its text is never printed |

## 8. Owner gates, in one list

Each needs its own explicit statement in chat, for that step only.

| Go | Gate |
|---|---|
| G01 | Exact backend deployment (default-off features included) |
| G02 | Exact frontend deployment |
| G03 | Exact Apps Script deployment, if this release needs one |
| G04 | Closing loopback `trust` in the VM's PostgreSQL (E21) |
| G05 | V2-14a main-role demotion |
| G06 | V2-14/V2-14b schema migration |
| G07 | V2-15 role grants and provisioning, and the validator |
| G08 | Authority initialization and the workspace and pepper pin |
| G09 | The rehearsal |
| G10 | Drain/freeze and final reconcile in the approved window |
| G11 | The Script Properties fence, archive naming and tab warnings |
| G12 | Import promotion of the exact signed manifest |
| G13 | Authority activation at the recorded epoch |
| G14 | The backend switch to postgres, then the health check |
| G15 | Enable mirror to the pinned new mirror workbook |
| G16 | Admitting the first non-import PG write |
| G17 | Phone acceptance, including p95 and restore RTO and RPO |
| G18 | Removing the old workbook's `reconcileAssafOsEdit` onEdit trigger, after acceptance |
| G19 | A rollback before the first non-import receipt |
| G20 | Each later feature release |
| G21 | Each restore |
| G22 | `fence`: stopping all ledger writes after the first non-import receipt (not pre-approved) |
| G23 | `init --rearm`: `fenced` to `shadow` with one epoch bump, to try again after a rollback |
| G24 | Staging the import into the production ledger (`os-job os-import`), before the promote of G12 |
