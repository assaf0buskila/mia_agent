**FROZEN v1 (2026-10-16)**

# OS v2 on PostgreSQL: frozen contracts for the parallel build (V2-14a to V2-39)

Frozen 2026-10-16 from these sources:
- `v2-design-full.json`: the roadmap (architecture, one_tap_undo_semantics, assistant_access,
  bank_block_model, chunks V2-14..V2-41) and both critiques;
- issues #122-#152;
- backend live `4c8eed5`;
- V2-14, **merged** as PR #157 → master `12254fa` (last fix `783e6bf`): `migrations_assaf_os/`,
  `app/db/os_ledger_migrate.py`, `app/workers/os_ledger.py`;
- V2-15 at `d7ded86` (branch `codex/os-v2-15-ledger-role`, on `12254fa`);
- V2-14a as built, uncommitted in `backend-v2-14a` (on `4c8eed5`);
- the default-off prototypes `app/prototypes/assaf_os_{primary,ledger,adapter}.py`;
- the in-flight V2-25 domain (`backend-statements`, `app/domain/statements/`);
- V2-08 (`backend-v2-08`);
- frontend v15 (`91afca6`).

**Revision.** v1 applies adversarial review r1 (22 refutations, 12 missing items). Every item was
accepted except the parts listed in §21, each with its reason.

**Purpose.** Freeze every interface so the chunks below can be built in parallel now. They run against
an in-memory FakeLedger until the real ledger core (V2-16, Codex) lands. Builders implement this file
exactly. A needed deviation goes to the main session as a fork and is never improvised in a branch.
Where this file is silent, the code wins.

**Approval scope.** Assaf's standing approval (relayed 2026-10-16) covers proceeding on the
recommended option of every fork until the wave-6 cutover, so §0 decides all of them.
- Everything built here is default-off.
- Wave 6 still needs his direct go for each step: the Script Properties fence, import promotion,
  authority activation, `backend=postgres`, enabling the mirror, and removing the trigger.
- Production DB changes before wave 6 (the V2-14a demote, applying V2-14/14b, the V2-15 grants, and
  `mia os-authority init`) follow the runbook in the FORK-PROD-ORDER order: jobs paused, a verified
  non-empty backup, the exact CI-green SHA, and evidence in HANDOFF.
- The handoff rule was "always ask before a deploy with a DB migration". The main session therefore
  confirms once with Assaf that the relayed approval covers those three changes before the first is
  applied.

**Waves.**
- Wave 4: foundation plus the parallel build, everything below.
- Wave 5: PG integration, the FE v2 release, and the rehearsal (V2-35/35a).
- Wave 6: the cutover (V2-36).

## 0. Forks, decided on the recommended option

