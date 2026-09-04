# Phase 1.5 function-cleanup synthesis

Date: 2026-08-28

## Exact scope and disposition coverage

The parent rebuilt the current `app/**/*.py` and `scripts/**/*.py` inventory from
function definitions and compared it with the three audit matrices. The baseline and
current tree both contain 164 function-bearing files out of 187 Python files. The
matrices contain 23 + 73 + 68 = 164 unique paths, with zero missing, extra, or
duplicate paths. Every row has one `KEEP`, `SIMPLIFY`, `MERGE`, or `REMOVE`
disposition plus symbol, caller/test, risk, and expected-benefit evidence.

## Actionable-finding reconciliation

All 21 ranked actionable findings were accepted because each had a bounded behavior
contract and a mechanical or focused-test verifier. None introduced another runtime
agent, provider, database, or trust path.

| Audit finding | Resolution | Evidence |
| --- | --- | --- |
| API 1 truthful Sheets prompt | Implemented bounded read/update/append wording and prohibitions | `function-cleanup-owner-boundary.md` |
| API 2 owner mint boundary | Implemented numeric allowlist check before claim/persistence | `function-cleanup-owner-boundary.md` |
| API 3 prospect adapter construction | Owner-only adapters now build only in the authenticated branch | `function-cleanup-owner-boundary.md` |
| API 4 exact parallel tool ceiling | Excess calls receive refused tool results and never execute | `function-cleanup-owner-boundary.md` |
| API 5 notification fan-out/retry | One multi-owner primitive; only confirmed rejection releases claims | `function-cleanup-delivery-client-trust.md` |
| API 6 legacy lookup/inactivity paths | Removed; ClientGraph owns both paths | `function-cleanup-delivery-client-trust.md` |
| API 7 explicit client authority | Website, prospect, and due-scan boundaries mint client principals; ClientGraph rejects owner trust | `function-cleanup-delivery-client-trust.md` |
| API 8 owner mechanics/health truth | Dead owner branches removed; Sheets readiness distinguishes mirror and allowlisted operations | owner-boundary and delivery/client-trust evidence |
| Domain 1 approval binding drift | One validator covers text, callback, Gmail decision, and Gmail send | `function-cleanup-approval-safety.md` |
| Domain 2 invalid website JSON truncation | Oversize proposal rejected before persistence | `function-cleanup-approval-safety.md` |
| Domain 3 consumed cancellation retry | Failed persistence releases the operation for one safe retry | `function-cleanup-approval-safety.md` |
| Domain 4 four dead symbols | Removed after fresh repository-wide reference searches | `function-cleanup-mechanical.md` |
| Domain 5 eight phrase helpers | Merged to one behavior-equivalent private matcher | `function-cleanup-mechanical.md` |
| Infra 1 due-scan notification claim | Confirmed full rejection releases; partial/ambiguous outcomes retain | `function-cleanup-delivery-client-trust.md` |
| Infra 2 deploy plaintext `--env` | Removed | `function-cleanup-mechanical.md` |
| Infra 3 arbitrary migration command | Runner pinned to `mia-migrate` | `function-cleanup-mechanical.md` |
| Infra 4 incomplete eval diff | Added calendar and routing families | `function-cleanup-mechanical.md` |
| Infra 5 split owner reply protocol | One async protocol and direct await | `function-cleanup-owner-boundary.md` |
| Infra 6 duplicate store counter | Removed after fresh reference search | `function-cleanup-mechanical.md` |
| Infra 7 rejected Sheets discovery remnants | Removed; explicit IDs remain mandatory | `function-cleanup-mechanical.md` |
| Infra 8 provider-key duplicate fallbacks | Removed with schema behavior preserved | `function-cleanup-mechanical.md` |

The audit `KEEP` conclusions remain deliberate: Postgres is still the system of
record; channels stay thin; policy, idempotency, provider pins, deterministic bilingual
lexicons, and the two-graph/shared-core shape remain. File length alone did not justify
splitting `owner.py`, `inbound.py`, the eval harness, or deterministic policy tables.

## Measured result

