"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Schema = require("../../ops/assaf-os/schema.js");
const Core = require("../../ops/assaf-os/core.js");

function base(overrides) {
  return Object.assign({
    coverage: { payments: "complete", time: "complete", expenses: "complete" },
    observed_at: "2026-10-12T09:00:00.000Z",
    clients: [{ person_id: "P_001", name: "דנה", status: "פעיל" }],
    works: [{ work_id: "W_001", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: 500000, currency: "ILS", status: "פעילה" }],
    payments: [], time_entries: [], expenses: [],
  }, overrides || {});
}

test("schema fixes Hebrew tab order, roles, and required enums", () => {
  assert.equal(Schema.tabs[0].name, "סיכום");
  assert.deepEqual(Schema.enums.path, ["לימוד", "אוטומציה"]);
  assert.deepEqual(Schema.enums.lead_status, ["חדש", "רלוונטי", "לא רלוונטי", "לא ענה", "הפך ללקוח"]);
  assert.deepEqual(Schema.enums.client_status, ["פעיל", "לא פעיל"]);
  assert.deepEqual(Schema.enums.task_status, ["לביצוע", "בתהליך", "ממתין", "בוצע", "בוטל"]);
  assert.deepEqual(Schema.tabs.filter((tab) => tab.kind === "input").map((tab) => tab.name), ["לידים", "לקוחות", "עבודות", "תקבולים", "שעות עבודה", "הוצאות והשקעות", "משימות", "הגדרות"]);
  assert.equal(Schema.tab("works").fields.find((field) => field.key === "agreed_amount_ils").role, "input");
  assert.equal(Schema.tab("clients").fields.find((field) => field.key === "person_id").stable, true);
  assert.equal(Schema.tab("payments").fields.find((field) => field.key === "original_payment_id").ref, "payments.payment_id");
});

test("client and work rollup columns are explicit calculated protected schema fields", () => {
  const clients = Schema.tab("clients");
  const works = Schema.tab("works");
  ["received_learning_ils", "received_automation_ils", "total_received_ils", "recorded_hours", "hourly_ils", "foreign_currency_flag"].forEach((key) => {
    const field = clients.fields.find((candidate) => candidate.key === key);
    assert.ok(field, `missing client field ${key}`);
    assert.equal(field.role, "calculated");
  });
  ["received_ils", "recorded_hours", "hourly_ils", "foreign_currency_flag"].forEach((key) => {
    const field = works.fields.find((candidate) => candidate.key === key);
    assert.ok(field, `missing work field ${key}`);
    assert.equal(field.role, "calculated");
  });
  assert.equal(works.fields.find((field) => field.key === "start_date").role, "input");
  assert.equal(works.fields.find((field) => field.key === "status").enum, "work_status");
  assert.ok(clients.fields.find((field) => field.key === "hourly_ils").label.includes("₪ בלבד"));
});

test("net cash subtracts refunds, excludes cancelled payments, and ignores agreed value", () => {
  const result = Core.snapshot(base({
    payments: [
      { payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 120000, currency: "ILS", kind: "התקבל" },
      { payment_id: "PAY_2", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-03", amount_agorot: 20000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_1" },
      { payment_id: "PAY_3", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-04", amount_agorot: 900000, currency: "ILS", kind: "בוטל" },
    ],
    time_entries: [{ time_id: "TIME_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", minutes: 120, status: "פעיל" }],
  }));
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].net_received_agorot, 100000);
  assert.equal(result.rows[0].total_hours, 2);
  assert.equal(result.rows[0].weighted_hourly_agorot, 50000);
});