| Fork | Decision | Why |
|---|---|---|
| FORK-DB | Schema `assaf_os` in DB `mia`, own owner/runtime/retention roles (owner decision; V2-14 built so) | One dump and restore path. |
| FORK-SESSION / FORK-REFRESH | 12 h idle / 14 d absolute; refresh every 3 min plus on return (owner decisions) | Already binding. |
| FORK-DELETE | Tasks: tombstone (`deleted_at`), restorable. Leads, clients, works, payments, time, expenses: the existing cancellation status. Refused with `has_dependents` when a live referrer exists, using the one `DEPENDENTS` table (§5.1, §11.1). | Summaries already exclude cancelled rows, and no FK ever breaks. |
| FORK-UNDO-WINDOW | 30 s server window, 8 s undo bar, a "last action" chip until `undo.until` | Covers a mis-tap and keeps undo conflicts rare. |
| FORK-EXPENSE-DATE | `charge_date` | Cash basis, so the calendar P&L equals the billing cycle. |
| FORK-PERSONAL-MIN | On, plus HMAC-peppered line keys and no personal detail in proposals, events, outbox or receipts | The only way "not kept" is true (critique 0 #3). |
| FORK-BATCH-EXPIRY | 7 days | He imports by the 20th, and a week absorbs a slip. |
| FORK-ISLAND | Not taken. V2-34 is NOT_INCLUDED and V2-26 has no island flag. | Oct and Nov are chat-only anyway, and one P&L authority beats a split December. Revisit at the V2-35 go/no-go if the cutover misses the Dec 20 cycle. |
| FORK-MIRROR-WB | A new workbook, read-only, shared only with the mirror service account; the old book is the frozen archive | Native edits can't create a second authority. |
| FORK-MIRROR-TRANSPORT | Google Sheets API v4 via a dedicated service account (RS256 JWT through `pyjwt[crypto]`, already a dependency, plus stdlib urllib), not Composio | Business data stays inside Google, with no new processor and no new dependency. |
| FORK-XLSX | A stdlib `zipfile` + `xml.etree` reader with hard caps; no openpyxl and no CSV fallback | CSV can't export the hidden `_assaf_os_*` journal tabs. |
| FORK-BACKUP | V2-10 (coordinator): whole-DB consistent dump every 15 min with sha256 recorded, an off-VM drill, `--exclude-table-data=assaf_os.os_statement_batch_lines` | One restore point for ApprovalRow and the ledger together. |
| FORK-ASSIST | Paste now, then static propose-only keys (V2-37/38). OAuth (V2-40) is NOT_INCLUDED until a client he uses requires it. | No public OAuth surface without a need. |
| FORK-CONNECTIONS | Managed in OS > Connections | Session + CSRF, one UI. |
| FORK-STEP-UP | Issuing a key needs a Google re-auth (`max_age=0`, `auth_time` within 600 s), proved by a 10-min HMAC step-up token bound to the session hash | A silent-SSO `iat` does not prove re-authentication (critique 0 #5). |
| FORK-SUPERSEDE | Auto-supersede only within the same credential, or between two owner pastes. A different source gives `conflict`: both stay reviewable, and a batch with a saved draft is never discarded without an owner action. | Critique 0 #4. |
| FORK-DRAFT-VERB | `POST /statement-batches/{id}/draft` | Today's boundary buffers and caps only POST bodies (`owner_os_boundary.py:71`), so POST is the proven path. V2-16s still extends the checks to every non-GET method. |
| FORK-STALE | Declined; superseded by the V2-42 server cache | No device storage. |
| FORK-FE-CI | Resolved: read-only deploy key (`e8c8d9a`) | — |
| FORK-APP-ROLE | Ownership transfer, as V2-14a built it: `mia_app` owns every `public` object and has USAGE, CREATE on `public`; `mia` stays superuser for backup, restore and `mia os-ledger` only (§18.1) | `migrate` and `app` share `/etc/mia/runtime.env`, so `mia-migrate` runs as `mia_app` after the switch and must be able to ALTER. `mia_app` already holds DELETE on all of `public`, so owning it adds little. The goal, no access to `assaf_os`, is unchanged. |
| FORK-LEDGER-DSN | One name: `MIA_OWNER_OS_LEDGER_DATABASE_URL` / `owner_os_ledger_database_url` (V2-15's), written to VM-owned `/etc/mia/runtime.env` by the provision step, never to `mia-env` | Already built and pinned by V2-15's tests. |
| FORK-PROD-ORDER | Production order: (1) V2-14a demote → (2) `mia os-ledger migrate` (V2-14 + 14b) → (3) V2-15 provision + validator → (4) `mia os-authority init` (`ensure_workspace`, epoch 1, pepper pin) → (5) rehearsal | Provision GRANTs on `assaf_os.<table>` and checks schema ownership, so the schema must exist first (§19). |

## 1. Invariants (every chunk; every reviewer checks every line)

- **I1 Exactly-once.**
  - Unique keys: `(workspace, request_id)` on `os_proposals`; one receipt per proposal; unique
    decision ids; unique `(workspace, undo_of)`.
  - A replay with an equal request digest returns the stored result, whatever its status. A different
    digest returns 409 `idempotency_mismatch` and writes nothing.
  - Client request ids and server decision ids share one receipt keyspace (`os_receipts` PK). A
    fused-kind id (owner_direct, owner_undo, abandon) that equals any proposal's `approval_id` or
    `rejection_id`, or any receipt key, is refused with `idempotency_mismatch` before any insert
    (§5.3).
  - Each write is one PG transaction under `SELECT … FOR UPDATE` of `os_workspaces`, so any failure
    rolls back everything.
  - Nothing (server, Worker, client) ever auto-re-POSTs. Recovery is a read.
- **I2 Server-decided truth.**
  - Only a committed ledger receipt says a write happened.
  - The model never asserts a write, a delivery, a contact or a mirror state.
  - UI rows change only from server-returned records.
  - The mirror is never the save confirmation. The UI says "Sheet verified HH:MM (vN)".
- **I3 No dual authority.**
  - For each domain, exactly one of Sheets or PG accepts business writes. Three things enforce this:
    the backend factory, `os_authority` state + epoch, and the Script Properties fence.
  - Mirror data re-enters PG only through the V2-30 import.
- **I4 Owner-only one tap.** commit, undo and abandon require all of: a cookie session, the exact
  Origin, CSRF, `assaf-os:direct`, the numeric owner re-check, the kill switch off, `core` active,
  and an epoch match. commit and undo also require the freeze off; abandon writes no business change,
  so the freeze allows it.
  - `OS_DIRECT` is assigned by the server.
  - Any request carrying an `Authorization` header is refused.
  - A route/tool enumeration test proves these paths are unreachable from MIA, Telegram and bearer.
- **I5 Model writes need approval.**
  - Every MIA or ASSISTANT_* write is an immutable, expiring proposal.
  - Decide happens only from OS (session + CSRF + `assaf-os:approve`) or TELEGRAM_APPROVAL (single
    proposals only, after the ApprovalRow CAS). Statement batches are decided from OS only.
  - An approved model proposal is never undoable through `/undo`.
- **I6 Numeric owner.** Every context carries the numeric owner, re-checked against
  `telegram_owner_user_id_set()` on every call. A username never grants. Every `owner_id` column
  has `CHECK '^[0-9]+$'`.
- **I7 No payload logging.**
  - Log lines are `<area> op=<op> outcome=<code> reason=<code> ms=<int>`, plus booleans and counts.
  - Never logged: bodies, record fields, merchant text, amounts, person ids, tokens, provider
    payloads, or DB error text.
  - Audit rows carry no payload.
- **I8 Kill switch and freeze everywhere.**
  - `settings.kill_switch` blocks: propose, decide, commit, undo, abandon, batch submit and approve,
    the assistant submit, key issue, mirror publish and import promote. That includes the Mia owner
    tool loop.
  - `assaf_os_freeze` (the T1 drain) blocks every **new intent** on both backends with 503
    `os_frozen`: propose (OS and MIA), commit, undo, batch submit and the assistant submit. Decide,
    reject and abandon stay allowed, so the drain can settle every pending item. On the Sheets path
    V2-17 enforces it in `AssafOsPort._call`; on the ledger path, the intent-creating services enforce
    it (§5.3).
- **I9 Fail-closed contracts.**
  - Readers are exact-key. Unknown keys, values or `actor_source` fail.
  - v2 is emitted only under `backend=postgres`, and v1 stays byte-identical under `sheets`.
  - The FE dual reader is live before any v2 emission.
- **I10 Least privilege.** §18. The ledger runtime role has no DELETE or TRUNCATE. Events, receipts
  and audit are INSERT-only. The main app role is non-superuser, owns only `public`, and has no
  access to `assaf_os`. Validators fail closed. A restore re-applies every role posture before the
  services start (§18.1).
- **I11 Privacy.**
  - Never stored: a raw statement, a card-like digit run, a bank credential.
  - Personal line detail lives only in `os_statement_batch_lines.detail` while the batch is pending,
    and never longer than `expires_at` + 1 day, whatever the kill switch, backend or authority state
    (expiry admission §5.3, retention backstop §18.2).
  - Personal lines are never mirrored. Line keys, personal rule keys and personal rule ids are
    HMAC-peppered. The pepper is write-once per workspace (§12.2).
- **I12 Epoch.** Anything from another epoch is refused: a context, proposal, receipt, cursor, marker
  binding or credential. Bumping the epoch invalidates them all.
- **I13** No Google call is made inside a DB transaction or under the workspace lock.
- **I14 Default-off.** A merge changes no production behaviour.

## 2. Chunks: author, start, merge gate

Format: author → reviewer (never the same agent). The bullets say when the chunk can start and what it
builds against. "Merge after" is the merge gate.

**Scaffold and fake (first)**
- **V2-16s PG-SCAFFOLD:** Claude opus (main session) → Codex gpt-6-astra. Starts now and builds against
  §3 and §5.1. Merge after: V2-15 (V2-15 creates `app/services/os_ledger/__init__.py`); rebased onto
  V2-08 if V2-08 merges first.
- **V2-16f FAKE-LEDGER + conformance suite:** Claude sonnet → a fresh Claude opus. Starts now from this
  text and builds against §5 and §6. Merge after: V2-16s.

**Ledger foundation**
- **V2-14 LEDGER-SQL:** Codex → Claude opus. **MERGED** (#157 → `12254fa`).
- **V2-15 LEDGER-ROLE:** Codex gpt-6-astra → Claude opus. Built at `d7ded86`; returns for **r2**
  against §18.2:
  - the exact record-table UPDATE columns;
  - `provision_ledger --mode reapply`;
  - a production target pin whose `--apply` writes the ledger DSN to VM-owned `runtime.env`;
  - the catalog-derived migration role (§5.5).
  Retention stays reserved NOLOGIN here (V2-14b owns it). Merge after: V2-14 (merged). It is the next
  backend merge.
- **V2-14b SCHEMA-ADDENDUM:** Codex gpt-6-astra → Claude opus. Starts now, on V2-15's head
  (`d7ded86`), and builds against §4 and §18.2. It rebases onto V2-15's merge. Merge after: V2-15,
  V2-16s and V2-16f (its CI line names their test directories, §3.4).
- **V2-14a APP-ROLE-DEMOTE:** Claude opus → Codex gpt-6-astra. Built (uncommitted) on `4c8eed5`.
  Finishing work: rebase onto master (V2-14 is there), import `_validate_main_role_catalog` instead of
  copying it, add the `reapply` mode, and declare `app_role_posture` as `str` (§3.3). Builds against
  §18.1 (rewritten to the as-built design). Merge after: V2-16s (`main.py`/`config.py` handoff).
  Its production apply is step 1 of FORK-PROD-ORDER.
- **V2-10 BACKUP + RESTORE:** coordinator (Claude opus) → Codex. Outside this file's chunk list, but
  it owns `mia restore`'s post-restore step (§18.1) and the restore-drill test. Merge after: V2-14a
  and V2-15 (it calls both `reapply` modes).
- **V2-16 LEDGER-CORE:** Codex gpt-6-astra (design) + gpt-5.6-sol (port) → Claude opus. Starts once
  V2-16s, V2-16f and V2-14b are merged, and builds against §5 and the conformance suite. Merge after:
  V2-14b.

**Backend build-out**
- **V2-17 OS-BACKEND:** Claude opus → Codex. Starts now, on the fake, and builds against §7. Its
  fence reads authority only through `api.core_authority_state` (V2-16s stub), so it needs no
  `TrustedOwnerContext` and does not wait for V2-19. Merge after: V2-16 and the V2-42 backend.
- **V2-18 READ-MODEL:** Codex gpt-5.6-sol → Claude opus. Starts after V2-14b (its tests seed rows with
  owner-role SQL) and builds against §8. Merge after: V2-16.
- **V2-19 AUTH-BIND:** Claude opus → Codex. Starts now, on the fake, and builds against §9. Merge after:
  V2-08 and V2-17.
- **V2-20 CONTRACT-V2-BE:** Codex gpt-5.6-sol → Claude opus. Starts now, on the fake, and builds
  against §10. Merge after: V2-18 and V2-19.
- **V2-24 OWNER-DIRECT-BE:** Codex gpt-6-astra → Claude opus. Starts now on the fake `LedgerTx` and
  builds against §11.1. Merge after: V2-19.
- **V2-25 STMT-DOMAIN:** coordinator (Claude opus) → Codex. In flight. A pure domain, so it merges at
  any time.
- **V2-26 STMT-BATCH-BE:** Codex gpt-6-astra → Claude opus. Starts once V2-25 is merged and builds
  against §12. Merge after: V2-19 and V2-25.
- **V2-28 TELEGRAM-LEADS-PG:** Codex gpt-6-astra → Claude opus. Starts once V2-17 is merged and builds
  against §13. Merge after: V2-20.
- **V2-29 MIRROR:** Claude opus → Codex. Starts now on a fake reader and a fake transport, and builds
  against §14. Merge after: V2-20.
- **V2-30 IMPORTER:** Codex gpt-5.6-sol → Claude opus. Starts now and builds against §15. Merge after:
  V2-16.
- **V2-31 FENCE + CUTOVER TOOLING:** Claude opus → Codex. Starts now and builds against §16. Merge
  after: V2-29 and V2-30.
- **V2-37 ASSIST-API:** Codex gpt-6-astra → Claude opus. Starts now with a stub
  `StatementBatchService` and builds against §17. Merge after: V2-26 and V2-08 (its step-up helper
  sits in `owner_google_login.py` after V2-08).
- **V2-38 ASSIST-MCP:** Claude opus → Codex. Starts now against stub routes and builds against §17.4.
  Merge after: V2-37.

**Frontend**
- **V2-21 CONTRACT-V2-FE:** Claude sonnet → Codex. Starts now, with hand fixtures from §10, and builds
  against §10, §12.4 and §17.3. Merge after: the FE chain head (after V2-09 and V2-42 fe).
- **V2-23 FE-CI-PG:** Claude sonnet → Codex. Starts now. Merge after: V2-21.
- **V2-22 PAGED-BOOT:** Claude sonnet → Codex. Starts after V2-21 and builds against §10.6. Merge
  after: V2-21.
- **V2-32 OWNER-DIRECT-FE:** Claude sonnet → Codex. Starts after V2-21 and builds against §11.2. Merge
  after: V2-24's fixtures.
- **V2-33 TASKS-FAST:** Claude sonnet → Codex. Starts after V2-32 and builds against §11.3. Merge after:
  V2-32.
- **V2-27 STMT-FE:** Claude sonnet → Codex. Starts after V2-21 and builds against §12.4. Merge after:
  V2-26's fixtures.
- **V2-39 CONNECTIONS-FE:** Claude sonnet → Codex. Starts after V2-21 and builds against §17.3. Merge
  after: V2-37's fixtures.

**Not included:** V2-34 and V2-40 (see §0).

**Concurrency and working rules.**
- Claude agents: at most 5 across both sessions (Assaf, 2026-10-15).
- Codex lanes run one per standalone clone. Codex cannot commit, so Claude commits Codex's tree
  unchanged.
- Each builder works in its own worktree, branched from the latest `origin/master` or the agreed FE
  chain head. Commands run in the foreground, with output redirected to a file.
- Stop conditions for every builder:
  - no push to master, no deploy, no gcloud, no `.env` read, and no provider or Google writes;
  - no edit outside its owned files;
  - after two failed fix attempts, stop and report a repro.

**Starts now (FROZEN v1), within the 5-Claude-agent cap; Codex lanes run one per standalone clone and
do not count:**
- Claude, wave A:
  - V2-16s (opus, main session);
  - V2-16f (sonnet);
  - V2-14a finishing work (opus);
  - V2-19 on the fake (opus);
  - V2-21 (sonnet).
- Claude, wave B, as wave-A slots free up: V2-17, V2-29, V2-31 and V2-38 (opus), then V2-23 (sonnet).
- Codex:
  - V2-15 r2 (astra, in `backend-codex-clone`);
  - V2-14b (astra, a new clone on `d7ded86`);
  - V2-24 (astra), V2-30 (sol), V2-20 (sol) and V2-37 (astra), each in its own clone.
- Coordinator: V2-10's restore post-step, V2-25 and V2-42.
- Waits for its prerequisite: V2-16 (V2-16s, V2-16f and V2-14b merged); V2-18 (V2-14b); V2-26
  (V2-25); V2-28 (V2-17); V2-22, V2-32, V2-27 and V2-39 (V2-21); V2-33 (V2-32).

## 3. File ownership (no two open branches edit the same file)

### 3.1 Backend (mia_agent)

- **V2-16s:** `app/services/os_ledger/api.py` and `app/api/os_v2/{__init__,errors}.py`. It does
  **not** create `app/services/os_ledger/__init__.py`; V2-15 does, and V2-16s rebases onto it.
  It also creates stubs, each `router = APIRouter()` with no routes:
  `app/api/os_v2/{reads,commits,statements,credentials}.py`, `app/api/assistant/{__init__,os,mcp}.py`
  and `app/api/owner_step_up.py` (`APIRouter(prefix="/v1/owner/auth")`, filled in by V2-37).
  Other files:
  - `app/workers/os_mirror.py`, a stub whose `start_if_enabled(settings) -> None` does nothing;
  - `app/main.py`: includes every router above, the `LedgerError` handler, and a lifespan call to
    `os_mirror.start_if_enabled`;
  - `tests/api/os_v2/{__init__,conftest}.py` and `tests/api/os_v2/test_os_v2_mount.py` (one test:
    the v2 routers mount with zero routes), so the CI path exists from V2-16s on (§3.4);
  - `app/core/config.py`: the §3.3 block, plus a change to `allowed_sheets_spreadsheet_ids()` that
    subtracts `os_mirror_spreadsheet_id` always, and subtracts `assaf_os_workbook_id` when
    `assaf_os_freeze` or `assaf_os_backend == "postgres"`;
  - `.env.example`: the names only;
  - `app/api/owner_os_boundary.py`: per-route caps (§10.5); every non-GET method buffered, capped,
    content-type-checked and duplicate-key-checked. The v2 write routes are **not** added to
    `WriteDispatch`: its `dispatched` counter only moves inside `AssafOsPort._call`, so registering a
    v2 route would stamp `not_started` on a v2 `unknown`. A v2 route's `write_outcome` comes only
    from its `LedgerError` (§5.2);
  - `tests/unit/test_os_v2_scaffold.py`.
- **V2-16f:** `tests/fakes/{__init__,os_ledger,contexts}.py` and
  `tests/services/os_ledger/{__init__,conftest}.py`, plus `tests/services/os_ledger/conformance/*`
  and `tests/services/os_ledger/test_pg_sentinel.py` (§3.4).
- **V2-15** (as built at `d7ded86`, plus r2):
  - `app/services/os_ledger/__init__.py` (the only chunk that creates it), `role_policy.py`;
  - `ops/assaf-os/{provision_ledger.py,LEDGER_ROLE.md}`;
  - `tests/services/test_os_ledger_role_policy.py`;
  - `app/core/config.py`: only `owner_os_ledger_database_url` (FORK-LEDGER-DSN);
  - `.env.example`: that name only;
  - `app/db/os_ledger_migrate.py`: only `RUNTIME_DSN_ENV`; `migrations_assaf_os/README.md`;
  - `.github/workflows/ci.yml`: only the `os-ledger-migration-postgres` pytest line, and its pin in
    `tests/unit/test_os_ledger_review_r1.py`.
- **V2-14b:**
  - three `migrations_assaf_os/20261016_*.sql` files (§4);
  - `app/db/os_ledger_migrate.py`: only `MIGRATIONS` and `TABLES`; `migrations_assaf_os/README.md`;
  - `app/services/os_ledger/role_policy.py`, `ops/assaf-os/provision_ledger.py` and
    `tests/services/test_os_ledger_role_policy.py`: the extension for its 7 tables, the new
    `os_workspaces`/`os_receipts` columns, and the retention role (§18.2);
  - `tests/db/test_assaf_os_ledger_addendum.py`;
  - `.github/workflows/ci.yml`: only the `os-ledger-migration-postgres` pytest line (§3.4), and its
    pin in `tests/unit/test_os_ledger_review_r1.py`.
- **V2-14a** (as built):
  - `ops/gcp/vm/{app-role-demote.sh,app_role_demote.py}` and `ops/gcp/vm/sql/app-role-demote.sql`;
  - `docs/runbooks/APP_ROLE_DEMOTE.md`;
  - `app/db/app_role_posture.py`;
  - `tests/unit/test_app_role_{demote_postgres,demote_script,posture}.py`;
  - `app/core/config.py`: only `app_role_posture` (a plain `str`, §3.3);
  - `app/main.py`: only `health_ready`;
  - `.env.example`: only `MIA_APP_ROLE_POSTURE`.
- **V2-10** (coordinator): `ops/gcp/vm/mia`: the `restore` post-step and the `owned` regex (§18.1);
  `tests/ops/test_restore_reapply.py`.
- **V2-16:**
  - `app/services/os_ledger/{core,tx,domain,summaries,ids,guard,authority,validate_full}.py`;
  - `tests/services/os_ledger/test_core_*.py` and `tests/services/os_ledger/ported/*`.
  The prototypes are **copied**, never moved or edited, because they remain V2-13's oracle.
- **V2-17:**
  - `app/services/os_backend.py`;
  - `app/services/assaf_os.py`: the fence only;
  - `app/api/assaf_os.py`: `_port`, `_run`, and the snapshot query passthrough;
  - constructor lines only in `app/tools/owner/assaf_os.py`, `app/services/assaf_os_leads.py` and
    `app/services/owner_actions.py`;
  - `tests/services/test_os_backend_factory.py`.
- **V2-18:**
  - `app/services/os_ledger/{reads,summaries_incremental,validate_scoped,verifier}.py`;
  - `app/services/os_ledger/core.py`: only the defaults in `build_ledger()` (handoff V2-16 → V2-18);
  - `app/workers/os_verifier.py` (exposes `main()`; the entrypoint is V2-31's);
  - `scripts/benchmark_assaf_os_ledger.py`;
  - `tests/services/os_ledger/test_{reads,summaries_property,validate_scoped_property,verifier}*.py`.
- **V2-19:**
  - `app/services/os_ledger/{authorize,adapter}.py`;
  - `app/api/owner_sessions.py`: the scope, the bearer refusal, and the mutation proof;
  - `app/api/os_v2/reads.py`;
  - `tests/services/os_ledger/test_channel_matrix.py`, `tests/api/os_v2/test_reads_routes.py` and
    `tests/unit/test_trusted_context_ast.py`.
- **V2-20:**
  - `app/services/os_ledger/contract.py` and `app/services/os_backend_pg.py`;
  - `app/domain/owner/assaf_os_follow_up.py`;
  - `app/tools/owner/assaf_os.py`: the 16,000-char model view, with merchant text tagged untrusted;
  - `scripts/export_os_contract_fixtures.py`: a **registry** that imports every
    `tests/contract/shapes/*.py` module (each exports `SHAPES: dict[str, Callable[[], dict]]`) and
    writes `tests/contract/golden/<chunk>/<shape>.json`;
  - `tests/contract/{__init__,test_goldens}.py`, `tests/contract/shapes/v2_20.py` and
    `tests/contract/golden/v2-20/*`;
  - `ci.yml`: the artifact step only.
  `tests/contract/golden/fixtures.sha256` is integrator-owned: regenerated on every rebase, never
  hand-merged.
- **V2-24:** `app/services/os_ledger/owner_direct.py`, `app/api/os_v2/commits.py`,
  `tests/contract/shapes/v2_24.py` + `tests/contract/golden/v2-24/*`, and its tests.
- **V2-25:** `app/domain/statements/*`, `tests/unit/statements/*` and `tests/fixtures/statements/*`.
- **V2-26:** `app/services/os_ledger/statements.py`, `app/api/os_v2/statements.py`,
  `app/workers/os_retention.py` (`expire_due` as runtime, then the purge and audit deletes as
  retention; exposes `main()`), `docs/STATEMENT_BATCH_FORMAT.md`, `tests/contract/shapes/v2_26.py` +
  `tests/contract/golden/v2-26/*`, and its tests.
- **V2-28:**
  - `app/services/owner_actions.py`, `app/api/telegram.py`, `app/services/assaf_os_leads.py`;
  - `app/domain/owner/proposal_cards.py`, `app/workers/crm_runtime.py`;
  - its tests.
- **V2-29:**
  - `app/workers/os_mirror.py`;
  - `app/services/os_ledger/{outbox,mirror_projection}.py`;
  - `app/integrations/sheets_mirror.py`;
  - its tests.
- **V2-30:**
  - `app/services/os_ledger/{xlsx,import_workbook,promote}.py`;
  - `app/prototypes/assaf_os_shadow_import.py`: the source check only;
  - `scripts/{os_import,os_workbook_diff}.py`;
  - `tests/fixtures/workbooks/*` (synthetic only), and its tests.
- **V2-31:**
  - `app/workers/os_authority.py`;
  - `ops/gcp/vm/mia`: the job allow-list for os-authority, os-import, os-verifier and os-retention.
    os-retention runs `app.workers.os_retention` (V2-26): `expire_due` as the runtime role, then the
    purge and audit deletes as the retention role;
  - `ops/gcp/vm/compose.yaml`: the privileged job services (profile-off, each with its own job-only
    env file, e.g. `/etc/mia/os-retention.env` holding `MIA_OS_LEDGER_RETENTION_DATABASE_URL`);
  - `pyproject.toml` `[project.scripts]`: the sole editor, adding `os-verifier`, `os-retention`,
    `os-import` and `os-authority`, each pointing at its chunk module's `main()`;
  - `ops/assaf-os/CUTOVER_RUNBOOK.md`;
  - `tests/workers/test_os_authority.py` and `tests/assaf-os/apps-script-fence.test.js`.
- **V2-37:**
  - `app/services/{assistant_credentials,assistant_step_up}.py` (the per-IP limiter lives in
    `assistant_credentials.py`, §17.2);
  - `app/api/assistant/os.py`, `app/api/os_v2/credentials.py` and `app/api/owner_step_up.py` (fills
    V2-16s's stub);
  - `app/services/owner_google_login.py`: only a new `verify_step_up_id_token(id_token, *, settings,
    now) -> int` helper returning `auth_time` (handoff V2-08 → V2-37);
  - `tests/contract/shapes/v2_37.py` + `tests/contract/golden/v2-37/*`, and its tests.
  - It never edits `ops/gcp/vm/Caddyfile` or `app/api/owner_sessions.py`.
- **V2-38:** `app/api/assistant/mcp.py`, `docs/openapi-assistant.yaml`, and its tests.

**Sequential handoffs** (A owns the file until A merges; then B rebases):
- `app/api/assaf_os.py`: V2-42 → V2-17;
- `app/api/owner_sessions.py`: V2-08 → V2-19;
- `app/services/owner_google_login.py`: V2-08 → V2-37;
- `app/tools/owner/assaf_os.py`: V2-17 → V2-20;
- `owner_actions.py` and `assaf_os_leads.py`: V2-17 → V2-28;
- `ci.yml` and `tests/unit/test_os_ledger_review_r1.py`: V2-14 → V2-15 → V2-14b → V2-20;
- `os_ledger_migrate.py` and `migrations_assaf_os/README.md`: V2-14 → V2-15 → V2-14b;
- `role_policy.py`, `provision_ledger.py` and `tests/services/test_os_ledger_role_policy.py`:
  V2-15 → V2-14b;
- `app/main.py`: V2-16s → V2-14a;
- `app/services/os_ledger/core.py`: V2-16 → V2-18 (the `build_ledger()` defaults only);
- `ops/gcp/vm/mia` and `ops/gcp/vm/compose.yaml`: V2-14 (merged) → V2-10 → V2-31.

`app/core/config.py` is the one exception. V2-08, V2-15, V2-16s and V2-14a edit disjoint blocks, and
each rebases. No other chunk edits it. `pyproject.toml` is V2-31's alone. `app/prototypes/*` is
read-only except V2-30's one source check.

### 3.2 Frontend (assaf-landingPage)

- **V2-21:**
  - `os-contracts.mjs`: every v2 reader, including the statement and credential readers;
  - `os-client.mjs`: thin transport for every endpoint in §10, §12 and §17;
  - `worker.mjs` + `bff.mjs`: the full §10.5 table, in lockstep;
  - `os-app.mjs`: the dual-reader dispatch, the source/mirror chip, and imports of the feature modules;
  - stubs with frozen exports: `os-boot-v2.mjs`, `os-commit.mjs`, `os-statements.mjs`,
    `os-connections.mjs`;
  - empty `os-{statements,commit,connections,tasks}.css`, linked from `index.html`, plus
    `<div id="undo-bar" hidden>` outside `#main`;
  - `tests/contract-v2.test.mjs` and `tests/fixtures/os-v2/*` + `fixtures.sha256`.

  V2-21's PR freezes the hook surface: the stub export names, the `ctx` object they receive, and the
  `os-tasks.mjs` exports that `os-app.mjs` already imports (`TASK_FILTERS`, `tasksView`,
  `todayInIsrael`; the file exists since V2-12). That surface is reviewed as part of this contract.
- **V2-23:**
  - `owner-os-integration.yml`;
  - `tests/e2e/harness.py`;
  - `tests/e2e/browser.mjs`, refactored to load `tests/e2e/controls/*.mjs`;
  - `performance-browser.mjs`, `cross-contract.test.mjs` and `backend-ref.txt`;
  - the templates.
- **V2-22:** `os-boot-v2.mjs`, `tests/paged-boot.test.mjs` and `controls/paged-boot.mjs`.
- **V2-32:** `os-commit.mjs`, `os-commit.css`, `DECISIONS.md`, `tests/owner-direct.test.mjs` and
  `controls/commit-undo.mjs`.
- **V2-33:** `os-tasks.mjs` (extends the V2-12 file and keeps the frozen exports), `os-tasks.css`,
  `tests/tasks-view.test.mjs` (existing), `tests/tasks-fast-list.test.mjs` and `controls/tasks.mjs`.
- **V2-27:** `os-statements.mjs`, `os-statements.css`, `tests/statements-view.test.mjs` and
  `controls/statements.mjs`.
- **V2-39:** `os-connections.mjs`, `os-connections.css` and `tests/connections.test.mjs`.

**Integrator-owned files** are regenerated on every rebase and never hand-merged: `worker-assets.json`,
the dashboard builder's asset count, and `T2\*-release-input.json`.

Feature chunks never edit `styles.css`, `os-contracts.mjs`, `worker.mjs`, `bff.mjs` or `os-app.mjs`.
A needed change there becomes a V2-21 follow-up PR.

### 3.3 Settings block (V2-16s, added once, all default-off)

| Field (env `MIA_<UPPER>`) | Default | Valid values (checked at use) | Rule |
|---|---|---|---|
| `assaf_os_backend` | `"sheets"` | `sheets`, `postgres` | The factory (§7) |
| `assaf_os_ledger_enabled` | False | bool | The ledger refuses everything while it is off (except `purpose="expire"`, §5.3) |
| `assaf_os_authority_epoch` | 0 | int ≥ 0 | 0 means the ledger refuses every write |
| `assaf_os_freeze` | False | bool | Drain flag (T1) |
| `assaf_os_owner_direct_enabled` | False | bool | Commits, undo and abandon routes |
| `owner_undo_window_seconds` | 30 | 10..60 | FORK-UNDO-WINDOW |
| `assaf_os_statements_enabled` | False | bool | Statement routes |
| `os_statement_pepper` (`repr=False`) | "" | ≥ 32 bytes; its fingerprint must equal the pinned one (§12.2) | Required at use |
| `assaf_os_mirror_enabled` | False | bool | Publisher thread |
| `os_mirror_spreadsheet_id` | "" | exactly one id | |
| `os_mirror_service_account_json` (`repr=False`) | "" | Secret Manager only | |
| `os_mirror_lag_warn_seconds` | 300 | 60..3600 | `mirror.state = lagging` |
| `os_mirror_anti_entropy_seconds` | 900 | 300..3600 | |
| `assaf_os_assistant_enabled` | False | bool | `/v1/assistant/*` |
| `os_assistant_ip_hmac_key` (`repr=False`) | "" | ≥ 32 bytes | Required at use |
| `os_step_up_secret` (`repr=False`) | "" | ≥ 32 bytes | Required at use |

The ledger workspace is the existing `assaf_os_workspace_id`. The runtime DSN field
`owner_os_ledger_database_url` is V2-15's (FORK-LEDGER-DSN), and `app_role_posture` is V2-14a's;
both follow the rule below.

**Misconfiguration never fails the settings load.** Every field above (and V2-14a's
`app_role_posture`) is declared with a plain `str`/`int`/`bool` annotation and no `Literal`, `ge` or
`le`. One shared `mode="before"` validator turns any unparsable or out-of-range value into the
field's default and logs `os_config op=load outcome=coerced reason=config_invalid field=<name>` once.
Every default is the safe, off state: an invalid `assaf_os_backend` becomes `sheets`, which the fence
still refuses while `core` is active (§7). `/health` adds `os_config_coerced: bool`. A route whose
required value is still missing at use returns 503 `ledger_unavailable` or 404
`operation_unavailable`. V2-16s's tests feed a bad value to every field. The existing
`assaf_os_mode: Literal[...]` is out of scope and stays as it is.

### 3.4 CI

V2-15 (as built) runs `tests/db/test_assaf_os_ledger_migration.py
tests/services/test_os_ledger_role_policy.py`. V2-14b widens the `os-ledger-migration-postgres`
pytest line to exactly:

```
tests/db/test_assaf_os_ledger_migration.py tests/services/test_os_ledger_role_policy.py tests/db/test_assaf_os_ledger_addendum.py tests/services/os_ledger tests/api/os_v2 -rs
```

and updates the pin in `tests/unit/test_os_ledger_review_r1.py` in the same commit. Both directories
exist by then: V2-16s creates `tests/api/os_v2` and V2-16f creates `tests/services/os_ledger`, and
both merge before V2-14b. The skip guard stays.
- **PG case generation.** A `"pg"` case is generated only when **both** hold:
  `importlib.util.find_spec("app.services.os_ledger.core")` is not None, **and**
  `MIA_OS_LEDGER_TEST_POSTGRES_URL` is set. It is never skipped. The PG-less `checks` job therefore
  generates none.
- **Sentinel.** `tests/services/os_ledger/test_pg_sentinel.py` (V2-16f) fails when both conditions
  hold and fewer than `MIN_PG_CASES` pg cases were collected. A `pytest_collection_modifyitems` hook in
  the V2-16f conftest counts them, and V2-16f sets the constant to its conformance case count.
- **PG fixture.** It never calls `provision_ledger` (its targets are pinned to 127.0.0.1:56215). It
  creates the three ledger roles and applies `role_policy.ledger_grant_statements()` directly, and it
  creates a demoted test main role so the §5.5 guard passes.
- Fake-only tests run everywhere.
- No other chunk edits `ci.yml`, except V2-20's artifact step.

## 4. V2-14b schema addendum (owner Codex; three new allow-listed files)

All three files run through the V2-14 runner as `assaf_os_owner`: one transaction each, with the
digest journal. They contain no GRANT. V2-14b's grants go into `role_policy.py`, which it extends
after V2-15 merges (§18.2).

**`20261016_assaf_os_ledger_core_v2.sql`.** Before dropping, the test asserts each constraint's name
from `pg_constraint`.
- `os_workspaces`:
  - add `pending_seq BIGINT NOT NULL DEFAULT 0 CHECK (pending_seq >= 0)` (the §10.2 `since` token);
  - add `statement_pepper_fp CHAR(64) NULL CHECK (statement_pepper_fp ~ '^[0-9a-f]{64}$')`, written
    once on the privileged DSN only (§12.2).
- `os_proposals`:
  - the status CHECK becomes `pending, committed, rejected, expired, target_changed, superseded, abandoned`;
  - add `CHECK (status <> 'abandoned' OR kind = 'owner_direct')`;
  - add the index `(workspace_id, status, kind, created_at)`.
- `os_receipts`:
  - the status CHECK becomes `committed, rejected, expired, target_changed, superseded, abandoned`;
  - add `CHECK ((undo_of IS NOT NULL) = (kind = 'owner_undo'))`;
  - add `CHECK (undo_until IS NULL OR (kind = 'owner_direct' AND status = 'committed'))`;
  - add `reason_codes JSONB NOT NULL DEFAULT '[]'` with `CHECK (jsonb_typeof(reason_codes) = 'array'
    AND jsonb_array_length(reason_codes) <= 20)`.
- `os_import_batches`: replace `UNIQUE (workspace_id, source_digest)` with
  `UNIQUE (workspace_id, authority_epoch, source_digest)` (§15).
- `undo_state` stays as V2-14 made it, but is **inert**: it is never UPDATEd (§5.3).

**`20261016_assaf_os_statements.sql`.** Five tables, each with `workspace_id` FK and `created_at`/`updated_at`:
- **`os_statements`**
  - Primary key `(workspace_id, statement_id)`.
  - Columns:
    - `statement_id` `^STM_[0-9a-f]{32}$`;
    - `account_label` VARCHAR(40), `CHECK !~ '[0-9]{8,}'`;
    - `charge_date` DATE, `cycle` CHAR(7), `cycle_from`, `cycle_to`;
    - `status` `active|replaced`;
    - `line_count`, `lines_digest`;
    - `current_proposal_id` NULL, `approved_proposal_id` NULL, `imported_version` NULL.
  - UNIQUE `(workspace_id, account_label, charge_date)`.
- **`os_statement_lines`**
  - Primary key `(workspace_id, statement_id, line_key)`.
  - Columns:
    - `line_key` CHAR(64) hex;
    - `tag` `business|personal`;
    - `state` `active|replaced|voided`;
    - facts: `txn_date`, `merchant_display` ≤80, `merchant_key` ≤160, `amount_minor` BIGINT > 0,
      `kind` `charge|credit`, `currency` CHAR(3), `original_amount_minor`, `original_currency`,
      `installment_n`, `installment_of`;
    - classification: `category` ≤40, `path`, `platform`, `is_subscription`, `rule_id`,
      `assistant_tag`, `owner_override` JSONB;
    - `expense_id` and `matched_expense_id`, both FK to `os_expenses`;
    - `expense_revision_at_import`, `revision`.
  - CHECKs:
    - `tag='personal'` ⇒ every fact, classification and expense column is NULL;
    - `tag='business'` ⇒ the facts are NOT NULL.
  - Partial UNIQUE `(workspace_id, matched_expense_id) WHERE state='active' AND matched_expense_id IS NOT NULL`.
- **`os_statement_batch_lines`**
  - Primary key `(workspace_id, proposal_id, line_key)`, with FK to proposals.
  - Columns: `occurrence_index`, `detail` JSONB NULL (the full line plus suggestions and confidence),
    `purged_at` NULL.
  - `CHECK ((detail IS NULL) = (purged_at IS NOT NULL))`.
- **`os_merchant_rules`**
  - Primary key `(workspace_id, rule_id)`.
  - Columns:
    - `match` `exact|prefix`, `tag`;
    - `merchant_key` NULL, `merchant_key_hmac` CHAR(64) NOT NULL;
    - `category`, `path`, `platform`, `is_subscription`, `label` ≤80;
    - `aliases` JSONB array ≤10, `active`, `revision`, `source_proposal_id`.
  - UNIQUE `(workspace_id, match, merchant_key_hmac)`; `rule_id` `^MR_[0-9a-f]{24}$`.
  - CHECKs:
    - `tag='personal'` ⇒ `merchant_key IS NULL AND match='exact' AND aliases='[]' AND label IS NULL
      AND category IS NULL AND path IS NULL AND platform IS NULL AND is_subscription IS NULL`;
    - `tag='business'` ⇒ `merchant_key IS NOT NULL`.
  - A personal rule's `rule_id` is HMAC-derived (§12.2), never the domain's sha256 id.
- **`os_statement_review_drafts`**
  - Primary key `(workspace_id, proposal_id)`, with FK.
  - Columns: `draft_revision` ≥0, `overrides` JSONB object, `choices` JSONB object.

**`20261016_assaf_os_assistant.sql`.**
- **`os_assistant_credentials`**
  - Primary key `(workspace_id, credential_id)`.
  - Columns:
    - `credential_id` `^AC_[0-9a-f]{32}$`;
    - `owner_id` numeric, `epoch`, `label` ≤40, `kind` `claude|gpt`;
    - `scopes` `CHECK = 'merchant_rules:read own_proposals:read statement:propose'`;
    - `secret_hash` CHAR(64) UNIQUE;
    - `expires_at` `CHECK <= created_at + interval '90 days'`;
    - `revoked_at`, `last_used_at`, `use_count` ≥0.
- **`os_assistant_audit`**
  - Primary key `(workspace_id, audit_id UUID)`.
  - Columns: `credential_id` NULL, `occurred_at`, `operation` ≤32, `reason` ≤64, `ok`, `line_count`,
    `bytes`, `proposal_id`, `client_ip_hmac` CHAR(64).
  - Index `(credential_id, occurred_at)`.
- Also: FK `os_proposals(workspace_id, credential_id)` → credentials.

**Tests.**
- Applies after V2-14 on an empty PG16, and re-running it is a no-op.
- Every CHECK above has a rejecting case, including `reason_codes`, `pending_seq`, the pepper
  fingerprint format and the extended personal-rule CHECK.
- The import unique key accepts the same `source_digest` at two epochs and refuses it twice at one.
- PUBLIC has no access.
- The three existing posture validators still pass, and the extended V2-15 validator passes on the
  extended map and fails on every extra privilege (§18.2).
- A normal `mia-migrate` still creates no `assaf_os` schema.

## 5. V2-16 ledger core API

### 5.1 `app/services/os_ledger/api.py`

These are the exact names, fields and signatures V2-16s lands; the bodies are V2-16s's own. Any
change to a name or a signature is a fork.

```python
LEDGER_API = "os-ledger.v1"
ENTITIES = ("leads", "clients", "works", "payments", "time", "expenses", "tasks")
ID_KEYS: Mapping[str, str]  # literal copy; a parity test pins it to app.prototypes.assaf_os_domain.ID_KEYS
Entity = Literal["leads", "clients", "works", "payments", "time", "expenses", "tasks"]
Domain = Literal["core", "statements"]
AuthorityState = Literal["shadow", "active", "fenced", "restoring"]
Channel = Literal["OS", "MIA", "TELEGRAM_APPROVAL", "ASSISTANT_CLAUDE", "ASSISTANT_GPT", "WORKER"]
Source = Literal["OS", "OS_DIRECT", "MIA", "TELEGRAM_APPROVAL",
                 "ASSISTANT_CLAUDE", "ASSISTANT_GPT", "ASSISTANT_PASTE", "IMPORT"]
ProposalKind = Literal["proposal", "owner_direct", "owner_undo", "statement_batch", "import"]
ProposalStatus = Literal["pending", "committed", "rejected", "expired", "target_changed",
                         "superseded", "abandoned"]
ReceiptStatus = Literal["committed", "rejected", "expired", "target_changed", "superseded", "abandoned"]
Operation = Literal["read", "read_rules", "read_own_batches", "propose", "propose_statement_batch",
                    "decide", "decide_statement_batch", "draft_statement_batch", "commit_direct",
                    "undo", "abandon", "manage_credentials", "import_promote", "authority_admin",
                    "expire_batches"]
RecordDict = dict[str, Any]  # the v1 wire record: schema fields + id key + "revision"

def digest(value: Any) -> str:  # sha256 hex of json.dumps(sort_keys, ensure_ascii=False, (",",":"), allow_nan=False)
def canonical_uuid(value: str, *, version: int | None = None) -> str  # lowercase; else LedgerError("invalid_request_id")
REQUEST_NS = uuid5(NAMESPACE_URL, "https://mia.assafweb.com/ns/assaf-os/request/v1")
def mia_request_uuid(owner_id: str, workspace_id: str, source_id: str) -> str  # uuid5(REQUEST_NS, f"{owner}:{ws}:{src}")
def paste_request_uuid(owner_id: str, client_batch_id: str) -> str            # uuid5(REQUEST_NS, f"paste:{owner}:{client_batch_id}")
def assistant_request_uuid(credential_id: str, client_batch_id: str) -> str    # uuid5(REQUEST_NS, f"{credential_id}:{client_batch_id}")
def import_request_uuid(epoch: int, manifest_digest: str) -> str               # uuid5(REQUEST_NS, f"import:{epoch}:{manifest_digest}")
def since_token(head: "Head") -> str   # f"{observed_version}.{pending_seq}.{mirror_verified_version}"; opaque to clients
def parse_since(token: str) -> tuple[int, int, int]   # ^\d{1,19}\.\d{1,19}\.\d{1,19}$, else LedgerError("invalid_query")

class LedgerError(Exception):
    code: str; status: int; details: Mapping[str, Any]   # details keys are allow-listed per code (5.2)
    write_outcome: Literal["not_started", "unknown"]      # "unknown" only for code "unknown"
    request_id: str | None; decision_id: str | None

@dataclass(frozen=True)
class TrustedOwnerContext:
    numeric_owner: int; workspace: str; authority_epoch: int; channel: Channel
    scopes: frozenset[str]; auth_expires_at: datetime; browser_mutation_verified: bool
    credential_id: str | None; _capability: object = field(repr=False)
    # __post_init__: _capability must be api._CAPABILITY, else LedgerError("invalid_trusted_context", 403)
def _issue(**fields) -> TrustedOwnerContext  # app callers: os_ledger/authorize.py ONLY (AST test); tests: tests/fakes/contexts.py

@dataclass(frozen=True) class RecordRef: entity: Entity; record_id: str
@dataclass(frozen=True) class Mutation:
    entity: Entity; record_id: str; op: Literal["upsert", "tombstone", "restore"]
    before: Mapping[str, Any] | None      # current wire record when built (None = create)
    after: Mapping[str, Any]              # full wire record; after["revision"] == (before or {revision:0})["revision"] + 1,
                                          # except in a kind="import" transaction: after["revision"] is the manifest
                                          # revision, >= 1 on a create and > the current revision on an update (§5.3)
@dataclass(frozen=True) class ProposalDraft:
    kind: ProposalKind; source: Source; request_id: str; request_digest: str
    entity: str            # Entity | "promote_lead" | "statements"
    record_id: str         # target id; statement_id for a batch; first item's id for a bulk
    expected_revision: int; mutations: tuple[Mutation, ...]; dependency_digest: str
    expires_at: datetime; credential_id: str | None = None
@dataclass(frozen=True) class ProposalRow:   # every os_proposals column, ISO datetimes
    workspace_id: str; proposal_id: str; request_id: str; request_digest: str; envelope_digest: str
    owner_id: str; epoch: int; approval_id: str; rejection_id: str; entity: str; record_id: str
    expected_revision: int; mutations: tuple[Mutation, ...]; dependency_digest: str; source: Source
    credential_id: str | None; kind: ProposalKind; status: ProposalStatus
    created_at: datetime; expires_at: datetime
@dataclass(frozen=True) class ReceiptRow:
    workspace_id: str; request_id: str; proposal_id: str; epoch: int; status: ReceiptStatus
    kind: ProposalKind; commit_version: int; records: tuple[Mapping[str, Any], ...]  # {entity, record_id, revision, digest}
    completed_at: datetime; undo_until: datetime | None; undo_of: str | None
    reason_codes: tuple[str, ...]                    # <= 20, e.g. ("credential_revoked",), ("restored_without_detail",)
@dataclass(frozen=True) class UndoView:
    state: Literal["available", "undone", "expired", "none"]; until: datetime | None; undo_request_id: str | None
@dataclass(frozen=True) class ReceiptResult:
    found: bool; receipt: ReceiptRow | None; proposal: ProposalRow | None
    records: tuple[tuple[Entity, RecordDict], ...]   # current records for committed/target_changed
    observed_version: int; undo: UndoView | None
@dataclass(frozen=True) class CreationResult:
    found: bool; proposal: ProposalRow | None; decision: ReceiptRow | None
    past_expiry: bool | None; observed_version: int
@dataclass(frozen=True) class ProposeInput:  # same validation as app.services.assaf_os.ProposeRequest
    request_id: str; entity: str; record_id: str | None; expected_revision: int
    input: Mapping[str, Any]; approval_expires_at: datetime | None = None
@dataclass(frozen=True) class AuthoritySnapshot:
    workspace_id: str; epoch: int; version: int
    states: Mapping[Domain, AuthorityState]; activated_at: Mapping[Domain, datetime | None]
@dataclass(frozen=True) class Head:
    observed_version: int; authority_epoch: int; states: Mapping[Domain, AuthorityState]
    pending_seq: int; mirror_verified_version: int
@dataclass(frozen=True) class Page:
    entity: Entity; records: tuple[RecordDict, ...]; next_cursor: str | None; total: int; observed_version: int
@dataclass(frozen=True) class ProposalPage: items: tuple[ProposalRow, ...]; next_cursor: str | None; observed_version: int

class LedgerTx(Protocol):
    ctx: TrustedOwnerContext; now: datetime; version: int        # DB now() at begin; version at lock
    connection: Any        # PG: this transaction's SQLAlchemy Connection; fake: None. Use ONLY for chunk-owned
                           # tables (§4 statements/assistant), never for the V2-14 core tables.
    scratch: dict[str, Any]  # fake: per-workspace store rolled back with the tx (in-memory chunk tables); PG: {}
    def get(self, entity: Entity, record_id: str, *, include_deleted: bool = False) -> RecordDict | None: ...
    def get_many(self, refs: Sequence[RecordRef], *, include_deleted: bool = False) -> dict[RecordRef, RecordDict]: ...
    def find_expenses(self, *, ids: Sequence[str] = (), source_key_prefix: str | None = None,
                      manual_window: tuple[date, date] | None = None) -> list[RecordDict]: ...
    def dependents(self, entity: Entity, record_id: str) -> list[RecordRef]: ...   # DEPENDENTS edges whose referrer is_live()
    def proposal(self, proposal_id: str) -> ProposalRow | None: ...
    def proposal_by_request(self, request_id: str) -> ProposalRow | None: ...
    def receipt(self, request_id: str) -> ReceiptRow | None: ...
    def receipt_for_proposal(self, proposal_id: str) -> ReceiptRow | None: ...
    def undo_receipt_of(self, request_id: str) -> ReceiptRow | None: ...
    def insert_proposal(self, draft: ProposalDraft) -> ProposalRow: ...            # status pending; bumps pending_seq
    def apply(self, mutations: Sequence[Mutation], *, source: Source, request_id: str) -> tuple[RecordDict, ...]: ...
    def finish(self, proposal: ProposalRow, *, status: ReceiptStatus, receipt_request_id: str,
               applied: Sequence[Mutation] = (), undo_until: datetime | None = None,
               undo_of: str | None = None, reason_codes: Sequence[str] = ()) -> ReceiptRow: ...  # bumps version + pending_seq

class LedgerStore(Protocol):
    def transaction(self, ctx: TrustedOwnerContext, *, domain: Domain, kind: ProposalKind,
                    purpose: Literal["write", "expire"] = "write"
                    ) -> AbstractContextManager[LedgerTx]: ...   # purpose="expire": kind statement_batch, WORKER only (§5.3)
class LedgerCore(Protocol):
    def propose(self, ctx: TrustedOwnerContext, req: ProposeInput) -> ProposalRow: ...
    def decide(self, ctx: TrustedOwnerContext, proposal_id: str, decision_id: str,
               decision: Literal["approve", "reject"]) -> ReceiptResult: ...   # kind "proposal" ONLY; else 404 proposal_not_found
    def receipt(self, ctx: TrustedOwnerContext, request_id: str) -> ReceiptResult: ...   # found=False == "unknown"
    def creation_request(self, ctx: TrustedOwnerContext, request_id: str) -> CreationResult: ...
class LedgerReader(Protocol):                                            # V2-18 implements on PG
    def head(self, ctx: TrustedOwnerContext) -> Head: ...
    def page(self, ctx, entity: Entity, *, after: str | None, limit: int) -> Page: ...
    def deleted(self, ctx, entity: Entity, *, after: str | None, limit: int) -> Page: ...
    def pending(self, ctx, *, after: str | None, limit: int) -> ProposalPage: ...  # kind proposal, status pending, expires_at > read-tx now()
    def labels(self, ctx, refs: Iterable[RecordRef]) -> dict[str, dict[str, str]]: ...  # {"clients.person_id": {id: label}}
    def monthly_summary(self, ctx) -> tuple[dict[str, Any], ...]: ...    # v1 monthly row shape, unchanged
class AuthorityAdmin(Protocol):                                          # privileged DSN, CLI jobs only
    def ensure_workspace(self, workspace_id: str, owner_id: str, epoch: int) -> AuthoritySnapshot: ...  # first call: epoch must be 1,
                                                                         # both domains shadow; an equal repeat is a no-op
    def state(self, workspace_id: str) -> AuthoritySnapshot: ...
    def transition(self, workspace_id: str, domain: Domain, to: AuthorityState, *,
                   expected_epoch: int, actor_owner_id: str) -> AuthoritySnapshot: ...
    def bump_epoch(self, workspace_id: str, *, expected_epoch: int) -> AuthoritySnapshot: ...
    def pin_statement_pepper(self, workspace_id: str, fingerprint: str) -> AuthoritySnapshot: ...  # write-once; equal = no-op;
                                                                         # different = LedgerError("ledger_unavailable"), reason statement_pepper_mismatch

# The one dependency table; FakeLedger and PG both import it (FORK-DELETE, §11.1). Mirrors every V2-14 FK.
DEPENDENTS: tuple[tuple[Entity, str, Entity], ...] = (   # (referrer entity, referrer column, referenced entity)
    ("works", "person_id", "clients"), ("payments", "person_id", "clients"), ("time", "person_id", "clients"),
    ("tasks", "person_id", "clients"), ("payments", "work_id", "works"), ("time", "work_id", "works"),
    ("tasks", "work_id", "works"), ("payments", "original_payment_id", "payments"))
def is_live(entity: Entity, record: RecordDict, *, deleted: bool) -> bool
    # False when deleted (tombstoned) or in its cancellation status: payments kind בוטל, works status בוטלה,
    # time status בוטל; tasks are live only with status in (לביצוע, בתהליך, ממתין)

def core_authority_state(settings) -> AuthorityState | None
    # Context-free read of os_authority(core) on the runtime DSN, for V2-17's Sheets fence ONLY.
    # None when owner_os_ledger_database_url is empty or no os_workspaces row exists (the Sheets call proceeds).
    # A DB error, or the V2-16 implementation module missing while the DSN is set: LedgerError("ledger_unavailable").
    # V2-16s ships it delegating lazily to app.services.os_ledger.authority.core_state (V2-16).
class StateValidator(Protocol):
    def validate(self, tx: LedgerTx, applied: Sequence[Mutation]) -> None: ...   # raises target_changed + details.reason
class SummaryMaintainer(Protocol):
    def on_apply(self, tx: LedgerTx, applied: Sequence[Mutation]) -> None: ...
class Ledger(LedgerStore, LedgerCore, LedgerReader, Protocol): ...
def build_ledger(settings) -> Ledger   # V2-16: os_ledger/core.py; raises LedgerError("ledger_unavailable") when off/misconfigured
```

### 5.2 Error codes

Every refusal is a `LedgerError`, and this table is the complete code list. Each code has exactly one
HTTP status. `write_outcome` is `"not_started"` for every code except `unknown`. `details` carries
only the listed keys; "—" means `details` is absent. The FE v2 readers accept exactly these codes.

| Code | HTTP | Retryable | `details` keys | Raised by / meaning |
|---|---|---|---|---|
| `invalid_query` | 400 | no | — | §10.2 query allow-list, a bad `since` token |
| `owner_auth_required` | 401 | no | — | the auth layer |
| `assistant_auth_required` | 401 | no | — | §17.2, uniform |
| `kill_switch` | 403 | no | — | I8 |
| `scope_denied` | 403 | no | — | §9 |
| `channel_operation_denied` | 403 | no | — | §9 matrix, including a TELEGRAM_APPROVAL propose |
| `owner_approval_required` | 403 | no | — | §9 (MIA decide) |
| `browser_mutation_verification_required` | 403 | no | — | §9 |
| `step_up_required` | 403 | no | — | §17.1 |
| `invalid_trusted_context` | 403 | no | — | §5.1 |
| `operation_unavailable` | 404 | no | — | a disabled feature flag or the sheets backend |
| `proposal_not_found` | 404 | no | — | also any decide/GET on a non-`proposal` kind (§5.3) |
| `commit_not_found` | 404 | no | — | §11.1 |
| `batch_not_found` | 404 | no | — | §12, §17.2 |
| `idempotency_mismatch` | 409 | no | — | I1, §5.3 keyspace rule |
| `decision_request_mismatch` | 409 | no | — | §5.3 |
| `already_decided` | 409 | no | — | §5.3 |
| `authority_changed` | 409 | no | — | the context's epoch, owner or workspace differs from the ledger's |
| `authority_moved` | 409 | no | — | a Sheets write while `core` is active or restoring (§7) |
| `cursor_invalidated` | 409 | no | `observed_version` | §8 |
| `target_changed` | 409 | no | `reason` | propose only; commits return a receipt instead |
| `not_undoable` | 409 | no | — | §11.1 |
| `already_undone` | 409 | no | `undo_request_id` | §11.1 |
| `undo_expired` | 409 | no | — | §11.1 |
| `undo_conflict` | 409 | no | `reason` (`record_changed`\|`dependent_exists`), `changed` (≤100 `{entity, record_id}`) | §11.1 |
| `has_dependents` | 409 | no | `dependents` (≤20 refs) | §11.1 |
| `review_stale` | 409 | no | — | §12.2 |
| `unassigned_lines` | 409 | no | — | §12.2 |
| `merge_choice_required` | 409 | no | — | §12.2 |
| `draft_conflict` | 409 | no | — | §12.2 |
| `batch_conflict` | 409 | no | `conflicting_proposal_id` | §12.2 |
| `lead_already_exists` | 409 | no | — | §13; a definite no-write |
| `import_quarantine_unresolved` | 409 | no | `count` | §15 |
| `manifest_digest_mismatch` | 409 | no | — | §15 |
| `import_orphan_rows` | 409 | no | `counts` (`{entity: int}`) | §15 |
| `validation_error` | 422 | no | `reason` | including the domain codes of `canonical_input`/`validate_transition` |
| `invalid_request_id` | 422 | no | — | §5.1 |
| `invalid_cursor` | 422 | no | — | §8 |
| `invalid_statement_batch` | 422 | no | `issues` (≤25 `{field, code}`) | §12.1 |
| `rate_limited` | 429 | yes | — | §17.2 |
| `ledger_unavailable` | 503 | yes | — | the guard, the role, durability, a missing workspace row, an unpinned or mismatched pepper, or the DB down before the transaction begins (the reason is logged only) |
| `backend_not_postgres` | 503 | no | — | §5.3 |
| `authority_not_active` | 503 | no | — | §5.3, §15 |
| `os_frozen` | 503 | yes | — | I8 |
| `ledger_integrity_failed` | 503 | no | — | a read-time receipt or summary mismatch, or any SQLSTATE class 23 at write time; logged with `reason=integrity_<sqlstate>`, never rendered as success |
| `unknown` | 503 | no | — | the COMMIT outcome is unknown (`write_outcome:"unknown"`) |

V2-31's CLI refusal codes (§16) are exit reasons, not `LedgerError` codes.

V2-16s ships `app/api/os_v2/errors.py`. It renders the V2-05 envelope:

```json
{"api_version":"owner-os.v1","request_id":"<hex>","error":{"code":"…","retryable":false,"write_outcome":"not_started","details":{…}}}
```

`write_outcome` is present only on write routes. `details` is present only when allowed.

### 5.3 Transactions and exactly-once

**`transaction(ctx, domain=, kind=, purpose="write")`.** Admission runs before anything is yielded,
in this order:
1. The guard (§5.5).
2. `ledger_enabled` and the kill switch.
3. `backend=postgres`, for every kind except `import`.
4. (The freeze is **not** checked here, because a decide also opens a transaction. The callers that
   create new intent check it first: `LedgerCore.propose`, owner-direct commit and undo, and the
   statement and assistant submits.)
5. `SELECT … FOR UPDATE` of `os_workspaces`. No row: `ledger_unavailable` (logged
   `reason=workspace_missing`).
6. The epoch check: `ctx.authority_epoch == settings.assaf_os_authority_epoch == os_workspaces.epoch`
   == the domain row's epoch, else `authority_changed`.
7. The state check per kind:
   - `import` needs `core=shadow`;
   - `proposal`, `owner_direct` and `owner_undo` need `core=active`;
   - `statement_batch` needs `core=active` and `statements=active`.

**Expiry admission (`purpose="expire"`).** It exists so that personal detail is purged on time
whatever the switches say (I11). It requires `kind="statement_batch"` and the WORKER channel
(`for_worker("retention")`), and runs only steps 1, 5 and 6. It skips `ledger_enabled`, the kill
switch, the backend and the authority states. Inside it, the only legal calls are `proposal()`,
`finish(status="expired")` on a pending `statement_batch` whose `expires_at < tx.now` (or that has no
batch lines, §12.2), and the batch-line purge through `tx.connection`. Anything else raises
`AssertionError("ledger_invariant:expire_scope")` on both implementations. Conformance runs it under
the kill switch, `backend=sheets`, `statements=fenced` and `ledger_enabled=False`.

**A pending proposal from an older epoch** is never decided. A decide or approve finds it through
`proposal()` and finishes it `expired` with `reason_codes=["epoch_changed"]`; `expire_due` does the
same for batches. The receipt carries the proposal's own epoch.

**Exit.**
- A normal exit commits, with `synchronous_commit=on`.
- An exception rolls back and re-raises a `LedgerError` unchanged. A PG `IntegrityError` (SQLSTATE
  class 23) becomes `ledger_integrity_failed` (non-retryable), never `ledger_unavailable`. Any other
  exception becomes `ledger_unavailable`.
- A failure or disconnect during COMMIT raises `unknown`.
- **Client-reachable constraints are pre-checked.** Every constraint a client input can reach (the
  expense source key, the lead `person_id`, the decision-id keyspace) is checked by the shared core
  logic and mapped to its business code before the INSERT, so neither implementation reaches the
  constraint. The fake's `fake_invariant:*` assertions therefore fire only on caller bugs.
  Conformance has one case per such constraint.

**`apply(mutations, source, request_id)`.**
- For each mutation:
  - **create:** the row must be absent;
  - **update or tombstone:** `current.revision == before.revision` and the row is not tombstoned;
  - **restore:** the row must be tombstoned at `before.revision`.
- Revision rule: `after.revision == before.revision + 1` (create: 1). **Exception:** in a
  `kind="import"` transaction, `after.revision` is the manifest revision, ≥ 1 on a create and greater
  than the current revision on an update. The fake and PG enforce the same rule.
- Any mismatch raises `target_changed`.
- It writes the `inputs` (canonical), the typed columns, `revision=after.revision`, `source`,
  `updated_at=tx.now`, and the `deleted_*` columns.
- Then it calls `StateValidator.validate` and `SummaryMaintainer.on_apply`.
- Revisions never rewind.
- **`apply` is atomic on its own.** PG runs it inside a SAVEPOINT; the fake snapshots before it. If
  it raises, none of its writes remain and the transaction stays usable. That lets a caller record a
  terminal `target_changed` receipt (`insert_proposal` + `finish(target_changed)`) in the same
  transaction instead of losing the replay record.

**`insert_proposal(draft)`** inserts the row as `pending` and bumps `os_workspaces.pending_seq` by 1.

**`finish(...)`.**
1. Sets `proposal.status` (pending → terminal).
2. Bumps `os_workspaces.version` by exactly 1, and `pending_seq` by 1.
3. Inserts the receipt:
   - `commit_version` = the new version;
   - `records` = `{entity, record_id, revision, digest(after)}` for each applied mutation when the
     status is `committed`, else `[]` (kind `import`: always `[]`, §15);
   - `undo_until = tx.now + owner_undo_window_seconds`, only for a committed `owner_direct`;
   - `reason_codes` as passed (≤20).
4. Inserts exactly one event, `{status, kind, effects}`. `effects` carry refs and revisions, never
   personal statement detail.
5. If committed, inserts exactly one outbox row, `{authority_epoch, refs:[{entity, record_id, revision}]}`.
   Refs only: the publisher projects the latest version. Kind `import` writes
   `{authority_epoch, refs:[], full:true}`, and the publisher projects everything.

**Receipts are INSERT-only.** Undo state is **derived**, never stored by UPDATE:
- `none` unless `kind = 'owner_direct' AND status = 'committed'` (so `target_changed`, `abandoned` and
  every other kind are `none`, and a NULL `undo_until` is never compared);
- otherwise `undone` if `undo_receipt_of(request_id)` exists;
- otherwise `expired` if `now > undo_until`;
- otherwise `available`.

**Request digests**, using `digest(...)` over these fields:

| Kind | Digest fields |
|---|---|
| `proposal` | the prototype set: `{owner, workspace, epoch, source, entity, record_id, revision, input(canonical), outer_expiry}` |
| `owner_direct` | `{owner, workspace, epoch, "OS_DIRECT", op, entity, record_id, expected_revision, patch(canonical)` or `items` in the given order`}` |
| `owner_undo` | `{owner, workspace, epoch, "undo", of}` |
| abandon | `{"abandon": request_id}` |
| `statement_batch` | `{StatementBatch.digest, source, credential_id}` |
| `import` | `{"import": manifest_digest, "epoch": epoch}`, with `request_id = import_request_uuid(epoch, manifest_digest)` |

**Fused-kind columns.** Every `os_proposals` column is NOT NULL, and the schema requires
`expires_at > created_at`, so both implementations fill them exactly like this:

| Kind | `entity` | `record_id` | `expected_revision` | `dependency_digest` | `expires_at` |
|---|---|---|---|---|---|
| `owner_direct` | the entity | the target id (a create: the server-minted id; a bulk: the first item's id) | the body's (bulk: the first item's) | `digest([{entity, record_id, revision}])` of the before-images, in mutation order (create: revision 0) | `tx.now + owner_undo_window_seconds` |
| `owner_undo` | the original's | the original's | the original receipt's first record revision | `digest(original_receipt.records)` | `tx.now + owner_undo_window_seconds` |
| abandon (kind `owner_direct`) | `"abandon"` | the abandoned `request_id` | 0 | `digest([])` | `tx.now + owner_undo_window_seconds` |
| `import` | `"import"` | the import `batch_id` | 0 | the manifest digest | `tx.now + owner_undo_window_seconds` |

Mutations: `owner_direct` and `owner_undo` hold their mutations; abandon and `import` hold `[]` (the
import's record of truth is `os_import_rows` plus the signed manifest).

**Decision ids.**
- `proposal` and `statement_batch` have server-minted UUIDv4 `approval_id` and `rejection_id`.
- For `owner_direct`, `owner_undo` and `import`, `approval_id = request_id`, and `rejection_id` is
  `uuid5(REQUEST_NS, request_id + ":reject")`, which can never be used.
- **Keyspace rule.** Before inserting a fused-kind proposal, the id is refused with
  `idempotency_mismatch` when it equals any proposal's `approval_id` or `rejection_id`, or any
  receipt key. Conformance covers each collision on both implementations.

**Receipt keys** (`os_receipts.request_id`), by terminal status:

| Status | Receipt key |
|---|---|
| `committed`, `target_changed` | `approval_id` (= `request_id` for fused kinds) |
| `rejected` (by the owner, or `credential_revoked`) | `rejection_id` |
| `superseded` | `rejection_id` |
| `expired` | the id the caller supplied (a late approve: `approval_id`; a late reject: `rejection_id`); from `expire_due` or an epoch change: `rejection_id` |
| `abandoned` | `approval_id` (= `request_id`) |

**Decide is for `kind="proposal"` only.** `LedgerCore.decide` and `GET /proposals/{id}` refuse any
proposal whose kind is not `proposal` with 404 `proposal_not_found` and write nothing. Statement
batches are decided only through `StatementBatchService.approve/reject` (§12.2). Conformance:
decide on a `statement_batch`, `owner_direct` or `import` id returns 404 with zero new rows.

**Replay table.**
- Same `request_id` with an equal digest returns the stored proposal or receipt.
- Same `request_id` with a different digest returns `idempotency_mismatch`.
- **Exception:** an existing proposal with status `abandoned` wins over a digest mismatch, so a late
  original returns the abandoned receipt.
- `decide` with a decision id that doesn't match the operation's id returns
  `decision_request_mismatch`.
- A second decision returns `already_decided`.

**Telegram.** ApprovalRow and the ledger commit are two transactions. The decision UUID is stored on
the ApprovalRow first. Recovery is `receipt(decision_id)`. A new decision id is never minted.

### 5.4 Authority state machine and epoch

**Workspace creation.** `ensure_workspace` creates the `os_workspaces` row at epoch 1 and both
`os_authority` rows at `shadow`. Its only caller is `mia os-authority init --approved-sha` (V2-31),
step 4 of FORK-PROD-ORDER, which runs before any import; it also pins the pepper fingerprint when the
pepper is configured (§12.2). Until the row exists, every ledger transaction and read returns
`ledger_unavailable` (logged `reason=workspace_missing`), and `core_authority_state` returns None.

**One epoch per workspace.** `os_workspaces.epoch` and both `os_authority` rows always hold the same
value. `bump_epoch(expected_epoch=E)` sets all three to E+1 in one transaction. If the current epoch is
already E+1, it returns without change, so the bump is idempotent. **Epoch bumps happen only** on
`fenced→shadow` and `restoring→active`. `activate` (`shadow→active`) never bumps.

**Legal transitions**, per domain; anything else is refused:
- `shadow→active` and `shadow→fenced`;
- `active→fenced`;
- `fenced→shadow`, only together with an epoch bump and only while no non-import receipt exists at
  the current epoch. This is the re-attempt path after a rollback before the first write: the next
  attempt re-imports at the new epoch. The import request id and the batch unique key include the
  epoch, kind `import` writes manifest revisions, and promote refuses orphans (§15);
- `fenced→restoring`, then `restoring→active` only together with an epoch bump.

There is no `fenced→active`. The Sheet is unfenced (LIVE) after a rollback, so re-activating without
a fresh import would serve stale data. Once a non-import receipt exists, the only way back to
`active` is through `restoring`, which means rolling forward.

**Reads** require `active` or `fenced`, or `shadow` for the WORKER channel.

**Privileges.** `AuthorityAdmin` runs only on the privileged DSN, inside the `mia os-authority` and
`mia os-ledger` jobs. The runtime role cannot UPDATE `os_authority`, `os_workspaces.epoch` or
`schema_migrations`.

### 5.5 Guard (`guard.py`)

The runtime never holds the privileged migration DSN, so the guard does **not** call V2-14's
`validate_main_role(main_engine, migration_engine)`. It reuses V2-15's runtime hook instead.

**Per engine** (on the first connection, again every 10 minutes, and after any reconnect; the result
is cached between runs):
- `role_policy.validate_ledger_role(db, main_engine, migration_role=None)`. It checks: the same server
  and database for both DSNs; `session_user = current_user = 'assaf_os_ledger'`; the full ledger grant
  map; and the main DSN's posture through V2-14's `_validate_main_role_catalog`. V2-15 r2 makes
  `migration_role=None` derive the migration role as the single ADMIN-only member of
  `assaf_os_owner`; zero or several such members give `ledger_role_denied`;
- `SHOW fsync` returns `on`.

**Per transaction** (in the same round trip as the workspace lock):
- `SET LOCAL synchronous_commit = on`;
- `SELECT current_user, session_user` equals `('assaf_os_ledger', 'assaf_os_ledger')`.

Any failure gives `ledger_unavailable`, logged as `os_ledger op=guard outcome=refused reason=<code>`.

## 6. FakeLedger and the conformance suite (V2-16f)

**Files.** `tests/fakes/os_ledger.py` holds `class FakeLedger` (`Ledger` + `AuthorityAdmin`).
`tests/fakes/contexts.py` holds `make_ctx(channel="OS", *, owner=…, epoch=1, scopes=…,
browser_mutation_verified=True, credential_id=None, expires_in=3600)`, which calls `api._issue`.

**Constructor.**
```python
FakeLedger(*, clock: Callable[[], datetime], uuid4: Callable[[], str] = default,
           settings_epoch: Callable[[], int], backend: Callable[[], str] = lambda: "postgres",
           kill_switch=lambda: False, freeze=lambda: False, undo_window_s=lambda: 30,
           enabled=lambda: True,
           validator: StateValidator | None = None, summaries: SummaryMaintainer | None = None)
```

**Behaviour, exactly as §5:**
- **Tables.** In-memory dicts that mirror the SQL tables of §4 and V2-14. No workspace exists until
  `ensure_workspace` is called.
- **The workspace lock** is one **non-reentrant** `threading.Lock` per workspace. A nested
  `transaction()` on the same thread raises `AssertionError("fake_invariant:nested_transaction")`,
  because on PG the second connection would wait forever on the first's `FOR UPDATE`. A service that
  needs ledger effects inside an open transaction takes the `LedgerTx` as a parameter (e.g.
  `revoke_pending_for_credential(tx, …)`, §12.2).
- **`transaction()`** deep-copies the workspace's tables on entry, restores them on any exception,
  and discards the copy on success.
- **Admission order (including `enabled` and `purpose="expire"`), codes, replay rules, version and
  `pending_seq` bumps, the one-event and one-outbox rule, the keyspace rule, and the derived undo
  state** are all exactly as §5.3 and §5.4.
- **`dependents()`** walks `api.DEPENDENTS` with `api.is_live`, the same table PG uses.
- **Validation** uses the same `canonical_input`, `validate_transition` and `validate_state` from
  `app.prototypes.assaf_os_domain`. The default validator is the full one; the default summaries are
  a full `build_summaries` recompute.
- **Readers.**
  - Same orders and limits as §8.
  - Cursors are opaque `fake:` + base64url, so no caller can parse them.
  - `cursor_invalidated` when the version moved.
- **SQL invariants.** The fake enforces every unique key and every CHECK of §4 and V2-14. A violation
  raises `AssertionError("fake_invariant:<name>")`, so a caller bug fails loudly and is never mapped
  to a business code. Client-reachable constraints never get this far: the shared core logic
  pre-checks them (§5.3), and PG maps any residual SQLSTATE class 23 to `ledger_integrity_failed`.

- **`apply`** snapshots before it runs and restores on raise (the SAVEPOINT rule in §5.3).
- **`tx.connection`** is `None`. **`tx.scratch`** is a per-workspace dict inside the copy-on-write
  state, so a chunk's in-memory tables (V2-26, V2-37) roll back with the transaction.

**Test hooks** (not part of `Ledger`):
- `inject(point, exc)`, one-shot, where `point` is one of `after_apply`, `after_receipt`,
  `before_commit` or `commit_unknown`. `commit_unknown` commits and then raises `unknown`.
- `pause(point) -> threading.Event`, for races.
- `rows(table)`, `version(ws)` and `advance(seconds)`.

**Conformance suite** (`tests/services/os_ledger/conformance/`), parametrized over `["fake"]`. The case
`"pg"` is generated only under the two conditions of §3.4, and the sentinel guards its count. Its
fixture applies the V2-14/14b migrations, creates the roles and applies
`role_policy.ledger_grant_statements()` directly (never `provision_ledger`), creates a demoted test
main role, and connects as the runtime role. V2-16 must pass the suite unchanged.

Required cases:
- Admission:
  - each kind in each authority state;
  - an epoch mismatch at every layer;
  - the kill switch, the freeze, `enabled=False` and `backend=sheets`;
  - no workspace row gives `ledger_unavailable`;
  - `purpose="expire"` succeeds under the kill switch, `backend=sheets`, `statements=fenced` and
    `enabled=False`, and refuses any call outside its scope;
  - a nested transaction on one thread fails loudly (fake) and is never attempted by a service.
- Proposals and decisions:
  - propose idempotency (equal and different digest);
  - decide approve, reject and expire;
  - decide on a `statement_batch`, `owner_direct` or `import` id gives 404 and zero new rows;
  - a dependency change gives `target_changed`;
  - a two-thread approve race gives exactly one receipt, one event and one outbox row;
  - every terminal status bumps the version by exactly 1;
  - a propose changes the `since` token but not `version`;
  - every receipt is stored under the key of the §5.3 receipt-key table;
  - each keyspace collision of §5.3 gives `idempotency_mismatch` and writes nothing.
- Fault injection: an injected failure at each point leaves zero partial rows, and `commit_unknown`
  followed by `receipt()` gives `found`.
- The full owner-direct state machine from §11.1: commit, undo, abandon, bulk, and every
  `has_dependents` and `undo_*` path, using the pinned `DEPENDENTS`; `undo.state` is `none` for
  `target_changed` and `abandoned` receipts; the fused-kind columns match §5.3.
- Import: manifest revisions on create and update; the same manifest at a new epoch is a new request.
- Reads:
  - cursor invalidation;
  - reads never change the version;
  - `labels` returns only the requested refs;
  - `pending` excludes proposals past `expires_at`.
- Each client-reachable constraint returns its business code, never `fake_invariant` or
  `ledger_integrity_failed`.
- UUIDv5 MIA-id replay is idempotent.
- Authority transitions and the idempotent epoch bump.

## 7. V2-17 OsBackend, factory, two-sided fence

```python
class OsBackend(Protocol):
    kind: Literal["sheets", "postgres"]
    def schema(self) -> dict: ...
    def snapshot(self, query: SnapshotQuery | None = None) -> dict: ...   # sheets: query is ignored (byte-identical v1)
    def propose(self, request: ProposeRequest) -> dict: ...
    def proposal(self, proposal_id: str) -> dict: ...
    def decide(self, operation: Literal["approve","reject"], proposal_id: str, request: DecisionRequest) -> dict: ...
    def receipt(self, request_id: str) -> dict: ...
    def proposal_request(self, request_id: str) -> dict: ...  # sheets: AssafOsError("operation_unavailable", 404)
@dataclass(frozen=True) class SnapshotQuery: entity: Entity = "tasks"; after: str | None = None; limit: int = 50; since: str | None = None
def get_os_backend(settings, owner_id: str, *, actor: ActorSource, principal=None) -> OsBackend
```

**Factory.**
- `sheets` returns `SheetsOsBackend`, which wraps `AssafOsPort` with unchanged behaviour.
- `postgres` returns `PostgresOsBackend`, imported lazily from `app.services.os_backend_pg` (V2-20).
- The postgres path fails closed with 503 `ledger_unavailable` when the ledger is off, the epoch is 0,
  or the module is missing.

**Every constructor site** calls the factory. That is 8 calls in 4 files: `api/assaf_os.py:21`,
`tools/owner/assaf_os.py:36`, `assaf_os_leads.py:567,885` and `owner_actions.py:457,466,499,1043`. A
grep test proves that nothing else constructs `AssafOsPort`.

**The fence has two sides.**
1. **The Sheets side.**
   - `AssafOsPort.__init__` raises 409 `authority_moved` under `backend=postgres` (one status per
     code, §5.2; the factory never reaches it).
   - Under `assaf_os_freeze`, `_call` refuses propose with 503 `os_frozen` before dispatch (I8).
     Approve and reject still pass.
   - For propose, approve and reject, `_call` calls **only** `api.core_authority_state(settings)`
     before dispatch. It needs no `TrustedOwnerContext` and no privileged DSN:
     - `active` or `restoring` refuses with 409 `authority_moved` and `write_outcome:"not_started"`;
     - `shadow` or `fenced` proceeds;
     - `None` (the DSN is unset, or `mia os-authority init` has not run yet) proceeds;
     - a `LedgerError` (DB unreachable while the DSN is set) refuses with 503 `ledger_unavailable`
       and `write_outcome:"not_started"`.
2. **The ledger side.** Business-write transactions refuse with `backend_not_postgres` (§5.3).

**v2 route mapping.** `_run` maps `LedgerError` to the §5.2 envelope. Under `postgres`, the snapshot
route parses the §10.2 query allow-list. Under `sheets`, it ignores the query, exactly as today.

## 8. V2-18 read model

**Isolation.** Reads run REPEATABLE READ READ ONLY and never write. This is asserted on PG through
`pg_stat_xact_*` and on the fake through the version.

**Pages.**
- `limit` is 1..200, default 50. `labels` covers only the refs present on the page.
- Sort keys:
  - **tasks:** open statuses first (`לביצוע, בתהליך, ממתין`, then `בוצע`, then `בוטל`), then
    `due_date ASC NULLS LAST`, then `record_id`;
  - **every other entity:** `business_date DESC NULLS LAST, record_id ASC`.
- Tombstoned rows are excluded from pages. `deleted()` lists them, ordered by `deleted_at DESC`.
- `total` is the count of non-deleted rows.
- `pending()` and `pending_count` list only `kind='proposal' AND status='pending' AND expires_at >`
  the read transaction's `now()`. Reads never write, so an overdue proposal stays `pending` in the
  table until a decide or `expire_due` finishes it (§5.3).
- `head()` returns `observed_version`, `pending_seq` and `os_mirror_state.verified_version` from one
  snapshot, for the `since` token (§10.2).

**Cursors** use `CursorCodec` (`owner_os_reads.py:109`), signed over `{v: observed_version, e: entity,
k: last sort key}`.
- A forged or malformed cursor gives `invalid_cursor`.
- A version change gives `cursor_invalidated`.

**Summaries.**
- **The authoritative store is `os_summary_monthly`, from V2-16 on.** V2-16's default full recompute
  writes every month row inside the same transaction. The runtime has no DELETE, so a month that loses
  all its data is rewritten as an empty summary, never deleted. `os_workspaces.summaries` holds only
  `{coverage, calculated_version}`. `monthly_summary` and the mirror (§14) read only
  `os_summary_monthly`.
- `os_summary_cycle` is **reserved and unused** in this contract: no chunk writes it, V2-26 derives
  cycle totals from `os_statement_lines`, and the verifier asserts that it is empty.
- `IncrementalSummaries` maintains `os_summary_monthly` (month × currency) inside every apply.
- `ScopedStateValidator` checks only the touched records' references, the touched client's
  payment/refund graph, and the expense source key (through the unique index).
- Both replace the full defaults in `build_ledger()` (`core.py`, handoff V2-16 → V2-18) only after a
  property test proves equivalence over random commit/undo/decide sequences:
  - scoped accept/reject equals full accept/reject;
  - incremental summaries equal a full recompute.
- The `os_verifier` job (registered by V2-31) recomputes in full and flags drift with
  `reason=summary_drift`.

**Budget.** A 20k-record benchmark through the real FastAPI and auth stack, in a container with
`--cpus=0.5 --memory=1g` and a concurrent `pg_dump`. Page p95 and write p95 are recorded in the PR.
If they fail, keep an explicit raised cap and report a fork.

## 9. V2-19 auth binding

**Constructors.** `authorize.py` is the only app module that calls `api._issue`.

| Function | Channel | Admitted when |
|---|---|---|
| `from_owner_read(principal, settings)` | OS | `browser_mutation_verified=False` |
| `from_owner_mutation(principal, settings)` | OS | The principal carries `owner_sessions._MUTATION_PROOF`, set only by `owner_mutation_scope` after Origin and CSRF pass |
| `from_telegram_decision(approval, settings)` | TELEGRAM_APPROVAL | Only after the ApprovalRow compare-and-set |
| `from_mia_tool(principal, settings)` | MIA | — |
| `from_assistant(credential, settings)` | ASSISTANT_CLAUDE or ASSISTANT_GPT | — |
| `for_worker(job, settings)` | WORKER | `job ∈ {import, mirror, verifier, authority, retention}`; the owner is `min(telegram_owner_user_id_set())` and must be the only id |

Every constructor re-checks the numeric allow-list and sets `auth_expires_at` from V2-08's
`OwnerSessionPrincipal.expires_at` (or the credential expiry).

**`require(ctx, op)`** enforces the matrix below. A forbidden cell raises the listed code.

| op | OS | MIA | TELEGRAM_APPROVAL | ASSISTANT_* | WORKER |
|---|---|---|---|---|---|
| read | `assaf-os:read` | bounded (16k chars) | `channel_operation_denied` | `channel_operation_denied` | verifier/mirror |
| read_rules | read | denied | denied | business rules only | denied |
| read_own_batches | read (all) | denied | denied | own `credential_id` only | denied |
| propose | `assaf-os:propose` + mutation | yes | `channel_operation_denied` | denied | denied |
| propose_statement_batch | propose + mutation (`ASSISTANT_PASTE`) | denied | denied | yes | denied |
| decide | `assaf-os:approve` + mutation | `owner_approval_required` | yes | denied | denied |
| decide_statement_batch / draft_statement_batch | approve + mutation | denied | denied | denied | denied |
| commit_direct / undo / abandon | `assaf-os:direct` + mutation | denied | denied | denied | denied |
| manage_credentials | approve + mutation (+ step-up to issue) | denied | denied | denied | denied |
| import_promote / authority_admin | denied | denied | denied | denied | import / authority |
| expire_batches | denied | denied | denied | denied | retention |

**Scope.**
- `assaf-os:direct` joins `KNOWN_SCOPES`, `ASSAF_OS_WRITE_SCOPES` and the `owner_mutation_scope`
  allow-list.
- Google login strips only `ACTION_SCOPES`, so the scope survives login.
- Granting it is the configured owner scope set, a wave-6 config step.
- `require_owner_session` refuses any `Authorization` header with 401.

**AST test** (`tests/unit/test_trusted_context_ast.py`). It resolves symbols; it never matches
substrings (`_issue(` already appears in six unrelated app modules). Outside the allow-list
(`app/services/os_ledger/{authorize,owner_direct,api}.py`, `app/api/os_v2/commits.py` and `tests/`)
it flags:
- any import of `_issue`, `_CAPABILITY` or `TrustedOwnerContext` from `app.services.os_ledger.api`;
- any attribute access `api._issue` or `api._CAPABILITY` on that module (however it was imported);
- any import from, or call into, `app.services.os_ledger.owner_direct`.

Route enumeration covers every router, Mia tool, Telegram callback and bearer route.

**V2-19 also serves** `GET /pending` and `GET /proposal-requests/{uuid}` in `app/api/os_v2/reads.py`,
under `backend=postgres` only. Under sheets, `/proposal-requests` stays absent, exactly as today. The
generic `POST /proposals/{id}/approve|reject` and `GET /proposals/{id}` reach only `kind='proposal'`
(§5.3), so a batch's `approval_request_id` can never be used on them.

## 10. Wire contract v2 (V2-20 backend, V2-21 readers and routes, V2-22 boot)

### 10.1 Binding

Every v2 body carries these keys:

| Key | Value |
|---|---|
| `api_version` | `"assaf-os.v2"` |
| `source` | `"postgresql"` |
| `workspace_id` | id |
| `authority_epoch` | int ≥1 |
| `observed_version` | int ≥0, on every read body except the schema |

Mode and workbook keys are absent. `sameBinding` (v2) compares `workspace_id` and `authority_epoch`.
v1 bodies are unchanged and stay on the v1 readers.

### 10.2 Reads (prefix `/v1/owner/os/unified`)

**`GET /schema`**
- Body: the binding (minus `observed_version`) + `schema_version:"assaf-os-unified.v1"`,
  `money_unit:"minor"`, `tabs`, `enums`.
- The tab, field and enum rules are identical to v1, generated from the frozen
  `assaf_os_local_schema.json`.

**`GET ""`**
- The query allow-list:
  - `entity` (default `tasks`);
  - `after` (≤512 chars);
  - `limit` (1..200, default 50);
  - `since` (an opaque token, ≤64 chars, from a previous body).
- Any other key, a duplicate, a malformed value, or `after` together with `since` gives 400
  `invalid_query`.
- **The `since` token** is `api.since_token(head)` = `"<version>.<pending_seq>.<mirror_verified_version>"`.
  The version moves only in `finish()`, but `pending_seq` also moves on every `insert_proposal`
  (propose from MIA, Telegram or OS, and batch submit), and the third part moves when the mirror
  verifies. Clients never parse it.
- When all three parts of `since` equal the head, the body is exactly the binding +
  `unchanged:true` + `since` + `mirror` (below), under 400 bytes. The mirror chip (I2) therefore keeps
  updating while the data is unchanged.
- Otherwise the page body has exactly these keys:
  - the binding;
  - `observed_at`, `unchanged:false`, `since`, `entity`, `records`, `next_cursor`, `total`,
    `coverage:"complete"`;
  - `summary`: `{coverage, monthly:[v1 monthly rows]}`;
  - `pending_count`;
  - `labels`: `{"clients.person_id":{id:label}, "works.work_id":{…}}`, where a label is
    `name || title || category`, at most 120 chars;
  - `mirror`: `{state: verified|lagging|unknown|conflict|disabled, last_verified_at: iso|null,
    verified_version: int, lag_s: int|null}`.

**`GET /pending?after&limit`**
- Body: the binding + `items:[proposal_v2]` + `next_cursor`.
- `proposal_v2` has exactly these keys:
  - the binding without `observed_version`;
  - `status`, `proposal_id`, `request_id`, `approval_request_id`, `rejection_request_id`;
  - `approval_guard_version:1`, `actor_source`, `kind`, `expires_at`;
  - `entity`, `record_id`, `expected_revision`, `input`, `before`, `after`.
- `actor_source` is one of `OS, OS_DIRECT, MIA, TELEGRAM_APPROVAL, ASSISTANT_CLAUDE, ASSISTANT_GPT,
  ASSISTANT_PASTE, IMPORT`.
- Pending items are `kind:"proposal"` only, and only those not past `expires_at` (§8). Batches are
  listed under `/statements`.

**`GET /proposals/{id}`, `POST /proposals`**
- They return `proposal_v2` (pending).
- `POST /proposals/{id}/approve|reject` returns `receipt_v2`.
- The bodies are the v1 bodies.
- Any id whose kind is not `proposal` gives 404 `proposal_not_found` (§5.3).

**`GET /receipts/{uuid}` → `receipt_v2`**, with exactly these keys:
- the binding;
- `status`: `committed|rejected|expired|target_changed|superseded|abandoned|unknown`;
- `request_id`, `proposal_id`, `kind`, `actor_source`, `commit_version`;
- `committed_at`: always present as a key; non-null only when committed;
- `server_now`: the server's clock when the body was built (ISO), always present, so the client never
  reads its own wall clock (§11.2);
- `records`: `[{entity, record}]`, the current records; non-empty only for `committed` and
  `target_changed`;
- `undo`: `{state: available|undone|expired|none, until, undo_request_id}`; non-null only for kind
  `owner_direct`, and its `state` is `none` unless the status is `committed` (§5.3);
- `undo_of`;
- `reason_codes` (≤20), from `os_receipts.reason_codes`.

When the status is `unknown`, every nullable field is null, `records` is `[]` and `reason_codes` is
`["receipt_not_found"]`.

**`GET /proposal-requests/{uuid}`**
- Body: the binding + `request_id`, `status: found|unknown`, `proposal: proposal_v2(any status)|null`,
  `receipt: receipt_v2|null`, `reason_codes`.
- `proposal_v2.status` here may be any of `pending, committed, rejected, expired, target_changed,
  superseded, abandoned`.
- **The v2 cross-check is derived from the §5.3 receipt-key table**, not copied from v1:
  `receipt.status == proposal.status`, `receipt.proposal_id == proposal.proposal_id`,
  `receipt.actor_source == proposal.actor_source`, and `receipt.request_id` equals:
  - `approval_request_id` for `committed`, `target_changed` and `abandoned`;
  - `rejection_request_id` for `rejected` and `superseded`;
  - either of the two for `expired`.
  There is no revision comparison (`receipt_v2` has no `revision`). With no receipt, only `pending` is
  accepted: in v2 every terminal status, `expired` included, is written by `finish()` together with
  its receipt.

**`GET /deleted?entity&after&limit`**
- Body: the binding + `entity`, `records`, `next_cursor`.

### 10.3 Mia consumers (V2-20)

- `assaf_os_follow_up.py` accepts v1 or v2 and rejects mixed shapes.
- Nothing PG-sourced is ever labelled `google_sheets`.
- The model view is at most 16,000 chars. `vendor` and merchant text are wrapped as untrusted data.
- `proposal_cards.py` belongs to V2-28 (§13).

### 10.4 Golden fixtures

- `scripts/export_os_contract_fixtures.py` is a registry (§3.1). Each chunk that defines shapes owns
  `tests/contract/shapes/<chunk>.py` and `tests/contract/golden/<chunk>/*`: V2-20 for §10, V2-24 for
  §11, V2-26 for §12.4 and V2-37 for §17. So a later chunk adds its shapes without touching V2-20's
  files. The `fixtures.sha256` manifest is integrator-owned and regenerated on every rebase.
- CI fails when the export differs from the committed golden copy.
- The frontend vendors the files by hash and records the backend SHA in `backend-ref.txt`.
- V2-21 starts from hand-written fixtures built from this section, then swaps in the exported ones.
  Any byte difference is a contract bug in one of the two, and it is fixed before V2-21 releases.

### 10.5 Worker and BFF route table (V2-21; identical in both; Worker deadline 35 s; upstream cap 262,144 B)

| FE path | Method | Body cap | Reader | osWrite |
|---|---|---|---|---|
| `/api/os` (query per §10.2, re-validated) | GET | — | `readOsSnapshotAny` (v1, v2 page, v2 unchanged) | |
| `/api/os/schema` | GET | — | `readOsSchemaAny` | |
| `/api/os/pending` | GET | — | `readPendingList` | |
| `/api/os/proposals` | POST | 16 KiB | `readProposalAny` | yes |
| `/api/os/proposals/{id}` | GET | — | `readProposalAny` | |
| `/api/os/proposals/{id}/(approve\|reject)` | POST | 16 KiB | `readReceiptAny` | yes |
| `/api/os/receipts/{uuid}` | GET | — | `readReceiptAny` | |
| `/api/os/proposal-requests/{uuid}` | GET | — | `readProposalRequestAny` | |
| `/api/os/commits` | POST | 16 KiB | `readCommitReceipt` | yes |
| `/api/os/commits/{uuid}/(undo\|abandon)` | POST | 16 KiB | `readCommitReceipt` | yes |
| `/api/os/deleted` | GET | — | `readDeletedPage` | |
| `/api/os/statements`, `/api/os/statements/{STM_id}` | GET | — | `readStatementList`, `readStatementView` | |
| `/api/os/statement-batches` | POST | 128 KiB | `readBatchCreated` | yes |
| `/api/os/statement-batches/{id}` and `…/lines` | GET | — | `readBatchReview`, `readBatchLines` | |
| `/api/os/statement-batches/{id}/draft` | POST | 16 KiB | `readDraftSaved` | yes |
| `/api/os/statement-batches/{id}/(approve\|reject)` | POST | 16 KiB | `readReceiptAny` | yes |
| `/api/os/merchant-rules` | GET | — | `readMerchantRules` | |
| `/api/os/assistant-credentials` | GET / POST | 16 KiB | `readCredentialList` / `readCredentialIssued` | POST |
| `/api/os/assistant-credentials/{id}/revoke`, `/revoke-all` | POST | 16 KiB | `readCredentialList` | yes |
| `/api/auth/step-up` | POST | 8 KiB | `readStepUp` | |

**osWrite semantics** (extending V2-05):
- `dispatched` is set immediately before the upstream fetch.
- `write_outcome:"not_started"` is emitted iff the request was not dispatched, or the upstream said
  `not_started`.
- `details` passes through only for the §5.2 codes that allow it.

**Dual reader.**
- Every `*Any` reader dispatches on `api_version` and fails anything else.
- Unknown keys still fail.
- New values the v2 readers accept, and nothing else:
  - receipt `status`: `superseded` and `abandoned`;
  - `proposal_v2.status` (inside `/proposal-requests` only): `superseded` and `abandoned`;
  - `undo.state`: `available`, `undone`, `expired`, `none`;
  - the keys `since` (snapshot bodies) and `server_now` (`receipt_v2`);
  - error codes: exactly the §5.2 table.
  (The roadmap's "statuses undone/undo_expired/undo_conflict" are an undo state and error codes
  here, not receipt statuses.)
- v1 tests are unchanged.

### 10.6 Paged boot (V2-22; v2 only, and v1 unchanged)

1. Boot runs: session, then `loadSchema()`, then `GET /api/os?entity=<route entity, default tasks>&limit=50`.
   That is at most 2 data calls. Under v2, the V2-42 cached read is skipped.
2. Other tabs load lazily on first visit. "Show more" fetches with `after`. `cursor_invalidated`
   refetches page 1.
3. The 3-min poll, and the poll on return, sends `since=<the last body's since token>`. `unchanged`
   re-renders only the mirror chip from its `mirror`. A change replaces page 1 of the current entity,
   the summary and `pending_count`.
4. Any read whose `observed_version` is below the highest `commit_version` received this session is
   ignored.
5. `canWrite` requires all of: `coverage` complete, the snapshot not cached, a session, and an epoch
   equal to the schema's.

## 11. Owner-direct commits, undo, fast tasks

### 11.1 Backend (V2-24)

Routes go in `app/api/os_v2/commits.py`. The whole route is guarded by `assaf_os_owner_direct_enabled`
and `backend=postgres`; when either is off it returns 404.

**`POST /commits`** takes one of two bodies.
- Single:
  ```json
  {"request_id":"<uuid v4>","op":"create|update|complete|reopen|delete|restore","entity":"<E>","record_id":"<id>|null","expected_revision":0,"patch":{}}
  ```
- Bulk:
  ```json
  {"request_id":"<uuid v4>","op":"bulk_complete|bulk_delete_done","entity":"tasks","items":[{"record_id":"<id>","expected_revision":1}]}
  ```
  `items` holds 1..100 unique pairs.

Body rules:
- **create:** `expected_revision` 0 and `record_id` null.
- **update:** the patch is merged over the current canonical input and re-canonicalized.
- **complete / reopen:** tasks only, setting status `בוצע` / `לביצוע`.
- **restore:** tombstoned tasks only.
- **delete**, per FORK-DELETE:

  | Entity | Delete sets |
  |---|---|
  | tasks | a tombstone |
  | payments | `kind` → `בוטל` |
  | expenses | `payment_status` → `בוטל` |
  | works | `status` → `בוטלה` |
  | time | `status` → `בוטל` |
  | clients | `status` → `לא פעיל` |
  | leads | `status` → `לא רלוונטי` |

  `has_dependents` is raised when `tx.dependents(entity, record_id)` is non-empty. It walks the one
  pinned table, `api.DEPENDENTS`, which mirrors every V2-14 FK:

  | Referenced | Live referrers that block it |
  |---|---|
  | clients | works, payments, time and tasks by `person_id` |
  | works | payments, time and tasks by `work_id` |
  | payments | refund payments by `original_payment_id` |
  | leads, expenses, tasks | none |

  "Live" is `api.is_live`: not tombstoned and not in the entity's cancellation status; a task is live
  only while its status is `לביצוע`, `בתהליך` or `ממתין`, so a done task never blocks.
- **bulk_delete_done:** every item must be `בוצע`. Otherwise the whole bulk becomes `target_changed`.

**Transaction order** (kind `owner_direct`):
1. `proposal_by_request`. If one exists:
   - `abandoned` returns that receipt;
   - an equal digest returns the stored receipt;
   - a different digest, or an existing proposal of another kind, returns `idempotency_mismatch`.
   If none exists, apply the §5.3 keyspace rule (the id must not be any `approval_id`,
   `rejection_id` or receipt key).
2. Build the mutations from the current rows. The proposal columns follow the §5.3 fused-kind table.
   - A revision mismatch, or a `validate` failure, inserts the proposal and calls
     `finish(target_changed)` with no business change. The response is a 200 `receipt_v2` with the
     fresh records.
3. `apply`.
4. `finish(committed)`.
5. Respond with `receipt_v2`: `status:"committed"`, the changed records, `commit_version`,
   `server_now`, `undo:{state:"available", until}`.

**`POST /commits/{request_id}/undo`** with body `{"undo_request_id":"<uuid v4>"}`. Same admission. Kind
`owner_undo`.
1. **Replay.** If a proposal exists for `undo_request_id`, apply the replay table (another kind under
   that id gives `idempotency_mismatch`). Otherwise apply the §5.3 keyspace rule.
2. **Load the original.** A missing original gives `commit_not_found`. One that is not a committed
   `owner_direct` at the same owner and epoch gives `not_undoable`.
3. **Prior undo.**
   - `undo_receipt_of` with the same `undo_request_id` returns that receipt.
   - With a different one, it gives `already_undone`.
4. **Window.** `tx.now > undo_until` gives `undo_expired`, and nothing is written.
5. **Revisions.** Every touched record's current revision must equal the revision in the original
   receipt. Otherwise `undo_conflict(record_changed)`.
6. **Dependents.** A created record that now has a live referrer under `api.DEPENDENTS` gives
   `undo_conflict(dependent_exists)`.
7. **Apply the before-images** as revision+1:
   - undo of a create is that entity's delete;
   - undo of a delete is a restore, or the prior status;
   - undo of an update restores the prior inputs.
8. `finish(committed, undo_of=original)`.

More undo rules:
- A bulk undo is all-or-nothing.
- An undo is never undoable itself.

**`POST /commits/{request_id}/abandon`** with body `{}`.
- An existing `owner_direct` or `owner_undo` proposal under that id returns its receipt.
- An existing proposal of any other kind under that id gives `idempotency_mismatch`, and so does an
  id that equals any proposal's `approval_id` or `rejection_id` or any receipt key (§5.3). An abandon
  can therefore never squat another proposal's decision key.
- Otherwise it inserts an `owner_direct` proposal with the abandon columns of §5.3 (no mutations) and
  calls `finish(abandoned)`: the version bumps, one event is written, and no outbox row. A late
  original then returns `abandoned` (§5.3).

**`GET /deleted`**, from §10.2.

**Logs:** `os_commit op=<op> outcome=<status|code> ms=…`.

### 11.2 Frontend commit path (V2-32, `os-commit.mjs`)

**Transport.** Through V2-21's `OsClient.commit(body)`, `.undo(id, undoId)`, `.abandon(id)` and
`.receiptV2(id)`.

**Marker.**
- Key: `assafweb.os.pending.v2.<binding>`.
- Value: an array of at most 20 entries `{request_id, op, entity, record_id, body_digest, saved_at}`.
  **Field values are never stored.**
- Written before dispatch, with no `await` in between.
- Cleared only on a terminal receipt (committed, target_changed, abandoned) or a `not_started` error.
- The v1 marker is still recovered.

**Lost response** (timeout, `unknown`, network):
1. `GET /receipts/{id}`. A terminal receipt is applied.
2. If the result is `unknown` and the body is still in memory, show the owner-tapped "שלח שוב", which
   sends the same id and the same body.
3. If the body is gone (a reload or an eviction), show "בטל ושלח מחדש". It calls `POST abandon`, waits
   for a terminal receipt, clears the marker, and then the owner re-enters with a new id.
4. There is never an automatic re-POST.

**Apply.** The row is replaced only from `receipt.records`, and the §10.6 stale guard applies.

**Undo bar.**
- `#undo-bar` is visible for 8 s and has 44 px targets and `aria-live`.
- It shows `remaining_ms = Date.parse(until) - Date.parse(server_now) - (performance.now() - receivedAt)`,
  which never reads the device wall clock. Using `server_now` instead of `committed_at` keeps the
  countdown right when the receipt was recovered later through `GET /receipts/{id}`.
- It survives a re-render.
- It hides on a 401, on sign-out, and on an epoch change.

**`DECISIONS.md` amendment:** "a server-confirmed row patch plus undo is not optimistic success". This
needs Assaf's OK under FREEZE.

### 11.3 Fast tasks (V2-33, `os-tasks.mjs` + `os-tasks.css`)

| UI action | Commit op |
|---|---|
| Checkbox | `complete` / `reopen`, with `expected_revision` |
| Quick-add (Enter) | `create` (`title`, `status:'לביצוע'`) |
| Inline edit | `update {title}`; pauses the refresh while focused |
| `⋯` → delete (E2E-tested) | `delete`; swipe is progressive enhancement, PHONE_VERIFIED only |
| Clear done | one `bulk_delete_done` of the visible done pairs, with one undo |

Every row changes only after its receipt.

## 12. Statements (V2-25 domain, V2-26 backend, V2-27 screen)

### 12.1 Domain (V2-25, as built)

The public API is `app.domain.statements.__all__`:
- `parse_statement_batch(raw, *, pepper, workspace)`;
- `statement_id(pepper, workspace, account_label, charge_date)`;
- `line_key(pepper, statement, facts, occurrence_index)`;
- `diff_statement`, `apply_rules`, `match_batch`, `map_to_expense`, `summarize_cycle`,
  `subscription_view`.

It is frozen at its merge SHA, and later changes are additive. The pepper is `os_statement_pepper`.
Format `statement-batch.v1`:
- top level: `{schema, client_batch_id, account_label, charge_date, lines}`;
- each line requires `txn_date, merchant_display, amount_minor, kind, currency, confidence,
  suggested_tag`, and may carry `original_amount_minor, original_currency, installment,
  suggested_category, suggested_path, is_subscription`;
- at most 300 lines and 128 KiB.

### 12.2 Service (V2-26, `app/services/os_ledger/statements.py`)

```python
class StatementBatchService(Protocol):
    def submit(self, ctx, raw: bytes, *, source: Literal["ASSISTANT_PASTE","ASSISTANT_CLAUDE","ASSISTANT_GPT"]) -> BatchSubmitted
    def context_for_assistant(self, ctx) -> dict            # business rules + last 12 statement keys
    def status_for_credential(self, ctx, proposal_id: str) -> dict   # own credential only, else batch_not_found
    def cycles(self, ctx) -> dict; def statement_view(self, ctx, statement_id: str) -> dict
    def review(self, ctx, proposal_id: str) -> dict; def lines(self, ctx, proposal_id: str, *, after, limit) -> dict
    def save_draft(self, ctx, proposal_id: str, draft_revision: int, overrides: list, choices: dict) -> dict
    def approve(self, ctx, proposal_id: str, decision_id: str, review_digest: str) -> ReceiptResult
    def reject(self, ctx, proposal_id: str, decision_id: str) -> ReceiptResult
    def revoke_pending_for_credential(self, tx: LedgerTx, credential_id: str) -> int
        # runs INSIDE the caller's open LedgerTx (V2-37's revoke); never opens a transaction of its own (§6).
        # finish(rejected, reason_codes=["credential_revoked"]) + purge, for each pending batch of that key
    def expire_due(self, ctx_worker) -> int
        # WORKER "retention", transaction(kind="statement_batch", purpose="expire") (§5.3): finish(expired) + purge for
        # pending batches past expires_at (reason_codes []), or with zero batch lines (["restored_without_detail"]),
        # or from an older epoch (["epoch_changed"])
```

**Statement tables.** V2-26 owns a `StatementTables` protocol with two implementations:
- PG, through `tx.connection` on the §4 tables;
- in-memory, through `tx.scratch`, for fake-backed tests.

Every statement write happens inside the same `LedgerTx` as the ledger effects it accompanies.

**The pepper is write-once.** `os_workspaces.statement_pepper_fp` holds
`HMAC-SHA256(pepper, "assaf-os-statement-pepper-fingerprint.v1")` as hex. It is written once, by
`AuthorityAdmin.pin_statement_pepper` from `mia os-authority init` on the privileged DSN; the runtime
has no UPDATE on it. Every submit, draft, approve, reject and `expire_due` compares the configured
pepper's fingerprint with the pinned one: NULL or a mismatch gives 503 `ledger_unavailable` (logged
`reason=statement_pepper_unpinned|statement_pepper_mismatch`) and writes nothing. A rotated pepper
would change every `statement_id` and `line_key` and collide on `UNIQUE (account_label,
charge_date)`, so rotation is not supported.

**Request ids.** Assistant: `api.assistant_request_uuid(credential_id, client_batch_id)`. Owner paste
(no credential): `api.paste_request_uuid(owner_id, client_batch_id)`.

**Submit.**
1. Parse with the domain.
2. Load the stored lines, the rules, and the candidate manual expenses.
3. Compute the base diff, with no overrides.
4. Insert:
   - `os_statement_batch_lines` (full detail, every line);
   - the proposal: `kind statement_batch`, `entity "statements"`, `record_id = statement_id`,
     `expires_at = now + 7 d`. Its mutations hold only the business effects plus
     `{line_key, tag:"personal"}` for personal lines.
5. Supersede per FORK-SUPERSEDE:
   - the same credential, or two owner pastes: the older pending proposal gets `finish(superseded)`,
     **unless** it has an `os_statement_review_drafts` row with `draft_revision > 0`. Then the result
     is `conflict:true` and both stay reviewable, because a batch with a saved draft is never
     discarded without an owner action;
   - otherwise `conflict:true`.
6. Personal rules are matched by `HMAC(pepper, merchant_key)`.

**Personal rule ids.** For `tag=personal`, V2-26 mints `rule_id = "MR_" +
HMAC-SHA256(pepper, "merchant-rule\x1f" + merchant_key)[:24]` and never persists the `rule_id` that the
domain's `learn_rules` derives (an unkeyed sha256 of the plaintext key, which a brute force over
low-entropy merchant names would reverse). Business rules keep the domain id. A test asserts that no
stored personal `rule_id` equals the sha256-derived id, and greps a test dump for the derived id.

**Draft.**
- Body: `{draft_revision, overrides:[{line_key, tag?, category?, path?, is_subscription?, make_rule?}] (≤100), choices?:{owner_edited:{key:"keep"|"overwrite"}, merge:{key:expense_id|null}}}`.
- **Delta semantics.** Each POST merges into the stored draft: an override entry replaces that
  line's stored override, an entry with every optional field `null` removes it, and `choices` merge
  by key. The stored draft holds at most one override per line (≤300), so a review needing more than
  100 corrections is saved over several POSTs, each under the 16 KiB cap.
- A stale `draft_revision` gives `draft_conflict`. Each accepted POST bumps `draft_revision` by 1.
- The response is `{draft_revision, review_digest, blocking}`.

**`review_digest`** = `digest({proposal_id, base_digest, draft_revision, overrides_digest, choices,
dependency_state_digest})`.

**Approve.** The body is `{request_id: approval_request_id, review_digest}`.
1. Recompute the diff against the current state with the draft applied.
2. Then:
   - a digest that differs from the current recomputation gives 409 `review_stale`, with no receipt
     (the client reloads the review);
   - blocking `unassigned_lines` or `merge_choice_required` gives 409.
3. Otherwise, in one `LedgerTx`:
   - the expense ops through `apply`;
   - statement and line upserts;
   - rule upserts for `make_rule`;
   - `UPDATE os_statement_batch_lines SET detail=NULL, purged_at=now()`;
   - any other pending batch for the statement gets `superseded`;
   - `finish(committed)`.

**Reject** and **expire** purge the same way. Reads never write, so expiry is applied only by:
- the `os-retention` job's `expire_due`, which runs as the runtime role on the WORKER channel under
  the expiry admission (§5.3), so the kill switch, `backend=sheets`, a fenced `statements` domain or a
  disabled ledger never stop it;
- or an approve or reject that arrives after `expires_at`, which finishes the batch as `expired`.

Until then, the review shows a pending batch past `expires_at` as expired, and Approve is disabled.
The same job then deletes purged rows using the retention role. **Backstop:** the retention role
also deletes the batch lines of any proposal whose `expires_at` is more than 1 day in the past,
whatever `purged_at` says (§18.2), so detail cannot outlive the window even if the runtime path is
down.

**After a restore.** FORK-BACKUP excludes `os_statement_batch_lines` data, so a restored pending batch
has no lines. `expire_due` finishes it `expired` with `reason_codes=["restored_without_detail"]` on
its first run, and Approve refuses it the same way until then.

**A revoked credential's batch** can never be committed. Approve re-checks the submitting credential.
A batch whose key is revoked, or was created at or before the owner's revoke-all watermark, is
finished as `rejected` with `reason_codes=["credential_revoked"]`.

**Mirror and summaries** never see personal lines. A missing cycle after the 20th is `null`, never 0.

### 12.3 Routes (`app/api/os_v2/statements.py`)

The routes are guarded by `assaf_os_statements_enabled`:
- `GET /statements`
- `GET /statements/{statement_id}`
- `POST /statement-batches` (paste; source `ASSISTANT_PASTE`)
- `GET /statement-batches/{id}`
- `GET /statement-batches/{id}/lines`
- `POST /statement-batches/{id}/draft`
- `POST /statement-batches/{id}/approve`
- `POST /statement-batches/{id}/reject`
- `GET /merchant-rules`

The cycle view replaces the roadmap's `/cycles/{charge_date}/business`: one route per screen.

### 12.4 Statement wire shapes (each also carries the §10.1 binding)

**`statement_list`** has `cycles`: up to 36 entries of:
```
{cycle, account_label, charge_date|null, statement_id|null,
 status: imported|pending|not_imported,
 business_total_minor|null, business_line_count|null, pending_proposal_id|null}
```

**`statement_view`** has:
- `statement_id, cycle, account_label, charge_date, cycle_from, cycle_to`;
- `totals`: `{business_charge_minor, business_credit_minor, business_total_minor,
  business_line_count, personal_count, unassigned_count}`;
- `by_category`: `[{category, amount_minor}]`;
- `subscriptions`: `[{label, amount_minor|null, original_amount_minor|null, original_currency|null,
  line_count, previous_amount_minor|null, badges:[new|changed|missing]}]`;
- `previous`: `{statement_id, business_total_minor}|null`.

**`batch_created`** has:
- `proposal_id, statement_id, status:"pending", conflict, superseded_proposal_id|null`;
- `summary`: `{line_count, business_count, personal_count, unassigned_count, disagreement_count}`.

**`batch_review`** has:
- `proposal_id, statement_id, cycle, account_label, charge_date`;
- `actor_source, credential_label|null, submitted_at, expires_at, status`;
- `approval_request_id, rejection_request_id`;
- `conflict_with:[ids]`, `draft_revision`, `review_digest`;
- `counts`: `{add, keep, update, void, owner_edited_kept, kept_earlier_choice}`;
- `merges`, `blocking:[codes]`, `totals` (as in `statement_view`), `line_count`.

**`batch_lines`** has `lines` (up to 100) and `next_cursor`, ordered by merchant_key, then txn_date,
then line_key. Each line:
```
{line_key, txn_date, merchant_display, amount_minor, kind, currency,
 original_amount_minor|null, original_currency|null, installment:{n,of}|null,
 suggested_tag|null, tag|null, category, path, is_subscription, rule_id|null,
 disagreement, confidence, action, flags:[…], candidate_expense_ids:[…]}
```

**`draft_saved`** is `{draft_revision, review_digest, blocking}`.

**`merchant_rules`** has `rules`. Each rule:
```
{rule_id, merchant_key|null, match, tag, category|null, path|null, platform|null,
 is_subscription|null, label|null, revision}
```

**V2-27 rendering rules.**
- Merchant text goes through `esc()` and `<bdi dir=ltr>`.
- Totals come only from server minor units.
- Approve stays disabled while `blocking` is non-empty.
- Tag edits autosave through `draft` and never touch storage.
- Approve sends the displayed `review_digest`.
- The screen needs Assaf's FREEZE visual OK.

## 13. Telegram and leads on the ledger (V2-28)

**Approve and reject cards.**
1. `assaf_os.approve|reject` stores the proposal's decision UUID (36 chars) in the ApprovalRow
   parameters JSON, not in `approval_id String(32)`.
2. It commits that CAS first.
3. Then it calls `ledger.decide` with `from_telegram_decision`.
4. Crash recovery is `ledger.receipt(decision_id)`, which replaces sticky `unknown` for this kind.

**Cards.**
- `current_target` compares a byte-stable proposal view.
- Cards are projected from `mutations`, including `promote_lead`, and stay within 3,800 chars.
- A batch only ever gets a link notice, never a button.

**Website leads.**
- The id is `mia_request_uuid(owner, ws, "website-lead-<hex>")`.
- The duplicate pre-check is a ledger read.
- A duplicate gives `lead_already_exists`, a definite no-write with no `pending_review` row.
- Review the legacy projection flag at `crm_runtime.py:195`.
- The Mia tool can never reach decide or commit; that is tested.

## 14. Mirror (V2-29)

**Transport** (`app/integrations/sheets_mirror.py`):
- `SheetsMirrorTransport(service_account_json, spreadsheet_id)` exposes `batch_update(ranges)` and
  `batch_get(ranges)`.
- Auth: an RS256 JWT-bearer grant (`pyjwt`), scope `spreadsheets`, with the token cached in memory.
- Writes use `valueInputOption=RAW`; reads use `UNFORMATTED_VALUE`.
- Calls go through stdlib urllib with a 15 s timeout.
- The spreadsheet id must equal `os_mirror_spreadsheet_id`.
- Values are never logged.

**Outbox** (`outbox.py`):
- `claim(limit, lease_owner, now, lease_ttl=120 s)` runs in a short transaction. `UPDATE … FOR UPDATE
  SKIP LOCKED` is not valid PostgreSQL, so the lock is taken in a subquery:
  ```sql
  UPDATE assaf_os.os_outbox SET status='leased', lease_owner=:owner, leased_at=:now,
         lease_generation=lease_generation+1, attempts=attempts+1, updated_at=:now
  WHERE (workspace_id, event_id) IN (
    SELECT workspace_id, event_id FROM assaf_os.os_outbox
    WHERE workspace_id=:ws AND (
          (status IN ('pending','unknown') AND next_attempt_at <= :now)
       OR (status = 'leased' AND leased_at < :now - :lease_ttl))
    ORDER BY commit_version
    FOR UPDATE SKIP LOCKED LIMIT :n)
  RETURNING workspace_id, event_id, commit_version, body, lease_generation
  ```
  A worker that crashed between claim and finish leaves a lease that is reclaimed after `lease_ttl`.
- `finish(event, generation, status, error_code, next_attempt_at)` is a compare-and-set on
  `lease_generation`. It sets `lease_owner` and `leased_at` to NULL, which V2-14's CHECKs require
  (`(status = 'leased') = (lease_owner IS NOT NULL)`).
- `os_mirror_state.verified_version = GREATEST(verified_version, <the observed_version of the
  projection that was read back equal>)`. It never moves backwards, and it is never set from the head
  or from an event's own `commit_version`.

**Worker loop** (`os_mirror.run_once`):
1. Claim.
2. Load the latest records through the reader. No transaction stays open.
3. Project the rows for each tab (headers are the schema labels). The summary tab and the client
   calculated columns come from `os_summary_monthly`.
4. Write.
5. Read back.
6. Outcome:
   - equal: `sent`, and `os_mirror_state.verified_version` advances by the GREATEST rule;
   - a transport error after the send: `unknown`, and the next tick reads back before any rewrite;
   - a mismatch: `conflict`.

An event of kind `import` (`{refs:[], full:true}`) projects every tab in full.

**Anti-entropy** runs every 900 s, even with an empty queue. Drift sets `status='conflict'` with
`last_error_code='drift'`, and is never imported.

**Conflict recovery.** `conflict` is cleared only by the worker itself: the next anti-entropy tick
rewrites the full projection and reads it back; an equal readback sets `synced` and advances
`verified_version`, and a mismatch keeps `conflict`. While it lasts, the owner sees it through the
`mirror.state = conflict` chip (I2), the reason-code log `os_mirror op=verify outcome=conflict
reason=<code>`, and the `os_verifier` job's report. There is no new outbound alert channel.

**Wire state.**

| `os_mirror_state` | `mirror.state` |
|---|---|
| synced | verified |
| pending/delayed past the warn threshold | lagging |
| unknown | unknown |
| conflict | conflict |
| fenced, or disabled | disabled |

**Pauses and start.**
- The worker pauses on the kill switch and the freeze.
- It is started only by `start_if_enabled`, in a single process.
- The config subtraction (§3.1) keeps the mirror out of Mia's `sheets.update`.

## 15. Importer (V2-30)

**`xlsx.read_xlsx(path)`** uses stdlib `zipfile` and `xml.etree`.
- Refused inputs:
  - absolute or `..` member names;
  - any `<!DOCTYPE` or `<!ENTITY`;
  - a compression ratio above 100;
  - more than 200 MB uncompressed, or more than 2,000,000 cells.
- It returns shared and inline strings, numbers as decimal text, booleans, and date serials as
  numbers plus a date-format flag. Hidden sheets are included.

**`import_workbook.stage(ctx_worker, workbook, source_binding)`**
- Maps rows through the prototype physical-row mapper into `os_import_*`.
- The source kind `owner_export:` joins `synthetic:` at `shadow_import.py:394-400`.
- Unaccepted and id-less rows are quarantined, never dropped.

**`manifest(batch_id)`** covers counts, ids, revisions, input digests, per-currency totals, refund
chains, references and missing-time nulls, plus `cancellations: [{entity, record_id}]` (empty on the
first epoch). Its digest is `digest(manifest)`.

**Epochs.** `os_import_batches` is unique on `(workspace_id, authority_epoch, source_digest)` (§4), so
re-staging an unchanged workbook after a rollback (`fenced→shadow`, epoch E+1) is a new batch.

**`promote(ctx_worker, batch_id, approved_manifest_digest)`**
- Runs in one `LedgerTx`, kind `import`, `core=shadow`, with
  `request_id = api.import_request_uuid(epoch, manifest_digest)` and digest
  `{"import": manifest_digest, "epoch": epoch}`. A receipt from epoch E is never replayed at E+1.
- Each record is written with its manifest revision (§5.3 import exception): a create carries the
  Sheets revision (≥ 1), and an update must carry a revision greater than PG's current one.
- **Orphans.** From the second epoch on, PG may hold live ids that the manifest lacks, because they
  were deleted in Sheets during the rollback window. Promote refuses with `import_orphan_rows`
  (details: counts per entity) unless the signed manifest lists each such id under `cancellations`.
  Promote then applies each as that entity's FORK-DELETE (a cancellation status, or a tombstone for a
  task), in the same transaction.
- It produces one receipt (with `records:[]` and the full-projection outbox row, §5.3), and is
  idempotent.
- It refuses `import_quarantine_unresolved`, `manifest_digest_mismatch`, `import_orphan_rows` and
  `authority_not_active`.

**`scripts/os_workbook_diff.py`** prints the count of changed cells plus `(sheet, cell)` refs, never
the values. It is local only, with no network. Fixtures are synthetic.

## 16. Fence and cutover tooling (V2-31)

**The CLI.** `mia os-authority init|preflight|freeze-plan|activate|fence|rollback|status --approved-sha <40-hex>`
runs on the privileged DSN, with the same SHA check as `mia os-ledger`.

**`init`** (step 4 of FORK-PROD-ORDER) calls `ensure_workspace(workspace, owner, epoch=1)` and, when
`os_statement_pepper` is set, `pin_statement_pepper`. It runs before any import. `preflight` records
the time of its Sheets fence probe in its evidence file, which `activate` reads.

**`activate` refuses**, with a reason code, unless every precondition holds. Each is machine-checked:

| Reason code | Precondition checked |
|---|---|
| `release_sha_mismatch` | The image SHA equals the approved SHA |
| `sheets_not_fenced` | A read-only Script probe returns `unavailable`. V2-31 calls the Apps Script `schema` action through the existing transport directly, not through the factory, which refuses under `postgres` |
| `freeze_off` | `settings.assaf_os_freeze` is true |
| `capture_before_fence` | The signed manifest's xlsx capture time is later than the fence-probe time recorded by `preflight` |
| `main_role_unsafe` | The main DSN passes the V2-14a posture check |
| `ledger_role_unsafe` | The V2-15 grant-map validator passes |
| `sheets_pending_proposals` | Zero Sheets proposals pending |
| `sheets_unknown_receipts` | Zero Sheets receipts unknown |
| `approval_cards_open` | Every assaf_os.approve card is terminal |
| `backup_too_old` | V2-10 evidence: a dump less than 1 h old |
| `restore_drill_too_old` | V2-10 evidence: a drill within 7 days |
| `mirror_not_verified` | The mirror is empty or verified |
| `manifest_not_signed` | The import manifest is signed |
| `fe_v2_reader_not_live` | Operator evidence file with the Cloudflare version and bundle hash |
| `journal_capacity` | V2-00 measure file: capacity below the cap |

**Other commands.**
- `activate` (`shadow→active`) never bumps the epoch. Bumps happen only on `fenced→shadow` and
  `restoring→active`, and each is idempotent through `expected_epoch` (§5.4).
- `freeze-plan` prints the exact `mia-env` keys to flip, then `mia settings`. The CLI never touches
  Secret Manager.
- `rollback` is allowed only before the first non-import receipt. It sets `core=fenced` and prints
  the steps to return to `backend=sheets` and `ASSAF_OS_MODE=LIVE`.

**Apps Script fence harness** (`tests/assaf-os/apps-script-fence.test.js`):
- Stubs PropertiesService.
- Under `FENCED`, it proves that `doPost`, `reconcileAssafOsEdit`, `reconcileAssafOsWorkbook` and
  `refreshAssafOsReferenceValidations` throw `unavailable`.
- Under `LIVE`, they work again.

**`CUTOVER_RUNBOOK.md`** covers T1-T7:
- T6 is `mia settings`, then `/health` (SHA and epoch).
- T3 adds warning protection and renames the old book "ARCHIVE – edits ignored".
- T7 adds a third xlsx capture, diffed against capture #1.

## 17. Assistant: propose-only API and MCP (V2-37, V2-38)

### 17.1 Credentials

- **Format:** `aosp_` + 43 base64url characters (256 bits), shown once.
- **Storage:** only `credential_hash(value, purpose="assistant")`.
- **Expiry:** default 30 days, maximum 90.

**Owner routes** (`app/api/os_v2/credentials.py`):
- `GET /assistant-credentials` returns `{credentials:[{credential_id, label, kind, created_at,
  expires_at, last_used_at|null, use_count, revoked_at|null}]}`.
- `POST /assistant-credentials` with `{label, kind, expires_in_days}` needs the header `X-Step-Up`.
  It returns that list item plus `secret`.
- `POST /assistant-credentials/{id}/revoke` and `POST /assistant-credentials/revoke-all` return the
  list. Revoking auto-rejects that key's pending batches.

**Revocation also comes from:**
- **The existing owner revoke-all.** V2-37 does **not** edit `owner_sessions.py`. On every call it
  reads the existing revoke-all watermark (`revoked_before`) through the session store. Any key with
  `created_at` at or before the watermark is revoked.
- **An epoch bump.** The key's epoch no longer matches.

**Step-up.** `POST /v1/owner/auth/step-up` with `{id_token}`, served from `app/api/owner_step_up.py`
(its own router under the same `/v1/owner/auth` prefix; `owner_sessions.py` is untouched).
- It needs an owner session + CSRF.
- The Google token is verified by `owner_google_login.verify_step_up_id_token` (V2-37's helper,
  added after V2-08) and must carry `auth_time` no older than 600 s.
- It returns `{step_up_token, expires_at}`. The token is an HMAC (`os_step_up_secret`) over
  `session_hash | auth_time | exp`, valid for 10 min.

### 17.2 Bearer routes (`app/api/assistant/os.py`)

These routes use the prefix `/v1/assistant/os`. Caddy already proxies every path to MIA unchanged, so
the Caddyfile is not edited (the stock `caddy:2.11.4-alpine` image has no rate-limit directive). The
**per-IP limit is enforced in MIA**, before the bearer lookup: 60 requests per 10 min per
`client_ip_hmac` (keyed with `os_assistant_ip_hmac_key`), stored with the existing auth-attempt
store pattern, otherwise 429 `rate_limited`. The Worker never forwards these routes.

**Enforcement order.**
1. A cookie present gives 401.
2. The header must match exactly `Bearer aosp_[A-Za-z0-9_-]{43}`.
3. Look up the hash.
4. Check revoked, expiry, the owner allow-list, the epoch, the kill switch, and `assistant_enabled`.
   Any failure is a uniform 401 `assistant_auth_required`. Only invalid lookups count against the
   dedicated `assistant_bearer` failure budget; a valid key is never blocked by it.
5. Rate limits: 30 requests/h and 5 batches/day per key, otherwise 429.
6. `from_assistant`, then `require(op)`.

**Endpoints.**
- **`GET /context`** returns `{api_version:"assaf-os-assistant.v1", rules:[business only:
  {merchant_key, match, category, path, platform, is_subscription, label}], statements:[{account_label,
  charge_date, status, line_count}] (≤12)}`.
- **`POST /statement-batches`** takes `statement-batch.v1` (≤128 KiB). Its `request_id` is
  `uuid5(REQUEST_NS, credential_id + ":" + client_batch_id)`. It returns `{api_version, proposal_id,
  status:"pending", review_url:"<owner_os_origin>/#statements/<proposal_id>", summary:{line_count,
  business_count, personal_count, unassigned_count}}`. The summary comes from the submitted lines
  only: no merge, void or replace counts.
- **`GET /statement-batches/{id}`** covers the key's own batches only, otherwise 404. It returns
  `{api_version, proposal_id, status, updated_at}`.

Decision ids never appear in any bearer response.

**Audit.** One `os_assistant_audit` row per call, with codes only. `client_ip_hmac` is keyed with
`os_assistant_ip_hmac_key`. Rows are kept for 180 days.

### 17.3 Connections screen (V2-39)

- The secret appears only in the issue response. It is never stored, and it leaves the DOM on dismiss.
- `step_up_required` leads to the Google re-auth.
- Actor labels:
  - "נוצר על ידי Claude";
  - "נוצר על ידי GPT";
  - "הדבקה (לא מאומת)".

### 17.4 MCP (V2-38)

`POST /v1/assistant/mcp` is stateless, JSON-RPC 2.0, Streamable HTTP with JSON responses only. It has
no SSE and no session id.
- Methods: `initialize`, `ping`, `tools/list`, `tools/call`.
- Tools, exactly:
  - `get_statement_context {}`;
  - `submit_statement_batch {batch}`;
  - `get_batch_status {proposal_id}`.
- It uses the same bearer dependency and service calls. Errors come back as `isError` content carrying
  codes only.
- `docs/openapi-assistant.yaml` describes the three REST routes with bearer auth, for a Custom GPT
  Action set to "Only me".

## 18. Roles

### 18.1 V2-14a APP-ROLE-DEMOTE

This section describes the design as V2-14a built it (FORK-APP-ROLE).

**Why ownership transfer.** `migrate` and `app` both read `/etc/mia/settings.env` and
`/etc/mia/runtime.env` (`compose.yaml`). After the DSN switch, `mia-migrate` (`init_db` +
`apply_migrations`) therefore runs as `mia_app`. A DML-only role cannot ALTER tables it does not own,
so every later schema migration would fail the deploy.

**The role.** `mia_app` is LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS, with
no memberships, and it is not the database owner.
- It **owns every `public` object**: tables (their serial/identity sequences move with them),
  sequences, views and functions. An extension-owned object stops the run with
  `app_role_extension_object_requires_review`.
- It has CONNECT on the database and USAGE, CREATE on schema `public`.
- Implicit PUBLIC EXECUTE on functions is revoked globally, as the built SQL does.
- It has nothing on `assaf_os`. The posture check is V2-14's `_validate_main_role_catalog`, imported
  from `app.db.os_ledger_migrate` and never copied.
- `mia` stays superuser for backup, restore and `mia os-ledger` only. Its DSN survives only in the
  VM-owned rollback file.

**Files and behaviour (as built, plus the finishing work in §2).**
- `ops/gcp/vm/app_role_demote.py` (wrapped by `app-role-demote.sh`, under
  `/var/lock/mia-state.lock`) has the modes `stage`, `check`, `activate`, `enforce`, `rollback` and
  the new `reapply`. It edits only VM-owned `/etc/mia/runtime.env` (`MIA_DATABASE_URL`,
  `MIA_APP_ROLE_POSTURE`) and its own staged and rollback files. It never touches `mia-env`, never
  restarts, deploys or fetches settings, and never prints a password.
- `mia settings` cannot switch the DSN, because its `owned` regex strips `MIA_DATABASE_URL` from
  `mia-env`. The runbook never uses it for this.
- `app/db/app_role_posture.py` + `/health/ready`: under `app_role_posture=enforce`, a failing
  posture makes `/health/ready` 503; liveness is unaffected. While the ledger is enabled, the §5.5
  guard checks the same posture per engine and refuses with `ledger_unavailable`.
- Runbook `docs/runbooks/APP_ROLE_DEMOTE.md`: jobs paused and a verified backup → `stage` →
  `activate` (observe) → the runbook's recreate of `migrate` + `app` → a migration smoke (`mia-migrate`
  exits 0 as `mia_app`) and readiness → `enforce` → recreate → evidence in HANDOFF. Rollback is
  `rollback` + recreate, never `mia restore`.

**Every restore re-applies the postures (V2-10).** `mia restore` runs `DROP/CREATE DATABASE mia` and
then `pg_restore --no-owner --no-privileges` as `mia`. Afterwards every object is owned by `mia` and
every grant is gone: `mia_app` could not read its own tables, and the `assaf_os` ownership and the
ledger grants would be lost, so any restore (drill or rollback) would be a full outage. V2-10 adds a
post-restore step that runs after `pg_restore` and **before `start_services`**, under the same lock.
Any failure aborts with the services down:
1. `app_role_demote.py reapply` (V2-14a) re-runs the ownership transfer and the grants as `mia`. It
   is idempotent. Roles are cluster-global, so they survive `DROP DATABASE`.
2. `provision_ledger.py --mode reapply` (V2-15 r2, extended by V2-14b):
   - creates a missing role, but never changes an existing role's password;
   - re-owns `assaf_os` and every object in it to `assaf_os_owner`;
   - re-applies the full grant map;
   - runs `validate_ledger_catalog`.
   On a database without `assaf_os` it is a no-op.
3. It sets `owner_os_revocations.revoked_before` to the restore time, so every owner session issued
   before the restore point is revoked (critique 0 #4.4).
4. After start, the first `os-retention` run finishes line-less pending batches as
   `restored_without_detail` (§12.2).

V2-10 also:
- applies `--exclude-table-data=assaf_os.os_statement_batch_lines` to the pre-restore safety dump,
  as FORK-BACKUP does to every dump;
- adds `MIA_OWNER_OS_\w+`, `MIA_OS_LEDGER_\w+` and `MIA_APP_ROLE_POSTURE` to the `owned` regex, so
  `mia-env` can never override a VM-owned DSN or the posture;
- owns the restore drill, `tests/ops/test_restore_reapply.py`. It dumps a database that has both
  postures, restores it through the same commands into a fresh database, and runs the post-step.
  Then both `_validate_main_role_catalog` and `validate_ledger_catalog` must pass, and `mia_app` must
  be able to SELECT and INSERT on its tables.

### 18.2 Ledger grant map (V2-15 for the V2-14 tables, V2-14b for its own; the validator fails on anything else)

**`assaf_os_ledger`** is LOGIN NOINHERIT, with no memberships, no CREATE or TEMP, and no SET ROLE. It
has USAGE on the schema and EXECUTE on `expense_source_key_sha256`. Its DSN is
`MIA_OWNER_OS_LEDGER_DATABASE_URL` (FORK-LEDGER-DSN). `role_policy.py` holds the map as literal
column tuples, and a test pins each tuple to the SQL's columns.

**The record tables.** The common UPDATE columns are `revision`, `inputs`, `business_date`, `source`,
`updated_at`, `deleted_at` and `deleted_by_request_id`. `apply` rewrites `source` on every update
(the prototype sets `current.source = row.source`), so without it the first owner edit of an imported
row would fail with "permission denied" and surface as `ledger_unavailable`. Each table adds its own
typed columns:

| Table | UPDATE = the common columns + |
|---|---|
| `os_leads` | `person_id` |
| `os_clients` | (nothing) |
| `os_works` | `person_id`, `agreed_amount_minor`, `currency` |
| `os_payments` | `person_id`, `work_id`, `amount_minor`, `currency`, `original_payment_id` |
| `os_time` | `person_id`, `work_id`, `minutes` |
| `os_expenses` | `amount_minor`, `currency`, `source_type`, `source_key`, `source_digest` |
| `os_tasks` | `person_id`, `work_id`, `due_date` |

Never updatable: `workspace_id`, `record_id`, `created_at`, and the generated `os_tasks.status`.

**The other tables:**

| Table | Owner chunk | SELECT | INSERT | UPDATE (columns) |
|---|---|---|---|---|
| `os_workspaces` | V2-15 (+14b) | yes | no | `version`, `summaries`, `updated_at`; V2-14b adds `pending_seq`. Never `epoch` or `statement_pepper_fp` |
| `os_authority` | V2-15 | yes | no | none |
| `schema_migrations` | — | no | no | none |
| `os_proposals` | V2-15 | yes | yes | `status` |
| `os_receipts`, `os_events` | V2-15 | yes | yes | none (`reason_codes` is INSERT-only) |
| `os_outbox` | V2-15 | yes | yes | `status`, `attempts`, `next_attempt_at`, `lease_owner`, `lease_generation`, `leased_at`, `last_error_code`, `updated_at` |
| `os_mirror_state` | V2-15 | yes | yes | `status`, `projected_version`, `verified_version`, `last_attempt_at`, `last_verified_at`, `last_error_code`, `watermark`, `scan_coverage`, `updated_at` |
| `os_summary_monthly` | V2-15 | yes | yes | `version`, `summary`, `calculated_at` |
| `os_summary_cycle` | V2-15 | yes | yes | `version`, `starts_on`, `ends_on`, `summary`, `calculated_at` (reserved, unused, §8) |
| `os_import_batches` | V2-15 | yes | yes | `status`, `quarantine`, `completed_at` |
| `os_import_rows` | V2-15 | yes | yes | `record_id`, `status`, `canonical_record`, `quarantine` |
| `os_statements` | V2-14b | yes | yes | `status`, `line_count`, `lines_digest`, `current_proposal_id`, `approved_proposal_id`, `imported_version`, `updated_at` |
| `os_statement_lines` | V2-14b | yes | yes | every column except `workspace_id`, `statement_id`, `line_key` and `created_at` |
| `os_merchant_rules` | V2-14b | yes | yes | every column except `workspace_id`, `rule_id` and `created_at` |
| `os_statement_review_drafts` | V2-14b | yes | yes | `draft_revision`, `overrides`, `choices`, `updated_at` |
| `os_statement_batch_lines` | V2-14b | yes | yes | `detail`, `purged_at` |
| `os_assistant_credentials` | V2-14b | yes | yes | `revoked_at`, `last_used_at`, `use_count` |
| `os_assistant_audit` | V2-14b | yes | yes | none |

V2-14b enumerates the "every column except" rows as literal tuples in `role_policy.py`.

Nowhere: DELETE, TRUNCATE, REFERENCES, TRIGGER.

**`assaf_os_retention`.** V2-15 keeps it NOLOGIN with zero privileges, as built, because its tables
do not exist yet. V2-14b makes it LOGIN NOINHERIT with exactly:
- USAGE on the schema;
- SELECT and DELETE on `os_statement_batch_lines`, used only for rows `WHERE purged_at IS NOT NULL`,
  **or** whose proposal's `expires_at < now() - interval '1 day'` (the I11 backstop, whatever
  `purged_at` says);
- SELECT and DELETE on `os_assistant_audit`, used only for rows with `occurred_at` older than 180 d;
- SELECT `(workspace_id, proposal_id, kind, status, expires_at)` on `os_proposals`.

Its DSN, `MIA_OS_LEDGER_RETENTION_DATABASE_URL`, is read with `os.getenv` by the os-retention job
only, from a job-only env file (§3.1 V2-31). It is never a `Settings` field and never in
`runtime.env`.

**The main app role** has nothing on `assaf_os`. The ledger, retention, migration and main DSN users
must all differ.

## 19. Integration order and merge sequence

Every merge needs review PASS and CI green on the exact head. **No dependent merges before V2-16.**
Each one's tests must pass on the fake and on PG.

**Backend:**
1. V2-14 (**merged**, `12254fa`)
2. V2-15 (r2)
3. V2-16s (after V2-15: `os_ledger/__init__.py`)
4. V2-16f
5. V2-14b (after V2-15, V2-16s and V2-16f)
6. V2-14a (after V2-16s: `main.py`/`config.py`)
7. V2-10's restore post-step (after V2-14a and V2-15; the coordinator's chunk)
8. V2-16 (after V2-14b)
9. V2-17 (after V2-42)
10. V2-18
11. V2-19 (after V2-08)
12. V2-20
13. V2-24
14. V2-26
15. V2-28
16. V2-29
17. V2-30
18. V2-31
19. V2-37 (after V2-08 for `owner_google_login.py`)
20. V2-38

V2-25 is independent and merges at any time.

**Frontend.** Every release contains `91afca6` and the agreed V2-07/V2-09/V2-42-fe chain.
1. V2-21. It is released **before** any backend emits v2, so it is inert under v1.
2. V2-23.
3. V2-22.
4. V2-32.
5. V2-33.
6. V2-27.
7. V2-39.

Serial FE chunks may ship as one release.

**Production, waves 5-6** (the FORK-PROD-ORDER order; every step pauses jobs and takes a verified
backup first):
1. A default-off backend deploy of the integrated SHA (including V2-10's restore post-step).
2. The V2-14a demote: `stage` → `activate` → recreate → smoke → `enforce`.
3. `mia os-ledger migrate` (V2-14 + 14b). It creates `assaf_os`, owned by `assaf_os_owner`.
4. V2-15 provision (`provision_ledger --target <prod pin> --apply`, which writes the ledger DSN to
   `runtime.env`), then the validator. Provision needs the schema and the tables to exist.
5. `mia os-authority init --approved-sha <sha>`: `ensure_workspace` at epoch 1 and the pepper pin.
6. Rehearsal (V2-35a, V2-35), including one `mia restore` drill on the rehearsal copy.
7. Wave 6 (V2-36): one go per step, then one feature release per deploy, in this order:
   - one-tap (V2-24 + V2-32);
   - fast tasks (V2-33);
   - statements (V2-26/27);
   - assistant (V2-37/38/39).

## 20. Tests-first and gates per chunk

**G-BE** (every backend chunk):
- red→green evidence in the PR;
- `MIA_ENV=test uv run pytest --basetemp=.cache/<chunk>` (full suite, output to a file);
- `uv run ruff check app tests`;
- the ledger PG job green with zero skips;
- a fresh reviewer who is not the author;
- CI green on the exact head: `checks`, `postgres`, `container`, `os-ledger-postgres`,
  `os-ledger-migration-postgres`, `review-gate`.

**HEAVY review** is required whenever I4, I5, I10 or I11 is touched.

**G-FE** (every frontend chunk):
- `npm test`;
- Chromium + WebKit E2E (LOCAL_TESTED until V2-23 lands);
- two green CI runs before release;
- a fresh review;
- a FREEZE OK for any visual change.

**Per chunk:**
- **V2-16s** (G-BE):
  - zero behaviour change, with v1 byte-identical;
  - the empty routers (including `owner_step_up.py`) expose zero routes;
  - the config subtraction tests;
  - a bad value for every §3.3 field coerces to the default and never fails the settings load;
  - non-GET caps, duplicate keys and content type on PUT/POST;
  - the `api.py` parity test; `since_token`/`parse_since` round-trip; `core_authority_state` returns
    None with no DSN.
- **V2-16f** (G-BE): the §6 list, against the fake; the fake's invariant assertions; the nested-lock
  assertion; the PG sentinel.
- **V2-15 r2** (G-BE + HEAVY security):
  - the exact map passes, and every extra privilege fails;
  - an owner edit of an imported row (UPDATE of `source` and the typed columns) succeeds as the
    runtime role on every record table;
  - `--mode reapply` is idempotent, keeps existing passwords, re-owns `assaf_os`, and validates;
  - `migration_role=None` derives exactly one ADMIN-only member, and zero or two are refused;
  - the production target pin; the dry-run prints the exact GRANTs;
  - the three existing validators are unchanged.
- **V2-14b** (G-BE + HEAVY migration review): the §4 list; the extended grant map, including the
  retention role and its backstop DELETE; the CI line and its pin.
- **V2-14a** (G-BE + HEAVY security):
  - posture fails on superuser, membership, database ownership or any `assaf_os` access, and passes
    with `mia_app` owning `public`;
  - `mia-migrate` succeeds as `mia_app` on a DB that has pending ALTER and CREATE migrations;
  - `reapply` after a `--no-owner --no-privileges` restore restores ownership and grants;
  - the dry-run SQL is exact; `runtime.env` is the only env file written; the rollback is documented.
- **V2-10 restore post-step** (G-BE + HEAVY): the restore drill of §18.1 is green; services stay
  down when any post-step fails; the safety dump excludes the batch-line data.
- **V2-16** (G-BE + HEAVY): the ported prototype tests and the conformance suite on PG; guard refusals;
  the authority transitions.
- **V2-17** (G-BE + HEAVY authority):
  - the factory matrix;
  - v1 byte-identical under sheets;
  - the Sheets write refused while `core` is active, using only `core_authority_state`;
  - the Sheets write proceeds with no DSN and with no workspace row; a DB error gives 503;
  - the ledger write refused under sheets;
  - the grep test.
- **V2-18** (G-BE + HEAVY financial):
  - the property equivalences;
  - the verifier catches tampering;
  - cursor invalidation;
  - reads never write;
  - budgets recorded.
- **V2-19** (G-BE + HEAVY security):
  - every matrix cell;
  - the AST and enumeration tests;
  - the kill switch blocks the owner tool loop;
  - the Google scope set is preserved;
  - `Authorization` on owner routes gives 401.
- **V2-20** (G-BE + HEAVY): goldens validate against a Python port of the strict readers; v1
  byte-identical; follow_up handles v1, v2 and mixed (refused).
- **V2-21** (G-FE + HEAVY):
  - every fixture parses;
  - one extra key, or an unknown actor, fails;
  - the Worker and BFF tables are equal;
  - the hash manifest;
  - coverage stays complete across pages.
- **V2-22** (G-FE): at most 2 boot calls; unchanged polls render only the mirror chip; a propose
  elsewhere shows up on the next poll; labels; the stale-version guard; v1 unchanged.
- **V2-23** (G-FE): a hash mismatch fails CI; E2E runs on the PG harness; controls are loaded from
  `controls/`.
- **V2-24** (G-BE + HEAVY): the §11.1 paths on the fake, then on PG; a rollback injection; one outbox
  row per commit and per undo; non-OS channels refused.
- **V2-25** (coordinator's tests_first, unchanged).
- **V2-26** (G-BE + HEAVY financial):
  - stale digest gives `review_stale`;
  - all-or-nothing approval;
  - MIA and ASSISTANT can't approve;
  - unassigned lines block approval;
  - personal detail is purged and absent from every other table and from a test dump;
  - `expire_due` finishes and purges overdue batches under the kill switch, `backend=sheets`,
    `statements=fenced` and a disabled ledger; line-less batches finish as `restored_without_detail`;
  - a revoked key's batch can never commit; `revoke_pending_for_credential` runs inside the caller's
    transaction;
  - supersede vs conflict, including an older batch with a saved draft;
  - personal rule ids are HMAC-derived, never the sha256 id;
  - a missing or mismatched pepper fingerprint refuses every statement write;
  - draft deltas: more than 100 corrections across several POSTs;
  - over-cap bodies are refused;
  - null for a missing cycle.
- **V2-27** (G-FE + HEAVY approval UI + FREEZE): escaping and `bdi`; approve disabled while blocking;
  a small draft POST with no storage; the digest is sent; a 300-line review stays responsive.
- **V2-28** (G-BE + HEAVY approvals and contact):
  - one receipt from a concurrent browser + Telegram approve;
  - crash recovery with no new id;
  - a duplicate callback writes once;
  - a duplicate lead writes nothing;
  - card ≤3,800 chars.
- **V2-29** (G-BE + HEAVY external writes):
  - a lost ack converges on readback;
  - a crash between claim and finish: the lease is reclaimed after `lease_ttl` and delivered once;
  - `verified_version` never moves backwards under out-of-order finishes;
  - `conflict` clears only through a full rewrite and an equal readback;
  - RAW formulas;
  - no transaction is open during a transport call (instrumented);
  - kill-switch pause;
  - no personal lines;
  - the allow-list subtraction.
- **V2-30** (G-BE + HEAVY migration):
  - the synthetic workbook (refund chain, two currencies, Hebrew enums, an unaccepted edit, a hidden
    journal);
  - quarantine blocks promotion;
  - promotion is idempotent within an epoch, and the same workbook promotes again at the next epoch;
  - manifest revisions are written as-is; orphans refuse unless listed under `cancellations`;
  - the xlsx bomb and DOCTYPE are refused;
  - the diff shows one cell.
- **V2-31** (G-BE + node + HEAVY):
  - each precondition refuses, including `sheets_not_fenced`, `freeze_off` and
    `capture_before_fence`;
  - `init` creates epoch 1 and is idempotent; `activate` never bumps; the `fenced→shadow` bump is
    idempotent;
  - rollback before the first write;
  - the fence harness for both modes.
- **V2-32** (G-FE + HEAVY): the row changes only after the receipt; no auto re-POST; resend uses the
  same id and body; the abandon path; the server-clock countdown; the v1 marker is recovered; no
  `style=`.
- **V2-33** (G-FE + HEAVY write calls + FREEZE): every action goes through commit; one bulk request
  with one undo; inline edits survive a refresh; the `⋯` path works by keyboard and screen reader.
- **V2-37** (G-BE + HEAVY security):
  - cookie and bearer cross-refusals;
  - a uniform 401 counted only for invalid keys;
  - scopes are fixed;
  - no decision ids;
  - route enumeration;
  - revoke auto-rejects inside the same transaction;
  - limits, including the in-MIA per-IP limit; the Caddyfile is unchanged;
  - audit carries no payload;
  - kill switch;
  - step-up required.
- **V2-38** (G-BE + HEAVY security): exactly 3 tools; propose-only; no decision ids; unauthenticated
  calls refused; the schema equals `STATEMENT_BATCH_FORMAT.md`.
- **V2-39** (G-FE + HEAVY secret display): the secret is absent from storage and from the DOM after
  dismiss; the server-only revoke update; escaped labels; the step-up path.

## 21. Rejected challenges (adversarial review r1)

Every refutation and missing item was spot-checked against the code and applied above, except these
parts:

1. **"Send V2-15 back for r2: retention LOGIN with the §18.2 grants."** Rejected in part. Under the
   new merge order (V2-14 → V2-15 → V2-14b), retention's tables (`os_statement_batch_lines`,
   `os_assistant_audit`) do not exist when V2-15 merges. V2-15's reserved NOLOGIN, zero-privilege
   posture is therefore correct for its table set. V2-14b turns retention into a LOGIN role with the
   §18.2 map when it adds the tables. V2-15 still goes back for r2 on the record-table UPDATE columns
   (the real defect) and on `reapply`.
2. **"Bump `pending_seq` on `save_draft`."** Rejected. A draft save changes nothing in the `GET ""`
   body (page, summary, `pending_count`, mirror). Bumping it would only force full responses.
   `insert_proposal` (so also batch submit) and `finish` bump it.
3. **"Rebase V2-14b onto `783e6bf`."** Superseded. V2-14 has since merged (`12254fa`), and V2-14b now
   starts on V2-15's head and rebases onto V2-15's merge.
4. **"Map SQLSTATE class 23 to `ledger_integrity_failed` or a definite 409."** The 409 option was not
   taken. Class 23 maps to `ledger_integrity_failed` (503, non-retryable). Client-reachable
   constraints are pre-checked to their business codes (§5.3), so a class-23 error always means a bug,
   never an owner-correctable conflict.

## 22. Errata after freeze (coordinator decisions; binding)

### E1 (2026-10-16): importer staging and orphan enumeration via typed `LedgerTx` methods (V2-30 ↔ §5.1)

**Gap.** §15 has `stage` writing `os_import_batches`/`os_import_rows` and `promote` detecting orphans.
But the frozen `LedgerTx` (§5.1) had no method for either, and `tx.connection` is barred from the V2-14
core tables. The §18.2 grant map already gives the runtime role INSERT and the listed UPDATE columns on
both import tables.

**Decision: option (b).** These are typed methods, implemented identically by the FakeLedger (V2-16f)
and PG (V2-16), and covered by the conformance suite. `tx.connection` stays barred from core and
import tables.

**V2-16s adds these to `LedgerTx` in `app/services/os_ledger/api.py`:**
- `insert_import_batch(*, batch_id, authority_epoch, source_digest, source_binding, manifest_digest=None) -> None`
- `insert_import_rows(batch_id, rows: Sequence[ImportRow]) -> int`
- `set_import_status(batch_id, status, *, manifest_digest=None) -> None`
- `live_ids(entity) -> frozenset[str]`

**Rules:**
- **Who may call them:**
  - the three import writers only in a transaction of kind `import`, with `core=shadow`;
  - `live_ids` in kind `import` and in reads only;
  - any other use raises `invalid_transaction_kind`. If V2-16s has no such code, add it to §5.2 as 409, non-retryable.
- **Immutability:** a batch is immutable once `promoted`, and import rows are insert-only. Status
  changes use only the §18.2 UPDATE columns.
- **Status transitions:** `staged → manifest_signed → promoted`, or `staged|manifest_signed → abandoned`.
  Anything else raises `import_status_invalid`.
- **`live_ids(entity)`:**
  - returns non-deleted, non-cancelled ids for this workspace's active epoch;
  - is capped at 100,000 ids per entity, above which it raises `local_record_limit`;
  - `entity` must be one of the 7 record entities, else `invalid_entity`.
- **Logging:** no row payloads in logs; reason codes and counts only.
- **Conformance suite (V2-16f)** must cover, against the fake now and against PG via V2-16:
  - each method's happy path;
  - the kind guard;
  - each illegal status transition;
  - insert-only rows;
  - the `live_ids` cap and the deleted/cancelled exclusion.

**Owners:**
- V2-16s: signatures, and these errata codes in §5.2.
- V2-16f: the fake plus conformance.
- V2-16: the PG implementation (executor's Codex lane).
- V2-30 phase B builds against these methods.

### E2 (2026-10-16): V2-14b may extend the V2-14 migration allow-list assertions (file ownership, §3.1)

V2-14b adds its three `20261016_*` files to `os_ledger_migrate.MIGRATIONS`. The merged V2-14 test
`tests/db/test_assaf_os_ledger_migration.py` hard-codes the single-file allow-list
(`MIGRATIONS == (MIGRATION,)`, plus the applied/already/journal assertions).

**Decision.** V2-14b may edit ONLY those assertions in that file, extending them to the exact
four-file allow-list in order. They stay exact equalities, and nothing may be loosened. Every other line
of the file stays untouched. No other open branch edits that file (checked by the coordinator,
2026-10-16).

### E3 (2026-10-16): authoring while Codex is out of quota (until 2026-10-22 09:13)

Owner decision (Assaf, in the coordinator session):
- **Low-risk** Claude-built chunks merge on an independent fresh Claude opus review, marked on the PR as
  the outage exception. Low-risk means pure logic, docs, UI, and read-only tooling.
- **Risky** chunks are built to review-ready and wait for a GPT cross-check. Risky means auth or sessions,
  DB roles or grants, migrations, approvals or commit/undo, and the authority fence or cutover.
- Codex-authored chunks keep a Claude reviewer, as before.
- Where a Codex author lane is dead, a Claude author may continue from the partial tree. The PR's
  `Author-Agent` is then `claude`, and the reviewer becomes Codex when it returns.

### E1-rev (2026-10-16): E1 revised to fit the merged V2-14 import schema. It supersedes E1 where they differ.

**Problem.** The builders reported that E1 contradicted the merged `20261015_assaf_os_ledger.sql`:
- E1's batch statuses do not match the CHECK on `os_import_batches.status`;
- there is no `manifest_digest` column;
- the §18.2 UPDATE grant has no column for it;
- `ImportRow` was undefined.

**Decision.** Fit E1 to the merged schema. There is no schema change and no change to V2-14b's scope.

**Batch statuses.** Use V2-14's exact vocabulary: `staged`, `validated`, `imported`, `quarantined`, `failed`.
- **Transitions.** Anything else raises `import_status_invalid`.
  - `staged → validated`: the manifest was computed, and no row is quarantined.
  - `staged → quarantined`: at least one row is quarantined. This is terminal for the batch: the owner fixes the source and re-stages, which creates a new batch because of the epoch-scoped unique key.
  - `validated → imported`: only inside `promote`'s import transaction.
  - `staged | validated → failed`: abandon.
- **`set_import_status`** sets `completed_at` exactly when it enters `imported`, `quarantined` or `failed`, matching the V2-14 CHECK. It requires a `quarantine` object when it enters `quarantined`.

**No `manifest_digest` column or parameter.** As §15 already says, the approved manifest digest is passed to `promote`. `promote` recomputes the manifest and refuses `manifest_digest_mismatch`, and the digest is bound in the receipt digest `{"import": manifest_digest, "epoch": epoch}`. The owner's sign-off evidence lives in the V2-31 cutover record.

**Signatures.**
- `insert_import_batch(*, batch_id, authority_epoch, source_digest, source_binding, provenance: Mapping) -> None`
- `insert_import_rows(batch_id, rows: Sequence[ImportRow]) -> int`
- `set_import_status(batch_id, status, *, quarantine: Mapping | None = None) -> None`
- `live_ids(entity) -> frozenset[str]`, unchanged from E1.

**`ImportRow`.** Accepted as the builders defined it from the `os_import_rows` insert columns:
- `row_number`
- `entity`
- `record_id | None`
- `source_digest`
- `status ("staged" | "quarantined")`
- `raw_record`
- `canonical_record`
- `provenance`
- `quarantine`

Rows stay insert-only through this API. The §18.2 UPDATE grant on `os_import_rows` is unused by this path.

**Error codes.**
- The new codes `invalid_transaction_kind` and `import_status_invalid` are 409 and non-retryable.
- `invalid_entity` and `local_record_limit` keep any status §5.2 already gives them. Otherwise they are 422 and 409 respectively, both non-retryable.

Everything else in E1 is unchanged: the kind guard, the `live_ids` cap and exclusions, conformance coverage, owners, and `tx.connection` still barred.

### E4 (2026-10-16): V2-26 obligations from the V2-25 round-3 review (nits carried forward)

These were found by the Claude opus review of V2-25 at `d1b4354`. V2-26 must implement them:
1. **Pass in linked hand entries.** §12.2 step 2 now reads "the candidate manual expenses, **including every
   hand entry linked to a line of this statement, with `linked_line_key` set**." If they are left out,
   the V2-25 stale-link check (`_stale_links`) is inert and a changed hand entry is silently kept linked.
2. **Drop stale choices before recomputing.** Before each recompute (review and approve), drop any
   draft merge choice that has become invalid, for example a "keep" for a link whose hand entry fits
   again, or a chosen candidate that was cancelled. Otherwise the batch is stuck on
   `merge_choice_invalid` until reject or expiry.
3. **Shared constant.** Export `LINKED_EXPENSE_CHANGED` from `app/domain/statements/__init__.py`. It is
   additive, and V2-26/27 use the constant instead of hard-coding the string.
4. **Compact JSON from the assistant.** `context_for_assistant` tells the assistant to emit compact
   UTF-8 JSON (`ensure_ascii=False`, separators `(",", ":")`). With default escaping, 300 lines with
   long Hebrew merchant names exceed the 128 KiB cap.
5. **Optional: a better candidate for a stale link.** For a stale link, also offer any free hand entry
   that fits (`match_manual` over the pool), and plan unlink plus link if the owner chooses it. If this
   is skipped, record it as a known limitation.

### E5 (2026-10-16): V2-16s may merge before V2-15 (merge order, §2/§19)

**Why.** §19 put V2-15 before V2-16s only because V2-15 creates `app/services/os_ledger/__init__.py`.
V2-15 is RISKY (roles and grants), so under E3 it waits for the GPT cross-check (Codex returns
2026-10-22). Holding V2-16s behind it would also stall V2-16f and every builder merge after it.

**Decision.** V2-16s (low-risk: default-off scaffold) may merge as soon as its delta re-review passes
and CI is green, independent of V2-15. Conditions:
- V2-16s creates `app/services/os_ledger/__init__.py` with exactly the one-line content that V2-15
  ships (`"""Inactive PostgreSQL ledger integration contracts."""` plus a newline). The files are then
  byte-identical, and V2-15's rebase onto master is clean (an identical add/add).
- V2-16f may then merge after V2-16s, as before.
- V2-14b's merge-after list (V2-15, V2-16s, V2-16f) is unchanged. V2-16 still merges after V2-14b.
- Every other §19 constraint is unchanged. In particular, no dependent merges before V2-16.

### E6 (2026-10-16): retention row predicates are enforced by the job, not by the database (V2-14b fork)

V2-14b gives `assaf_os_retention` LOGIN plus exactly its purge and backstop DELETE grants (§18.2). PostgreSQL
grants are table-level, so the *row* predicates are not enforced by the database. "Only purge personal
statement detail past retention" and "only the backstop rows" are enforced only by the `os-retention` job's
SQL.

**Decision (recommended option, executor 2026-10-16, recorded by the coordinator).** No RLS. Adding RLS
would be a contract change across V2-14b, V2-15 and V2-16. Instead:
- the retention role's credentials stay VM-owned and are used only by the `os-retention` job;
- the job's DELETE statements are fixed, parameter-bound SQL with the retention predicates;
- V2-14b/V2-26 tests assert that the job deletes nothing outside its predicates;
- the threat model in the PR documents the gap.

**Revisit trigger:** any second user of the retention role, or any path that gives it ad-hoc SQL, means RLS
becomes mandatory first.

### E7 (2026-10-16): V2-16f review-r1 decisions, pinned by the conformance suite. V2-16 (PG) must match.

**(a) `live_ids(entity)` exclusion** (supersedes E1/E1-rev's "non-deleted, non-cancelled"). It excludes only
rows that are tombstoned, or already in their FORK-DELETE end state:
- tasks: tombstoned only. Done (`בוצע`) and `בוטל` tasks are INCLUDED, because FORK-DELETE for a task
  is a tombstone.
- payments: `kind = בוטל`.
- expenses: `payment_status = בוטל`.
- works: `status = בוטלה`.
- time: `status = בוטל`.
- clients: `status = לא פעיל`.
- leads: `status = לא רלוונטי`.

Rationale: an orphan that still needs a cancellation is a PG row that the Sheets manifest lacks and that
is not yet in its end state.

`is_live` stays unchanged for dependents. To prevent drift between the fake and PG, the end-state map is
defined ONCE as an additive constant beside the FORK-DELETE dependency table, which both the FakeLedger
and PG import. Neither side keeps a private copy.

**(b)** `insert_import_batch` with `authority_epoch != ctx.authority_epoch` refuses `authority_changed` (I12).

**(c)** `find_expenses` combines its filters as a UNION (ids ∪ the manual date window), because V2-26/E4
needs the linked hand entries plus the window candidates. The semantics are pinned in conformance and
named in the docstring.

**(d)** `pending()` lists only proposals at the context's authority epoch (I12).

Already decided by the contract text, so no erratum:
- expire-scope finish of a zero-line batch, with `restored_without_detail`;
- a fused kind (`owner_direct`, `owner_undo`, `import`) never finishes under its `rejection_id`. Only `proposal` and `statement_batch` expiries (from `expire_due`, or on `epoch_changed`) are keyed by `rejection_id` (wording fixed after the V2-16f r2 review; the fake and conformance enforce it);
- `insert_proposal` checks `draft.kind` == the transaction kind;
- the PG fixture preconditions from V2-15 r2.

**Restored-batch caveat (V2-16f r2).** PG admits the not-overdue expiry branch only when `reason_codes == ['restored_without_detail']` AND the batch has zero `os_statement_batch_lines` rows. The fake checks only the code (a fake-only test pins the no-code refusal), so V2-26 must test its zero-lines decision against both the fake and PG.

### E8 (2026-10-16): retention credential and CONNECT (V2-14b forks; obligations for V2-31 and V2-26)

1. **Retention credential.** `assaf_os_retention` is LOGIN, but provisioning sets no password, and V2-15
   r2's runtime.env staging covers only the runtime role. **V2-31's owner-approved cutover runbook**
   provisions the retention password and a job-only env file carrying
   `MIA_OS_LEDGER_RETENTION_DATABASE_URL`, the same way V2-15 r2 does for the runtime role:
   - VM-owned;
   - never in `mia-env`;
   - already covered by the `MIA_OS_LEDGER_\w+` owned-regex from V2-10.
   V2-14b itself does nothing here.
2. **CONNECT.** Retention relies on PUBLIC CONNECT on DB `mia` today. If production ever revokes PUBLIC
   CONNECT, a §18.2 erratum grants retention exactly CONNECT, mirroring the runtime role. No change now.
3. **V2-26** executes exactly `role_policy.RETENTION_DELETE_SQL`, the fixed statements tested by E6.
   If it needs different statements, it moves them into its own module and re-points the E6 test there,
   with the same outside-predicate and second-run-deletes-0 assertions.
4. `LEDGER_ROLE.md` (V2-15) is updated to "retention is LOGIN" when #170 and #168 merge.

### E9 (2026-10-16): V2-29 may edit `allowed_sheets_spreadsheet_ids()` in `app/core/config.py` (ownership, §3.1)

V2-29 MIRROR (now built by the executor session) may edit ONLY the method
`allowed_sheets_spreadsheet_ids()` in `app/core/config.py`.
- **Always** subtract `os_mirror_spreadsheet_id`. The mirror is never a Mia-proposable write target
  (V2-00 review note on #108).
- **Subtract `assaf_os_workbook_id` once T1 has started.** The sync-safe Settings signal is the §3.3
  fields already on master through V2-16s: subtract it when `assaf_os_freeze` is True OR
  `assaf_os_backend == "postgres"`. A coerced or invalid value must fail toward subtracting, never
  toward allowing.
- A test pins both exclusions, plus the fail-safe direction.
- V2-08 (#163) edits a different region of `config.py`. Whichever merges second rebases.

### E10 (2026-10-16): narrow the `mia` owned-regex. §18.1's `MIA_OWNER_OS_\w+` was wrong.

**Bug (found by the V2-10 builder).** `ops/assaf-os/provision_auth.py` writes live Owner OS keys into
`mia-env`:
- `MIA_OWNER_OS_ENABLED`;
- `*_GOOGLE_*`;
- `*_SESSION_SECRET`;
- `*_SESSION_DATABASE_URL`;
- `*_ORIGIN`;
- others.

Adding `MIA_OWNER_OS_\w+` to `mia`'s `owned` regex would strip all of them from `settings.env` at the
next `mia deploy` or `mia settings`. Owner OS would go down, failing closed.

**Decision.** The owned-regex additions are exactly `MIA_OWNER_OS_LEDGER_\w+|MIA_OS_LEDGER_\w+|MIA_APP_ROLE_POSTURE`.
These are the VM-owned ledger DSNs (runtime, migration and retention) and the posture. Keys in the
`MIA_OWNER_OS_*` session, auth and Google family stay in `mia-env`. A test pins that
`MIA_OWNER_OS_ENABLED` and `MIA_OWNER_OS_SESSION_DATABASE_URL` survive the filter.

**Related V2-10 decisions:**
- The ledger-reapply container mounts `/etc/mia` **read-only**. Roles are cluster-global and survive a
  restore, so a missing runtime role during a restore is an anomaly: abort with `ledger_reapply_failed`
  and provision explicitly, rather than letting a root container write VM secrets.
- Restore step 4 also sets `revoked_at` on open sessions, which is stronger than `revoked_before` alone.
  Accepted.
- Restore step 3 runs the real `_validate_main_role_catalog` as an extra abort condition. Accepted.
- The approval for the ledger reapply is the running image's own `MIA_BUILD_SHA`; the operator running
  `mia restore` is the approval. Accepted.

### E11 (2026-10-16): consequences of E9 for V2-29, plus a CI-line ownership extension

1. **Flipped scaffold assertion (consequence of E9).** V2-29 flips ONE assertion in V2-16s's
   `tests/unit/test_os_v2_scaffold.py`. It pinned "an invalid backend keeps the OS workbook allowed". E9
   requires the opposite: a coerced or invalid value fails toward subtracting. This is a correction to
   E9's rule, not a weakening. No other assertion in that file may change.
2. **CI line.** V2-29's PG outbox cases in `tests/workers/test_os_mirror_outbox.py` run only when
   `MIA_OS_LEDGER_TEST_POSTGRES_URL` is set, and no CI job includes `tests/workers`. V2-29 may edit ONLY
   the pytest path list of the `os-ledger-migration-postgres` job in `.github/workflows/ci.yml`, adding
   `tests/workers/test_os_mirror_outbox.py`, so the test and its CI coverage land atomically. It must not
   change any other line of `ci.yml`. If V2-14b's widened line merges first, rebase onto it.
3. **Lazy production wiring.** V2-29's production wiring resolves lazily against:
   - V2-15: `owner_os_ledger_database_url`;
   - V2-16: `api.build_ledger`;
   - V2-19: `authorize.for_worker('mirror')`.
   Until then it defers with `ledger_dsn_missing` or `ledger_unavailable`. Accepted.
4. **Known limits, accepted and recorded for V2-31 and V2-35a.**
   - The 8 mirror tabs must already exist in the new mirror workbook, because there is no `addSheet`. The
     V2-35a guide and V2-31's runbook must create them.
   - Reference columns show raw ids. Rendering labels is a follow-up.
   - Anti-entropy runs per process: one publisher thread per process.

### E12 (2026-10-17): V2-21 rulings. v2 decide dispatch ownership, plus three wire-shape strictness rules. Binding for the V2-20 and V2-37 goldens.

**v1 cold load (coordinator ruling, applied in the V2-21 fix round).** V2-21 adds no `modulepreload` and
no CSS link. v2 modules load only through a dynamic `import()` after a v2 schema. A test pins that the v1
cold-load request list equals v17's.

**The three admitted v1 changes are accepted.** They are §10-required and invisible, and the PR declares
them:
- `write_outcome` on a refused decide;
- `invalid_query` for a query v1 never sends;
- the BFF matching the Worker.

**1. The v2 decide dispatch is owned by V2-21b, a follow-up to V2-21.** This is the os-app approve/reject
path for generic proposals reading **v2 receipts**. §3.2 gives V2-21 os-app's dual-reader dispatch, and the
decide path is part of it.
- It is a hard prerequisite for the backend emitting v2: without it, the v1 decide reader refuses v2
  receipts after the flip.
- The builders session builds it next after the V2-21 fix round, before V2-23.
- Keep it in a separate module, with a minimal os-app hook. It rebases onto V2-07 and V2-09 when they ship.

**2. Wire-shape strictness. The strict readers and the goldens follow exactly this:**
- **(a) Binding everywhere.** Every owner-facing v2 JSON body carries the §10.1 binding, including the
  §17.1 credential bodies (the list, and the item plus the one-time secret). It's one uniform rule, so a
  body from another workspace can never be accepted. V2-37's goldens include it.
- **(b) `observed_version`.** It is REQUIRED on every body that reflects a ledger write: `receipt_v2`,
  `batch_created` and `draft_saved`. It is ABSENT from the credential bodies, because credentials are not
  versioned business state, and the strict reader rejects it there.
- **(c) Proposal-request lookup.** `reason_codes` is `["proposal_request_unknown"]` when unknown and `[]`
  when found, exactly as V2-21 does.

### E13 (2026-10-17): V2-16 may adjust one V2-16s scaffold test

`tests/unit/test_os_v2_scaffold.py::test_lazy_entry_points_fail_closed_before_v2_16_lands` asserts
`implementation_missing`. That can only hold before `core.py` exists. V2-16 may:
- rename it to `..._when_the_implementation_is_missing(monkeypatch)`;
- monkeypatch `api.importlib.import_module` to raise `ImportError`, so the fail-closed path is still
  exercised.

The assertions stay unchanged, and no other line in that file changes. (V2-29's single E11 flip in the
same file is independent; whichever merges later rebases.)

### E14 (2026-10-17): V2-20 review-r1 decisions

1. **Read-only proposal-by-id.** `GET /proposals/{id}` currently runs inside a write transaction. It is
   refused under the kill switch and while core is fenced, but `pending()` is not.
   - `LedgerReader` gains a read-only `proposal(proposal_id) -> ProposalView | None`. It is additive to
     `api.py`, follows §8 isolation (REPEATABLE READ, READ ONLY, never writes), and is subject to the I12
     epoch filter like `pending()`.
   - **V2-18** adds the signature, the FakeLedger method, the PG implementation and a conformance case.
   - **V2-20** keeps its current path as a documented deviation and switches to `proposal()` when V2-18
     lands.
2. **Mia model view under postgres keeps v1 parity.** Under `assaf_os_backend=postgres`, the snapshot
   model view shows the first page of EVERY entity (all 7, not tasks only), plus the summary and the
   labels.
   - It is capped at 16,000 characters with a deterministic truncation marker.
   - Free text stays wrapped as `untrusted_text`.
   - Mia's overview must not shrink on the switch.
3. **Receipt `records` rule (clarified 2026-10-17).** On a COMMITTED receipt, `records` is REQUIRED non-empty for the kinds that always mutate records (`proposal`, `owner_direct`, `owner_undo`); for `statement_batch` and `import` it MAY be empty or non-empty (a §5.3 finish may carry applied expense records), and readers must accept both. Original wording follows for history: `records` is non-empty only for kinds that always mutate records
   (`proposal`, `owner_direct`, `owner_undo`). A committed `statement_batch` or `import` receipt may carry
   `records: []`. The V2-20 port and the V2-21 `readReceiptV2` must agree; the goldens include a committed
   statement_batch receipt.

### E15 (2026-10-17): owner decision. Close every chunk WITHOUT GPT (supersedes E3's "risky waits for GPT")

Assaf, directly in the coordinator session: "for now, we resume without the GPT. Try to close all tasks
without it."

**All chunks, including the RISKY ones,** now merge on an independent fresh Claude opus review: read-only,
never the builder agent, round 2 at the final SHA.

**HEAVY chunks** (DB roles and grants, migrations, auth/sessions, approvals/commit, fence/cutover) get the
full HEAVY brief:
- the contract sections;
- earlier review findings, verified as claims;
- re-running the gates;
- for the security chunks, adversarial probing.

**Merges.**
- The review-gate stays red on claude/claude PRs by design, and that is accepted.
- Merge on review PASS/PASS_WITH_NITS plus every other CI job green, in §19 order.

**Unchanged:**
- Production: each FORK-PROD-ORDER step and the cutover still need Assaf's explicit go.
- If GPT's quota returns before a chunk merges, a GPT review may be added, but nothing waits for it.

### E16 (2026-10-17): a decided proposal on the GET or replay path (V2-20/V2-21 open question)

`GET /proposals/{id}` and a replayed `POST /proposals` return the STORED `proposal_v2`, which may already
be decided (approved, committed, rejected, expired or target_changed). I1 requires this: a replay returns
the stored result. V2-21's pending-only `readProposalV2` would turn that into `invalid_response`.

**Decision.**
- The client reads creation responses (first POST) with the pending-only reader.
- The client reads the GET and replay recovery paths with `readProposalAny`, which accepts every
  `PROPOSAL_STATUSES` value, with the status-specific invariants:
  - BOTH decision ids (`approval_request_id`, `rejection_request_id`) are present in EVERY status (corrected 2026-10-17: §10.2 fixes one exact `proposal_v2` key set, and the client never mints a decision id). When pending, they are the ids to send. When decided, they are the receipt keys per §5.3;
  - `expires_at` is still required.
- A decided proposal on recovery is a valid, final state. The client shows it with its receipt and
  never re-POSTs.

**Owner: V2-21b** (the v2 decide dispatch), which owns exactly this path. V2-20's Python port keeps
`statuses=` explicit, and goldens for decided proposal reads are added there. That is V2-20 follow-up
nit 9 (owner_undo, import and decided proposal_request goldens), done at the V2-20 rebase before merge.

### E17 (2026-10-17): V2-24 may change the commits-router assertion in `tests/api/os_v2/test_os_v2_mount.py`

`test_v2_routers_mount_with_zero_routes` asserts `commits.router.routes == []`, so it fails by design once
§11.1's routes exist. V2-24 may:
- drop `commits` from the zero-route loop;
- pin EXACTLY its 4 routes instead (method, path), still unmounted.

Every other line of that file stays unchanged, including the "no /unified/commits|deleted path mounted on
app" check. V2-24 does not touch `app/main.py`.

**V2-24 interface needs, owned by V2-19:**
- `authorize.from_owner_mutation(principal, settings)` and `authorize.from_owner_read(principal, settings)`;
- `require(ctx, op)` for op in {`commit_direct`, `undo`, `abandon`, `read`};
- `assaf-os:direct` in `owner_sessions.KNOWN_SCOPES` and `ASSAF_OS_WRITE_SCOPES`.

Until these exist, V2-24's write routes fail closed with 503.

**Integrator duty (coordinator, at the V2-20 rebase):** register `tests/contract/shapes/v2_24.py` in the
exporter registry and regenerate `tests/contract/golden/fixtures.sha256`.

### E18 (2026-10-17): an owner-initiated "send again" for an uncertain decision with no receipt (V2-21b)

**Problem.** An uncertain decide can leave a v2 marker forever, for example an offline tap that never
reached the server:
- `GET /proposals/{id}` keeps returning `pending` (reads never write, §8);
- `receipt_not_found` persists.
The marker then blocks every v2 decision on that device.

**Decision.** After a READ confirms BOTH `receipt_not_found` for the stored decision id AND that the
proposal is still `pending`, the client may offer an explicit, owner-tapped "send again". It re-POSTs the
SAME server-minted decision id with the byte-identical stored body.
- It is never automatic (I1).
- The server's replay table makes it exactly-once: an equal digest returns the stored result, and a
  differing digest is refused.
- Past `expires_at`, the resend gets the definitive `expired` receipt, which clears the marker.
- "Abandon" (§11.2) stays available as the alternative.

**Tests pin:**
- no resend offer without both READ facts;
- same id and body;
- the equal-digest replay;
- never automatic.

This is the same pattern as the §20 V2-32 note "resend uses the same id and body". It applies to the v2
decide path now; v1 keeps its existing behaviour.

### E19 (2026-10-18): the v2 commit routes are mounted from V2-16s; the flag is the gate (corrects E17's premise)

`app.main` already includes `os_v2_commits_router` (V2-16s, `main.py:323`). Under FastAPI 0.141,
`app.router.routes` holds `_IncludedRouter` wrappers, so the old mount test's id and path checks were
vacuous. Once V2-24 deploys, its 4 routes exist in production.

**Decision: option (a).** The gate is `assaf_os_owner_direct_enabled` (default off), plus
`assaf_os_backend=postgres` (404 first), plus the 503 fail-closed until V2-19's authorize exists.

V2-24 fixes the mount test to walk the included routers through `app.openapi()` paths. It pins:
- that the 4 routes exist;
- that they return 404 while the flag is off or the backend is sheets;
- that the §3.3 coercion turns an invalid flag value into OFF.

E17's "still unmounted" wording is superseded.

### E20 (2026-10-18): the dependency rule applies to every path that updates into a FORK-DELETE end state

V2-24 found that an `op:update` landing a record in its FORK-DELETE end state (for example, cancelling a
work with a live payment) must meet `has_dependents`. The generic PROPOSAL apply path has the same gap.

**Decision.** The rule lives in the shared ledger apply (V2-16 core): any update into a FORK-DELETE end
state refuses `has_dependents`, whatever the kind (proposal, owner_direct or import). The FakeLedger gets
the same rule, with one conformance case per entity that has dependents.

**Owner:** the coordinator, as an author-side fix on #175 before it merges. V2-24's own guard stays as
defense in depth.

**FE note (V2-21b/V2-32).** A tombstone receipt row (task delete, `bulk_delete_done`, undo of a create or
restore) has the same shape as an update. The client must branch on the record `op`, never on the shape.

### E18a (2026-10-18): a refused resend keeps the marker

If an E18 "send again" is refused with `not_started` (for example 403 `kill_switch` or 401), that refusal
proves nothing about the FIRST POST, which stays unknown. So on a RESEND only, a `not_started` refusal
does NOT clear the marker. The client shows the message and runs `recover()`. That re-offers the resend
once both READ facts hold again; after a 401 it happens at the next sign-in, so there is no lock-out.

The marker still blocks every other decision until a read settles it, which keeps the module's own rule.
This is delivered as V2-21c, a small follow-up PR.

### E21 (2026-10-18): close loopback `trust` in the VM's PostgreSQL. Pre-existing, found in the V2-10 review.

**Problem.** The official postgres image runs `initdb` with no `--auth` flags. `pg_hba.conf` therefore
has `host all all 127.0.0.1/32 trust` and `::1/128 trust` ABOVE the line that `POSTGRES_HOST_AUTH_METHOD`
appends. The DB listens on loopback with host networking, so any process on the VM can connect as ANY role,
superuser `mia` included, without a password. That defeats the V2-14a, V2-15 and V2-14b role separation.

**Decision. V2-10 carries the fix**, with owner-gated activation like the rest of FORK-PROD-ORDER.
- **New clusters:** `ops/gcp/vm/bootstrap.sh` and `compose.yaml` set
  `POSTGRES_INITDB_ARGS="--auth-host=scram-sha-256 --auth-local=scram-sha-256"`. These two files get a
  narrow ownership extension for V2-10, limited to this setting.
- **The existing cluster:** a new idempotent `ops/gcp/vm/pg-hba-harden.sh` with modes
  `--check`, `--apply` and `--rollback`.
  - `--apply` backs up `pg_hba.conf`, rewrites every `trust` line to `scram-sha-256`, and reloads.
  - It first verifies that every role used by a live DSN has a scram password: `mia`, `mia_app` when
    present, `assaf_os_session`, `assaf_os_ledger` and `assaf_os_retention`. It aborts if any is missing,
    so the app never locks itself out.
  - `--rollback` restores the backup and reloads.
  - Tests use a disposable cluster.
- **Production order (§19):** this runs as step **1a**, after the deploy of the integrated SHA and BEFORE
  the V2-14a demote. It runs with jobs paused, a verified backup, Assaf's go, a `/health/ready` check
  after the reload, and rollback on failure.

### E22 (2026-10-18): `apply` uses a strict CAS, and `before` must equal the stored record (V2-18 review)

**Problem.** `apply`'s compare-and-set checks only `before.revision`, while V2-18's IncrementalSummaries takes the old
month and amount from the `before` content. If a Mutation carries the current revision but stale content, the core
accepts it. The old month then keeps counting the payment until the verifier flags `summary_drift`. This was
reproduced on fake and PG. No in-tree builder produces such a `before` today, but V2-26 and V2-30 would be exposed.

**Decision. V2-16 (#175) carries the fix**, together with E20, in the shared core apply AND in the FakeLedger:
- an update or delete whose `before` differs from the stored record in any field (the canonical record digest, not
  just the revision) refuses with `ledger_integrity_failed` and writes nothing;
- add one conformance case per write kind (proposal, owner_direct, import), red first, green on fake AND PG;
- the error is a `LedgerError`; no new error code.

### E23 (2026-10-18): V2-18 write and page budgets (§8): the raised caps are accepted, and core scoping is a tracked follow-up

On 20k records with PG at 0.5 CPU / 1 GB plus a concurrent pg_dump loop, V2-18 measures page p95 91 ms and
write p95 1.1 s. The §8 targets were 50 ms and 0.8 s. Without pg_dump the figures are 26 ms and 0.67 s, and the
old defaults took about 4 s per commit. The remaining cost is O(N) work in V2-16's core: `tx._apply_one` validates
over `snapshot_records()`, and `decide` digests the full snapshot.

**Decision (owner standing approval: the recommended option).**
- (a) The enforced §8 caps become **page p95 100 ms** and **write p95 1.2 s** under concurrent pg_dump. The V2-18
  benchmark script enforces them.
- (b) The follow-up chunk **V2-16b, "scope the core validate and digest paths"** (core.py and tx.py; no migration) must
  land BEFORE V2-32's one-tap UI ships. Its target is the original §8 numbers.
- (c) Index migrations are not pursued unless V2-16b's measurements show they are needed.

### E24 (2026-10-18): V2-19 owns the reads-router mount assertion (E19 pattern)

**Problem.** `tests/api/os_v2/test_os_v2_mount.py` asserts `reads.router.routes == []`. §9 makes V2-19 serve
`GET /pending` and `GET /proposal-requests/{request_id}`, so the assertion is false by design. Master's id check is
also vacuous under FastAPI 0.141 (`_IncludedRouter` wrappers), as E19 found.

**Decision.**
- V2-19 may change that test to pin `reads.router` to EXACTLY those two routes. It walks the mounted app with
  `app.openapi()` paths, per E19.
- The path assertions stay unchanged, and `commits` stays empty in V2-19.
- V2-19 merges before V2-24. V2-24's E19 version rebases on top and adds its commit routes.

**Also recorded.** At V2-20's rebase, `PostgresOsBackend` calls `authorize.require(ctx, op)` after each context
constructor, so the channel x operation matrix is enforced in one place. This joins the V2-20 rebase list.

### E25 (2026-10-18): Mia's overview under postgres includes the first /pending page (keeps E14.2 "must not shrink")

V2-20 round-2 review, finding 2: under postgres, Mia's overview carries only `pending_count`, while under sheets it
listed up to 200 pending proposals. **Decision (b):** the postgres overview adds the FIRST page of `/pending`.
- It stays inside the same 16,000-character cap, with round-robin truncation and `truncated=true` as today.
- Proposal input and the before/after values are wrapped as `untrusted_text`, like every other free text.
- No extra pages are walked.

V2-20 carries this. `_port` → V2-17's `get_os_backend` stays a rebase obligation for when #176 merges.

### E26 (2026-10-18): an apply-time `has_dependents` inside `decide` ends the proposal as `target_changed` (V2-16 review r1)

**Problem.** A pending `kind=proposal` that cancels a work passes approve when a live payment was added after it
was proposed: `dependency_digest` does not cover referrers. Apply then raises `has_dependents` (409, not_started),
which propagates out of `decide`. No receipt is written, and the proposal stays pending and listed by `pending()` forever.

**Decision.**
- In `core.decide` AND the FakeLedger, an apply-time `has_dependents` finishes the proposal as `target_changed`
  with reason code `has_dependents`. It writes the terminal receipt and event, and nothing else.
- Every other apply refusal keeps its current behaviour. `ledger_integrity_failed` is still re-raised (E22, §5.2).
- Add conformance on fake and PG, red first.
- **Carried by V2-16b**, together with E23's scoping. It must land before V2-21b or V2-28 consume decide outcomes in production.
- The V2-24 rebase must pre-check dependents for an undo that lands a record in its end state, mapped to
  `undo_conflict(dependent_exists)`.

### E23a (2026-10-20): V2-16b outcome on the §8 budgets

V2-16b measures write p95 **337 ms** under concurrent pg_dump, so the original 0.8 s §8 target is met again. Page p95
is **86-89 ms**: inside E23(a)'s enforced cap of 100 ms, but above the original 50 ms. **Decision:** this is not a fork
for Assaf. E23(a) stands, so the page cap stays 100 ms. The 50 ms page target is a post-cutover improvement that uses
E23(c) indexes or a read-path change, only if Assaf's phone timings show the page wait.

The V2-16b review also found that the V2-18 benchmark's `within_caps` could pass without the pg_dump load, without
limits, or when every dump failed. That is a MAJOR harness defect, pre-existing from V2-18. It is fixed in #187 before
merge, so the enforced numbers above are trustworthy.

### E27 (2026-10-20): typed date cells in the owner xlsx import (§15 gap found in the V2-30 review)

**Problem.** V2-30 quarantines any date-formatted numeric cell and any `t="d"` cell
(`date_serial_binding_unspecified`). If the owner's export types its dates, which Google Sheets usually does for date
columns, the importer cannot promote real data.

**Decision (standing approval; the recommended option).** V2-30b reads typed dates deterministically:
- **Serials.** A numeric cell whose number format is a date or datetime format, or a `t="d"` ISO cell, is a date. Serials use
  the 1900 system unless the workbook sets `date1904`. Excel's fictional 1900-02-29 is rejected (`unsafe_date_value`).
- **Date-only fields** (the contract's date columns) take the calendar date with no time zone conversion and map to `YYYY-MM-DD`.
  A fractional part on a date-only field is refused (`unsafe_date_value`).
- **Datetime fields** (for example the journal `accepted_at`) treat the serial as wall-clock time in **Asia/Jerusalem** and convert it
  to an ISO-8601 instant with the correct DST offset. Ambiguous or nonexistent local times are refused.
- **Out-of-range serials** (below 1 or above 2958465) are refused.
- **Strings** keep today's ISO-string path unchanged.
- **Tests:** one golden per rule. The V2-35 rehearsal checks it against a real owner export.

### E28 (2026-10-21): E21 hardens the TCP (`host`) lines only. The container-local socket stays `trust`.

The V2-10 review found that the PR sets `--auth-host=scram-sha-256` only and rewrites only `host*` lines. E21's text also
named `--auth-local`.

**Decision (standing approval).** The risk E21 closes is loopback TCP. With host networking, any process on the VM could
connect. The Unix socket lives inside the postgres container, and only a process already inside that container (or
`docker exec`, which needs root on the VM) can reach it. The mia CLI's admin operations rely on it.

**Rule.**
- `local` lines stay `trust`.
- `POSTGRES_INITDB_ARGS` is `--auth-host=scram-sha-256` only.
- `pg-hba-harden.sh` rewrites only `host*` lines.
- Its live-DSN preflight MUST include every DSN the app or the jobs read, including `MIA_OWNER_OS_READ_DATABASE_URL`
  and `MIA_OWNER_OS_ACTION_DATABASE_URL` (V2-10b).

### E29 (2026-10-23): the ledger keys turn on at T1 (drain), not T6 (V2-31b finding)

**Problem.** The os-import job (G24 stage at T4, G12 promote at T5) needs `MIA_ASSAF_OS_LEDGER_ENABLED=true` and
`MIA_ASSAF_OS_AUTHORITY_EPOCH=E`. Without them, `authorize.for_worker` refuses with `epoch_unset`, and ledger admission needs
`ledger_enabled` plus the epoch. But `freeze-plan --phase switch` (T6) was the first phase to print those keys, so the
cutover would stall at T4 with nothing written.

**Decision (option a).**
- `freeze-plan --phase drain` (T1) prints `MIA_ASSAF_OS_FREEZE=true`, `MIA_ASSAF_OS_LEDGER_ENABLED=true` and
  `MIA_ASSAF_OS_AUTHORITY_EPOCH=E`. The backend stays `sheets`.
- `--phase switch` (T6) flips only `MIA_ASSAF_OS_BACKEND=postgres`.
- The T1/T2 rollback prints the revert keys (freeze false, ledger false, epoch 0).
- A job-local env override (option b) is rejected, because it would weaken the epoch binding.

**Required proof (V2-31b review).**
- Every reader of the two keys is enumerated.
- Under backend=sheets with ledger_enabled=true and epoch=E:
  - the factory still returns the Sheets port;
  - v1 propose is frozen, and v1 decide still drains (I8);
  - the ledger core admits only kind=import while core is shadow;
  - Mia's overview, the Telegram cards and the mirror behave exactly as with the keys off.

### E30 (2026-10-24): os-import stays a mounted script, not a console script (V2-31b finding 5)

§3.1 listed os-import under `[project.scripts]`. V2-31b keeps it as `scripts/os_import.py`, run by the compose `os-import` service from
the deployed tag's read-only mounted scripts directory. A console entry point would need an image rebuild for every script
fix, and the script is operator-run only at T4/T5. **Accepted.** Follow-up: an `import_batch_status` LedgerTx reader, so a
re-staged batch prints its stored status instead of `validated_or_imported`.

### E31 (2026-10-24): one statement per account per cycle (V2-26 review #2)

The DB key is (account, charge_date), but §12.4 assumes one statement per account per cycle. Two charge dates in one cycle
would double-book the cycle. **Decision (a):**
- A new blocking code, `cycle_has_statement`, applies when another statement of the same account already exists in that
  cycle with a different charge_date. Approve stays disabled, and the review shows the reason.
- GET /statements lists each statement as its own row, never hiding one.

### E32 (2026-10-24): statement reads go through write admission, accepted for now (V2-26 review #3)

Statement reads currently use the write admission: they are refused under the kill switch, a fenced statements domain or a
non-active core, and they take the workspace FOR UPDATE lock. **Accepted until after the switch-over.** Follow-up: a
read-purpose admission without the row lock, and with reads allowed under the kill switch (I8 lists only writes).

Also recorded from the same review:
- expire_due writes the drafts registry and current_proposal_id inside its expire scope;
- an equal-digest replay of a decided batch returns 409 already_decided rather than the stored result (I1 deviation, safe);
- personal markers stay out of proposal mutations;
- a pending batch with neither draft nor detail is not listable without a new typed LedgerTx reader (follow-up).

### E33 (2026-10-24): the owner-direct marker carries `epoch` as its 7th key (V2-32 review, §11.2)

I12 requires the epoch to be bound into the per-request marker, so §11.2's marker gains `epoch`. The page-level
tombstone-row removal lives in V2-22's os-boot-v2.mjs and moves to V2-33.

### E31a (2026-10-24): `cycle_has_statement` joins the §5.2 error table

- **The code.** `cycle_has_statement` is HTTP 409, added to api.ERROR_CODES; the scaffold pin goes from 50 to 51.
- **What holds a cycle.** Another statement holds a cycle when it is imported, or when its current batch is pending, in its
  window, this epoch, with detail. Rejected, superseded or expired batches hold nothing.
- **Two pending statements.** Two pending statements in one cycle block each other until one is rejected.
- **V2-21 follow-up.** approveBatch requires `request_id` to equal the approval id, but a revoked-credential or epoch-change
  receipt is keyed by the rejection id. The screen recovers by reading, and a test pins that.

- **E31 wording, amended.** "Another statement already exists" means: another statement **holds** the cycle, in E31a's
  sense (imported, or its current batch still awaits the owner). The same account is compared by `identity_text`
  (NFKC plus casefold), never by the raw label, both in the guard and in the cycles() grouping.

### E34 (2026-10-24): V2-37's key modules are renamed to stay outside the `*credentials*`/`*secret*` guard (Assaf)

**The problem.** Assaf's global Claude permission rule denies reading and writing `**/*credentials*` and `**/*secret*`. The
contract's names for the assistant key store sit inside that deny, even though they never hold a secret: only the hash is
stored, and the plaintext key is shown once.

**The ruling.** On 2026-10-24 Assaf chose "Rename the files" over adding a path exception. His deny stays untouched.

**The new names:**

| Contract name | New name |
|---|---|
| `app/services/assistant_credentials.py` | `app/services/assistant_keys.py` |
| `app/api/os_v2/credentials.py` (the V2-16s stub, imported by `main.py` as `os_v2_credentials_router`) | `app/api/os_v2/owner_keys.py` |

**Mechanics:**
- Codex makes the `main.py` import change and removes the empty stub, because Claude sessions cannot open the old path.
- The router's mount path, route names and pins (E17/E19/E24) are unchanged by the rename. Only module paths change.
- Any other contract text naming the old modules reads as the new names.

### E35 (2026-10-24): the mirror publishes under the freeze only while core authority is `active` (§14; fixes a T7 deadlock)

**The defect.** `os_mirror.pause_reason` (`app/workers/os_mirror.py:93-94`) paused unconditionally on `assaf_os_freeze`.
Two things in the runbook require a verified mirror while frozen:
- **T6** keeps the freeze on.
- **T7**, and section 4's first-write boundary, require `mirror: verified`.

So the production cutover would stall at T7 with the OS frozen. Both review passes caught it during G09 readiness
(R21/R37), and the coordinator confirmed it in the code.

**The rule.** §14's "pauses on the kill switch and the freeze" now reads:
- **Kill switch:** the mirror pauses on it unconditionally.
- **Freeze:** the mirror pauses on it **unless core authority is `active`**.
  - It reads the authority once before any outbox, reader or Sheets call.
  - The states `shadow`, `fenced` and `restoring`, and any failed or absent authority read, keep the `os_frozen` pause.
    That is fail-closed, and it preserves I8 and I14 for rollback and restore.
- **While paused:** still no outbox claim, no reader call and no Sheets call, and no attempts are consumed.
- **The snapshot gate:** the existing `_PUBLISHING` check on the consistent snapshot stays as the second gate.

**Tests:**
- T7 seeds and verifies with the freeze on and core `active`.
- Freeze plus `restoring`, `fenced` or `shadow` pauses `os_frozen`, with zero calls.
- An authority-read error pauses `os_frozen`.
- The kill switch still pauses first.

### E35a (2026-10-24): the snapshot freeze gate and version-bound mirror status

Under the freeze, the consistent-snapshot gate also requires core `active` (the precheck and
the snapshot must both see `active`); with the freeze off, `_PUBLISHING` (shadow, active) is unchanged. The
`os-authority status` mirror probe reports `verified` only for the current version (stale verification after a
rollback or rearm is not shown as verified).
Pre-activation `mirror_not_verified` accepts an empty mirror or a clean last projection (synced, no error,
and `verified_version` equal to `projected_version`) even when behind the ledger, while T7 and section 4 step 1
use `status`, which requires verification at the current ledger version.

### E36 (2026-10-24): G09 is replaced by fixes, tests and a careful live run (Assaf's ruling)

**The ruling.** Assaf asked why a rehearsal machine is needed, was given the reasons and the options, and chose
**"Lighter: tests + careful live run"**. The isolated-copy rehearsal of `OS_REHEARSAL_PLAN.md` (V2-35, V2-35a) is
therefore **NOT_INCLUDED**. No throwaway VM, workbook copy or TEST Script is created.

**What G09 becomes, before wave 6:**
1. **The blockers it found are fixed and merged:**
   - E35, the mirror deadlock (WP1);
   - the drift checker also compares `date_1904`, sheet `state` and the sheet set and order (WP2).
2. **An automated cutover-sequence test in CI** proves T6→T7 under the freeze: mirror verified, zero drift. This is WP12, and
   its exact scope is set at design time.
3. **A restore drill on Assaf's PC** (`ops/gcp/backup/restore_drill.py`) runs fewer than 7 days before activation, because the
   preflight's `restore_drill_too_old` still applies.
4. **The live run** happens in a quiet window that Assaf names, with section 5's rollback ready before the first non-import
   receipt. Every G10-G17 gate keeps its explicit go.

**Assaf's limits.** RTO ≤ 2 h, RPO ≤ 15 min (R45). They are recorded for the restore drill and for any post-cutover restore.

**Other coordinator rulings, from the readiness matrix:**
- The ARCHIVE name is the contract literal `ARCHIVE – edits ignored`.
- T6 reads the epoch from `status` and from the v2 binding.
- Write smoke happens only after T7 and G16, as in section 4.
