# Final fresh HEAVY review

Date: 2026-08-28
Reviewer role: independent verifier, not an implementation author
Verdict: **FAIL**

Two unresolved P2 findings remain. Therefore `gates/leaf-1.4.2-review.md` G1 does
not qualify for completion and was not changed.

## Scope and evidence boundary

I reviewed the complete current diff against `HEAD`, including all modified and
untracked production, test, gate, and documentation source. I first read
`AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`,
`PLAN.md`, every `gates/*.md`, and the existing `gates/evidence/*.md`. Generated
`.pytest-tmp` artifacts were not treated as source. I did not inspect `.env`, use
credentials, call a live provider, inspect AWS, deploy, or mutate production data.

The review lenses were runtime reachability; authorization, idempotency, untrusted
input, and secrets; ADR-039 deletion collateral and test integrity; Sheets, GA4,
GSC, LinkedIn, and Telegram voice contracts; architectural minimality; and the
honesty of local versus live evidence.

## Findings

### P0

None found.

### P1

None found.

### P2-1: ordinary Hebrew “method” text can authorize a Sheets write

**Evidence:** `app/tools/registries/owner_tools.py:711-713` relies on
`_has_explicit_sheets_write_request` as the deterministic current-message write
gate. At `app/tools/registries/owner_tools.py:767-773`, the Sheets-reference test
uses substring membership and includes the bare Hebrew fragment `שיט`. That
fragment occurs inside ordinary words such as `שיטה` and `שיטות` (“method” and
“methods”). The same function accepts broad Hebrew write verbs such as `כתוב`.
Once this false-positive gate passes, the write reaches the claimed operation and
capability execution at `app/tools/registries/owner_tools.py:727-740`.

**Reachable reproduction:** with an in-memory SQLite store, an authenticated owner
`Principal`, an allowlisted spreadsheet ID, a non-empty Telegram event reference,
and `FakeSheetsPort`, I invoked the real `sheets_append` handler with the unrelated
current owner text `כתוב לי על השיטות הכי טובות` (“write to me about the best
methods”). The result was:

```text
{'ok': True, 'result': '1 Sheet row(s) appended.'}
[('append', 'sheet-allowed', 'KPI!A1', [['changed']])]
```

The pure guard also returned `True` for `כתוב לי שיטה מלאה` and
`הוסף פירוט על השיטה`. No external service was called.

**Why this is blocking:** ADR-042 and `docs/ARCHITECTURE.md:75-77` permit a Sheets
write only after an explicit authenticated owner request. Authentication,
allowlisting, policy, kill switch, and idempotency still apply, so this is not an
unauthenticated arbitrary write; however, the deterministic proof that the current
message explicitly requested a Sheets write is absent for common Hebrew text. A
model-selected tool call can therefore turn an unrelated authenticated-owner
request into an external mutation.

**Test/evidence gap:** `tests/unit/test_owner_live_tools.py:208-239` covers no
current text, an English `Sheet address` false positive, an English explicit write,
and retry idempotency, but no Hebrew lexical false positive. In addition,
`gates/leaf-1.2.2-owner-integrations.md:17-18` is already checked and describes the
Hebrew reference as conservative; this reproduction disproves that evidence.

**Required resolution:** use a conservative token/phrase boundary for actual
Sheets references (including tested Hebrew product terminology) rather than the
bare `שיט` stem, and add negative Hebrew cases through the real handler as well as
positive Hebrew/English cases.

### P2-2: the documented bounded Sheets range is not bounded before provider I/O

**Evidence:** `app/integrations/sheets.py:173-178` declares 20-row and 10-column
limits, but the A1 regex permits any one-to-three-letter column and row up to six
digits. `_validate_owner_sheet_request` at `app/integrations/sheets.py:218-233`
checks only regex shape for the range; it neither parses the endpoints nor checks
span, ordering, or the 20-by-10 limit. `ComposioSheetsPort.read_values` sends that
range at `app/integrations/sheets.py:438-448` and caps the response only afterward
at lines 449-452.

**Reachable reproduction:** using `httpx.MockTransport` and no network or
credentials, I called the real live adapter with an allowlisted spreadsheet ID and
`a1_range='A1:XFD999999'`. The adapter returned the synthetic one-cell response and
the captured provider request contained:

```text
[['ok']]
A1:XFD999999
```

Thus a provider-scale read is issued before the response-size validator can help.
The same validator also accepts reversed endpoints such as `Z99:A1`. Writes bound
the supplied value matrix but do not validate the target range span.

