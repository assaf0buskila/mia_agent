"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createHarness } = require("./apps-script-harness.js");

function client() { return { person_id: "PER_1", name: "דנה", status: "פעיל", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_client1" }; }
function work() { return { work_id: "WOR_1", person_id: "PER_1", title: "אתר", path: "אוטומציה", status: "פעילה", currency: "ILS", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_work1" }; }
function payment() { return { payment_id: "PAY_1", person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-01", actual_amount_ils: 10000, currency: "ILS", kind: "התקבל", created_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_payment1" }; }

function propose(h, entity, input, recordId = null, expectedRevision = 0, actor = "OS") {
  const requestId = `proposal-${entity}-${Math.random().toString(16).slice(2)}`;
  const response = h.call(h.request("propose", { request_id: requestId, entity, record_id: recordId, expected_revision: expectedRevision, input }, { request_id: requestId, actor_source: actor }));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  return response.data;
}
function approve(h, proposal, actor = "OS") {
  return h.call(h.request("approve", { proposal_id: proposal.proposal_id, request_id: proposal.approval_request_id }, { request_id: proposal.approval_request_id, actor_source: actor }));
}

test("actual Apps Script creates a work without treating its new ID as a dependency", () => {
  const h = createHarness(); h.addRecord("clients", client());
  const requestId = "proposal-work-idempotent", input = { person_id: "PER_1", title: "אוטומציה חדשה", path: "אוטומציה", status: "פעילה", currency: "ILS" };
  const firstRequest = h.request("propose", { request_id: requestId, entity: "works", record_id: null, expected_revision: 0, input }, { request_id: requestId });
  const proposal = h.call(firstRequest).data;
  const replay = h.call(Object.assign({}, firstRequest, { timestamp: firstRequest.timestamp + 1 })).data;
  assert.equal(replay.proposal_id, proposal.proposal_id);
  const result = approve(h, proposal);
  assert.equal(result.ok, true, JSON.stringify(result.data));
  assert.equal(result.data.status, "committed");
  const rows = h.workbook.getSheetByName("עבודות").getDataRange().getValues();
  assert.equal(rows[1][2], "אוטומציה חדשה");
});

test("dependent refund fingerprint prevents two pending refunds from over-refunding", () => {
  const h = createHarness(); h.addRecord("clients", client()); h.addRecord("works", work()); h.addRecord("payments", payment());
  const refund = (suffix) => ({ person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-02", actual_amount_ils: 6000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_1", notes: suffix });
  const first = propose(h, "payments", refund("א")), second = propose(h, "payments", refund("ב"));
  assert.equal(approve(h, first).data.status, "committed");
  const stale = approve(h, second);
  assert.equal(stale.ok, true);
  assert.equal(stale.data.status, "target_changed");
  assert.equal(h.workbook.getSheetByName("תקבולים").getDataRange().getValues().length, 3);
});

test("original payment updates validate the reciprocal refund graph", () => {
  const h = createHarness(); h.addRecord("clients", client()); h.addRecord("works", work()); h.addRecord("payments", payment());
  h.addRecord("payments", { payment_id: "PAY_2", person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-02", actual_amount_ils: 6000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_1", created_at: "2026-10-02T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_refund1" });
  const requestId = "proposal-original-too-small", request = h.request("propose", { request_id: requestId, entity: "payments", record_id: "PAY_1", expected_revision: 1, input: { actual_amount_ils: 2000 } }, { request_id: requestId });
  const response = h.call(request);
  assert.equal(response.ok, false);
  assert.equal(response.data.error.code, "refund_exceeds_original");
  const tab = h.Schema.tab("payments"), sheet = h.workbook.getSheetByName(tab.name), amountIndex = tab.fields.findIndex((field) => field.key === "actual_amount_ils");
  sheet.setCell(2, amountIndex + 1, 20);
  assert.equal(h.context.assafOsReconcileRow_(h.workbook, tab, 2, "test"), "issue");
  assert.equal(h.workbook.getSheetByName("_assaf_os_accepted").getLastRow(), 1);
});

test("promotion atomically journals both lead and client accepted snapshots", () => {
  const h = createHarness();
  h.addRecord("leads", { lead_id: "LEA_1", person_id: "PER_1", name: "דנה", path: "לימוד", status: "רלוונטי", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_lead1" });
  const proposal = propose(h, "promote_lead", {}, "LEA_1", 1);
  assert.equal(approve(h, proposal).data.status, "committed");
  const accepted = h.workbook.getSheetByName("_assaf_os_accepted").getDataRange().getValues().slice(1);
  assert.deepEqual(accepted.map((row) => row[0]).sort(), ["clients", "leads"]);
  assert.equal(h.workbook.getSheetByName("לקוחות").getDataRange().getValues()[1][0], "PER_1");
});

test("fresh receipt lookup recovers a lost approval and cross-channel retry commits once", () => {
  const h = createHarness(); h.addRecord("clients", client());
  const proposal = propose(h, "works", { person_id: "PER_1", title: "שחזור", path: "לימוד", status: "פעילה", currency: "ILS" }, null, 0, "MIA");
  const first = approve(h, proposal, "TELEGRAM_APPROVAL"); assert.equal(first.data.status, "committed");
  const repeated = approve(h, proposal, "OS"); assert.deepEqual(repeated.data, first.data);
  const lookup = h.call(h.request("receipt", { request_id: proposal.approval_request_id }));
  assert.equal(lookup.ok, true); assert.deepEqual(lookup.data, first.data);
  const unknown = h.call(h.request("receipt", { request_id: "missing-original-request" }));
  assert.equal(unknown.data.status, "unknown"); assert.deepEqual(unknown.data.reason_codes, ["receipt_not_found"]);
  assert.equal(h.workbook.getSheetByName("עבודות").getDataRange().getValues().length, 2);
  const mismatched = h.call(h.request("approve", { proposal_id: "PR_DIFFERENT", request_id: proposal.approval_request_id }, { request_id: proposal.approval_request_id, actor_source: "TELEGRAM_APPROVAL" }));
  assert.equal(mismatched.ok, false);
  assert.equal(mismatched.data.error.code, "request_id_reused");
});

test("native dates retain the Asia Jerusalem business date across UTC month boundaries", () => {
  const h = createHarness(), expenses = h.Schema.tab("expenses");
  assert.equal(h.context.assafOsRowToRecord_(expenses, expenses.fields.map((field) => field.key === "date" ? new Date("2026-10-01T00:00:00+03:00") : "")).date, "2026-10-01");
  assert.equal(h.context.assafOsRowToRecord_(expenses, expenses.fields.map((field) => field.key === "date" ? new Date("2026-10-12T00:00:00+03:00") : "")).date, "2026-10-12");
});

test("native Date is normalized, formula rows surface issues, and system reconciliation is field-limited", () => {
  const h = createHarness(), tab = h.Schema.tab("expenses"), sheet = h.workbook.getSheetByName(tab.name);
  const row = tab.fields.map(() => "");
  Object.assign(row, { [tab.fields.findIndex((f) => f.key === "date")]: new Date("2026-10-12T00:00:00.000Z"), [tab.fields.findIndex((f) => f.key === "category")]: "כלים", [tab.fields.findIndex((f) => f.key === "platform")]: "אחר", [tab.fields.findIndex((f) => f.key === "path")]: "משותף", [tab.fields.findIndex((f) => f.key === "amount_ils")]: 12.34, [tab.fields.findIndex((f) => f.key === "currency")]: "ILS", [tab.fields.findIndex((f) => f.key === "payment_status")]: "שולם", [tab.fields.findIndex((f) => f.key === "source_type")]: "תשלום בפועל", [tab.fields.findIndex((f) => f.key === "source_key")]: "invoice-native-1" });
  sheet.setRow(2, row);
  assert.equal(h.context.assafOsReconcileRow_(h.workbook, tab, 2, "test"), "accepted");
  const firstBatchUpdates = h.batches[0].filter((request) => request.updateCells).map((request) => request.updateCells.range.startColumnIndex);
  assert.ok(firstBatchUpdates.every((index) => tab.fields[index].role === "system"));
  assert.match(String(sheet.getDataRange().getValues()[1][0]), /^EXP_/);
  const formulaRow = row.slice(); formulaRow[tab.fields.findIndex((f) => f.key === "source_key")] = "invoice-native-2";
  const formulas = formulaRow.map(() => ""); formulas[tab.fields.findIndex((f) => f.key === "category")] = "=1+1";
  sheet.setRow(3, formulaRow, formulas);
  assert.equal(h.context.assafOsReconcileRow_(h.workbook, tab, 3, "test"), "issue");
  const issue = h.workbook.getSheetByName("_assaf_os_issues").getDataRange().getValues().at(-1);
  assert.equal(issue[2], "formula_drift");
});

test("header drift prevents reconciliation writes and yields partial/unknown coverage with monthly array", () => {
  const h = createHarness({ maxRows: 25 }), before = h.batches.length;
  h.workbook.getSheetByName("לידים").setCell(1, 1, "כותרת שגויה");
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  assert.equal(response.data.entity_coverage.leads, "unknown");
  assert.equal(response.data.coverage, "partial");
  assert.ok(Array.isArray(response.data.summary.monthly));
  assert.equal(h.batches.length, before);
});

test("first partial snapshot accepts an unrelated valid row without strict projection failure", () => {
  const h = createHarness({ maxRows: 25 });
  h.addRecord("clients", { name: "incomplete client" });
  h.addRecord("tasks", { title: "Valid new task", status: "לביצוע" });
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  assert.equal(response.data.coverage, "partial");
  assert.equal(response.data.entity_coverage.clients, "partial");
  assert.match(response.data.records.tasks[0].task_id, /^TAS_/);
  assert.ok(Array.isArray(response.data.summary.monthly));
});

test("native client work payment and time rows receive stable IDs in dependency order", () => {
  const h = createHarness({ maxRows: 25 });
  h.addRecord("clients", { name: "Native client", status: "פעיל" });
  let snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.ok, true, JSON.stringify(snapshot.data));
  const personId = snapshot.data.records.clients[0].person_id;
  assert.match(personId, /^PER_/);

  h.addRecord("works", { person_id: personId, title: "Native work", path: "לימוד", status: "פעילה", currency: "ILS" });
  snapshot = h.call(h.request("snapshot", {}));
  const workId = snapshot.data.records.works[0].work_id;
  assert.match(workId, /^WOR_/);

  h.addRecord("payments", { person_id: personId, work_id: workId, path: "לימוד", date: "2026-10-12", actual_amount_ils: 10000, currency: "ILS", kind: "התקבל" });
  snapshot = h.call(h.request("snapshot", {}));
  const paymentId = snapshot.data.records.payments[0].payment_id;
  assert.match(paymentId, /^PAY_/);

  h.addRecord("time", { person_id: personId, work_id: workId, path: "לימוד", date: "2026-10-12", minutes: 60, status: "פעיל" });
  snapshot = h.call(h.request("snapshot", {}));
  assert.match(snapshot.data.records.time[0].time_id, /^TIM_/);
  assert.equal(snapshot.data.summary.monthly[0].paid_income_ils, 10000);
  assert.equal(snapshot.data.summary.monthly[0].recorded_minutes, 60);
});

test("multiple fresh native payments and a following refund reconcile in one snapshot each", () => {
  const h = createHarness({ maxRows: 25 }); h.addRecord("clients", client()); h.addRecord("works", work());
  h.addRecord("payments", { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-12", actual_amount_ils: 10000, currency: "ILS", kind: "התקבל" });
  h.addRecord("payments", { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-12", actual_amount_ils: 20000, currency: "ILS", kind: "התקבל" });
  let snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.ok, true, JSON.stringify(snapshot.data));
  assert.equal(snapshot.data.records.payments.length, 2);
  assert.ok(snapshot.data.records.payments.every((row) => /^PAY_/.test(row.payment_id)));
  assert.equal(snapshot.data.entity_coverage.payments, "complete");
  const originalId = snapshot.data.records.payments[0].payment_id;

  h.addRecord("payments", { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-13", actual_amount_ils: 5000, currency: "ILS", kind: "החזר", original_payment_id: originalId });
  snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.ok, true, JSON.stringify(snapshot.data));
  assert.equal(snapshot.data.records.payments.length, 3);
  assert.equal(snapshot.data.entity_coverage.payments, "complete");
  assert.equal(snapshot.data.summary.monthly[0].paid_income_ils, 25000);
  assert.ok(!snapshot.data.issues.some((issue) => issue.entity === "payments"));
});

test("invalid fresh payment peer cannot block or renumber an accepted native payment", () => {
  const h = createHarness({ maxRows: 25 }); h.addRecord("clients", client()); h.addRecord("works", work());
  h.addRecord("payments", { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-12", actual_amount_ils: 10000, currency: "ILS", kind: "התקבל" });
  h.addRecord("payments", { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-13", actual_amount_ils: 5000, currency: "ILS", kind: "החזר" });
  let snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.ok, true, JSON.stringify(snapshot.data));
  assert.equal(snapshot.data.records.payments.length, 1);
  const acceptedId = snapshot.data.records.payments[0].payment_id;
  assert.match(acceptedId, /^PAY_/);
  assert.equal(snapshot.data.entity_coverage.payments, "partial");
  assert.ok(snapshot.data.issues.some((issue) => issue.entity === "payments"));
  snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.data.records.payments.length, 1);
  assert.equal(snapshot.data.records.payments[0].payment_id, acceptedId);
  assert.equal(h.workbook.getSheetByName("_assaf_os_accepted").getDataRange().getValues().filter((row) => row[0] === "payments" && row[1] === acceptedId).length, 1);
});

test("invalid native original payment edit cannot project unsafe financial totals", () => {
  const h = createHarness({ maxRows: 25 }); h.addRecord("clients", client()); h.addRecord("works", work()); h.addRecord("payments", payment());
  h.addRecord("payments", { payment_id: "PAY_2", person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-02", actual_amount_ils: 6000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_1", created_at: "2026-10-02T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_refund1" });
  let snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.data.records.works[0].received_ils, 4000);
  const before = h.batches.length, tab = h.Schema.tab("payments"), amountIndex = tab.fields.findIndex((field) => field.key === "actual_amount_ils");
  h.workbook.getSheetByName(tab.name).setCell(2, amountIndex + 1, 20);
  snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.ok, true, JSON.stringify(snapshot.data));
  assert.equal(snapshot.data.entity_coverage.payments, "partial");
  assert.equal(snapshot.data.entity_coverage.works, "partial");
  assert.equal(snapshot.data.entity_coverage.clients, "partial");
  assert.equal(snapshot.data.records.works[0].received_ils, null);
  assert.equal(snapshot.data.records.clients[0].total_received_ils, null);
  assert.equal(snapshot.data.summary.monthly[0].paid_income_ils, null);
  const projectionWrites = h.batches.slice(before).flat().filter((request) => request.repeatCell);
  assert.equal(projectionWrites.length, 0);
});

test("unrelated approval cannot project an unreconciled invalid native payment graph", () => {
  const h = createHarness({ maxRows: 25 }); h.addRecord("clients", client()); h.addRecord("works", work()); h.addRecord("payments", payment());
  h.addRecord("payments", { payment_id: "PAY_2", person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-02", actual_amount_ils: 6000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_1", created_at: "2026-10-02T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_refund1" });
  let snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.data.records.works[0].received_ils, 4000);
  const tab = h.Schema.tab("payments"), amountIndex = tab.fields.findIndex((field) => field.key === "actual_amount_ils");
  h.workbook.getSheetByName(tab.name).setCell(2, amountIndex + 1, 20);
  const proposal = propose(h, "tasks", { title: "Unrelated valid task", status: "לביצוע" });
  const beforeApproval = h.batches.length, approved = approve(h, proposal, "TELEGRAM_APPROVAL");
  assert.equal(approved.data.status, "committed");
  assert.equal(h.batches.slice(beforeApproval).flat().filter((request) => request.repeatCell).length, 0);
  snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.data.records.works[0].received_ils, null);
  assert.equal(snapshot.data.summary.monthly[0].paid_income_ils, null);
});

test("projection boundary canonical-validates unreconciled native input rows", () => {
  const h = createHarness({ maxRows: 25 }); h.addRecord("clients", client()); h.addRecord("works", work());
  h.addRecord("time", { time_id: "TIM_BAD", person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-12", minutes: -5, status: "פעיל", created_at: "2026-10-12T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_badtime" });
  const proposal = propose(h, "tasks", { title: "Another task", status: "לביצוע" });
  const beforeApproval = h.batches.length, approved = approve(h, proposal);
  assert.equal(approved.data.status, "committed");
  assert.equal(h.batches.slice(beforeApproval).flat().filter((request) => request.repeatCell).length, 0);
});

test("calculated sheet clear stays inside the real grid and transport money remains minor", () => {
  const h = createHarness({ maxRows: 25 }); h.addRecord("clients", client()); h.addRecord("works", work()); h.addRecord("payments", payment());
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  assert.equal(response.data.records.payments[0].actual_amount_ils, 10000);
  assert.equal(response.data.records.works[0].received_ils, 10000);
  assert.equal(response.data.records.clients[0].total_received_ils, 10000);
  assert.equal(response.data.summary.monthly[0].paid_income_ils, 10000);
  assert.equal(response.data.summary.monthly[0].currency, "ILS");
});

test("native monthly hourly stays null when one paid work has no time", () => {
  const h = createHarness({ maxRows: 25 }); h.addRecord("clients", client());
  h.addRecord("works", work());
  h.addRecord("works", { work_id: "WOR_2", person_id: "PER_1", title: "Second ILS", path: "אוטומציה", status: "פעילה", currency: "ILS", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_work2" });
  h.addRecord("works", { work_id: "WOR_3", person_id: "PER_1", title: "EUR work", path: "לימוד", status: "פעילה", currency: "EUR", created_at: "2026-10-01T00:00:00.000Z", updated_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_work3" });
  h.addRecord("payments", payment());
  h.addRecord("payments", { payment_id: "PAY_2", person_id: "PER_1", work_id: "WOR_2", path: "אוטומציה", date: "2026-10-01", actual_amount_ils: 5000, currency: "ILS", kind: "התקבל", created_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_payment2" });
  h.addRecord("payments", { payment_id: "PAY_3", person_id: "PER_1", work_id: "WOR_3", path: "לימוד", date: "2026-10-01", actual_amount_ils: 7000, currency: "EUR", kind: "התקבל", created_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_payment3" });
  h.addRecord("time", { time_id: "TIM_1", person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-01", minutes: 120, status: "פעיל", created_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_time1" });
  h.addRecord("time", { time_id: "TIM_2", person_id: "PER_1", work_id: "WOR_3", path: "לימוד", date: "2026-10-01", minutes: 60, status: "פעיל", created_at: "2026-10-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_time2" });
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  const ils = response.data.summary.monthly.find((row) => row.currency === "ILS"), eur = response.data.summary.monthly.find((row) => row.currency === "EUR");
  assert.equal(ils.paid_income_ils, 15000); assert.equal(ils.recorded_minutes, 120); assert.equal(ils.effective_hourly_ils, null); assert.equal(ils.coverage, "partial"); assert.ok(ils.reason_codes.includes("missing_time"));
  assert.equal(eur.paid_income_ils, 7000); assert.equal(eur.recorded_minutes, 60); assert.equal(eur.effective_hourly_ils, 7000); assert.equal(eur.coverage, "complete");
});

test("offsetting work cash cannot hide actual monthly missing-time evidence", () => {
  const h = createHarness({ maxRows: 25 }); h.addRecord("clients", client()); h.addRecord("works", work());
  h.addRecord("works", { work_id: "WOR_2", person_id: "PER_1", title: "Refund work", path: "אוטומציה", status: "פעילה", currency: "ILS", created_at: "2026-09-01T00:00:00.000Z", updated_at: "2026-09-01T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_work2" });
  h.addRecord("payments", payment());
  h.addRecord("payments", { payment_id: "PAY_OLD", person_id: "PER_1", work_id: "WOR_2", path: "אוטומציה", date: "2026-09-20", actual_amount_ils: 10000, currency: "ILS", kind: "התקבל", created_at: "2026-09-20T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_old" });
  h.addRecord("payments", { payment_id: "PAY_REF", person_id: "PER_1", work_id: "WOR_2", path: "אוטומציה", date: "2026-10-03", actual_amount_ils: 10000, currency: "ILS", kind: "החזר", original_payment_id: "PAY_OLD", created_at: "2026-10-03T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_ref" });
  h.addRecord("time", { time_id: "TIM_2", person_id: "PER_1", work_id: "WOR_2", path: "אוטומציה", date: "2026-10-03", minutes: 60, status: "פעיל", created_at: "2026-10-03T00:00:00.000Z", revision: 1, write_source: "משתמש", request_id: "EDIT_time2" });
  const response = h.call(h.request("snapshot", {})), october = response.data.summary.monthly.find((row) => row.month === "2026-10" && row.currency === "ILS");
  assert.equal(october.paid_income_ils, 0); assert.equal(october.recorded_minutes, 60); assert.equal(october.effective_hourly_ils, null); assert.equal(october.coverage, "partial"); assert.ok(october.reason_codes.includes("missing_time"));
});

test("MIA proposals approved by Telegram preserve per-work monthly time evidence", () => {
  const h = createHarness({ maxRows: 40 }); h.addRecord("clients", client());
  function commit(entity, input) { const proposal = propose(h, entity, input, null, 0, "MIA"); const receipt = approve(h, proposal, "TELEGRAM_APPROVAL"); assert.equal(receipt.data.status, "committed"); return proposal.record_id; }
  const work1 = commit("works", { person_id: "PER_1", title: "Paid with hours", path: "לימוד", status: "פעילה", currency: "ILS" });
  const work2 = commit("works", { person_id: "PER_1", title: "Paid without hours", path: "לימוד", status: "פעילה", currency: "ILS" });
  const work3 = commit("works", { person_id: "PER_1", title: "Foreign paid", path: "אוטומציה", status: "פעילה", currency: "EUR" });
  commit("payments", { person_id: "PER_1", work_id: work1, path: "לימוד", date: "2026-10-12", actual_amount_ils: 10000, currency: "ILS", kind: "התקבל" });
  commit("payments", { person_id: "PER_1", work_id: work2, path: "לימוד", date: "2026-10-12", actual_amount_ils: 5000, currency: "ILS", kind: "התקבל" });
  commit("payments", { person_id: "PER_1", work_id: work3, path: "אוטומציה", date: "2026-10-12", actual_amount_ils: 7000, currency: "EUR", kind: "התקבל" });
  commit("time", { person_id: "PER_1", work_id: work1, path: "לימוד", date: "2026-10-12", minutes: 120, status: "פעיל" });
  commit("time", { person_id: "PER_1", work_id: work3, path: "אוטומציה", date: "2026-10-12", minutes: 60, status: "פעיל" });
  const response = h.call(h.request("snapshot", {}));
  const ils = response.data.summary.monthly.find((row) => row.currency === "ILS"), eur = response.data.summary.monthly.find((row) => row.currency === "EUR");
  assert.deepEqual({ income: ils.paid_income_ils, minutes: ils.recorded_minutes, hourly: ils.effective_hourly_ils, coverage: ils.coverage }, { income: 15000, minutes: 120, hourly: null, coverage: "partial" });
  assert.ok(ils.reason_codes.includes("missing_time"));
  assert.deepEqual({ income: eur.paid_income_ils, minutes: eur.recorded_minutes, hourly: eur.effective_hourly_ils, coverage: eur.coverage }, { income: 7000, minutes: 60, hourly: 7000, coverage: "complete" });
});
