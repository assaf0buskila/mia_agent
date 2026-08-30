# Phase 1.5 post-review repair verification

Date: 2026-08-28

## Repaired findings

The failed review in `function-cleanup-heavy-review.md` named one P1, three P2, and two
P3 findings. The current frozen tree repairs each item:

1. Sheets mutations require an explicit mutation request plus the exact spreadsheet id,
   bounded A1 range, and each cell as an exact JSON-quoted literal in the current numeric
   owner's message. The guard runs before claims and adapters.
2. Telegram Gmail approval executes through the existing risk/write-flag/demo/kill-switch
   boundary. Valid approved replays recover deferred or known-failed sends; a completed
   operation is idempotent; invalid binding never reaches Gmail.
3. `send=False` website finalization returns before taking a notification claim.
4. ClientGraph HANDOFF returns after the hot-handoff result and does not also finalize the
   same turn; fan-out remains one card per configured numeric owner.
5. Unauthorized or empty owner batches return before settings and default adapters are
   constructed.
6. No pre-cleanup physical-line measurement exists. The final review contract is amended
   to retain only measured before/after metrics and to record current physical/non-blank
   counts without inventing a baseline.
7. Sheets mutation binding is operation-specific, boundary-safe for ids/ranges, and
   multiset-exact for JSON-quoted values; English and Hebrew inverse operations fail
   before claims or adapter construction.
8. HANDOFF delivery claims are durable per numeric owner. Missing configuration/blank
   text claim nothing; explicit rejection releases only that owner; accepted and
   ambiguous recipients remain claimed. The local owner inbox row is persisted
   independently from delivery eligibility.
9. Sheets authorization plus pure allowlist/range/shape/formula validation executes
   before adapter construction and before the idempotency claim. All rejected requests
   leave no port call or claim and a corrected same-event replay remains eligible.
10. Ordinary website finalization and due reminders claim per notification instance and
    recipient. Finalization uses conversation id; due reminders use local day. Missing
    configuration consumes no claim, partial rejection releases only that recipient,
    and returning conversations remain distinct.
11. Hot-handoff risk policy executes before every state mutation, inbox write, recipient
    claim, or transport attempt. A kill-switch denial leaves the stored lead unchanged.
12. Sheets mutation intent now rejects the requested operation when negated and rejects
    conflicting affirmative operations. Natural English and Hebrew negation forms are
    denial-only vocabulary; they do not broaden affirmative authority.
13. The complete multiset of owner-provided JSON-quoted cell literals must equal the
    flattened tool payload. Strict subsets, supersets, and duplicate-count mismatches fail
    before adapter construction or claim.
14. Legacy finalization claims conservatively block a resend for their exact historical
    conversation; a legacy due-reminder claim blocks only its same local day. Website
    message eligibility and inactivity grouping now bind both lead and conversation, so
    an empty returning session cannot borrow an older session's message.
15. A Sheets mutation now requires exactly one unquoted allowlisted spreadsheet id and
    one unquoted bounded A1 target in the current owner turn. Multiple ids, multiple
    ranges, different tabs, quoted targets, and complete-token collisions fail before
    adapter construction or an operation claim.
16. Shared Sheets value normalization rejects every cell that trims to empty while
    preserving internal spaces in non-empty values; whitespace-only writes cannot reach
    the provider or consume a claim.
17. Bare English `not append` and `not update` forms are negative-only. They cannot
    authorize a mutation or construct a port, claim an operation, persist idempotency,
    or reach the provider.
18. Sheets target extraction now retains the complete bounded A1 target, including its
    full spaced tab prefix. A suffix such as `Bar!A1` cannot be selected from owner text
    naming `Foo Bar!A1`, and repeated/multiple complete targets fail closed; one exact
    spaced target remains valid and replay-idempotent.
19. Sheets writes now fail closed when any standalone English or Hebrew negator appears
    outside JSON-quoted cell literals anywhere in the authenticated owner turn. This
    removes the finite-adverb bypass class rather than extending another word list;
    clean EN/HE positives and quoted negation data remain valid.
