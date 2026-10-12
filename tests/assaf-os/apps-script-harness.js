"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const vm = require("node:vm");
const Schema = require("../../ops/assaf-os/schema.js");
const Core = require("../../ops/assaf-os/core.js");
const Runtime = require("../../ops/assaf-os/runtime.js");

function cellValue(cell) {
  if (!cell || !cell.userEnteredValue) return "";
  const value = cell.userEnteredValue;
  if (Object.prototype.hasOwnProperty.call(value, "numberValue")) return value.numberValue;
  if (Object.prototype.hasOwnProperty.call(value, "boolValue")) return value.boolValue;
  if (Object.prototype.hasOwnProperty.call(value, "stringValue")) return value.stringValue;
  return "";
}

class Range {
  constructor(sheet, row, column, rows, columns) { this.sheet = sheet; this.row = row; this.column = column; this.rows = rows; this.columns = columns; }
  getValues() { this.sheet.workbook.metrics.getValues += 1; return Array.from({ length: this.rows }, (_, r) => Array.from({ length: this.columns }, (_, c) => this.sheet.value(this.row + r, this.column + c))); }
  getFormulas() { this.sheet.workbook.metrics.getFormulas += 1; return Array.from({ length: this.rows }, (_, r) => Array.from({ length: this.columns }, (_, c) => this.sheet.formula(this.row + r, this.column + c))); }
  getSheet() { return this.sheet; }
  getRow() { return this.row; }
  setDataValidation(rule) { this.sheet.workbook.metrics.setDataValidation += 1; this.sheet.validations.set(`${this.row}:${this.column}:${this.rows}`, rule); return this; }
}

class Sheet {
  constructor(workbook, id, name, headers, maxRows = 2001) {
    this.workbook = workbook; this.id = id; this.name = name; this.maxRows = maxRows;
    this.rows = [headers.slice()]; this.formulas = [headers.map(() => "")];
    this.validations = new Map();
  }
  getName() { return this.name; }
  getSheetId() { return this.id; }
  getParent() { return this.workbook; }
  getMaxRows() { return this.maxRows; }
  getLastRow() { this.workbook.metrics.getLastRow += 1; for (let i = this.rows.length - 1; i >= 0; i -= 1) if ((this.rows[i] || []).some((v) => v !== "" && v !== null)) return i + 1; return 0; }
  getLastColumn() { this.workbook.metrics.getLastColumn += 1; return Math.max(0, ...this.rows.map((row) => { for (let i = row.length - 1; i >= 0; i -= 1) if (row[i] !== "" && row[i] !== null) return i + 1; return 0; })); }
  getDataRange() { this.workbook.metrics.getDataRange += 1; return new Range(this, 1, 1, Math.max(this.getLastRow(), 1), Math.max(this.getLastColumn(), 1)); }
  getRange(row, column, rows = 1, columns = 1) { return new Range(this, row, column, rows, columns); }
  value(row, column) { return (this.rows[row - 1] || [])[column - 1] ?? ""; }
  formula(row, column) { return (this.formulas[row - 1] || [])[column - 1] || ""; }
  setRow(row, values, formulas) {
    if (row > this.maxRows) throw new Error("grid_range_exceeded");
    while (this.rows.length < row) { this.rows.push([]); this.formulas.push([]); }
    this.rows[row - 1] = values.slice(); this.formulas[row - 1] = formulas ? formulas.slice() : values.map(() => "");
  }
  setCell(row, column, value) {
    if (row > this.maxRows) throw new Error("grid_range_exceeded");
    while (this.rows.length < row) { this.rows.push([]); this.formulas.push([]); }
    this.rows[row - 1][column - 1] = value; this.formulas[row - 1][column - 1] = "";
  }
}

class Workbook {
  constructor(id) { this.id = id; this.sheets = new Map(); this.nextId = 1; this.resetMetrics(); }
  resetMetrics() { this.metrics = { getValues: 0, getFormulas: 0, getLastRow: 0, getLastColumn: 0, getDataRange: 0, setDataValidation: 0, batchUpdate: 0 }; }
  add(name, headers, maxRows) { const sheet = new Sheet(this, this.nextId++, name, headers, maxRows); this.sheets.set(name, sheet); return sheet; }
  getSheetByName(name) { return this.sheets.get(name) || null; }
  getId() { return this.id; }
}

