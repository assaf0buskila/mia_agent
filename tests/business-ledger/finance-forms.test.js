"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const crypto = require("node:crypto");
const fixture = require("./runtime-fixture.cjs");

const DEAL_ID = "11111111-1111-4111-8111-111111111111";
const REQUEST_ID = "22222222-2222-4222-8222-222222222222";
const CHARGE_IDS = [
  "33333333-3333-4333-8333-333333333331",
  "33333333-3333-4333-8333-333333333332",
  "33333333-3333-4333-8333-333333333333",
];
const PAYMENT_ID = "44444444-4444-4444-8444-444444444444";
const DEAL_RECEIPT = "55555555-5555-4555-8555-555555555555";

function ledgerRuntime(options = {}) {
  const accepted = {
    id: DEAL_ID, revision: 1, title: "עסקת אתר", amount_minor: 10000, currency: "ILS",
    payment_plan: "installments", start_date: "2026-10-11", status: "active",
    project_id: null, notes: "", updated_at: fixture.NOW,
  };
  const deals = [fixture.DEAL_HEADERS, [DEAL_ID, 1, accepted.title, 10000, "ILS", "installments", "2026-10-11", "active", "", "", fixture.NOW]];
  const receipt = {status: "committed", request_id: REQUEST_ID, id: DEAL_ID, revision: 1, receipt_id: DEAL_RECEIPT, committed_at: fixture.NOW, source: "google_sheets", mode: "TEST"};
  const audit = [fixture.AUDIT_HEADERS, ["accepted", "deal", DEAL_ID, 1, REQUEST_ID, fixture.OWNER, "deal-hash", JSON.stringify(accepted), JSON.stringify(receipt), fixture.NOW]];
  const rt = fixture.runtime(Object.assign({deals, audit}, options));
  return rt;
}

function schedule(overrides = {}) {
  return Object.assign({
    request_id: "66666666-6666-4666-8666-666666666666", expected_revision: 1,
    deal_id: DEAL_ID, number_of_charges: 3, first_due_date: "2026-10-31",
    charge_ids: CHARGE_IDS.slice(),
  }, overrides);
}

function payment(overrides = {}) {
  return Object.assign({
    request_id: "77777777-7777-4777-8777-777777777777", expected_charge_revision: 1,
    confirmed_received: true,
    payment: {id: PAYMENT_ID, charge_id: CHARGE_IDS[0], amount_minor: 1200, currency: "ILS", received_at: "2026-10-12T10:11:12.000Z"},
  }, overrides);
}

function prepareSchedule(rt, data = schedule()) {
  return rt.context.prepareFinanceFormReview("save_schedule", vm.runInContext(`(${JSON.stringify(data)})`, rt.context));
}

function committedScheduleRuntime(options = {}) {
  const rt = ledgerRuntime(Object.assign({applyBatch: true}, options));
  const prepared = prepareSchedule(rt);
  const committed = rt.context.confirmFinanceFormReview(prepared.result.review_id, true);
  assert.equal(committed.status, "ok");
  return {rt, committed};
}

test("server review freezes exact total schedule and lists every charge", () => {
  const rt = ledgerRuntime();
  const result = prepareSchedule(rt);
  assert.equal(result.status, "review");
  assert.match(result.result.summary, /סכום העסקה הכולל באגורות: 10000/);
  assert.match(result.result.summary, /2026-10-31 — 3334 אגורות/);
  assert.match(result.result.summary, /2026-11-30 — 3333 אגורות/);
  assert.match(result.result.summary, /2026-12-31 — 3333 אגורות/);
  assert.equal(rt.batchCalls(), 0);
  const stored = JSON.parse(rt.userStore.BUSINESS_LEDGER_FINANCE_REVIEW_V1);
  assert.equal(stored.owner_id, fixture.OWNER);
  assert.equal(stored.workspace_id, fixture.WORKSPACE);
  assert.equal(stored.target_revision, 1);
  assert.ok(stored.payload_json.includes(CHARGE_IDS[0]));
});

