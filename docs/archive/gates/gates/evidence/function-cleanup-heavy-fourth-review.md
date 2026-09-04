# Phase 1.5 clean-room HEAVY fourth outcome review

Date: 2026-08-28
Mode: independent read-only outcome review; only this evidence file was created
Verdict: **FAIL**

The complete mechanical gate is green and every reported inventory/size/complexity
number reconciles exactly. The outcome gate is not green: two P1 defects and one P2
defect remain. The current tests do not cover these transitions and therefore do not
override the direct real-store reproductions below. No P0 was found.

## Review basis and boundaries

The reviewer read, in order, `AGENTS.md`, `docs/PRODUCT.md`,
`docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `PLAN.md`, the root/Phase 1.5/final
review gates, all three historical failed HEAVY reviews, the current synthesis and
repair evidence, the three 164-row audit matrices, and current production, test,
migration, packaging, and diff surfaces. The `unlazy` skill supplied the four-pass
ledger discipline; the existing Phase 1.5 gate files were used as the ledger because
the review boundary forbade creating a separate `GATES.md`.

No `.env` file or secret value was inspected. No live provider/network call, AWS
mutation, deployment, production/test/migration/application edit, or destructive
command was made. Provider behavior in reproductions is local fake-boundary evidence.

## Blocking findings

### P1 — Sheets binding accepts a negated operation and a strict subset of the owner's literal payload

`app/tools/registries/owner_tools.py:790-805` treats the presence of an operation
word as authorization. It does not recognize negation or reject a turn that also names
the opposite operation. Therefore `Do not append. Update ...` authorizes
`sheets_append`.

`app/tools/registries/owner_tools.py:823-832` proves only that every model-produced
cell occurs at least as often in the owner's quoted literals. The comparison is one-way
(`owner_literals[cell] >= count`), so the model may omit an owner-specified value and
still pass. The write validator also permits a payload smaller than the bounded target
range (`app/integrations/sheets.py:220-237`), which is correct for generic Sheets access
but does not repair the missing owner-to-tool binding.

Real `LeadStore` + real owner registry + `FakeSheetsPort` reproduction, with an explicit
SQLite test DSN and numeric-owner principal:

```text
owner: Do not append. Update sheet-allowed at KPI!A1 with "x" in the Sheet
tool:  sheets_append(sheet-allowed, KPI!A1, [["x"]])
-> ok=True
-> ('append', 'sheet-allowed', 'KPI!A1', [['x']])

owner: Please update sheet-allowed at KPI!A1:B1 with "red" and "blue" in the Sheet
tool:  sheets_update(sheet-allowed, KPI!A1:B1, [["red"]])
-> ok=True
-> ('update', 'sheet-allowed', 'KPI!A1:B1', [['red']])
```

Exact combined output:

```text
{'sheets_negated_append_ok': True, 'sheets_subset_ok': True,
 'ops': [('append', 'sheet-allowed', 'KPI!A1', [['x']]),
         ('update', 'sheet-allowed', 'KPI!A1:B1', [['red']])]}
```

This violates the required exact operation/value-multiplicity boundary. The previous
repairs do correctly enforce complete spreadsheet/range tokens, duplicate minimum
multiplicity, allowlist, bounded A1 order/caps, shape, formula/empty rejection, kill
switch, principal policy, pre-port/pre-claim denial, RAW mode, and exact-event
idempotency; the remaining defect is the semantic binding above.

Required repair gate:

1. Reject an operation if it is negated or if the current turn gives conflicting
   operation instructions; do not let a prohibition authorize the prohibited tool.
2. Bind the complete owner-specified value payload, including multiplicity in both
   directions, to the exact tool payload. A model-produced strict subset or superset must
   fail before port construction and before an operation claim.
3. Add the two exact regressions above plus ambiguous two-operation/multiple-target tests,
   while retaining all current ordering, allowlist, A1, shape, formula, empty, kill,
   wrong-principal, corrected-replay, RAW, and successful-idempotency tests.

### P1 — The recipient-ledger migration reopens legacy completed notifications and can resend historical conversations

The additive migration explicitly has no backfill because the old lead/conversation
claim cannot identify a recipient (`migrations/20260828_owner_notification_recipient_claims.sql:3-6`).
That uncertainty should be handled conservatively as ambiguous delivery. Instead,
current finalization consults only the new per-recipient table
(`app/services/finalization.py:167-190`) and never checks the legacy durable
`owner_notification_claims` row. The inactivity scan likewise stopped using legacy
conversation claims (`app/agents/client/graph.py:281-287`).

Representative upgrade-state reproduction used the real current models and store:

```text
precondition:
  owner_notification_claims contains
  (web_final_v1, lead_legacy_done, web_legacy_done)
  owner_notification_recipient_claims is empty (the declared no-backfill state)

