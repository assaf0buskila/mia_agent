"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const vm = require("node:vm");
const { SECRET, WORKSPACE, OWNER, DEAL_ID, NOW, DEAL_HEADERS, AUDIT_HEADERS, CONFLICT_HEADERS, HEBREW_SCHEMA, hmac, runtime, event, inner, acceptedRows } = require("./runtime-fixture.cjs");
test("mocked web-app path uses a script lock and marks ambiguous batch failure unavailable", () => {
  const rt = runtime({ throwBatch: true });
  const data = {
    request_id: "33333333-3333-4333-8333-333333333333", expected_revision: 0,
    deal: { id: DEAL_ID, title: "TEST deal", amount_minor: 1000, currency: "ILS", payment_plan: "full", start_date: "2026-10-11", status: "active", project_id: null, notes: "mock only" },
  };
  const response = inner(rt.context.doPost(event("save_deal", data)));
  assert.equal(response.status, "unavailable");
  assert.equal(response.result.reason_code, "provider_result_unknown");
  assert.equal(rt.batchCalls(), 1);
  assert.equal(rt.released(), 1);
});

test("Unicode request and response HMAC use explicit Apps Script UTF-8", () => {
  const rt = runtime({ applyBatch: true, requireExplicitHmacCharset: true });
  const data = {
    request_id: "33333333-3333-4333-8333-333333333333", expected_revision: 0,
    deal: { id: DEAL_ID, title: "עסקת café 🚀", amount_minor: 1000, currency: "ILS", payment_plan: "full", start_date: "2026-10-11", status: "active", project_id: null, notes: "בדיקה עברית, naïve, emoji 🎯" },
  };
  const output = rt.context.doPost(event("save_deal", data));
  const wrapper = JSON.parse(output.text);
  const response = JSON.parse(wrapper.payload);
  assert.equal(response.status, "ok");
  assert.equal(response.result.id, DEAL_ID);
  assert.equal(wrapper.signature, hmac(wrapper.payload));
  assert.deepEqual(rt.hmacCharsets, ["UTF_8", "UTF_8"]);
  assert.equal(rt.batchCalls(), 1);
});

test("mocked snapshot detects accepted-row drift without writing a conflict", () => {
  const accepted = { id: DEAL_ID, revision: 1, title: "Accepted", amount_minor: 1000, currency: "ILS", payment_plan: "full", start_date: "2026-10-11", status: "active", project_id: null, notes: "", updated_at: NOW };
  const row = [DEAL_ID, 1, "Raw edit", 1000, "ILS", "full", "2026-10-11", "active", "", "", NOW];
  const audit = ["accepted", "deal", DEAL_ID, 1, "33333333-3333-4333-8333-333333333333", OWNER, "data-hash", JSON.stringify(accepted), JSON.stringify({ status: "committed" }), NOW];
  const rt = runtime({ deals: [DEAL_HEADERS, row], audit: [AUDIT_HEADERS, audit] });
  const response = inner(rt.context.doPost(event("snapshot", {})));
  assert.equal(response.status, "conflict");
  assert.equal(response.result.reason_code, "deal_row_tampered");
  assert.equal(rt.batchCalls(), 0);
  assert.equal(rt.released(), 1);
});

test("mocked adapter fails closed instead of returning partial coverage over row cap", () => {
  const excessRows = [DEAL_HEADERS].concat(Array.from({ length: 501 }, () => Array(DEAL_HEADERS.length).fill("")));
  const rt = runtime({ deals: excessRows });
  const response = inner(rt.context.doPost(event("snapshot", {})));
  assert.equal(response.status, "unavailable");
  assert.equal(response.result.reason_code, "coverage_limit_exceeded");
  assert.equal(rt.batchCalls(), 0);
  assert.equal(rt.released(), 1);
});