test("mutated, wrongly bound, and expired reviews cannot write", () => {
  for (const mutate of [
    (review) => { review.owner_id = "987654321"; },
    (review) => { review.workspace_id = "other"; },
    (review) => { review.payload_json = review.payload_json.replace("2026-10-31", "2026-11-01"); },
  ]) {
    const rt = ledgerRuntime();
    const prepared = prepareSchedule(rt);
    const review = JSON.parse(rt.userStore.BUSINESS_LEDGER_FINANCE_REVIEW_V1);
    mutate(review);
    rt.userStore.BUSINESS_LEDGER_FINANCE_REVIEW_V1 = JSON.stringify(review);
    assert.throws(() => rt.context.confirmFinanceFormReview(prepared.result.review_id, true));
    assert.equal(rt.batchCalls(), 0);
  }
  const expired = ledgerRuntime();
  const prepared = prepareSchedule(expired);
  const review = JSON.parse(expired.userStore.BUSINESS_LEDGER_FINANCE_REVIEW_V1);
  review.created_at = "2020-01-01T00:00:00.000Z";
  review.expires_at = "2020-01-01T00:02:00.000Z";
  expired.userStore.BUSINESS_LEDGER_FINANCE_REVIEW_V1 = JSON.stringify(review);
  const result = expired.context.confirmFinanceFormReview(prepared.result.review_id, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {status: "invalid", result: {reason_code: "review_expired"}});
  assert.equal(expired.batchCalls(), 0);
});

test("explicit approval is strict and one review can be consumed once", () => {
  const rt = ledgerRuntime({applyBatch: true});
  const prepared = prepareSchedule(rt);
  assert.throws(() => rt.context.confirmFinanceFormReview(prepared.result.review_id, 1), /explicit_approval_required/);
  assert.equal(rt.batchCalls(), 0);
  assert.equal(rt.context.confirmFinanceFormReview(prepared.result.review_id, true).status, "ok");
  assert.equal(rt.batchCalls(), 1);
  assert.throws(() => rt.context.confirmFinanceFormReview(prepared.result.review_id, true), /finance_form_recovery_required/);
  assert.equal(rt.batchCalls(), 1);
});

test("pending metadata survives reopen and a bound committed receipt can be acknowledged", () => {
  const {rt, committed} = committedScheduleRuntime();
  const pending = JSON.parse(rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1);
  assert.deepEqual(Object.keys(pending).sort(), ["created_at", "data_hash", "operation", "owner_id", "receipt_id", "receipt_revision", "request_id", "target_id", "target_revision", "workspace_id"]);
  assert.equal(pending.payload_json, undefined);
  const reopened = rt.context.getFinanceFormBootstrap();
  assert.equal(reopened.mode, "committed");
  assert.equal(reopened.receipt.id, DEAL_ID);
  const ack = rt.context.acknowledgeFinanceFormReceipt(committed.result.request_id, committed.result.receipt_id);
  assert.equal(ack.status, "ok");
  assert.equal(rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1, undefined);
});

test("lost commit response stays locked and not-found receipt lookup cannot clear it", () => {
  const rt = ledgerRuntime({throwBatch: true});
  const prepared = prepareSchedule(rt);
  assert.throws(() => rt.context.confirmFinanceFormReview(prepared.result.review_id, true), /ambiguous provider failure/);
  assert.equal(rt.batchCalls(), 1);
  assert.equal(rt.userStore.BUSINESS_LEDGER_FINANCE_REVIEW_V1, undefined);
  const marker = rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1;
  assert.ok(marker);
  assert.equal(rt.context.getFinanceFormBootstrap().mode, "recovery");
  assert.equal(rt.context.checkPendingFinanceReceipt().status, "not_found");
  assert.equal(rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1, marker);
  assert.throws(() => prepareSchedule(rt), /finance_form_recovery_required/);
  assert.equal(rt.batchCalls(), 1);
});

test("tampered receipt owner, hash, request, and receipt body remain locked", () => {
  const mutations = [
    (row) => { row[5] = "987654321"; },
    (row) => { row[6] = "0".repeat(64); },
    (row) => { row[4] = "88888888-8888-4888-8888-888888888888"; },
    (row) => { const receipt = JSON.parse(row[8]); receipt.revision = 99; row[8] = JSON.stringify(receipt); },
  ];
  for (const mutate of mutations) {
    const {rt} = committedScheduleRuntime();
    const auditRows = rt.sheets.Audit.getDataRange().getValues();
    mutate(auditRows[auditRows.length - 1]);
    assert.throws(() => rt.context.getFinanceFormBootstrap());
    assert.ok(rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1);
  }
});

test("manual false and overpayment fail before another batch", () => {
  const manual = ledgerRuntime();
  const bad = payment({confirmed_received: false});
  assert.throws(() => manual.context.prepareFinanceFormReview("record_payment", vm.runInContext(`(${JSON.stringify(bad)})`, manual.context)), /manual_confirmation_required/);
  assert.equal(manual.batchCalls(), 0);

  const {rt, committed} = committedScheduleRuntime();
  assert.equal(rt.context.acknowledgeFinanceFormReceipt(committed.result.request_id, committed.result.receipt_id).status, "ok");
  const before = rt.batchCalls();
  const excessive = payment({payment: Object.assign({}, payment().payment, {amount_minor: 3335})});
  assert.throws(() => rt.context.prepareFinanceFormReview("record_payment", vm.runInContext(`(${JSON.stringify(excessive)})`, rt.context)), /payment_exceeds_charge/);
  assert.equal(rt.batchCalls(), before);
});

