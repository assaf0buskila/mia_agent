# פרומפטים מתואמים: אתר AssafWeb ↔ Mia ↔ AssafWeb OS Business

שני הפרומפטים מיועדים לריצה בשני checkouts מבודדים מול אותו חוזה קיים. הם אינם אישור לפריסה, להפעלה או לגישה לנתוני לקוחות.

## 1. אתר AssafWeb

```text
מטרה: להשלים בחיבור מקומי ומבוקר את מסירת הלידים שכבר נשמרו בטופסי AssafWeb אל חוזה הקליטה הקיים של Mia. עבוד רק ב-checkout מבודד של אתר AssafWeb וקרא תחילה את AGENTS.md ואת הקוד הפעיל. בסיס המקור שאומת לחוזה הזה הוא worktree בשם `owner-os-baseline-intake-check` ב-SHA המלא `85d939987b7d0355f7717c9d3afbcdd9c6c5d402`; ה-checkout הראשי הנוכחי נמצא בגרסה אחרת. אמת `git rev-parse HEAD` לפני עבודה. אם אינך על הבסיס המאומת, אתר אותו דרך `git worktree list` או דווח על אי-ההתאמה ועצור; אל תניח שהנתיבים קיימים בגרסה אחרת ואל תשחזר אותם מהפרומפט.

החוזה הקיים הוא מקור האמת: POST /api/lead שומר תחילה את הליד ב-Supabase ומחזיר את UUID הרשומה. השתמש באותו UUID בלתי משתנה כ-source_id. מסירת CRM קיימת ב-lib/lead-crm.ts ושולחת רק אל POST https://mia.assafweb.com/v1/internal/assafweb/form-leads עם X-AssafWeb-Intake-Secret בצד השרת. מסלול ההתאוששות הקיים הוא POST /api/ops/crm-sync; הוא רשאי לסיים רשומה כ-synced רק אחרי תשובת captured או replayed תקינה עם contact_id ו-activity_id, ובאותו crm_lease_id. שמור את מנגנון ה-lease, את נקודת החיתוך MIA_FORM_INTAKE_STARTED_AT ואת מצב ברירת המחדל הכבוי.

אל תיצור endpoint קליטה חדש, CRM חדש, טבלה חלופית או מזהה אחר. אל תעבוד על P0B או על שכתוב כללי של תור המסירה. אל תעביר IP hash, gclid, מזהי Meta, browser identifiers, טענת תשלום או תוכן שאינו ברשימת השדות הקיימת. אל תבצע replay היסטורי. אל תשלח הודעה אוטומטית ללקוח. אל תקרא או תדפיס סודות. אל תשנה את Google Sheets הפיננסי: הוא מקור TEST נפרד לנתוני business-ledger ואינו CRM.

אם הקוד הקיים כבר עומד בחוזה, אל תשכתב אותו; הוסף רק את החיבור או הבדיקה החסרים הקטנים ביותר. הוכח מכנית: (1) UUID הרשומה שחזר מ-Supabase עובר ללא שינוי כ-source_id; (2) duplicate/retry עם payload זהה מתקבל דרך אותה קבלה ואינו יוצר מסירה שנייה; (3) payload שונה תחת אותו UUID נעצר כ-conflict; (4) timeout/5xx נשאר retry ואינו מסומן synced; (5) test/internal ולידים לפני נקודת החיתוך אינם נשלחים; (6) לוגים ותשובות אינם חושפים פרטי ליד או secret.

הרץ לפחות:
- node --test scripts/lead.test.mjs scripts/lead-crm.test.mjs
- npm run lint
- אם יש DSN מקומי מפורש ולא סודי: node --test scripts/lead-crm-postgres.test.mjs; אחרת דווח SKIPPED, לא PASS.

פלט נדרש: רשימת קבצים מדויקת, תוצאות בדיקות PASS/FAIL/SKIPPED, תרשים קצר של source UUID → lease → Mia receipt → synced, והצהרה מפורשת שאין deploy, הפעלה, secret change, replay היסטורי, הודעת לקוח או שינוי ב-ledger הפיננסי. אל תבצע commit, push, merge או פעולה חיצונית.
```

## 2. Mia ו-AssafWeb OS Business

