# Phase 1.5 clean-room HEAVY sixth outcome review

Date: 2026-08-28
Mode: independent outcome review; only this evidence file was created
Verdict: **FAIL**

The exact 19-file combined suite, complete 2,431-test tree, lint, origin gate,
deterministic evals, migration tests, complexity measurement, diff hygiene, and exact
164-file audit reconciliation all pass. The outcome gate does not pass: two P1 Sheets
authorization defects remain outside that regression boundary. Both direct probes used
the real owner registry, real `LeadStore` on SQLite, and `FakeSheetsPort`; both constructed
the port, consumed the operation claim, and mutated the fake provider. No P0 was found.

No `.env` file or secret value was inspected. No live provider/network call, AWS action,
deployment, production database, or application/test/migration/document edit occurred.

## Severity-ranked findings

### P1 - spaced tab names collapse and let the model choose a different Sheet tab

The owner-to-tool binder does not preserve a complete tab name containing spaces:

- `app/tools/registries/owner_tools.py:914` reduces the model range to the last
  whitespace-delimited token with `a1_range.rsplit(" ", 1)[-1]`.
- `app/tools/registries/owner_tools.py:927-933` reconstructs a tab identity from only the
  final ASCII word before `!`.
- `app/tools/registries/owner_tools.py:948-952` then considers `Bar!A1` a complete token
  inside `Foo Bar!A1` because the preceding character is whitespace.
- Spaced tab names are valid by the actual bounded-range contract at
  `app/integrations/sheets.py:174-176`.

Consequently two different owner-stated targets, `Foo Bar!A1` and `Other Bar!A1`, collapse
to one identity, `Bar!A1`. The model can select that third tab even though the owner never
named it as a target.

Exact reproduction result:

```text
owner: Please append "x" to sheet-allowed at Foo Bar!A1 or Other Bar!A1 in the Sheet
tool:  sheets_append(sheet-allowed, Bar!A1, [["x"]])

{'case': 'spaced_tabs', 'ok': True, 'error': '',
 'calls': {'port': 1, 'claim': 1}, 'rows': 1,
 'operations': [('append', 'sheet-allowed', 'Bar!A1', [['x']])]}
```

Runnable reproduction (PowerShell, local-only):

```powershell
$env:MIA_DATABASE_URL='sqlite:///:memory:'
@'
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.db.models import IdempotencyRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.integrations.sheets import FakeSheetsPort
from app.tools.registries import owner_tools
from app.tools.registries.owner_tools import ToolContext, execute_tool

init_db()
session = get_session_factory()()
store = LeadStore(session)
port = FakeSheetsPort()
ctx = ToolContext(
    principal=Principal.owner(source="probe"), store=store, brain=BrainStore(session),
    settings=get_settings().model_copy(
        update={"sheets_allowed_spreadsheet_ids": "sheet-allowed,sheet-other"}
    ),
    embedding_port=FakeEmbeddingPort(), source_ref="telegram:tabs",
    owner_text='Please append "x" to sheet-allowed at Foo Bar!A1 or Other Bar!A1 in the Sheet',
    sheets=port,
)
calls = {"port": 0, "claim": 0}
real_port = owner_tools._owner_sheets_port
real_claim = store.claim_operation
def counted_port(inner):
    calls["port"] += 1
    return real_port(inner)
def counted_claim(**kwargs):
    calls["claim"] += 1
    return real_claim(**kwargs)
owner_tools._owner_sheets_port = counted_port
store.claim_operation = counted_claim
result = execute_tool("sheets_append", {
    "spreadsheet_id": "sheet-allowed", "range": "Bar!A1", "values": [["x"]]
}, ctx)
print({"ok": result.ok, "calls": calls,
       "rows": session.query(IdempotencyRow).filter_by(
           scope="owner_sheets_write").count(),
       "operations": port.owner_operations})
'@ | uv --offline --cache-dir .uv-cache run python -
```

