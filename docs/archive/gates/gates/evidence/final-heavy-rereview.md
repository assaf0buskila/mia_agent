# Final fresh HEAVY rereview

Date: 2026-08-28
Reviewer role: second independent verifier, not an implementation author
Verdict: **PASS**

All P2 reproductions found across the original final review and this clean-room
rereview are closed after two bounded remediations. No unresolved P0, P1, or P2
finding remains. `gates/leaf-1.4.2-review.md` G1 and G4 are complete; the root
ledger continues to show live/provider/deployment proof as pending.

## Scope and evidence boundary

I read `AGENTS.md`, the canonical product/architecture/decision docs, `PLAN.md`,
all gate and evidence files, and reviewed every modified or untracked source,
test, script, and living-document hunk in the current worktree. I did not inspect
`.env`, retrieve secret values, call AWS or a live provider, deploy, or mutate
production data. Generated `.pytest-tmp` artifacts were not treated as source.

Review lenses: real handler/adapter reachability; authorization, policy, kill
switch, idempotency, secrets, and untrusted text; ADR-039 deletion collateral and
test integrity; Telegram voice; GA4/GSC/LinkedIn; client knowledge retrieval;
simplification; comments/docs; and local-versus-live evidence honesty.

## Former P2 closure reproductions

### Former P2-1: Hebrew `שיטה` / `שיטות` collision — closed

`_has_explicit_sheets_write_request` now requires a bounded English Sheets token
or the Hebrew token/phrase `גיליון`, `לגיליון`, `גיליון גוגל`, `לגיליון גוגל`, or
`שיטס`; the removed bare `שיט` substring no longer matches ordinary “method” text.

The real `sheets_append` handler test exercised an authenticated owner context,
allowlisted id, source event, and `FakeSheetsPort`. These unrelated phrases all
returned failure and left `owner_operations` empty:

- `כתוב לי על השיטות הכי טובות`
- `כתוב לי שיטה מלאה`
- `הוסף פירוט על השיטה`

The same real-handler test then proved both positive paths: English `Please append
this row to the Sheet` and Hebrew `הוסף את השורה לגיליון גוגל` each appended once.
The focused former-P2 command passed all **7 tests**.

### Former P2-2: unbounded/reversed A1 ranges — closed

`validate_owner_sheet_request` now calls `_parse_bounded_a1_range` before any port
operation. That parser converts both endpoints, rejects reverse row/column order,
enforces at most 20 rows by 10 columns, and verifies a supplied value matrix fits
the requested range. The validation runs in both the capability handler and the
live adapter.

Transport/fake-port tests prove `A1:XFD999999`, `Z99:A1`, and `KPI!A1:K21` are
rejected before HTTP or `FakeSheetsPort` mutation, and a 3-cell matrix targeting
`KPI!A1:B1` is rejected before HTTP. The same focused former-P2 command passed all
**7 tests**.

## Findings

### P0

None found.

### P1

None found.

### P2-1: read-style owner wording still authorizes a Sheets mutation

**Evidence:** `app/tools/registries/owner_tools.py:712` treats
`_has_explicit_sheets_write_request` as the deterministic current-message
authorization gate. At lines 767-775, any word-boundary English `write` plus any
word-boundary `Sheet` token is accepted; the guard does not distinguish “write to
the Sheet” from “write/tell me what is in the Sheet.” Once it returns true, the
handler claims and executes `sheets.append` at lines 716-740.

**Exact real-handler reproduction:** I created an in-memory SQLite store, an
authenticated owner `Principal`, an allowlisted spreadsheet id, a non-empty
Telegram event reference, and `FakeSheetsPort`, then invoked the real
`sheets_append` handler with current owner text:

```text
write me what is in the Sheet
```

and model-provided arguments targeting `KPI!A1` with `[["changed"]]`. The captured
result was:

```text
{'ok': True, 'text': '1 Sheet row(s) appended.', 'error': ''}
[('append', 'sheet-allowed', 'KPI!A1', [['changed']])]
```

This is an ordinary request to report/read Sheet contents, not an explicit request
to mutate the Sheet. A mistaken or adversarial model tool choice can therefore
still convert a non-write authenticated-owner message into an external write.
Spreadsheet allowlisting, principal policy, kill switch, and per-event
idempotency all remain intact; the missing boundary is explicit current-message
write intent required by ADR-042.

