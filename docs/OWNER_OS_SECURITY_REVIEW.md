# Owner OS — ביקורת אבטחה וקבלה עצמאית

תאריך: 11.10.2026. החלטה סופית: **PASS לאינטגרציה המקומית שנבדקה**.
**NO-GO להפעלת בטא אמיתית או ייצור** עד השלמת שערי השחרור המפורטים להלן.

## היקף ועצמאות

הסקירה נעשתה בידי סוקר נפרד ממחברי המימוש, שהחל ללא היסטוריית הבנייה.
הסוקר לא שינה קוד מוצר או בדיקות; הקובץ הזה הוא תוצר הכתיבה היחיד שלו.
נבדקו קבצים חדשים לצד diff, חוזה Owner OS, שירותי Quick Lead/Reminders
הקנוניים, הממשק הקפוא ומסמכי ההפעלה. הסקירה הראשונית נכשלה וממצאיה
הועברו לתיקון; ההחלטה כאן מתייחסת למצב לאחר בדיקת התיקונים.

עוגן Git: `9fc30581705e8d18f21f53fe28c6203909cdd050`, בענף
`codex/owner-os-integration`, עם תוספת מקומית שטרם קיבלה commit בעת הסקירה.
אין כאן אישור ל־SHA ממוזג או פרוס. הממשק נבדק מול
`5b9de3cdc6c4912290aa57f90af3875abd79eb33`; diff של
`prototypes/assafweb-os-mobile/` מולו ריק.

## הכרעת אבטחה והתנהגות

לא נותרו ממצאי P1/P2 חוסמים בהיקף המקומי שנבדק.

- אימות הבעלים נשען על המאמת הקיים להוכחת Telegram חתומה ועל allowlist
  מספרי. username אינו מקנה הרשאה. תחום 600 השניות והמאמת לא שונו.
- session אטום נשמר ב־PostgreSQL באמצעות hash, עם תפוגה מוחלטת, scopes
  ו־workspace מהשרת. cookie מוגן, challenge/CSRF, Origin, rotation,
  צריכה חד־פעמית וביטול נבדקים בשרת. allowlist/scopes/workspace נבדקים מחדש.
- הקריאות משתמשות בחיבור נפרד, SELECT בלבד, REPEATABLE READ READ ONLY,
  ללא autoflush ועם rollback. בדיקת הרשאות אפקטיביות קודמת לקריאת מידע.
  אין שימוש ב־CRM search המייבא Sheets או ב־provider לצורך הקריאות.
- גם תפקיד הפעולות המוכן נבדק כעת מול מפה מצומצמת: עשר טבלאות ושתי
  sequences עם USAGE בלבד. ownership, memberships, הרשאות עודפות,
  CREATE/TEMP ופונקציות יישום נגישות דרך PUBLIC נדחים לפני business logic.
  אין לתפקיד גישה לטבלאות sessions או DELETE על נתוני העסק.
- autoflush=True מוגבל לטרנזקציית פעולה מאומתת, כדי לשמר את סדר העבודה
  של helpers קנוניים לטיפול בהתנגשות זהויות. הקריאות נשארות מבודדות ממנו.
- Quick Lead משתמש בהצעה, אישור מדויק, בדיקת יעד וקבלה לאחר commit של
  השירות הקנוני. הוכחה מקורית טרייה נדרשת גם לצד session ארוך יותר.
  תזכורות שומרות על owner binding, expected_revision ו־dispatch guard.
- cursors קשורים לבעלים, workspace ושאילתה. שגיאות, מידע חלקי ומועד לא ידוע
  אינם הופכים לאפסים, לתאריך משוער או לסיווג עסקי מומצא.
- BFF מגיש allowlist של assets קפואים, מעביר cookie אל Mia ואינו מחזיק
  CRM או session map. forwarding של פעולות נשאר חסום. שני דגלי Mia
  כבויים כברירת מחדל; מעבר הבדיקות אינו אישור להפעיל אותם.

## ממצאים שתוקנו ונבדקו מחדש

| ממצא | תיקון ומסקנה |
|---|---|
| מזהה אישור קנוני נדחה כלא תקין | שימוש ב־APPROVAL_ID_RE של השירות הקיים; אישור אמיתי עבר בבדיקת PostgreSQL |
| timeout נמחק בעקבות rollback | הגדרות statement/lock timeout ניתנות מחדש לפני הפעולה |
| יציאה מוצלחת הוצגה ככישלון | BFF ממפה 204 לגוף JSON שהלקוח הקפוא מצפה לו ומטפל בגוף upstream חסר |
| Telegram data-onauth השתמש ב־eval שחסום ב־CSP | מעבר ל־Telegram.Login.auth עם callback פונקציה; ללא unsafe-eval |
| revoke-all יכול להוריד את חותמת הביטול במירוץ | watermark מונוטוני תחת נעילת owner ובדיקת regression |
| cursor לא היה קשור ל־workspace | שילוב workspace בהקשר ובמפתח HMAC נגזר |
| תזכורת חוקית ארוכה או emoji שברו את חוזה התצוגה | excerpt עד 500 יחידות UTF-16 ללא חיתוך code point, סימון מפורש ושמירת המקור |
| תפקיד הפעולות נבדק רק לפי flags כלליים | validator להרשאות אפקטיביות ובדיקת תשעה ניסיונות הרחבת הרשאה ב־PostgreSQL |
| בדיקת proof עתידי הפכה לתקפה במהלך סוויטה ארוכה | יצירת שני תרחישי הזמן השליליים בזמן ביצוע הבדיקה; המאמת ודרישת 401 נשמרו |
| הגדרות חדשות לא תועדו בדוגמת התצורה | כל עשר ההגדרות נוספו ל־.env.example עם דגלים כבויים וערכים רגישים ריקים |