| Metric | Baseline | Final current tree |
| --- | ---: | ---: |
| Function-bearing production/script files | 164 | 164 |
| Definition lines | 1,622 | 1,646 |
| Strict Ruff C901 offenders | 37 | 36 |
| Exact owner-task phrase matcher bodies | 8 | 1 |
| Proven dead domain/brain/store symbols | 5 | 0 |
| Rejected Sheets discovery helpers | 2 | 0 |
| Duplicate Search Console same-key fallbacks | 4 | 0 |
| Eval families in `eval_diff` | 8 | 10 |
| Deploy plaintext-env/arbitrary-migration CLI options | 2 | 0 |

The improved C901 total and small net definition increase are intentional and honest.
Approval binding first raised strict C901 to 38; helper extraction returned it to the
37-function production/script baseline. Gmail callback recovery and durable
per-recipient notification recovery, conservative upgrade compatibility, and the
Sheets intent classifier, exact-target binder, and Telegram media helpers then produced a net
twenty-four necessary definition lines after the clean-room reviews, producing 1,646 rather than
the 1,622 baseline. The Telegram download extraction reduced strict C901 from the 37-function
baseline to 36; the positive Sheets mutation-clause grammar retains that improvement. The value of
this pass is the named dead/duplicate removals and stronger authority/idempotency seams,
not a cosmetic claim that every large policy function or total line count became smaller.

The pre-cleanup physical-line total was not captured before work began and cannot be
reconstructed from the already-dirty starting tree. It is therefore not claimed. The
repeatable final measurement is **42,537 physical lines** and **37,805 non-blank lines**
across the 164 function-bearing production/script files. The first clean-room review's
37,034 figure is close to the non-blank measure and must not be relabelled as a physical
baseline. This evidence retains only the before/after metrics that were actually captured.

## Post-review repair gate

The first clean-room HEAVY review failed with one P1, three P2, and two P3 findings. All
six were repaired before the final gate:

- Sheets mutations now bind the operation, exact spreadsheet id, exact bounded A1 range,
  and every cell as an exact JSON-quoted literal in the authenticated owner's current
  message. Vague and substring-collision requests fail before an idempotency claim or
  adapter call.
- A valid approved Gmail callback now enters the real send boundary. Disabled, demo, or
  killed execution defers without claiming; a known provider failure releases the send
  claim; a later valid replay retries; a completed send cannot duplicate.
- Kill-switch-suppressed website finalization returns before taking the durable delivery
  claim, so a kill-off retry can deliver once.
- A website HANDOFF uses its hot-handoff notification as the only owner-card path for that
  turn, including multi-owner fan-out.
- `process_owner_texts` filters numeric-authorized items before settings or default owner
  adapter construction.
- The final metrics above explicitly mark the missing physical baseline as unavailable.

The second clean-room HEAVY review then failed on two additional adversarial transitions,
which were also repaired:

- Sheets update and append are now operation-specific in English and Hebrew; target ids
  and ranges use complete-token boundaries; quoted values bind as a multiset, including
  duplicate multiplicity. The ambiguous Hebrew `הכנס` form is append-only.
- Hot-handoff delivery now has a durable claim per `(kind, lead, numeric owner)`. Missing
  configuration or blank text consumes no recipient claim; explicit rejection releases
  only that recipient; accepted and ambiguous recipients remain duplicate-safe. The local
  owner inbox row remains independent of transport claims.

The third clean-room HEAVY review found three final ordering/recovery defects, which were
also repaired:

- Sheets authorization and the shared pure bounds/allowlist/value validator now run before
  live-port construction and before the operation claim. A rejected request can therefore
  be corrected and replayed with the same event without being stranded by an in-flight row.
- Website finalization and due reminders now claim per notification instance and numeric
  recipient: conversation id for finalization and local day for due reminders. Missing
  configuration claims nothing; explicit rejection releases only that recipient; accepted
  and ambiguous outcomes stay duplicate-safe; a returning conversation remains distinct.
- Hot-handoff risk enforcement now runs before takeover state, follow-up cancellation,
  inbox rows, recipient claims, and transport. A killed handoff leaves all of them unchanged.

