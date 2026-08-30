# Function Cleanup Seventh Review Repair

## Acceptance ledger

- [x] Sheets explicit negation denies all supported verbs before port, claim, idempotency, or provider effects.
  CHECK: focused real-registry SQLite counting-port Sheets regression
  EXPECT: all requested cases pass with zero side effects
  EVIDENCE: `test_owner_sheets_bounded_negation_modifiers_deny_before_all_side_effects` exercises `append`, `add`, `update`, `fill`, and `enter` against five bounded ordinary modifier families. Every denied case has zero port constructions, claims, provider operations, and `owner_sheets_write` idempotency rows.
- [x] Telegram accepts only normalized supported audio MIME families before STT, preserving authenticated-before-download ordering.
  CHECK: focused Telegram voice regression
  EXPECT: non-audio, blank, malformed, and generic MIME cases fail visibly with zero STT and OwnerGraph calls
  EVIDENCE: direct MockTransport rejects missing, blank, HTML, JSON, image, generic-binary, and malformed types; webhook regressions prove a visible fixed failure reply plus zero STT and OwnerGraph calls. `Audio/Ogg; codecs=opus` normalizes to `audio/ogg`.
- [x] Gmail recovery tests are independently isolated and pass alone plus both pair orders.
  CHECK: focused pytest invocations
  EXPECT: all commands pass
  EVIDENCE: each test passes alone and the pair passes in both requested orders using distinct per-test FakeGmailPort draft IDs; no production code or test fixture/config changed.
- [x] Owned code passes relevant regressions, lint, diff hygiene, and strict C901 measurement.
  CHECK: pytest, ruff, git diff --check
  EXPECT: no test/lint/diff failures and no added C901 finding
  EVIDENCE: relevant regression suite exit 0 (149 tests); owned-path Ruff passes; `git diff --check` exits 0. Strict C901 reports only pre-existing `owner_tools._website_kpis` (14), not a Telegram finding after the bounded helper extraction.

## Repair evidence

## Changes

- Sheets negation now accepts at most four explicitly listed ordinary connectors between
  `do not`/`don't`/`never`/`not` and a mutation verb. This closes `do not even` and
  similar explicit prohibitions without an unbounded catch-all. Quoted JSON literals are
  removed before classification as before.
- Telegram media accepts only `audio/mpeg`, `audio/mp4`, `audio/ogg`, `audio/opus`,
  `audio/wav`, `audio/webm`, and `audio/x-wav`. The normalizer lowercases the media type,
  permits only non-empty `name=value` parameters, and rejects absent, blank, malformed,
  HTML, JSON, image, and generic-binary types. The API repeats normalization immediately
  before STT as defense in depth for alternate media ports. Authentication still occurs in
  the webhook before the download path.
- The two Gmail recovery tests use distinct fake draft IDs (`draft_callback_recovery_isolated_1`
  and `draft_console_retry_isolated_1`) so process-lifetime SQLite state cannot make either
  test observe the other test's completed approval.
- `TelegramPort._download_voice_file` keeps HTTP/size validation separate from metadata
  resolution, retaining behavior while avoiding a new strict-C901 finding on `download_voice`.

## Four passes

1. Implemented bounded guards and real-path regressions.
2. Re-read against the registry grammar; added the `add` alias to the all-verb Sheets matrix.
3. Hunted defects with lint/strict complexity; fixed three line-length errors and extracted
   Telegram file download handling after C901 identified the touched method.
4. Re-ran the relevant regression suite, Gmail isolation permutations, lint, complexity
   measurement, and diff hygiene.

## Adversarial coverage

- Sheets: each of `append`, `add`, `update`, `fill`, and `enter` is denied with `do not even`,
  `do not accidentally`, `don't really`, `never under any circumstances`, and `not yet`.
  Positive English `fill`, positive Hebrew `הכנס`, and positive append whose quoted cell is
  `"do not even append"` still succeed.
- Telegram: direct adapter media responses with no content type, blank type, `text/html`,
  `application/json`, `image/png`, `application/octet-stream`, and `audio/ogg; broken` are
  denied. The webhook tests additionally prove zero STT calls and zero OwnerGraph calls.
- Gmail: the deferred/retry callback and console retry tests each pass alone, then pass in
  outbound→console and console→outbound order.

## Commands and results

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-seventh-final-regression `
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py `
  tests/unit/test_vnext_principal.py tests/unit/test_telegram.py tests/unit/test_transcribe.py `
  tests/unit/test_telegram_owner_outbound.py tests/unit/test_owner_gmail_console.py -q
```

