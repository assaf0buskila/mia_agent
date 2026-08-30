# Phase 1.5 function cleanup — eighth review repair

Date: 2026-08-28

## Acceptance ledger

- [x] **Sheets Hebrew standalone negation:** quoted JSON literals are masked, while unquoted
  `לא` / `אל` use Hebrew-letter boundaries (`\u05d0-\u05ea`) and therefore deny on comma,
  parentheses, maqaf, colon/dash, newline, long-distance and separate-clause forms without
  matching `אל` inside `אלופה`.
  - EVIDENCE: real registry + SQLite `LeadStore` counting-port test
    `test_owner_sheets_bounded_negation_modifiers_deny_before_all_side_effects` passed. Each
    denial left `claim_operation` and port construction at 4 (the prior clean/quoted/embedded
    positives only), owner operations at 4, and no denial added an idempotency row or provider
    operation.
- [x] **Voice retry idempotency:** the canonical Telegram `webhook_events` row is claimed after
  numeric allowlist authorization and before media download/STT. `process_owner_texts` can reuse
  only a supplied event id whose existing row is exactly `received` for the same provider,
  channel and envelope kind.
  - EVIDENCE: route tests prove duplicate success performs one download, one STT, one OwnerGraph
    call and one reply; duplicate failure performs one visible reply. A direct graph test proves
    an arbitrary `preclaimed_event_id` with no row returns a duplicate and sends nothing.
- [x] **Alternate media ports:** one central validator requires non-empty actual `bytes`, at most
  16,000,000 bytes, and a normalized supported audio MIME. It runs in `TelegramPort.download_voice`
  and immediately before STT.
  - EVIDENCE: default-adapter empty/oversize tests and route-level alternate-port tests for empty,
    oversize, non-bytes, absent/malformed/wrong-type MIME all return the safe visible failure with
    zero STT and OwnerGraph calls. Existing parameterized `Audio/Ogg; codecs=opus` test still
    normalizes to `audio/ogg`; default host/path/HTTP tests remain in the Telegram suite.
- [x] **Gmail test isolation:** both named recovery tests allocate a new short fake draft id per
  invocation, preserving the production resource/hash binding assertions.
  - EVIDENCE: named tests pass twice via two sequential `pytest.main(...)` calls in one Python
    process and pass in both file orders.
- [x] **Mechanics:** owned-path Ruff and diff whitespace checks pass; strict C901 was measured.
  - EVIDENCE: normal Ruff reported `All checks passed!`; `git diff --check` exited 0 (only
    repository-wide CRLF warnings). Strict C901 reported the two pre-existing touched-path
    offenders: `process_owner_item` (55) and `_website_kpis` (14); this repair added no C901
    offender.

## Exact changes

1. `app/tools/registries/owner_tools.py` changes the Hebrew negator arm from whitespace/punctuation
   context to Hebrew-letter boundaries, treating Hebrew maqaf as a separator but not matching
   syllables inside Hebrew words.
2. `app/api/telegram.py` claims an authorized voice event before download, uses the claim for
   one-shot failure replies, and passes an explicit preclaimed event/kind to the owner text path
   only after successful STT.
3. `app/api/owner.py` verifies the precise existing `received` canonical claim before it skips
   the normal claim. The channel still reauthorizes the numeric owner on this path.
4. `app/integrations/telegram.py` supplies `validate_telegram_voice_media`; the default adapter
   and the STT boundary share it.
5. The affected unit tests add real-registry/SQLite Sheets denials, real webhook duplicate and
   alternate-media outcomes, preclaim-forgery denial, default empty/oversize adapter coverage,
   and fresh Gmail fake IDs.

## Commands and results

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-eighth-repair-regression `
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py `
  tests/unit/test_vnext_principal.py tests/unit/test_telegram.py tests/unit/test_transcribe.py `
  tests/unit/test_telegram_owner_graph.py tests/unit/test_telegram_owner_outbound.py `
  tests/unit/test_owner_gmail_console.py -q
```

Result: **162 passed**.

```powershell
uv --offline --cache-dir .uv-cache run python -c "... pytest.main(named_paths) twice ..."
```

Result: **2 passed, then 2 passed in the same Python process**.

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-eighth-repair-pair-a <outbound then console>
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-eighth-repair-pair-b <console then outbound>
```

