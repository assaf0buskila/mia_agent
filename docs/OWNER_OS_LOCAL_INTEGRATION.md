# AssafWeb OS — אינטגרציה מקומית, 11.10.2026

## מצב ועוגני מקור

העבודה נמצאת בענף המבודד `codex/owner-os-integration`, מבסיס
`9fc30581705e8d18f21f53fe28c6203909cdd050`. אין commit, merge או פריסה של התוספת.
ה־UI הקפוא הוא PR #59 של `assaf0buskila/assaf-landingPage`, HEAD
`5b9de3cdc6c4912290aa57f90af3875abd79eb33`, בתוך
`prototypes/assafweb-os-mobile/`. לא שונו קובצי הממשק.

נקודת הבריאות הציבורית של הייצור נבדקה בנפרד ב־11.10.2026 בשעה
09:20:36 UTC: מצב ok, SHA `a213c9a7fbdf5586e13cb38ffdbc4646f59c4541`,
schema `20261008_assafweb_form_intake_receipts.sql`. קוד master ותוספת מקומית
אינם הוכחת פריסה. לא בוצעו שינוי ייצור, מיגרציה חיה, קריאת מידע עסקי חי,
הפעלת תכונה, שינוי GCP/Vercel/DNS, merge או פנייה ללקוח.

נקראו AGENTS.md, HANDOFF.md, MIA_V2.md, README.md, TASKS.md,
חוזי Quick Lead והאישורים, שירות התזכורות והמיגרציה שלו, Telegram Owner Privacy,
FREEZE.md/INTEGRATION.md של המובייל וחוזה OWNER_OS_INTEGRATION_CONTRACT.md
מענף ה־KPI הנפרד. קוד השירותים הקנוניים קבע את ההתנהגות כאשר מסמך היה ישן.

## 1. ארכיטקטורת אימות

נבחר Telegram Login הרשמי עם callback וחתימת HMAC, דרך המאמת הקיים
`verify_owner_login`, ומזהה Telegram מספרי ברשימת המורשים. אין הנחה ש־Mini App
initData קיים ב־PWA, ואין שרת OAuth חדש. המאמת, טווח 600 השניות והסטייה הקיימת
לא שונו. פרטי הבוט הפומביים משמשים ל־popup; ה־token נשאר בשרת Mia.

challenge מקדים, CSRF ו־Origin מדויק נדרשים בכניסה. challenge והוכחה נצרכים
פעם אחת בתוך טרנזקציה. לאחר האימות נוצר session נפרד בן עד 30 דקות, ללא refresh,
המאוחסן ב־PostgreSQL. cookie בשם `__Host-mia-owner` הוא Secure, HttpOnly,
SameSite=Strict, Path=/, ללא Domain. במאגר נשמרים רק hashes של credentials
והוכחה, מזהה הבעלים, workspace, scopes וזמנים. logout מבטל את ההרשאה בשרת;
revoke-all משתמש בנעילה ובחותמת ביטול שאינה יכולה לרדת במירוץ בין בקשות.

ה־allowlist, ה־workspace וה־scopes העדכניים נבדקים בכל בקשה. scopes ברירת המחדל
הם `owner-os:read:contacts`, `owner-os:read:activities`,
`owner-os:read:reminders`. הדפדפן אינו יכול להעניק scopes לעצמו.
כל בקשת Mia פרטית דורשת Origin תקין; כל שינוי דורש גם CSRF.
מגבלות כניסה נשמרות ב־DB: עד 30 challenges ו־20 ניסיונות login בדקה, כלליות
לאפליקציה. תקלה באימות או בהרשאות סוגרת את הגישה.

מודל האיומים המלא נמצא ב־[OWNER_OS_AUTH_THREAT_MODEL.md](OWNER_OS_AUTH_THREAT_MODEL.md).
רישום הדומיין ב־BotFather והתחברות אמיתית ב־Safari נשארים שערי בטא.

## 2. endpoints מדויקים ב־Mia

