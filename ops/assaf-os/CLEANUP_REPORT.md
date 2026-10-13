# סגירת משאבי Assaf OS הישן

12.10.2026, ישראל. בוצע בעקבות בקשת הבעלים לנקות את ה־OS הקודם ולהפסיק לשלם על סביבת האירוח הישנה. בדיקה נפרדת וסקירת HEAVY עצמאית אישרו את המטרות וההפרדה מ־MIA לפני הביצוע.

| מטרה | זהות שנבדקה לפני מחיקה | תוצאה מאומתת |
|---|---|---|
| VM | tomibot-telegram / me-west1-a / owner-os-staging; ID 2838465986836182722 | נמחק; רשימת המשאב הרלוונטי ריקה |
| boot disk | owner-os-staging; ID 5435912514961815234; 20GiB; boot=true, autoDelete=true; מחובר רק ל־VM זה | נמחק יחד עם ה־VM; רשימת הדיסק הרלוונטי ריקה |
| IPv4 סטטי | owner-os-staging-ip / me-west1; ID 4005858447444031225; 34.0.74.144 | לפני שחרור: RESERVED ללא users; נמחק; רשימת הכתובת הרלוונטית ריקה |
| MIA נשמרה | mia-assafweb / me-west1-a / mia; ID 8764131532174833241 | אותו ID ו־RUNNING לאחר הניקוי |

רק פקודות metadata ומחיקה למטרות לעיל בוצעו. הפרויקט, רשתות, firewall rules, bucket בנייה, Artifact Registry, שרת MIA, דיסק MIA ובוט MIA לא נמחקו או שונו. לא נוצרו משאב חדש או snapshot בתשלום.

## חומר שחזור שנבדק

חומר קיים ב־`.codex-worktrees/owner-os-private-staging/.cache/` נבדק בלי קריאת סודות:

- `owner-os-gcp-staging.zip`: SHA256 `1a3fc125ca42fe0a52eae42371637615e53d34bf7df66bb57c18a9058b155a64`.
- `owner-os-gcp-images.tar`: SHA256 `e1b153b5bf50ef00a84027b93c9328006ff148ea11d8ec66c1b58f12660c2419`.
- manifests, locks, validation והוראות הקמה מחדש קיימים לצד החבילות.

החבילות משמרות מקור, images ותצורה לצורך הקמה מחדש. הן אינן גיבוי לסשנים, לשינויים במסד הבדיקות הסינתטי או ל־credentials בזמן ריצה. שחזור דורש credentials חדשים ואינו מבטיח את אותה כתובת IP. אין לתאר את המחיקה ככיבוי הפיך מיידית. כתובת ה־nip.io הישנה של staging הפסיקה להיות יעד פעיל.

## ראיות הביצוע

ב־worktree האיחוד נשמרו metadata ללא סודות ב־`.cache/os-cleanup-vm-before.json`, `.cache/os-cleanup-disk-after.json`, `.cache/os-cleanup-address-before.json` ו־`.cache/os-cleanup-after.json`. לפני כל מחיקה נבדקו הפרויקט, האזור, השם והזהות; אחרי המחיקות נבדקו שלוש רשימות המשאבים בהצלחה ואותה זהות MIA נבדקה מחדש.

PASS להסרת שלושת מקורות החיוב שזוהו עבור staging של OS. חשבונית או export חיובים מאוחר עדיין לא נבדקו; חיובים שכבר נצברו לפני המחיקה יכולים להופיע בהמשך. אין טענה שכל החשבון או שירותי MIA, AI ו־Workspace הם ללא עלות. משאבי פרויקט אחרים שנשמרו אינם חלק מהבדיקה הזאת.
