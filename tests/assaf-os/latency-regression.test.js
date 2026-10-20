"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createHarness } = require("./apps-script-harness.js");

const CURRENT_SOURCE = fs.readFileSync("ops/assaf-os/UnifiedRuntime.gs", "utf8");
const LEGACY_METADATA_PRELOAD = `function assafOsVerifyMetadata_(workbook) {
  Object.keys(ASSAF_OS_META).forEach(function (key) {
    var spec = ASSAF_OS_META[key], sheet = workbook.getSheetByName(spec.name);
    if (!sheet) throw assafOsError_("runtime_not_installed");
    var lastRow = sheet.getLastRow();
    if (lastRow > ASSAF_OS_MAX_ROWS + 1) throw assafOsError_("metadata_capacity_exceeded");
    var rows = sheet.getRange(1, 1, Math.max(lastRow, 1), spec.headers.length).getValues();
    var headers = rows[0] || [];
    if (spec.headers.some(function (value, index) { return headers[index] !== value; })) throw assafOsError_("metadata_header_drift");
    var cache = ASSAF_OS_REQUEST_READ_CACHE, cacheKey = String(sheet.getSheetId());
    if (cache && cache.workbook_id === workbook.getId()) cache.values[cacheKey] = rows;
  });
}
`;
const METADATA_PRELOAD_PATTERN = /function assafOsVerifyMetadata_\(workbook\) \{[\s\S]*?(?=function assafOsReferenceCatalog_)/;
assert.equal(CURRENT_SOURCE.split("function assafOsVerifyMetadata_(workbook)").length - 1, 1, "current metadata preload function must appear exactly once");
assert.match(CURRENT_SOURCE, METADATA_PRELOAD_PATTERN, "current metadata preload function must be replaceable");
const BASELINE_SOURCE = CURRENT_SOURCE.replace(METADATA_PRELOAD_PATTERN, `${LEGACY_METADATA_PRELOAD}\n`);
const RESULTS_PATH = path.resolve(".cache/os-latency-audit-results.json");
const ISO_TIME = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z/g;
const SHA256 = /[0-9a-f]{64}/g;

function client() {
  return { person_id: "PER_1", name: "דנה", status: "פעיל", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_client1" };
}

function lead() {
  return { lead_id: "LEA_1", person_id: "PER_2", name: "רון", phone: "0501234567", path: "לימוד", status: "רלוונטי", source_note: "synthetic", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_lead1" };
}

function work() {
  return { work_id: "WOR_1", person_id: "PER_1", title: "אתר", path: "אוטומציה", status: "פעילה", currency: "ILS", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_work1" };
}

function payment() {
  return { payment_id: "PAY_1", person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-01", actual_amount_ils: 10000, currency: "ILS", kind: "התקבל", created_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_payment1" };
}

function seedDependencies(h) {
  h.addRecord("clients", client());
  h.addRecord("works", work());
  h.addRecord("payments", payment());
}

const CASES = [
  { entity: "leads", create: { name: "Synthetic lead", phone: "0507654321", path: "לימוד", status: "חדש", source_note: "synthetic" }, edit: { status: "לא ענה" }, seed(h) { h.addRecord("leads", lead()); }, id: "LEA_1" },
  { entity: "clients", create: { name: "Synthetic client", status: "פעיל", notes: "synthetic" }, edit: { notes: "updated" }, seed(h) { h.addRecord("clients", client()); }, id: "PER_1" },
  { entity: "works", create: { person_id: "PER_1", title: "Synthetic work", path: "אוטומציה", status: "פעילה", currency: "ILS" }, edit: { title: "Updated work" }, seed(h) { h.addRecord("clients", client()); h.addRecord("works", work()); }, id: "WOR_1" },
  { entity: "payments", create: { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-02", actual_amount_ils: 5000, currency: "ILS", kind: "התקבל" }, edit: { notes: "updated" }, seed(h) { seedDependencies(h); }, id: "PAY_1" },
  { entity: "time", create: { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-02", minutes: 60, description: "synthetic", status: "פעיל" }, edit: { description: "updated" }, seed(h) { seedDependencies(h); h.addRecord("time", { time_id: "TIM_1", person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-01", minutes: 30, description: "seed", status: "פעיל", created_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_time1" }); }, id: "TIM_1" },
  { entity: "expenses", create: { date: "2026-10-02", category: "כלים", platform: "אחר", path: "משותף", vendor: "Synthetic", amount_ils: 2500, currency: "ILS", payment_status: "שולם", source_type: "תשלום בפועל", source_key: "expense-create" }, edit: { notes: "updated" }, seed(h) { h.addRecord("expenses", { expense_id: "EXP_1", date: "2026-10-01", category: "כלים", platform: "אחר", path: "משותף", vendor: "Seed", amount_ils: 1000, currency: "ILS", payment_status: "שולם", source_type: "תשלום בפועל", source_key: "expense-seed", created_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_expense1" }); }, id: "EXP_1" },
  { entity: "tasks", create: { title: "Synthetic task", status: "לביצוע", notes: "synthetic" }, edit: { status: "בתהליך" }, seed(h) { h.addRecord("tasks", { task_id: "TAS_1", title: "Seed task", status: "לביצוע", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_task1" }); }, id: "TAS_1" },
];

function normalize(value) {
  if (Array.isArray(value)) return Array.from(value, normalize);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, key === "timestamp" ? "<timestamp-seconds>" : normalize(item)]));
  if (typeof value === "string") return value.replace(ISO_TIME, "<timestamp>").replace(SHA256, "<sha256>");
  return value;
}

function workbookState(h) {
  const state = Object.fromEntries([...h.workbook.sheets.entries()].map(([name, sheet]) => [name, sheet.rows]));
  return JSON.parse(JSON.stringify(normalize(state)));
}

function firstDifference(left, right, trail = "root") {
  if (Object.is(left, right)) return null;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return { trail, left, right };
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  for (const key of keys) {
    const found = firstDifference(left[key], right[key], `${trail}.${key}`);
    if (found) return found;
  }
  return null;
}

function readCalls(metrics) {
  return metrics.getValues + metrics.getFormulas + metrics.getLastRow + metrics.getLastColumn + metrics.getDataRange + metrics.batchGet;
}

function propose(h, entity, input, recordId, expectedRevision, requestId) {
  return h.call(h.request("propose", { request_id: requestId, entity, record_id: recordId, expected_revision: expectedRevision, input }, { request_id: requestId })).data;
}

function approve(h, proposal) {
  return h.call(h.request("approve", { proposal_id: proposal.proposal_id, request_id: proposal.approval_request_id }, { request_id: proposal.approval_request_id }));
}

function runPair(name, setup, invoke) {
  const current = createHarness({ maxRows: 2001, runtimeSource: CURRENT_SOURCE, withSheets: true });
  const baseline = createHarness({ maxRows: 2001, runtimeSource: BASELINE_SOURCE, withSheets: true });
  setup(current); setup(baseline);
  current.resetMetrics(); baseline.resetMetrics();
  const currentResponse = invoke(current), baselineResponse = invoke(baseline);
  const currentMetrics = current.metrics(), baselineMetrics = baseline.metrics();
  assert.deepEqual(normalize(currentResponse), normalize(baselineResponse), `${name}: response changed`);
  const currentState = workbookState(current), baselineState = workbookState(baseline);
  assert.deepEqual(currentState, baselineState, `${name}: workbook effects changed at ${JSON.stringify(firstDifference(currentState, baselineState))}`);
  assert.equal(currentMetrics.batchGet, 1, `${name}: current runtime must batch metadata reads once`);
  assert.equal(baselineMetrics.batchGet, 0, `${name}: committed baseline must not use batchGet`);
  assert.equal(baselineMetrics.getValues - currentMetrics.getValues, 5, `${name}: five metadata getValues calls should be removed`);
  assert.equal(readCalls(baselineMetrics) - readCalls(currentMetrics), 4, `${name}: net read-call reduction should be four`);
  return {
    case: name,
    semantic_equal: true,
    current: { ...currentMetrics, service_read_calls: readCalls(currentMetrics) },
    baseline: { ...baselineMetrics, service_read_calls: readCalls(baselineMetrics) },
    service_read_call_reduction: readCalls(baselineMetrics) - readCalls(currentMetrics),
  };
}

test("current metadata batch read preserves all operation semantics and reduces service reads", () => {
  const matrix = [];
  matrix.push(runPair("schema", () => {}, (h) => h.call(h.request("schema", {}))));
  matrix.push(runPair("snapshot", (h) => seedDependencies(h), (h) => h.call(h.request("snapshot", {}))));

  for (const spec of CASES) {
    matrix.push(runPair(`propose:create:${spec.entity}`, (h) => { if (["works", "payments", "time"].includes(spec.entity)) seedDependencies(h); }, (h) => {
      const requestId = `create-${spec.entity}-request`;
      return h.call(h.request("propose", { request_id: requestId, entity: spec.entity, record_id: null, expected_revision: 0, input: spec.create }, { request_id: requestId }));
    }));
    matrix.push(runPair(`propose:edit:${spec.entity}`, spec.seed, (h) => {
      const requestId = `update-${spec.entity}-request`;
      return h.call(h.request("propose", { request_id: requestId, entity: spec.entity, record_id: spec.id, expected_revision: 1, input: spec.edit }, { request_id: requestId }));
    }));
  }

  matrix.push(runPair("proposal", () => {}, (h) => {
    const proposal = propose(h, "tasks", { title: "Lookup", status: "לביצוע" }, null, 0, "proposal-lookup-create");
    h.resetMetrics();
    return h.call(h.request("proposal", { proposal_id: proposal.proposal_id }, { request_id: "proposal-lookup-read" }));
  }));
  matrix.push(runPair("approve", () => {}, (h) => {
    const proposal = propose(h, "tasks", { title: "Approve", status: "לביצוע" }, null, 0, "approve-setup-request");
    h.resetMetrics();
    return approve(h, proposal);
  }));
  matrix.push(runPair("reject", () => {}, (h) => {
    const proposal = propose(h, "tasks", { title: "Reject", status: "לביצוע" }, null, 0, "reject-setup-request");
    h.resetMetrics();
    return h.call(h.request("reject", { proposal_id: proposal.proposal_id, request_id: proposal.rejection_request_id }, { request_id: proposal.rejection_request_id }));
  }));
  matrix.push(runPair("receipt", () => {}, (h) => {
    const proposal = propose(h, "tasks", { title: "Receipt", status: "לביצוע" }, null, 0, "receipt-setup-request");
    const receipt = approve(h, proposal);
    h.resetMetrics();
    return h.call(h.request("receipt", { request_id: receipt.data.request_id }, { request_id: "receipt-lookup-read" }));
  }));
  matrix.push(runPair("promote_lead:propose", (h) => h.addRecord("leads", lead()), (h) => {
    const requestId = "promote-lead-proposal";
    return h.call(h.request("propose", { request_id: requestId, entity: "promote_lead", record_id: "LEA_1", expected_revision: 1, input: {} }, { request_id: requestId }));
  }));
  matrix.push(runPair("promote_lead:approve", (h) => h.addRecord("leads", lead()), (h) => {
    const proposal = propose(h, "promote_lead", {}, "LEA_1", 1, "promote-lead-approve-setup");
    h.resetMetrics();
    return approve(h, proposal);
  }));
  matrix.push(runPair("refund:propose", seedDependencies, (h) => {
    const requestId = "refund-proposal-request";
    const input = { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-03", actual_amount_ils: 2000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_1", notes: "synthetic" };
    return h.call(h.request("propose", { request_id: requestId, entity: "payments", record_id: null, expected_revision: 0, input }, { request_id: requestId }));
  }));
  matrix.push(runPair("refund:approve", seedDependencies, (h) => {
    const input = { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-03", actual_amount_ils: 2000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_1", notes: "synthetic" };
    const proposal = propose(h, "payments", input, null, 0, "refund-approve-setup");
    h.resetMetrics();
    return approve(h, proposal);
  }));

  fs.mkdirSync(path.dirname(RESULTS_PATH), { recursive: true });
  fs.writeFileSync(RESULTS_PATH, `${JSON.stringify({
    evidence_scope: "LOCAL synthetic Apps Script harness; service-call counts, not elapsed production latency",
    baseline: "working-tree runtime with exact legacy assafOsVerifyMetadata_ fixture injected",
    current: "working tree ops/assaf-os/UnifiedRuntime.gs",
    generated_at: new Date().toISOString(),
    cases: matrix,
  }, null, 2)}\n`);
  assert.equal(matrix.length, 24);
});

function assertNoWrites(h, response, code) {
  assert.equal(response.ok, false);
  assert.equal(response.data.error.code, code);
  assert.equal(h.metrics().batchUpdate, 0);
  assert.equal(h.batches.length, 0);
}

test("batched metadata response fails closed before writes", async (t) => {
  await t.test("wrong workbook id in response", () => {
    const h = createHarness({ runtimeSource: CURRENT_SOURCE, withSheets: true });
    const original = h.context.Sheets.Spreadsheets.Values.batchGet;
    h.context.Sheets.Spreadsheets.Values.batchGet = (id, body) => ({ ...original(id, body), spreadsheetId: "wrong-workbook" });
    assertNoWrites(h, h.call(h.request("schema", {})), "metadata_read_invalid");
  });
  await t.test("missing range response", () => {
    const h = createHarness({ runtimeSource: CURRENT_SOURCE, withSheets: true });
    const original = h.context.Sheets.Spreadsheets.Values.batchGet;
    h.context.Sheets.Spreadsheets.Values.batchGet = (id, body) => { const response = original(id, body); response.valueRanges.pop(); return response; };
    assertNoWrites(h, h.call(h.request("schema", {})), "metadata_read_invalid");
  });
  await t.test("malformed range values", () => {
    const h = createHarness({ runtimeSource: CURRENT_SOURCE, withSheets: true });
    const original = h.context.Sheets.Spreadsheets.Values.batchGet;
    h.context.Sheets.Spreadsheets.Values.batchGet = (id, body) => { const response = original(id, body); response.valueRanges[0].values = "not-rows"; return response; };
    assertNoWrites(h, h.call(h.request("schema", {})), "metadata_read_invalid");
  });
  await t.test("metadata header drift", () => {
    const h = createHarness({ runtimeSource: CURRENT_SOURCE, withSheets: true });
    h.workbook.getSheetByName("_assaf_os_receipts").setCell(1, 1, "drifted");
    assertNoWrites(h, h.call(h.request("schema", {})), "metadata_header_drift");
  });
  await t.test("metadata capacity", () => {
    const h = createHarness({ runtimeSource: CURRENT_SOURCE, withSheets: true, maxRows: 2002 });
    h.workbook.getSheetByName("_assaf_os_accepted").setRow(2002, ["overflow"]);
    h.resetMetrics();
    assertNoWrites(h, h.call(h.request("schema", {})), "metadata_capacity_exceeded");
    assert.equal(h.metrics().batchGet, 0);
    assert.equal(h.metrics().getValues, 0);
  });
});
