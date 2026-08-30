# Phase 1.5 function cleanup: twenty-second clean-room HEAVY review

Date: 2026-08-30

## Verdict

**FAIL.** The complete current dirty tree is not releasable. The review-21 persistent
126-case Unicode matrix is repaired exactly, but the positive Sheets mutation grammar
still discards arbitrary pre-verb text. A fresh full-`execute_tool` probe found **24**
mutation-like, mixed-script, punctuation/spacing, sentinel-confusable, or malformed
prefixes that were ignored while a later valid suffix crossed all four counted effects:
one live-port construction, one operation claim, one persisted `IdempotencyRow`, and one
`FakeSheetsPort` mutation. This is a structural whole-turn authority defect, not another
finite Unicode-blacklist gap. No completion gate was changed.

## Scope and boundaries

This was an independent verifier pass over the complete current dirty tree after the
twenty-first-review repair. Required sources were read in this exact order:

1. `AGENTS.md`
2. `docs/PRODUCT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DECISIONS.md` (all current lines, read in bounded chunks)
5. `PLAN.md`
6. `gates/root.md`
7. `gates/node-1.5.md`
8. `gates/leaf-1.5.4-function-cleanup.md`
9. `gates/leaf-1.5.4f-final-review.md`
10. `gates/evidence/function-cleanup-synthesis.md`
11. `gates/evidence/function-cleanup-repair-verification.md`
12. `gates/evidence/function-cleanup-heavy-twenty-first-review.md`
13. `gates/evidence/function-cleanup-twenty-first-repair.md`
14. Current task code, tests, status, and diff

No `.env`, archive, secret value, network, provider, AWS, commit, push, deployment, or
production action was used. Product code and committed tests were read-only. Disposable
fail-capable probes live only under ignored `.heavy-review-22/` and `.pytest-review22-*`
paths.

## Findings

### P0

None found.

### P1 — suffix-only positive grammar discards arbitrary mutation-bearing prefixes

Exact code evidence:

- `app/tools/registries/owner_tools.py:797-814` authorizes when the normalized view has
  one known requested verb and no known opposite/negation. It does not prove that the
  rest of the owner turn contains only harmless preface plus one complete mutation.
- `app/tools/registries/owner_tools.py:843-865` makes an NFKD/mark/control/casefold
  security view. That catches compatibility equivalents but intentionally is not a
  Unicode confusable skeleton and cannot classify arbitrary raw words or phrases.
- `app/tools/registries/owner_tools.py:1076-1083` rejects only the known readable/private
  sentinel vocabulary outside JSON strings.
- `app/tools/registries/owner_tools.py:1126-1137` builds the role view, finds the one
  recognized verb, and then assigns `clause = clause_view[verb.end():]`. Everything before
  that recognized verb is discarded.
- `app/tools/registries/owner_tools.py:1138-1144` full-matches only that suffix. Therefore
  an arbitrary prefix can be mutation-bearing, visually confusable, malformed, or
  value-like and still have no effect on authorization.
- The resulting `True` reaches `app/tools/registries/owner_tools.py:714-784`: port
  construction at line 747, operation claim at line 757, capability/provider mutation at
  lines 762-770, and persisted completion at line 784.

The 42-case independent probe classified harmless prefaces separately. The ordinary
`frobnicate blue; ...` and `nonsense; ...` prefaces were observed as accepted controls.
However, **24 denial-expected prefixes** also returned success and each produced exact
effect delta `(port=1, claim=1, idempotency_row=1, provider_mutation=1)`:

- **Nine mixed-script earlier mutation lookalikes:** five Cyrillic substitutions across
  `append`, `add`, `update`, `fill`, and `enter`; two Greek substitutions for `append` and
  `add`; and two mixed Latin/Hebrew variants before later valid Hebrew clauses.
- **Three readable-sentinel confusables:** Cyrillic/Greek substitutions in visually
  plausible `CELL`, `ID`, and `TARGET` prefixes.
- **Seven split operation forms:** `App end`, `App-end`, `App.end`, `App/end`, `App_end`,
  `App<U+00A0>end`, and `App<U+202F>end` before a later valid append clause.
