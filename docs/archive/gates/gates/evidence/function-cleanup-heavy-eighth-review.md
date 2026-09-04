# Phase 1.5 function cleanup — eighth HEAVY clean-room review

Date: 2026-08-28

Reviewer mode: fresh verifier; no implementation work

Verdict: **FAIL**

Phase 1.5 must remain open. The current dirty worktree has one reproducible P1 and
three reproducible P2 findings. The mechanical suites and the exact 164-file
inventory reconcile, but they do not override outcome failures.

## Release-blocking findings

### P1 — standalone Hebrew negators next to punctuation still authorize Sheets writes

`app/tools/registries/owner_tools.py:840` recognizes `לא` and `אל` only when they
are preceded by the start of the string or whitespace. That is narrower than the
owner-turn rule, which requires any unquoted standalone Hebrew negator anywhere in
the turn to deny the write before port construction, claim, idempotency, or provider
mutation.

I invoked the real owner Sheets registry with a real SQLite `LeadStore`, an allowed
spreadsheet ID, and a counting fake Sheets port. These unquoted owner turns were
accepted:

```text
בבקשה,לא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1
בבקשה (לא) לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1
בבקשה אל־תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1
```

Each call used `sheets_append` with `spreadsheet_id=sheet-allowed`,
`range=KPI!A1`, and `values=[["x"]]`. The cumulative result was:

```text
comma_he {'ok': True, 'error': '', 'port': 1, 'claim': 1, 'rows': 1,
          'ops': [('append', 'sheet-allowed', 'KPI!A1', [['x']])]}
paren_he {'ok': True, 'error': '', 'port': 2, 'claim': 2, 'rows': 2,
          'ops': [... second append ...]}
maqaf_he {'ok': True, 'error': '', 'port': 3, 'claim': 3, 'rows': 3,
          'ops': [... third append ...]}
```

This is not a cosmetic matcher preference: an explicit owner prohibition reaches
the claim and mutation boundary.

### P2 — duplicate Telegram updates repeat download and STT before the webhook claim

`app/api/telegram.py:205-256` transcribes voice before `process_owner_texts` can
claim the provider event. Replaying the same authenticated-owner update twice with
a counting media port, real webhook route, real store, and counting transcription
and OwnerGraph fakes produced:

```text
{'first': {'processed': 1, 'duplicates': 0, 'sent': True, 'sent_count': 1,
           'reply': 'ok'},
 'second': {'processed': 0, 'duplicates': 1, 'sent': False,
            'sent_count': 0, 'reply': None},
 'downloads': 2, 'stt_calls': 2, 'graph_calls': 1, 'replies': 1}
```

OwnerGraph and the Telegram reply are deduplicated, but the external download and
STT provider call are not. This violates the required voice retry-idempotency
boundary and can duplicate provider cost and disclosure.

### P2 — an alternate Telegram media port bypasses empty/size controls

The default Telegram adapter enforces the 16,000,000-byte maximum at
`app/integrations/telegram.py:233`, but the route-level boundary at
`app/api/telegram.py:46-78` only normalizes MIME before invoking STT. A custom
`download_voice` implementation can therefore bypass the adapter check.

Direct calls through `_transcribe_telegram_voice` with a valid `audio/ogg` MIME and
counting transcription port produced:

```text
oversize_alternate {'failed': False, 'stt_calls': 1, 'text': 'accepted'}
empty_audio        {'failed': False, 'stt_calls': 1, 'text': 'accepted'}
```

The first payload was 16,000,001 bytes and the second was `b''`. Both should fail
visibly with zero STT calls under the required alternate-media-port lens.

### P2 — the named Gmail recovery test is not isolated from its own same-process rerun

Both named recovery tests pass alone and in both pair orders. I then executed each
test twice in one Python process using two sequential `pytest.main(...)` calls. The
console test passed twice. The outbound callback test passed once and failed on the
second run:

```text
tests/unit/test_telegram_owner_outbound.py::
  test_gmail_callback_recovers_deferred_and_failed_send_once
run 1: rc=0
run 2: rc=1

E assert [] == ['draft_callback_recovery_isolated_1']
E at tests/unit/test_telegram_owner_outbound.py:357

tests/unit/test_owner_gmail_console.py::
  test_approved_gmail_send_deferrals_remain_retryable
run 1: rc=0
run 2: rc=0
```

The outbound test's fixed fake resource ID at
`tests/unit/test_telegram_owner_outbound.py:278` collides with completed
process-lifetime SQLite state on its rerun. Customizing the fake resource does not
weaken the production resource/hash binding assertion, but the current customization
does not satisfy the required repeatability evidence.

## Adversarial coverage that passed

- Sheets: the current focused run rechecked clean exact English/Hebrew positives,
  quoted JSON negation inertness, embedded `notable`, all English negator forms,
  spaced tabs, repeated/multiple/quoted/collision targets, ID/range/value/operation
  binding, RAW input rejection, allowlist, policy, principal/source, kill switch,
  empty values, and replay idempotency. Those cases pass; the punctuation-adjacent
  Hebrew cases above are an uncovered counterexample.
- Telegram voice: authenticated-owner checking precedes download. Missing, blank,
  malformed, and non-audio MIME fail visibly; supported case/parameterized audio
  normalizes and reaches the same OwnerGraph; host/path/HTTP checks remain in the
  default adapter; failure replies do not expose provider details; no TTS path was
  found. The retry and alternate-port byte-boundary counterexamples remain.