Required repair: parse and compare the complete bounded A1 target (including its full
tab prefix) rather than reconstructing it from the final word. Reject every second
distinct complete tab/range before port construction and before claiming. Add the exact
spaced-tab regression above alongside the current simple `KPI!A1`/`Other!A1` case.

### P1 - bare English negation authorizes the prohibited append

`app/tools/registries/owner_tools.py:821-843` first finds the bare verb `append` as an
affirmative mention. Its English negation pattern at line 837 recognizes only `do not`,
`don't`/`dont`, and `never`; it does not recognize the direct natural construction
`Please not append ...`. The request therefore reaches the provider even though its only
operation instruction is negative.

Exact reproduction result:

```text
owner: Please not append "x" to sheet-allowed at KPI!A1 in the Sheet
tool:  sheets_append(sheet-allowed, KPI!A1, [["x"]])

{'case': 'negation', 'ok': True, 'error': '',
 'calls': {'port': 1, 'claim': 1}, 'rows': 1,
 'operations': [('append', 'sheet-allowed', 'KPI!A1', [['x']])]}
```

The same runnable harness above reproduces this finding by changing `owner_text` to the
shown sentence and `range` to `KPI!A1`.

Required repair: make negative classification fail closed for a standalone `not`
immediately governing the operation verb, while preserving the current quoted-cell
exclusion and affirmative English/Hebrew cases. Add this exact direct-registry case and
its update equivalent, proving zero port construction, zero claim, zero idempotency row,
and zero provider operation.

## Mandatory direct probes

### Sheets target and value boundary

The maintained real-registry tests and separate counter probes covered two allowed IDs
plus two ranges, one ID plus two ranges, two IDs plus one range, simple different tabs
with the same core A1, quoted targets, ID/range prefix and suffix collisions, exact one
target success and replay, whitespace-only cells, internal spaces, exact quoted
multiplicity, subset/superset mismatches, duplicate mismatch, reversed/oversized ranges,
shape, formula, allowlist, RAW mode, kill switch, client principal, missing source event,
and English/Hebrew negation/conflict. All maintained cases passed and every maintained
deny stayed before port/claim/provider. The two P1 probes above deliberately extended
that boundary and mutated.

A separate positive real-registry probe confirmed exact duplicate literals, replay
idempotency, and internal spaces:

```text
{'duplicate_ok': True, 'replay_ok': True, 'internal_spaces_ok': True,
 'operations': [
   ('update', 'sheet-allowed', 'KPI!A1:B1', [['x', 'x']]),
   ('append', 'sheet-allowed', 'KPI!A2', [['x  y']])
 ]}
```

### Notifications, finalization, Gmail, and authority transitions

The focused and combined suites passed the direct real-store transitions for:

- exact-conversation legacy finalization suppression; same-local-day-only legacy due
  suppression; empty returning-session isolation;
- no-config/no-text recipient behavior; accepted, ambiguous, partial rejection, and
  missing-recipient recovery for ordinary finalization, due reminders, and HANDOFF;
- hot-handoff kill/risk before takeover, follow-up, inbox, recipient claim, and transport;
- Gmail callback binding, deferral, provider failure release, retry, and completed-send
  idempotency;
- unauthorized/empty owner batches before settings/default adapter construction and
  ClientGraph rejection of an owner principal.

These are local fake-boundary and SQLite results, not live Telegram/Gmail proof.

### Voice, provider, and product shape

Focused tests and current-source inspection confirm numeric Telegram message
authorization before voice download/STT, voice input entering the same OwnerGraph text
path, text-only output and no TTS implementation, one bounded `run_owner_agent` loop,
one OwnerGraph plus one ClientGraph, one website knowledge retrieval, normalized
AssafWeb GA4/GSC reads, profile-only LinkedIn, explicit-ID Sheets operations, and no
runtime Sheets Drive discovery or Sheets-as-system-of-record recovery. Repository-wide
search found no remaining ADR-039 runtime surfaces named by the cleanup audit.

## Verification commands and exact results