The fourth clean-room HEAVY review then found two P1 and one P2 semantic transition,
which were repaired and added to the regression boundary:

- Sheets mutation verbs are classified as affirmative or negated outside cell literals.
  Negated requested operations and conflicting affirmative operations fail closed in
  English and Hebrew, including natural inflected negative forms. The complete multiset
  of owner-quoted cells must equal the tool payload; neither subset nor superset may write.
- A legacy finalization claim is dual-read as accepted-or-ambiguous delivery for its exact
  historical conversation, so the new recipient ledger cannot resend it after migration.
  A legacy due-reminder claim suppresses only its same local day, preserving later days.
- Website message eligibility and inactivity aggregation bind both `lead_id` and
  `conversation_id`; an empty returning identity cannot borrow another session's message.

Parent repair verification passed **140** focused Sheets/finalization tests, **177**
focused approval/Gmail tests, **181** cross-cutting Sheets/HANDOFF/E2E tests, and a final
**287-test** combined repair/voice/migration suite after the fourth review repairs. The
final frozen integrated tree then passed **2,429 tests**, whole-tree Ruff, origin
binding, all ten deterministic eval families (**273/273**), exact 164-row inventory
reconciliation, and diff-check. A fifth clean-room HEAVY review remains required before
Phase 1.5 is accepted.

The eighteenth clean-room HEAVY review found one P1 malformed-cell class: **1,227**
unmatched closing-container variants (`]`, `}`, and adjacent sequences) were invisible to
the residual candidate scanner and reached port, claim, idempotency row, and fake provider.
The repair treats closing delimiters symmetrically with opening delimiters at bounded cell
positions and also denies JSON-like pseudo-cells there; valid quoted lookalikes remain inert.
Parent verification again passed **24** owner-live tests, the exact **330-test** 19-file
suite, and the full **2,472-test** tree; whole-tree Ruff, origin binding, **273/273** evals,
and diff-check pass. Current measurements are 1,640 definitions, 42,429 physical lines,
37,710 nonblank lines, and 36 strict C901 findings. A nineteenth clean-room HEAVY review
is required before Phase 1.5 is accepted.

The nineteenth clean-room HEAVY review found one P1 structural class: **1,000**
arbitrary unquoted bare-token variants could be ignored while quoted cells still reached
port, claim, idempotency row, and fake provider. The repair removes the finite token
blacklist and accepts only four complete positive mutation-clause shapes (English/Hebrew,
values-first/target-first) after exact JSON strings, the selected spreadsheet ID, and the
selected target are replaced with distinct internal private-use sentinels. Follow-ups remove
the former ID-side wildcard and reject raw readable or private-use sentinel collisions while
quoted lookalikes remain exact data. Parent verification passed **24** owner-live tests, the
exact **330-test** 19-file suite, and the full **2,472-test** tree; whole-tree Ruff, origin
binding, **273/273** evals, and diff-check pass. Current measurements are 1,642 definitions,
42,459 physical lines, 37,736 nonblank lines, and 36 strict C901 findings. A twentieth
clean-room HEAVY review is required before Phase 1.5 is accepted.

The twentieth clean-room HEAVY review found one P1 structural bypass and one P2 valid-input
denial. Twenty earlier-clause/readable-sentinel variants discarded an unauthorized prefix
and accepted a later valid same-operation suffix, crossing all four counted effects; Hebrew
target-first denied a valid write when the spreadsheet ID equalled the A1 target. The repair
requires exactly one requested EN/HE operation occurrence, rejects raw readable/private
sentinels outside JSON strings across the complete turn, and evaluates both semantic role
orders for equal ID/target tokens. Parent verification passed **25** owner-live tests, the
exact **331-test** 19-file suite, and the full **2,473-test** tree; whole-tree Ruff, origin
binding, **273/273** evals, and diff-check pass. Current measurements are 1,645 definitions,
42,507 physical lines, 37,778 nonblank lines, and 36 strict C901 findings. A twenty-first
clean-room HEAVY review is required before Phase 1.5 is accepted.

