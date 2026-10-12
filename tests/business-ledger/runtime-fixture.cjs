"use strict";


const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const vm = require("node:vm");

const SECRET = "test-secret-kept-only-in-this-mock";
const WORKSPACE = "assaf-ledger-TEST";
const OWNER = "123456789";
const DEAL_ID = "11111111-1111-4111-8111-111111111111";
const NOW = "2026-10-11T00:00:00.000Z";
const DEAL_HEADERS = ["id", "revision", "title", "amount_minor", "currency", "payment_plan", "start_date", "status", "project_id", "notes", "updated_at"];
const AUDIT_HEADERS = ["event_type", "entity_type", "entity_id", "revision", "request_id", "owner_id", "data_hash", "snapshot_json", "receipt_json", "committed_at"];
const CONFLICT_HEADERS = ["id", "detected_at", "entity_type", "entity_id", "reason_code", "observed_json"];
const ALL_HEADERS = {
  Deals: DEAL_HEADERS, Charges: ["id", "revision", "deal_id", "status", "amount_minor", "currency", "due_date", "updated_at"],
  Payments: ["id", "revision", "charge_id", "status", "amount_minor", "currency", "received_at", "updated_at"],
  Expenses: ["id", "revision", "project_id", "amount_minor", "currency", "occurred_on", "notes", "updated_at"],
  Projects: ["id", "revision", "title", "status", "deadline", "notes", "updated_at"], Settings: ["key", "value"],
  Audit: AUDIT_HEADERS, Conflicts: CONFLICT_HEADERS, "CRM View": ["status", "message"],
};
const HEBREW_SCHEMA = {
  Deals: ["עסקאות", ["מזהה", "גרסה", "כותרת", "סכום באגורות", "מטבע", "תוכנית תשלום", "תאריך התחלה", "סטטוס", "מזהה פרויקט", "הערות", "עודכן בתאריך"]],
  Charges: ["חיובים צפויים", ["מזהה", "גרסה", "מזהה עסקה", "סטטוס", "סכום באגורות", "מטבע", "תאריך חיוב", "עודכן בתאריך"]],
  Payments: ["תשלומים שהתקבלו", ["מזהה", "גרסה", "מזהה חיוב", "סטטוס", "סכום באגורות", "מטבע", "התקבל בתאריך", "עודכן בתאריך"]],
  Expenses: ["הוצאות", ["מזהה", "גרסה", "מזהה פרויקט", "סכום באגורות", "מטבע", "תאריך הוצאה", "הערות", "עודכן בתאריך"]],
  Projects: ["פרויקטים", ["מזהה", "גרסה", "כותרת", "סטטוס", "תאריך יעד", "הערות", "עודכן בתאריך"]],
  Settings: ["הגדרות", ["מפתח", "ערך"]], Audit: ["יומן שינויים", ["סוג אירוע", "סוג ישות", "מזהה ישות", "גרסה", "מזהה בקשה", "מזהה בעלים", "גיבוב נתונים", "תמונת מצב JSON", "קבלה JSON", "נשמר בתאריך"]],
  Conflicts: ["קונפליקטים", ["מזהה", "זוהה בתאריך", "סוג ישות", "מזהה ישות", "קוד סיבה", "נתונים שנצפו"]], "CRM View": ["תצוגת CRM", ["סטטוס", "הודעה"]],
};

function signedBytes(buffer) { return [...buffer].map((value) => value > 127 ? value - 256 : value); }
function hmac(value) { return crypto.createHmac("sha256", SECRET).update(value).digest("hex"); }

function makeSheet(id, rows, onWrite = () => {}) {
  const protections = [];
  return {
    getDataRange: () => ({ getValues: () => rows }),
    getLastRow: () => rows.length,
    getLastColumn: () => Math.max(...rows.map(row => row.length)),
    getSheetId: () => id,
    getRange: (row, column, rowCount, columnCount) => ({
      getValues: () => Array.from({ length: rowCount }, (_, r) => Array.from({ length: columnCount }, (_, c) => (rows[row - 1 + r] || [])[column - 1 + c] ?? "")),
      setValues: (values) => { onWrite(); values.forEach((source, r) => source.forEach((value, c) => {
        while (rows.length < row + r) rows.push([]);
        rows[row - 1 + r][column - 1 + c] = value;
      })); },
    }),
    getProtections: () => protections,
    protect: () => {
      const protection = { setDescription() { return this; }, getEditors: () => [], removeEditors() {}, canDomainEdit: () => false, setDomainEdit() {} };
      protections.push(protection);
      return protection;
    },
  };
}