test("inactive clients retain historical totals", () => {
  const result = Core.snapshot(base({
    clients: [{ person_id: "P_001", name: "דנה", status: "לא פעיל" }],
    payments: [{ payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-09-30", amount_agorot: 30000, currency: "ILS", kind: "התקבל" }],
    time_entries: [{ time_id: "TIME_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-09-30", minutes: 60, status: "פעיל" }],
  }));
  assert.equal(result.rows[0].net_received_agorot, 30000);
});

test("missing and unknown time never become zero hourly value", () => {
  const payment = { payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 10000, currency: "ILS", kind: "התקבל" };
  const missing = Core.snapshot(base({ payments: [payment] })).rows[0];
  assert.equal(missing.total_minutes, 0); assert.equal(missing.weighted_hourly_agorot, null); assert.equal(missing.missing_time, true);
  const unknown = Core.snapshot(base({ coverage: { payments: "complete", time: "unknown", expenses: "complete" }, payments: [payment] })).rows[0];
  assert.equal(unknown.total_minutes, null); assert.equal(unknown.weighted_hourly_agorot, null); assert.ok(unknown.reason_codes.includes("time_coverage_unknown"));
});

test("unknown payment coverage is null rather than zero", () => {
  const result = Core.snapshot(base({
    coverage: { payments: "unknown", time: "complete", expenses: "complete" },
    time_entries: [{ time_id: "TIME_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", minutes: 60, status: "פעיל" }],
  }));
  assert.equal(result.rows[0].net_received_agorot, null);
  assert.equal(result.rows[0].weighted_hourly_agorot, null);
});

test("currencies remain separate instead of being summed", () => {
  const result = Core.snapshot(base({
    works: [
      { work_id: "W_001", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" },
      { work_id: "W_002", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "USD", status: "פעילה" },
    ],
    payments: [
      { payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 10000, currency: "ILS", kind: "התקבל" },
      { payment_id: "PAY_2", person_id: "P_001", work_id: "W_002", path: "אוטומציה", date: "2026-10-03", amount_agorot: 10000, currency: "USD", kind: "התקבל" },
    ],
  }));
  assert.deepEqual(result.rows.map((row) => row.currency).sort(), ["ILS", "USD"]);
  assert.deepEqual(result.rows.map((row) => row.net_received_agorot), [10000, 10000]);
  assert.equal(result.client_rollups[0].total_received_ils_agorot, 10000);
  assert.equal(result.client_rollups[0].foreign_currency_present, true);
});

test("shared actual costs remain unallocated and ad usage is not cash spend", () => {
  const result = Core.snapshot(base({ expenses: [
    { expense_id: "EXP_1", date: "2026-10-01", path: "משותף", amount_agorot: 40000, currency: "ILS", payment_status: "שולם", source_type: "תשלום בפועל", source_key: "invoice-1" },
    { expense_id: "EXP_2", date: "2026-10-02", path: "אוטומציה", amount_agorot: 90000, currency: "ILS", payment_status: "שולם", source_type: "דיווח שימוש בפלטפורמה", source_key: "google-usage-1" },
    { expense_id: "EXP_3", date: "2026-10-03", path: "לימוד", amount_agorot: 7000, currency: "ILS", payment_status: "בוטל", source_type: "תשלום בפועל", source_key: "cancelled-1" },
  ] }));
  assert.deepEqual(result.costs.shared_unallocated_agorot, { ILS: 40000 });
  assert.deepEqual(result.costs.by_path_agorot, { "לימוד": {}, "אוטומציה": {} });
  assert.equal(result.costs.excluded_ad_usage_records, 1);
  assert.equal(result.costs.scope, "all_time");
});

test("work/path mismatches and fractional minutes fail validation", () => {
  assert.throws(() => Core.snapshot(base({ payments: [{ payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "לימוד", date: "2026-10-02", amount_agorot: 100, currency: "ILS", kind: "התקבל" }] })), /payment_work_mismatch/);
  assert.throws(() => Core.snapshot(base({ time_entries: [{ time_id: "TIME_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", minutes: 1.5, status: "פעיל" }] })), /invalid_minutes/);
});

test("promotion reuses person id, is idempotent, and rejects ambiguous identity", () => {
  const args = { request_id: "REQ_001", person_id: "P_001", lead: { lead_id: "LEAD_1", person_id: "P_001" }, existing_client: null, existing_receipt: null, identity_candidates: ["P_001"], committed_at: "2026-10-12T09:00:00Z" };
  const first = Core.decidePromotion(args);
  assert.equal(first.kind, "commit"); assert.equal(first.client.person_id, "P_001"); assert.equal(first.lead_patch.status, "הפך ללקוח");
  const replay = Core.decidePromotion(Object.assign({}, args, { existing_receipt: first.receipt }));
  assert.equal(replay.kind, "replay"); assert.deepEqual(replay.receipt, first.receipt);
  const ambiguous = Core.decidePromotion(Object.assign({}, args, { identity_candidates: ["P_001", "P_999"] }));
  assert.deepEqual(ambiguous, { kind: "conflict", reason_code: "ambiguous_identity" });
});

test("request ids cannot be replayed for another promotion payload", () => {
  const first = Core.decidePromotion({ request_id: "REQ_001", person_id: "P_001", lead: { lead_id: "LEAD_1", person_id: "P_001" }, existing_client: null, existing_receipt: null, identity_candidates: [], committed_at: "2026-10-12T09:00:00Z" });
  const result = Core.decidePromotion({ request_id: "REQ_001", person_id: "P_001", lead: { lead_id: "LEAD_2", person_id: "P_001" }, existing_client: null, existing_receipt: first.receipt, identity_candidates: [], committed_at: "2026-10-12T09:01:00Z" });
  assert.deepEqual(result, { kind: "conflict", reason_code: "request_id_reused" });
});

test("installer defaults to preflight while declaring the explicit installable edit trigger", () => {
  const source = fs.readFileSync(path.join(__dirname, "../../ops/assaf-os/UnifiedSheets.gs"), "utf8");
  let inserts = 0;
  const workbook = {
    getSheets: () => [{ getName: () => "Sheet1", getLastRow: () => 0, getLastColumn: () => 0 }],
    insertSheet: () => { inserts += 1; throw new Error("must not insert during preflight"); },
  };
  const sandbox = {
    AssafOsSchema: Schema,
    SpreadsheetApp: { openById: (id) => { assert.equal(id, "12345678901234567890"); return workbook; } },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
  };
  vm.runInNewContext(source, sandbox, { filename: "UnifiedSheets.gs" });
  const result = sandbox.setupUnifiedWorkbook("12345678901234567890", Schema.installConfirmation);
  assert.equal(result.mode, "DRY_RUN");
  assert.equal(result.ready, true);
  assert.equal(inserts, 0);
  assert.equal(source.includes("function doPost"), false);
  assert.equal(source.includes("newTrigger(\"reconcileAssafOsEdit\")"), true);
});

test("preflight diagnoses exact-schema empty partial-install tabs without overwriting them", () => {
  const source = fs.readFileSync(path.join(__dirname, "../../ops/assaf-os/UnifiedSheets.gs"), "utf8");
  const tab = Schema.tab("summary"), headers = tab.fields.map((field) => field.label);
  const sheet = { getName: () => tab.name, getLastRow: () => 1, getLastColumn: () => headers.length, getRange: () => ({ getValues: () => [headers] }) };
  const workbook = { getSheets: () => [sheet], getSheetByName: (name) => name === tab.name ? sheet : null };
  const sandbox = { AssafOsSchema: Schema, ASSAF_OS_META: {}, SpreadsheetApp: { openById: () => workbook }, LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock() {} }) } };
  vm.runInNewContext(source, sandbox, { filename: "UnifiedSheets.gs" });
  const result = sandbox.preflightUnifiedWorkbook("12345678901234567890", Schema.installConfirmation);
  assert.equal(result.ready, false);
  assert.equal(result.partial_installation, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.partial_installation_diagnostics)), [{ name: tab.name, exact_headers: true, data_rows: 0, recoverable_empty_created_tab: true }]);
  assert.match(result.recovery_instruction, /never deletes or overwrites/);
  assert.match(source, /function preflightConfiguredUnifiedWorkbook\(\)/);
  assert.match(source, /function installConfiguredUnifiedWorkbook\(\)/);
  assert.match(source, /mode !== "TEST" && mode !== "LIVE"/);
});

