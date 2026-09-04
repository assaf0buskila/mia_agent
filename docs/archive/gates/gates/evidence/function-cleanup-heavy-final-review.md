# Phase 1.5 clean-room HEAVY final outcome review

Date: 2026-08-28
Mode: independent read-only outcome review; only gate/evidence files were edited
Verdict: **FAIL**

The current tree passes its mechanical regression and migration-enumeration checks, and
the repaired HANDOFF recipient ledger behaves correctly. It does not pass the outcome
gate: one P1 and two P2 defects remain. No P0 was found.

## Review basis and boundaries

The reviewer read, in the required order, `AGENTS.md`, `docs/PRODUCT.md`,
`docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, the complete `unlazy` skill plus its required
references, both historical failed reviews, repair verification, synthesis, the final
review leaf, all three function-audit matrices, and the current production, test,
migration, and diff surfaces. Claims from earlier reviews were treated as untrusted and
remeasured on the current tree.

No `.env` file or secret value was inspected. No live provider or network call, AWS
mutation, deployment, production/test/migration edit, or destructive command was made.
Provider behavior below is local fake-boundary evidence only.

## Blocking findings

### P1 — Sheets policy-invalid writes construct the real port boundary and consume an operation claim before rejection

The repaired textual binding in `app/tools/registries/owner_tools.py:713-724` correctly
binds update versus append (including accepted Hebrew `הכנס` as append-only), complete
spreadsheet/range tokens, quoted literal multiplicity, and prevents quoted literals from
supplying a verb. Those earlier authorization defects are fixed.

However, `_sheets_write` constructs the port at line 725 and calls
`claim_operation` at line 736. Only afterward does `execute_capability` invoke the Sheets
handler. The allowlist, bounded-A1, shape, and 20-by-10 cap checks live in
`app/integrations/sheets.py:220-263`; the kill switch and capability policy are likewise
enforced only inside `execute_capability` at lines 741-749. Therefore policy-invalid
requests violate the required reject-before-port and reject-before-claim boundary.

Real owner registry + real `LeadStore` + `FakeSheetsPort` provider-boundary probe:

```powershell
$env:MIA_DATABASE_URL='sqlite:///:memory:'
@'
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.db.models import IdempotencyRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.integrations.sheets import FakeSheetsPort
from app.tools.registries import owner_tools
from app.tools.registries.owner_tools import ToolContext, execute_tool
init_db(); session=get_session_factory()(); store=LeadStore(session); port=FakeSheetsPort()
ctx=ToolContext(principal=Principal.owner(source='probe'), store=store,
    brain=BrainStore(session), settings=get_settings().model_copy(
        update={'sheets_allowed_spreadsheet_ids':'sheet-allowed'}),
    embedding_port=FakeEmbeddingPort(), source_ref='telegram:probe',
    owner_text='Please append "x" to outside at KPI!A1 in the Sheet', sheets=port)
calls={'port':0,'claim':0}; real_port=owner_tools._owner_sheets_port
real_claim=store.claim_operation
def counted_port(inner): calls['port']+=1; return real_port(inner)
def counted_claim(**kwargs): calls['claim']+=1; return real_claim(**kwargs)
owner_tools._owner_sheets_port=counted_port; store.claim_operation=counted_claim
result = execute_tool(
    "sheets_append",
    {"spreadsheet_id": "outside", "range": "KPI!A1", "values": [["x"]]},
    ctx,
)
row=session.query(IdempotencyRow).filter(
    IdempotencyRow.scope=='owner_sheets_write').one()
print({"ok": result.ok, "error": result.error, "calls": calls,
       "claim_status": row.status, "adapter_ops": port.owner_operations})
owner_tools._owner_sheets_port=real_port; session.close()
'@ | uv --offline --cache-dir .uv-cache run python -
```

Exact result:

```text
{'ok': False, 'error': 'InvalidArguments', 'calls': {'port': 1, 'claim': 1},
 'claim_status': 'in_flight', 'adapter_ops': []}
