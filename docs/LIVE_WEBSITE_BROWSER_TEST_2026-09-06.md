# Live website browser test — 2026-09-06

Target: https://www.assafweb.com/. Tested through the browser plugin against the live Mia widget, using a new test conversation and a 390 × 844 mobile viewport as well as the default desktop viewport. Mobile viewport testing is not iPhone Safari testing.

## Observed conversation

1. Visitor: `יש לי מספרה`
   Mia: `מה העסק עושה בפועל?`
   **Fail:** business was already explicitly identified.
2. Visitor: `מספרה, תספורות וצבע. אני מפספס הודעות בוואטסאפ בזמן שאני עובד עם לקוחות`
   Mia: `אולי אפשר להקל על הפספוסים בוואטסאפ בזמן התספורות והצבע, כדי שפחות פניות יישארו בלי מענה. כדי שאסף יוכל להמשיך אתכם, צריך טלפון או אימייל.`
   **Pass:** value suggestion and phone-or-email form appeared without more discovery.
3. Visitor: `לא רוצה להשאיר פרטים עדיין. איך זה יעזור לי בפועל?`
   Mia: `אין לי עדיין פירוט מדויק איך זה יעבוד בפועל במספרה שלכם. אסף יקבל את ההקשר שכבר שיתפתם ויסביר לכם ישירות.`
   **Quality failure:** no useful practical explanation; handoff language despite no contact supplied. No repeated contact demand in this reply.
4. Visitor: `כמה זה עולה?`
   Mia: `אין מחירון ציבורי קבוע. המחיר תלוי במה שבונים, ואחרי שיחת אבחון קצרה אסף נותן הצעה מסודרת עם מחיר סופי, בלי מע״מ ובלי הפתעות בהמשך.`
   **Pass:** no invented numeric price.
5. After refresh, visitor: `אוקיי, ומה השלב הבא?`
   Mia: `השלב הבא הוא להעביר אתכם לאסף בוואטסאפ. הוא יקבל את ההקשר שכבר שיתפתם כאן וימשיך משם.`
   Contact form appeared again; no business/friction rediscovery.

## Widget checks

- Empty contact submission showed `השאירו טלפון או אימייל כדי שאסף יחזור אליכם.` No contact values were submitted.
- Close/reopen retained transcript and form.
- Refresh retained transcript but lost the contact form until the next reply. This is a recovery defect.
- Mobile viewport showed chat input and send/record controls within the viewport. No physical device or software-keyboard test was performed.
- Microphone control eventually entered its active state. Closing the panel cancelled recording; reopening showed inactive state. No deliberate recording upload or transcription test was performed.
- Direct health-page navigation was blocked by the browser client, so this test did not independently establish the deployed SHA.

## Source corroboration

`app/surfaces/site.py` business markers omit Hebrew `מספרה`; explicit short business statements can fall through to discovery. `app/web/ask_mia.js` restores transcript rows as role/text only and does not restore contact controls with those rows.

This report records observed failures as well as passes. It does not establish that all conversion scenarios, contact handoff, or iPhone Safari voice work.
