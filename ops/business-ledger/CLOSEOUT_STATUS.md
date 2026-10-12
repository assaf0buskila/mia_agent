# AssafWeb OS Business — מצב סגירה ושחרור

12.10.2026, ישראל. כל הראיות הכספיות כאן הן TEST סינתטי. השם העסקי אינו משנה את סביבת ההפעלה.

## התוכנית הפעילה לאחר שינוי הכיוון

הבעלים בחר חוברת Google Sheets עסקית אחת, Apps Script, Cloudflare וכניסת Google, כולל עריכה ישירה שלו ופעולות MIA לאחר אישור. [תוכנית האיחוד והביצוע](../../docs/ASSAF_OS_PLAN.md) היא התוכנית הפעילה. ההפרדה ההיסטורית במסמך זה בין PostgreSQL CRM ל־Sheets פיננסי היא נקודת המוצא למעבר, ואינה חוזה המוצר החדש.

ב־12.10.2026 נמחקו לאחר בדיקה וסקירה עצמאית רק VM ה־OS הישן `owner-os-staging`, הדיסק שלו ו־`owner-os-staging-ip` בפרויקט `tomibot-telegram`. אומת ששלוש המטרות אינן קיימות ושרת MIA המקורי בפרויקט `mia-assafweb` עדיין פעיל. הכתובת הישנה אינה זמינה. [ראיות הניקוי](../assaf-os/CLEANUP_REPORT.md). ההתאמות החדשות עדיין מקומיות והחוברת החדשה אינה מסונכרנת עם MIA בייצור.

## ראיות היסטוריות של גרסת Business