The twenty-first clean-room HEAVY review found two P1 Unicode security-view gaps. Sixty-four
M*/Cf/full-width earlier-operation variants and nine obscured readable-sentinel variants
evaded the raw counter/guard; all **73** reached port, claim, idempotency row, and fake
provider. The repair masks exact JSON strings, applies NFKD compatibility normalization,
removes every M*/Cf character, and casefolds only a security view used for operation,
negation, sentinel, and positive-clause matching. Raw ID, target, decoded literal, payload,
and provider data remain exact. Parent verification passed **26** owner-live tests, the
exact **332-test** 19-file suite, and the full **2,474-test** tree; whole-tree Ruff, origin
binding, **273/273** evals, and diff-check pass. Current measurements are 1,646 definitions,
42,529 physical lines, 37,797 nonblank lines, and 36 strict C901 findings. A twenty-second
clean-room HEAVY review is required before Phase 1.5 is accepted.

The twenty-second clean-room HEAVY review found one P1 whole-turn authority defect. Twenty-four
mixed-script, sentinel-confusable, split-operation, and multilingual mutation-bearing prefixes
were discarded while a later valid suffix reached all four effects. The structural repair
removes suffix-only matching and full-matches the complete security-view request: only bare,
`Please`, exact `Please record this now:`, or tested `אלופה` prefaces may precede one
supported operation and one complete bound clause. Parent verification passed **27**
owner-live tests, the exact **333-test** 19-file suite, and the full **2,475-test** tree;
whole-tree Ruff, origin binding, **273/273** evals, and diff-check pass. Current measurements
are 1,646 definitions, 42,537 physical lines, 37,805 nonblank lines, and 36 strict C901
findings. A twenty-third clean-room HEAVY review is required before Phase 1.5 is accepted.

The twenty-third clean-room HEAVY review passes with no unresolved P0/P1/P2/P3. It
independently reproduces the persistent **126-case** and **42-case** full-path matrices and
adds a fresh **168-case** whole-turn probe. Across all three, **246** denials remain before
port, claim, idempotency row, and provider, while **90** positives replay with one durable
row and one provider mutation. The reviewer also reran the full **2,475-test** tree,
whole-tree Ruff, origin binding, **273/273** evals, diff-check, and exact 164/164 inventory.
Phase 1.5 is accepted; production deployment and live Telegram/provider proof remain
separate root gates.

The fifth clean-room HEAVY review found two remaining P1 Sheets transitions. A message
that named more than one complete allowlisted target still let the model select one, and
a JSON-quoted whitespace-only cell normalized to an empty string and reached the provider.
Both are now repaired before any adapter construction or operation claim: the binder
requires exactly one unquoted allowlisted spreadsheet id and one unquoted bounded A1
target, while the shared value validator rejects cells that trim to empty and preserves
internal spaces in non-empty values. Parent verification on this repaired tree passed
**98** focused Sheets/principal tests, a **289-test** combined repair/voice/migration
suite, the full **2,431-test** tree, whole-tree Ruff, origin binding, all ten deterministic
eval families (**273/273**), exact 164-row inventory reconciliation, and diff-check. A
sixth clean-room HEAVY review remains required before Phase 1.5 is accepted.

The sixth clean-room HEAVY review then reproduced two further P1 authorization defects:
bare English `not append`/`not update` remained affirmative, and spaced tab names were
reconstructed from only their final word, allowing two owner-stated targets to collapse
and the model to select an unstated suffix tab. The repair classifies standalone `not` as
negative-only and extracts one complete bounded A1 target, including its full spaced tab
prefix, after an explicit target introducer. The negative classifier also covers bounded
`to`/`ever` modifiers while quoted cell data remains inert. Suffix selection, repeated targets, and
multiple targets now fail before port construction or claiming; an exact spaced target
succeeds once and replays idempotently. Parent verification on this repaired tree passed
**99** focused Sheets/principal tests, the exact **290-test** 19-file combined suite, the
full **2,432-test** tree, whole-tree Ruff, origin binding, all ten deterministic eval
families (**273/273**), exact 164-row inventory reconciliation, and diff-check. A seventh
clean-room HEAVY review remains required before Phase 1.5 is accepted.