test("target changing after preview is stale before the reviewed batch", () => {
  const rt = ledgerRuntime({applyBatch: true});
  const prepared = prepareSchedule(rt);
  const competing = schedule({request_id: "88888888-8888-4888-8888-888888888888", charge_ids: ["99999999-9999-4999-8999-999999999991", "99999999-9999-4999-8999-999999999992", "99999999-9999-4999-8999-999999999993"]});
  const direct = rt.context.financeOperation_({owner_id: fixture.OWNER, operation: "save_schedule", data: vm.runInContext(`(${JSON.stringify(competing)})`, rt.context)}, rt.context.ledgerConfig_());
  assert.equal(direct.status, "ok");
  const before = rt.batchCalls();
  assert.throws(() => rt.context.confirmFinanceFormReview(prepared.result.review_id, true), /revision_conflict/);
  assert.equal(rt.batchCalls(), before);
  assert.equal(rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1, undefined);
});

test("deal-form pending blocks finance bootstrap, preview, and confirmation", () => {
  const rt = ledgerRuntime({userStore: {BUSINESS_LEDGER_PENDING_V1: "metadata"}});
  assert.equal(rt.context.getFinanceFormBootstrap().mode, "blocked");
  assert.throws(() => prepareSchedule(rt), /deal_form_recovery_required/);
  assert.equal(rt.batchCalls(), 0);

  const second = ledgerRuntime();
  const prepared = prepareSchedule(second);
  second.userStore.BUSINESS_LEDGER_PENDING_V1 = "metadata";
  assert.throws(() => second.context.confirmFinanceFormReview(prepared.result.review_id, true), /deal_form_recovery_required/);
  assert.equal(second.batchCalls(), 0);
});

test("a pending finance receipt blocks deal form reads and every subsequent deal write", () => {
  const {rt} = committedScheduleRuntime();
  const marker = rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1;
  const count = rt.batchCalls();
  assert.equal(rt.context.getDealFormBootstrap().mode, "finance_recovery");
  const rejected = rt.context.controlledSaveDealForm(vm.runInContext(`(${JSON.stringify({request_id: PAYMENT_ID, deal_id: CHARGE_IDS[0], expected_revision: 0, title: "עסקה נוספת", amount_ils: "10.00", payment_plan: "full", start_date: "2026-10-11", status: "active", notes: ""})})`, rt.context));
  assert.equal(rejected.status, "recovery_required");
  assert.equal(rejected.result.reason_code, "finance_form_recovery_required");
  assert.equal(rt.batchCalls(), count);
  assert.equal(rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1, marker);
});

test("reopened HTML exposes receipt-only check and acknowledgment while all writes remain blocked", () => {
  for (const committed of [false, true]) {
    const rt = ledgerRuntime({applyBatch: committed, throwBatch: !committed});
    const prepared = prepareSchedule(rt);
    try { rt.context.confirmFinanceFormReview(prepared.result.review_id, true); } catch (_) { assert.equal(committed, false); }
    const before = rt.batchCalls(), marker = rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1;
    const script = rt.context.financeDialogHtml_().match(/<script>([\s\S]*)<\/script>/)[1];
    const elements = {};
    let checks = 0, acknowledgments = 0, confirmations = 0, closed = 0;
    const runner = {
      withSuccessHandler(handler) { this.success = handler; return this; },
      withFailureHandler(handler) { this.failure = handler; return this; },
      getFinanceFormBootstrap() { this.success(rt.context.getFinanceFormBootstrap()); },
      checkPendingFinanceReceipt() { checks += 1; this.success(rt.context.checkPendingFinanceReceipt()); },
      acknowledgeFinanceFormReceipt(key, receipt) { acknowledgments += 1; this.success(rt.context.acknowledgeFinanceFormReceipt(key, receipt)); },
      confirmFinanceFormReview() { confirmations += 1; },
      prepareFinanceFormReview() { throw new Error("recovery cannot prepare another financial write"); },
    };
    const browser = vm.createContext({document: {getElementById: id => (elements[id] ||= {disabled:false, hidden:true, value:"", textContent:"", appendChild(){}}), createElement: () => ({appendChild(){}})}, google: {script: {run: runner, host: {close(){closed += 1;}}}}, crypto: crypto.webcrypto, Date});
    new vm.Script(script).runInContext(browser);
    assert.equal(elements.recovery.hidden, false);
    assert.equal(elements.prepare.disabled, true);
    assert.equal(elements.receiptAck.hidden, !committed);
    browser.prepareReview(); browser.confirmReview();
    assert.equal(confirmations, 0);
    browser.checkFinanceReceipt();
    assert.equal(checks, 1);
    assert.equal(rt.batchCalls(), before);
    if (committed) {
      browser.acknowledgeFinanceReceipt(); browser.acknowledgeFinanceReceipt();
      assert.equal(acknowledgments, 1);
      assert.equal(closed, 1);
      assert.equal(rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1, undefined);
    } else {
      assert.equal(rt.userStore.BUSINESS_LEDGER_FINANCE_PENDING_V1, marker);
      assert.equal(elements.prepare.disabled, true);
      assert.equal(elements.receiptAck.hidden, true);
      assert.match(elements.out.textContent, /מצב השחזור נשאר נעול/);
    }
  }
});

