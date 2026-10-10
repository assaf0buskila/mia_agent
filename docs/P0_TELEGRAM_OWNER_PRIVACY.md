**MIA: תיקון פרטיות P0 לבעלים ב־Telegram — ראיות והוראות שחרור**

בסיס הענף `codex/mia-telegram-owner-privacy` הוא בדיוק
`27168610ec3e8cbd3aee836f70b00d6807743f6d`. העבודה בוצעה ב־worktree המבודד
`.codex-worktrees/mia-telegram-owner-privacy`. מסמך זה מתעד בדיקות מקומיות;
תוצאות CI וה־SHA של ה־PR נבדקים בנפרד לאחר יצירתו. Merge ו־deployment דורשים
אישור נפרד. לא נקרא `.env`, לא בוצעה פעולה בפרודקשן ולא שונו סודות או הגדרות Telegram.

הכשל בבסיס אפשר לבעלים מורשה לפנות מקבוצה ולקבל בה תשובות או תוצאות אישור.
מעתה נדרשים יחד: סוד webhook תקין, שולח בעל מזהה מספרי מורשה, סוג צ׳אט
`private`, ושוויון מדויק בין מזהה הצ׳אט למזהה השולח. שם משתמש אינו הרשאה.
גם צ׳אט פרטי של בעלים מורשה אחר אינו יעד תקין עבור השולח הנוכחי.

| קובץ | שינוי |
| --- | --- |
| `app/integrations/telegram.py` | הודעות ו־callbacks דורשים sender/chat IDs מסוג integer חיובי ודוחים bool, float, string, list ו־mapping. אין השלמת chat ID חסר מהשולח. נשמר `chat_type`; `is_valid_telegram_owner_context` מחייב allowlist, מזהה ASCII מספרי חיובי, `private` ו־`chat_id == from`. |
| `app/api/telegram.py` | אימות לפני claim, background task או callback acknowledgement. הוסר `Depends(get_db)` מה־webhook: session נפתח באמצעות מחזור החיים הקיים רק אחרי אימות ורק להודעה או callback מזוהה ותקין. בקשות נדחות אינן יוצרות session. נשמרים commit של claim, rollback, HTTP 503 וסגירה. callback פגום אינו נופל למסלול הודעה באותו payload. `_handle_callback` מאמת שוב לפני החלטה, adapters, עריכה ו־fallback. |
| `app/workers/telegram_owner.py` | אימות חוזר לפני session, הורדת מדיה, תמלול, ראיית תמונה, איחוד הודעות, typing, מודל ותשובה. ההקשר הפרטי נשמר ואין השלמת יעד חסר. |
| `tests/unit/test_telegram_owner_privacy.py` | 101 מקרי פרטיות; מתוכם 24 חדשים בעת סגירת ה־PR, כולל בדיקות HTTP שחוסמות כל ניסיון לקרוא ל־session factory, ודחיית סוד webhook. |
| `tests/unit/test_owner_model_led_surface.py`, `test_release_handoff_regressions.py`, `test_telegram.py`, `test_telegram_format.py`, `test_telegram_owner_outbound.py`, `test_turn_coalesce.py` | הוספת מטא־דאטה של צ׳אט פרטי ל־fixtures חיוביים והתאמת תוצאת parser ל־`chat_type`. אין החלשת assertions או הסרת בדיקות. |
| `.github/workflows/ci.yml` | הוספת קובצי בדיקות הפרטיות ו־`test_due_scan_worker.py` לרשימת בדיקות job ה־PostgreSQL. אין שינוי ב־workflow אחר. |
| `docs/P0_TELEGRAM_OWNER_PRIVACY.md` | תיעוד הגבול, הבדיקות, הדילוגים שהושלמו והוראות השחרור. |

Group, supergroup, channel, סוג חסר/לא מוכר, מזהים חסרים/לא תואמים ו־inline
callback נדחים. גם סמני `sender_chat`, `business_connection_id`,
`guest_query_id`, `guest_bot_caller_user` ו־`guest_bot_caller_chat` נדחים
לפי נוכחותם, כולל null. הודעות ערוכות, קול, אודיו/מסמך קולי ותמונה עוברות
אותו גבול. Forward שהבעלים שולח בפרטי נשאר תקין לפי זהות השולח הנוכחי.

דחיית הקשר אינה מפעילה מסד, claim, הורדת מדיה, תמלול, מודל, אישור/דחייה של פעולה
או תשובת בעלים. callbacks לא מורשים אינם מקבלים acknowledgement.
callback לא מזוהה מהבעלים המאומת בפרטי ממשיך לקבל acknowledgement ריק,
בלי פתיחת session או פעולה עסקית, בהתאם להתנהגות הקודמת.
לוגי דחייה כוללים reason codes ובוליאנים בלבד. יעדי תשובות, כרטיסי אישור,
כשל מדיה, timeout, עריכת תוצאה ו־fallback נובעים מהצ׳אט הפרטי שאומת.