```text
מטרה: להשלים ולאמת מקומית את הצד של Mia בחוזה הקיים לליד מטופס AssafWeb, כך שקבלה קנונית תוביל לרשומת owner-read קיימת ב-AssafWeb OS Business. עבוד רק ב-worktree מבודד של assaf_agent וקרא AGENTS.md, HANDOFF.md, MIA_V2.md ו-TASKS.md לפני שינוי.

אל תמציא route חדש. השתמש ב-POST /v1/internal/assafweb/form-leads שב-app/api/form_intake.py, עם X-AssafWeb-Intake-Secret, body מוגבל ורשימת השדות של AssafWebFormLead. source_id הוא ה-UUID הבלתי משתנה של public.leads באתר. app/services/form_intake.py חייב להמשיך לבצע באותה טרנזקציה: נעילת idempotency ל-source_id, בדיקת payload_sha256, CrmService.capture_form_lead, יצירת Contact + Activity + תור המסירה הקיים, ושמירת CrmFormIntakeReceiptRow לפני acknowledgement. replay זהה מחזיר את אותם contact_id ו-activity_id; payload שונה תחת אותו source_id מחזיר 409; test/internal מוחזרים ignored ואינם יוצרים CRM.

אמת שהרשומה שנקלטה ניתנת לקריאה רק דרך נתיבי ה-owner הקיימים והמוגנים: GET /v1/owner/os/contacts, GET /v1/owner/os/contacts/{contact_id}, GET /v1/owner/os/contacts/{contact_id}/activities ו-GET /v1/owner/os/today?sections=next_actions. אין להוסיף endpoint OS חדש ואין להרחיב הרשאות. אימות הבעלים נשאר numeric Telegram owner + session/scopes קיימים; אין username auth ואין מידע בעלים בנתיב הציבורי.

אל תעבוד על P0B, אל תיצור CRM נוסף, אל תבצע import/replay היסטורי, ואל תפעיל הודעות אוטומטיות חדשות. אל תשלח הודעות לקוח. אל תקרא סודות, אל תשנה production/customer data ואל תיגע בפריסה, migrations חיות או activation flags.

שמור את הפיננסים מופרדים מה-CRM. ה-Slice C המקומי הקיים משתמש ב-Google Sheets כמקור הסמכות היחיד של נתוני `business-ledger` במצב TEST: `GET /api/business/finance` ב-BFF ממפה רק אל `GET /v1/owner/os/business/finance`; ארבעת נתיבי הכתיבה הקבועים `POST /api/business/schedules`, `POST /api/business/payments`, `POST /api/business/expenses`, `POST /api/business/projects` ממפים בהתאמה רק אל אותם שמות תחת `/v1/owner/os/business/`. קבלות נקראות רק דרך `GET /api/business/receipts/{request_id}` אל `GET /v1/owner/os/business/receipts/{request_id}`. שמור גם את `GET /v1/owner/os/business` ואת `POST /v1/owner/os/business/deals` הקיימים. אין נתיב ספק שרירותי ואין מקור פיננסי חלופי.

כל כתיבה פיננסית דורשת מסך אישור אנושי מדויק וקצר-תוקף לפני POST יחיד. תשלום הוא תיעוד ידני בלבד של סכום שהתקבל בפועל מול חיוב מדויק. תוצאת POST שאבדה נשארת `UNKNOWN`: חסום כל כתיבה עסקית נוספת ובדוק רק קבלה עם אותו `request_id`; אין retry או resend אוטומטי, ו-404 בקבלת השחזור אינו הוכחת כישלון. snapshot פיננסי עם כיסוי `unknown`, שגיאת קריאה או מידע מיושן מוצג כ״טרם נאמן״/״כיסוי חלקי״ ולעולם לא כאפס. אין לטעון שהנתונים הם הכנסה, revenue, תשלום מאומת אצל ספק, או production. ה-Slice נשאר מקומי וכבוי כברירת מחדל; אל תפעיל את הדגל, אל תפרוס את ה-OS או את האתר, ואל תשנה את אתר production.

הוסף רק תיקון מינימלי אם בדיקה מוכיחה פער. הוכח מכנית: (1) capture ראשון יוצר Contact, Activity וקבלת intake תואמים; (2) replay זהה מחזיר אותם IDs ללא Activity/notification נוספת; (3) payload conflict מחזיר 409 בלי side effect; (4) test/internal ignored; (5) rollback אינו משאיר קבלה חלקית; (6) owner OS מציג את contact/activity דרך הנתיבים הקיימים בלבד ובמעטפת owner-os.v1; (7) משתמש ללא scope/owner תקין נדחה לפני קריאה.

הרץ לפחות, עם פלט pytest לקובץ ולא דרך pipe:
- MIA_ENV=test uv run pytest --basetemp=.cache/form-intake-os-<unique> -p no:cacheprovider tests/unit/test_form_intake.py tests/unit/test_owner_os_reads.py > .cache/form-intake-os-<unique>.txt 2>&1
- uv run ruff check app tests
- אם מסד PostgreSQL מקומי מוגדר במפורש: הוסף tests/unit/test_owner_os_postgres.py; אחרת דווח SKIPPED, לא PASS.

פלט נדרש: רשימת קבצים מדויקת, תוצאות PASS/FAIL/SKIPPED, התאמה מפורשת source_id → payload hash → contact_id/activity_id → CrmFormIntakeReceiptRow → owner OS read, והצהרה שאין deploy, migration חיה, הפעלה, secret change, replay היסטורי, הודעה אוטומטית חדשה או שינוי ב-Google Sheets הפיננסי. אל תבצע commit, push, merge או פעולה חיצונית.
```