20. Telegram voice media is normalized against an explicit audio MIME allowlist in both
    the direct download adapter and immediately before STT. Missing, malformed, HTML,
    JSON, image, and generic-binary types produce one safe visible failure with zero STT
    or OwnerGraph calls.
21. The two Gmail recovery proofs now use distinct fake draft resources and pass alone
    plus in both pair orders, so process-lifetime SQLite state cannot mask either path.
22. Hebrew Sheets negators use Hebrew-letter rather than full-block boundaries, so
    comma, parentheses, dash, newline, and maqaf are separators while syllables inside
    Hebrew words remain inert. All denials stay before port, claim, idempotency, and
    provider effects.
23. Authorized Telegram voice events claim the canonical webhook row before download
    or STT. The downstream owner path may reuse only the exact existing `received` row
    for the same provider, event, channel, and envelope kind; arbitrary caller data
    cannot mint a preclaim. Duplicate success/failure updates repeat no media, STT,
    OwnerGraph, or reply effect.
24. One shared voice-media validator enforces actual non-empty bytes, the 16,000,000-byte
    cap, and normalized supported audio MIME both in the default Telegram adapter and
    immediately before STT, closing alternate-port bypasses.
25. Gmail recovery and Telegram duplicate-route tests allocate fresh per-invocation fake
    resources/events. Parent same-process runs pass twice without weakening the within-test
    duplicate assertions.
26. An alternate Telegram media port returning `None`, a short or long tuple, a string,
    or a mapping now takes the already-claimed one-time voice-failure path. Return-shape
    errors are caught only around unpacking; media validation and STT retain separate
    exception boundaries, and retries repeat no download, STT, OwnerGraph, or reply effect.
27. Sheets negation detection now decomposes Unicode and removes combining marks only
    after JSON cell literals are masked. Standalone pointed or cantillated `לא`/`אל`
    therefore deny the entire write turn, while quoted cell values and letters embedded
    in ordinary Hebrew words remain inert.
28. Sheets target binding requires the exact selected range once after an approved target
    introducer, blanks that exact span, and rejects every remaining unquoted bare or
    bang-qualified bounded A1 token. Lowercase/mixed spaced tab names remain valid; suffix
    selection, repeated targets, and punctuation-separated secondary targets fail before
    adapter construction or operation claims.
29. Sheets negation normalization removes all Unicode mark categories (`Mn`, `Mc`, and
    `Me`) plus visually inert format controls (`Cf`) after quoted literals are masked,
    not only characters with a nonzero combining class. Class-zero U+034F, ZWJ/LRM, and
    related insertions in standalone `לא`/`אל` therefore deny before every side effect
    while quoted values and embedded words remain inert.
30. Sheets quoted-cell binding compares raw decoded JSON codepoints to the trimmed model
    cells without Unicode or case normalization. Canonically/compatibility-equivalent,
    variation-selector, and control-character substitutions therefore fail before every
    side effect; the exact raw literal remains valid and replay-idempotent.
31. Sheets target binding rejects adjacent repeated or mixed EN/HE introducer chains,
    including mixed capitalization. Malformed non-string cells are type-checked before
    trimming and fail as ordinary denied tool results rather than escaping as exceptions.
32. Sheets target binding rejects repeated introducers even when punctuation, parentheses,
    or newlines separate them; every remaining bare or bang-qualified A1 target is scanned
    case-insensitively. A single English introducer remains valid at any capitalization.
33. Sheets target binding treats every non-word separator, including `:`, `!`, `-`, symbols,
    marks, and format controls, as a repeated-introducer chain separator. Residual target
    detection now also rejects absolute/mixed cells plus whole-column and whole-row A1
    references, bare or tab-qualified, before all claims and adapter effects.
34. Sheets target binding also treats LOW LINE as an introducer-chain separator while
    leaving longer alphanumeric words inert. Exactly one complete raw opaque spreadsheet-ID
    occurrence outside the selected target is masked; duplicate A1-like ID occurrences fail
    closed so they cannot conceal a secondary target.