All Python commands used `uv --offline --cache-dir .uv-cache`; pytest used
`MIA_DATABASE_URL=sqlite:///:memory:`, `-p no:cacheprovider`, and a workspace basetemp.

1. Focused adversarial suite (enumerated Sheets, notification, Gmail callback,
   principal, voice, GA4/GSC/LinkedIn, website retrieval, script, and migration cases):

   ```powershell
   uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
     --basetemp .pytest-heavy-sixth-focused-review `
     tests/unit/test_owner_live_tools.py::test_owner_sheets_write_binds_id_range_and_every_literal_before_claim `
     tests/unit/test_owner_live_tools.py::test_owner_sheets_semantic_binding_rejects_negation_conflicts_and_payload_mismatches `
     tests/unit/test_owner_live_tools.py::test_owner_sheets_write_binds_exactly_one_unquoted_target_before_side_effects `
     tests/unit/test_owner_live_tools.py::test_owner_sheets_prevalidates_policy_and_arguments_before_port_or_claim `
     tests/unit/test_owner_live_tools.py::test_owner_sheets_write_accepts_exact_english_binding `
     tests/unit/test_owner_sheets.py::test_owner_sheets_read_update_append_request_shapes `
     tests/unit/test_owner_sheets.py::test_owner_sheets_normalization_preserves_internal_spaces_in_nonempty_cells `
     tests/unit/test_owner_sheets.py::test_owner_sheets_outside_allowlist_and_bad_values_do_not_call_http `
     tests/unit/test_owner_sheets.py::test_owner_sheets_values_must_fit_target_range_before_http `
     tests/unit/test_owner_sheets.py::test_owner_sheets_policy_denies_client_and_kill_switch_before_fake_write `
     tests/unit/test_vnext_finalization.py::test_legacy_completed_conversation_claim_is_retained_after_recipient_upgrade `
     tests/unit/test_vnext_finalization.py::test_finalization_recipient_ledger_retries_only_known_rejection `
     tests/unit/test_vnext_finalization.py::test_finalization_no_config_replays_when_delivery_becomes_available `
     tests/unit/test_vnext_finalization.py::test_empty_returning_session_cannot_borrow_an_old_sessions_visitor_message `
     tests/unit/test_due_scan_worker.py::test_due_reminder_retries_only_the_known_rejected_owner `
     tests/unit/test_due_scan_worker.py::test_legacy_same_day_due_claim_does_not_resend_but_old_day_does `
     tests/unit/test_hot_handoff.py::test_hot_handoff_kill_switch_mutates_nothing_in_the_real_store `
     tests/unit/test_website_handoff_owner_notify.py::test_website_handoff_partial_rejection_retries_only_the_missing_owner `
     tests/unit/test_website_handoff_owner_notify.py::test_website_handoff_ambiguous_recipient_is_not_retried `
     tests/unit/test_website_handoff_owner_notify.py::test_website_handoff_successful_replay_does_not_resend `
     tests/unit/test_telegram_owner_outbound.py::test_gmail_callback_recovers_deferred_and_failed_send_once `
     tests/unit/test_vnext_principal.py tests/unit/test_telegram.py `
     tests/unit/test_vnext_owner_voice.py tests/unit/test_ga4.py `
     tests/unit/test_search_console.py tests/unit/test_linkedin.py `
     tests/unit/test_visitor_knowledge.py::test_website_client_graph_executes_knowledge_once `
     tests/unit/test_scripts.py tests/unit/test_migrate.py
   ```

   ```text
   101 passed, 56 warnings in 15.43s
   ```