```

The same harness with an allowlisted exact request and `ctx.kill_switch=True` produced:

```text
{'ok': False, 'error': 'sheets write denied', 'calls': {'port': 1, 'claim': 1}, 'adapter_ops': []}
```

The allowlist rejection is also converted to generic `InvalidArguments` outside the
`_sheets_write` exception handlers, so this path does not call `fail_operation`; the
claim can remain in-progress. The adapter did not mutate, but construction and claim
consumption are themselves forbidden boundary effects and can suppress a corrected
same-event replay.

Required repair gate:

1. Run pure allowlist, bounded-A1, shape/cap, kill-switch, and capability-policy
   prevalidation before `_owner_sheets_port` and before `claim_operation`.
2. Prove every rejection leaves both counters at zero and leaves no operation claim.
3. Preserve the now-correct HE/EN operation binding, complete ID/range boundaries,
   duplicate literal multiset, quoted-value verb exclusion, RAW writes, and exact-event
   successful idempotency.

### P2 — Ordinary finalization and due reminders still use a global fan-out claim that loses recoverable recipients

`finalize_website_conversation` inserts one conversation claim at
`app/services/finalization.py:125-130`, fans out at line 137, and releases only when
`delivery.confirmed_failure` is true at lines 138-141. In
`app/services/notifications.py:30-32`, missing token/owners/text (`no_attempt=True`) is
not a confirmed failure, and a partial known rejection is not a confirmed failure when
any recipient was delivered. Thus a later valid replay cannot run at all, and a known
rejected recipient is permanently skipped. `app/workers/due_scan.py:58-73` has the same
global-claim/release predicate for the daily due reminder.

Real `LeadStore` no-configuration then valid-replay probe:

```powershell
$env:MIA_DATABASE_URL='sqlite:///:memory:'
@'
from sqlalchemy.orm import sessionmaker
from app.core.config import Settings
from app.db.models import Base
from app.db.session import make_engine
from app.db.store import LeadStore
from app.services import finalization
from app.services.finalization import (ConversationSummary,
    finalize_website_conversation, kind_for)
from app.services.notifications import OwnerTelegramDelivery
engine=make_engine('sqlite:///:memory:'); Base.metadata.create_all(engine)
db=sessionmaker(bind=engine)(); store=LeadStore(db)
summary = ConversationSummary(
    conversation_id="web_probe_missing", lead_id="lead_probe_missing"
)
first = finalize_website_conversation(store, summary=summary, settings=Settings())
db.commit()
present=store.has_owner_notification_claim(kind=kind_for('v1'),
    lead_id=summary.lead_id, conversation_id=summary.conversation_id)
calls=[]; real=finalization.deliver_owner_telegram
finalization.deliver_owner_telegram=lambda **kwargs: (
    calls.append(kwargs) or OwnerTelegramDelivery(delivered=('111',)))
second = finalize_website_conversation(
    store, summary=summary,
    settings=Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
)
print({"first": first.model_dump(), "claim_after_first": present,
       "retry": second.model_dump(), "delivery_calls": len(calls)})
finalization.deliver_owner_telegram=real; db.close(); engine.dispose()
'@ | uv --offline --cache-dir .uv-cache run python -
```

Exact result:

```text
{'first': {'claimed': True, 'sent': False, 'duplicate': False, 'kind': 'web_final_v1'},
 'claim_after_first': True,
 'retry': {'claimed': False, 'sent': False, 'duplicate': True, 'kind': 'web_final_v1'},
 'delivery_calls': 0}
```

Real-store partial known-rejection probe, with the local delivery boundary returning
`delivered=('111',), rejected=('222',)`:

```text
{'first': {'claimed': True, 'sent': True, 'duplicate': False, 'kind': 'web_final_v1'},
 'retry': {'claimed': False, 'sent': False, 'duplicate': True, 'kind': 'web_final_v1'},
 'delivery_calls': ['first']}