test("Hebrew form is safe, parses, and dispatches confirmation once", () => {
  const rt = ledgerRuntime();
  const html = rt.context.financeDialogHtml_();
  assert.match(html, /ניהול כספים/);
  assert.match(html, /סכום העסקה הכולל/);
  assert.match(html, /אני מאשר ידנית שהכסף התקבל בפועל/);
  assert.match(html, /font-family:Rubik/);
  assert.doesNotMatch(html, /innerHTML|owner_id|LEDGER_HMAC_SECRET/);
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  assert.doesNotThrow(() => new vm.Script(script));

  const ids = ["operation", "deal", "charge", "count", "firstDue", "amount", "receivedAt", "confirmed", "expenseDate", "project", "title", "projectStatus", "deadline", "notes", "prepare", "review", "summary", "confirm", "out"];
  const elements = Object.fromEntries(ids.map((id) => [id, {id, disabled: false, hidden: true, value: "", checked: false, textContent: "", appendChild() {}}]));
  let confirmations = 0;
  const runner = {
    withSuccessHandler(handler) { this.success = handler; return this; },
    withFailureHandler(handler) { this.failure = handler; return this; },
    getFinanceFormBootstrap() { this.success({mode: "review", review: {review_id: "99999999-9999-4999-8999-999999999999", summary: "סיכום", expires_at: "2099-01-01T00:00:00.000Z"}}); },
    confirmFinanceFormReview() { confirmations += 1; },
  };
  const browser = vm.createContext({document: {getElementById: (id) => elements[id], createElement: () => ({appendChild() {}})}, google: {script: {run: runner, host: {close() {}}}}, crypto: crypto.webcrypto, Date});
  new vm.Script(script).runInContext(browser);
  browser.confirmReview();
  browser.confirmReview();
  assert.equal(confirmations, 1);
  assert.equal(elements.confirm.disabled, true);
  assert.equal(elements.summary.textContent, "סיכום");
});

test("choosing an existing project preserves its fields before an exact notes edit", () => {
  const rt = ledgerRuntime();
  const html = rt.context.financeDialogHtml_();
  assert.match(html, /id="project" onchange="selectedProject\(\)"/);
  const project = {id: DEAL_RECEIPT, revision: 3, title: "פרויקט קיים", status: "active", deadline: "2026-12-31", notes: "הערות קיימות"};
  const elements = {};
  const runner = {
    withSuccessHandler(handler) { this.success = handler; return this; },
    withFailureHandler(handler) { this.failure = handler; return this; },
    getFinanceFormBootstrap() { this.success({mode:"edit", deals:[], charges:[], projects:[project]}); },
  };
  const browser = vm.createContext({document: {getElementById: id => (elements[id] ||= {disabled:false, value:"", textContent:"", appendChild(){}}), createElement: () => ({})}, google: {script: {run:runner, host:{close(){}}}}, crypto:crypto.webcrypto, Date});
  new vm.Script(html.match(/<script>([\s\S]*)<\/script>/)[1]).runInContext(browser);
  elements.operation.value = "save_project";
  elements.project.value = project.id;
  browser.selectedProject();
  assert.deepEqual([elements.title.value, elements.projectStatus.value, elements.deadline.value, elements.notes.value], [project.title, project.status, project.deadline, project.notes]);
  elements.notes.value = "הערה חדשה בלבד";
  const request = browser.data();
  assert.equal(request.data.expected_revision, 3);
  assert.deepEqual(JSON.parse(JSON.stringify(request.data.project)), {id:project.id, title:project.title, status:project.status, deadline:project.deadline, notes:"הערה חדשה בלבד"});
  elements.operation.value = "record_expense";
  elements.notes.value = "הערת הוצאה";
  browser.selectedProject();
  assert.equal(elements.notes.value, "הערת הוצאה");
  elements.operation.value = "save_project";
  elements.project.value = "";
  browser.selectedProject();
  assert.deepEqual([elements.title.value, elements.projectStatus.value, elements.deadline.value, elements.notes.value], ["", "planned", "", ""]);
});
