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

test("two pending new leads with equivalent contact cannot both commit", () => {
  const h = createHarness();
  const first = propose(h, "leads", { name: "דנה", phone: "+972502222222", path: "לימוד", status: "חדש", source_note: "site one" });
  const second = propose(h, "leads", { name: "דנה שוב", phone: "050-222-2222", path: "אוטומציה", status: "חדש", source_note: "site two" });
  assert.equal(approve(h, first).data.status, "committed");
  const stale = approve(h, second);
  assert.equal(stale.ok, true);
  assert.equal(stale.data.status, "target_changed");
  assert.equal(h.workbook.getSheetByName("לידים").getDataRange().getValues().length, 2);
});

test("new lead proposal rejects an equivalent contact already committed", () => {
  const h = createHarness();
  const first = propose(h, "leads", { name: "דנה", phone: "+972502222222", path: "לימוד", status: "חדש", source_note: "site one" });
  assert.equal(approve(h, first).data.status, "committed");
  const requestId = "proposal-lead-existing-contact";
  const duplicate = h.call(h.request("propose", { request_id: requestId, entity: "leads", record_id: null, expected_revision: 0, input: { name: "דנה שוב", phone: "050-222-2222", path: "אוטומציה", status: "חדש", source_note: "site two" } }, { request_id: requestId, actor_source: "MIA" }));
  assert.equal(duplicate.ok, false);
  assert.equal(duplicate.data.error.code, "lead_already_exists");
  assert.equal(h.workbook.getSheetByName("לידים").getDataRange().getValues().length, 2);
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

test("combined Apps Script keeps a duplicate-ID snapshot partial and disables affected pickers", () => {
  const h = createHarness({ maxRows: 25, withSheets: true });
  h.addRecord("clients", client());
  h.addRecord("clients", { ...client(), name: "שם שני", request_id: "EDIT_duplicate" });
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  assert.equal(response.data.entity_coverage.clients, "partial");
  const tab = h.Schema.tab("works"), personColumn = tab.fields.findIndex((field) => field.key === "person_id") + 1;
  const validation = h.workbook.getSheetByName(tab.name).validations.get(`2:${personColumn}:24`);
  assert.equal(validation.allowInvalid, false);
  assert.equal(validation.formula, "=FALSE");
  assert.equal(validation.values, undefined);
  h.workbook.getSheetByName("לקוחות").setRow(3, h.Schema.tab("clients").fields.map(() => ""));
  h.resetMetrics();
  const repaired = h.call(h.request("snapshot", {}));
  assert.equal(repaired.ok, true, JSON.stringify(repaired.data));
  const refreshed = h.workbook.getSheetByName(tab.name).validations.get(`2:${personColumn}:24`);
  assert.ok(refreshed.values.some((value) => value.endsWith("⟦PER_1⟧")));
  assert.ok(h.metrics().setDataValidation > 0);
});

test("combined Apps Script refreshes work choices after an approved OS write", () => {
  const h = createHarness({ maxRows: 25, withSheets: true });
  h.addRecord("clients", client());
  const proposal = propose(h, "works", { person_id: "PER_1", title: "עבודה מאושרת", path: "אוטומציה", status: "פעילה", currency: "ILS" });
  const receipt = approve(h, proposal);
  assert.equal(receipt.data.status, "committed");
  const tab = h.Schema.tab("payments"), workColumn = tab.fields.findIndex((field) => field.key === "work_id") + 1;
  const validation = h.workbook.getSheetByName(tab.name).validations.get(`2:${workColumn}:24`);
  assert.ok(validation.values.some((value) => value.includes("עבודה מאושרת") && value.endsWith(`⟦${proposal.record_id}⟧`)));
});

test("combined Apps Script refreshes original-payment choices after approval", () => {
  const h = createHarness({ maxRows: 25, withSheets: true });
  h.addRecord("clients", client()); h.addRecord("works", work());
  const proposal = propose(h, "payments", { person_id: "PER_1", work_id: "WOR_1", path: "אוטומציה", date: "2026-10-12", actual_amount_ils: 10000, currency: "ILS", kind: "התקבל" });
  assert.equal(approve(h, proposal).data.status, "committed");
  const tab = h.Schema.tab("payments"), originalColumn = tab.fields.findIndex((field) => field.key === "original_payment_id") + 1;
  const validation = h.workbook.getSheetByName(tab.name).validations.get(`2:${originalColumn}:24`);
  assert.ok(validation.values.some((value) => value.endsWith(`⟦${proposal.record_id}⟧`)));
});

test("combined Apps Script refreshes customer choices after a native edit trigger", () => {
  const h = createHarness({ maxRows: 25, withSheets: true }), tab = h.Schema.tab("clients"), sheet = h.workbook.getSheetByName(tab.name);
  h.addRecord("clients", { name: "לקוחה חדשה", status: "פעיל" });
  h.context.reconcileAssafOsEdit({ range: sheet.getRange(2, 2) });
  const personId = sheet.getDataRange().getValues()[1][0];
  assert.match(personId, /^PER_/);
  const workTab = h.Schema.tab("works"), personColumn = workTab.fields.findIndex((field) => field.key === "person_id") + 1;
  const validation = h.workbook.getSheetByName(workTab.name).validations.get(`2:${personColumn}:24`);
  assert.ok(validation.values.some((value) => value.includes("לקוחה חדשה") && value.endsWith(`⟦${personId}⟧`)));
  sheet.setRow(2, tab.fields.map(() => ""));
  h.resetMetrics();
  h.context.reconcileAssafOsEdit({ range: sheet.getRange(2, 2) });
  const afterDelete = h.workbook.getSheetByName(workTab.name).validations.get(`2:${personColumn}:24`);
  assert.equal(afterDelete.formula, "=FALSE");
  assert.ok(h.metrics().setDataValidation > 0);
});

test("post-commit picker refresh failure cannot turn a committed approval into failure", () => {
  const h = createHarness({ maxRows: 25, withSheets: true });
  h.addRecord("clients", client());
  const proposal = propose(h, "works", { person_id: "PER_1", title: "נשארת מאושרת", path: "לימוד", status: "פעילה", currency: "ILS" });
  const realRefresh = h.context.refreshAssafOsReferenceValidations_;
  h.context.refreshAssafOsReferenceValidations_ = () => { throw new Error("synthetic_ui_failure"); };
  const receipt = approve(h, proposal);
  assert.equal(receipt.ok, true, JSON.stringify(receipt.data));
  assert.equal(receipt.data.status, "committed");
  assert.ok(h.workbook.getSheetByName("עבודות").getDataRange().getValues().some((row) => row[2] === "נשארת מאושרת"));
  assert.equal(h.scriptProperties.ASSAF_OS_REFERENCE_VALIDATION_DIRTY, "true");
  h.context.refreshAssafOsReferenceValidations_ = realRefresh;
  h.resetMetrics();
  const retry = h.call(h.request("snapshot", {}));
  assert.equal(retry.ok, true, JSON.stringify(retry.data));
  assert.ok(h.metrics().setDataValidation > 0);
  assert.equal(h.scriptProperties.ASSAF_OS_REFERENCE_VALIDATION_DIRTY, undefined);
});

test("missing catalog fingerprint refreshes instead of blessing stale picker choices", () => {
  const h = createHarness({ maxRows: 25, withSheets: true });
  h.addRecord("clients", client());
  assert.equal(h.call(h.request("snapshot", {})).ok, true);
  const workTab = h.Schema.tab("works"), personColumn = workTab.fields.findIndex((field) => field.key === "person_id") + 1;
  assert.ok(h.workbook.getSheetByName(workTab.name).validations.get(`2:${personColumn}:24`).values.some((value) => value.endsWith("⟦PER_1⟧")));
  delete h.scriptProperties.ASSAF_OS_REFERENCE_VALIDATION_FINGERPRINT;
  h.workbook.getSheetByName("לקוחות").setRow(2, h.Schema.tab("clients").fields.map(() => ""));
  h.resetMetrics();
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  assert.equal(h.workbook.getSheetByName(workTab.name).validations.get(`2:${personColumn}:24`).formula, "=FALSE");
  assert.ok(h.metrics().setDataValidation > 0);
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

test("actual Apps Script resolves readable native pickers to exact stable IDs", () => {
  const h = createHarness({ maxRows: 30 });
  h.addRecord("clients", client());
  h.addRecord("clients", { ...client(), person_id: "PER_10", request_id: "EDIT_client10" });
  const personField = h.Schema.tab("works").fields.find((field) => field.key === "person_id");
  const choices = h.context.assafOsReferenceChoices_(h.workbook, personField);
  assert.equal(choices.length, 2);
  assert.ok(choices.some((value) => value.endsWith("⟦PER_1⟧")));
  assert.ok(choices.some((value) => value.endsWith("⟦PER_10⟧")));
  assert.equal(h.context.assafOsResolveReferenceValue_(h.workbook, personField, "תווית שלא סומכים עליה ⟦PER_10⟧"), "PER_10");

  const chosen = choices.find((value) => value.endsWith("⟦PER_10⟧"));
  h.addRecord("works", { person_id: chosen, title: "בחירה קריאה", path: "לימוד", status: "פעילה", currency: "ILS" });
  let snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.data.records.works.find((row) => row.title === "בחירה קריאה").person_id, "PER_10");

  const proposal = propose(h, "works", { person_id: "PER_1", title: "נכתבה מה־OS", path: "אוטומציה", status: "פעילה", currency: "ILS" });
  assert.equal(approve(h, proposal).data.status, "committed");
  const workTab = h.Schema.tab("works"), personIndex = workTab.fields.findIndex((field) => field.key === "person_id");
  const written = h.workbook.getSheetByName(workTab.name).getDataRange().getValues().find((row) => row[2] === "נכתבה מה־OS");
  assert.ok(String(written[personIndex]).endsWith("⟦PER_1⟧"));
  assert.equal(h.context.assafOsRowToRecord_(workTab, written, h.workbook).person_id, "PER_1");

  h.addRecord("works", { person_id: "דנה ⟦PER_100⟧", title: "מזהה דומה", path: "לימוד", status: "פעילה", currency: "ILS" });
  h.addRecord("works", { person_id: "דנה", title: "כינוי לא ידוע", path: "לימוד", status: "פעילה", currency: "ILS" });
  snapshot = h.call(h.request("snapshot", {}));
  assert.equal(snapshot.data.records.works.filter((row) => ["מזהה דומה", "כינוי לא ידוע"].includes(row.title)).length, 0);
  assert.ok(snapshot.data.issues.some((issue) => issue.entity === "works" && issue.code === "unknown_reference"));
  assert.equal(h.workbook.getSheetByName("_assaf_os_issues").getDataRange().getValues().filter((row) => row[2] === "unknown_reference" && row[3] === "works").length, 2);
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

test("unchanged empty snapshot performs one raw read per sheet and no UI or projection writes", () => {
  const h = createHarness({ maxRows: 2001, withSheets: true });
  const initialized = h.context.refreshAssafOsReferenceValidations();
  assert.equal(initialized.refreshed, true);
  assert.ok(h.metrics().setDataValidation > 0);
  assert.match(h.scriptProperties.ASSAF_OS_REFERENCE_VALIDATION_FINGERPRINT, /^[0-9a-f]{64}$/);
  h.resetMetrics();
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  assert.deepEqual(response.data.records.clients, []);
  assert.deepEqual(response.data.records.works, []);
  assert.deepEqual(h.metrics(), {
    getValues: 13, getFormulas: 8, getLastRow: 13, getLastColumn: 16,
    getDataRange: 8, setDataValidation: 0, batchUpdate: 0,
  });
  assert.equal(h.context.ASSAF_OS_REQUEST_READ_CACHE, null);
});

test("unchanged populated snapshot keeps bounded reads and skips stable picker and projection writes", () => {
  const h = createHarness({ maxRows: 2001, withSheets: true });
  h.addRecord("clients", client()); h.addRecord("works", work()); h.addRecord("payments", payment());
  const settled = h.call(h.request("snapshot", {}));
  assert.equal(settled.ok, true, JSON.stringify(settled.data));
  h.resetMetrics();
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.ok, true, JSON.stringify(response.data));
  assert.equal(response.data.records.payments[0].payment_id, "PAY_1");
  assert.equal(response.data.summary.monthly[0].paid_income_ils, 10000);
  assert.deepEqual(h.metrics(), {
    getValues: 13, getFormulas: 8, getLastRow: 13, getLastColumn: 16,
    getDataRange: 8, setDataValidation: 0, batchUpdate: 0,
  });
});

test("five-lead fresh snapshot keeps metadata reads constant and performs no writes", () => {
  const h = createHarness({ maxRows: 2001, withSheets: true });
  for (let index = 0; index < 5; index += 1) {
    const proposal = propose(h, "leads", { name: `Synthetic ${index}`, phone: `+9725022222${index}`, path: "לימוד", status: "חדש", source_note: "synthetic" }, null, 0, "MIA");
    assert.equal(approve(h, proposal, "TELEGRAM_APPROVAL").data.status, "committed");
  }
  assert.equal(h.context.refreshAssafOsReferenceValidations().refreshed, true);
  h.resetMetrics();
  const response = h.call(h.request("snapshot", {}));
  assert.equal(response.data.records.leads.length, 5);
  assert.deepEqual(h.metrics(), {
    getValues: 13, getFormulas: 8, getLastRow: 13, getLastColumn: 16,
    getDataRange: 8, setDataValidation: 0, batchUpdate: 0,
  });
});

test("guarded lead approval has a bounded service-call shape", () => {
  const h = createHarness({ maxRows: 2001, withSheets: true });
  const proposal = propose(h, "leads", { name: "Synthetic", phone: "+972502222222", path: "לימוד", status: "חדש", source_note: "synthetic" }, null, 0, "MIA");
  assert.equal(proposal.approval_guard_version, 1);
  h.resetMetrics();
  const response = approve(h, proposal, "TELEGRAM_APPROVAL");
  assert.equal(response.data.status, "committed");
  assert.deepEqual(h.metrics(), {
    getValues: 12, getFormulas: 7, getLastRow: 19, getLastColumn: 14,
    getDataRange: 7, setDataValidation: 0, batchUpdate: 1,
  });
});

test("metadata preload checks capacity before any body read", () => {
  const h = createHarness({ maxRows: 2002 });
  h.workbook.getSheetByName("_assaf_os_accepted").setRow(2002, ["overflow"]);
  h.resetMetrics();
  const response = h.call(h.request("schema", {}));
  assert.equal(response.ok, false);
  assert.equal(response.data.error.code, "metadata_capacity_exceeded");
  assert.equal(h.metrics().getValues, 0);
  assert.equal(h.metrics().getDataRange, 0);
});

test("metadata preload rejects header drift before runtime reads", () => {
  const h = createHarness({ maxRows: 2001 });
  h.workbook.getSheetByName("_assaf_os_receipts").setCell(1, 1, "drifted");
  const response = h.call(h.request("schema", {}));
  assert.equal(response.ok, false);
  assert.equal(response.data.error.code, "metadata_header_drift");
});

test("successful metadata appends invalidate preloaded proposal and replay bodies", () => {
  const h = createHarness({ maxRows: 2001 });
  h.context.assafOsBeginRequestReadCache_(h.workbook);
  h.context.assafOsVerifyMetadata_(h.workbook);
  const proposals = h.workbook.getSheetByName("_assaf_os_proposals");
  const replay = h.workbook.getSheetByName("_assaf_os_replay");
  assert.equal(h.context.assafOsReadValuesOnce_(proposals).length, 1);
  assert.equal(h.context.assafOsReadValuesOnce_(replay).length, 1);
  const request = h.request("propose", null, { actor_source: "MIA" });
  request.data = {
    request_id: request.request_id,
    entity: "leads",
    record_id: null,
    expected_revision: 0,
    input: { name: "Synthetic", phone: "+972502222222", path: "לימוד", status: "חדש", source_note: "synthetic" },
  };
  h.context.assafOsPropose_(
    h.workbook,
    request,
    { ...h.binding, secret: h.secret },
    h.context.assafOsIdempotencyHash_(request),
    null,
  );
  assert.equal(h.context.assafOsReadValuesOnce_(proposals).length, 2);
  assert.equal(h.context.assafOsReadValuesOnce_(replay).length, 2);
});

test("snapshot repairs calculated summary drift without refreshing stable native pickers", () => {
  const h = createHarness({ maxRows: 30, withSheets: true });
  h.addRecord("clients", client()); h.addRecord("works", work()); h.addRecord("payments", payment());
  const settled = h.call(h.request("snapshot", {}));
  assert.equal(settled.ok, true, JSON.stringify(settled.data));
  const summaryTab = h.Schema.tab("summary"), summarySheet = h.workbook.getSheetByName(summaryTab.name);
  const paidColumn = summaryTab.fields.findIndex((field) => field.key === "net_received_ils") + 1;
  summarySheet.setCell(2, paidColumn, 999);
  h.resetMetrics();
  const repaired = h.call(h.request("snapshot", {}));
  assert.equal(repaired.ok, true, JSON.stringify(repaired.data));
  assert.equal(repaired.data.summary.monthly[0].paid_income_ils, 10000);
  assert.equal(summarySheet.value(2, paidColumn), 100);
  assert.equal(h.metrics().batchUpdate, 1);
  assert.equal(h.metrics().setDataValidation, 0);
});

test("schema and snapshot reads do not consume replay capacity", () => {
  const h = createHarness({ maxRows: 25 });
  const replay = h.workbook.getSheetByName("_assaf_os_replay");
  assert.equal(replay.getLastRow(), 1);
  assert.equal(h.call(h.request("schema", {})).ok, true);
  assert.equal(h.call(h.request("snapshot", {})).ok, true);
  assert.equal(h.call(h.request("snapshot", {})).ok, true);
  assert.equal(replay.getLastRow(), 1);
  propose(h, "tasks", { title: "Replay is reserved for mutations", status: "לביצוע" });
  assert.equal(replay.getLastRow(), 2);
});