```

The retry never reaches delivery, so owner 222 cannot recover. This is distinct from the
repaired HANDOFF path, which now has durable per-recipient claims.

Required repair gate:

1. No token, no numeric owner, or blank rendered text must consume no recipient claim;
   later valid replay sends once.
2. Ordinary finalization and due reminders must track recipient outcomes durably (or an
   equivalent structure): accepted and ambiguous recipients remain claimed, explicit
   rejection releases only that recipient, and retry sends only missing recipients.
3. Prove all-success replay sends none and concurrent SQLite/PostgreSQL claim semantics
   cannot duplicate a recipient.

### P2 — Hot HANDOFF mutates takeover state before the kill policy is checked

`apply_hot_handoff` calls `set_takeover_state` and `cancel_pending_follow_up` at
`app/domain/hot_handoff.py:126-127`, then checks `assert_allowed(...kill_switch=...)` at
lines 129-135. The live legacy inbound path calls this function for HANDOFF at
`app/api/inbound.py:596-603`, so this is production-reachable, not a dead helper.

Real `LeadStore` probe with a seeded website lead and `kill_switch=True`:

```powershell
$env:MIA_DATABASE_URL='sqlite:///:memory:'
@'
from sqlalchemy.orm import sessionmaker
from app.core.config import Settings
from app.db.models import Base
from app.db.session import make_engine
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.hot_handoff import KIND_HOT_LEAD, apply_hot_handoff
engine=make_engine('sqlite:///:memory:'); Base.metadata.create_all(engine)
db=sessionmaker(bind=engine)(); store=LeadStore(db)
_,lead_id=store.open_channel_lead(channel=Channel.WEBSITE, external_id='kill-probe')
before=store.get_takeover_state(lead_id)
attempt=apply_hot_handoff(store, lead_id=lead_id, inbound_id='in-kill',
    want='human', kill_switch=True,
    settings=Settings(telegram_bot_token='tok', telegram_owner_user_ids='111'))
print({'before':before, 'after':store.get_takeover_state(lead_id),
    'owner_inbox':store.has_owner_notification(kind=KIND_HOT_LEAD, lead_id=lead_id),
    'attempted':attempt.attempted, 'known_unreachable':attempt.known_unreachable})
db.close(); engine.dispose()
'@ | uv --offline --cache-dir .uv-cache run python -
```

```text
{'before': 'mia_active', 'after': 'human_takeover_required', 'owner_inbox': False,
 'attempted': False, 'known_unreachable': False}
```

The notification is correctly suppressed, but takeover state is still written and any
pending follow-up is cancelled. This violates the architecture contract that the kill
switch denies before side effects and can strand a lead in human takeover without an
owner notification.

Required repair gate: enforce the kill/risk decision before either state mutation, then
prove kill-on leaves takeover state, follow-up state, inbox rows, recipient claims, and
transport calls unchanged while kill-off retains the repaired HANDOFF behavior.

## HANDOFF recipient repair and migration assessment

The new HANDOFF implementation otherwise passes the adversarial outcome review:

- the owner-inbox row is independent of transport state;
- no token, no numeric owner, or blank text consumes no recipient claim;
- claims are durable by `(kind, lead_id, numeric recipient_id)`;
- explicit rejection releases only the failed recipient, accepted/ambiguous recipients
  remain claimed, partial retry sends only the missing owner, and all-success replay sends
  none;
- ClientGraph returns from HANDOFF before ordinary finalization, so it does not emit both
  a hot-handoff and finalization card;
- `LeadStore._insert_ignoring_conflicts` selects SQLite or PostgreSQL dialect insert and
  uses one `ON CONFLICT DO NOTHING` statement.

Migration/deploy inclusion is present, ordered, and locally applied:

- `deploy/Dockerfile:7` copies the entire `migrations` directory into `/app/migrations`;
- `pyproject.toml:19` maps `mia-migrate` to `app.workers.migrate:main`;
- the worker calls `init_db()` and then `apply_migrations`; enumeration is filename-sorted;
- `scripts/run_ecs_migration.py:37` fixes the Fargate override to `mia-migrate`;
- `20260828_owner_notification_recipient_claims.sql` is not SQLite-skipped.

Exact local application probe:

```text
{'present': True, 'index': 36, 'last': '20260828_owner_notification_recipient_claims.sql',
 'postgres_only': False, 'failed': '', 'applied_or_already': True,
 'recorded': '20260828_owner_notification_recipient_claims.sql',
 'columns': ['kind', 'lead_id', 'recipient_id', 'claimed_at']}