חתימה או הקשר זרים של cursor מחזירים 400 `invalid_query`.
409 `cursor_invalidated` שמור ל־cursor אותנטי שפג או לגבול נתונים שהשתנה;
ההבחנה שנבדקה נכונה.

## ראיות סופיות

הסוקר קרא את ה־XML ואת סיכום הריצה הסופית, ולא הסתמך על ריצת ביניים.

| בדיקה | תוצאה ומקור |
|---|---|
| סוויטת Python מלאה עם כל מסדי הבדיקה המקומיים | **2,993 passed; 0 failures; 0 errors; 0 skipped**, כ־206 שניות. `.cache/full-suite-verified.txt` ו־`.cache/full-suite-verified.xml` |
| Owner OS בתוך אותה ריצה | **40 passed**: 14 PostgreSQL, 12 reader, 14 sessions; כולל ACL, conflict, replay, ביטול ו־BFF |
| ריצה עצמאית של יחידות session/read לאחר התיקונים | **26 passed**, `.cache/review-last-delta.txt` |
| Ruff | **PASS**, הורץ עצמאית על `app tests` |
| widget behavioral checks | **PASS**, הורץ עצמאית |
| בדיקות הממשק הקפוא | **40 passed; 0 failed; 0 skipped**, הורצו עצמאית; זו בדיקת regression של הממשק, בנפרד מהחיבור החדש |
| החיבור החדש | OwnerClient ו־DTO validators הקפואים פעלו דרך Node BFF → FastAPI → PostgreSQL; שני מופעי BFF, logout, emoji, Host/Origin ו־fixture-serving נבדקו |

ראיות Python נוצרו עם נתונים והוכחות סינתטיים בלבד. מסד Owner OS הזמני
ב־loopback:55439 נפרד ממסד הרגרסיה בעל pgvector ב־loopback:55440.
סוויטת הרגרסיה המלאה הסופית החליפה את תוצאת הריצה הקודמת הלא ירוקה.
הקבצים תחת `.cache` הם ראיות מקומיות, ואינם התחייבות לארכוב ב־Git.

## התאמת מסמכים ומגבלות

נבדקו `OWNER_OS_LOCAL_INTEGRATION.md`, `OWNER_OS_AUTH_THREAT_MODEL.md`
ו־`integrations/assafweb-os/README.md`. תוקן שם שגיאה מתועד כך שיתאים
לקוד: תוכן מקור מחוץ לחוזה מחזיר 503 `source_unavailable`.
תיאור תפקידי ה־DB, ההפרדה בין session להוכחת Telegram, הדגלים הכבויים
והפעולות החסומות תואם למימוש. ראיית health חיה, אם מדווחת במסמך ההפעלה,
היא תצפית נפרדת של מבצע העבודה; אינה ראיית פריסה של התוספת הזאת.

ה־CRM הוא workspace משותף לבעלים המורשים, ולא בידוד tenant לכל מזהה owner.
snapshot טרי אינו watermark של קליטת Website/Leo/Sheets. metadata של excerpt
קיים בתשובת backend, אך הממשק הקפוא מציג כרגע רק את הקיצור עם שלוש נקודות.
שדות אחרים החורגים מהחוזה הקפוא נכשלים באופן סגור; לא הומצאה תמיכה בהם.

## שערי בטא וייצור שנותרו

1. אישור merge/פריסה נפרד, SHA נקי שעבר CI, backup ומיגרציות מאושרות.
2. רישום דומיין Telegram, TLS/proxy ותפקידי DB אמיתיים עם בדיקת effective ACL.
3. כניסה, חזרה מ־popup, Secure-cookie, logout, expiry ו־revocation על iPhone
   Safari וגם Home Screen. בדיקת Node המקומית מדמה transport ואינה מוכיחה זאת.
4. בדיקת cache/CDN/APM/logs ונתונים אמיתיים מול חוזי הכיסוי והעדכניות;
   נוהל retention מאושר לרשומות auth ישנות בלי לאפשר replay.
5. אישור נפרד להפעלת פעולות, חיבור reauthentication והוכחה מקורית טרייה,
   preview/approval/recovery בממשק. ה־BFF הנוכחי משאיר אותן חסומות.

לא נעשו במסגרת ביקורת זו שינויי ייצור, מיגרציות חיות, שליחת הודעות,
הפעלת feature, שינוי credentials, merge או deployment.