| Method | Endpoint | תנאים ותוצאה |
|---|---|---|
| GET | /v1/owner/auth/challenge | Origin, challenge cookie ו־CSRF קצרי חיים |
| POST | /v1/owner/auth/login | challenge, CSRF, original signed login_proof; session חדש |
| GET | /v1/owner/auth/session | session חי; authenticated, CSRF, expires_at |
| POST | /v1/owner/auth/logout | session ו־CSRF; ביטול ה־session, 204 |
| POST | /v1/owner/auth/revoke-all | session ו־CSRF; ביטול כל ה־sessions של אותו owner, 204 |
| GET | /v1/owner/os/today | contacts scope; next_actions ותזכורות רק אם scope קיים |
| GET | /v1/owner/os/contacts | contacts scope; limit 1–50 ו־cursor |
| GET | /v1/owner/os/contacts/{contact_id} | contacts scope; ID קנוני ו־revision |
| GET | /v1/owner/os/contacts/{contact_id}/activities | activities scope; limit ו־cursor |
| GET | /v1/owner/os/reminders | reminders scope; limit, cursor, status, from/to עם אזור זמן |
| POST | /v1/owner/os/quick-leads/proposals | adapter כבוי; action scope, CSRF והוכחה מקורית טרייה |
| POST | /v1/owner/os/quick-leads/approvals/{approval_id} | adapter כבוי; אישור קנוני מדויק והוכחה מקורית טרייה |
| GET | /v1/owner/os/quick-leads/receipts/{request_id} | adapter כבוי; קבלה קיימת, שייכת ל־owner, ללא כתיבה |
| POST | /v1/owner/os/reminders | adapter כבוי; reminders.write, CSRF, request_id ו־AwareDatetime |
| POST | /v1/owner/os/reminders/{reminder_id}/complete | adapter כבוי; expected_revision |
| POST | /v1/owner/os/reminders/{reminder_id}/snooze | adapter כבוי; expected_revision ו־scheduled_at |
| POST | /v1/owner/os/reminders/{reminder_id}/reschedule | adapter כבוי; expected_revision ו־scheduled_at |

כל נתיבי הפעולות נשארים מאחורי `MIA_OWNER_OS_ACTIONS_ENABLED=false` בנוסף
ל־`MIA_OWNER_OS_ENABLED=false`. גם אם מתאם הפעולות מופעל בסביבה מקומית,
ה־BFF אינו מעביר פעולות מהמובייל. אין מסלול cancel, עריכת לקוח או שליחת הודעה חדש.

## 3. רכיבים קיימים ששימשו

המאמת הרשמי של Quick Lead, Principal.owner ו־numeric allowlist;
CrmContactRow/CrmActivityRow וה־IDs/revisions הקיימים; QuickLeadService,
הצעה ואישור קנוניים immutable/expiring, target revalidation, idempotency_records,
approvals והקבלה העמידה; ReminderService ו־owner_reminders עם guard למסירה ו־CAS.
לא שונו שירותי ה־CRM, Quick Lead, התזכורות או ה־delivery הקנוניים.

התוספות: routers פרטיים, שירות sessions, reader, בודק הרשאות פעולות, מיגרציית
sessions ו־Node BFF באותה סביבת עבודה. המיגרציה PostgreSQL-only ואינה משנה
טבלאות לקוחות. ה־BFF מגיש רק שישה assets קפואים ואינו מחזיק session map או CRM.

## 4. חוזה המידע העסקי

owner-os.v1 עם source=mia_postgresql, snapshot_id, observed_at,
coverage/coverage_scope/freshness, capabilities ו־pagination.
snapshot טרי מעיד על קריאה שבוצעה כעת מה־PG, לא על סנכרון עם מקורות חיצוניים.
אין מונים, הכנסות, תשלומים, פרויקטים או qualification/pipeline_stage מומצאים.
source_statuses ריק משום שאין watermark מאומת של Website/Leo/Sheets.

Today מציג next_step שנשמר בפועל ויכול לכלול פעולות שמועדן לא ידוע; הוא אינו
מייחס להן דדליין להיום. date היסטורי מוצג כ־null/unverified בהיעדר הוכחת משמעות.
ערכי status/source שנרשמו מוצגים כערכים רשומים, לא כשלב מכירה או מקור מאומת.
רשימת CRM משותפת ל־workspace של AssafWeb ולכל הבעלים המספריים המורשים;
תזכורות וקבלות מוגבלות ל־owner המסוים. זה אינו מוצר מרובה tenants.

