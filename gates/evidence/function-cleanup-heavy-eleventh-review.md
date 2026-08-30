VERDICT FAIL

# Function cleanup — fresh HEAVY eleventh review

Date: 2026-08-28

Scope: clean-room review of the current dirty Mia tree after the tenth-review Unicode
repair and subsequent format-control hardening. The tree was preserved. This review
created only this evidence file and did not edit the four review gates because two P1
findings remain.

## Findings

### P1 — canonical normalization changes quoted cell literals before exact binding

- Code: `app/tools/registries/owner_tools.py:876-884` applies NFKC to the entire owner
  message and then derives the quoted-literal multiset from that normalized text.
- Contract violated: quoted marked/control-containing JSON cell literals must remain inert
  and exact. Normalization may help target matching, but it may not make a model-proposed
  cell value equivalent to a different sequence actually quoted by the owner.
- Independent real-boundary reproduction: current owner tool registry, numeric owner
  principal, real SQLite `LeadStore`, actual `FakeSheetsPort`, plus counters around
  `_owner_sheets_port` and `claim_operation`.

```text
owner text: Please append "e\u0301" to sheet-allowed at KPI!A1 in the Sheet
tool value: "\u00e9"
result: ok=true; port_delta=1; claim_delta=1; operation_delta=1; row_delta=1
recorded operation: ["append", "sheet-allowed", "KPI!A1", [["\u00e9"]]]
```

The owner quoted the two-code-point sequence `U+0065 U+0301`; the provider received the
distinct precomposed value `U+00E9`. A binding rejection was required before adapter,
claim, idempotency persistence, or provider effect. All four boundaries were crossed.

### P1 — repeated/mixed target introducers are accepted as one selected target

- Code: `app/tools/registries/owner_tools.py:903-945` finds exactly one
  introducer-plus-target match, blanks only the target span, and rejects other A1 tokens.
  It never rejects another bare target introducer left beside the selected match.
- Contract violated: one exact unquoted bounded A1 target must occur once after one
  approved introducer; repeated introducers are ambiguous and must deny before effects.
- Independent reproductions used the same real registry/store/counter/FakeSheetsPort
  harness as the first finding.

```text
Please append "x" to sheet-allowed at at KPI!A1 in the Sheet
  -> ok=true; port_delta=1; claim_delta=1; operation_delta=1; row_delta=1

Please append "x" to sheet-allowed at range KPI!A1 in the Sheet
  -> ok=true; port_delta=1; claim_delta=1; operation_delta=1; row_delta=1

recorded operation for each: ["append", "sheet-allowed", "KPI!A1", [["x"]]]
```

No additional P0/P1/P2 was confirmed.

## Mandatory lens results

- Sheets denial normalization: `app/tools/registries/owner_tools.py:796-854` masks quoted
  JSON strings before operation classification, then NFKD-strips every Unicode `M*`
  category and `Cf` for denial-only matching. Focused adversarial tests cover ordinary
  niqqud/cantillation, class-zero `U+034F`, `Mn`/`Mc`/`Me`, variation selectors, mixed
  marks, ZWJ/ZWNJ, LRM/RLM, word joiner, zero-width space, soft hyphen, quoted inertness,
  and embedded-word non-denial. Those cases remained green. The first P1 is later in the
  exact literal-binding stage and is not cured by this ordering.
- Sheets target/operation validation: aside from the two findings, the real-boundary and
  focused tests retained exact operation binding, negation/conflict denial, one allowlisted
  spreadsheet id, rejection of other bare/bang-qualified A1 tokens (including punctuation,
  newline, conjunction, repetition and suffix-selection forms), quoted target inertness,
  selected range-endpoint handling, case/spaced-tab positives, formula/empty/shape/range/
  allowlist checks before effects, correction/replay/idempotency, and downstream RAW writes.
  Green aggregate tests do not supersede either direct mutation reproduction.