- **Five structural/multilingual prefixes:** `change the blue cells`, `put blue there`,
  a raw value-like `blue`, Hebrew `שנה כחול`, and mixed English/Hebrew mutation phrasing.

Representative effectful requests were:

```text
App<U+0435 CYRILLIC SMALL LETTER IE>nd blue; Append "x" to sheet-main at KPI!A1 in the Sheet
T<U+0391 GREEK CAPITAL LETTER ALPHA>RGET; Append "x" to sheet-main at KPI!A1 in the Sheet
App-end blue; Append "x" to sheet-main at KPI!A1 in the Sheet
change the blue cells; Append "x" to sheet-main at KPI!A1 in the Sheet
שנה כחול; הוסף "x" לגיליון sheet-main בטווח KPI!A1
```

The same probe proved that target-like (`KPI!B2`) and quoted-value prefixes deny with zero
effects, and that a valid clause followed by an invalid suffix denies. The asymmetry is the
defect: prefix authority is open-ended while suffix authority is full-matched. Since any
unknown raw word/phrase may currently be treated as harmless, another finite confusable,
verb, sentinel, punctuation, or language blacklist cannot establish a complete exact
whole-turn authority contract. Repair must change the structural grammar boundary (for
example, match an entire permitted request shape with an explicitly bounded harmless
preface), not extend the blacklist.

### P2

None found independently.

The probe explicitly determined that sole compatibility-obscured operations are
authorized: full-width `Ａｐｐｅｎｄ`, mathematical-bold `𝐀𝐩𝐩𝐞𝐧𝐝`, marked
`App<U+034F>end`, and soft-hyphen `App<U+00AD>end` each reached the four effects. This is
not reported as a separate P2 because it is the documented security-view contract: an
operation is recognized semantically after compatibility/mark/control normalization,
while authority-bearing data stays raw-exact. Full-width spreadsheet ID, full-width A1
target, and a compatibility-equivalent but non-exact quoted literal all denied with zero
effects. Raw ID, target, literal, order, grid, and A1 binding therefore remain exact. The
unresolved P1 is that arbitrary text outside the recognized suffix is ignored, not that a
sole compatibility-equivalent action word is recognized.

### P3

None found. Current inventory and strict-C901 measurements match the repair evidence.

## Adversarial coverage and counts

### Review-21 matrix reproduction

The persistent full-path regression was rerun through `execute_tool` with the real owner
principal/capability policy, real operation ledger and `IdempotencyRow`, and
`FakeSheetsPort`:

- **126 total cases**.
- **103/103 denials** with zero deltas in all four effects.
- All **63** M*/Cf-hidden operation variants denied.
- The full-width duplicate and all **nine** obscured readable sentinels denied.
- **23/23 positives** passed and replayed idempotently.
- All **18** ID==target positives passed: values-first and target-first for five English
  and four Hebrew verbs.
- The five equal-ID/target extra/reversed-occurrence controls denied with zero effects.

The complete owner-live file also rechecked raw literals, order, duplicate multiplicity,
rectangular grids, A1 dimensions, full-width/raw Unicode literal preservation, quoted
negation/sentinels, extra targets, malformed JSON/container/scalar/bare tokens, and exact
ID/target role views: **26/26 passed**.

### Fresh review-22 probe

- **42 total cases**.
- **24 denial-expected cases failed closedness** and each crossed exactly the four effects.
- Compatibility duplicates (circled/math/ligature/true soft-hyphen/stacked-mark forms)
  that NFKD exposes denied with zero effects.
- Target-like and quoted-value prefixes plus invalid suffixes denied with zero effects.
- Harmless/unknown prefaces were observed separately as positives.
- Four sole compatibility-obscured operation forms authorized.
- Full-width ID, full-width target, and compatibility-only literal substitution denied
  with zero effects.

## Voice, Gmail, notifications, authority, and architecture

- `app/api/telegram.py:176-230` verifies webhook secret, kill switch, payload type, and
  numeric owner id before the canonical audio claim; the claim precedes download/STT.
  The exact suite covers duplicate success/failure routes, MIME/byte validation, the same
  OwnerGraph path, one text reply, and no graph call on media/STT failure.