קריאה עסקית אינה מייבאת Sheets, מפעילה reconciliation, יוצרת outbox, מסמנת seen
או קוראת provider. כל snapshot משתמש ב־REPEATABLE READ READ ONLY,
autoflush=False וב־rollback בסיום, דרך DSN נפרד בעל SELECT בלבד.
cursor חתום קצר, קשור ל־owner/workspace/query/order/limit, פג אחרי חמש דקות.
חתימה/context זרים נדחים ב־400 invalid_query; cursor אותנטי שפג או boundary
שהשתנה ב־409 cursor_invalidated. מעבר בין עמודים אינו snapshot עקבי יחיד לכל המסע;
השינוי מאותר בגבול, וה־UI ממשיך למזג לפי IDs/revisions.

עמוד ראשון מלא יכול להיות available עם רשימה ריקה רק כאשר ה־PG נקרא בהצלחה
והכיסוי של הסקופ המוגדר שלם. עמודי המשך מסומנים partial. תקלה אינה אפס:
אין success ריק עבור מקור בלתי זמין; מוחזרת שגיאה מצומצמת, לרוב 503.
פעילויות מוצגות דרך allowlist, ללא transcript או provider payload.

תזכורות קנוניות עד 2,800 code points מקוצרות לתצוגת עד 500 יחידות UTF-16,
עם … ו־metadata שמציין excerpt; הטקסט ב־DB לא משתנה. ה־UI הקפוא מציג את
הקיצור אך אינו מציג עדיין את metadata המורחב. שדות לקוח ופעולה מעבר למגבלת
התצוגה הקפואה או תוכן Unicode פגום נכשלים ב־503 source_unavailable, ללא החלפת ערך.
תמיכה בתוכן ארוך יותר מחייבת שינוי חוזה נפרד.

## 5. תאימות Quick Lead ותזכורות

להצעה ואישור נדרש header `X-AssafWeb-Owner-Login` ובו אותה הוכחה חתומה בדיוק
שיצרה את ה־session, ועדיין בטווח החיים המקורי שלה. session בן 30 דקות אינו מאריך
proof בן 10 דקות. proof חדש ב־session ישן נדחה; כניסה מחדש נדרשת. אין proof מזויף,
refresh או המרת session ל־Telegram proof.

request_id נשמר לאורך retries; duplicate ו־identity_conflict נשארים 409 לפי
ה־API הקיים. קבלת created ניתנת רק אחרי commit מאומת עם approval/activity/contact.
לאחר אובדן תשובה, GET receipt משחזר את אותה תוצאה מהקבלה הקנונית, ללא יצירה
נוספת או אישור מחדש. בקשה ללא ראיה להשלמה נשארת unknown/not-found.
ה־revision בקבלה הוא ה־revision הנוכחי של הלקוח, לא טענה שהוא revision בזמן יצירה.

תזכורות משתמשות במאגר וב־delivery_state הקיימים; request_id קושר retry לאותה יצירה,
payload שונה נדחה, שינוי דורש expected_revision. dispatching מונע שינוי מתחרה.
זמן ללא אזור זמן נדחה. אין Google Calendar, פנייה ללקוח או provider חדש.
טרנזקציית הפעולות החדשה מאפשרת autoflush עבור helpers קנוניים של conflict;
קריאות נשארות בחיבור הנפרד, ללא autoflush.

## 6. בדיקות וסקירה

תוצאות סופיות: **2,993 בדיקות pytest עברו, 0 נכשלו, 0 שגיאות ו־0 דילוגים**,
206.10 שניות, כולל PostgreSQL אמיתי. מתוכן 40 בדיקות Owner OS: 14 PG/E2E,
12 reader ו־14 sessions. 40 בדיקות המובייל הקפוא, Ruff, widget behavioral checks,
בדיקות תחביר Node ו־git diff --check עברו. ההשוואה ל־HEAD של המובייל הקפוא
החזירה diff=0. 1,099 אזהרות הן deprecation של תלויות/SQLite/asyncio;
הן אינן כישלונות. לא נמדד אחוז כיסוי שורות; הכיסוי המדווח הוא כיסוי תרחישים.