The seventh clean-room HEAVY review found one P1 and two P2 boundaries: an unseen adverb
could still separate an English negator from a Sheets mutation, Telegram forwarded
non-audio MIME bodies to STT, and the Gmail recovery pair depended on test order. The
Sheets repair was deliberately simplified after parent adversarial review: any unquoted
standalone EN/HE negator now denies the whole write turn, removing distance and vocabulary
bypasses. Telegram enforces normalized audio MIME before STT, and the Gmail proofs use
distinct fake draft resources. Parent verification passed **149** focused repair tests,
the exact **305-test** 19-file suite, and the full **2,447-test** tree with **1,928 warnings**;
whole-tree Ruff, origin binding, **273/273** evals, exact 164-row reconciliation, and
diff-check also pass. Current measurements are 1,633 definitions, 42,223 physical lines,
37,524 nonblank lines, and 36 strict C901 findings. An eighth clean-room HEAVY review is
required before Phase 1.5 is accepted.

The seventeenth clean-room HEAVY review found two P1 authority gaps: punctuation after a
write/list introducer hid **24** non-string or malformed extra-cell variants, and multiset
comparison allowed the model to reorder or reshape quoted values. The repair uses a bounded
non-word separator grammar for residual candidates and requires exact row-major literal order
plus a rectangular payload whose dimensions equal the selected bounded A1 target. Parent
verification passed **24** owner-live tests, the exact **330-test** 19-file suite, and the
full **2,472-test** tree; whole-tree Ruff, origin binding, **273/273** evals, and diff-check
pass. Current measurements are 1,640 definitions, 42,426 physical lines, 37,707 nonblank
lines, and 36 strict C901 findings. An eighteenth clean-room HEAVY review is required before
Phase 1.5 is accepted.

The eighth clean-room HEAVY review found one P1 and three P2 boundaries: punctuation and
maqaf around Hebrew negators bypassed Sheets denial; duplicate voice updates repeated
download/STT before the webhook claim; alternate media ports bypassed byte limits; and
the Gmail callback proof still collided with itself on same-process repetition. The
repair now uses Hebrew-letter boundaries, claims the canonical authorized voice event
before media effects, verifies exact preclaimed rows downstream, centralizes byte plus
MIME validation at both adapter and STT boundaries, and allocates fresh test resources.
The final parent follow-up also makes malformed alternate-port return shapes fail visibly
once without swallowing downstream errors. Parent verification passed both Telegram
duplicate routes plus Gmail recovery twice in one process, the exact **321-test** 19-file
suite, and the full **2,463-test** tree with **1,955 warnings**;
whole-tree Ruff, origin binding, **273/273** evals, exact 164-row reconciliation, and
diff-check also pass. Current measurements are 1,634 definitions, 42,251 physical lines,
37,550 nonblank lines, and 36 strict C901 findings. A ninth clean-room HEAVY review is
required before Phase 1.5 is accepted.

The ninth clean-room HEAVY review then reproduced two P1 Sheets mutation paths: Hebrew
prohibitions containing niqqud were not recognized, and a second complete A1 target after
punctuation was ignored unless it repeated a preferred introducer. The repair masks JSON
cell literals, removes Unicode combining marks for the standalone-negator test, requires
the exact selected range once after an approved introducer, blanks that range, and rejects
every other bare or bang-qualified A1 token. A parent follow-up removed an interim
title-case-only tab assumption, preserving lowercase and mixed-case spaced tabs accepted
by the provider validator. Parent verification passed **101** focused Sheets/principal
tests, the exact **322-test** 19-file suite, and the full **2,464-test** tree with **1,955
warnings**; whole-tree Ruff, origin binding, **273/273** evals, exact 164-row reconciliation,
and diff-check also pass. Current measurements are 1,635 definitions, 42,270 physical lines,
37,567 nonblank lines, and 36 strict C901 findings. A tenth clean-room HEAVY review is
required before Phase 1.5 is accepted.