Result: **2 passed** in each order.

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-eighth-repair-adversarial `
  tests/unit/test_owner_live_tools.py::test_owner_sheets_bounded_negation_modifiers_deny_before_all_side_effects `
  tests/unit/test_telegram.py tests/unit/test_telegram_owner_graph.py -q
```

Result: **33 passed**.

```powershell
uv --offline --cache-dir .uv-cache run ruff check <owned app and test paths>
uv --offline --cache-dir .uv-cache run ruff check <touched app paths> --select C901
git diff --check
```

Result: Ruff clean; strict C901 reports only the two existing functions named above; diff check
clean (CRLF warnings only).

## Four-pass record

1. Implemented the four bounded fixes at their actual side-effect boundaries.
2. Re-read the flows for trust/ordering: authorization precedes claim; claim precedes download;
   only a matching received claim permits reuse; failure replies use that same claim.
3. Ran punctuation/maqaf/embedded-word, duplicate success/failure, forged-preclaim, default and
   alternate-port media, and same-process Gmail adversarial checks.
4. Removed the fixture-ID length regression discovered by the first focused run, then reran the
   focused regression and final mechanics.

## Non-claims

- No full suite was run; the parent owns the integrated full mechanics.
- No `.env`, AWS, deployment, migrations, live Telegram/STT/Gmail/Sheets providers, or TTS path
  was inspected or invoked.
- Fake-adapter outcomes prove these local boundary contracts, not production credentials, provider
  availability, rate limits, or real-device voice behavior.

## Parent follow-up — same-process Telegram route repeatability

The initial duplicate-success regression used a fixed Telegram update id, which collided with the
process-lifetime SQLite webhook row when that same test was invoked by sequential `pytest.main(...)`
calls. Test-only update allocation now uses a module-level monotonic counter. Every route test gets
a fresh update id per invocation; each duplicate success/failure test stores that one id locally and
uses it for both the first request and its retry. Parameterized invalid-MIME and alternate-media
route cases also allocate at invocation time, so their cases cannot collide on a same-process rerun.

```powershell
uv --offline --cache-dir .uv-cache run python -c "... pytest.main([duplicate-success, duplicate-failure]) twice ..."
```

Result: **2 passed, then 2 passed in the same Python process**.

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-eighth-followup `
  tests/unit/test_telegram.py tests/unit/test_telegram_owner_graph.py `
  tests/unit/test_transcribe.py tests/unit/test_owner_live_tools.py -q
```

Result: **52 passed**.

```powershell
uv --offline --cache-dir .uv-cache run ruff check tests/unit/test_telegram.py `
  app/api/telegram.py app/api/owner.py app/integrations/telegram.py
git diff --check
```

Result: Ruff clean; diff check clean (repository CRLF warnings only).

## Parent follow-up — malformed alternate media return contracts

`_transcribe_telegram_voice` now awaits the media port before attempting unpacking. Only the
return-shape unpack (`TypeError` / `ValueError`) is classified as a media-contract failure;
validation remains the central byte/MIME check. STT is in its own exception boundary, so an
arbitrary downstream `TypeError` or `ValueError` is not silently converted into a media failure.
Cancellation and `BaseException` are not caught.

The real webhook-route regression covers `None`, a one-item tuple, a three-item tuple, a string,
and a two-key mapping. For every case the first request takes the safe visible one-time failure
path, marks the already-claimed webhook through the route, and the retry performs no second
download, STT, OwnerGraph call, or reply.

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-eighth-malformed `
  tests/unit/test_telegram.py tests/unit/test_telegram_owner_graph.py `
  tests/unit/test_transcribe.py tests/unit/test_owner_live_tools.py -q
```

Result: **57 passed**.

```powershell
uv --offline --cache-dir .uv-cache run ruff check tests/unit/test_telegram.py `
  app/api/telegram.py app/api/owner.py app/integrations/telegram.py
uv --offline --cache-dir .uv-cache run ruff check app/api/telegram.py app/api/owner.py `
  app/integrations/telegram.py --select C901
git diff --check
```

Result: Ruff clean; strict C901 reports only pre-existing `process_owner_item` (55); diff check
clean (repository CRLF warnings only).
