"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { createHarness } = require("./apps-script-harness.js");

const DEFAULT_SAMPLES = 12;
const GROWTH_SAMPLES = 10;
const BASE_GIT_HEAD = "bda48fa697a79b0df6b41f6ef8165f13b4deaec0";
const REPOSITORY_ROOT = path.resolve(__dirname, "../..");
const RUNTIME_SOURCE = path.join(REPOSITORY_ROOT, "ops/assaf-os/UnifiedRuntime.gs");
const DEFAULT_OUTPUT = path.resolve(__dirname, "../../../performance/backend/backend-profile-raw.json");
const STAGES = [
  "assafOsVerifyMetadata_",
  "assafOsReadSheetOnce_",
  "assafOsReadValuesOnce_",
  "assafOsReconcileAll_",
  "assafOsReconcileRow_",
  "assafOsLatestAccepted_",
  "assafOsLatestIssue_",
  "assafOsPropose_",
  "assafOsApprove_",
  "assafOsReject_",
  "assafOsGetProposal_",
  "assafOsGetReceipt_",
  "assafOsCanProject_",
  "assafOsProjectionRequests_",
  "assafOsReferenceCatalog_",
  "assafOsRefreshReferenceValidationsSafely_",
  "assafOsSummary_",
  "assafOsSignedResponse_",
];

function cpuMs(start) {
  const used = process.cpuUsage(start);
  return (used.user + used.system) / 1000;
}

function currentGitHead() {
  try {
    return childProcess.execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim() || null;
  } catch (_) {
    return null;
  }
}