call:
  finalize_website_conversation(... same lead/conversation ..., owner 111 configured)

result:
{'legacy_claim_present': True,
 'replayed': {'claimed': True, 'sent': True, 'duplicate': False,
              'kind': 'web_final_v1'},
 'delivery_calls': [('111',)], 'new_recipient_claim': True}
```

On deployment, previously completed inactive conversations are therefore eligible for a
second owner Telegram card. Since the scan is bounded to 50 rows rather than to newly
created conversations, repeated scans can work through historical rows. This is a
migration compatibility failure even though the DDL, ordering, container inclusion, and
new-state conflict insert are mechanically sound.

Required repair gate:

1. Define a conservative compatibility rule for legacy claims. A legacy completed claim
   must not be treated as a known failure and silently reopened for every configured
   recipient.
2. Prove a pre-migration completed finalization does not resend after migration, while a
   genuinely new conversation and an explicitly known-rejected recipient still can send.
3. Cover both ordinary finalization and the per-day due reminder upgrade boundary. If a
   backfill is chosen, make it deterministic and tenant-safe; if dual-read compatibility
   is chosen, prove when the legacy key is retired.

### P2 — An empty returning website session is considered to have a visitor message and is pinged

`qualify_and_finalize` checks `has_website_prospect_message(lead_id)` at
`app/services/finalization.py:249-264`. The store query is explicitly lead-scoped and
does not filter by conversation id (`app/db/store.py:2795-2808`). A returning visitor can
have two website identities on one lead, so an old message in session A makes an empty
session B pass the `require_visitor_message` gate. `build_conversation_summary` then reads
turns by session B and sends a nearly empty card.

Real store reproduction:

```text
lead: one old prospect MESSAGE_IN in web_old_with_message
new identity on same lead: web_new_empty, no events

qualify_and_finalize(session_id='web_new_empty', require_visitor_message=True)
-> {'claimed': True, 'sent': True, 'duplicate': False, 'kind': 'web_final_v1'}
-> one owner delivery:
   New website conversation

   Recommended next step: inactivity
   Conversation ID: web_new_empty
```

This contradicts PRODUCT's `Empty opens are not pinged` contract and can also cause the
inactivity worker to notify on an identity whose own conversation never began.

Required repair gate: make the visitor-message predicate conversation-specific and use
the same session/lead binding in the inactivity query and finalization service. Add a
real-store regression with one messaged session and one empty identity on the same lead;
the empty identity must create no inbox row, recipient claim, or transport call.

## Objective-by-objective outcome table

| Objective | Verdict | Independent evidence |
| --- | --- | --- |
| Sheets operation/id/range/value ordering and denial-before-port/claim | **FAIL** | Pure allowlist/A1/shape/formula/empty/kill/principal prevalidation and corrected same-event replay pass, but the direct negated-operation and strict-subset probes above mutate. |
| Website finalization and due-reminder recipient semantics | **FAIL** | New rows use conversation id/local day and current partial rejection/ambiguous/no-config tests pass, but legacy completed claims reopen and empty returning sessions are notified. |
| Hot HANDOFF ordering and normal multi-owner retry | PASS | `app/domain/hot_handoff.py:126-177` enforces kill/risk before takeover/follow-up/inbox/claim/transport; focused real-store tests cover no-attempt, all-success, partial rejection, ambiguous retention, and no resend. |
| Gmail callback recovery/binding | PASS (local) | Numeric callback auth precedes approval/Gmail construction; binding/expiry/risk/write/demo/kill checks remain; known provider failure releases, completed send dedupes. |
| Unauthorized owner construction and principal isolation | PASS | `process_owner_texts` filters the batch before settings/default builders; `process_owner_item` authenticates before `Principal.owner`; ClientGraph rejects owner principals. |
| HANDOFF single-card and visitor truth | PASS for current-state flow | ClientGraph returns at HANDOFF before ordinary finalization and only reports completed receipt after at least one accepted owner delivery. |
| Telegram voice and one-owner-agent runtime shape | PASS (local) | Numeric owner auth occurs before voice download/STT; transcript enters the same `process_owner_texts`/OwnerGraph text path; repository search found no TTS implementation; one bounded `run_owner_agent` loop remains. |
| GA4/GSC/LinkedIn/Sheets capability wiring | PASS except Sheets authorization finding | Named owner-only capabilities, pinned typed adapters, and normalized projections are wired and focused tests pass; GA4/GSC/LinkedIn remain reads/profile-only. |
| Minimality/removal claims | PASS | Removed symbol/name-discovery searches returned zero definitions; eval families are 10; deploy plaintext-env and arbitrary migration command surfaces remain removed. |
| Migration packaging/enumeration/new-state application | **FAIL overall** | Docker copies `migrations`; `mia-migrate` is pinned; 37 files enumerate in filename order; new file is index 36/last, portable, recorded, and creates five columns; PostgreSQL compiles one `ON CONFLICT DO NOTHING`. Legacy-state semantics fail as above. |
| 164-row audit reconciliation and measurements | PASS | 164 current files = 164 matrix rows, 23/73/68, zero missing/extra/duplicate; dispositions 139 KEEP / 24 SIMPLIFY / 1 MERGE. |
| Full regression/lint/evals/origin/diff | PASS mechanically | Exact results below. |

## Inventory and measured numbers

The inventory was rebuilt from current `app/**/*.py` and `scripts/**/*.py` files that
contain `def` or `async def`. Matrix rows were parsed independently from all three audit
documents and normalized against the filesystem.

```text
function-bearing files                  164
definition lines                        1,626
physical lines                          42,054
non-blank lines                         37,373
strict app+scripts C901 findings        37
matrix rows / unique paths              164 / 164
missing / extra / duplicate paths       0 / 0 / 0
partition                               23 API / 73 domain / 68 infra
dispositions                            KEEP 139 / SIMPLIFY 24 / MERGE 1 / REMOVE 0
```

These are current-tree measurements only. No pre-cleanup physical/nonblank baseline is
claimed.

## Migration/package probe

The representative SQLite probe created the current base schema, ran the real migration
worker function, inspected the new table, and separately compiled the store insert for
the PostgreSQL dialect:

```text
{'count': 37, 'sorted': True, 'target_present': True, 'target_index': 36,
 'last': '20260828_owner_notification_recipient_claims.sql',
 'postgres_only': False, 'failed': '', 'recorded': True,
 'columns': ['kind', 'lead_id', 'notification_key', 'recipient_id', 'claimed_at']}