function runtime({ deals, audit, settings, title = "Assaf Business Ledger TEST", throwBatch = false, schema = "english", ambiguousDeals = false, userStore = {}, applyBatch = false, requireExplicitHmacCharset = false } = {}) {
  const rowsByName = {};
  Object.keys(ALL_HEADERS).forEach((name) => {
    const physicalName = schema === "hebrew" ? HEBREW_SCHEMA[name][0] : name;
    const headers = schema === "hebrew" ? HEBREW_SCHEMA[name][1] : ALL_HEADERS[name];
    rowsByName[physicalName] = [headers];
  });
  const dealsName = schema === "hebrew" ? HEBREW_SCHEMA.Deals[0] : "Deals";
  const auditName = schema === "hebrew" ? HEBREW_SCHEMA.Audit[0] : "Audit";
  const settingsName = schema === "hebrew" ? HEBREW_SCHEMA.Settings[0] : "Settings";
  if (deals) rowsByName[dealsName] = deals;
  if (audit) rowsByName[auditName] = audit;
  rowsByName[settingsName] = settings || [[schema === "hebrew" ? "מפתח" : "key", schema === "hebrew" ? "ערך" : "value"], ["mode", "TEST"], ["currency", "ILS"], ["vat_rate", "0"], ["workspace_id", WORKSPACE]];
  if (ambiguousDeals) rowsByName[schema === "hebrew" ? "Deals" : HEBREW_SCHEMA.Deals[0]] = [schema === "hebrew" ? DEAL_HEADERS : HEBREW_SCHEMA.Deals[1]];
  const sheets = {};
  let batchCalls = 0, released = 0, rangeWrites = 0;
  const hmacCharsets = [];
  const batchBodies = [];
  Object.keys(rowsByName).forEach((name, i) => { sheets[name] = makeSheet(i + 1, rowsByName[name], () => { rangeWrites += 1; }); });
  const ui = { ButtonSet: { YES_NO: "YES_NO" }, Button: { YES: "YES" }, alert: (...args) => args.length > 1 ? "YES" : undefined, createMenu: () => ({ addItem() { return this; }, addSeparator() { return this; }, addToUi() {} }) };
  function applyBatchBody(body) {
    for (const request of body.requests || []) {
      if (!request.updateCells) continue;
      const target = Object.values(sheets).find((sheet) => sheet.getSheetId() === request.updateCells.range.sheetId);
      if (!target) continue;
      const rows = target.getDataRange().getValues();
      request.updateCells.rows.forEach((sourceRow, offset) => {
      const rowIndex = request.updateCells.range.startRowIndex + offset;
      while (rows.length <= rowIndex) rows.push([]);
      sourceRow.values.forEach((cell, column) => {
        const value = cell.userEnteredValue;
        rows[rowIndex][column] = Object.prototype.hasOwnProperty.call(value, "numberValue") ? value.numberValue : Object.prototype.hasOwnProperty.call(value, "boolValue") ? value.boolValue : value.stringValue;
      });
      });
    }
  }
  const context = vm.createContext({
    console,
    PropertiesService: { getScriptProperties: () => ({ getProperty: (key) => ({
      TEST_WORKBOOK_ID: "test-workbook", TEST_WORKSPACE_ID: WORKSPACE,
      OWNER_NUMERIC_IDS: OWNER, LEDGER_HMAC_SECRET: SECRET, OPERATOR_OWNER_MAP: `{"operator@example.test":"${OWNER}"}`,
    })[key] || null }), getUserProperties: () => ({ getProperty: (key) => userStore[key] || null, setProperty: (key, value) => { userStore[key] = value; }, deleteProperty: (key) => { delete userStore[key]; } }) },
    LockService: {
      getDocumentLock: () => { throw new Error("document lock must not be used by a web app"); },
      getScriptLock: () => ({ tryLock: () => true, releaseLock: () => { released += 1; } }),
    },
    SpreadsheetApp: { ProtectionType: { SHEET: "SHEET" }, openById: () => ({ getName: () => title, getSheetByName: (name) => sheets[name] || null }), getUi: () => ui },
    Sheets: { Spreadsheets: { batchUpdate: (body) => { batchCalls += 1; batchBodies.push(body); if (throwBatch) throw new Error("ambiguous provider failure"); if (applyBatch) applyBatchBody(body); } } },
    Utilities: {
      newBlob: (value) => ({ getBytes: () => [...Buffer.from(value)] }),
      computeHmacSha256Signature: (value, secret, charset) => {
        hmacCharsets.push(charset);
        if (requireExplicitHmacCharset && charset !== "UTF_8") throw new Error("explicit UTF-8 charset required");
        return signedBytes(crypto.createHmac("sha256", secret).update(Buffer.from(value, "utf8")).digest());
      },
      computeDigest: (_algorithm, value) => signedBytes(crypto.createHash("sha256").update(value).digest()),
      DigestAlgorithm: { SHA_256: "SHA_256" }, Charset: { UTF_8: "UTF_8" },
      getUuid: () => "99999999-9999-4999-8999-999999999999",
      formatDate: () => { throw new Error("not expected"); },
    },
    ContentService: {
      MimeType: { JSON: "JSON" },
      createTextOutput: (text) => ({ text, setMimeType() { return this; } }),
    },
    Session: { getActiveUser: () => ({ getEmail: () => "operator@example.test" }) }, HtmlService: {},
  });
  vm.runInContext(fs.readFileSync("ops/business-ledger/core.js", "utf8"), context, { filename: "core.js" });
  vm.runInContext(fs.readFileSync("ops/business-ledger/finance-core.js", "utf8"), context, { filename: "finance-core.js" });
  vm.runInContext(fs.readFileSync("ops/business-ledger/Code.gs", "utf8"), context, { filename: "Code.gs" });
  vm.runInContext(fs.readFileSync("ops/business-ledger/finance-adapter.gs", "utf8"), context, { filename: "finance-adapter.gs" });
  vm.runInContext(fs.readFileSync("ops/business-ledger/finance-forms.gs", "utf8"), context, { filename: "finance-forms.gs" });
  vm.runInContext(fs.readFileSync("ops/business-ledger/crm-projection.gs", "utf8"), context, { filename: "crm-projection.gs" });
  return { context, sheets, settings: rowsByName[settingsName], userStore, batchBodies, hmacCharsets, batchCalls: () => batchCalls, rangeWrites: () => rangeWrites, released: () => released };
}