function sha256File(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function measure(fn) {
  const startCpu = process.cpuUsage();
  const startWall = process.hrtime.bigint();
  const value = fn();
  return {
    value,
    cpu_ms: cpuMs(startCpu),
    wall_ms: Number(process.hrtime.bigint() - startWall) / 1e6,
  };
}

function blankCounters() {
  return {
    spreadsheet_open: 0,
    get_sheet_by_name: 0,
    lock_get: 0,
    lock_try: 0,
    lock_release: 0,
    properties_get_service: 0,
    property_get: 0,
    property_set: 0,
    property_delete: 0,
    batch_get_ranges: 0,
    batch_get_cells: 0,
    batch_update_requests: 0,
  };
}

function instrument(h) {
  let counters = blankCounters();
  let stages = {};

  function bump(key, amount = 1) { counters[key] += amount; }

  const originalOpen = h.context.SpreadsheetApp.openById;
  h.context.SpreadsheetApp.openById = function (...args) {
    bump("spreadsheet_open");
    return originalOpen.apply(this, args);
  };

  const originalGetSheet = h.workbook.getSheetByName.bind(h.workbook);
  h.workbook.getSheetByName = function (...args) {
    bump("get_sheet_by_name");
    return originalGetSheet(...args);
  };

  const originalLockFactory = h.context.LockService.getScriptLock;
  h.context.LockService.getScriptLock = function (...args) {
    bump("lock_get");
    const lock = originalLockFactory.apply(this, args);
    const tryLock = lock.tryLock.bind(lock);
    const releaseLock = lock.releaseLock.bind(lock);
    lock.tryLock = (...lockArgs) => { bump("lock_try"); return tryLock(...lockArgs); };
    lock.releaseLock = (...lockArgs) => { bump("lock_release"); return releaseLock(...lockArgs); };
    return lock;
  };

  const originalPropertiesFactory = h.context.PropertiesService.getScriptProperties;
  h.context.PropertiesService.getScriptProperties = function (...args) {
    bump("properties_get_service");
    const props = originalPropertiesFactory.apply(this, args);
    for (const [name, key] of [["getProperty", "property_get"], ["setProperty", "property_set"], ["deleteProperty", "property_delete"]]) {
      const original = props[name].bind(props);
      props[name] = (...propertyArgs) => { bump(key); return original(...propertyArgs); };
    }
    return props;
  };

  const originalBatchGet = h.context.Sheets.Spreadsheets.Values.batchGet;
  h.context.Sheets.Spreadsheets.Values.batchGet = function (id, body) {
    bump("batch_get_ranges", body.ranges.length);
    const response = originalBatchGet(id, body);
    bump("batch_get_cells", response.valueRanges.reduce((sum, item) => sum + (item.values || []).reduce((rowSum, row) => rowSum + row.length, 0), 0));
    return response;
  };

  const originalBatchUpdate = h.context.Sheets.Spreadsheets.batchUpdate;
  h.context.Sheets.Spreadsheets.batchUpdate = function (body, id) {
    bump("batch_update_requests", body.requests.length);
    return originalBatchUpdate(body, id);
  };

  for (const name of STAGES) {
    const original = h.context[name];
    if (typeof original !== "function") continue;
    h.context[name] = function (...args) {
      const started = process.cpuUsage();
      try { return original.apply(this, args); }
      finally {
        const entry = stages[name] || (stages[name] = { calls: 0, inclusive_cpu_ms: 0 });
        entry.calls += 1;
        entry.inclusive_cpu_ms += cpuMs(started);
      }
    };
  }

  return {
    reset() { counters = blankCounters(); stages = {}; h.resetMetrics(); },
    snapshot() {
      const metrics = h.metrics();
      const sheetReads = metrics.getValues + metrics.getFormulas + metrics.getLastRow + metrics.getLastColumn + metrics.getDataRange + metrics.batchGet;
      const serviceApiCalls = sheetReads + metrics.batchUpdate + metrics.setDataValidation
        + counters.spreadsheet_open + counters.get_sheet_by_name + counters.lock_get + counters.lock_try + counters.lock_release
        + counters.properties_get_service + counters.property_get + counters.property_set + counters.property_delete;
      return {
        harness: metrics,
        facade: { ...counters },
        totals: { sheet_read_calls: sheetReads, service_api_invocations: serviceApiCalls },
        stages: JSON.parse(JSON.stringify(stages)),
      };
    },
  };
}

function ids(index) {
  const suffix = String(index).padStart(4, "0");
  return { person: `PER_${suffix}`, lead: `LEA_${suffix}`, work: `WOR_${suffix}`, payment: `PAY_${suffix}`, time: `TIM_${suffix}`, expense: `EXP_${suffix}`, task: `TAS_${suffix}` };
}

function rowFixture(h, index) {
  const id = ids(index);
  const created = "2026-10-01T00:00:00.000Z";
  const date = `2026-10-${String(index % 28 + 1).padStart(2, "0")}`;
  const source = h.Schema.enums.source[0];
  const learning = h.Schema.enums.path[0];
  const automation = h.Schema.enums.path[1];
  return {
    leads: { lead_id: id.lead, person_id: `LEADPERSON_${String(index).padStart(4, "0")}`, name: `Lead ${index}`, phone: `050${String(1000000 + index).slice(-7)}`, path: learning, status: h.Schema.enums.lead_status[0], source_note: "synthetic-profile", created_at: created, updated_at: created, revision: 1, write_source: source, request_id: `EDIT_lead_${index}` },
    clients: { person_id: id.person, name: `Client ${index}`, status: h.Schema.enums.client_status[0], notes: "synthetic-profile", created_at: created, updated_at: created, revision: 1, write_source: source, request_id: `EDIT_client_${index}` },
    works: { work_id: id.work, person_id: id.person, title: `Work ${index}`, path: automation, status: h.Schema.enums.work_status[1], currency: "ILS", created_at: created, updated_at: created, revision: 1, write_source: source, request_id: `EDIT_work_${index}` },
    payments: { payment_id: id.payment, person_id: id.person, work_id: id.work, path: automation, date, actual_amount_ils: 10000 + index, currency: "ILS", kind: h.Schema.enums.payment_kind[0], created_at: created, revision: 1, write_source: source, request_id: `EDIT_payment_${index}` },
    time: { time_id: id.time, person_id: id.person, work_id: id.work, path: automation, date, minutes: 30 + index % 30, description: "synthetic-profile", status: h.Schema.enums.time_status[0], created_at: created, revision: 1, write_source: source, request_id: `EDIT_time_${index}` },
    expenses: { expense_id: id.expense, date, category: "profile", platform: h.Schema.tab("expenses").fields.find((field) => field.key === "platform").values[2], path: learning, vendor: `Vendor ${index}`, amount_ils: 2500 + index, currency: "ILS", payment_status: h.Schema.enums.payment_status[0], source_type: h.Schema.enums.expense_source[0], source_key: `profile-expense-${index}`, created_at: created, revision: 1, write_source: source, request_id: `EDIT_expense_${index}` },
    tasks: { task_id: id.task, title: `Task ${index}`, status: h.Schema.enums.task_status[0], notes: "synthetic-profile", created_at: created, updated_at: created, revision: 1, write_source: source, request_id: `EDIT_task_${index}` },
  };
}

function seedRows(h, count) {
  for (let index = 1; index <= count; index += 1) {
    const fixture = rowFixture(h, index);
    for (const entity of ["leads", "clients", "works", "payments", "time", "expenses", "tasks"]) h.addRecord(entity, fixture[entity]);
  }
}

function call(h, operation, data, requestId) {
  const request = h.request(operation, data, requestId ? { request_id: requestId } : {});
  const response = h.call(request);
  return { request, response };
}

function propose(h, entity, input, recordId = null, expectedRevision = 0, requestId = `profile-propose-${entity}`) {
  const result = call(h, "propose", { request_id: requestId, entity, record_id: recordId, expected_revision: expectedRevision, input }, requestId);
  assert.equal(result.response.ok, true);
  return result.response.data;
}

function approve(h, proposal) {
  return call(h, "approve", { proposal_id: proposal.proposal_id, request_id: proposal.approval_request_id }, proposal.approval_request_id);
}

function reject(h, proposal) {
  return call(h, "reject", { proposal_id: proposal.proposal_id, request_id: proposal.rejection_request_id }, proposal.rejection_request_id);
}

function taskInput(h, suffix = "create") {
  return { title: `Profile task ${suffix}`, status: h.Schema.enums.task_status[0], notes: "synthetic-profile" };
}

function refundInput(h, suffix = "refund") {
  const id = ids(1);
  return { person_id: id.person, work_id: id.work, path: h.Schema.enums.path[1], date: "2026-10-20", actual_amount_ils: 1000, currency: "ILS", kind: h.Schema.enums.payment_kind[1], original_payment_id: id.payment, notes: `synthetic-${suffix}` };
}

function setupStable(scale = 1, expectResponseTooLarge = false) {
  const h = createHarness({ withSheets: true, maxRows: 2001 });
  const observer = instrument(h);
  seedRows(h, scale);
  const warm = call(h, "snapshot", {});
  if (expectResponseTooLarge) {
    assert.equal(warm.response.ok, false);
    assert.equal(warm.response.data.error.code, "response_too_large");
  } else {
    assert.equal(warm.response.ok, true, JSON.stringify(warm.response.data));
  }
  return { h, observer };
}

function setupEmpty() {
  const h = createHarness({ withSheets: true, maxRows: 2001 });
  return { h, observer: instrument(h) };
}

function responseBytes(value) {
  if (value === undefined) return 0;
  return Buffer.byteLength(JSON.stringify(value), "utf8");
}

function runSample(family, sampleNumber) {
  const setup = measure(() => family.setup(sampleNumber));
  const { h, observer } = setup.value;
  observer.reset();
  const operation = measure(() => family.run(h, sampleNumber));
  if (family.verify) family.verify(h, operation.value, sampleNumber);
  return {
    sample: sampleNumber,
    setup_cpu_ms: setup.cpu_ms,
    setup_wall_ms: setup.wall_ms,
    operation_cpu_ms: operation.cpu_ms,
    operation_wall_ms: operation.wall_ms,
    response_bytes: responseBytes(operation.value),
    metrics: observer.snapshot(),
  };
}

function percentile(values, fraction) {
  const sorted = values.slice().sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
}

function distribution(values) {
  return { p50: percentile(values, 0.5), p95: percentile(values, 0.95), max: Math.max(...values) };
}

function exactOrDistribution(values) {
  return values.every((value) => value === values[0]) ? values[0] : distribution(values);
}

function summarize(name, fixture, samples) {
  const serviceKeys = Object.keys(samples[0].metrics.harness).map((key) => ["harness", key])
    .concat(Object.keys(samples[0].metrics.facade).map((key) => ["facade", key]));
  const services = Object.fromEntries(serviceKeys.map(([group, key]) => [key, exactOrDistribution(samples.map((sample) => sample.metrics[group][key]))]));
  const stageNames = [...new Set(samples.flatMap((sample) => Object.keys(sample.metrics.stages)))].sort();
  const stages = Object.fromEntries(stageNames.map((stage) => [stage, {
    calls: exactOrDistribution(samples.map((sample) => sample.metrics.stages[stage] ? sample.metrics.stages[stage].calls : 0)),
    inclusive_cpu_ms: distribution(samples.map((sample) => sample.metrics.stages[stage] ? sample.metrics.stages[stage].inclusive_cpu_ms : 0)),
  }]));
  return {
    name,
    fixture,
    independent_samples: samples.length,
    setup_cpu_ms: distribution(samples.map((sample) => sample.setup_cpu_ms)),
    operation_cpu_ms: distribution(samples.map((sample) => sample.operation_cpu_ms)),
    operation_wall_ms: distribution(samples.map((sample) => sample.operation_wall_ms)),
    response_bytes: distribution(samples.map((sample) => sample.response_bytes)),
    service_api_invocations: exactOrDistribution(samples.map((sample) => sample.metrics.totals.service_api_invocations)),
    sheet_read_calls: exactOrDistribution(samples.map((sample) => sample.metrics.totals.sheet_read_calls)),
    services,
    stages,
  };
}

function families() {
  const ordinary = [
    { name: "schema", fixture: "LOCAL synthetic empty workbook; actual UnifiedRuntime.gs", setup: setupEmpty, run: (h) => call(h, "schema", {}).response, verify: (_h, r) => assert.equal(r.ok, true) },
    { name: "snapshot_stable", fixture: "LOCAL synthetic representative seven-entity workbook; initial reconcile excluded", setup: () => setupStable(1), run: (h) => call(h, "snapshot", {}).response, verify: (_h, r) => { assert.equal(r.ok, true); assert.equal(r.data.coverage, "complete", JSON.stringify(r.data.issues)); } },
    { name: "snapshot_reconcile_two_new_expenses", fixture: "LOCAL synthetic stable workbook plus exactly two unaccepted expense rows; models unverified real symptom without asserting cause", setup: () => { const state = setupStable(1); const fixture = rowFixture(state.h, 2).expenses; state.h.addRecord("expenses", { ...fixture, expense_id: null, revision: null, write_source: null, request_id: null, created_at: null }); const fixture3 = rowFixture(state.h, 3).expenses; state.h.addRecord("expenses", { ...fixture3, expense_id: null, revision: null, write_source: null, request_id: null, created_at: null }); return state; }, run: (h) => call(h, "snapshot", {}).response, verify: (_h, r) => { assert.equal(r.ok, true); assert.equal(r.data.records.expenses.length, 3); assert.equal(r.data.summary.monthly[0].paid_expenses_ils, 7506); } },
    { name: "native_edit_changed_expense", fixture: "LOCAL synthetic stable workbook; direct installable-edit handler on one valid input cell", setup: () => { const state = setupStable(1); const tab = state.h.Schema.tab("expenses"), sheet = state.h.workbook.getSheetByName(tab.name), column = tab.fields.findIndex((field) => field.key === "vendor") + 1; sheet.setCell(2, column, "Changed vendor"); state.range = sheet.getRange(2, column); return state; }, run: (h) => { h.context.reconcileAssafOsEdit({ range: h.workbook.getSheetByName(h.Schema.tab("expenses").name).getRange(2, h.Schema.tab("expenses").fields.findIndex((field) => field.key === "vendor") + 1) }); return { status: "completed" }; }, verify: (h) => { const tab = h.Schema.tab("expenses"), revision = h.workbook.getSheetByName(tab.name).value(2, tab.fields.findIndex((field) => field.key === "revision") + 1); assert.equal(revision, 2); } },
    { name: "propose_task_create", fixture: "LOCAL synthetic stable workbook; simple create proposal", setup: () => setupStable(1), run: (h, n) => propose(h, "tasks", taskInput(h, n), null, 0, `profile-task-propose-${n}`), verify: (_h, r) => assert.equal(r.status, "pending") },
    { name: "propose_refund_create", fixture: "LOCAL synthetic stable workbook; dependency and refund-graph proposal", setup: () => setupStable(1), run: (h, n) => propose(h, "payments", refundInput(h, n), null, 0, `profile-refund-propose-${n}`), verify: (_h, r) => assert.equal(r.status, "pending") },
    { name: "propose_expense_edit", fixture: "LOCAL synthetic stable workbook; duplicate-source guarded edit proposal", setup: () => setupStable(1), run: (h, n) => propose(h, "expenses", { notes: `edited-${n}` }, ids(1).expense, 1, `profile-expense-propose-${n}`), verify: (_h, r) => assert.equal(r.status, "pending") },
    { name: "propose_promote_lead", fixture: "LOCAL synthetic stable workbook; lead promotion proposal", setup: () => setupStable(1), run: (h, n) => propose(h, "promote_lead", {}, ids(1).lead, 1, `profile-promote-propose-${n}`), verify: (_h, r) => assert.equal(r.status, "pending") },
    { name: "approve_task_create", fixture: "LOCAL synthetic stable workbook; proposal setup excluded", setup: (n) => { const state = setupStable(1); state.proposal = propose(state.h, "tasks", taskInput(state.h, n), null, 0, `profile-task-approve-setup-${n}`); return state; }, run: (h, _n, state) => approve(h, state.proposal), verify: (_h, r) => { assert.equal(r.response.ok, true); assert.equal(r.response.data.status, "committed"); } },
    { name: "approve_refund_create", fixture: "LOCAL synthetic stable workbook; dependency/refund proposal setup excluded", setup: (n) => { const state = setupStable(1); state.proposal = propose(state.h, "payments", refundInput(state.h, n), null, 0, `profile-refund-approve-setup-${n}`); return state; }, run: (h, _n, state) => approve(h, state.proposal), verify: (_h, r) => { assert.equal(r.response.ok, true); assert.equal(r.response.data.status, "committed"); } },
    { name: "approve_expense_edit", fixture: "LOCAL synthetic stable workbook; duplicate-source guarded proposal setup excluded", setup: (n) => { const state = setupStable(1); state.proposal = propose(state.h, "expenses", { notes: `approved-${n}` }, ids(1).expense, 1, `profile-expense-approve-setup-${n}`); return state; }, run: (h, _n, state) => approve(h, state.proposal), verify: (_h, r) => { assert.equal(r.response.ok, true); assert.equal(r.response.data.status, "committed"); } },
    { name: "approve_promote_lead", fixture: "LOCAL synthetic stable workbook; promotion proposal setup excluded", setup: (n) => { const state = setupStable(1); state.proposal = propose(state.h, "promote_lead", {}, ids(1).lead, 1, `profile-promote-approve-setup-${n}`); return state; }, run: (h, _n, state) => approve(h, state.proposal), verify: (_h, r) => { assert.equal(r.response.ok, true); assert.equal(r.response.data.status, "committed"); } },
    { name: "proposal_lookup", fixture: "LOCAL synthetic stable workbook; pending proposal setup excluded", setup: (n) => { const state = setupStable(1); state.proposal = propose(state.h, "tasks", taskInput(state.h, n), null, 0, `profile-proposal-lookup-setup-${n}`); return state; }, run: (h, n, state) => call(h, "proposal", { proposal_id: state.proposal.proposal_id }, `profile-proposal-lookup-${n}`).response, verify: (_h, r) => { assert.equal(r.ok, true); assert.equal(r.data.status, "pending"); } },
    { name: "reject_task", fixture: "LOCAL synthetic stable workbook; pending proposal setup excluded", setup: (n) => { const state = setupStable(1); state.proposal = propose(state.h, "tasks", taskInput(state.h, n), null, 0, `profile-reject-setup-${n}`); return state; }, run: (h, _n, state) => reject(h, state.proposal), verify: (_h, r) => { assert.equal(r.response.ok, true); assert.equal(r.response.data.status, "rejected"); } },
    { name: "receipt_committed", fixture: "LOCAL synthetic stable workbook; propose+approve setup excluded", setup: (n) => { const state = setupStable(1); state.proposal = propose(state.h, "tasks", taskInput(state.h, n), null, 0, `profile-receipt-setup-${n}`); state.receipt = approve(state.h, state.proposal).response.data; return state; }, run: (h, n, state) => call(h, "receipt", { request_id: state.receipt.request_id }, `profile-receipt-read-${n}`).response, verify: (_h, r) => { assert.equal(r.ok, true); assert.equal(r.data.status, "committed"); } },
    { name: "receipt_unknown", fixture: "LOCAL synthetic stable workbook; nonexistent decision id", setup: () => setupStable(1), run: (h, n) => call(h, "receipt", { request_id: `missing-decision-${n}` }, `profile-missing-receipt-read-${n}`).response, verify: (_h, r) => { assert.equal(r.ok, true); assert.equal(r.data.status, "unknown"); } },
    { name: "safety_invalid_native", fixture: "LOCAL synthetic stable workbook; invalid task enum through edit handler", setup: () => { const state = setupStable(1); const tab = state.h.Schema.tab("tasks"), sheet = state.h.workbook.getSheetByName(tab.name), column = tab.fields.findIndex((field) => field.key === "status") + 1; sheet.setCell(2, column, "INVALID"); return state; }, run: (h) => { const tab = h.Schema.tab("tasks"), sheet = h.workbook.getSheetByName(tab.name), column = tab.fields.findIndex((field) => field.key === "status") + 1; h.context.reconcileAssafOsEdit({ range: sheet.getRange(2, column) }); return { status: "issue_recorded" }; }, verify: (h) => { const rows = h.workbook.getSheetByName("_assaf_os_issues").rows; assert.ok(rows.slice(1).some((row) => row[3] === "tasks" && row[6] === "open"), JSON.stringify(rows)); } },
    { name: "safety_header_drift", fixture: "LOCAL synthetic stable workbook; business header altered before snapshot", setup: () => { const state = setupStable(1); state.h.workbook.getSheetByName(state.h.Schema.tab("expenses").name).setCell(1, 1, "DRIFT"); return state; }, run: (h) => call(h, "snapshot", {}).response, verify: (_h, r) => { assert.equal(r.ok, true); assert.equal(r.data.entity_coverage.expenses, "unknown"); } },
    { name: "safety_formula_drift", fixture: "LOCAL synthetic stable workbook; one formula in an input row before snapshot", setup: () => { const state = setupStable(1); const tab = state.h.Schema.tab("expenses"), sheet = state.h.workbook.getSheetByName(tab.name), values = sheet.rows[1].slice(), formulas = values.map(() => ""); formulas[tab.fields.findIndex((field) => field.key === "vendor")] = "=1+1"; sheet.setRow(2, values, formulas); return state; }, run: (h) => call(h, "snapshot", {}).response, verify: (_h, r) => { assert.equal(r.ok, true); assert.ok(r.data.issues.some((issue) => issue.code === "formula_drift" && issue.entity === "expenses")); } },
    { name: "safety_duplicate_expense_source", fixture: "LOCAL synthetic stable workbook; second expense repeats source tuple", setup: () => { const state = setupStable(1); const duplicate = { ...rowFixture(state.h, 2).expenses, expense_id: null, source_key: rowFixture(state.h, 1).expenses.source_key, revision: null, write_source: null, request_id: null, created_at: null }; state.h.addRecord("expenses", duplicate); return state; }, run: (h) => call(h, "snapshot", {}).response, verify: (_h, r) => { assert.equal(r.ok, true); assert.ok(r.data.issues.some((issue) => issue.entity === "expenses"), JSON.stringify(r.data.issues)); } },
  ];
  return ordinary.concat([1, 10, 50, 100].map((size) => ({
    name: `snapshot_growth_${size}_rows_per_entity`,
    fixture: `LOCAL synthetic stable workbook; ${size} rows in each of seven entities (${size * 7} business rows); initial reconcile excluded${size === 100 ? "; snapshot is expected to fail closed at the signed response-size guard" : ""}`,
    samples: GROWTH_SAMPLES,
    setup: () => setupStable(size, size === 100),
    run: (h) => call(h, "snapshot", {}).response,
    verify: (_h, r) => {
      if (size === 100) { assert.equal(r.ok, false); assert.equal(r.data.error.code, "response_too_large"); }
      else { assert.equal(r.ok, true); assert.equal(r.data.coverage, "complete"); }
    },
  })));
}

function main() {
  const outputArg = process.argv.find((arg) => arg.startsWith("--output="));
  const output = outputArg ? path.resolve(outputArg.slice("--output=".length)) : DEFAULT_OUTPUT;
  const reports = [];
  const rawFamilies = [];
  for (const family of families()) {
    const count = family.samples || DEFAULT_SAMPLES;
    const samples = [];
    for (let sample = 1; sample <= count; sample += 1) {
      const setup = measure(() => family.setup(sample));
      const state = setup.value;
      state.observer.reset();
      const operation = measure(() => family.run(state.h, sample, state));
      if (family.verify) family.verify(state.h, operation.value, sample, state);
      samples.push({
        sample,
        setup_cpu_ms: setup.cpu_ms,
        setup_wall_ms: setup.wall_ms,
        operation_cpu_ms: operation.cpu_ms,
        operation_wall_ms: operation.wall_ms,
        response_bytes: responseBytes(operation.value),
        metrics: state.observer.snapshot(),
      });
    }
    const summary = summarize(family.name, family.fixture, samples);
    reports.push(summary);
    rawFamilies.push({ ...summary, samples });
    process.stdout.write(`${family.name}: n=${samples.length} cpu p50=${summary.operation_cpu_ms.p50.toFixed(3)}ms p95=${summary.operation_cpu_ms.p95.toFixed(3)}ms service=${JSON.stringify(summary.service_api_invocations)}\n`);
  }
  const report = {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    base_git_head: BASE_GIT_HEAD,
    git_head: currentGitHead(),
    measured_source: {
      path: path.relative(REPOSITORY_ROOT, RUNTIME_SOURCE).replaceAll("\\", "/"),
      sha256: sha256File(RUNTIME_SOURCE),
    },
    evidence_scope: "LOCAL synthetic Node VM executing actual ops/assaf-os/UnifiedRuntime.gs through tests/assaf-os/apps-script-harness.js; no mocked delay, network, Apps Script production timing, credentials, or live calls",
    timing_note: "CPU and wall times describe local Node execution only. Service counts are harness/API-facade invocations and do not claim provider network round trips.",
    setup_note: "Every sample creates an independent VM and workbook. setup_* includes seed/reconcile preparation and is excluded from operation_* distributions.",
    summaries: reports,
    families: rawFamilies,
  };
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`wrote ${output}\n`);
}

if (require.main === module) main();
