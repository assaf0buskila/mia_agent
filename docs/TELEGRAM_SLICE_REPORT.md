# Telegram slice — deployment report

**Date:** 2026-08-24
**Branch state:** committed, working tree clean
**Tests:** `uv run pytest` → **2268 passed, 0 failed**. `uv run ruff check app tests scripts` → clean.
**Live now:** `mia:16` (task `mia:18`) — has the brain, does **not** have anything in this report.

Read this top to bottom before deploying. Section 1 is the finding that matters most; it may
be a **config fix, not a code deploy**.

---

## 1. Root cause: the owner agent has never actually run in production

`/health` reports `owner_agent: {"ready": true}`. That is true and useless: `owner_agent_ready()`
only checks the model **string is non-empty**. It never verifies OpenAI accepts it.

Evidence from Assaf's live Telegram session (2026-08-24 01:0x): Mia replied

> אני לא מפעילה כלי Composio — פייתון מנהל את החיבורים (Gmail, Calendar, Sheets, Meta ו-WhatsApp)

That is a word-for-word Hebrew paraphrase of rule 2 of the **`owner_telegram_v2`** prompt
(`app/integrations/owner_reply.py`):

> "Do not call, name, or request Composio tools. Python owns Gmail, Calendar, Sheets, Meta reads and WhatsApp send."

That prompt runs **only on the fallback path**, i.e. only when `answer_owner()` returns
`used_agent=False`. So the agent was constructed, raised, and silently degraded to the
pre-brain keyword classifier on every single turn.

This one cause explains all four reported symptoms:

| Symptom | Cause |
|---|---|
| "no Composio tools even when I push her" | Agent never ran; fallback forbids tools by design |
| "same long message every time" | `OPERATOR_SNAPSHOT` canned digest — pre-brain behaviour |
| "who's the watches guy?" → stats dump | No memory retrieval, because no agent |
| "Summarize my gmail" → stats dump | `gmail_summary` tool never invoked |

Corroborating: `/health` `brain.corpus.memories = 1` after a long session. Extraction was
failing too.

### Why `gpt-5.6-terra` and not `gpt-5.6-luna`

Researched against `developers.openai.com`. **`gpt-5.6-terra` is real, current and GA.** Its
model page lists `v1/chat/completions` as *Supported*, identical to luna. It is not deprecated,
needs no dated snapshot, and has **identical Tier-1 rate limits** to luna. No verification
gate, allowlist, tier gate or regional restriction appears on any GPT-5.6 model page.

The one documented mechanism that produces exactly this symptom — same key, same endpoint,
one model works and another does not — is **project model permissions**:

