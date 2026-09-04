VERDICT: FAIL

# Phase 1.5 function cleanup — ninth HEAVY clean-room review

Date: 2026-08-28

Reviewer mode: fresh verifier; no implementation work

Phase 1.5 remains open. Two independently reproduced P1 Sheets authorization/binding
defects survive in the current dirty worktree. The claimed mechanical results reconcile,
but cannot override mutation-boundary counterexamples.

## Release-blocking findings

### P1 — Hebrew prohibitions containing niqqud authorize a Sheets mutation

`app/tools/registries/owner_tools.py:821-841` searches for contiguous `לא` or `אל`.
Common pointed Hebrew forms insert a combining mark between those letters, so the
negation is missed and the affirmative mutation verb remains eligible.

I called the real owner Sheets registry with a real SQLite `LeadStore`, an explicit
numeric-owner principal, `sheet-allowed` as the only allowlisted spreadsheet, and a
counting fake Sheets port. The tool call was `sheets_append(sheet-allowed, KPI!A1,
[["x"]])`. Both owner turns below were accepted:

```text
בבקשה לֹא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1
בבקשה אַל תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1
```

Observed independently for each fresh store:

```text
hebrew_holam {'ok': True, 'calls': {'port': 1, 'claim': 1}, 'rows': 1,
               'ops': [('append', 'sheet-allowed', 'KPI!A1', [['x']])]}
hebrew_patah {'ok': True, 'calls': {'port': 1, 'claim': 1}, 'rows': 1,
               'ops': [('append', 'sheet-allowed', 'KPI!A1', [['x']])]}
```

This is a direct safety-boundary failure: an explicit prohibition reaches both the
durable claim and provider mutation.

### P1 — a second complete unquoted A1 target is ignored without another introducer

`app/tools/registries/owner_tools.py:891-920` counts only A1 tokens matched after its
small introducer grammar. A complete second target following punctuation is invisible,
so the model may choose one of two owner-stated mutation targets.

With the same real registry/store/counting-port harness, the tool again selected
`KPI!A1` and `[["x"]]`. Both ambiguous turns were accepted:

```text
Please append "x" to sheet-allowed at KPI!A1, KPI!B1 in the Sheet
Please append "x" to sheet-allowed at KPI!A1 (KPI!B1) in the Sheet
```

Observed independently for each fresh store:

```text
comma_second_target {'ok': True, 'calls': {'port': 1, 'claim': 1}, 'rows': 1,
                      'ops': [('append', 'sheet-allowed', 'KPI!A1', [['x']])]}
paren_second_target {'ok': True, 'calls': {'port': 1, 'claim': 1}, 'rows': 1,
                      'ops': [('append', 'sheet-allowed', 'KPI!A1', [['x']])]}
```

The contract requires exactly one complete bounded unquoted A1 target, not exactly one
target recognized after a preferred introducer. This ambiguity also reaches claim and
mutation.

## Mandatory-lens verification

- Sheets: source inspection confirms policy authorization and pure argument validation
  precede port construction/claim at `app/tools/registries/owner_tools.py:713-761`, with
  allowlisting, bounded A1/shape/value/formula validation in `app/integrations/sheets.py`
  and `RAW` write semantics. Focused tests cover exact operation, quoted-literal multiset,
  spaced tab names, empty/formula/shape bounds, principal/source/kill policy, and replay
  idempotency. A repeated spreadsheet ID was rejected with zero port/claim rows. The two
  counterexamples above falsify the remaining negation and exact-target guarantees.
- Telegram voice: numeric-owner filtering precedes effects; the webhook is claimed before
  download/STT; duplicate success/failure tests show no repeated download, STT, OwnerGraph,
  or reply. Missing, forged, wrong-channel, wrong-kind, and wrong-status preclaims produced
  zero effects; an exact received Telegram voice preclaim produced one. Alternate-port
  media validation rejects non-bytes, empty, over 16,000,000 bytes, and unsupported or
  malformed MIME. Malformed return shapes fail visibly once and replay without effects.
  Direct probes confirmed download/STT `TypeError`, STT `ValueError`, and cancellation
  propagate; only unpack-shape errors are translated. The default adapter fixes the
  Telegram host and validates path/HTTP/media. Failure replies are fixed and provider
  neutral. Voice produces one text reply through OwnerGraph and no TTS path was found.