The tenth clean-room HEAVY review found one P1 Unicode edge in that repair: U+034F is an
`Mn` mark with combining class zero, so `unicodedata.combining()` did not remove it and a
visually marked standalone prohibition still reached mutation. The corrected implementation
removes all Unicode `M*` categories plus visually inert `Cf` format controls after quoted
literals are masked; stored/provider text remains unchanged. Parent verification again passed **101** focused
Sheets/principal tests, the exact **322-test** 19-file suite, and the full **2,464-test**
tree with **1,955 warnings**; Ruff, origin binding, **273/273** evals, exact 164-row
reconciliation, and diff-check pass. Current measurements are 1,635 definitions, 42,274
physical lines, 37,571 nonblank lines, and 36 strict C901 findings. An eleventh clean-room
HEAVY review is required before Phase 1.5 is accepted.

The eleventh clean-room HEAVY review found two P1 Sheets mutations: whole-turn NFKC
normalization let a canonically equivalent but non-exact quoted value bind, and adjacent
repeated/mixed target introducers still reached the selected target. Quoted literals now
compare as raw decoded JSON codepoints to trimmed model cells without Unicode/case
normalization, while chained introducers deny structurally. Parent hardening also restores
type checks before model-cell trimming and makes the chain detector case-insensitive for
English tokens. Parent verification passed **102** focused Sheets/principal tests, the exact
**323-test** 19-file suite, and the full **2,465-test** tree with **1,955 warnings**; Ruff,
origin binding, **273/273** evals, exact 164-row reconciliation, and diff-check pass. Current
measurements are 1,635 definitions, 42,281 physical lines, 37,578 nonblank lines, and 36
strict C901 findings. A twelfth clean-room HEAVY review is required before Phase 1.5 is
accepted.

The twelfth clean-room HEAVY review found two P1 target-binding gaps and one P2 denial:
punctuation/parenthesis-separated introducer chains reached mutation, lowercase secondary
A1 targets evaded the remaining-target scan, and mixed-case English introducers were
rejected. The bounded repair now recognizes English introducers case-insensitively, rejects
repeated EN/HE chains across punctuation and newlines, and scans remaining bare or
bang-qualified A1 targets case-insensitively. Parent verification passed **18** owner-live
tests, the exact **324-test** 19-file suite, and the full **2,466-test** tree; whole-tree
Ruff, **273/273** evals, and diff-check pass. Current measurements are 1,635 definitions,
42,283 physical lines, 37,580 nonblank lines, and 36 strict C901 findings. A thirteenth
clean-room HEAVY review is required before Phase 1.5 is accepted.

The thirteenth clean-room HEAVY review found two P1 Sheets mutations: `:`, `!`, and `-`
still hid earlier repeated introducers, and residual absolute/mixed, whole-column, and
whole-row A1 references were not recognized. The repair treats every non-word separator as
part of a repeated-introducer chain and replaces the bounded-cell-only residual scan with
one ASCII-case-insensitive A1 grammar covering all four reference shapes while quoted JSON
strings remain masked. Parent verification passed **18** owner-live tests, the exact
**324-test** 19-file suite, and the full **2,466-test** tree; whole-tree Ruff, origin
binding, **273/273** evals, and diff-check pass. Current measurements are 1,635 definitions,
42,288 physical lines, 37,584 nonblank lines, and 36 strict C901 findings. A fourteenth
clean-room HEAVY review is required before Phase 1.5 is accepted.

The fourteenth clean-room HEAVY review exhaustively swept 12,040 Unicode mark,
punctuation, symbol, and format separators and found one P1 bypass: LOW LINE still hid a
prior introducer. It also found one P2 false denial when an exact allowlisted opaque ID
ended in A1-like text. The repair treats underscore as a separator without recognizing
`at` inside a longer alphanumeric word, masks exactly one complete raw ID occurrence outside
the selected target, and fails closed on repeated A1-like ID occurrences. Parent verification
passed **20** owner-live tests, the exact **326-test** 19-file suite, and the full
**2,467-test** tree; whole-tree Ruff, origin binding, **273/273** evals, and diff-check pass.
Current measurements are 1,637 definitions, 42,312 physical lines, 37,604 nonblank lines,
and 36 strict C901 findings. A fifteenth clean-room HEAVY review is required before Phase
1.5 is accepted.

