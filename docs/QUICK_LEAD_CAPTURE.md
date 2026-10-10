# AssafWeb OS: לכידת ליד מהירה

מימוש backend מקומי עבור PWA עתידית. בסיס הפיתוח המקורי: `cbbd0a0614291ba706f853cc604021f1973cd56a`.
בסיס ה־PR אחרי rebase: master ב־`27168610ec3e8cbd3aee836f70b00d6807743f6d`.
ענף האינטגרציה: `codex/quick-lead-capture`. לא בוצעו מיזוג, פריסה או שינוי נתוני production.
היעד של 15–30 שניות הוא יעד
לבדיקת שימוש באייפון אחרי בניית הממשק; אין כאן הוכחת ביצועים במכשיר.

## מה כבר קיים ומה נוצל

- `app/services/crm_v2.py`: PostgreSQL הקנוני, זהויות, snapshots וגרסאות, לכידה,
  פעילות, התנגשויות ו־outbox ל־Contacts/Activity. אין CRM או טבלת אנשי קשר חדשים.
- `app/services/owner_actions.py`: הצעה מדויקת ובלתי משתנה, hash, בעלים, תפוגה,
  החלטת אישור ובדיקה מחדש של היעד. נעשה שימוש ב־`propose_owner_action`,
  `read_owner_action`, `decide_owner_action` ו־`execute_owner_action`.
- `app/tools/owner/crm.py`: נבדק מסלול `crm_upsert` הקיים. הוא מתאים לעדכון מאושר
  ב־Telegram, אך אינו API לטופס: הוא מאפשר upsert, דורש כוונה בטקסט ומייבא Sheets.
  הטופס החדש דורש create בלבד, כדי שפרטי ליד ידני לא ידרסו איש קשר קיים.
- `Principal` ובדיקת allowlist של מזהי Telegram מספריים: אותה סמכות בעלים.
- `app/services/phone_identity.py`: ולידציית טלפון חדשה משותפת, כולל דחיית מספר
  סלולרי ישראלי קצר מדי. אינו מוכיח שמספר מוקצה או שייך לאדם מסוים.
- `ApprovalRow`, `IdempotencyRow`, `CrmContactRow`, `CrmActivityRow`, זהויות והתנגשויות:
  שימוש בטבלאות הקיימות, ללא migration.

לא נעשית קריאת provider במסלול החדש. ה־outbox הרגיל יכול להקרין Contacts ו־Activity
ל־Sheets אחרי פריסה; אישור יצירה מתייחס לשמירה ב־PostgreSQL בלבד. לא נשלחות הודעות
WhatsApp או אימייל, לא נוצרים אירועי Calendar ולא נרשמות תזכורות.

## קבצים

- `app/services/quick_leads.py`: חוזה קלט, בדיקת זהויות, קבלות בקשה ואישור אטומי.
- `app/api/quick_leads.py`: אימות בעלים ונתיבי HTTP עם commit לפני תשובת הצלחה.
- `app/main.py`: חיבור router אחד.
- `tests/unit/test_quick_leads_service.py`: בדיקות שירות ומנגנון אישור.
- `tests/unit/test_quick_leads_api.py`: בדיקות גבול HTTP, persistence ומקביליות.
- `docs/QUICK_LEAD_CAPTURE.md`: מסירה זו.
- `.github/workflows/ci.yml`: בדיקות PostgreSQL לתכונה ובדיקות widget התנהגותיות ב־CI.

## חוזה לממשק

שני נתיבי POST:

1. `/v1/owner/quick-leads/proposals`: שמירת הצעה לבדיקה.
2. `/v1/owner/quick-leads/approvals/{approval_id}`: אישור ההצעה המדויקת ושמירת הליד.

בכל בקשה נדרשים `Origin` שמופיע ב־`MIA_CORS_ORIGINS` ו־header בשם
`X-AssafWeb-Owner-Login`, המכיל JSON של הוכחת Telegram Login Widget חתומה.
השרת בודק HMAC, גיל של עד 600 שניות, סטיית שעון עתידית עד 30 שניות, ואת המזהה
המספרי מול `MIA_TELEGRAM_OWNER_USER_IDS`. username אינו מקנה גישה. ב־production
נדרש Origin של HTTPS. bot token נשאר בשרת וב־Secrets Manager; אין להעבירו לממשק.