function event(operation, data) {
  const payload = JSON.stringify({ request_id: "22222222-2222-4222-8222-222222222222", timestamp: Math.floor(Date.now() / 1000), workspace_id: WORKSPACE, owner_id: OWNER, operation, data });
  return { postData: { contents: JSON.stringify({ payload, signature: hmac(payload) }) } };
}

function inner(output) { return JSON.parse(JSON.parse(output.text).payload); }

function acceptedRows(count, notes = "") {
  const deals = [DEAL_HEADERS], audits = [AUDIT_HEADERS];
  for (let i = 1; i <= count; i += 1) {
    const suffix = String(i).padStart(12, "0");
    const id = `00000000-0000-4000-8000-${suffix}`;
    const requestId = `10000000-0000-4000-8000-${suffix}`;
    const accepted = { id, revision: 1, title: `Deal ${i}`, amount_minor: 1000, currency: "ILS", payment_plan: "full", start_date: "2026-10-11", status: "active", project_id: null, notes, updated_at: NOW };
    deals.push([id, 1, accepted.title, 1000, "ILS", "full", "2026-10-11", "active", "", notes, NOW]);
    audits.push(["accepted", "deal", id, 1, requestId, OWNER, `hash-${i}`, JSON.stringify(accepted), JSON.stringify({ status: "committed", request_id: requestId }), NOW]);
  }
  return { deals, audit: audits };
}

module.exports = { SECRET, WORKSPACE, OWNER, DEAL_ID, NOW, DEAL_HEADERS, AUDIT_HEADERS, CONFLICT_HEADERS, ALL_HEADERS, HEBREW_SCHEMA, signedBytes, hmac, makeSheet, runtime, event, inner, acceptedRows };