הראיות המקומיות: .cache/full-suite-verified.txt ו־.cache/full-suite-verified.xml.
הסקירה העצמאית והרצת 26 בדיקות unit בידי הסוקר מפורטות ב־
[OWNER_OS_SECURITY_REVIEW.md](OWNER_OS_SECURITY_REVIEW.md).
הרצה מוקדמת נכשלה בשל הרחבת vector חסרה בסביבת הרגרסיות, הגדרות חסרות
ב־.env.example והוכחה עתידית שנוצרה בזמן collection והספיקה להפוך לתקפה.
הסביבה והתיעוד תוקנו; רק יצירת שתי ההוכחות תלויות הזמן הועברה לזמן ריצת הבדיקה.
ה־verifier, טווח החיים ודרישת 401 לא שונו. הסוויטה המלאה הורצה מחדש ועברה.
הבדיקות מקומיות בלבד, עם bot token והוכחות סינתטיים ו־PostgreSQL 16 זמני ב־loopback.
הן אינן הוכחת login אמיתי ב־Telegram, cookie אמיתי ב־Safari, delivery חי,
הרשאות DB בייצור או קבלת משתמש על iPhone.

התרחישים מכסים login/logout/expiry/revoke/replay ומירוץ צריכת proof; Origin/CSRF;
owner/scopes; pagination ופעילות; unknown/partial/unavailable; Quick Lead אישור,
קבלה אחרי commit, duplicate/conflict, retry אחרי תקלה בתשובה; reminder revisions
ו־dispatch guard; מניעת payload/PII בשגיאות ו־cache; קריאות provider חסומות.
בדיקת ה־BFF מפעילה את OwnerClient ואת validators הקפואים בפועל, שתי מופעי BFF
מול sessions שנשמרו ב־PG, logout, emoji, מניעת cross-site/Host ו־fixture serving.

בדיקות ACL מריצות SQL אסור בפועל ומנסות הענקת הרשאות עמודה/טבלה, memberships,
INHERIT, CREATE/TEMP, sequence ופונקציות דרך PUBLIC. read/auth/action roles נפרדים;
כל בדיקה מתרחשת לפני business logic. הסקירה העצמאית HEAVY רשאית להיכשל:
ממצאים באישור ID, timeout, logout/CSP, revocation, Unicode והרשאות פעולה תוקנו.

## 7. חיבור המובייל

להחליף את ה־BFF המוצע של PR59 במודול
`integrations/assafweb-os/bff.mjs`, תוך שמירת assets ה־UI ו־OwnerClient.
createServer מקבל origin HTTPS מדויק, backend HTTPS, frontendRoot ומזהה/שם
בוט פומביים. הוא צריך לרוץ מאחורי TLS proxy ב־origin אחד, על פורט פרטי.
HTTP loopback מותר רק עם localTest=true בבדיקות. אין client secret או token ב־Node.

auth/start מפנה ל־auth/telegram באותו origin. callback הרשמי שולח proof ב־POST,
לא ב־URL. api/session מקבל CSRF ו־expiry מ־Mia; api/logout ממפה 204 ל־JSON
שהלקוח הקפוא מצפה לו. contacts/detail/reminders/today ממופים לנתיבי Mia.
Today ב־BFF מבקש sections=next_actions. UI לעריכת לקוח ולפעולות נשאר חסום.

תוספות frontend עתידיות נדרשות לתצוגת activities (ה־backend קיים),
לתצוגת metadata של reminder excerpt, revoke-all ולדיאלוג reauthentication
מדויק לפעולות. הפעלת Quick Lead דורשת העברת proof טרי ללא אחסונו המתמשך;
הממשק הנוכחי אינו שומר אותו אחרי הניווט. אין לשנות זאת אוטומטית או להדליק writes.

## 8. חסמים לבטא אמיתית על iPhone

