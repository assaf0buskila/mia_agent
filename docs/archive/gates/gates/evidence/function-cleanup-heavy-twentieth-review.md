# Phase 1.5 function cleanup: twentieth clean-room HEAVY review

Date: 2026-08-30

## Verdict

**FAIL.** The complete current dirty tree is not releasable. A fresh full-tool-path
probe reproduced one P1 structural Sheets-authority defect across **20** English,
Hebrew, cross-language, and readable-sentinel variants. Each accepted variant caused
exactly one live-port construction, one operation claim, one persisted idempotency row,
and one fake-provider mutation. One P2 Hebrew target-first `spreadsheet_id == target`
positive is also denied. No gate status was changed.

## Scope and boundaries

This was an independent verifier pass over the complete current dirty tree after the
nineteenth-review structural repair. The required sources were read in this order:

1. `AGENTS.md`
2. `docs/PRODUCT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DECISIONS.md` (all 947 lines, chunked to avoid output truncation)
5. `PLAN.md`
6. `gates/root.md`
7. `gates/node-1.5.md`
8. `gates/leaf-1.5.4-function-cleanup.md`
9. `gates/leaf-1.5.4f-final-review.md`
10. `gates/evidence/function-cleanup-synthesis.md`
11. `gates/evidence/function-cleanup-repair-verification.md`
12. `gates/evidence/function-cleanup-nineteenth-repair.md`
13. Current task-specific code, tests, and diff

No `.env`, archived document, secret value, network, provider, AWS, commit, push,
deployment, or production action was used. Production still has one OwnerGraph owner
agent; the disposable verifier under `tests/.heavy-review-twentieth/` is ignored test
material and is not a runtime agent or production change.

## Findings

### P0

None found.

### P1 — scanning every verb accepts a later valid suffix and discards an earlier unauthorized clause

Exact evidence:

- `app/tools/registries/owner_tools.py:1099-1106` iterates over **every** matching
  English and Hebrew operation verb, slices `view[verb.end():]`, and returns `True` if
  any later suffix full-matches. It does not require exactly one operation verb or bind
  the accepted suffix to the complete mutation-bearing portion of the turn.
- `app/tools/registries/owner_tools.py:822-835` records only boolean presence for the
  requested operation. Multiple affirmative mentions of the same operation remain
  `affirmative=True` and are not ambiguous.
- `app/tools/registries/owner_tools.py:1068-1071` rejects raw private-use sentinel
  boundaries globally, but readable `CELL` / `ID` / `TARGET` lookalikes are left in the
  prefix that the later-verb scan discards.
- The unsafe `True` result passes directly through `app/tools/registries/owner_tools.py:714-747`,
  constructs the Sheet port, claims at lines 750-757, executes the capability/provider
  at lines 761-770, and persists completion at lines 784-786.

The counted matrix contained **28 denial-expected cases**. **20 were effectful** and
**8 were effect-free**. Every effectful case had the same delta:
`port +1, claim +1, idempotency row +1, fake-provider mutation +1`.

Effectful classes:

- 5/5 English earlier malformed same-operation clauses: `append`, `add`, `update`,
  `fill`, and `enter`.
- 4/4 Hebrew earlier malformed same-operation clauses: `הוסף`, `הכנס`, `עדכן`, and
  `מלא`.
- 1/1 cross-language earlier malformed clause followed by a valid English suffix.
- 1/1 same-operation verb in prefix (`Update later; now update ...`).
- 9/9 raw readable prefix collisions: `CELL/cell/CeLl`, `ID/id/iD`, and
  `TARGET/target/TaRgEt`.

Representative reproduced mutations:

```text
Append blue; Append "x" to sheet-main at KPI!A1 in the Sheet
הוסף כחול; הוסף "x" לגיליון sheet-main בטווח KPI!A1
הוסף כחול; Append "x" to sheet-main at KPI!A1 in the Sheet
CELL; Append "x" to sheet-main at KPI!A1 in the Sheet
```

In every case the tool payload contained only `[["x"]]`; the earlier requested raw
clause/token was silently ignored while the later selected subset mutated the Sheet.
This violates the exact authenticated-owner statement and no-model-selected-subset
contract. The required repair boundary is structural: exactly one requested operation
occurrence must own the one complete accepted clause; scanning later occurrences may
not discard an earlier mutation occurrence or collision token.

The 8 effect-free controls were: suffix prose after a valid clause, an opposite-operation
prefix, a negated prefix, and five raw private-use boundary/sequence cases (`U+E000`,
`U+E001`, and the injected C/I/T-shaped sequences). Quoted readable lookalikes and quoted
private-use text remained accepted as exact data, which is correct.

### P2 — Hebrew target-first ID==target positive is rejected

Exact evidence:

- When `spreadsheet_id == a1_range`, `app/tools/registries/owner_tools.py:1072-1075`
  blindly replaces the first raw occurrence as the ID and the next as the target.
- Hebrew target-first grammar at `app/tools/registries/owner_tools.py:1096-1098` expects
  the opposite semantic order: target first, then spreadsheet ID.

The exact valid owner statement `הוסף את A1 בגיליון A1 ב-"x"` with
`spreadsheet_id="A1"`, `range="A1"`, and `values=[["x"]]` was denied before effects.
English values-first ID==target and ordinary EN/HE values-first/target-first controls
remain covered and pass. The overlap binder must assign ID and target sentinels by the
matched grammatical roles, not by unconditional raw occurrence order.