> "Use project model permissions to set an allowlist or denylist for a project."
> — [Admin APIs](https://developers.openai.com/api/docs/guides/admin-apis)

Terra costs 10× luna on input and output, which is a very common reason to restrict it.

**Check before deploying anything:**
- Dashboard → organization settings → the project → **Limits → Model usage**, or
- `GET /organization/projects/{project_id}/model_permissions`

**Caveat, stated plainly:** OpenAI does not document what error a blocked model returns, so
this is the strongest documented hypothesis, not a verified fact. The probe in §2 settles it
in one command.

### The immediate fix

Set the agent to the model that demonstrably works on this account:

```
MIA_OWNER_AGENT_MODEL          = gpt-5.6-luna
MIA_OWNER_AGENT_FALLBACK_MODEL = gpt-5.6-terra
```

`gpt-5.6-luna` is what the website sales path already uses, and the website works well.
Putting terra second means it is used automatically if/when the permission is granted.

---

## 2. `scripts/probe_owner_agent.py` — run this first

```bash
uv run python scripts/probe_owner_agent.py
```

Read-only. Never prints the API key. For every configured model it does:

1. `GET /v1/models/{id}` — proves the id exists, and surfaces `shutdown_date`, the only
   documented early warning for retirement.
2. One real completion with `max_completion_tokens: 1` — the **only** check that proves this
   key can call this model. (`/v1/models` scoping to project permissions is undocumented, so
   layer 1 alone cannot answer it.)

On 403/404 it prints the project-model-permissions hint from §1.

**Expected outcome:** luna `CALL ok`, terra `CALL FAILED http=404`. If terra also returns
`CALL ok`, the root cause is something else and the failure reason now appears in the logs
(§3) — send that line.

---

## 3. Two real bugs fixed in the agent path

### 3a. `MIA_OWNER_AGENT_FALLBACK_MODEL` was documented but ignored

`build_agent_client` built a client from `chain[0]` only and discarded the rest. A primary the
account cannot call dropped straight to the keyword classifier instead of trying the secondary.

Now `LlmModelChain` (`app/integrations/llm_client.py`) walks every configured model, with a
policy grounded in the OpenAI error docs:

| Condition | Behaviour | Why |
|---|---|---|
| 403 / 404 / 410 | **advance** to next model | this model will never work for this key |
| 400 | raise, do **not** advance | payload bug; every model rejects it identically |
| 429 / 500 / 503 | raise, do **not** advance | load, not access — must not silently demote on a rate limit |

### 3b. The fallback was silent — this is why it hid for a full day

`answer_owner` returned `used_agent=False` with no reason recorded and nothing logged.
A misconfigured model was indistinguishable from normal operation.

`OwnerBrainResult` now carries `fallback_reason` and `model`, and `app/api/inbound.py` emits
one line per owner turn via `log_owner_agent`:

```
owner_agent used=False model=gpt-5.6-terra task=operator_snapshot tools=- \
  reason=all models failed [gpt-5.6-terra:llm request failed: HTTP 404]
```

Reasons: `kill_switch_or_disabled`, `deterministic_intent` (approvals etc. — by design, not a
failure), `no_model_configured`, or the provider error with model and status.

**After deploy, grep the logs for `owner_agent used=`.** That single line answers "is the agent
running" definitively.

---

## 4. Lead headlines — the "who's the watches guy?" fix

The console listed leads as:

```
lead_82f527e3be5e · workflow · שלב ידני · עלות מאומתת · הוצע וואטסאפ
```

Every field true, none of it says who this is. Assaf asked *"מי זה הבחור של השעונים?"* about a
lead whose own conversation says he sells watches, and the console could not connect them.

**New:** `app/domain/lead_label.py` derives a short label from the prospect's **own words**,
persisted on `SalesState.headline`:

```
lead_82f527e3be5e · אז אני מוכר שעונים ורוב הזמן בוואטסאפ · workflow · שלב ידני · ...
```

- **The lead id stays full**, never truncated — Assaf references it back to Mia.
- Sanitised: every digit run, URL, email, and currency symbol is stripped, length-capped at 42
  chars. A label is a glance, not a quote.
- **Owner-facing only.** It is deliberately *not* used in
  `website_handoff_brief._recommended_first_line`, which is copied into a message sent to the
  **customer** and therefore stays on the strict topic allowlist. Assaf already sees the full
  transcript in the briefing, so echoing a fragment to him exposes nothing new.

**Requires a migration** — see §7.

---

## 5. The handoff briefing is now short

Before: two lines of preamble, an opaque id, facts one per line, then the whole transcript
inline. ~700+ chars, with the thing Assaf needs buried.

After (~510 chars, and the transcript is collapsed):

```
<b>ליד מהאתר → וואטסאפ</b>
lead_af13a10309f3 · אני מוכר שעונים ורוב הזמן בוואטסאפ
מיה לא תענה שם. תטפל אתה.

<b>מה ידוע</b>: יודעים מה העסק עושה · יש שלב ידני ברור · כאב P2

השורה שלך:
היי, דיברתם באתר על שלב שעדיין נעשה ידנית. בואו נמשיך מפה.

השיחה:
<blockquote expandable>לקוח: ...</blockquote>
```

Who it is on line one; facts on one line; the paste line; transcript one tap away.
`_notify_telegram` now sends `parse_mode: HTML` with link previews disabled. The markers
`השורה שלך:` and `השיחה:` are unchanged, so the existing contract and tests hold.

**Known limitation, not fixed:** the paste line still comes from the allowlisted topic table
(`_TOPIC_NEEDLES`), which has no entry for watches — hence the generic "שלב שעדיין נעשה ידנית".
Extending that table is safe and additive; changing the mechanism is not, because that string
is sent to a customer. Left as a deliberate decision for Assaf.

---

## 6. What is NOT fixed, and why

- **Composio tools returning real data** — cannot be verified until the agent actually runs
  (§1). The registry, allowlist and fail-closed behaviour are tested; live data is not.
- **Voice** — `voice_in: ready=true` and a real bug was fixed earlier (`verbose_json` sent to
  `gpt-transcribe`, which made every owner voice note fail silently). **No voice note appears
  in the live evidence.** Send one; that is the whole test.
- **Composio resource discovery** (`MIA_COMPOSIO_DISCOVERY`) — stays **false**. The live probe
  returned 404 on all three list tools. Likely the version pin, since discovery reuses each
  toolkit's version constant and the research explicitly warned against that. Not touched here.
- **`_notify_telegram` notifies only `sorted(owner_ids)[0]`** — unchanged, single owner today.

---

## 7. Deploy steps

1. **Set the models** (ECS task definition → plain `environment`, not Secrets Manager):
   ```
   MIA_OWNER_AGENT_MODEL          = gpt-5.6-luna
   MIA_OWNER_AGENT_FALLBACK_MODEL = gpt-5.6-terra
   ```
2. **Run the migration** as a one-off task **before** the new image serves traffic:
   `migrations/20260824_lead_sales_state_headline.sql` adds
   `lead_sales_state.headline VARCHAR(120) NOT NULL DEFAULT ''`. Additive; the runner skips
   duplicate columns, so re-running is safe.
3. **Deploy the new image.**
4. **Verify** — send one Telegram message, then check the logs:
   ```
   owner_agent used=True model=gpt-5.6-luna task=... tools=search_memory,...
   ```
   `used=True` with a non-empty `tools` list means the agent is genuinely running. `used=False`
   now prints the reason.
5. Optionally run `scripts/probe_owner_agent.py` first — it answers step 4 before deploying.

**Rollback:** image `mia:16`, or blank `MIA_OWNER_AGENT_MODEL` to fall back to the classifier
without redeploying.

**Headlines are populated going forward only.** Existing leads keep an empty headline and
display exactly as before; new website conversations get one on the first substantive turn.

---

## 8. Test coverage added

| File | Covers |
|---|---|
| `tests/unit/test_owner_agent_fallback.py` (13) | advance on 403/404/410; do **not** advance on 400/429/500/503; fallback model actually used; `fallback_reason` names model and status; `deterministic_intent` is not a failure |
| `tests/unit/test_lead_label.py` (18) | business description → label; filler → none; phones/emails/prices/URLs/digits never survive; length cap; full lead id preserved |
| `tests/unit/test_website_handoff_brief.py` (11, existing) | markers and no-token/no-price guarantees still hold against the new HTML layout |

Full suite: **2268 passed, 0 failed.**