זהו חיבור אימות מינימלי לשיטה הוותיקה של Telegram. התיעוד הרשמי לשיטת החתימה:
[Telegram legacy Login Widget](https://core.telegram.org/widgets/login-legacy).
Telegram מפרסם גם [מסלול OIDC חדש](https://core.telegram.org/bots/telegram-login);
מימוש session ארוך טווח או החלפת verifier ל־OIDC שייכים לעבודת חיבור הממשק בעתיד.
אין להשתמש ב־JSON מפוענח של OIDC במקום ההוכחה הנתמכת כאן.

ההוכחה היא credential קצר חיים: להעביר ב־HTTPS, לשמור בזיכרון בלבד, ולהשמיט מלוגים,
URL, analytics, cache ו־service worker. תשובות תקינות מסומנות `Cache-Control: no-store`.
הרשמת domain לבוט והוספת origin הן פעולות הגדרה נפרדות, שטרם בוצעו.

### כניסה ל־PWA עצמאית: חוזה קיים מול חיבור חסר

ה־verifier הקיים מאמת Login Widget של **אתר רגיל**, ללא `Telegram.WebApp.initData`.
Telegram מתעד callback JSON ו־redirect לאתר. לכן מנגנון החתימה אינו מוגבל ל־Mini App.
עם זאת, תמיכה בפרוטוקול אתר אינה הוכחת כניסה ב־Safari או באפליקציה שהותקנה למסך
הבית: לא נבדקו חזרה מ־Telegram, popup/cookie restrictions או הישארות מחוברת באייפון.
אין בפרויקט exchange/session endpoints לבעלים. **האימות למוצר PWA עדיין לא הושלם**.

זרימת server-session הנדרשת לפני חיבור מוצר, שטרם מומשה:

1. ה־PWA וה־BFF של הבעלים יוגשו מאותו origin ב־HTTPS. נקודת `login/start` תיצור
   ניסיון כניסה חד־פעמי עם TTL קצר ו־state אקראי, הקשור לעוגיית pre-login מאובטחת.
2. אפשר להשתמש ב־legacy callback JSON ולשלוח אותו ב־POST ל־exchange יחד עם state
   ו־CSRF. חלופה מועדפת לחיבור חדש היא Telegram OIDC Authorization Code + PKCE S256
   ב־top-level redirect, עם state ו־nonce בצד השרת. החלופה דורשת verifier אחר;
   יש לבקש רק `openid profile`, כדי לקבל `id` מספרי. ה־API הנוכחי אינו מקבל OIDC JWT
   או Mini App initData. כתובות callback/origin יירשמו
   מראש בבוט באישור נפרד, ללא בקשת הרשאת הודעות או טלפון.
3. ב־legacy השרת יאמת HMAC, זמן ומזהה מספרי דרך `verify_owner_login`. ב־OIDC
   השרת יחליף code באמצעות secret שרת ויאמת חתימה מול JWKS, issuer, audience,
   exp/iat, nonce ו־`id` מספרי מול allowlist. `sub` או username אינם תחליף למזהה זה.
   בכל מסלול יש לבדוק state פעם אחת, למנוע login CSRF/replay ולא לסמוך על JSON מפוענח.
4. רק אחרי האימות ייווצר session שרת אטום עם token אקראי בעוגיית
   `__Host-assafweb-owner` עם `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, ללא Domain.
   לשמור hash של token, מזהה בעלים, תפוגה וביטול במאגר שרת משותף/עמיד; להחליף token
   בכניסה, להגביל זמן חוסר פעילות ומשך מוחלט, ולאפשר logout וביטול. מאגר session
   יתווסף לתשתית Mia הקיימת; אין צורך במסד CRM או אפליקציה נפרדים.
5. כל בקשת כתיבה תיבדק שוב מול session, allowlist העדכני, kill switch, Origin מדויק
   ו־CSRF token. השרת ייצור Principal רק לאחר אימות זה. האישור הקיים של הליד יישאר
   חובה; session אינו מעניק אישור אוטומטי. יש להוסיף תמיכת session ל־dependency
   של Quick Lead או adapter שרת מאומת, בלי לזייף הוכחת Telegram או להעביר את bot token.
6. הטופס ישמור request_id לצורך retry, אך לא credentials ב־localStorage/URL/cache.
   401 יחזיר לכניסה חדשה תוך שימור טיוטה בזיכרון. קבלות API עם מידע פרטי, כולל
   שגיאות, יקבלו no-store. נדרשות בדיקות session fixation, CSRF, replay, logout,
   שינוי allowlist, פקיעת session וחזרה למסך הבית ב־iPhone אמיתי.

הנוכחי מתאים לבדיקת backend עם הוכחת אתר קצרה; הוא אינו session API מוכן למובייל.

בקשת הצעה לדוגמה:

```json
{
  "request_id": "b4dbfb7a-c31f-466a-a3db-d7dd050f1fe8",
  "name": "דנה",
  "phone": "0501234567",
  "email": "",
  "business": "העסק של דנה",
  "need": "אתר קטן להצגת השירותים",
  "source": "phone_call",
  "next_action": "להתקשר ולברר היקף",
  "follow_up_date": "2026-10-20"
}
```

| שדה | חובה | פורמט / תפקיד |
|---|---|---|
| `request_id` | כן | UUID חדש לכל הגשה לוגית; אותו UUID בכל retry |
| `name` | כן | שם, עד 160 תווים |
| `phone` | אחד משני פרטי הקשר | טקסט מספר, עד 40 תווים; מספר ישראלי או בינלאומי עם `+` |
| `email` | אחד משני פרטי הקשר | כתובת תקינה, עד 320 תווים |
| `business` | כשידוע | עד 160 תווים; חסר נשאר ריק |
| `need` | כן | צורך הלקוח, עד 1,000 תווים |
| `source` | כן | `phone_call`, `in_person`, `whatsapp`, `referral`, `other` |
| `next_action` | לא | עד 500 תווים; נתון בלבד |
| `follow_up_date` | לא | תאריך `YYYY-MM-DD`; נתון CRM בלבד |

שדות אחרים נדחים. אין `status`, סכום, paid, מזהה לקוח או סמכות בעלים בקלט.
הסטטוס נקבע בשרת כ־`lead`; לא נוצרת עסקה או טענה לתשלום. פרטים חסרים לא מושלמים
על ידי מודל. תאריך המעקב נשמר בשדה `date`, הצורך ב־`want`, והפעולה הבאה ב־`next_step`.

## תשובות ו־retry

- `202 pending_approval`: `approval_id`, `proposal_id`, `expires_at`, `preview`.
  מדובר בהצעה שמורה בלבד; אין להציג "הליד נשמר".
- `200 created`: `contact_id` של הליד שנשמר. חוזר רק אחרי commit. אישור חוזר מחזיר
  את אותו מזהה בלי ליצור רשומה או פעילות נוספת. `revision` היא הגרסה הנוכחית של
  רשומת CRM זו; יש להשתמש במזהה השרת היציב בכל חיבור עתידי.
- `409 duplicate`: איש קשר קיים; אין עדכון או מיזוג אוטומטי.
- `409 identity_conflict`: הזהויות מצביעות לאנשי קשר שונים; נשמרה התנגשות לבדיקה.
- duplicate והתנגשות כוללים `preview` של המידע שהבעלים הזין. המידע המדויק נשמר
  גם בקבלת הבקשה; במקרה התנגשות הוא נשמר גם ב־`crm_issues.details_json.manual_lead`.
- `409 idempotency_conflict`: שימוש באותו UUID עם תוכן אחר. עריכה דורשת UUID חדש.
- `409 target_changed`: היעד השתנה בין ההצעה לאישור; ליצור הצעה חדשה לבדיקה.
- `409 expired`, `unbound` או תוצאת אישור אחרת: אין אישור יצירה; לא להניח שנשמר.
- `409 unknown` או `already_handled` ללא קבלת `created`: להעביר לבדיקת בעלים.
  אין להציג הצלחה ואין ליצור UUID חדש או להפעיל שמירה חדשה אוטומטית. כשל באמצע
  לכידה מבטל את שינויי CRM ושומר את טענת הביצוע כ־`pending_review`.
- `401`: הוכחת זהות חסרה, לא תקינה או פגת תוקף; להתחבר מחדש.
- `403`: Origin אסור, בעלים לא מורשה או kill switch.
- `422`: פרטי קלט חסרים או שגויים; לתקן בשדה המתאים.
- `503 persistence_failed`: העסקה בוטלה; אין הצלחה. לאחר חזרת השרת לנסות עם אותו
  `request_id` או אותו `approval_id`, לפי הבקשה שנכשלה.

אין לקבל תוכן חדש בזמן אישור; האישור מתייחס להצעה שכבר נשמרה. אובדן תשובת רשת
מחייב retry של אותו מזהה. קבלה חוזרת של הצעה יכולה להחזיר את ההצעה המקורית גם
אחרי אישורה; לחזור על האישור כדי לקבל קבלת יצירה. אין צורך ב־GET ציבורי לפרטי CRM.

## זיהוי כפילויות והיסטוריה

מספרים ישראליים חדשים מנורמלים ל־`+972`. החיפוש כולל גם מפתחות מקומיים ישנים
מה־CRM הקיים, מספרי קידומת מדינה בלי `+`, וקידומת `00`. מתבצע חיפוש זהויות מדויק
בלבד; alias שאינו תקין לפי ה־CRM אינו מועבר לחיפוש חופשי. מספר מקומי ובינלאומי
שהיו משויכים לשתי רשומות שונות נשארים מקרה
לבדיקת בעלים. התאמה לפי שם או עסק לבדם אינה מספיק מדויקת כדי למזג אנשים.

מקור הליד נשמר ברשומה ובערוץ הפעילות. פעילות `manual_lead_captured` מקושרת
לבקשה, לבעלים ולליד; קבלת הבקשה מקושרת להצעה המדויקת. duplicate לא משנה מקור,
היסטוריה או סטטוס של איש קשר קיים. הנתונים שהוזנו בבקשת duplicate נשמרים בקבלה
לביקורת, בלי להוסיף פעילות לקוח לא מאושרת. הצעה ונתוני התנגשות נשמרים ב־PostgreSQL.
עדכון איש קשר או פעילות מיובאת מחייב מסלול עתידי נפרד: מזהה יציב, גרסה צפויה,
preview מדויק, אישור ובדיקת יעד מחודשת. Quick Lead אינו endpoint לעדכון כזה.

המסלול תופס את נעילת CRM הקיימת לאורך העסקה, ובודק שוב זהויות תחת הנעילה לפני
כתיבה. ב־SQLite לבדיקה מקומית בלבד משתמשים ב־`BEGIN IMMEDIATE` עם session חדש.
ביצוע האישור משתמש בחשבון פעולה מקומי ללא commits פנימיים של provider.

## זרימת הממשק באייפון

1. אחרי כניסה: כפתור בולט `+ ליד חדש`.
2. טופס קצר: שם, טלפון או אימייל, עסק אם ידוע, משפט על הצורך ובחירת מקור.
   מקלדת `tel`/`email` לפי השדה. פעולה ותאריך מעקב בתוך אזור אופציונלי.
3. `בדיקה לפני שמירה` שולח הצעה. מסך האישור מציג את ה־preview שהשרת החזיר.
4. `אישור ושמירת ליד` מאשר את `approval_id`, ללא שינוי פרטים. עריכה חוזרת לטופס
   ויוצרת בקשה חדשה. בזמן בקשה חוסמים לחיצה כפולה אך שומרים את מזהה ה־retry.
5. רק `created` מציג `הליד נשמר`. duplicate מציג `איש הקשר כבר קיים`;
   התנגשות מציגה `צריך לבדוק את פרטי הקשר`. לא שולחים הודעה ללקוח.

לא נבנה כאן frontend, dashboard או PWA. עבודת הממשק העתידית כוללת כניסה במכשיר,
חידוש credential, RTL, התקנה למסך הבית, רשת איטית ו־retry, ובדיקת זמן אמיתית.
אין offline auto-submit ואין cache של נתוני הבעלים. Smart Reminders היא תוספת
נפרדת; שדה תאריך אינו מפעיל אותה ואין שינוי ב־worktree שלה.

לחיבור מוצר אמיתי מעבר לטופס זה עדיין נדרשים מסלולים נפרדים: session מאובטח ארוך
טווח או auth exchange בצד השרת; קריאות בעלים ל־contacts list/detail מתוך PostgreSQL
בלי import של Sheets; ורשימת יכולות, next actions ומשימות. במסירה זו קיימים רק שני
נתיבי Quick Lead המתוארים לעיל, ולא API לעריכה, פעילות, תשלום או ניהול תזכורות.
הזמינות של list/complete/snooze/reschedule לתזכורות דורשת מסירה מהמשימה המקבילה
ובדיקה אחרי שילובה; היא לא אומתה כאן. אין להמציא endpoint חלופי או לעקוף אימות
כדי לחבר את הממשק. הקוד נבדק מקומית באמצעות TestClient; הנתיבים אינם מאומתים כפרוסים.

## בדיקות וביקורת פיתוח ראשוניות

התוצאות בסעיף זה הן מבסיס הפיתוח המקורי שלעיל. בדיקות שחרור מלאות אחרי עדכון
הבסיס ל־master, ה־SHA המדויק ותוצאות CI יתועדו ב־PR; אין להסיק מהן מעבר של בסיס חדש.

בדיקות ממוקדות: **76 עברו, ללא דילוגים**. הן כוללות את גבול HTTP האמיתי מול
SQLite ו־PostgreSQL 17 מקומי, הגשות ואישורים מקבילים, repeat/replay, owner auth,
בעלים מורשה אחר שמנסה לאשר, קלט חסר/פסול, source/payment guards, כל צורות המספר
הישנות, זהות חדשה אחרי ההצעה, התנגשות ושחזור פרטיה, תפוגה/שינוי הצעה,
כשל commit ו־rollback אחרי שינוי חלקי, וכן הגבלת outbox ל־Contacts/Activity.

הסוויטה המלאה: **2,616 עברו, ללא דילוגים**, ב־125.43 שניות; 875 אזהרות deprecation.
היא כוללת גם את בדיקות PostgreSQL הקיימות מול מסד הבדיקה המקומי.
`uv run --no-sync ruff check app tests`: עבר. `node tests/unit/widget_behavior.test.js`: עבר.
הלוג הממוקד: `.cache/quick-lead-focused.txt`; הלוג המלא: `.cache/quick-lead-full-final.txt`.
כלל הבטיחות הקיים זיהה בתחילה בדיקת סמכות מקומית; השירות הותאם ל־`authorize`
הקיים והסוויטה המלאה הורצה שוב. לא הוחלשה או הוסרה בדיקה.

ביקורת HEAVY עצמאית מצאה שני ליקויים שתוקנו: זיהוי מספר legacy בלי `+`, ושמירת
פרטי הליד שהוזנו במקרה התנגשות. המבקר שחזר עצמאית ארבע צורות מספר ובדק כשל
אחרי כתיבה חלקית ואישר שלא נשארו contact, activity או outbox. הביקורת הסופית:
**PASS** בבסיס הפיתוח, ללא חסמים פתוחים בששת קבצי המימוש המקוריים. נבדקו גם אימות מספרי, Origin, אישורים
בלתי משתנים ופגי תוקף, בדיקה חוזרת של היעד ונעילה עד commit. הסוקר בדק את שני
לוגי הבדיקות. כניסת Telegram אמיתית, production וקבלה באייפון/PWA טרם אומתו.

בדיקות מקביליות
PostgreSQL מחייבות `MIA_QUICK_LEAD_TEST_POSTGRES_URL` המצביע ל־localhost ולמסד
בדיקה בשם `quick_lead_test`; כל בדיקה מקבלת schema זמני נפרד.

## בדיקות הכנה ל־PR

אחרי rebase ל־master הרשום בראש המסמך: **84 בדיקות ממוקדות עברו** מול SQLite
ו־PostgreSQL 16 זמני על loopback, כולל ולידציה משותפת ומדיניות טלפון פעילה במסד
בדיקה קנוני. **2,731 בדיקות בסוויטה המלאה עברו, ללא דילוגים**, ב־156.34 שניות;
911 אזהרות deprecation. Ruff לכל app/tests, בדיקות widget ו־diff check עברו.
הלוגים: `.cache/quick-lead-pr-focused.txt`, `.cache/quick-lead-pr-full.txt`.
ה־CI כולל כעת בדיקות widget וסוויטה מלאה מול PostgreSQL 16 disposable, עם שני
test DSNs, נוסף לשער migration/pgvector הקיים. תוצאת CI על ה־commit המדויק תימסר
ב־PR ובדוח הסופי. הקוד הסופי נבדק מחדש בביקורת עצמאית; זו אינה הוכחת שימוש במובייל.

## נקודות אינטגרציה עם הענפים הנפרדים

תמונת Git מקומית שנבדקה בקריאה בלבד ב־2026-10-11:

- Telegram Owner Privacy: ענף `codex/mia-telegram-owner-privacy`, HEAD
  `27168610ec3e8cbd3aee836f70b00d6807743f6d`, עם שינויים לא committed ב־Telegram API,
  transport, worker, בדיקות ו־CI. HEAD אינו מייצג את התיקון. החפיפה עם Quick Lead
  היא `.github/workflows/ci.yml`; אין חפיפה ישירה בקבצי Quick Lead או ב־CRM.
- Smart Reminders: ענף `codex/internal-smart-reminders`, HEAD
  `0b8222c244dcc9ce0e336fc6861b53010983955d`, עם שינויים נוספים לא committed ב־worker,
  parsing, בדיקות ומסירה. גם כאן HEAD אינו גרסה סופית מאומתת. בדיקת commit בלבד
  מצביעה על חפיפה ב־`app/main.py` וב־CI: lifecycle של worker לעומת חיבור router של
  Quick Lead, ורשימת בדיקות PostgreSQL לעומת job נפרד של Quick Lead.
- חפיפת קבצים אינה הוכחת merge conflict, וגם hunks נפרדים אינם הוכחת תאימות.
  Quick Lead מכבד את `MIA_CRM_ISRAELI_PHONE_NORMALIZATION_ENABLED` הקיים. אין להפעיל
  אותו במסגרת PR זה: ב־CRM עם זהויות legacy, מדיניות master דורשת תחילה מעבר
  זהויות מבוקר וחוסמת כתיבה לפניו. זו דרישת הגדרה/נתונים קיימת, ללא migration חדשה כאן.
  בזמן שילוב יש לשמור את כל ה־routers, startup/shutdown של workers וכל שערי CI.
  שדה `follow_up_date` של Quick Lead נשאר נתון CRM ולא יוצר Reminder/Task, כך שאין
  כאן מקור שליחה חדש לתזכורות או ל־daily due-scan. אין migration חדשה ל־Quick Lead.
- אחרי ששני הענפים יפורסמו עם SHA סופי ו־CI ירוק נדרשת בדיקה משותפת: privacy של
  כניסה/שליחה ב־Telegram, כשל ואישור/retry של Quick Lead, וכללי worker/receipt/duplicate
  של Reminders עם due-scan. לא נבדקו או מוזגו שלוש התכונות יחד במשימה זו.

GO לבדיקת backend מבודדת אחרי בדיקות שחרור וביקורת; NO-GO להצגת PWA שימושית עד
השלמת server-session ואימות באייפון. אינטגרציה משותפת תלויה בגרסאות המדויקות של
שני הענפים האחרים. שום worktree אחר לא שונה.

## תכנית פריסה ו־rollback ב־GCP VM

התכנית מבוססת על `ops/gcp/README.md`, `ops/gcp/deploy.ps1`, `ops/gcp/vm/mia`
ו־`ops/gcp/vm/compose.yaml`. Mia רצה ב־VM `mia` בפרויקט `mia-assafweb`, אזור
`me-west1-a`: PostgreSQL 16 על loopback, Compose app/migrate ו־Caddy ל־HTTPS;
הגדרות ב־Google Secret Manager `mia-env`. המקורות נבדקו; לא נבדק כאן מצב VM חי.
הוראות ECS ב־README/HANDOFF ההיסטוריים אינן נוהל הפריסה של תכונה זו.

1. אישור מפורש למיזוג ולפריסה, בנפרד. משימה זו מסתיימת אחרי PR ו־CI.
2. לפני מיזוג: ביקורת עצמאית, CI ירוק ובדיקת ההתאמה עם ענף Smart Reminders.
   אין להעתיק את קבציו או לכלול אותם ב־commit של תכונה זו.
3. לפני פריסה מורשית: לשמור SHA/tag נוכחיים ו־backup בעזרת `sudo mia backup`,
   ולפרוס מ־checkout נקי ב־SHA המדויק שעבר CI באמצעות
   `ops/gcp/deploy.ps1 -ProjectId mia-assafweb -Ref <approved-sha>`.
   הסקריפט מעתיק git archive, בונה image ב־VM ומפעיל `mia deploy <tag> <sha>`;
   `migrate` חייב להסתיים לפני `app`. אין migration חדשה לתכונה זו.
4. להשאיר את secrets ומזהי הבעלים בשרת; להגדיר domain ו־HTTPS origin של הממשק
   באישור נפרד. לא להרחיב גישה לפי username ולא לתת ל־website principal הרשאת בעלים.
5. אחרי פריסה מורשית: `/health` צריך לדווח את SHA המדויק ו־`/health/ready` להחזיר
   200; בדיקת `/health/live` לבדה אינה מספיקה. `sudo mia status` ו־logs של app/migrate
   מאמתים את השירותים ואת מצב systemd timers הקיים. Quick Lead לא מוסיף worker,
   timer או flag ואין להפעיל תזכורות/ingest לצורך בדיקתו. smoke מאושר: בעלים תקין,
   משתמש זר, ליד בדיקה אחד, שתי לחיצות על שמירה,
   duplicate, היסטוריית פעילות והיעדר הודעת לקוח/Calendar. זמן באייפון נמדד בנפרד.
   לא בוצע smoke זה כאן.
6. אין פקודת `mia rollback` בקוד. rollback מורשה הוא פריסה חוזרת של SHA קודם
   שאושר, באותו `ops/gcp/deploy.ps1 -ProjectId mia-assafweb -Ref <previous-sha>`.
   ה־VM שומר רק שלושה builds, לכן אין להניח ש־image ישן נשמר; archive מאפשר בנייה
   מחדש של אותו SHA. אין להריץ `go-live`, לרשום webhook, לבצע restore DB או להפעיל
   flags לצורך rollback זה. חשוב: `mia deploy` קורא ל־`install_timers`, שמפעיל
   `systemctl enable --now` לשני ה־timers הקיימים. jobs-paused marker נשמר ב־VM
   קיים, אך אין להסיק שמצב timers שהיה disabled יישמר. לפני פריסה/rollback מורשים
   יש לתעד מצב jobs וכל timer, להגן מפני הרצת jobs בזמן החלפת image ולשמר/להחזיר
   את המצב המקורי בתכנית התפעול המאושרת. אין להפעיל worker חדש. לבדוק health/SHA.
   אין rollback סכימה ואין מחיקת לידים.
   רשומות ואישורים שכבר נשמרו נשארים ב־PostgreSQL. הצעות מהסוג החדש לא יבוצעו
   על ידי הגרסה הישנה; יש להשביתן בממשק ולבדוק ידנית לפני שדרוג חוזר.