נדרשים אישור פריסה נפרד; רישום דומיין מתאים ב־BotFather;
TLS/proxy והפניית Host/Origin/Set-Cookie שנבדקו; migrations ו־roles בסביבת staging
ואחריה רק בהרשאה בייצור; סקירת effective ACL על המאגר האמיתי;
מבחן Safari/Home Screen login, logout, expiry, revoke, חזרה מ־popup וכשלים;
בדיקת cache/log/APM/CDN על origin הפרטי; תצפית על unknown dates/coverage ותוכן
אמיתי מול החוזה הקפוא. ניהול מחיקת רשומות sessions/challenges/proofs/attempts
ישנות דורש retention job ייעודי, ללא שינוי זמן התוקף או עידוד replay.

פעולות נשארות שלב נפרד: חיבור reauthentication, preview/approval/recovery
והחלטה מפורשת על הפעלה. בדיקות mocked/synthetic אינן מחליפות את שערי המכשיר.

## 9. דרישות GCP ו־rollback — תוכנית בלבד

לאחר אישור merge ופריסה: checkout נקי ב־SHA המדויק שעבר CI;
backup מאומת, jobs במצב מבוקר, procedure של ops/gcp/README.md.
יש להחיל את כל המיגרציות החסרות לפני start, כולל אלו של Quick Lead/Reminders
אם אינן בפרודקשן, ואז מיגרציית sessions. migration user נפרד מה־runtime roles.

הגדרות חדשות הן MIA_OWNER_OS_ENABLED, MIA_OWNER_OS_ACTIONS_ENABLED,
MIA_OWNER_OS_ORIGIN, MIA_OWNER_OS_WORKSPACE_ID,
MIA_OWNER_OS_SESSION_DATABASE_URL, MIA_OWNER_OS_READ_DATABASE_URL,
MIA_OWNER_OS_ACTION_DATABASE_URL, MIA_OWNER_OS_CURSOR_SECRET,
MIA_OWNER_OS_SESSION_TTL_SECONDS ו־MIA_OWNER_OS_SESSION_SCOPES.
ערכים רגישים נשארים ב־Secret Manager/VM לפי הנוהל; אין העתקת .env.
יש לעדכן באמצעות PatchSecret כדי לשמר את יתר ההגדרות.
Origin ו־allowlist הם exact configuration; CORS לבדו אינו authorization.

auth role: זכויות המיגרציה המפורטות רק לחמש טבלאות sessions ול־sequence שלהן.
read role: SELECT רק crm_contacts/crm_activities/owner_reminders/approvals/idempotency_records.
action role: המפה המדויקת ב־ACTION_TABLE_PRIVILEGES ו־ACTION_SEQUENCE_PRIVILEGES,
ללא DELETE או גישה ל־sessions. כל role הוא LOGIN NOINHERIT ללא memberships,
ownership, CREATE/TEMP, הרשאות עמודה עודפות או callable application routines.
אין להעביר ל־frontend אף אחד מה־DSNs. PUBLIC/default ACL קיימים עשויים לחייב
hardening מאושר לפני שהבודקים המחמירים יאפשרו כניסה.

פריסה ראשונה כשהדגלים off; אימות health/schema/SHA, ניטור reason codes בלבד,
בדיקת owner app ב־staging ומכשיר אמיתי, ואז הפעלת reads באישור. actions נשאר off.
אין שינוי DNS/Vercel מובנה בתוספת; כל topology חדש דורש אישור נפרד.

rollback: כיבוי OWNER_OS_ENABLED ו־OWNER_OS_ACTIONS_ENABLED, ביטול sessions
באמצעות operator מאושר, החזרת SHA נקי קודם והגדרות שהשתנו בלבד.
להשאיר מיגרציית sessions additive וטבלאות CRM ללא down migration/מחיקה.
אם קיימות פעולות שאושרו ונוצרו, rollback אינו מוחק קבלות/outbox/לקוחות;
יש לפעול לפי נוהל קנוני מאושר. בדיקת health/readiness/SHA/jobs ומניעת cookie תקף
אחרי כיבוי נדרשת לפני הכרזת rollback מוצלח.

## 10. החלטה

Local E2E: **GO**, עבור הסביבה המבודדת והסינתטית שנבדקה, עם פעולות כבויות כברירת מחדל.
Real iPhone beta / production activation: NO-GO עד השלמת שערי הפריסה והמכשיר.