| מוצר / עבודה | מצב מאומת | הצעד שנותר |
|---|---|---|
| MIA בייצור | `/health`: `a213c9a7fbdf5586e13cb38ffdbc4646f59c4541`; סכימה `20261008_assafweb_form_intake_receipts.sql` | שחרור Owner OS נפרד, migrations והרשאות באישור; אין פריסה במשימה זו |
| MIA master | GitHub: `9fc30581705e8d18f21f53fe28c6203909cdd050`; Quick Lead #98 ותזכורות #97 מוזגו | מיזוג אינו ראיית הפעלה. writes/workers נשארים כבויים |
| Owner API #99 | Draft, `18bf322c82c4a9000961e43b137ec38c7e23c1fa`; CI success run 38133636670 | חבילת ה־Business החדשה עדיין מקומית ומצריכה commit/push/CI באישור |
| חבילת Business החדשה | נשמרה באישור הבעלים ב־Drafts [MIA #100](https://github.com/assaf0buskila/mia_agent/pull/100) ו־[אפליקציה #65](https://github.com/assaf0buskila/assaf-landingPage/pull/65), מעל #99 ו־#61 | CI ראשוני מצא פערים; תיקוני CI עברו בדיקות וסקירה מקומיות. יש לאמת CI ל־heads החדשים |
| האפליקציה #59 / #61 | Drafts; #61 `ba1278d65d1b17b56ca06c95937c2a7593bc0bce`; שני workflows עברו | שינויי Business/Rubik חדשים עדיין מקומיים; אין merge ל־main הציבורי |
| iPhone / GCP staging | ה־API הפרטי עדיין `18bf322…`, BFF בסיס `ba1278d…`; מסד staging סינתטי נפרד | פריסה פרטית של SHAs חדשים אחרי CI ואישור; קבלה חוזרת ב־Safari/Home Screen |
| אתר → MIA | בדיקות חוזה 214/214; PR #64 מוזג ב־GitHub ל־`7825f8d80817b0e3f22adeb3f6c10dcd9f92d8eb`; שלושה קובצי בדיקות/דוח בלבד | הגדרות intake וגרסת האתר החיה לא אומתו; אין טענה לקבלת ליד חי או לפריסת Vercel |
| קליטה → Owner API | תיקון תצוגת טקסט CRM ארוך משולב מקומית; 25 בדיקות intake PostgreSQL ו־13 בדיקות Owner API עברו; סקירה עצמאית PASS | לא פרוס; CI חדש נדרש אחרי commit/push |
| Google Business | אותו גיליון פרטי, תשע לשוניות עברית/RTL/Rubik; שירות v4 באותה כתובת והרשאות | נתונים סינתטיים בלבד; אינו ה־CRM הקנוני ואינו מקור הכנסות אמיתי |
| תצוגת CRM בגיליון | בדיקת D בפועל עברה: קליטה קנונית, replay/conflict, PostgreSQL, auth/read API, כתיבה חתומה וקבלה; projection revision 1 | snapshot TEST מוגבל; אין scheduler או יבוא לקוחות. מקור הסמכות נשאר PostgreSQL |
| כספים ואישורי MIA | A/B read closeout, C כספים ו־E אישור/שחזור קבלה עברו מול Google TEST בפועל | אינו אישור התנהגות MIA בייצור או מודל/Telegram חי |
| עריכה מהגיליון | טופס Chrome שמר הערות פרויקט בלבד: revision 3 וקבלה עמידה; קריאה דרך OwnerClient/BFF/API, validator ותצוגות עברה; אפס business POSTs ומונה הכספים ללא שינוי | פריסת הלקוח החדש וקבלה פיזית במכשיר עדיין נדרשות |

## הרשאות וחיבור לנתונים אמיתיים

בדיקת catalog חיה מקבילה, עם ביקורת עצמאית, מצאה: auth/read של staging עוברים את guards; בייצור חסרים נתב Owner OS, טבלאות session וחיבורי roles נפרדים. חשבון MIA הראשי הוא superuser ו־PUBLIC TEMPORARY קיים; אין להשתמש בו כקורא Owner OS. אין הרחבת SQL pgvector או פונקציות משתמש עם PUBLIC EXECUTE בשני המסדים שנבדקו. לא בוצעו שינויי DB ולא נקראו לקוחות.

החיבור החדש של Business Ledger תומך **TEST בדיוק** ודוחה PROD/DEV לפני transport. הוא אינו מחובר ל־MIA בייצור; גרסת הייצור אינה מכילה את מודוליו. אין להחליף דגל ולכנות זאת LIVE. הפעלת כספים אמיתיים דורשת חוזה LIVE נפרד, סקירה ואישור משאבים/הרשאות/נתונים. חיבור Sheets ישן בייצור אינו ראיה לחיבור לגיליון Business החדש, והוא לא נבדק עם סודות או נתוני לקוחות.

ראיות מקבילות: [דוח הרשאות](../../../../.codex-worktrees/mia-lead-owner-chain/.cache/pg-permissions-audit/READONLY_PERMISSIONS_REPORT.md) ו־[תוכנית קבלה חיה](../../../../.codex-worktrees/mia-lead-owner-chain/.cache/pg-permissions-audit/LIVE_ACCEPTANCE_PLAN.md); דוח האתר `docs/mia-form-contract-verification-2026-10-12.md` ב־PR #64. תוצאות מקומיות וסיכומי בדיקות נמצאים ב־`.cache/business-ledger-test/`; מפתחות/config אינם חלק מחבילת השחרור.

חבילת GCP הפרוסה עדיין מקבעת SHAs ישנים וכוללת auth/read בלבד, עם רשת API פנימית. הוכנה התאמה מוגבלת ב־`staging/`: נתיבי Business קבועים, scopes לבעלים ב־TEST, סוד בקובץ שרת ונעילת הגיליון/השירות/ה־workspace הקיימים; proxy פנימי מוגבל לשני hosts רשמיים של Google על 443, ללא Internet כללי ל־BFF או למסד. ה־DNS וזמן המנהרה מוגבלים, כתובות פרטיות/metadata נדחות. עשר בדיקות מקומיות וסקירת אבטחה עצמאית עברו. ההתאמה אינה פרוסה; בניית images, בדיקת רשת/זיכרון/rollback ואריזה חתומה לפי hashes עדיין נדרשות אחרי CI ל־heads החדשים. החלפת SHA לבדה אינה מספיקה.

## סדר הסגירה

1. הושלמו קריאת עריכת הגיליון דרך ה־BFF, סקירת הדלתא המשולבת ו־Ruff/widget/רגרסיות ממוקדות. חמש הרצות TEST בפועל עברו: A/B closeout, C, D, E ו־Sheet readonly. אין מדידת אחוז כיסוי שורות חדשה ואין טענה לריצת כל 2,993 הבדיקות על הדלתא החדשה; הקמפיין המלא הושלם לבסיס בלבד.
2. commit/push ופתיחת שני Draft PRs אושרו ובוצעו, בלי merge או force push. יש להשלים CI על ה־heads האחרונים; תיקוני רגרסיות CI ובדיקת logout ב־WebKit נבדקו עצמאית לפני ההרצה החוזרת.
3. להכין את ההתאמה המוגבלת לחבילת staging הקיימת, לבדוק נתיבים/רשת/סודות/rollback ולקבל סקירה עצמאית. רק באישור פריסה פרטית להשתמש בחבילה וב־SHAs שעברו CI על VM ה־staging הקיים; אותו origin/bot נפרד, synthetic DB ו־TEST workbook. לשמור rollback `18bf322…` / `ba1278d…`; לבטל sessions בעת rollback. אין משאבים חדשים או הרשאות ייצור.
4. לקבלת הבעלים: Safari ו־Home Screen, Telegram אמיתי, login/logout/expiry/revoke, CRM, כספים, preview/receipt/recovery; ללא Quick Lead/reminder/provider writes.
5. רק אחר כך שער LIVE מפורש: migration/roles/auth/read על MIA הקנונית, חוזה Ledger LIVE שנבדק, מאגר כספים מתאים וראיית ליד חדש לגיטימי עם מזהים וקבלה. `synced` לבדו אינו הוכחת lease/CAS או מסירת הודעה. אין העתקת לקוחות ל־staging, replay היסטוריה או הרחבת הרשאות frontend.

## צ׳אטים ומשימות אחרות

העבודות הקיימות **״השלם חיבור Mia ל־AssafWeb OS״** ו־**״השלם קליטת לידים ל־AssafWeb OS״** סיפקו בהתאמה את בדיקת האתר והבדיקה/תוכנית ההרשאות. אין צורך לפתוח צ׳אטים כפולים; חבילת Business מרוכזת כאן.

PRs פתוחים נוספים: MIA #91 (timeouts), #46 (מדריך), #8 (Sheets ישן); אתר #60 (מדריך קליניקה), #58 (P0B). הם אינם תלות מוכחת לחיבור Business הנוכחי. אין merge/סגירה/הפעלה שלהם במסגרת זו; P0B נשאר מוקפא. חקירת CRM Sheets ההיסטורית הסתיימה, תיקון נתונים נדחה. Leo/פרסום/SEO נשארים בעבודותיהם הקיימות.

**החלטה:** GO למבחני TEST המבודדים שעברו; NO-GO לשימוש עסקי חי עד שערי השחרור והקבלה. אין שינוי ייצור, public website, DNS, credentials או customer data במשימה זו.
