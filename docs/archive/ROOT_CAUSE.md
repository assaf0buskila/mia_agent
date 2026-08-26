# Root cause: website discovery loop + Telegram generic acknowledgment

Evidence date 2026-08-23. Live host `https://mia.assafweb.com`, image mia:10, `automation_mode=auto_approved`, `sales_llm=true`.

## Defect A — website discovery does not advance

### Observed (live production, session `web_caaa7423247d425b`)

```
MIA   ספר לי קצת איך נראה יום רגיל בעסק. במה אתה רוב הזמן עסוק?
USER  אני מוכר נעליים יש לי עיסוק רק במלאי
MIA   [deepen_pain] הבנתי, אתה מוכר נעליים ומתמקד בעיקר בניהול המלאי.
      מה הכי חשוב לך לקדם עכשיו: יותר פניות, אתר שמביא לקוחות, או אוטומציה...?
USER  להכניס הכל לשיטס
MIA   [offer_whatsapp] אצלנו ב-AssafWeb אסף בונה אוטומציות ועובדים דיגיטליים
      שיכולים לשמור את כל המידע באופן אוטומטי בגוגל שיטס... נמשיך בוואטסאפ?
USER  נעליים מידות דגמים
MIA   [quantify] האם אתם מחפשים אתר להצגת נעליים לפי מידות ודגמים,
      או מערכת שתעזור ללקוחות למצוא את הדגם והמידה המתאימים?
```

Three separate failures in four turns: a word-level echo instead of a meaning
reflection, a three-option menu question, a solution pitch before any friction was
understood, and a final turn that reinterprets the business from scratch and asks
about a website the prospect never mentioned.

### Code path

`app/api/website.py::process_website_message`
→ `app/graph/orchestrator.py::sales_next_action`
→ `app/domain/extract.py::extract_sales_signals`
→ `app/domain/sales.py::select_next_action`
→ `app/graph/replies.py::reply_for`
→ `app/integrations/sales_reply.py::OpenAISalesReplyPort.compose`

### Cause

1. **The reply port is stateless.** `compose()` receives only `action`, `canned`,
   `latest_message`, `channel`, `page_path`, `page_section`. It never receives the
   transcript, the persisted `SalesState`, or the questions already asked. The model
   therefore re-derives the business from a single fragment every turn. `נעליים מידות
   דגמים` in isolation reads like a catalogue request, which is exactly what turn 4
   produced. `compose()` also does `del action`, so the prompt does not even carry the
   deterministic intent it is supposed to phrase.
2. **No anti-repeat state.** `mark_action_delivered` records only `REFLECT`,
   `OFFER_HYPOTHESIS` and `OFFER_WHATSAPP`. Discovery questions are never marked as
   asked, so nothing in code or prompt forbids re-asking an answered question. When
   `extract_sales_signals` fails to set `workflow_known` (keyword-only matcher, so any
   short answer without a listed token misses) `UNDERSTAND_WORKFLOW` is re-selected and
   the opening line is emitted verbatim.
3. **Canned copy is generic and multi-question.** `WEBSITE_REPLIES[DEEPEN_PAIN]` is a
   three-option menu (`אין פניות, אין אתר, או שאין זמן`) unrelated to whatever the
   prospect described. Because `lint_customer_reply` rejects any candidate with more
   than one question mark, a good model answer that happens to contain two questions
   falls back to this generic menu. Fallback undoes the improved behaviour.
4. **WhatsApp offer fires too early.** `website_whatsapp_continuation_ready` needs only
   `workflow_known` plus `pain_level >= P2`. `שיטס` alone sets P2, so the offer fired on
   exchange two, before any friction, frequency or consequence was known, and it arrived
   attached to a product pitch.

### Affected files

`app/integrations/sales_reply.py`, `app/graph/replies.py`, `app/domain/sales.py`,
`app/domain/extract.py`, `app/graph/orchestrator.py`, `app/graph/state.py`,
`app/db/store.py`, `app/api/website.py`, `app/api/inbound.py`.

### Smallest safe repair

Keep `select_next_action` deterministic. Add a persisted discovery ledger (facts
learned, questions asked) and a conversation-turn read API, pass both plus the action
intent into the reply port, and replace the canned table with single-question copy that
is safe to fall back to. Gate the WhatsApp offer on real friction plus engagement rather
than a single pain token.

### Test plan

Multi-turn website tests asserting: no answered question re-asked, no verbatim repeat of
a prior Mia line, sales state monotonically progresses, one question mark per reply, no
pitch before friction is known, WhatsApp offered inside 3 to 6 meaningful exchanges.

## Defect B — Telegram returns the same acknowledgment

### Observed (exact production code path, `MIA_ENV=test`)

| Owner message | task type | reply |
| --- | --- | --- |
| `מה קרה היום?` | daily_brief | real digest |
| `תראה לי לידים חמים` | hot_leads | real hot-lead list |
| `תספרי לי על ליד מסוים` | sales | `נרשם כמשימת מכירות. לא ביצעתי אותה.` |
| `מה מחכה לאישור?` | owner_status | `אני כאן. זו קונסולת הבעלים…` |
| `תנתחי את השיחות מהאתר` | owner_status | `אני כאן. זו קונסולת הבעלים…` (identical) |
| `מחר תבדקי אם הליד חזר אלינו` | sales | `נרשם כמשימת מכירות. לא ביצעתי אותה.` |

Six unrelated requests collapse into three strings, two of them byte-identical.

### Code path

`app/api/telegram.py::receive_webhook`
→ `app/api/inbound.py::process_inbound_texts` (owner branch)
→ `app/domain/owner_tasks.py::classify_owner_task`
→ `promote_unclassified_text_to_status`
→ `ack_for_owner_task` / `app/domain/owner_status.py::format_owner_status_ack`

### Cause

1. **The owner channel has no model and no memory.** The reply is a keyword classifier
   feeding a fixed template table. The owner branch of `process_inbound_texts` never
   builds a graph, never calls a reply port, and never reads prior owner turns. A
   follow-up like `מה הכי מעניין?` has no antecedent to resolve against.
2. **One bucket absorbs everything unmatched.** `promote_unclassified_text_to_status`
   maps every unclassified owner message to `OWNER_STATUS`, and
   `format_owner_status_ack` ignores the message text entirely, so any unrecognised
   question yields the same digest plus the same menu.
3. **Classified-but-not-executed types share one template.** `sales`, `note`,
   `support`, `research` and `linkedin` all render
   `נרשם כמשימת {label}. לא ביצעתי אותה.`, so the reply carries no information about
   what was actually asked.
4. **No read API for owner history.** `LeadStore` persists `message_in` / `message_out`
   canonical events but exposes no method to list them for a conversation, so memory
   cannot be loaded even if a composer existed.

### Affected files

`app/api/inbound.py`, `app/domain/owner_tasks.py`, `app/domain/owner_status.py`,
`app/db/store.py`, plus a new owner reply composer.

### Smallest safe repair

Keep classification, permissions, approvals, write flags, risk levels, idempotency and
audit exactly where they are. Add an owner conversation-memory read, an owner
answer composer that phrases real persisted data over prior turns, and route the
previously-unclassified read questions (pending approvals, website conversation
analysis, single-lead lookup) to real data instead of a menu.

### Test plan

Six-message Telegram sequence asserting all replies differ and each contains data
relevant to its own question; follow-up question resolved from prior turn; unauthorized
user rejected before any model call; approval binds to one approval id; duplicate update
ignored; provider failure not reported as success.