**Test gap:** the post-fix handler test adds the three Hebrew method negatives but
does not add ambiguous/read-style English or Hebrew phrases containing a Sheets
reference and a conversational “write/tell me” verb. Its positive English fixture
uses nearly the same broad lexical form, so the green test does not prove semantic
write direction.

**Required resolution:** authorize only an explicit operation directed at the
Sheet, preferably with conservative operation phrases (for example, append/add/
update/fill/enter **to/in** the named Sheet or range) rather than the unordered
co-occurrence of `write` and `Sheet`. Add negative real-handler cases such as the
reproduction above, plus equivalent Hebrew read/report wording, while preserving
the existing explicit HE/EN positive cases.

### P3

No new P3 finding is necessary for the verdict. The two stale-comment/runbook P3s
from the first review are closed: owner-tool/brain comments now name ADR-042's
guarded exception, and the removed spend-without-leads runbook item is gone.

## Cross-lens conclusions

- **ADR-039 collateral/test integrity:** the residual-symbol gate returned no
  matches under `app/` outside the explicitly excluded capability-status registry.
  Deleted tests and source are confined to paid campaigns, pacing, prelaunch, and
  LinkedIn post analytics; generic approval, commitment, due-scan, owner-routing,
  event, and adversarial coverage remains present and passed in the full suite.
- **Telegram voice:** numeric owner authorization precedes media download; successful
  STT reaches the one OwnerGraph and returns escaped HTML text. Failure returns fixed
  text without provider/audio detail and duplicate updates do not double-send. No
  TTS path was added. Live webhook, provider latency, and device behavior remain
  unproven.
- **GA4/GSC/LinkedIn:** the adapters remain typed and pinned, provider/schema/auth
  failures are classified, KPI output is normalized, and LinkedIn remains
  profile-only. Mocked/local evidence does not prove active OAuth or live data.
- **Client graph/minimality:** ClientGraph owns one knowledge capability retrieval
  and does not inject the legacy second lookup. `mirror_sales_turn` removes the
  duplicated website/inbound mirror sequence without adding a graph, runtime agent,
  database, provider, or ambient principal.
- **Evidence honesty:** live Telegram, provider, AWS, migration, and deployment gates
  remain explicitly blocked/pending. Root G2, G3, and G6 remain pending, so the root
  ledger does not present local tests as production proof. Root G4 remains honest;
  this FAIL prevents completing the final review ledger.

## Independent mechanical evidence

All commands ran on the current tree on 2026-08-28 without live provider access.

- Former-P2 real-handler/transport subset: **7 passed**.
- Focused Sheets/owner-tool suite: **84 passed**.
- Complete repository suite with the documented process-scoped PowerShell policy
  bypass and workspace basetemp: reached **100%**, exit **0**.
- Full Ruff: `All checks passed!`.
- `git diff --check`: exit **0**; only Windows LF/CRLF conversion warnings.
- Origin binding: `origin-bind: ok`.
- Deterministic evals: **233/233 passed** — sales 51, buyer 43, website handoff 15,
  safety 20, objection 20, extract 30, writing 33, gold 21.
- ADR-039 residual-symbol search: no matches.
- Direct in-memory real-handler reproduction for the new P2: reproduced exactly as
  shown above.

Green tests, lint, and evals do not override the remaining external-write
authorization gap. Final verdict remains **FAIL**.

## Interim remediation verification — 2026-08-28

At this checkpoint, the verdict remained **FAIL**. The implementer closed the exact English reproduction
above, but the same authorization class remains reachable through another generic
Hebrew substring.

### What is now closed

The remediation removed generic English `write` and Hebrew `כתוב` from the
mutation-verb list. Through the real `sheets_append`/`sheets_update` handlers with
an authenticated owner `Principal`, allowlisted spreadsheet id, non-empty Telegram
event reference, in-memory store, and `FakeSheetsPort`, I independently observed:

```text
read_exact     "write me what is in the Sheet"       -> denied, 0 operations
read_adjacent  "write down what the Sheet says"      -> denied, 0 operations
read_hebrew    "כתוב לי מה יש בגיליון"               -> denied, 0 operations
positive_append "Please append this row to the Sheet" -> allowed
positive_update "Please update the Sheet with this value" -> allowed
positive_hebrew "הוסף את השורה לגיליון גוגל"          -> allowed
```

The updated real-handler regression passed. Thus the exact reproduction reported
in the first rereview is closed without breaking the explicit HE/EN positive paths.

### Remaining P2: Hebrew `מלא` is still an unbounded substring verb

`_has_explicit_sheets_write_request` still uses substring membership for the
Hebrew verbs and includes bare `מלא`. That sequence occurs inside ordinary nouns
such as `מלאי` (“inventory”). The current added negatives do not cover this
adjacent lexical collision.

Exact real-handler reproduction on the remediated tree:

```text
owner text: מה המלאי של גיליון גוגל
tool: sheets_append
result: ok=True
FakeSheetsPort: [('append', 'sheet-allowed', 'KPI!A1', [['changed']])]
```

This asks what the inventory in/of the Google Sheet is; it does not explicitly
request a Sheet mutation. Nevertheless `מלא` inside `מלאי`, plus the valid
`גיליון גוגל` reference, authorizes the model-selected external write. The ADR-042
explicit-current-message boundary therefore remains incomplete.

Required closure: use token/phrase-aware Hebrew mutation verbs, not substring
membership, and add this exact real-handler negative plus neighboring noun/adjective
forms while retaining an explicit imperative fill/update positive.

### Remediation mechanical checks

- Updated real-handler negative/positive regression: **1 passed**.
- Focused Sheets/owner suite (`test_owner_live_tools.py`, `test_owner_sheets.py`,
  `test_sheets.py`): **84 passed**.
- Full Ruff: `All checks passed!`.
- `git diff --check`: exit **0**; only LF/CRLF conversion warnings.

Because an external-write authorization P2 remained at that checkpoint, G1 and G4 in
`gates/leaf-1.4.2-review.md` stayed pending. No gate checkbox was changed then.

## Final remediation closure — 2026-08-28

This section supersedes the interim FAIL checkpoint above. The implementer replaced
Hebrew mutation-verb substring membership with an explicit regular expression whose
left and right boundaries reject adjacent Hebrew letters:

```text
(?<![\u0590-\u05ff])(?:עדכן|הוסף|מלא|הכנס)(?![\u0590-\u05ff])
```

The real-handler regression now includes both collision phrases and keeps exact-token
Hebrew positives. I independently executed `execute_tool` with an authenticated owner
principal, allowlisted spreadsheet id, non-empty unique Telegram event reference,
in-memory store, and `FakeSheetsPort`. Results were:

```text
inventory_collision: ok=False transport_delta=0 error='explicit Sheets write request required'
income_collision:    ok=False transport_delta=0 error='explicit Sheets write request required'
english_read:        ok=False transport_delta=0 error='explicit Sheets write request required'
hebrew_update:       ok=True  transport_delta=1
hebrew_append:       ok=True  transport_delta=1
hebrew_fill:         ok=True  transport_delta=1
hebrew_enter:        ok=True  transport_delta=1
```

The exact negative messages were `מה המלאי של גיליון גוגל` and
`מה ההכנסות של גיליון גוגל`. They reached the real `sheets_append` handler but were
rejected before the fake transport. Exact mutation-token commands `עדכן את גיליון
גוגל`, `הוסף שורה לגיליון גוגל`, `מלא את גיליון גוגל`, and `הכנס שורה לגיליון גוגל`
continued to reach the update/append adapter exactly once each.

Independent bounded closure checks on the final tree:

- `test_owner_live_tools.py` plus `test_owner_sheets.py`: **20 passed**, exit 0.
- Full Ruff over `app tests`: `All checks passed!`.
- `git diff --check`: exit 0; only Windows LF/CRLF conversion warnings.

The token matcher closes the remaining reachable external-write authorization
collision without weakening the explicit Hebrew/English positive paths. Combined
with the earlier complete review, A1/matrix pre-transport validation, full suite,
eval, origin-binding, and collateral evidence, there are no unresolved P0/P1/P2
findings. Final verdict: **PASS**.