### P3 — strict-C901 evidence is stale by one

`gates/leaf-1.5.4f-final-review.md:8` and `gates/node-1.5.md:12` record **37** strict
C901 findings. A fresh `ruff check --select C901 app scripts --exit-zero` reports
`Found 36 errors.` The favorable reduction does not change the FAIL verdict, but the
current metric should be corrected in the next repair evidence instead of preserving a
known stale count.

## Adversarial probe coverage

The disposable probe was deliberately fail-capable and exercised `execute_tool`, the
real owner binder, real capability/policy path, real operation ledger, and
`FakeSheetsPort`; it did not call a provider.

- Focused 17-case run: **8 failures, 9 passes**.
  - Seven failures were effectful safety cases (three earlier-clause variants, one
    operation-prefix variant, and three readable-sentinel-prefix variants).
  - One failure was the P2 Hebrew ID==target positive denial.
  - Five raw private-use collisions denied; quoted readable/private-use lookalikes and
    harmless prefix prose succeeded; the two-valid-clause control denied.
- Counted 28-case denial matrix: **20 effectful, 8 effect-free**, with all four effects
  counted independently for every case.
- Existing current-tree owner-live parser suite: **24 passed**. It covers the historical
  negation, Unicode marks/format controls, quoted JSON/container/scalar/bare-token,
  malformed quote/closer, exact ID/target multiplicity, values-first/target-first EN+HE,
  exact order, duplicate multiplicity, rectangular grid, A1 dimension, and no-subset
  cases. The new earlier-verb/prefix transition is absent from that green suite.
- Exact values/order/shape inspection: `app/tools/registries/owner_tools.py:884-898`
  compares the complete raw decoded quoted sequence to row-major payload order and checks
  exact target dimensions. `app/integrations/sheets.py:183-203` rejects non-string,
  empty, oversized, and formula-leading cells, and lines 494-524 send `RAW` values.

## Voice, Gmail, notification, authority, and architecture recheck

- Telegram webhook secret verification and kill switch occur before payload/media work
  (`app/api/telegram.py:176-195`); numeric owner authorization is checked at lines
  208-212; the canonical audio webhook claim occurs at lines 215-227 before download/STT
  at lines 228-230. Duplicate voice routes, byte/MIME enforcement, STT, same OwnerGraph
  path, and text-only reply tests are included in the 330-test exact suite.
- Gmail deferred/failed-send recovery passed in both test orders: **2 passed + 2 passed**.
  Kill/demo/write-flag/approval binding precede the send claim in
  `app/domain/gmail_drafts.py:147-190`; known failure releases the claim and completion
  remains idempotent at lines 190-200.
- Per-recipient finalization, hot-handoff, and due-reminder recovery tests are included in
  the exact suite. Source inspection confirms hot-handoff risk enforcement precedes
  takeover/inbox/recipient claims, known rejection alone releases a recipient, and
  ambiguous transport outcomes retain their claims.
- `app/agents/owner/graph.py` remains the single OwnerGraph. Client authority is explicit
  in `app/agents/client/graph.py:58-75`; owner tools are not available to ClientGraph.
  `app/graph/owner_agent.py:19-20` retains one owner agent/model hop and no sub-agents.
  No TTS/speech-synthesis output surface was found; Telegram voice is input-only.
- Sheets remains bounded operational state, not a system of record. The exact suite
  includes the regression proving sales state loads from Postgres rather than fake Sheet
  data.

No additional P0/P1/P2 issue was found in these non-Sheets seams.

## Mechanical results

1. Exact 19-file suite from `function-cleanup-repair-verification.md`:
   **330 passed, 326 warnings in 10.40s**.
2. `tests/unit/test_owner_live_tools.py`: **24 passed, 2 warnings in 2.93s**.
3. Full current pytest with workspace-local base temp and no cache provider:
   **2,472 passed, 1,955 warnings in 57.84s**. Independent collection also reported
   **2,472 tests collected**.
4. Whole-tree Ruff (`app tests scripts`): **All checks passed**.
5. Origin binding: **`origin-bind: ok`**.
6. Deterministic eval diff: **273/273** across sales 51, buyer 43, calendar 20,
   website_handoff 15, safety 20, objection 20, routing 20, extract 30, writing 33,
   and gold 21.
7. `git diff --check`: exit 0; Windows LF-to-CRLF warnings only.
8. Independent audit reconciliation:
   - current function-bearing files: **164**
   - audit rows / unique audit rows: **164 / 164**
   - duplicates / missing / extra: **0 / 0 / 0**
   - definition lines: **1,642**
   - physical lines: **42,459**
   - nonblank lines: **37,736**
   - strict C901 findings: **36**

## Gate decision and non-claims

Because P1 and P2 remain, the independent-review/completion items in
`gates/leaf-1.5.4f-final-review.md`, `gates/leaf-1.5.4-function-cleanup.md`,
`gates/node-1.5.md`, and `gates/root.md` remain unchecked. Root live G2/G3 were not
touched. This evidence makes no deployment, live Telegram, live Sheets, provider,
credential, AWS, production-concurrency, commit, or release claim.
