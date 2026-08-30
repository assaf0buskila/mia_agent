# Phase 1.5 function cleanup: twenty-first clean-room HEAVY review

Date: 2026-08-30

## Verdict

**FAIL.** The complete current dirty tree is not releasable. A fresh full-tool-path
matrix reproduced two related P1 Unicode security-view gaps in the owner Sheets write
binder. Sixty-four visually equivalent earlier EN/HE operation occurrences and nine
visually equivalent readable-sentinel occurrences were discarded while a later valid
clause crossed all four counted effects. Every accepted denial case caused exactly one
live-port construction, one operation claim, one persisted idempotency row, and one
fake-provider mutation. No completion gate was changed.

## Scope and boundaries

This was an independent verifier pass over the complete current dirty tree after the
twentieth-review repair. Required sources were read in this exact order:

1. `AGENTS.md`
2. `docs/PRODUCT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DECISIONS.md` (all 946 current lines, read in bounded chunks)
5. `PLAN.md`
6. `gates/root.md`
7. `gates/node-1.5.md`
8. `gates/leaf-1.5.4-function-cleanup.md`
9. `gates/leaf-1.5.4f-final-review.md`
10. `gates/evidence/function-cleanup-synthesis.md`
11. `gates/evidence/function-cleanup-repair-verification.md`
12. `gates/evidence/function-cleanup-heavy-twentieth-review.md`
13. `gates/evidence/function-cleanup-twentieth-repair.md`
14. Current task-specific code, tests, status, and diff

No `.env`, archive, secret value, network, provider, AWS, commit, push, deployment, or
production action was used. Code and committed tests were read-only. Disposable probes
live only under ignored `.heavy-review-*` / `.pytest-*` directories and are not product
or gate changes.

## Findings

### P0

None found.

### P1 — Unicode-inert operation characters defeat exactly-one-operation counting

Exact code evidence:

- `app/tools/registries/owner_tools.py:797-815` masks JSON strings and lowercases the
  request, but does not build a mark/control/compatibility-normalized security view for
  operation counting.
- `app/tools/registries/owner_tools.py:823-835` counts only raw exact EN/HE operation
  regex matches. The NFKD plus `M*`/`Cf` removal at lines 847-857 is used only by the
  negation detector; it does not protect operation occurrence counting.
- `app/tools/registries/owner_tools.py:1106-1115` repeats the raw exact-verb scan in the
  positive grammar and then slices from the one later exact verb. An earlier visually
  equivalent operation containing an inert mark/control is therefore absent from both
  counts, and the later valid suffix owns the mutation.
- The unsafe `True` reaches `app/tools/registries/owner_tools.py:716-747`, constructs the
  port at line 747, claims at line 757, calls capability/provider code at lines 762-770,
  and persists completion at line 784.

The counted probe inserted each of seven visually inert Unicode characters inside every
supported English and Hebrew operation synonym:

```text
U+200D ZERO WIDTH JOINER
U+200C ZERO WIDTH NON-JOINER
U+200E LEFT-TO-RIGHT MARK
U+2060 WORD JOINER
U+FEFF ZERO WIDTH NO-BREAK SPACE
U+034F COMBINING GRAPHEME JOINER
U+0301 COMBINING ACUTE ACCENT
```

That is 7 x 9 = **63 effectful duplicate-operation cases** across `append`, `add`,
`update`, `fill`, `enter`, `הוסף`, `הכנס`, `עדכן`, and `מלא`. One compatibility-equivalent
full-width `Ａｐｐｅｎｄ` prefix also mutated. Representative requests were:

```text
App<U+200D>end blue; Append "x" to sheet-main at KPI!A1 in the Sheet
הו<U+034F>סף כחול; הוסף "x" לגיליון sheet-main בטווח KPI!A1
Ａｐｐｅｎｄ blue; Append "x" to sheet-main at KPI!A1 in the Sheet
```

Each produced delta `(port=1, claim=1, idempotency_row=1, provider_mutation=1)`.
This is the same forbidden earlier-clause/subset transition as review 20, now reached
through a Unicode security view the repair did not normalize. Quoted values were masked
before counting, and raw target/value codepoints were not normalized by the probe.

### P1 — visually equivalent readable sentinels bypass the raw-token collision guard

Exact code evidence:

- `app/tools/registries/owner_tools.py:1051` recognizes only exact raw `CELL`, `ID`, or
  `TARGET` codepoints with case-insensitive word boundaries.
- `app/tools/registries/owner_tools.py:1058-1065` masks JSON strings, then tests exact
  private-use boundaries and that raw readable regex. It does not remove visually inert
  `M*`/`Cf` characters or apply compatibility normalization to the security-only view.
- The prefix is then discarded by the one later valid verb at lines 1106-1115, reaching
  the same port/claim/idempotency/provider effects at lines 747-784.

The probe found **9 effectful readable-sentinel cases**:

```text
CE<U+200D>LL, I<U+200D>D, TAR<U+200D>GET
CE<U+034F>LL, I<U+034F>D, TAR<U+034F>GET
ＣＥＬＬ, ＩＤ, ＴＡＲＧＥＴ
```

Every one was outside JSON, prefixed the otherwise valid append clause, and produced
the same `(1,1,1,1)` effect delta. Exact `CELL`/`ID`/`TARGET` case variants,
punctuation-wrapped exact forms, raw U+E000/U+E001 private sentinels, and complete
private sequences denied with zero effects. Quoted exact readable/private lookalikes
remained literal data. Longer ordinary words `CELLULAR`, `myID`, and `TARGETS` did not
false-collide and all three valid writes succeeded.

### P2

None found. In particular, the review-20 equal-ID/target repair passed every supported
positive role assignment and did not admit an invented role or subset.