```

PostgreSQL dialect compilation of the production store statement:

```text
INSERT INTO owner_notification_recipient_claims (kind, lead_id, recipient_id, claimed_at)
VALUES (%(kind)s, %(lead_id)s, %(recipient_id)s, %(claimed_at)s) ON CONFLICT DO NOTHING
```

`tests/unit/test_migrate.py::test_apply_migrations_first_run_sqlite` enumerates and applies
all non-PostgreSQL-only SQL files and fails on any migration failure; the explicit probe
above additionally proves this new filename is enumerated, recorded, and creates its
four columns. This is strong packaging/enumeration/SQLite and PostgreSQL-compilation
evidence, not a live PostgreSQL deployment or migration claim.

## Earlier repaired seams rechecked

- Gmail callback authentication remains numeric-owner-first. Valid deferred/failed sends
  can replay; invalid/tampered callbacks do not send; completed sends do not duplicate.
- Kill-suppressed ordinary finalization (`send=False`) returns before its claim. The P2
  above concerns later delivery no-attempt/partial fan-out, not this repaired branch.
- Unauthorized/empty owner batches are filtered before settings or adapter builders.
- Owner/client `Principal` boundaries remain explicit; one production OwnerGraph and one
  bounded owner-agent loop preserve ADR-031/032.
- Telegram/website voice is STT input with text output; no TTS implementation was found.
- GA4, GSC, LinkedIn, and Sheets remain owner-scoped with pinned narrow operations. GA4,
  GSC, and LinkedIn are read-only; Sheets exposes get/update/append but no arbitrary
  create/delete/share/format surface.
- Deployment scripts expose fixed migration/deploy actions, preserve the origin bind, and
  do not accept arbitrary environment or migration commands.

## Current measurements

Independent AST/physical-line and matrix reconciliation on this tree:

```text
function-bearing app+scripts files: 164
definition lines (def + async def, including nested): 1,622
physical lines in those files: 41,939
non-blank lines in those files: 37,263
strict app+scripts C901 findings: 37
matrix rows / unique rows: 164 / 164
missing / extra / duplicate matrix paths: 0 / 0 / 0
dispositions: KEEP 139, SIMPLIFY 24, MERGE 1, REMOVE 0
partition: API 23, domain 73, infra 68
```

These are current-tree measurements only. There is no measured pre-cleanup physical or
non-blank baseline, so this review makes no physical-line shrink claim. Definition-line
changes are not presented as physical shrink.

## Commands and exact results

All Python commands used the repository-local cache. Pytest used a repository-local
`--basetemp`, disabled its cache provider, and used `MIA_DATABASE_URL=sqlite:///:memory:`.

Recorded focused adversarial suite (the twenty paths are shown exactly):

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-heavy-final-recorded-20260828 tests/unit/test_owner_live_tools.py tests/unit/test_owner_sheets.py tests/unit/test_sheets.py tests/unit/test_gmail.py tests/unit/test_owner_gmail_console.py tests/unit/test_telegram.py tests/unit/test_telegram_owner_graph.py tests/unit/test_vnext_finalization.py tests/unit/test_hot_handoff.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_due_scan_worker.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_owner_voice.py tests/unit/test_vnext_client_voice.py tests/unit/test_ga4.py tests/unit/test_search_console.py tests/unit/test_linkedin.py tests/unit/test_deploy_secret_box.py tests/unit/test_scripts.py tests/unit/test_migrate.py
```

Result: **262 passed, 119 warnings in 22.67s**.

Full regression:

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-heavy-final-full-20260828
```

Result: **2,419 passed, 1,856 warnings in 79.76s**.

Whole-tree lint:

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
```

Result: **All checks passed**.

Strict complexity measurement:

```powershell
uv --offline --cache-dir .uv-cache run ruff check app scripts --select C901 --output-format concise
```

Result: expected nonzero measurement exit; **37 C901 findings**.

Origin binding:

```powershell
uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
```

Result: **origin-bind: ok**.

Deterministic evals:

```powershell
uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
```

Result: **273/273**, zero failures across sales 51, buyer 43, calendar 20,
website_handoff 15, safety 20, objection 20, routing 20, extract 30, writing 33, gold 21.

Diff hygiene before evidence edit:

```powershell
git diff --check
```

Result: exit **0**; only LF-to-CRLF working-copy warnings, no whitespace error.

## Decision and non-claims

**FAIL — G4 remains open and Phase 1.5 must not be approved.** Passing tests, migration
proof, evals, lint, and complete 164-row accounting do not supersede the three direct
adversarial reproductions.

This review does not claim live Gmail/Telegram/Google/Meta behavior, live PostgreSQL
concurrency, an applied production migration, deployment, AWS state, browser/device
voice behavior, or a pre-cleanup physical-line reduction.
