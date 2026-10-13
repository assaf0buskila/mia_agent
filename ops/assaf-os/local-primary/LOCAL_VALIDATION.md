# Seven-entity local validation, 2026-10-14

Outcome: the approved synthetic seven-entity PostgreSQL prototype and summaries
are implemented and independently reviewed. They remain default off and have no
runtime registration or external effects.

## Checkout and ownership

- Windows checkout: `C:/Users/lenovo/Documents/Codex/2026-10-14/task/os-primary-seven`
- Branch: `codex/os-primary-seven-entities-local`
- Base prototype: `a8e46d4662362df8536438265cfeef7f0bc76d9e`
- Its parent release base: `459b945a2cb947b066fd3fab8f0fcf8425db40b3`
- Original `codex/os-primary-local-prototype` checkout remains clean at its
  original commit. No other worktree was modified.
- Root owned the isolated checkout, ledger/transaction tests, verification,
  documentation, and handoff. MID authors owned disjoint domain/schema tests
  and summaries/oracle tests. A separate HEAVY reviewer owned read-only
  acceptance review. Model routing followed the repository `AGENTS.md`.

## Access recovery evidence

The new delegated task exposed `exec_command`; PowerShell executed on the selected
Windows computer. Its initial sandboxed check found the old checkout but Git
returned `Permission denied`. Approved exact-path executor calls successfully
read its clean status, instructions, plan, and history, then created this isolated
worktree. No alternative environment or access bypass was used.

The executor transport later disconnected (`exec-server transport disconnected`,
followed by a recovery timeout). Broader checks were reported blocked rather than
passed. A subsequent harmless directory/Git check recovered on this same computer,
and all final checks below ran afterward. The source-thread notification tool
returned `thread not found`; the delegated task's final outcome is the handoff.

## Final checks

All checks used local cached dependencies. No dependency download or paid resource
was required. Python project dependencies were installed from the offline cache
into this checkout's ignored `.venv`.

| Check | Result | Local evidence |
|---|---|---|
| Original PostgreSQL task baseline | 28 passed | `.cache/baseline-primary.log` |
| All original/new prototype tests | 92 passed, 2 warnings | `.cache/seven-review-fixes.log` |
| Independent prototype rerun | 92 passed, exit 0 | `.cache/independent-review-tests.log` |
| Full repository Python suite | 3,299 passed, 111 skipped, 1,245 warnings, 151.04 seconds | `.cache/repo-final.log` |
| `uv run --offline ruff check app tests` | passed | `.cache/ruff-all.log` |
| Widget behavioral checks | passed | `.cache/widget.log` |
| Existing runtime/Apps Script Node suites | 42 passed, none skipped | `.cache/node-final.log` |
| Git whitespace check | passed | terminal check |

The full repository suite retained its separate opt-in/skipped tests. Those 111
skips are not passing evidence for those integrations. Deprecation warnings
include existing asyncio event-loop policy and Starlette/httpx test-client usage;
no dependency upgrade was made. The 54 transaction tests (28 original task and
26 new ledger) used actual PostgreSQL; domain/summary tests additionally used
offline Node oracles.

Test server: cached `postgres:16`, image ID `ca0bd484cb98`, PostgreSQL 16.15,
disposable container `assaf-os-seven-local-20261014`, published only at
`127.0.0.1:56196`. Database names began `assaf_os_local_` and carried the
`disposable_only` marker. Fresh synthetic schemas were used after metadata changes.
`fsync=on` and `synchronous_commit=on` were observed. No existing database was
used or changed. Cleanup stops only this task-labelled disposable container.

## Independent review

Verdict: **PASS**, no remaining blocking findings. The reviewer inspected all new
modules, schema, tests, and existing native input/summary/dependency/promotion rules.
It independently confirmed:

- explicit-null/omitted work currency parity against the Apps Script summary;
- long Unicode expense source keys commit with the digest index and exact-key guard;
- aggregate overflow fails closed under competing individually valid proposals;
- approve/reject races produce one terminal receipt;
- same-revision fingerprint changes and primary version rollback are refused;
- numeric-owner/source/workspace/epoch boundaries, dependency revalidation,
  capacity serialization, typed dates, atomic promotion, rollback, and receipt recovery;
- no application imports of the new prototype outside `app/prototypes`.

Frozen ledger SHA256:
`76F9CD81107EDA260BE2AD319066E44242A9C4B191509C37C367383EC968574F`.

The two review findings were corrected and covered by regressions: optional/null
work currency now follows the native ILS fallback; expense source uniqueness now
indexes a bounded canonical digest while retaining original source semantics.
Existing-client promotion also advances the client revision like Apps Script.

## Scope and later gates

Seven-domain parity, deterministic summaries, and local atomic decision handling
are complete for this approved prototype scope. The original task slice is unchanged.
No push, merge, deployment, live route, Google write, production import, authority
switch, new credential, or paid resource occurred.

The principal is injected synthetic context; real owner sessions/CSRF, Telegram
callbacks, policy/kill switch and owner approval-envelope integration remain future
work. Import staging/reconciliation, asynchronous mirror/anti-entropy, backup/restore,
writer fencing/cutover, production schemas and measured browser/owner SLO acceptance
remain the existing architecture plan's later gates. Lost-response/rollback injection
does not establish power-loss durability or a real network interruption. Separate
snapshot pages expose their versions and may need reconciliation after concurrent
writes. This is local evidence, not live product acceptance.