### P3

None found. Current inventory and strict-C901 measurements match the repair evidence.

## Adversarial probe coverage and counts

The disposable probe exercised the real `execute_tool` registry path, real principal and
capability policy, real operation ledger, `IdempotencyRow`, and `FakeSheetsPort`; it did
not call a provider.

- Total matrix: **126 cases**.
- Denial-expected: **103 cases**.
  - **73 effectful**: 63 marked/format-controlled EN/HE duplicate verbs, one full-width
    duplicate verb, and nine marked/format-controlled/full-width readable sentinels.
  - **30 effect-free**: exact same-operation/cross-language/three-verb/synonym duplicates,
    operation or negated prefixes, valid-before-invalid and invalid-before-valid forms,
    exact readable/private sentinels, and equal-ID/target extra-occurrence controls.
- Positive controls: **23/23 passed**.
  - `CELLULAR`, `myID`, `TARGETS`, one harmless preface, and quoted operation/sentinel data.
  - All **18** equal-ID/target positives: both values-first and target-first shapes for
    five English verbs and four Hebrew verbs.
- Equal-ID/target extra/reversed occurrence denials: **5/5** effect-free; no second target
  was authorized.
- Existing full owner-live parser file: **25/25 passed**. It rechecks exact literals,
  sequence, duplicate multiplicity, rectangular grid, A1 dimensions, raw Unicode literal
  preservation, quoted negation/sentinels, marked negation, and the historical malformed
  JSON/container/scalar/bare-token denial classes. Its green exact-token cases do not cover
  the newly reproduced operation/sentinel security-view normalization gap.

## Telegram voice, Gmail, notifications, authority, and architecture

- `app/api/telegram.py:176-195` verifies the webhook secret and kill switch before payload
  work. Numeric owner authorization occurs at lines 195-212. The canonical audio claim at
  lines 215-227 precedes download/STT at lines 228-230; downstream receives the exact
  preclaim at lines 269-272. The exact suite covers duplicate success/failure updates,
  MIME/byte checks, same OwnerGraph path, one text reply, and zero graph call on failure.
- `app/agents/owner/graph.py:15-52` remains one OwnerGraph with three named nodes;
  `app/graph/owner_agent.py:19-20` explicitly retains one bounded owner agent/model hop.
  `app/agents/client/graph.py:58-75` requires a client principal and lines 233-237 retain
  the distinct ClientGraph. No runtime sub-agent or TTS/speech-output path was found.
- Gmail deferred and known-failed recovery passed in both pair orders: **2 passed + 2
  passed**. `app/domain/gmail_drafts.py:147-190` keeps demo/kill/config/binding/risk checks
  before the send claim, releases a known provider failure, and preserves completed-send
  idempotency.
- The exact 19-file suite passed per-recipient finalization, hot-handoff, and due-reminder
  recovery. Source inspection confirms hot-handoff risk enforcement precedes takeover,
  inbox, recipient claims, and transport; only known rejection releases a recipient claim,
  while ambiguous outcomes remain duplicate-safe.
- GA4, GSC, LinkedIn profile, and bounded Sheets remain named capabilities behind a
  request-derived principal, Python policy, and typed adapters. Owner-live fake-port tests
  passed separate GA4/GSC KPI reads, LinkedIn profile-only output, and client denial.
- Sheets remains an allowlisted operational surface, not the system of record.
  `tests/unit/test_sheets.py:1335` proves sales state loads from Postgres rather than fake
  Sheet data; the exact suite ran that test. No Drive discovery, arbitrary Sheet selection,
  formula generation, or client Sheets capability was found.

No additional P0/P1/P2 issue was found in these non-Sheets seams.

## Mechanical commands and results

```powershell
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q `
  --basetemp .pytest-review21-owner-live -p no:cacheprovider
```

Result: **25 passed**.

The exact 19-file command from `function-cleanup-repair-verification.md` was rerun with
the same ordered paths and a fresh workspace-local base temp.

Result: **331 passed, 326 warnings in 17.72s**.

```powershell
uv --offline --cache-dir .uv-cache run pytest --collect-only -p no:cacheprovider
uv --offline --cache-dir .uv-cache run pytest --basetemp .pytest-review21-full `
  -p no:cacheprovider --disable-warnings
```

Results: **2,473 tests collected**; **2,473 passed, 1,955 warnings in 91.33s**.

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts `
  --output-format concise --exit-zero
uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
git diff --check
```

Results:

- Whole-tree Ruff: **All checks passed**.
- Strict C901: **36 findings**.
- Origin binding: **`origin-bind: ok`**.
- Deterministic eval diff: **273/273** across sales 51, buyer 43, calendar 20,
  website_handoff 15, safety 20, objection 20, routing 20, extract 30, writing 33,
  and gold 21.
- Diff check: exit 0; repository-wide LF-to-CRLF warnings only.

Independent AST and audit-matrix reconciliation:

```text
function-bearing app/scripts files = 164
definition lines                  = 1,645
physical lines                    = 42,507
nonblank lines                    = 37,778
audit rows / unique rows          = 164 / 164
duplicates / missing / extra      = 0 / 0 / 0
strict C901 findings              = 36
```

## Gate decision and non-claims

Because two P1 boundaries remain, the independent completion items in
`gates/leaf-1.5.4f-final-review.md`, `gates/leaf-1.5.4-function-cleanup.md`,
`gates/node-1.5.md`, and root G7 remain unchecked. Root G2/G3 and historical root G6
evidence were not touched. This evidence makes no AWS, deployment, live Telegram, live
Sheets/provider, credential, production-concurrency, commit, push, or release claim.