לא שונו לוגיקת CRM, תזכורות, Google Sheets, Leo או האתר. לא נוספו migration
או הגדרה. התראות מתוזמנות ממשיכות לבחור נמענים מהמזהים המוגדרים.
claim לפי update ID והחלטה חד־פעמית לפי approval ID נשמרו.

**בדיקות מקומיות, 11.10.2026**

| בדיקה | תוצאה | ראיה מקומית שאינה נכללת ב־commit |
| --- | --- | --- |
| הסוויטה המלאה, עם PostgreSQL זמני | 2,748 עברו, אפס דילוגים, 929 אזהרות; exit 0, 120.09 שניות | `.cache/privacy-pr-full-final.log` |
| כל 12 המקרים שדולגו בעבר | 12 עברו; אין דילוג | `.cache/privacy-skipped-postgres.log` |
| בדיקות הפרטיות | 101 עברו | `.cache/privacy-db-owner-privacy.log` |
| סוויטת Telegram ממוקדת | 336 עברו | `.cache/privacy-db-telegram-suite.log` |
| בדיקות owner surface | 35 עברו | `.cache/privacy-db-owner-surface.log` |
| שחזור HTTP עצמאי של הקמת session לפני דחייה | 8 עברו אחרי התיקון; לפניו 8 נכשלו | `.cache/privacy-db-acceptance.log`, `.cache/final-acceptance-db.log` |
| Ruff, widget behavior, syntax של widget, origin bind, בדיקות settings ו־diff whitespace | עברו | פקודות מקומיות עם exit 0 |
| הוכחת רגרסיה מול הבסיס הפגיע | 30 מקרים נכשלו כצפוי בעת claim או acknowledgement אסור | `.cache/privacy-red-matrix.txt` |
| סקירת אבטחה עצמאית HEAVY חוזרת | PASS; 243 עברו ו־1 דילוג בסקירת הרגרסיות (הושלם בהרצת PostgreSQL); 16 בדיקות עצמאיות נוספות עברו | `.cache/final-acceptance-review.md` |

סקירת הסגירה זיהתה תחילה session שנוצר לפני אימות דרך FastAPI.
הממצא תוקן באמצעות דחיית `get_db`; לא הסתפקנו בהיעדר SQL בסשן ריק.
הסקירה הסופית חלה על הקוד לאחר התיקון ועל שינוי ה־CI.

הבדיקות המקומיות השתמשו ב־Python 3.14.3, pytest 8.4.2 ו־virtualenv קיים.
CI משתמש ב־Python 3.12. בכל הבדיקות האחרונות נבחר הקוד מה־worktree במפורש,
באמצעות `PYTHONPATH` ו־`python -m pytest`, כדי שהתקנת editable משותפת
לא תבחר checkout אחר. פלט pytest הועבר לקובץ.

**12 המקרים שהיו תלויים ב־PostgreSQL**

הם הורצו מול container חדש ומבודד של
`pgvector/pgvector:0.8.6-pg18-trixie`, בפורט מקומי נפרד, באמצעות
`MIA_TEST_POSTGRES_URL`. כל בדיקה משתמשת בסכמת בדיקה נפרדת; לא נעשה שימוש
במסד קיים או בפרודקשן. הסוויטה הכללית ממשיכה להשתמש במסד הבדיקות הרגיל.

| קובץ ומקרי בדיקה | מספר מקרים | קשר לגבול האבטחה |
| --- | ---: | --- |
| `test_brain_knowledge_postgres.py::test_postgres_partial_and_concurrent_refresh_reuse_deterministic_chunk_rows` | 1 | refresh מקביל של ידע; אינו בודק הרשאת Telegram |
| `test_crm_diagnostics_cli.py::test_plain_postgres_dsn_runs_in_read_only_isolated_schema` | 1 | diagnostics בסכמה מבודדת |
| `test_crm_v2.py::test_postgres_concurrent_identical_missing_contact_observation_creates_one_issue` | 1 | יצירת issue מקבילית |
| `test_crm_v2.py::test_postgres_revision_compare_and_swap_uses_dedicated_schema` | 1 | revision compare-and-swap |
| `test_crm_v2.py::test_postgres_issue_creation_serializes_with_final_contact_effect` | 1 | סנכרון issue עם contact effect |
| `test_due_scan_worker.py::test_due_reminder_claim_survives_outer_rollback_after_accepted_send` | 1 | רלוונטי לדרישה לשמר התראות ו־dedup: claim נשמר אחרי rollback של caller |
| `test_form_intake.py::test_postgres_concurrent_retry_creates_one_capture` | 1 | intake מקביל |
| `test_phone_identity.py::test_postgres_concurrent_local_and_international_events_resolve_one_contact` | 1 | איחוד זהויות טלפון |
| `test_v2_migration_postgres.py::test_v2_sql_creates_schema_and_preserves_existing_contact_history` | 1 | migration והיסטוריה קיימת |
| `test_website_v2_postgres.py::test_concurrent_messages_preserve_history_and_deduplicate` | 2 | היסטוריית אתר ו־dedup מקביל; שני פרמטרים |
| `test_website_v2_postgres.py::test_async_http_concurrency_does_not_block_event_loop_on_session_lock` | 1 | נעילת session באתר |