2. Exact required 19-file combined suite:

   ```powershell
   uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
     --basetemp .pytest-heavy-sixth-combined-review `
     tests/unit/test_vnext_finalization.py `
     tests/unit/test_website_handoff_owner_notify.py `
     tests/unit/test_hot_handoff.py `
     tests/unit/test_due_scan_worker.py `
     tests/unit/test_comm_operating_model.py `
     tests/unit/test_owner_notify.py `
     tests/unit/test_website_client_graph.py `
     tests/unit/test_vnext_graph_functions.py `
     tests/unit/test_migrate.py `
     tests/unit/test_owner_sheets.py `
     tests/unit/test_owner_live_tools.py `
     tests/unit/test_sheets.py `
     tests/unit/test_vnext_principal.py `
     tests/unit/test_vnext_owner_voice.py `
     tests/unit/test_telegram.py `
     tests/unit/test_transcribe.py `
     tests/unit/test_telegram_owner_outbound.py `
     tests/unit/test_telegram_owner_graph.py `
     tests/unit/test_telegram_format.py
   ```

   Result: **289 passed, 227 warnings in 11.73s**.

3. Complete tree:

   ```powershell
   uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
     --basetemp .pytest-heavy-sixth-full-review
   ```

   Result: **2,431 passed, 1,856 warnings in 79.80s**.

4. `uv --offline --cache-dir .uv-cache run ruff check app tests scripts`

   Result: **All checks passed**.

5. `uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py`

   Result: **origin-bind: ok**.

6. `uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py`

   Result: **273/273**, zero failures: sales 51, buyer 43, calendar 20,
   website_handoff 15, safety 20, objection 20, routing 20, extract 30, writing 33,
   and gold 21.

7. `uv --offline --cache-dir .uv-cache run ruff check app scripts --select C901
   --output-format concise`

   Result: expected measurement exit 1, **37 C901 findings**.

8. `git diff --check`

   Result before and after this evidence edit: exit **0**; line-ending warnings only.

Green aggregate commands do not supersede the two direct provider-boundary mutations.

## Inventory and migration assessment

The inventory was rebuilt from current `app/**/*.py` and `scripts/**/*.py` files that
contain `def` or `async def`, then compared with disposition rows parsed independently
from the three matrices:

```text
function-bearing files                  164
definition lines                        1,632
physical lines                          42,202
non-blank lines                         37,505
strict app+scripts C901 findings        37
matrix rows / unique paths              164 / 164
missing / extra / duplicate paths       0 / 0 / 0
partition                               23 API / 73 domain / 68 infra
dispositions                            139 KEEP / 24 SIMPLIFY / 1 MERGE / 0 REMOVE
```

Every current function-bearing production/script file remains dispositioned exactly
once. These are current-tree measurements; no pre-cleanup physical/nonblank baseline is
claimed.

Migration/package checks found **37** sorted SQL files; the target migration is present
at zero-based index 36 and is last. `deploy/Dockerfile` copies `migrations`, the project
maps `mia-migrate`, the ECS runner pins exactly `mia-migrate`, and the deploy/migration
scripts expose neither plaintext `--env` injection nor an arbitrary `--command` switch.
The focused/combined migration tests passed first local SQLite apply, second-run
idempotency, image-layout discovery, and migration worker ordering. Real-store recipient
tests passed conflict/deduplication behavior. PostgreSQL dialect compilation produced:

```sql
INSERT INTO owner_notification_recipient_claims
  (kind, lead_id, notification_key, recipient_id, claimed_at)
VALUES (%(kind)s, %(lead_id)s, %(notification_key)s, %(recipient_id)s, %(claimed_at)s)
ON CONFLICT DO NOTHING
```

This proves local SQLite apply/idempotency and SQL construction, not a live PostgreSQL
migration or production concurrency.

## Decision and explicit non-claims

**FAIL - leave `gates/leaf-1.5.4f-final-review.md`,
`gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, and `gates/root.md`
unchanged.** Passing 2,431 tests and every mechanical gate cannot close an outcome gate
while two direct prohibited/wrong-target mutations remain.

This review does not claim a deployed image, AWS/ECS state, live Telegram voice or
callback behavior, live Gmail send, live Sheets/GA4/GSC/LinkedIn execution, real-device
latency or barge-in, live PostgreSQL migration/concurrency, or any pre-cleanup
physical/nonblank line reduction.