- Gmail callback recovery: fresh UUID-backed draft resources recover deferred and failed
  sends exactly once. Same-process A-then-B and B-then-A executions both returned `0,0`;
  an additional same-process repetition run returned `0,0`.
- Notifications/finalization/HANDOFF: source and focused tests confirm per-recipient durable
  claims, rejected-vs-ambiguous claim handling, kill/risk policy before mutation/effects,
  missing-config and partial-recipient isolation, conversation-scoped returning-visitor
  finalization, and early return from the ClientGraph handoff branch.

## Verification commands and current-tree results

All pytest runs used a workspace-local `--basetemp`, `-p no:cacheprovider`, and the
offline workspace uv cache.

```text
Exact recorded 19-file suite: 321 passed
Full pytest tree:             2463 passed, 1955 warnings in 60.74s
Same-process voice/Gmail trio, run twice: 3 passed; 3 passed; RC=0,0
Gmail same-process order A,B: 1 passed; 1 passed; RC=0,0
Gmail same-process order B,A: 1 passed; 1 passed; RC=0,0

uv ... ruff check app tests scripts
  All checks passed!

uv ... python scripts/assert_origin_bind.py
  origin-bind: ok

uv ... python scripts/eval_diff.py
  sales 51/51; buyer 43/43; calendar 20/20; website_handoff 15/15;
  safety 20/20; objection 20/20; routing 20/20; extract 30/30;
  writing 33/33; gold 21/21; total 273/273

uv ... ruff check app scripts --select C901 --output-format concise
  36 findings (expected nonzero inventory measurement)

git diff --check
  exit 0; line-ending warnings only
```

The exact 19-file suite was:

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

## Inventory and evidence reconciliation

An independent current-tree AST/physical-line counter and a disposition-only parse of all
three audit matrices returned:

```text
function-bearing app/scripts files = 164
definitions                       = 1634
physical lines                    = 42251
nonblank lines                    = 37550
C901                              = 36
matrix rows/unique                = 164/164
missing/extra/duplicates          = 0/0/0
KEEP/SIMPLIFY/MERGE/REMOVE        = 139/24/1/0
```

Thus the parent's `321`, `2463/1955`, Ruff, origin, `273/273`, code-size, C901, and
`164/164` hypotheses all reproduce exactly. Earlier review counts are historical
snapshots and correctly differ from the present tree. None supplies a semantic oracle for
the two owner-turn counterexamples.

## Architecture and minimality conclusions

- Runtime remains one owner-agent loop (`app/graph/owner_agent.py`) with one caller from
  `app/domain/owner_brain.py`; no production swarm or second owner agent was found.
- OwnerGraph and ClientGraph remain distinct and share the core capability/policy/store
  layers. Postgres remains the system of record. Sheets is a bounded owner tool, not a
  source of truth, and no runtime Drive spreadsheet discovery was found.
- Removed campaigns, executable LinkedIn analytics, and the duplicate website knowledge
  path were not reintroduced. Retained enum/metadata names are compatibility records, not
  live tools.
- No secret values were inspected or emitted. Configured model/provider chains are
  explicit, ordered, and observable through selected-model/error metadata; no unconfigured
  silent provider/model substitution or unsupported live capability claim was found.
- The exact 164-file matrix accounts for every current function-bearing app/scripts file
  once. The blockers are narrow input-binding defects and require no architecture redesign.

## Explicit non-claims

- This review did not inspect `.env`, secrets, AWS, production, or call any live Telegram,
  Gmail, Sheets, GA4, GSC, LinkedIn, Composio, model, or STT provider.
- It does not claim deployment, applied production migrations, production traffic,
  real-device voice behavior, live credentials/scopes/rate limits, or provider success.
- Passing fake-adapter tests does not prove live-provider semantics or observability.
- Historical evidence and the dirty worktree were preserved. `PLAN.md`, implementation,
  tests, docs, migrations, scripts, and the four leaf/node/root gates were not edited.

Only this ninth-review evidence file was created by this verifier.