אף אחד מ־12 המקרים אינו בודק ישירות את הרשאת הצ׳אט הפרטי.
בדיקות הפרטיות מוכיחות שהדחייה מתרחשת לפני כל session, ובודקות מסלולים חיוביים
יחד עם הבדיקות הקיימות. עסקאות approval תקינות נבדקות מול SQLite וספקים מדומים;
אין כאן בדיקת end-to-end חדשה של approval מול PostgreSQL ו־Telegram חי.
job ה־PostgreSQL ב־CI מריץ כעת גם פרטיות וגם את בדיקת rollback של התזכורת.
ה־job הכללי עדיין צפוי לדלג על 12 המקרים בלי DSN; זו אינה השלמתם בפני עצמה,
ולכן יש לדרוש PASS גם ב־job הייעודי.

**שחזור הבדיקה המקומית**

`MIA_TEST_POSTGRES_URL` חייב להפנות למסד בדיקה חד־פעמי בלבד.

```powershell
$env:MIA_ENV = 'test'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = (Get-Location).Path
$env:UV_PROJECT_ENVIRONMENT = 'C:\Users\lenovo\Desktop\assaf project\assaf_agent\.venv'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.cache\uv'
# Set MIA_TEST_POSTGRES_URL to a newly created disposable PostgreSQL instance.
uv run --no-sync python -m pytest --basetemp=.cache/pytest-privacy-pr-final -rs > .cache/privacy-pr-full-final.log 2>&1
uv run --no-sync ruff check app tests
node tests/unit/widget_behavior.test.js
node --check app/web/ask_mia.js
uv run --no-sync python scripts/assert_origin_bind.py
pwsh -NoProfile -File ops/gcp/tests/test-settings.ps1
git diff --check
```

**סיכונים שנותרו וקבלת שחרור**

- לא נבדקו Telegram חי, מכשיר בעלים, מודל חי או פרודקשן. בדיקות מקומיות ו־CI
  אינן הוכחת deployment או איכות ספק חי.
- התיקון אינו מוחק הודעות רגישות שאולי נשלחו בעבר לקבוצות.
- `run_owner_loop` וה־compatibility facade הפנימיים מניחים caller מאומת.
  הסקירה לא מצאה כניסה חיצונית שעוקפת את ה־worker המוגן; caller חדש חייב לשמר את הגבול.
- לפני שחרור יש לוודא שגרסת הפרודקשן עדיין תואמת לבסיס. אם התקדמה,
  נדרשים התאמה, בדיקות וסקירה על ה־SHA החדש.
- אישור CI חייב להתייחס ל־head SHA המדויק של ה־PR. אחרי merge יש לדרוש CI
  גם על SHA ה־master המדויק לפני deployment.

**הוראות שחרור עתידי, לאחר אישור נפרד**

1. לאשר בנפרד merge ו־deployment. להפעיל את נוהל `README.md`
   ו־`ops/gcp/README.md` מ־checkout נקי של SHA שעבר CI.
2. לעצור jobs וליצור גיבוי מסד מאומת לפי הנוהל. אין לתיקון migration חדש
   ואין צורך בשינוי סוד או webhook.
3. להציב SHA מאושר ב־`$ReleaseSha` ולהפעיל:
   `.\ops\gcp\deploy.ps1 -ProjectId mia-assafweb -Ref $ReleaseSha`.
4. לאמת `/health` עם `deployment.commit_sha` המדויק,
   `/health/ready` עם HTTP 200 ו־`sudo mia status`.
5. לבצע קבלת Telegram מבוקרת: טקסט, עריכה, קול ותמונה בפרטי; אישור ודחייה
   של פעולה בטוחה בפרטי; חסימה שקטה בקבוצה/סופרקבוצה/ערוץ; replay ללא פעולה נוספת;
   fallback רק בפרטי והתראה מתוזמנת לנמען המוגדר. פעולות חיות דורשות אישור נפרד.
6. אם הקבלה נכשלת, לעצור את השחרור. חזרה לבסיס הפגיע מחזירה את פרצת הפרטיות.
   לאחר קבלה תקינה להחזיר jobs לפי הנוהל ולבדוק reason codes ללא תוכן רגיש.
