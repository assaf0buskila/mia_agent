# Private owner follow-up draft

Local draft, 13 October 2026. Base `bda48fa697a79b0df6b41f6ef8165f13b4deaec0`,
branch `codex/owner-evidence-completion`. The original `codex/assaf-os-unified`
checkout is preserved. This draft is not merged, deployed or live accepted.

The existing owner loop already retrieves context, writes lasting memory only on
explicit request, prepares immutable proposals and uses numeric-owner approval.
The missing behavior selected here is a compact, source-backed read of recorded
follow-ups, together with explicit completion checks for exact writes. It adds
no memory store, workflow platform, autonomous delivery or scheduler.

## Exact behavior

- `assaf_os_follow_up` is an on-demand, no-argument tool gated by the existing
  Assaf OS enable flag, private Telegram chat and numeric owner allowlist. It
  reads the existing signed, pinned-workbook snapshot once.
- It projects only shared-Sheet leads whose recorded status is new, and tasks
  recorded as to-do, in progress or waiting. It returns at most five of each in
  source order, with stable ID, revision, bounded name/title and recorded status.
  Observation time, TEST/LIVE mode, workspace/workbook identity, per-entity
  coverage, counts and explicit omissions accompany the items. Phone, email,
  source notes, payment values and task notes are excluded from this projection.
- Each item has a fixed **suggestion**, not an action receipt: review the new
  lead, confirm task progress, or ask the owner what a waiting task needs.
  Waiting tasks are `blocked`. The assistant can propose the next step in its
  reply; this tool never prepares or executes it.
- Partial/unknown coverage, relevant source issues, malformed IDs/revisions/
  statuses/labels and duplicate IDs suppress that entity's items and return
  `unknown`, with nullable counts. A complete empty source or only completed/
  cancelled tasks returns `no_recorded_follow_up`; it never asserts that the
  business, or unrecorded work, is complete. Unrelated financial issues remain
  visible as overall coverage and do not erase valid lead/task evidence.
- The existing `assaf_os_receipt` model tool now returns a compact check with
  `scope=exact_historical_write_only`. An exact signed `committed` receipt with
  complete target/revision/binding and a valid historical timestamp returns
  `completed`. Rejected, expired and target-changed receipts return `blocked`.
  Missing/unavailable/malformed evidence returns `unknown`; failed reads retain
  the original request ID and suggest querying it without replaying a mutation.
  TEST/LIVE and workbook/workspace provenance remain explicit. A timestamp more
  than five minutes after the owner turn cannot prove historical completion.
- The owner prompt asks for a current source read, a brief source/time citation,
  explicit unknown/blocked/omission states, and one suggested next step. A receipt
  establishes the historical write only. Checking task/business completion needs
  a current record read. This draft does not add a natural-language claim validator.

For the supplied operating-business shape (five new leads, one agreed 500 ILS work,
no recorded tasks/hours/receipts), the synthetic regression returns five lead
review suggestions and no recorded task follow-ups. It does not infer payment,
income, missing hours, payment corrections or overall business completion. Those
live facts were supplied in the brief; this task did not reread live records.

## Boundaries and source paths

The projection is `app/domain/owner/assaf_os_follow_up.py`. Tool adapters live in
`app/tools/owner/assaf_os.py`; registry/state/routing integration is in
`app/tools/registries/owner_tools.py`, `app/domain/two_state.py` and
`app/domain/owner/request_routing.py`. Scoped model instructions and prompt
version are in `app/graph/owner_agent.py` (`owner_agent_v10`). Regression scenarios
are in `tests/unit/test_assaf_os_follow_up.py`.

The transport and data authority remain `app/services/assaf_os.py` and
`ops/assaf-os/CONTRACT.md` / `schema.js`. Snapshot reads retain the existing native
edit reconciliation/acceptance behavior described by that contract; the new tool
does not introduce another writer, approval, proposal or send path. All proposed
consequential changes still use the exact immutable, expiring owner approval and
target revalidation. Memory/context isolation and explicit correction/remember
patterns are unchanged. Public tools and Lio are unchanged.

`AGENTS.md`, `HANDOFF.md`, `MIA_V2.md`, `TASKS.md` and current Assaf OS release/live
docs were inspected. The requested `docs/PROJECT_MAP.md`, `ARCHITECTURE.md`,
`PRD.md`, `BUILD_STATUS.md`, `DECISIONS.md`, ADRs 017/026/030/031 and `.agents/skills`
are absent from both the selected worktree and this repository's main checkout.
No product policy was inferred from those missing documents.

## Verification and limits

Focused regression: **87 passed**, including private/group/visitor/source/ID
gates, malformed and duplicated evidence, source coverage/issues, task states,
receipt request/provenance/time/target validation, output-size/JSON preservation,
and the actual owner loop with a scripted model and read-only provider double.
Independent HEAVY review and separate adversarial probes: **PASS** after fixing
receipt provenance preservation and future timestamp rejection.

Aggregate evidence is recorded in `.cache/follow-up-*.txt`. Ruff for app/tests and
staging/provisioning, the origin-bind gate, widget syntax/behavior, settings merge,
79 business-ledger Node tests and 78 Assaf OS Node tests passed. Final full Python
suite: **3,198 passed, 111 skipped**, 1,245 existing deprecation warnings,
144.75 seconds. Staging suite: **10 passed**. The full suite includes the existing
deterministic predeploy eval tests; no separate type-check command is configured
in this repository's project/CI contract.

Final logs: `.cache/follow-up-full-final.txt`,
`.cache/follow-up-staging-final.txt`, `.cache/follow-up-focused-final.txt`,
`.cache/follow-up-ruff-final.txt`, `.cache/follow-up-widget.txt`,
`.cache/follow-up-ledger.txt`, `.cache/follow-up-assaf-os.txt`,
`.cache/follow-up-settings.txt` and `.cache/follow-up-predeploy.json`.

Tests use the existing CPython 3.14.3 development environment via
`uv run --active --no-sync`; it is not a freshly synced or Python 3.12 CI
environment. Offline sync lacked cached dependencies, and sandboxed Windows
asyncio/local-socket tests hung; the final aggregate retry pins test mode, SQLite,
empty provider credentials and no optional PostgreSQL DSNs outside the sandbox.

Docker's `dockerDesktopLinuxEngine` pipe is absent. Disposable PostgreSQL,
container build/startup and Caddy container validation therefore remain unrun.
The real-model predeploy gate explicitly reported **SKIPPED** (exit 2), with no
provider calls. Live model selection/wording, production receipts and device/browser
acceptance remain unverified. No browser retry, provider-key access/creation, live
notification, financial edit, deployment or merge occurred.