function createHarness(options = {}) {
  const binding = { owner_id: "123456", workspace_id: "workspace-main", workbook_id: "Workbook_12345678901234567890", mode: options.mode || "TEST" };
  const secret = "h".repeat(40), workbook = new Workbook(binding.workbook_id), batches = [];
  const scriptProperties = { ASSAF_OS_OWNER_ID: binding.owner_id, ASSAF_OS_WORKSPACE_ID: binding.workspace_id, ASSAF_OS_WORKBOOK_ID: binding.workbook_id, ASSAF_OS_MODE: binding.mode, ASSAF_OS_HMAC_SECRET: secret };
  Schema.tabs.forEach((tab) => workbook.add(tab.name, tab.fields.map((field) => field.label), options.maxRows || 2001));
  const meta = {
    accepted: ["entity", "record_id", "revision", "input_fingerprint", "raw_fingerprint", "snapshot_json", "accepted_at", "source", "request_id"],
    proposals: ["proposal_id", "request_id", "owner_id", "actor_source", "status", "entity", "record_id", "expected_revision", "target_fingerprint", "proposal_json", "created_at", "expires_at", "decided_at"],
    receipts: ["request_id", "owner_id", "proposal_id", "status", "receipt_json", "committed_at"],
    replay: ["request_id", "owner_id", "operation", "actor_source", "request_hash", "seen_at"],
    issues: ["issue_id", "detected_at", "code", "entity", "record_id", "row_number", "status"],
  };
  Object.entries(meta).forEach(([key, headers]) => workbook.add(`_assaf_os_${key}`, headers, options.maxRows || 2001));

  function applyRequest(request) {
    if (request.appendCells) {
      const sheet = [...workbook.sheets.values()].find((item) => item.id === request.appendCells.sheetId);
      request.appendCells.rows.forEach((row) => sheet.setRow(sheet.getLastRow() + 1, row.values.map(cellValue)));
      return;
    }
    if (request.updateCells) {
      const spec = request.updateCells, sheet = [...workbook.sheets.values()].find((item) => item.id === spec.range.sheetId);
      spec.rows.forEach((row, r) => row.values.forEach((cell, c) => sheet.setCell(spec.range.startRowIndex + r + 1, spec.range.startColumnIndex + c + 1, cellValue(cell))));
      return;
    }
    if (request.repeatCell) {
      const spec = request.repeatCell, sheet = [...workbook.sheets.values()].find((item) => item.id === spec.range.sheetId);
      if (spec.range.endRowIndex > sheet.getMaxRows()) throw new Error("grid_range_exceeded");
      for (let r = spec.range.startRowIndex + 1; r <= spec.range.endRowIndex; r += 1) for (let c = spec.range.startColumnIndex + 1; c <= spec.range.endColumnIndex; c += 1) sheet.setCell(r, c, "");
    }
  }

  let uuid = 1;
  const context = {
    Date, JSON, Math, Set, Map,
    AssafOsSchema: Schema, AssafOsCore: Core, AssafOsRuntime: Runtime,
    ContentService: { MimeType: { JSON: "json" }, createTextOutput: (text) => ({ text, setMimeType() { return this; } }) },
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (key) => scriptProperties[key] || null,
      setProperty: (key, value) => { scriptProperties[key] = String(value); },
      deleteProperty: (key) => { delete scriptProperties[key]; },
    }) },
    SpreadsheetApp: {
      openById: (id) => { if (id !== binding.workbook_id) throw new Error("wrong_workbook"); return workbook; },
      newDataValidation: () => {
        const rule = {};
        return {
          setAllowInvalid(value) { rule.allowInvalid = value; return this; }, setHelpText(value) { rule.help = value; return this; },
          requireValueInList(values) { rule.values = values.slice(); return this; }, requireFormulaSatisfied(value) { rule.formula = value; return this; },
          requireDate() { rule.kind = "date"; return this; }, requireNumberGreaterThanOrEqualTo(value) { rule.minimum = value; return this; },
          build() { return { ...rule }; },
        };
      },
    },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock() {} }) },
    Sheets: { Spreadsheets: { batchUpdate: (body, id) => { if (id !== binding.workbook_id) throw new Error("wrong_workbook"); workbook.metrics.batchUpdate += 1; batches.push(body.requests); body.requests.forEach(applyRequest); } } },
    Utilities: {
      Charset: { UTF_8: "UTF_8" }, DigestAlgorithm: { SHA_256: "SHA_256" },
      newBlob: (value) => ({ getBytes: () => [...Buffer.from(value, "utf8")] }),
      computeHmacSha256Signature: (value, key) => [...crypto.createHmac("sha256", key).update(value, "utf8").digest()].map((v) => v > 127 ? v - 256 : v),
      computeDigest: (_algorithm, value) => [...crypto.createHash("sha256").update(value, "utf8").digest()].map((v) => v > 127 ? v - 256 : v),
      getUuid: () => `00000000-0000-4000-8000-${String(uuid++).padStart(12, "0")}`,
      formatDate: (value, timeZone) => { const parts = new Intl.DateTimeFormat("en-US", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(value); const pick = (type) => parts.find((part) => part.type === type).value; return `${pick("year")}-${pick("month")}-${pick("day")}`; },
    },
  };
  vm.createContext(context);
  vm.runInContext(options.runtimeSource || fs.readFileSync("ops/assaf-os/UnifiedRuntime.gs", "utf8"), context, { filename: "UnifiedRuntime.gs" });
  if (options.withSheets) vm.runInContext(options.sheetsSource || fs.readFileSync("ops/assaf-os/UnifiedSheets.gs", "utf8"), context, { filename: "UnifiedSheets.gs" });

  function sign(request) { const payload = JSON.stringify(request); return { payload, signature: crypto.createHmac("sha256", secret).update(payload).digest("hex") }; }
  function call(request) {
    const output = context.doPost({ postData: { contents: JSON.stringify(sign(request)) } });
    const wrapper = JSON.parse(output.text), expected = crypto.createHmac("sha256", secret).update(wrapper.payload).digest("hex");
    if (wrapper.signature !== expected) throw new Error("invalid_response_signature");
    return JSON.parse(wrapper.payload);
  }
  function request(operation, data, overrides = {}) { return Object.assign({ api_version: "assaf-os.v1", operation, actor_source: "OS", owner_id: binding.owner_id, workspace_id: binding.workspace_id, workbook_id: binding.workbook_id, request_id: `transport-${String(uuid++).padStart(8, "0")}`, timestamp: Math.floor(Date.now() / 1000), data }, overrides); }
  function addRecord(entity, record) {
    const tab = Schema.tab(entity), values = tab.fields.map((field) => {
      let value = record[field.key];
      if (field.type === "money_ils" && value != null) value /= 100;
      return value == null ? "" : value;
    });
    const sheet = workbook.getSheetByName(tab.name); sheet.setRow(sheet.getLastRow() + 1, values); return sheet.getLastRow();
  }
  return { binding, secret, workbook, context, batches, scriptProperties, call, request, addRecord, resetMetrics: () => workbook.resetMetrics(), metrics: () => ({ ...workbook.metrics }), Schema, Runtime };
}

module.exports = { createHarness };