INSERT INTO owner_notification_recipient_claims
  (kind, lead_id, notification_key, recipient_id, claimed_at)
VALUES (...) ON CONFLICT DO NOTHING
```

`deploy/Dockerfile:7` copies the migrations directory, `pyproject.toml:19` maps
`mia-migrate`, and `scripts/run_ecs_migration.py:37` fixes the ECS override to that
entry point. This proves packaging/enumeration/SQLite application and PostgreSQL SQL
compilation, not live PostgreSQL migration or production compatibility.

## Commands and exact results

All Python commands used `uv --offline --cache-dir .uv-cache`; pytest used a
workspace-local `--basetemp`, disabled its cache provider, and set
`MIA_DATABASE_URL=sqlite:///:memory:`.

1. Focused 20-file adversarial suite covering Sheets, Gmail/callbacks, finalization,
   due reminders, HANDOFF, principal boundaries, Telegram/website voice, GA4/GSC/
   LinkedIn, deploy scripts, and migration:

   ```text
   267 passed, 119 warnings in 22.90s
   ```

2. Complete pytest tree:

   ```text
   2,424 passed, 1,856 warnings in 79.55s
   ```

3. `uv --offline --cache-dir .uv-cache run ruff check app tests scripts`

   ```text
   All checks passed!
   ```

4. Strict complexity measurement:

   ```text
   Found 37 errors.  (expected nonzero C901 measurement exit)
   ```

5. `uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py`

   ```text
   origin-bind: ok
   ```

6. `uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py`

   ```text
   273/273: sales 51, buyer 43, calendar 20, website_handoff 15,
   safety 20, objection 20, routing 20, extract 30, writing 33, gold 21
   ```

7. `git diff --check`

   ```text
   exit 0; LF-to-CRLF working-copy warnings only, no whitespace error
   ```

Green aggregate commands are incorporated but do not supersede the three explicit
semantic reproductions.

## Decision and non-claims

**FAIL — leave Phase 1.5.4f G4, node 1.5 G4/G5, and root G7 open.** Do not
deploy this cleanup tree until the exact repair gates above are implemented and a fifth
fresh HEAVY reviewer can fail the outcome.

This review does not claim a deployed image, AWS state, live Telegram/Gmail/Sheets/GA4/
GSC/LinkedIn behavior, real-device voice latency/barge-in, live PostgreSQL concurrency,
an applied production migration, or a pre-cleanup physical-line reduction. It also does
not claim that new-state SQLite/PostgreSQL conflict syntax proves upgrade compatibility.