test("mocked save rejects the 501st deal before the atomic deal/audit commit", () => {
  const rows = acceptedRows(500);
  const rt = runtime(rows);
  const data = { request_id: "33333333-3333-4333-8333-333333333333", expected_revision: 0, deal: { id: DEAL_ID, title: "Over cap", amount_minor: 1000, currency: "ILS", payment_plan: "full", start_date: "2026-10-11", status: "active", project_id: null, notes: "" } };
  const response = inner(rt.context.doPost(event("save_deal", data)));
  assert.equal(response.status, "conflict");
  assert.equal(response.result.reason_code, "deal_capacity_exceeded");
  assert.equal(rt.batchCalls(), 1, "only the Conflicts row is written");
});

test("mocked save keeps the 5001st Audit row out of the accepted transaction", () => {
  const audit = [AUDIT_HEADERS].concat(Array.from({ length: 5000 }, () => ["diagnostic"]));
  const rt = runtime({ audit });
  const data = { request_id: "33333333-3333-4333-8333-333333333333", expected_revision: 0, deal: { id: DEAL_ID, title: "Audit cap", amount_minor: 1000, currency: "ILS", payment_plan: "full", start_date: "2026-10-11", status: "active", project_id: null, notes: "" } };
  const response = inner(rt.context.doPost(event("save_deal", data)));
  assert.equal(response.status, "conflict");
  assert.equal(response.result.reason_code, "audit_capacity_exceeded");
  assert.equal(rt.batchCalls(), 1, "only the Conflicts row is written");
});

test("mocked snapshot enforces UTF-8 response budget without truncating coverage", () => {
  const rows = acceptedRows(100, "界".repeat(1000));
  const rt = runtime(rows);
  const response = inner(rt.context.doPost(event("snapshot", {})));
  assert.equal(response.status, "unavailable");
  assert.equal(response.result.reason_code, "snapshot_capacity_exceeded");
  assert.equal(rt.batchCalls(), 0);
});

test("mocked snapshot sizes the signed outer envelope with quote escaping", () => {
  const rows = acceptedRows(80, '"'.repeat(1000));
  const rt = runtime(rows);
  const response = inner(rt.context.doPost(event("snapshot", {})));
  assert.equal(response.status, "unavailable");
  assert.equal(response.result.reason_code, "snapshot_capacity_exceeded");
  assert.equal(rt.batchCalls(), 0);
});

test("wrong workbook title or Settings mode rejects before any sheet write", () => {
  const wrongTitle = runtime({ title: "Assaf Business Ledger LIVE" });
  const titleResponse = inner(wrongTitle.context.doPost(event("snapshot", {})));
  assert.equal(titleResponse.status, "invalid");
  assert.equal(titleResponse.result.reason_code, "workbook_not_test");
  assert.equal(wrongTitle.batchCalls() + wrongTitle.rangeWrites(), 0);

  const wrongMode = runtime({ settings: [["key", "value"], ["mode", "LIVE"], ["workspace_id", WORKSPACE]] });
  const modeResponse = inner(wrongMode.context.doPost(event("snapshot", {})));
  assert.equal(modeResponse.status, "invalid");
  assert.equal(modeResponse.result.reason_code, "settings_mode_not_test");
  assert.equal(wrongMode.batchCalls() + wrongMode.rangeWrites(), 0);
});

test("exact AssafWeb OS Business title is accepted only with TEST Settings mode", () => {
  const named = runtime({ title: "AssafWeb OS Business" });
  assert.equal(inner(named.context.doPost(event("snapshot", {}))).status, "ok");

  const liveMode = runtime({
    title: "AssafWeb OS Business",
    settings: [["key", "value"], ["mode", "LIVE"], ["workspace_id", WORKSPACE]],
  });
  const response = inner(liveMode.context.doPost(event("snapshot", {})));
  assert.equal(response.status, "invalid");
  assert.equal(response.result.reason_code, "settings_mode_not_test");
  assert.equal(liveMode.batchCalls() + liveMode.rangeWrites(), 0);
});