- Telegram: numeric authorization precedes canonical media claim; the claim precedes
  download/STT; only the exact received audio claim is forwarded. Duplicate success and
  failure have zero repeated effects. The suite covers forged preclaims, malformed and
  alternate media returns, non-bytes/empty/oversize/MIME denial, classified exceptions,
  cancellation propagation, path/host/HTTP/detail secrecy, and one OwnerGraph text reply.
  No TTS implementation was found.
- Gmail: numeric callback authorization precedes approval/Gmail construction. Binding,
  deferral, known-failure release/retry, and completed-send dedupe remained green. The two
  recovery tests returned `[0, 0, 0, 0]` in one Python process in A/B/B/A order.
- Notifications/finalization/HANDOFF: local tests and source inspection retain durable
  recipient-specific claims, explicit-rejection release, ambiguous-result retention,
  missing-configuration recovery, legacy conversation/day compatibility, empty returning-
  session isolation, HANDOFF early return, and kill-before-effect ordering. No duplicate
  owner-card transition was reproduced.

## Independent checks

All checks were local. Pytest disabled its cache provider and used workspace-local
basetemps.

```text
Focused Sheets/principal suite: 101 passed
Exact required 19-file suite:    322 passed
Full pytest tree:                2,464 passed, 1,955 warnings
Collection cross-check:          2,464 tests collected
Ruff app/tests/scripts:          All checks passed
Origin binding:                  origin-bind: ok
Deterministic evals:             273/273 across all ten families
Strict app/scripts C901:         36 findings (measurement exit 1)
git diff --check:                exit 0; line-ending warnings only
Gmail same-process A/B/B/A:      [0, 0, 0, 0]
```

The deterministic eval totals were independently reconciled as sales 51, buyer 43,
calendar 20, website handoff 15, safety 20, objection 20, routing 20, extraction 30,
writing 33, and gold 21.

## Audit reconciliation and architecture/minimality

Independent AST, line, and matrix parsing returned:

```text
function-bearing app/scripts files = 164
definitions                       = 1,635
physical lines                    = 42,274
nonblank lines                    = 37,571
strict C901 findings              = 36
matrix rows / unique paths        = 164 / 164
missing / extra / duplicate paths = 0 / 0 / 0
```

All 164 audit rows across the API, domain, and infrastructure matrices were reconciled;
there are no coverage gaps or duplicate paths.

Current source retains one production owner-agent definition and one caller, two compiled
graphs (OwnerGraph and ClientGraph) with shared core, explicit request-derived principals,
ClientGraph owner-capability denial, and Postgres as system of record. Sheets is pinned by
allowlisted id, has no runtime Drive discovery, and is never read back as Mia truth.
LinkedIn remains profile-only. Campaign analysis/pacing/prelaunch remain non-executable
`SPECIFIED` catalog entries with empty ports, not live campaign ownership. No duplicate
knowledge-retrieval owner, TTS surface, secret fallback, live-provider claim, or executable
removed analytics/campaign surface was found. ADR-031/032 and the current PRODUCT/
ARCHITECTURE contracts remain aligned apart from the two concrete Sheets binding defects.

Minimality review found the tenth/Cf change confined to the denial normalizer and its
tests. The remaining defects require narrow binding corrections; they do not justify a
graph, provider, persistence, or architecture redesign.

## Evidence boundary and nonclaims

- This is current local dirty-tree evidence, not deployed-image, AWS, ECS, RDS/Postgres,
  migration-application, concurrency, performance, or real-device proof.
- No `.env`, secret value, AWS resource, deployment state, or live Telegram, Gmail,
  Sheets, GA4, GSC, LinkedIn, Composio, model, or STT provider was inspected or called.
- The full suite's warnings were observed but not treated as proof of live behavior.
- No claim is made that the two P1 defects are repaired. Until both deny before adapter
  and claim and receive regression coverage, leaf 1.5.4f, leaf 1.5.4, node 1.5, and root
  remain unverified; their gate files were intentionally unchanged.