Result: exit 0; 149 selected regressions passed (warnings only).

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-seventh-gmail-outbound-alone `
  tests/unit/test_telegram_owner_outbound.py::test_gmail_callback_recovers_deferred_and_failed_send_once -q
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-seventh-gmail-console-alone `
  tests/unit/test_owner_gmail_console.py::test_approved_gmail_send_deferrals_remain_retryable -q
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-seventh-gmail-pair-a `
  tests/unit/test_telegram_owner_outbound.py::test_gmail_callback_recovers_deferred_and_failed_send_once `
  tests/unit/test_owner_gmail_console.py::test_approved_gmail_send_deferrals_remain_retryable -q
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-seventh-gmail-pair-b `
  tests/unit/test_owner_gmail_console.py::test_approved_gmail_send_deferrals_remain_retryable `
  tests/unit/test_telegram_owner_outbound.py::test_gmail_callback_recovers_deferred_and_failed_send_once -q
```

Result: all four invocations exit 0 (one, one, two, and two tests respectively).

```powershell
uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py `
  app/integrations/telegram.py app/api/telegram.py tests/unit/test_owner_live_tools.py `
  tests/unit/test_telegram.py tests/unit/test_telegram_owner_outbound.py `
  tests/unit/test_owner_gmail_console.py
git diff --check
```

Result: Ruff reports `All checks passed!`; diff check exits 0 (only existing LF/CRLF warnings).

```powershell
uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py `
  app/integrations/telegram.py app/api/telegram.py --select C901 --output-format concise
```

Result: expected nonzero measurement result from one pre-existing finding:
`app/tools/registries/owner_tools.py:587:5 C901 _website_kpis (14 > 10)`.
`TelegramPort.download_voice` is absent from the result after the repair.

## Non-claims

- No `.env`, secret value, AWS resource, production database, Telegram endpoint, STT provider,
  Gmail provider, or other live provider was read or called.
- SQLite and fake ports prove local policy/order/idempotency behavior, not production provider
  behavior, concurrency, or upstream MIME behavior.
- This does not prove the whole suite, a deployment, a migration application, or live Telegram
  content types. It does prove the listed local regressions and pair-order isolation.

## Follow-up: generalized Sheets negation grammar

Parent reproduction found that the prior finite connector list still authorized ordinary unseen
adverbs such as `mistakenly`, `possibly`, and `unintentionally`. The matcher now permits at most
three arbitrary lowercase, non-punctuation word tokens between a negator and mutation verb, while
excluding `and`, `but`, `or`, `then`, `however`, `instead`, and `also`. This is bounded and clause
safe: punctuation cannot match as whitespace-plus-word, and a clause joiner prevents an earlier
negation from consuming a later affirmative operation.

The real-registry SQLite counting-port regression now denies every supported verb (`append`, `add`,
`update`, `fill`, `enter`) for the original modifier families plus representative unseen adverbs:
`mistakenly`, `possibly`, `unintentionally`, `whimsically`, and `prematurely`. Each denial asserts
zero port construction, claim, provider mutation, and idempotency rows. It also proves that
semicolon and `but` clause boundaries preserve a later affirmative append; quoted negation data,
English positive fill, and Hebrew positive append remain valid.

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-seventh-negation-followup tests/unit/test_owner_live_tools.py -q
uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py `
  tests/unit/test_owner_live_tools.py
uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py `
  --select C901 --output-format concise
git diff --check
```

Result: focused suite exit 0 (15 passed); Ruff passes; diff check exits 0 (existing LF/CRLF
warnings only). Strict C901 still reports only the pre-existing `_website_kpis` finding (14 > 10),
not the changed negation helper.

## Follow-up: turn-level fail-closed negation

The bounded-word grammar was intentionally replaced after review found that any finite distance
still leaves a longer-distance bypass and has an equivalent Hebrew problem. After JSON-quoted
cell literals are masked, any standalone explicit English negator (`not`, `never`, `do not`,
`don't`) or Hebrew negator (`לא`, `אל`) anywhere in the owner turn makes every Sheets write
ineligible. This deliberately rejects a turn containing a separate-clause negation and a later
affirmative operation; an owner must resend one clean write request. Quoted negation remains data
because the existing JSON-string mask runs before this check.

The real-registry/SQLite counting-port regression covers arbitrarily long English and Hebrew
negation distance, English and Hebrew separate-clause negation, all supported write verbs, clean
English/Hebrew positives, and quoted-negation data. Every new denial proves zero port construction,
claim, provider mutation, and idempotency persistence.

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-seventh-negation-turn tests/unit/test_owner_live_tools.py -q
uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py `
  tests/unit/test_owner_live_tools.py
uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py `
  --select C901 --output-format concise
git diff --check
```

Result: focused suite exit 0 (15 passed); Ruff passes; diff check exits 0 (existing LF/CRLF
warnings only). Strict C901 remains limited to the pre-existing `_website_kpis` finding (14 > 10).
