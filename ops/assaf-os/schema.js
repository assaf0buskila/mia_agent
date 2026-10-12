/* global globalThis */
"use strict";

(function (root, factory) {
  const value = factory();
  if (typeof module === "object" && module.exports) module.exports = value;
  root.AssafOsSchema = value;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const SCHEMA_VERSION = "assaf-os-unified.v1";
  const INSTALL_CONFIRMATION = "ASSAF_OS_UNIFIED_V1";

  const ENUMS = Object.freeze({
    path: Object.freeze(["לימוד", "אוטומציה"]),
    lead_status: Object.freeze(["חדש", "רלוונטי", "לא רלוונטי", "לא ענה", "הפך ללקוח"]),
    client_status: Object.freeze(["פעיל", "לא פעיל"]),
    task_status: Object.freeze(["לביצוע", "בתהליך", "ממתין", "בוצע", "בוטל"]),
    work_status: Object.freeze(["מתוכננת", "פעילה", "הושלמה", "בוטלה"]),
    time_status: Object.freeze(["פעיל", "בוטל"]),
    payment_kind: Object.freeze(["התקבל", "החזר", "בוטל"]),
    payment_status: Object.freeze(["שולם", "לא שולם", "בוטל"]),
    expense_source: Object.freeze(["תשלום בפועל", "דיווח שימוש בפלטפורמה"]),
    source: Object.freeze(["משתמש", "OS", "MIA"]),
    currency: Object.freeze(["ILS", "USD", "EUR", "GBP"]),
    shared_path: Object.freeze(["לימוד", "אוטומציה", "משותף"]),
  });

  function f(key, label, type, role, options) {
    return Object.freeze(Object.assign({ key: key, label: label, type: type, role: role, required: false }, options || {}));
  }

  const tabs = [
    {
      key: "summary", name: "סיכום", kind: "calculated", frozenRows: 1,
      fields: [
        f("summary_scope", "סוג סיכום", "text", "calculated"),
        f("month", "חודש", "month", "calculated"),
        f("person_id", "מזהה אדם", "id", "calculated"),
        f("client_name", "לקוח", "text", "calculated"),
        f("path", "מסלול", "enum", "calculated", { enum: "path" }),
        f("currency", "מטבע", "enum", "calculated", { enum: "currency" }),
        f("net_received_ils", "תקבולים נטו", "money_ils", "calculated"),
        f("hours", "שעות מתועדות", "decimal", "calculated"),
        f("weighted_hourly_ils", "הכנסה משוקללת לשעה", "money_ils", "calculated"),
        f("missing_time", "חסר דיווח זמן", "boolean", "calculated"),
        f("paid_expenses_ils", "הוצאות ששולמו בחודש", "money_ils", "calculated"),
        f("net_cash_ils", "תזרים נטו בחודש", "money_ils", "calculated"),
        f("coverage", "כיסוי נתונים", "text", "system"),
        f("calculated_at", "חושב בתאריך", "datetime", "system"),
      ],
    },
    {
      key: "leads", name: "לידים", kind: "input", frozenRows: 1,
      fields: [
        f("lead_id", "מזהה ליד", "id", "system", { required: true, stable: true }),
        f("person_id", "מזהה אדם", "id", "system", { required: true, stable: true, note: "נשמר גם בהפיכה ללקוח" }),
        f("name", "שם", "text", "input", { required: true }),
        f("phone", "טלפון", "text", "input"), f("email", "אימייל", "text", "input"),
        f("path", "מסלול", "enum", "input", { enum: "path", required: true }),
        f("status", "סטטוס", "enum", "input", { enum: "lead_status", required: true }),
        f("source_note", "מקור / הערה", "text", "input"),
        f("created_at", "נוצר בתאריך", "datetime", "system"), f("updated_at", "עודכן בתאריך", "datetime", "system"),
        f("revision", "גרסה", "integer", "system"), f("write_source", "מקור כתיבה", "enum", "system", { enum: "source" }),
        f("request_id", "מזהה בקשה", "id", "system"),
      ],
    },
    {
      key: "clients", name: "לקוחות", kind: "input", frozenRows: 1,
      fields: [
        f("person_id", "מזהה אדם", "id", "system", { required: true, stable: true, unique: true }),
        f("name", "שם", "text", "input", { required: true }), f("phone", "טלפון", "text", "input"), f("email", "אימייל", "text", "input"),
        f("status", "סטטוס", "enum", "input", { enum: "client_status", required: true }),
        f("notes", "הערות", "text", "input"), f("created_at", "נוצר בתאריך", "datetime", "system"),
        f("updated_at", "עודכן בתאריך", "datetime", "system"), f("revision", "גרסה", "integer", "system"),
        f("write_source", "מקור כתיבה", "enum", "system", { enum: "source" }), f("request_id", "מזהה בקשה", "id", "system"),
        f("received_learning_ils", "תקבולים נטו — לימוד (₪ בלבד)", "money_ils", "calculated"),
        f("received_automation_ils", "תקבולים נטו — אוטומציה (₪ בלבד)", "money_ils", "calculated"),
        f("total_received_ils", "סה״כ תקבולים נטו (₪ בלבד)", "money_ils", "calculated"),
        f("recorded_hours", "שעות מתועדות", "decimal", "calculated"),
        f("hourly_ils", "הכנסה משוקללת לשעה (₪ בלבד)", "money_ils", "calculated", { note: "סה״כ תקבולים נטו חלקי סה״כ שעות; חסר זמן נשאר ריק" }),
        f("foreign_currency_flag", "קיימים תקבולים במטבע זר", "boolean", "calculated"),
      ],
    },
    {
      key: "works", name: "עבודות", kind: "input", frozenRows: 1,
      fields: [
        f("work_id", "מזהה עבודה", "id", "system", { required: true, stable: true, unique: true }),
        f("person_id", "מזהה אדם", "id", "input", { required: true, ref: "clients.person_id" }),
        f("title", "שם העבודה", "text", "input", { required: true }),
        f("path", "מסלול", "enum", "input", { enum: "path", required: true, immutableAfterCreate: true }),
        f("start_date", "תאריך התחלה", "date", "input"),
        f("status", "סטטוס", "enum", "input", { enum: "work_status", required: true }),
        f("agreed_amount_ils", "סכום מוסכם", "money_ils", "input", { note: "אופציונלי; אינו הכנסה בפועל" }),
        f("currency", "מטבע", "enum", "input", { enum: "currency" }),
        f("created_at", "נוצר בתאריך", "datetime", "system"), f("updated_at", "עודכן בתאריך", "datetime", "system"),
        f("revision", "גרסה", "integer", "system"), f("write_source", "מקור כתיבה", "enum", "system", { enum: "source" }),
        f("request_id", "מזהה בקשה", "id", "system"),
        f("received_ils", "תקבולים נטו לעבודה (₪ בלבד)", "money_ils", "calculated"),
        f("recorded_hours", "שעות מתועדות לעבודה", "decimal", "calculated"),
        f("hourly_ils", "הכנסה משוקללת לשעה (₪ בלבד)", "money_ils", "calculated", { note: "חסר זמן נשאר ריק" }),
        f("foreign_currency_flag", "קיימים תקבולים במטבע זר", "boolean", "calculated"),
      ],
    },
    {
      key: "payments", name: "תקבולים", kind: "input", frozenRows: 1,
      fields: [
        f("payment_id", "מזהה תקבול", "id", "system", { required: true, stable: true, unique: true }),
        f("person_id", "מזהה אדם", "id", "input", { required: true, ref: "clients.person_id" }),
        f("work_id", "מזהה עבודה", "id", "input", { required: true, ref: "works.work_id" }),
        f("path", "מסלול", "enum", "input", { enum: "path", required: true, immutableAfterCreate: true }),
        f("date", "תאריך", "date", "input", { required: true }), f("actual_amount_ils", "סכום בפועל", "money_ils", "input", { required: true }),
        f("currency", "מטבע", "enum", "input", { enum: "currency", required: true }),
        f("kind", "סוג תקבול", "enum", "input", { enum: "payment_kind", required: true }),
        f("original_payment_id", "מזהה תקבול מקורי להחזר", "id", "input", { ref: "payments.payment_id", note: "חובה בהחזר; ריק בתקבול רגיל או מבוטל" }),
        f("notes", "הערות", "text", "input"), f("created_at", "נוצר בתאריך", "datetime", "system"),
        f("revision", "גרסה", "integer", "system"), f("write_source", "מקור כתיבה", "enum", "system", { enum: "source" }),
        f("request_id", "מזהה בקשה", "id", "system"),
      ],
    },
    {
      key: "time", name: "שעות עבודה", kind: "input", frozenRows: 1,
      fields: [
        f("time_id", "מזהה דיווח", "id", "system", { required: true, stable: true, unique: true }),
        f("person_id", "מזהה אדם", "id", "input", { required: true, ref: "clients.person_id" }),
        f("work_id", "מזהה עבודה", "id", "input", { required: true, ref: "works.work_id" }),
        f("path", "מסלול", "enum", "input", { enum: "path", required: true, immutableAfterCreate: true }),
        f("date", "תאריך", "date", "input", { required: true }),
        f("minutes", "דקות עבודה", "integer", "input", { required: true, minimum: 0, note: "כולל הכנה ותמיכה" }),
        f("description", "תיאור", "text", "input"), f("status", "סטטוס", "enum", "input", { enum: "time_status", required: true }),
        f("created_at", "נוצר בתאריך", "datetime", "system"), f("revision", "גרסה", "integer", "system"),
        f("write_source", "מקור כתיבה", "enum", "system", { enum: "source" }), f("request_id", "מזהה בקשה", "id", "system"),
      ],
    },
    {
      key: "expenses", name: "הוצאות והשקעות", kind: "input", frozenRows: 1,
      fields: [
        f("expense_id", "מזהה הוצאה", "id", "system", { required: true, stable: true, unique: true }),
        f("date", "תאריך", "date", "input", { required: true }), f("category", "קטגוריה", "text", "input", { required: true }),
        f("platform", "פלטפורמה", "enum", "input", { values: ["Google Ads", "Meta", "אחר"], required: true }),
        f("path", "מסלול", "enum", "input", { enum: "shared_path", required: true }), f("vendor", "ספק", "text", "input"),
        f("amount_ils", "סכום", "money_ils", "input", { required: true }), f("currency", "מטבע", "enum", "input", { enum: "currency", required: true }),
        f("payment_status", "סטטוס תשלום", "enum", "input", { enum: "payment_status", required: true }),
        f("source_type", "סוג מקור", "enum", "input", { enum: "expense_source", required: true, note: "שימוש במודעות אינו הוכחת תשלום" }),
        f("source_key", "מפתח מקור", "text", "input", { required: true }), f("notes", "הערות", "text", "input"),
        f("created_at", "נוצר בתאריך", "datetime", "system"), f("revision", "גרסה", "integer", "system"),
        f("write_source", "מקור כתיבה", "enum", "system", { enum: "source" }), f("request_id", "מזהה בקשה", "id", "system"),
      ],
    },
    {
      key: "tasks", name: "משימות", kind: "input", frozenRows: 1,
      fields: [
        f("task_id", "מזהה משימה", "id", "system", { required: true, stable: true, unique: true }),
        f("title", "משימה", "text", "input", { required: true }), f("person_id", "מזהה אדם", "id", "input", { ref: "clients.person_id" }),
        f("work_id", "מזהה עבודה", "id", "input", { ref: "works.work_id" }), f("path", "מסלול", "enum", "input", { enum: "path" }),
        f("status", "סטטוס", "enum", "input", { enum: "task_status", required: true }), f("due_date", "תאריך יעד", "date", "input"),
        f("notes", "הערות", "text", "input"), f("created_at", "נוצר בתאריך", "datetime", "system"),
        f("updated_at", "עודכן בתאריך", "datetime", "system"), f("revision", "גרסה", "integer", "system"),
        f("write_source", "מקור כתיבה", "enum", "system", { enum: "source" }), f("request_id", "מזהה בקשה", "id", "system"),
      ],
    },
    {
      key: "settings", name: "הגדרות", kind: "input", frozenRows: 1,
      fields: [
        f("key", "מפתח", "text", "system", { required: true, unique: true }), f("value", "ערך", "text", "input"),
        f("description", "תיאור", "text", "system"), f("updated_at", "עודכן בתאריך", "datetime", "system"),
      ],
    },
  ].map(function (tab, index) { return Object.freeze(Object.assign({ order: index + 1 }, tab, { fields: Object.freeze(tab.fields) })); });

  function tab(key) { return tabs.find(function (item) { return item.key === key; }) || null; }
  function headers(key) { const found = tab(key); return found ? found.fields.map(function (field) { return field.label; }) : []; }

  return Object.freeze({ version: SCHEMA_VERSION, installConfirmation: INSTALL_CONFIRMATION, enums: ENUMS, tabs: Object.freeze(tabs), tab: tab, headers: headers });
});