35. The residual security view removes every Unicode mark and format control only after
    the exact selected target and ID are blanked; raw provider text is untouched. Every
    quoted candidate must decode as a JSON string, explicit unquoted JSON scalars fail
    closed, an allowlisted ID equal to the selected range is not double-counted, and the
    owner prompt accurately states ADR-042's narrow Sheets-write exception.
36. The complete-cell binder scans the residual owner request for non-string JSON scalar
    and container candidates only at explicit English/Hebrew write and list positions.
    This closes the review-16 `plus`/`with`/plain-vav and array/object gaps before port
    construction or any claim, idempotency-row, or provider effect while keeping exact
    selected IDs, targets, and quoted strings inert.
37. The residual cell grammar permits bounded non-word separator runs after an explicit
    EN/HE write/list introducer, closing punctuation-obscured candidates. The quoted-cell
    binder is ordered rather than multiset-based and requires the tool payload to be a
    rectangle whose exact dimensions match the selected bounded A1 target, eliminating
    reordering, ragged, partial, and reshaped writes before every side effect.
38. Residual `]` and `}` closers are treated symmetrically with openers at bounded cell
    positions, including adjacent malformed sequences and punctuation/Unicode separators.
    JSON-like pseudo-cells are rejected at the same boundary while valid quoted closer and
    container text remains exact and inert.
39. Exactly one requested mutation verb across the English/Hebrew synonym set must own the
    one complete positive clause. An earlier same-operation clause, cross-language duplicate,
    operation verb in a prefix, or raw readable/private sentinel outside JSON strings denies
    before port construction, claim, idempotency row, or provider mutation.
40. When the spreadsheet ID and selected A1 target are the same raw token, the positive
    grammar evaluates both semantic role orders. This admits the valid Hebrew target-first
    shape without weakening exact occurrence, target, literal, order, grid, or A1 binding.
41. A shared post-JSON-mask security view applies NFKD compatibility normalization, removes
    every Unicode `M*` mark and `Cf` control, and casefolds only for intent/security matching.
    Obscured or full-width earlier operation verbs and readable sentinel tokens therefore
    deny before all four effects, while raw ID, A1 target, decoded literal, payload, and
    provider codepoints remain exact and unnormalized.
42. Sheets mutation authorization now full-matches the complete security-view turn. Only
    the tested bare, `Please`, exact `Please record this now:`, or `אלופה` preface may
    precede one supported operation and one complete bound clause; arbitrary mutation-like,
    mixed-script, malformed, or multilingual prefix/suffix text denies before all effects.

## Parent commands and results

- Sheets/finalization focused suite: **140 passed**.
- Approval/Gmail focused suite: **177 passed**.
- Second-rereview cross-cutting Sheets/HANDOFF/E2E suite: **181 passed**.
- Final exact 19-file Sheets/notification/handoff/migration/Telegram voice suite after
  the twenty-second-review repair: **333 passed**.
- Parent same-process run of both Telegram duplicate routes plus Gmail recovery:
  **3 passed, then 3 passed** in the same Python process.
- Full tree with workspace-local cache/temp and no pytest cache provider: **2,475 passed**.
- `uv --offline --cache-dir .uv-cache run ruff check app tests scripts`: **passed**.
- `uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py`:
  **origin-bind: ok**.
- `uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py`: **273/273**
  across sales 51, buyer 43, calendar 20, website_handoff 15, safety 20, objection 20,
  routing 20, extract 30, writing 33, and gold 21.
- Function inventory: **164** current function-bearing files, **164** audit rows, zero
  missing/extra/duplicate paths, **1,646** definition lines, **42,537** physical lines,
  **37,805** non-blank lines, and **36** strict C901 findings in `app` plus `scripts`.
- `git diff --check`: exit 0 after the evidence and gate updates; only Windows
  line-ending warnings were emitted.

## Exact 19-file combined suite contract

The reproducible combined command uses these exact paths (in this order):

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

## Non-claims

This evidence does not prove AWS deployment or live Telegram/provider behavior and does
not claim a pre-cleanup physical-line count. The twenty-third independent HEAVY review
reran the repaired outcome, added a fresh 168-case four-effect probe, and passed Phase 1.5
with no unresolved P0/P1/P2.