test("visible menu modal and installer use the exact Business product name", () => {
  const source = fs.readFileSync("ops/business-ledger/Code.gs", "utf8");
  assert.match(source, /createMenu\("AssafWeb OS Business"\)/);
  assert.match(source, /showModalDialog\(html, "AssafWeb OS Business"\)/);
  assert.match(source, /ui\.alert\("AssafWeb OS Business"/);
  assert.match(source, /סביבת בדיקה \/ נתונים סינתטיים/);
  assert.doesNotMatch(source, /ניהול עסקאות TEST|עסקה ב-Business Ledger TEST|Install TEST ledger/);
});

test("installer preserves imported settings and adds only controlled keys", () => {
  const settings = [["key", "value"], ["mode", "TEST"], ["currency", "ILS"], ["vat_rate", "0"], ["custom", "preserve-me"]];
  const rt = runtime({ settings });
  rt.context.installBusinessLedgerTest();
  const map = Object.fromEntries(settings.slice(1).map((row) => [row[0], row[1]]));
  assert.equal(map.mode, "TEST");
  assert.equal(map.currency, "ILS");
  assert.equal(map.vat_rate, "0");
  assert.equal(map.custom, "preserve-me");
  assert.equal(map.workspace_id, WORKSPACE);
  assert.equal(map.financial_coverage, "deals_only");
});

test("installer refuses missing or duplicate mode and capacity before mutation", () => {
  const missing = runtime({ settings: [["key", "value"], ["currency", "ILS"]] });
  assert.throws(() => missing.context.installBusinessLedgerTest(), /settings_mode_missing/);
  assert.equal(missing.rangeWrites(), 0);

  const duplicate = runtime({ settings: [["key", "value"], ["mode", "TEST"], ["mode", "TEST"]] });
  assert.throws(() => duplicate.context.installBusinessLedgerTest(), /invalid_settings/);
  assert.equal(duplicate.rangeWrites(), 0);

  const full = [["key", "value"], ["mode", "TEST"]];
  for (let i = 1; i < 100; i += 1) full.push([`key_${i}`, "value"]);
  const atCapacity = runtime({ settings: full });
  assert.throws(() => atCapacity.context.installBusinessLedgerTest(), /settings_capacity_exceeded/);
  assert.equal(atCapacity.rangeWrites(), 0);
});

test("Hebrew tab and header aliases remain readable while mixed or duplicate aliases fail closed", () => {
  const hebrew = runtime({ schema: "hebrew" });
  const ok = inner(hebrew.context.doPost(event("snapshot", {})));
  assert.equal(ok.status, "ok");

  const duplicate = runtime({ ambiguousDeals: true });
  const duplicateResult = inner(duplicate.context.doPost(event("snapshot", {})));
  assert.equal(duplicateResult.status, "invalid");
  assert.equal(duplicateResult.result.reason_code, "ambiguous_sheet_alias_deals");
  assert.equal(duplicate.batchCalls() + duplicate.rangeWrites(), 0);

  const mixed = runtime({ schema: "hebrew", deals: [DEAL_HEADERS] });
  const mixedResult = inner(mixed.context.doPost(event("snapshot", {})));
  assert.equal(mixedResult.status, "invalid");
  assert.equal(mixedResult.result.reason_code, "invalid_headers_deals");
});

test("installer emits bounded Hebrew RTL metadata updates without touching record rows", () => {
  const settings = [["key", "value"], ["mode", "TEST"], ["currency", "ILS"], ["vat_rate", "0"]];
  const rt = runtime({ settings });
  rt.context.installBusinessLedgerTest();
  assert.equal(rt.batchCalls(), 1);
  const requests = rt.batchBodies[0].requests;
  const properties = requests.filter((request) => request.updateSheetProperties).map((request) => request.updateSheetProperties.properties);
  assert.deepEqual(Array.from(properties, (value) => value.title), Object.values(HEBREW_SCHEMA).map((value) => value[0]));
  assert.ok(properties.every((value) => value.rightToLeft === true && value.gridProperties.frozenRowCount === 1));
  assert.equal(requests.filter((request) => request.updateCells && request.updateCells.range.startRowIndex === 0).length, 9);
  assert.equal(requests.filter((request) => request.repeatCell && request.repeatCell.cell.userEnteredFormat.textFormat.fontFamily === "Rubik").length, 18);
  const headerFormats = requests.filter((request) => request.repeatCell && request.repeatCell.range.startRowIndex === 0 && request.repeatCell.range.endRowIndex === 1);
  assert.equal(headerFormats.length, 9);
  assert.ok(headerFormats.every((request) => request.repeatCell.fields === "userEnteredFormat.textFormat.fontFamily,userEnteredFormat.textFormat.bold,userEnteredFormat.textFormat.foregroundColor,userEnteredFormat.horizontalAlignment"));
  assert.ok(headerFormats.every((request) => JSON.stringify(request.repeatCell.cell.userEnteredFormat.textFormat.foregroundColor) === JSON.stringify({ red: 1, green: 1, blue: 1 })));
  assert.equal(rt.sheets.Deals.getDataRange().getValues().length, 1, "metadata translation does not rewrite records");
});

test("Hebrew deal form replaces JSON paste and converts only bounded human fields", () => {
  const rt = runtime();
  const html = rt.context.dealDialogHtml_();
  assert.match(html, /יצירה או עדכון עסקה/);
  assert.match(html, /סכום העסקה/);
  assert.match(html, /controlledSaveDealForm/);
  assert.match(html, /סביבת בדיקה \/ נתונים סינתטיים/);
  assert.doesNotMatch(html, /Business Ledger TEST|ניהול עסקאות TEST/);
  assert.doesNotMatch(html, /<textarea|Paste the exact save data JSON/);
  assert.doesNotMatch(html, /owner_id|LEDGER_HMAC_SECRET/);
  assert.doesNotMatch(html, /abandonPendingDealAfterNotFound|שחרור וחזרה לטופס חדש/);
  const generatedScript = html.match(/<script>([\s\S]*)<\/script>/)[1];
  assert.doesNotThrow(() => new vm.Script(generatedScript), "the actual emitted browser script must parse");

  const elements = Object.fromEntries(["dealSelect", "title", "amount", "start", "plan", "status", "notes", "save", "out"].map((id) => [id, { id, disabled: false, value: "", addEventListener() {}, appendChild() {} }]));
  const scriptRunner = { withSuccessHandler(handler) { this.success = handler; return this; }, withFailureHandler() { return this; }, getDealFormBootstrap() { this.success({ mode: "edit", deals: [] }); } };
  const browser = vm.createContext({ document: { getElementById: (id) => elements[id], createElement: () => ({ appendChild() {} }) }, google: { script: { run: scriptRunner, host: { close() {} } } }, crypto: crypto.webcrypto, confirm: () => true, setTimeout() {} });
  new vm.Script(generatedScript).runInContext(browser);
  browser.setLocked(true);
  for (const id of ["dealSelect", "title", "amount", "start", "plan", "status", "notes", "save"]) assert.equal(elements[id].disabled, true, `${id} must lock during unknown/inflight state`);

  const form = { request_id: "33333333-3333-4333-8333-333333333333", deal_id: DEAL_ID, expected_revision: 0, title: "עסקת TEST", amount_ils: "1234.56", payment_plan: "full", start_date: "2026-10-11", status: "active", notes: "הערה" };
  const data = vm.runInContext(`formToSaveData_(${JSON.stringify(form)})`, rt.context);
  assert.equal(data.deal.amount_minor, 123456);
  assert.equal(data.deal.currency, "ILS");
  assert.equal(data.deal.project_id, null);
  assert.throws(() => vm.runInContext(`formToSaveData_(${JSON.stringify({ ...form, amount_ils: "1,234" })})`, rt.context), /invalid_amount_ils/);
});

test("lost response survives reopen and permits receipt recovery without a second write", () => {
  const userStore = {};
  const rt = runtime({ userStore, applyBatch: true });
  const form = { request_id: "33333333-3333-4333-8333-333333333333", deal_id: DEAL_ID, expected_revision: 0, title: "עסקת TEST", amount_ils: "1234.56", payment_plan: "full", start_date: "2026-10-11", status: "active", notes: "הערה" };
  const expression = `controlledSaveDealForm(${JSON.stringify(form)})`;
  const committed = vm.runInContext(expression, rt.context);
  assert.equal(committed.status, "ok");
  assert.ok(userStore.BUSINESS_LEDGER_PENDING_V1, "pending pointer stays durable until client receipt acknowledgement");
  const writesAfterCommit = rt.batchCalls();

  const duplicateAttempt = vm.runInContext(expression, rt.context);
  assert.equal(duplicateAttempt.status, "recovery_required");
  assert.equal(rt.batchCalls(), writesAfterCommit, "pending operation cannot issue a second finance write");

  const reopened = rt.context.getDealFormBootstrap();
  assert.equal(reopened.mode, "committed");
  assert.equal(reopened.receipt.request_id, form.request_id);
  assert.equal(reopened.receipt.id, form.deal_id);
  assert.deepEqual(Array.from(Object.keys(reopened.pending).sort()), ["created_at", "deal_id", "expected_revision", "request_id"]);
  assert.equal(reopened.pending.owner_id, undefined);
  assert.equal(reopened.pending.workspace_id, undefined);
  assert.equal(reopened.pending.data_hash, undefined);
  const acknowledged = rt.context.acknowledgeDealFormReceipt(reopened.receipt.request_id, reopened.receipt.receipt_id);
  assert.equal(acknowledged.status, "ok");
  assert.equal(userStore.BUSINESS_LEDGER_PENDING_V1, undefined);
  const refreshed = rt.context.getDealFormBootstrap();
  assert.equal(refreshed.mode, "edit");
  assert.equal(refreshed.deals[0].revision, 1);
});

test("controlled form preview freezes exact fields and dispatches only once after explicit approval", () => {
  const html = runtime().context.dealDialogHtml_();
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const elements = Object.fromEntries(["dealSelect", "title", "amount", "start", "plan", "status", "notes", "save", "out", "review", "reviewSummary"].map(id => [id, {disabled: false, value: "", hidden: true, textContent: "", addEventListener() {}, appendChild() {}}]));
  let moment = 1000;
  const sent = [];
  const runner = {
    withSuccessHandler(handler) { this.success = handler; return this; },
    withFailureHandler(handler) { this.failure = handler; return this; },
    getDealFormBootstrap() { this.success({mode: "edit", deals: []}); },
    controlledSaveDealForm(form) { sent.push(JSON.parse(JSON.stringify(form))); this.success({status: "unavailable"}); },
  };
  const browser = vm.createContext({document: {getElementById: id => elements[id], createElement: () => ({appendChild() {}})}, google: {script: {run: runner, host: {close() {}}}}, crypto: crypto.webcrypto, Date: {now: () => moment}, setTimeout() {}, confirm() { throw Error("native confirmation must not be used"); }});
  new vm.Script(script).runInContext(browser);
  const fill = () => {
    elements.title.value = "עסקה סינתטית — בדיקה";
    elements.amount.value = "345.67";
    elements.start.value = "2026-10-12";
    elements.plan.value = "full";
    elements.status.value = "active";
    elements.notes.value = "ללא נתוני לקוח";
  };
  fill();
  browser.saveDeal();
  assert.equal(sent.length, 0, "preview must not dispatch a write");
  assert.equal(elements.review.hidden, false);
  assert.match(elements.reviewSummary.textContent, /סכום העסקה הכולל: ₪345.67/);
  assert.match(elements.reviewSummary.textContent, /גרסה צפויה: 0/);
  assert.ok(["title", "amount", "start", "plan", "status", "notes", "save"].every(id => elements[id].disabled));
  browser.cancelDealReview();
  browser.confirmSaveDeal();
  assert.equal(sent.length, 0, "cancellation cannot authorize a write");
  assert.equal(elements.amount.disabled, false);

  browser.saveDeal();
  moment += 120001;
  browser.confirmSaveDeal();
  assert.equal(sent.length, 0, "expired preview cannot authorize a write");
  assert.match(elements.out.textContent, /תוקף האישור פג/);

  fill();
  browser.saveDeal();
  elements.amount.value = "9999.99";
  elements.title.value = "שינוי לאחר התצוגה";
  browser.confirmSaveDeal();
  browser.confirmSaveDeal();
  browser.saveDeal();
  assert.equal(sent.length, 1, "approval dispatches exactly once even during unknown response");
  assert.equal(sent[0].amount_ils, "345.67", "write must use the displayed frozen amount");
  assert.equal(sent[0].title, "עסקה סינתטית — בדיקה");
  assert.equal(sent[0].notes, "ללא נתוני לקוח");
  assert.equal(elements.amount.disabled, true, "ambiguous dispatch must remain locked");
  assert.match(elements.out.textContent, /תוצאת השמירה אינה ידועה/);
});

test("ambiguous dispatched write stays locked when receipt is not found", () => {
  const userStore = {};
  const rt = runtime({ userStore, throwBatch: true });
  const form = { request_id: "33333333-3333-4333-8333-333333333333", deal_id: DEAL_ID, expected_revision: 0, title: "עסקת TEST", amount_ils: "10.00", payment_plan: "full", start_date: "2026-10-11", status: "active", notes: "" };
  assert.throws(() => vm.runInContext(`controlledSaveDealForm(${JSON.stringify(form)})`, rt.context), /ambiguous provider failure/);
  const marker = userStore.BUSINESS_LEDGER_PENDING_V1;
  assert.ok(marker);
  const lookup = rt.context.checkPendingDealReceipt();
  assert.equal(lookup.status, "not_found");
  assert.equal(userStore.BUSINESS_LEDGER_PENDING_V1, marker, "not_found cannot clear an unknown dispatched write");
  const reopened = rt.context.getDealFormBootstrap();
  assert.equal(reopened.mode, "recovery");
  assert.deepEqual(Array.from(Object.keys(reopened.pending).sort()), ["created_at", "deal_id", "expected_revision", "request_id"]);
  assert.equal(typeof rt.context.abandonPendingDealAfterNotFound, "undefined");
});

test("receipt recovery rejects tampered status, provenance, revision, UUID, and timestamp", () => {
  const mutations = [
    (receipt) => { receipt.status = "ok"; },
    (receipt) => { receipt.source = "other"; },
    (receipt) => { receipt.mode = "LIVE"; },
    (receipt) => { receipt.revision = 99; },
    (receipt) => { receipt.receipt_id = "not-a-uuid"; },
    (receipt) => { receipt.committed_at = "2026-99-99T00:00:00.000Z"; },
  ];
  for (const mutate of mutations) {
    const userStore = {};
    const rt = runtime({ userStore, applyBatch: true });
    const form = { request_id: "33333333-3333-4333-8333-333333333333", deal_id: DEAL_ID, expected_revision: 0, title: "עסקת TEST", amount_ils: "10.00", payment_plan: "full", start_date: "2026-10-11", status: "active", notes: "" };
    const committed = vm.runInContext(`controlledSaveDealForm(${JSON.stringify(form)})`, rt.context);
    assert.equal(committed.status, "ok");
    const auditRow = rt.sheets.Audit.getDataRange().getValues()[1];
    const receipt = JSON.parse(auditRow[8]);
    mutate(receipt);
    auditRow[8] = JSON.stringify(receipt);
    let caught;
    try { rt.context.getDealFormBootstrap(); } catch (error) { caught = error; }
    assert.equal(caught && (caught.ledgerCode || caught.code), "pending_receipt_mismatch");
    assert.ok(userStore.BUSINESS_LEDGER_PENDING_V1, "tampered receipt cannot clear recovery marker");
  }
});

test("pending marker persistence failure aborts before any Sheets write", () => {
  const rt = runtime();
  rt.context.PropertiesService.getUserProperties = () => ({ getProperty: () => null, setProperty: () => { throw new Error("user properties unavailable"); }, deleteProperty() {} });
  const form = { request_id: "33333333-3333-4333-8333-333333333333", deal_id: DEAL_ID, expected_revision: 0, title: "עסקת TEST", amount_ils: "10.00", payment_plan: "full", start_date: "2026-10-11", status: "active", notes: "" };
  assert.throws(() => vm.runInContext(`controlledSaveDealForm(${JSON.stringify(form)})`, rt.context), /user properties unavailable/);
  assert.equal(rt.batchCalls(), 0);
});