- Gmail: each named test passes alone and in both pair orders. Fake IDs are hashed
  into the same approval resource-binding assertion as production IDs. The outbound
  test still fails its own feasible same-process rerun as reported above.
- Current notification/finalization/recipient tests cover per-recipient claims,
  legacy conversation and same-day due-key compatibility, empty returning sessions,
  hot-handoff kill ordering, and explicit provider failure/release behavior.
- The provider suite rechecked normalized GA4/GSC KPI reads and LinkedIn profile-only
  traversal through capability, policy, registry, and adapter boundaries.
- Source inspection found one runtime OwnerGraph agent entry point
  (`app/graph/owner_agent.py`) and one call from `app/domain/owner_brain.py`; no
  runtime swarm or advertised executable path for the removed ADR-039 capabilities
  was found. Retained enum/metadata identifiers are compatibility records, not live
  tools.

## Commands and current-tree results

All pytest commands used a workspace-local `--basetemp` and
`-p no:cacheprovider`. `uv` ran offline with the workspace `.uv-cache`.

Focused repair suite:

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-heavy-eighth-focused-review `
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py `
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py `
  tests/unit/test_telegram.py tests/unit/test_transcribe.py `
  tests/unit/test_telegram_owner_outbound.py tests/unit/test_owner_gmail_console.py
```

Result: **149 passed**.

Exact recorded 19-file suite:

```text
tests/unit/test_vnext_finalization.py
tests/unit/test_website_handoff_owner_notify.py
tests/unit/test_hot_handoff.py
tests/unit/test_due_scan_worker.py
tests/unit/test_comm_operating_model.py
tests/unit/test_owner_notify.py
tests/unit/test_website_client_graph.py
tests/unit/test_vnext_graph_functions.py
tests/unit/test_migrate.py
tests/unit/test_owner_sheets.py
tests/unit/test_owner_live_tools.py
tests/unit/test_sheets.py
tests/unit/test_vnext_principal.py
tests/unit/test_vnext_owner_voice.py
tests/unit/test_telegram.py
tests/unit/test_transcribe.py
tests/unit/test_telegram_owner_outbound.py
tests/unit/test_telegram_owner_graph.py
tests/unit/test_telegram_format.py
```

Command form: `uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider
--basetemp .pytest-heavy-eighth-combined-review <the files above in that order>`.
Result: **305 passed**.

Provider seam suite:

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-heavy-eighth-provider-review `
  tests/unit/test_ga4.py tests/unit/test_search_console.py `
  tests/unit/test_linkedin.py tests/unit/test_owner_composio_capabilities.py `
  tests/unit/test_owner_live_tools.py
```

Result: **63 passed**.

Full suite:

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-heavy-eighth-full-review
```

Result: **2,447 passed, 1,928 warnings in 117.26s**.

Other mechanics:

```text
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
  All checks passed!

uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
  origin-bind: ok

uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
  sales 51/51; buyer 43/43; calendar 20/20; website_handoff 15/15;
  safety 20/20; objection 20/20; routing 20/20; extract 30/30;
  writing 33/33; gold 21/21; total 273/273

uv --offline --cache-dir .uv-cache run ruff check app scripts --select C901
  Found 36 errors. (strict measurement; expected nonzero inventory metric)

git diff --check
  exit 0; line-ending warnings only
```

## Independent inventory reconciliation

An independent AST/physical-line counter over the audited `app/**/*.py` and
`scripts/*.py` universe, joined against all three matrices, returned:

```text
files=164
definitions=1633
physical_lines=42223
nonblank_lines=37524
C901=36
matrix_rows=164
matrix_unique=164
missing=[]
extra=[]
duplicates=[]
partition={api: 23, domain: 73, infra: 68}
dispositions={KEEP: 139, SIMPLIFY: 24, MERGE: 1, REMOVE: 0}
```

The exact 164-file inventory is accounted for with zero matrix gaps. Inventory
reconciliation is necessary but not sufficient for PASS.

## Minimality assessment

The cleanup generally keeps channel, capability/policy, and adapter boundaries
intact, and the 164-file matrices do not expose an unaccounted dead file. The four
findings are simpler boundary-ordering/isolation defects, not requests for redesign:
the current code duplicates a size invariant across only one adapter, places the
voice claim after an external provider effect, narrows the Hebrew prohibition rule
with punctuation-sensitive context, and uses a process-persistent fake identifier.
Each has correctness, cost/privacy, or verification-integrity impact at P1/P2.

## Explicit non-claims

- This review did not inspect `.env`, secret values, AWS, production, or live
  Telegram, Gmail, Sheets, GA4, GSC, LinkedIn, Composio, or STT providers.
- It does not claim deployment, migration application, production traffic proof,
  real-device voice behavior, or external-provider success.
- Passing fake-adapter tests does not prove live credentials, scopes, rate limits,
  provider semantics, or production observability.
- This review does not reverse an accepted ADR, introduce a runtime swarm, or treat
  development review agents as production Mia agents.
- Historical failed-review evidence is preserved. No Phase 1.5 leaf/node/root gate
  was closed, and `PLAN.md` was not edited.

Only this evidence file was created by the eighth reviewer.