test("duplicate external expense source keys fail closed", () => {
  const expense = { expense_id: "EXP_1", date: "2026-10-01", path: "משותף", amount_agorot: 100, currency: "ILS", payment_status: "שולם", source_type: "תשלום בפועל", source_key: "invoice-1" };
  assert.throws(() => Core.snapshot(base({ expenses: [expense, Object.assign({}, expense, { expense_id: "EXP_2" })] })), /duplicate_expense_source_key/);
});

test("weighted hourly transport refuses unsafe integer overflow", () => {
  assert.throws(() => Core.snapshot(base({
    works: [{ work_id: "W_001", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" }],
    payments: [{ payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: Number.MAX_SAFE_INTEGER, currency: "ILS", kind: "התקבל" }],
    time_entries: [{ time_id: "TIME_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", minutes: 1, status: "פעיל" }],
  })), /weighted_hourly_overflow/);
});

test("installer uses strict integer validation, neutral currency format, and restricted protections", () => {
  const source = fs.readFileSync(path.join(__dirname, "../../ops/assaf-os/UnifiedSheets.gs"), "utf8");
  assert.match(source, /requireFormulaSatisfied\([^\n]+INT\(/);
  assert.equal(source.includes("₪#,##0.00"), false);
  assert.match(source, /removeEditors/);
  assert.match(source, /setDomainEdit\(false\)/);
  assert.match(source, /field\.role === "input"\) applyValidation_/);
  assert.match(source, /type === "id" \|\| type === "text"/);
  assert.match(source, /setNumberFormat\("@"\)/);
});

test("client total is weighted across paths and never averages path rates", () => {
  const result = Core.snapshot(base({
    works: [
      { work_id: "W_001", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: 500000, currency: "ILS", status: "פעילה" },
      { work_id: "W_002", person_id: "P_001", path: "לימוד", agreed_amount_agorot: 200000, currency: "ILS", status: "פעילה" },
    ],
    payments: [
      { payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 90000, currency: "ILS", kind: "התקבל" },
      { payment_id: "PAY_2", person_id: "P_001", work_id: "W_002", path: "לימוד", date: "2026-10-03", amount_agorot: 10000, currency: "ILS", kind: "התקבל" },
    ],
    time_entries: [
      { time_id: "TIME_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", minutes: 60, status: "פעיל" },
      { time_id: "TIME_2", person_id: "P_001", work_id: "W_002", path: "לימוד", date: "2026-10-03", minutes: 540, status: "פעיל" },
    ],
  }));
  const total = result.client_totals[0];
  assert.equal(total.received_learning_agorot, 10000);
  assert.equal(total.received_automation_agorot, 90000);
  assert.equal(total.total_received_agorot, 100000);
  assert.equal(total.total_minutes, 600);
  assert.equal(total.weighted_hourly_agorot, 10000); // 1000 ILS / 10h, not average of 900 and ~11.11 ILS/h.
  assert.equal(result.client_rollups[0].weighted_hourly_ils_agorot, 10000);
  assert.equal(result.work_rollups.length, 2);
});

test("client cross-path hourly remains null when a paid path has no recorded time", () => {
  const result = Core.snapshot(base({
    works: [
      { work_id: "W_001", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" },
      { work_id: "W_002", person_id: "P_001", path: "לימוד", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" },
    ],
    payments: [
      { payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 90000, currency: "ILS", kind: "התקבל" },
      { payment_id: "PAY_2", person_id: "P_001", work_id: "W_002", path: "לימוד", date: "2026-10-03", amount_agorot: 10000, currency: "ILS", kind: "התקבל" },
    ],
    time_entries: [{ time_id: "TIME_2", person_id: "P_001", work_id: "W_002", path: "לימוד", date: "2026-10-03", minutes: 60, status: "פעיל" }],
  }));
  assert.equal(result.client_totals[0].missing_time, true);
  assert.equal(result.client_totals[0].weighted_hourly_agorot, null);
});

test("time on another work in the same path cannot hide missing paid-work time", () => {
  const result = Core.snapshot(base({
    works: [
      { work_id: "W_001", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" },
      { work_id: "W_002", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" },
    ],
    payments: [{ payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 90000, currency: "ILS", kind: "התקבל" }],
    time_entries: [{ time_id: "TIME_2", person_id: "P_001", work_id: "W_002", path: "אוטומציה", date: "2026-10-03", minutes: 60, status: "פעיל" }],
  }));
  assert.equal(result.rows[0].missing_time, true);
  assert.equal(result.rows[0].weighted_hourly_agorot, null);
  assert.equal(result.client_rollups[0].weighted_hourly_ils_agorot, null);
});

test("work status is validated against the schema enum", () => {
  assert.throws(() => Core.snapshot(base({ works: [{ work_id: "W_001", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעיל" }] })), /invalid_work_status/);
  assert.throws(() => Core.snapshot(base({ works: [{ work_id: "W_001", person_id: "P_999", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" }] })), /orphan_work_client/);
});

test("foreign currency activity remains flagged when receipt and refund net to zero", () => {
  const result = Core.snapshot(base({
    works: [{ work_id: "W_USD", person_id: "P_001", path: "לימוד", agreed_amount_agorot: null, currency: "USD", status: "פעילה" }],
    payments: [
      { payment_id: "PAY_USD_1", person_id: "P_001", work_id: "W_USD", path: "לימוד", date: "2026-10-02", amount_agorot: 10000, currency: "USD", kind: "התקבל" },
      { payment_id: "PAY_USD_2", person_id: "P_001", work_id: "W_USD", path: "לימוד", date: "2026-10-03", amount_agorot: 10000, currency: "USD", kind: "החזר", original_payment_id: "PAY_USD_1" },
    ],
  }));
  assert.equal(result.client_rollups[0].foreign_currency_present, true);
  assert.equal(result.client_rollups[0].total_received_ils_agorot, 0);
});

test("offsetting works cannot erase monthly missing-time evidence", () => {
  const result = Core.snapshot(base({
    works: [
      { work_id: "W_001", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" },
      { work_id: "W_002", person_id: "P_001", path: "אוטומציה", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" },
    ],
    payments: [
      { payment_id: "PAY_0", person_id: "P_001", work_id: "W_002", path: "אוטומציה", date: "2026-09-20", amount_agorot: 10000, currency: "ILS", kind: "התקבל" },
      { payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 10000, currency: "ILS", kind: "התקבל" },
      { payment_id: "PAY_2", person_id: "P_001", work_id: "W_002", path: "אוטומציה", date: "2026-10-03", amount_agorot: 10000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_0" },
    ],
    time_entries: [{ time_id: "TIME_2", person_id: "P_001", work_id: "W_002", path: "אוטומציה", date: "2026-10-03", minutes: 60, status: "פעיל" }],
  }));
  const octoberRow = result.rows.find((row) => row.month === "2026-10");
  const octoberTotal = result.client_totals.find((row) => row.month === "2026-10");
  assert.equal(octoberRow.missing_time, true);
  assert.ok(octoberRow.reason_codes.includes("missing_time"));
  assert.equal(octoberTotal.missing_time, true);
  assert.equal(octoberTotal.weighted_hourly_agorot, null);
});

test("monthly foreign-currency presence remains unknown when payment coverage is unknown", () => {
  const result = Core.snapshot(base({
    coverage: { payments: "unknown", time: "complete", expenses: "complete" },
    time_entries: [{ time_id: "TIME_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-03", minutes: 60, status: "פעיל" }],
  }));
  assert.equal(result.client_totals[0].foreign_currency_present, null);
  assert.equal(result.client_rollups[0].foreign_currency_present, null);
});

test("refunds require a matching original receipt and cannot cumulatively exceed it", () => {
  const original = { payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-01", amount_agorot: 10000, currency: "ILS", kind: "התקבל" };
  const orphan = { payment_id: "REF_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 1000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_404" };
  assert.throws(() => Core.snapshot(base({ payments: [orphan] })), /refund_original_not_found/);
  const overOne = Object.assign({}, orphan, { original_payment_id: "PAY_1", amount_agorot: 6000 });
  const overTwo = Object.assign({}, overOne, { payment_id: "REF_2" });
  assert.throws(() => Core.snapshot(base({ payments: [original, overOne, overTwo] })), /refund_exceeds_original/);
  const otherWork = { work_id: "W_002", person_id: "P_001", path: "לימוד", agreed_amount_agorot: null, currency: "ILS", status: "פעילה" };
  const mismatch = Object.assign({}, orphan, { original_payment_id: "PAY_1", work_id: "W_002", path: "לימוד" });
  assert.throws(() => Core.snapshot(base({ works: [base().works[0], otherWork], payments: [original, mismatch] })), /refund_original_mismatch/);
});

test("monthly paid expenses and net cash use actual paid records only", () => {
  const result = Core.snapshot(base({
    payments: [{ payment_id: "PAY_1", person_id: "P_001", work_id: "W_001", path: "אוטומציה", date: "2026-10-02", amount_agorot: 10000, currency: "ILS", kind: "התקבל" }],
    expenses: [
      { expense_id: "EXP_1", date: "2026-10-03", path: "משותף", amount_agorot: 2500, currency: "ILS", payment_status: "שולם", source_type: "תשלום בפועל", source_key: "invoice-1" },
      { expense_id: "EXP_2", date: "2026-10-04", path: "משותף", amount_agorot: 9999, currency: "ILS", payment_status: "לא שולם", source_type: "תשלום בפועל", source_key: "invoice-2" },
      { expense_id: "EXP_3", date: "2026-10-05", path: "משותף", amount_agorot: 9999, currency: "ILS", payment_status: "שולם", source_type: "דיווח שימוש בפלטפורמה", source_key: "ads-1" },
    ],
  }));
  assert.deepEqual(result.monthly, [{ month: "2026-10", currency: "ILS", paid_income_agorot: 10000, paid_expenses_agorot: 2500, net_cash_agorot: 7500 }]);
});