- `app/agents/owner/graph.py:15-52` remains one three-node OwnerGraph;
  `app/graph/owner_agent.py:19-20` retains one bounded owner agent/model hop. ClientGraph
  requires a client principal at `app/agents/client/graph.py:58-75`. No runtime sub-agent
  or TTS/speech-output path was found.
- Gmail deferred and known-failed recovery passed in both execution orders: **2 passed +
  2 passed**. `app/domain/gmail_drafts.py:147-199` keeps demo/kill/config/binding/risk
  checks before the send claim, releases known provider failure, and retains completed
  send idempotency.
- The exact 19-file suite passed per-recipient finalization, hot-handoff, and due-reminder
  recovery. Source inspection confirms hot-handoff risk enforcement precedes takeover,
  inbox, recipient claims, and transport; only explicit rejection releases a recipient
  claim while ambiguous outcomes remain duplicate-safe.
- GA4, GSC, LinkedIn profile, and bounded Sheets remain named owner-only capabilities
  behind the request-derived principal, Python policy, and typed adapters. Owner-live
  fake-port tests passed distinct KPI reads, profile-only LinkedIn output, and client
  denial. Sheets remains an allowlisted operational surface; Postgres remains the system
  of record, with no Drive discovery, formula generation, arbitrary target selection, or
  client Sheets capability found.

No additional P0/P1/P2 issue was found in these non-Sheets seams.

## Mechanical commands and results

```powershell
uv --offline --cache-dir .uv-cache run pytest \
  tests/unit/test_owner_live_tools.py::test_owner_sheets_twenty_first_unicode_security_view_is_effect_free_on_denial \
  -q --basetemp .pytest-review22-matrix -p no:cacheprovider
```

Result: **1 passed**, reproducing the internal **126-case** matrix above.

```powershell
uv --offline --cache-dir .uv-cache run pytest .heavy-review-22/test_review22_probe.py \
  -q -s --basetemp .pytest-review22-probe-final -p no:cacheprovider
```

Result: **failed as designed** with **24 unsafe effectful cases** and exact four-effect
deltas.

```powershell
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q \
  --basetemp .pytest-review22-owner-live -p no:cacheprovider
```

Result: **26 passed**.

The exact 19-file command from `function-cleanup-repair-verification.md` was rerun with
the same ordered paths and a fresh workspace-local base temp.

Result: **332 passed, 326 warnings in 18.93s**.

The two Gmail recovery tests were run in both path orders with separate base temps.

Result: **2 passed + 2 passed**.

```powershell
uv --offline --cache-dir .uv-cache run pytest --collect-only -p no:cacheprovider
uv --offline --cache-dir .uv-cache run pytest --basetemp .pytest-review22-full \
  -p no:cacheprovider --disable-warnings
```

Results: **2,474 collected**; **2,474 passed, 1,955 warnings in 98.22s**.

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts \
  --output-format concise --exit-zero
uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
uv --offline --cache-dir .uv-cache run python .heavy-review-22/inventory.py
git diff --check
```

Results:

- Whole-tree Ruff: **All checks passed**.
- Strict C901: **36 findings**.
- Origin binding: **`origin-bind: ok`**.
- Deterministic eval diff: **273/273** across sales 51, buyer 43, calendar 20,
  website_handoff 15, safety 20, objection 20, routing 20, extract 30, writing 33, and
  gold 21.
- Function inventory: **164** current function-bearing files and **164** audit rows,
  zero missing/extra/duplicate paths, **1,646** definition lines, **42,529** physical
  lines, and **37,797** nonblank lines.
- Diff check: exit 0; repository-wide LF-to-CRLF warnings only.

## Gate decision and non-claims

Because one structural P1 boundary remains, the independent completion items in
`gates/leaf-1.5.4f-final-review.md`, `gates/leaf-1.5.4-function-cleanup.md`,
`gates/node-1.5.md`, and root G7 remain unchecked. Root G2/G3 were not checked, root G6
was not altered, and no other gate text changed. This evidence makes no AWS, deployment,
live Telegram, live Sheets/provider, credential, production-concurrency, commit, push, or
release claim.