**Why this is blocking:** `docs/PRODUCT.md:24-27`,
`docs/ARCHITECTURE.md:75-77`, and ADR-042 (`docs/DECISIONS.md:923-933`) authorize
only bounded value operations. A very large read can make the upstream provider and
the local HTTP/JSON layer process a much larger response before normalization
rejects it. This is a reliability/resource-boundary defect on an external tool
path, even though the spreadsheet ID remains owner-allowlisted.

**Test/evidence gap:** `tests/unit/test_owner_sheets.py:77-84` rejects an
unallowlisted ID, formula input, and the malformed range `KPI`, but has no oversized
or reversed valid-A1 case. `gates/leaf-1.2.2-owner-integrations.md:5-6` claims
bounded A1 ranges and 20x10x500 handling without proving the request-range bound.

**Required resolution:** parse A1 endpoints before any provider call, reject
reversed ranges, enforce a monotonic span no larger than the stated row/column
limits for reads and writes, and add transport-level tests proving rejection occurs
before HTTP.

### P3-1: owner-tool safety comments no longer describe the accepted architecture

`app/tools/registries/owner_tools.py:3-6` says every tool is a read or an internal
memory write and that meaningful writes go through approvals. The registry now
contains model-selected external Sheets update/append tools guarded by policy and
idempotency, as accepted by ADR-042. Similarly,
`app/domain/owner_brain.py:3-7,62-63` and `app/api/owner.py:717-720` broadly state
that every state-changing/write intent stays deterministic and never reaches the
model. Those comments should distinguish model-selected, deterministically guarded
Sheets value writes from approval/high-risk writes. The runtime design can remain
accepted, but the stale safety commentary makes future review and maintenance less
reliable.

### P3-2: the living runbook still carries removed campaign-alert work

`docs/RUNBOOK.md:219-227` lists “Spend-without-leads” as missing ops-alert work,
including an owner-ack claim. ADR-039 at `docs/DECISIONS.md:853-869` removed Meta
Ads, campaigns, pacing, prelaunch, and their owner surfaces. This stale runbook item
should be removed or explicitly labeled historical; otherwise it advertises future
work for a deliberately deleted product surface.

## Cross-lens conclusions

- **ADR-039 cleanup and test integrity:** residual campaign strings under `app/`
  were limited to generic UTM/attribution data, safety refusal text, migration
  history, specified capability tombstones, and the explicit non-capability owner
  fallback. I found no runtime import of the deleted Meta Ads, LinkedIn analytics,
  campaign, pacing, or prelaunch implementations. The full suite remained green.
- **GA4, GSC, and LinkedIn:** the reviewed adapters use pinned, typed tool shapes,
  classify HTTP/provider/schema failures, and normalize owner-visible output.
  LinkedIn remains profile-only. No live-provider claim is warranted from these
  mocks.
- **Telegram voice:** numeric owner authorization occurs before media download;
  accepted audio is transcribed in memory, enters OwnerGraph, and returns escaped
  text. No TTS/voice-output path was introduced. Tests cover Telegram/STT shapes,
  but production latency, webhook behavior, and device behavior remain unproven.
- **Client knowledge and minimality:** the website path performs the capability
  knowledge lookup once and passes serializable hits inward. The two-graph/shared-
  core architecture remains intact, and the helper extraction did not introduce a
  new service, graph, or persistence layer.
- **Verification honesty:** `gates/root.md` and
  `gates/leaf-1.4.1-live-proof.md` correctly keep the live Telegram/provider/deploy
  gates pending and state that AWS/provider proof is blocked. Those boundaries are
  honest. The two completed Sheets evidence claims called out above are not.

## Independent mechanical checks

All checks were run on the current tree on 2026-08-28 with no live provider access.

- Focused Sheets/owner-tool/Telegram/STT/client-knowledge suite: **166 passed**.
- Full suite with the documented process-scoped PowerShell execution-policy bypass
  and workspace basetemp: reached 100%, **exit 0**.
- `uv --offline --cache-dir .uv-cache run ruff check app tests`: **All checks passed**.
- `git diff --check`: **exit 0**; only CRLF conversion warnings.
- `scripts/assert_origin_bind.py`: **origin-bind: ok**.
- `scripts/eval_diff.py`: **233/233 passed** (sales 51, buyer 43, website handoff
  15, safety 20, objection 20, extract 30, writing 33, gold 21).
- Direct in-memory/mocked reproductions for P2-1 and P2-2: both reproduced as
  described above.

Green regression, lint, and eval results do not override the two missing negative
authorization/resource-boundary cases. Final verdict remains **FAIL**.
