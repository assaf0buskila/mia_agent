# Phase 1.5 function cleanup: fifteenth-review repair

Date: 2026-08-30

This bounded repair addresses the two P1s, one P2, and P3 in
`function-cleanup-heavy-fifteenth-review.md`.

## Changes

- Residual A1 security scanning strips Unicode mark (`M*`) and format (`Cf`)
  characters only after exact selected-target and selected-ID spans are masked.
  Raw target, ID, and quoted value codepoints remain unchanged.
- Every matched quoted candidate must decode as a JSON string. Invalid escapes,
  raw-newline candidates, unmatched quotes, and non-string candidates fail
  closed. Explicit list/conjunction positions reject unquoted JSON scalar cells;
  exact target and ID spans are masked first so their digits are not cells.
- Allowlisted-ID mention counting occurs after masking the selected target, so an
  unrelated configured ID equal to that target cannot create a false denial.
- Owner prompt wording states the reads/owner-memory surface and ADR-042's
  bounded authenticated allowlisted Sheets value-write exception.

## Local evidence

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q
22 passed
uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py app/graph/owner_agent.py tests/unit/test_owner_live_tools.py
All checks passed!
git diff --check
exit 0 (repository-wide LF-to-CRLF warnings only)
```

The added counted regression covers marked secondary targets, malformed/raw-
newline/non-string additional cells, unquoted numeric/boolean cells, the
overlapping allowlist control, provider/claim/idempotency-row zero effects on
rejection, and idempotent retry on the valid overlap case. The follow-up
regression covers signed/decimal/exponent and boolean/null scalars after EN/HE
verbs, plus/with, and Hebrew ו-hyphen/maqaf conjunctions, while preserving
numeric IDs/targets and quoted numeric values.

The parent full-suite attempt first stopped on one stale prompt assertion expecting
the removed phrase ``You have read tools only``; no production failure was reported.
That directly relevant assertion now checks the truthful ADR-042 contract and all
send/book/approve/pay/publish/campaign/delete refusals. Parent verification then passed
the exact 19-file suite (**326 passed**) and the full tree (**2,470 passed**), plus
whole-tree Ruff, origin binding, deterministic evals (**273/273**), and diff-check.
The reconciled inventory is 164 function files, 1,638 definitions, 42,369 physical
lines, 37,657 nonblank lines, and 36 strict C901 findings.

## Explicit non-claims

No provider, credential, deployment, production concurrency, or live Google
Sheets behavior is proven. A fresh independent HEAVY **review 16** remains
required before approval, commit, or deployment.