The fifteenth clean-room HEAVY review found two P1 Sheets authority gaps plus one P2 and
one P3: `M*`/`Cf` inside a secondary reference bypassed residual A1 detection, malformed or
unquoted additional cells could escape the subset-write binder, an allowlisted ID equal to
the selected range caused a false denial, and the owner prompt still claimed read-only
tools. The repair preserves raw IDs, targets, and values while normalizing only the residual
security view; requires every quoted candidate to decode as a JSON string; rejects explicit
unquoted JSON scalars; masks the selected target before counting IDs; and accurately states
ADR-042's bounded Sheets-write exception. Parent verification passed **22** owner-live tests,
the exact **326-test** 19-file suite, and the full **2,470-test** tree; whole-tree Ruff,
origin binding, **273/273** evals, and diff-check pass. Current measurements are 1,638
definitions, 42,369 physical lines, 37,657 nonblank lines, and 36 strict C901 findings. A
sixteenth clean-room HEAVY review is required before Phase 1.5 is accepted.

The sixteenth clean-room HEAVY review found one P1 complete-cell authority gap: **59**
unquoted JSON scalar/container variants at `plus`, `with`, plain Hebrew-vav, and
array/object positions reached the fake provider with a quoted subset. The repair performs
a bounded residual candidate scan after masking the exact selected ID, target, and quoted
JSON strings, and rejects every such extra before port construction or any durable effect.
Parent verification passed **23** owner-live tests, the exact **329-test** 19-file suite,
and the full **2,471-test** tree; whole-tree Ruff, origin binding, **273/273** evals, and
diff-check pass. Current measurements are 1,638 definitions, 42,392 physical lines, 37,679
nonblank lines, and 36 strict C901 findings. A seventeenth clean-room HEAVY review is
required before Phase 1.5 is accepted.

## Parent verification

- First implementation wave, rerun by parent: 68 owner-boundary tests, 170
  approval-safety tests, and 286 mechanical tests passed.
- Delivery/client-trust gate after parent-found ambiguity, authority, and health fixes:
  108 focused tests passed; its later stale-caller integration run passed 164 tests.
- The first full-tree run correctly failed 9 tests. Six stale callers/assertions were
  updated to the explicit-principal and centralized-notification contracts without
  weakening delivery checks; three Windows deployment tests received process-scoped
  `-ExecutionPolicy Bypass` while production scripts stayed unchanged.
- Final pre-review tree: **2,405 passed**; first repair tree: **2,414 passed**; second
  rereview repair tree: **2,419 passed**; third-review repair tree: **2,424 passed**;
  fourth-review repair tree: **2,429 passed**; fifth-review repair tree: **2,431 passed**;
  sixth-review repair tree: **2,432 passed**; seventh-review repair tree: **2,447 passed**;
  eighth-review repair tree: **2,458 passed**; final malformed-media repair tree:
  **2,463 passed**; ninth/tenth-review repair tree: **2,464 passed**; eleventh-review
  repair tree: **2,465 passed**; twelfth/thirteenth-review repair tree: **2,466 passed**;
  fourteenth-review repair tree: **2,467 passed**; fifteenth-review repair tree:
  **2,470 passed**; sixteenth-review repair tree: **2,471 passed**; seventeenth-review
  repair tree: **2,472 passed**; eighteenth-review repair tree: **2,472 passed**;
  nineteenth-review structural repair tree: **2,472 passed**; twentieth-review repair
  tree: **2,473 passed**; twenty-first-review repair tree: **2,474 passed**;
  twenty-second-review repair tree: **2,475 passed**.
- Whole-tree Ruff (`app tests scripts`): **All checks passed**.
- Origin binding: **ok**.
- Deterministic evals: **273/273** across all ten families, including calendar 20/20
  and routing 20/20.
- `git diff --check`: exit 0; only Windows line-ending warnings.

## Non-claims

This evidence does not prove a new image is deployed, a real Telegram voice message
completed, or GA4, Search Console, LinkedIn, and Sheets executed through the live owner
conversation. Those remain the production/live gates after the independent review.
