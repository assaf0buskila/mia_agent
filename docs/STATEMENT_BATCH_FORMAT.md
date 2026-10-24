# Statement batch format and flow (`statement-batch.v1`)

For the assistant that reads Assaf's card statement, and for whoever builds the screen. This is the
contract of V2-26 (`app/services/os_ledger/statements.py`); the domain rules are V2-25
(`app/domain/statements`). Nothing here is a bank credential: a batch never carries a card number,
and a card-like digit run anywhere in it is refused.

## The flow

1. **Submit.** The assistant (or Assaf, by paste) sends one batch. It becomes a `pending` batch for
   7 days. Nothing is booked yet.
2. **Review.** Assaf sees every line with the tag and category the server resolved, what would
   change, and what blocks approval. He can save a **draft** (a tag, a category, "always for this
   merchant", a choice between two hand entries). The draft lives on the server.
3. **Approve.** Approve sends the `review_digest` of exactly what he saw. If anything moved since
   (his draft, a hand entry, a rule), the answer is `409 review_stale` and he reviews again. If it
   matches, one transaction books the expenses, writes the statement and its lines, stores the rules
   he confirmed, deletes the line detail and records the receipt. Nothing is half done.
4. **Reject, expire, replace.** Reject, a 7-day expiry, a newer batch for the same statement and a
   revoked key all end the batch and delete its line detail the same way.

## The format

Send compact UTF-8 JSON: `json.dumps(batch, ensure_ascii=False, separators=(",", ":"))`. With
default escaping, 300 lines with long Hebrew merchant names exceed the 128 KiB cap; compact UTF-8
does not. At most **300 lines** and **131,072 bytes**.

```json
{
  "schema": "statement-batch.v1",
  "client_batch_id": "0b6f7a52-3c1d-4e8a-9f20-5d1c7e4b9a01",
  "account_label": "Business card",
  "charge_date": "2026-10-15",
  "lines": [
    {"txn_date": "2026-09-20", "merchant_display": "CLAUDE.AI SUBSCRIPTION", "amount_minor": 7500,
     "kind": "charge", "currency": "ILS", "confidence": 0.9, "suggested_tag": "business",
     "suggested_category": "AI", "suggested_path": "אוטומציה", "is_subscription": true,
     "original_amount_minor": 2000, "original_currency": "USD"}
  ]
}
```

| Field | Rule |
|---|---|
| `schema` | exactly `statement-batch.v1` |
| `client_batch_id` | a lowercase UUID the sender makes up; the same id with the same content is a replay, never a second batch |
| `account_label` | at most 40 characters, no run of 8 or more digits; names the card, never numbers it |
| `charge_date` | `YYYY-MM-DD`; the day the card is charged; no line may be dated after it |
| line `txn_date`, `merchant_display` (at most 80), `amount_minor` (a positive integer, agorot), `kind` (`charge` or `credit`), `currency` (`ILS`), `confidence` (0 to 1), `suggested_tag` (`business`, `personal` or `null`) | required |
| line `original_amount_minor` + `original_currency` | optional, together; a foreign charge (`USD`, `EUR`, ...) |
| line `installment` | optional `{"n": 2, "of": 6}` |
| line `suggested_category` (at most 40), `suggested_path` (`לימוד`, `אוטומציה`, `משותף`), `is_subscription` | optional |

Amounts are integers in minor units, never floats. Any other field is refused.
A failed batch is `422 invalid_statement_batch` with `details.issues`: a list of
`{"field", "code"}` and nothing else (never a value). The assistant never computes a `statement_id`
or a `line_key`: the server derives both with a secret pepper.

## What the assistant may read first

`context_for_assistant` returns the **business** merchant rules, the last 12 statements as
`{account_label, charge_date, status, line_count}`, and the exact format limits above. It never
contains a personal rule, a personal line, an amount or a merchant of one.

## Personal lines

A line tagged `personal` keeps only its key and its tag once the batch is decided. While the batch
is pending its detail exists in one table, `os_statement_batch_lines`, and nowhere else: not in the
proposal, the receipt, the event, the outbox, the mirror or any log. Reject, expiry, replacement and
approval purge it at once; the `os-retention` job deletes the purged rows, and also any line whose
batch expired more than a day ago, whatever its flag says. A personal merchant rule is stored by an
HMAC of the merchant key and holds no merchant text; its id is derived with the same pepper.

## Submitting twice

* The same `client_batch_id` and the same content from the same sender returns the stored batch.
  Different content under the same id is `409 idempotency_mismatch`. A batch that was already
  decided answers `409 already_decided`; read its status instead.
* A new batch for the same statement **replaces** a pending one from the same sender, unless that
  one has a saved draft. Then both stay open (`conflict: true`) and Assaf picks one; approving one
  replaces the others.

## Errors (HTTP status, then code)

| | |
|---|---|
| 404 | `operation_unavailable` (the feature is off), `batch_not_found` (also another key's batch) |
| 409 | `idempotency_mismatch`, `already_decided`, `decision_request_mismatch`, `review_stale`, `unassigned_lines`, `merge_choice_required`, `cycle_has_statement`, `draft_conflict` |
| 422 | `invalid_statement_batch`, `validation_error` (`details.reason`), `invalid_request_id`, `invalid_cursor` |
| 400 | `invalid_query` |
| 503 | `ledger_unavailable` (also a missing or changed pepper), `os_frozen` |

`validation_error` reasons on a draft: `too_many_overrides` (more than 100 in one POST),
`invalid_override`, `invalid_line_key`, `unknown_line`, `rule_without_tag` ("always for this
merchant" needs the tag in the same entry), `invalid_choices`, `choice_unknown_line`,
`choice_invalid`, `merge_choice_invalid`, `merge_choice_conflict`.

## Routes (`/v1/owner/os/unified`, owner session)

`GET /statements`, `GET /statements/{statement_id}`, `POST /statement-batches` (paste, 128 KiB),
`GET /statement-batches/{id}`, `GET /statement-batches/{id}/lines?after&limit`,
`POST /statement-batches/{id}/draft`, `POST /statement-batches/{id}/approve`,
`POST /statement-batches/{id}/reject`, `GET /merchant-rules`. Every body carries the v2 binding
(`api_version`, `source`, `workspace_id`, `authority_epoch`); the write bodies (`batch_created`,
`draft_saved`, `receipt_v2`) also carry `observed_version`.

A missing cycle is `null` in `GET /statements`, never `0`.

## Draft semantics

`POST .../draft` sends `{draft_revision, overrides: [...], choices?}`. Each POST is a **delta**: an
override entry replaces that line's stored override, an entry with every optional field `null`
removes it, and `choices` merge by key. A review needing more than 100 corrections is saved over
several POSTs. A stale `draft_revision` is `409 draft_conflict`. The response is
`{draft_revision, review_digest, blocking}`.

## Known limits

* A stale link (a hand entry that stopped fitting a line) offers only the entry already linked or
  "unlink"; it does not search for a better free hand entry (V2-25 round 3, optional item 5).
* The proposal's stored effects hold the business expense effects only. The `{line_key, tag:
  "personal"}` markers of contract 12.2 are not stored: the core receipt read resolves every stored
  effect as a ledger entity, which a marker is not. Personal lines are recorded as key-only rows.
* A statement's `cycle_from` and `cycle_to` are those of its first batch: the runtime role may not
  UPDATE them (contract 18.2), so a re-import that moves the date range keeps the original range
  in `GET /statements/{id}`. The totals and the lines are always the latest approval.
* The review of a batch that is no longer pending (decided, or restored without detail) shows its
  status and header with zero counts; its line detail is gone by design.
