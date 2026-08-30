VERDICT FAIL

# Phase 1.5 function cleanup — tenth HEAVY clean-room review

Date: 2026-08-28

Reviewer mode: fresh verifier; no implementation work

The mechanical gate and exact 164-file audit reconcile, but one independently reproduced
P1 Sheets authorization defect remains. Any unresolved P0/P1/P2 is a release failure, so
Phase 1.5 stays open and the four PASS-only gate files remain unchanged.

## Release-blocking finding

### P1 — class-zero Unicode marks inside standalone Hebrew negators authorize a Sheets mutation

`app/tools/registries/owner_tools.py:845-850` normalizes the unquoted owner turn with NFKD
and removes a code point only when `unicodedata.combining(char)` is nonzero. That does not
remove every Unicode mark. For example, U+034F COMBINING GRAPHEME JOINER and U+FE0F
VARIATION SELECTOR-16 are category `Mn` but have canonical combining class zero. Inserted
between the Hebrew letters, they prevent the standalone `לא` / `אל` regular expression
from matching.

I called the real owner registry with a real SQLite `LeadStore`, a numeric owner
`Principal`, `sheet-allowed` as the allowlisted ID, counters around
`_owner_sheets_port` and `claim_operation`, and `FakeSheetsPort` only at the provider
boundary. The tool call in each case was:

```text
sheets_append(sheet-allowed, KPI!A1, [["x"]])
```

Representative prohibited owner turns:

```text
בבקשה ל\u034fא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1
בבקשה א\u034fל תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1
```

Exact isolated result for each fresh source event:

```text
ok=True
port constructions=1
claim calls=1
owner_sheets_write rows=1
provider operations=1
operation=('append', 'sheet-allowed', 'KPI!A1', [['x']])
```

The U+FE0F form of `לא` produced the same mutation. This directly falsifies the required
rule that pointed, cantillated, and unusual combining-mark variants of standalone Hebrew
negators deny before port construction, claim, idempotency persistence, and provider
effects. The maintained tests cover ordinary niqqud/cantillation because those marks have
nonzero combining classes; they do not cover class-zero Unicode marks.

Required closure: after JSON literals are masked, remove every Unicode mark category
(`Mn`, `Mc`, and `Me`) or otherwise canonicalize the Hebrew negator token independently
of combining class. Add real-registry cases for both `לא` and `אל` with U+034F and a
class-zero variation selector, plus quoted-value and embedded-word controls. Every denial
must prove zero port, claim, idempotency, and provider effects; corrected same-event and
exact replay behavior must remain green.

## Mandatory outcome lenses

- Sheets other than the finding: the real registry/store tests retain one distinct
  allowlisted ID, exact operation binding, one exact unquoted bounded A1 target, rejection
  of bare/bang-qualified secondary targets across punctuation/newline/conjunction forms,
  suffix-selection denial, quoted-target inertness, range-endpoint handling, uppercase/
  lowercase/mixed spaced-tab positives, exact JSON-literal multiplicity, normalized
  nonempty values, empty/formula/shape/range/allowlist denial before port/claim, RAW writes,
  correction of the same event, and successful replay idempotency. These do not close the
  P1 class-zero-mark counterexample.
- Telegram: numeric authorization occurs before the canonical audio webhook claim; the
  claim occurs before download/STT; only an exact existing `received` Telegram/audio claim
  can be reused by `process_owner_texts`. The recorded duplicate success/failure, forged
  preclaim, malformed media-return, non-bytes/empty/oversize/MIME, host/path/HTTP/detail,
  exception, and cancellation tests pass. Voice reaches the same OwnerGraph and produces
  one text response; no TTS surface was found.
- Gmail: the two recovery tests passed in one process in A→B→B→A order with return codes
  `[0, 0, 0, 0]`. Binding, deferred/known-failed recovery, and completed-send dedupe remain
  green locally.
- Notifications/finalization/HANDOFF: per-recipient claims, explicit-rejection release,
  ambiguous-result retention, missing-configuration recovery, legacy conversation/day
  compatibility, empty returning-session isolation, HANDOFF early return, and kill-before-
  effect tests pass. No duplicate owner-card transition was reproduced.
- Owner/client authority and provider scope: request-derived principals remain explicit;
  ClientGraph rejects owner trust. GA4/GSC are normalized owner reads, LinkedIn is
  profile-only, Sheets has no Drive discovery and is not the system of record. Postgres
  remains the system of record.

No additional P0/P1/P2 was confirmed.

## Independent checks and results

All local Python runs set `MIA_ENV=test`, used the workspace `.uv-cache`, and used a local
SQLite test DSN. Pytest disabled its cache provider and used workspace-local basetemps.

```text
Focused Sheets/principal suite: 101 passed
Exact required 19-file suite:    322 passed
Full pytest tree:                2,464 passed, 1,955 warnings
Ruff app/tests/scripts:          All checks passed
Origin binding:                  origin-bind: ok
Deterministic evals:             273/273 across all ten families
Strict app/scripts C901:         36 findings (expected measurement exit 1)
git diff --check:                exit 0; line-ending warnings only
Gmail same-process A/B/B/A:      [0, 0, 0, 0]
```

The green aggregate tests are necessary but do not supersede the direct P1 mutation.

## Inventory and evidence reconciliation

Independent AST, physical-line, and audit-matrix parsing returned:

```text
function-bearing app/scripts files = 164
definitions                       = 1,635
physical lines                    = 42,270
nonblank lines                    = 37,567
C901                              = 36
matrix rows / unique              = 164 / 164
missing / extra / duplicates      = 0 / 0 / 0
partition                         = 23 / 73 / 68
KEEP / SIMPLIFY / MERGE / REMOVE  = 139 / 24 / 1 / 0
```

All 164 current function-bearing production/script files are dispositioned exactly once.
No pre-cleanup physical/nonblank baseline is claimed.

## Architecture and minimality

Runtime remains one bounded owner-agent loop under ADR-031/032, one OwnerGraph and one
ClientGraph over shared core policy/capabilities/adapters, with thin channels and no
runtime sub-agent swarm. Removed campaign/pacing/prelaunch and executable LinkedIn-
analytics surfaces were not reintroduced; retained capability identifiers are status/
compatibility metadata. No second knowledge path, Sheets recovery source, new database,
provider, model fallback, TTS, or live-capability claim was found. The blocking repair is
a narrow Unicode-token hardening, not an architecture change.

## Non-claims and edit boundary

No `.env`, secret value, AWS resource, production database, deployment, or live Telegram,
Gmail, Sheets, GA4, GSC, LinkedIn, Composio, model, or STT provider was inspected or called.
SQLite/fake-adapter outcomes prove local ordering and authorization, not live credentials,
provider semantics, production concurrency, migration application, or real-device voice.

This review created only `gates/evidence/function-cleanup-heavy-tenth-review.md`. It did
not edit application code, tests, scripts, migrations, canonical docs, `PLAN.md`, prior
evidence, or any leaf/node/root gate.
