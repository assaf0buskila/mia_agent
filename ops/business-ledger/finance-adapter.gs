/* global BusinessLedgerCore, BusinessLedgerFinanceCore, Sheets */
"use strict";

const FINANCE_NAMES = ["Charges", "Payments", "Expenses", "Projects"];
const FINANCE_VALIDATORS = {
  Charges: "canonicalCharge", Payments: "canonicalPayment", Expenses: "canonicalExpense", Projects: "canonicalProject",
};
const MAX_FINANCE_ROWS = 500;

function exactFinanceKeys_(value, keys) {
  if (!plainObject_(value) || Object.keys(value).sort().join(",") !== keys.slice().sort().join(",")) throw ledgerError_("invalid_finance_shape");
}
function financeRevision_(value, allowZero) {
  if (!Number.isSafeInteger(value) || value < (allowZero ? 0 : 1) || value > BusinessLedgerCore.MAX_REVISION) throw ledgerError_("invalid_expected_revision");
}
function validateFinanceData_(operation, data) {
  const now = new Date().toISOString();
  if (operation === "finance_snapshot") { exactFinanceKeys_(data, []); return; }
  BusinessLedgerCore.uuid(data.request_id, "invalid_request_id");
  if (operation === "save_schedule") {
    exactFinanceKeys_(data, ["request_id", "expected_revision", "deal_id", "number_of_charges", "first_due_date", "charge_ids"]);
    BusinessLedgerCore.uuid(data.deal_id, "invalid_deal_id"); financeRevision_(data.expected_revision, false);
    if (!Number.isSafeInteger(data.number_of_charges) || data.number_of_charges < 1 || data.number_of_charges > 36 || !Array.isArray(data.charge_ids) || data.charge_ids.length !== data.number_of_charges) throw ledgerError_("invalid_charge_plan");
    data.charge_ids.forEach(function(id) { BusinessLedgerCore.uuid(id, "invalid_charge_id"); });
    if (new Set(data.charge_ids).size !== data.charge_ids.length) throw ledgerError_("duplicate_charge_id");
    BusinessLedgerFinanceCore.addCalendarMonths(data.first_due_date, 0); return;
  }
  if (operation === "record_payment") {
    exactFinanceKeys_(data, ["request_id", "expected_charge_revision", "confirmed_received", "payment"]);
    financeRevision_(data.expected_charge_revision, false);
    if (data.confirmed_received !== true) throw ledgerError_("manual_confirmation_required");
    exactFinanceKeys_(data.payment, ["id", "charge_id", "amount_minor", "currency", "received_at"]);
    BusinessLedgerFinanceCore.canonicalPayment(Object.assign({ revision: 1, status: "posted", updated_at: now }, data.payment)); return;
  }
  if (operation === "record_expense") {
    exactFinanceKeys_(data, ["request_id", "expected_revision", "expense"]);
    if (data.expected_revision !== 0) throw ledgerError_("financial_adjustment_required");
    exactFinanceKeys_(data.expense, ["id", "project_id", "amount_minor", "currency", "occurred_on", "notes"]);
    BusinessLedgerFinanceCore.canonicalExpense(Object.assign({ revision: 1, updated_at: now }, data.expense)); return;
  }
  if (operation === "save_project") {
    exactFinanceKeys_(data, ["request_id", "expected_revision", "project"]);
    financeRevision_(data.expected_revision, true);
    exactFinanceKeys_(data.project, ["id", "title", "status", "deadline", "notes"]);
    BusinessLedgerFinanceCore.canonicalProject(Object.assign({ revision: data.expected_revision + 1, updated_at: now }, data.project)); return;
  }
  throw ledgerError_("invalid_operation");
}

function mergeFinanceAuditRow_(row, accepted, receipts, financeAccepted) {
  let bundle, receipt;
  try { bundle = JSON.parse(row[7]); receipt = JSON.parse(row[8]); } catch (_) { throw ledgerError_("audit_corrupt"); }
  exactFinanceKeys_(bundle, ["records"]);
  if (!Array.isArray(bundle.records) || !bundle.records.length || bundle.records.length > 37) throw ledgerError_("audit_corrupt");
  const requestId = BusinessLedgerCore.uuid(String(row[4]), "audit_corrupt");
  if (receipts[requestId]) throw ledgerError_("duplicate_receipt_key");
  exactFinanceKeys_(receipt, ["status", "request_id", "id", "revision", "receipt_id", "committed_at", "source", "mode"]);
  if (receipt.status !== "committed" || receipt.mode !== "TEST" || receipt.source !== "google_sheets" || receipt.request_id !== requestId || receipt.id !== row[2] || receipt.revision !== Number(row[3]) || receipt.committed_at !== row[9] || !/^[1-9][0-9]{0,19}$/.test(String(row[5])) || !/^[0-9a-f]{64}$/.test(String(row[6]))) throw ledgerError_("audit_corrupt");
  BusinessLedgerCore.uuid(receipt.receipt_id, "audit_corrupt");
  let anchor = false;
  const seen = new Set();
  bundle.records.forEach(function(entry) {
    exactFinanceKeys_(entry, ["type", "snapshot"]);
    const name = entry.type;
    const validate = name === "Deals" ? "canonicalDeal" : FINANCE_VALIDATORS[name];
    if (!validate) throw ledgerError_("audit_corrupt");
    const snapshot = BusinessLedgerFinanceCore[validate](entry.snapshot);
    const key = name + ":" + snapshot.id;
    if (seen.has(key) || snapshot.updated_at !== row[9]) throw ledgerError_("audit_corrupt");
    seen.add(key);
    if (snapshot.id === receipt.id && snapshot.revision === receipt.revision) anchor = true;
    const target = name === "Deals" ? accepted : financeAccepted[name];
    if (!target[snapshot.id] || snapshot.revision > target[snapshot.id].revision) target[snapshot.id] = snapshot;
  });
  if (!anchor) throw ledgerError_("audit_corrupt");
  receipts[requestId] = { owner_id: String(row[5]), data_hash: String(row[6]), receipt: receipt };
}

function readFinanceState_(config, base) {
  const state = base || readState_(config);
  if (firstTamper_(state)) throw ledgerError_("deal_row_tampered");
  const workbook = openVerifiedWorkbook_(config, false);
  state.financeRows = {}; state.financeById = {}; state.financeRowById = {}; state.financeNextRow = {};
  FINANCE_NAMES.forEach(function(name) {
    const resolved = resolveSheet_(workbook, name), sheet = resolved.sheet;
    const rows = sheet.getDataRange().getValues();
    if (rows.length > MAX_FINANCE_ROWS + 1) throw ledgerError_("coverage_limit_exceeded");
    assertResolvedHeaders_(rows[0], resolved, name);
    const records = [], byId = {}, rowById = {};
    for (let i = 1; i < rows.length; i += 1) {
      if (rows[i].every(function(cell) { return cell === ""; })) continue;
      const object = {};
      LEDGER_TABS[name].forEach(function(key, index) {
        let value = rows[i][index];
        if (key === "revision" || key === "amount_minor") value = Number(value);
        else if (key === "project_id" || key === "deadline") value = value === "" || value === null ? null : String(value);
        else if (key === "due_date" || key === "occurred_on") value = sheetDate_(value);
        else value = String(value);
        object[key] = value;
      });
      let record;
      try { record = BusinessLedgerFinanceCore[FINANCE_VALIDATORS[name]](object); } catch (_) { throw ledgerError_("finance_row_tampered"); }
      if (byId[record.id]) throw ledgerError_("finance_row_tampered");
      byId[record.id] = record; rowById[record.id] = i + 1; records.push(record);
    }
    const accepted = state.financeAccepted[name];
    const ids = new Set(Object.keys(byId).concat(Object.keys(accepted)));
    ids.forEach(function(id) { if (!byId[id] || !accepted[id] || BusinessLedgerCore.stableStringify(byId[id]) !== BusinessLedgerCore.stableStringify(accepted[id])) throw ledgerError_("finance_row_tampered"); });
    state.financeRows[name] = records; state.financeById[name] = byId; state.financeRowById[name] = rowById;
    state.financeNextRow[name] = Math.max(sheet.getLastRow() + 1, 2); state.sheetIds[name] = sheet.getSheetId();
  });
  financeSnapshotFromState_(state, new Date().toISOString());
  return state;
}

function financeSnapshotFromState_(state, now) {
  const rows = state.financeRows;
  const projects = new Set(rows.Projects.map(function(record) { return record.id; }));
  const deals = new Map(state.deals.map(function(record) { return [record.id, record]; }));
  const charges = new Map(rows.Charges.map(function(record) { return [record.id, record]; }));
  const paid = {};
  rows.Charges.forEach(function(record) { if (!deals.has(record.deal_id)) throw ledgerError_("orphan_charge"); });
  rows.Payments.forEach(function(record) {
    const charge = charges.get(record.charge_id);
    if (!charge || charge.status !== "planned") throw ledgerError_("orphan_payment");
    paid[charge.id] = (paid[charge.id] || 0) + record.amount_minor;
    if (!Number.isSafeInteger(paid[charge.id]) || paid[charge.id] > charge.amount_minor) throw ledgerError_("payment_exceeds_charge");
  });
  rows.Expenses.forEach(function(record) { if (record.project_id !== null && !projects.has(record.project_id)) throw ledgerError_("orphan_expense_project"); });
  let complete = true;
  state.deals.forEach(function(deal) {
    const plan = rows.Charges.filter(function(record) { return record.deal_id === deal.id; });
    if (!plan.length) { if (deal.status === "active") complete = false; return; }
    BusinessLedgerFinanceCore.validateChargePlan(deal, plan);
    if (deal.status === "cancelled" && plan.some(function(record) { return record.status !== "cancelled"; })) throw ledgerError_("cancelled_deal_has_planned_charge");
  });
  const result = BusinessLedgerFinanceCore.financialSnapshot({coverage: complete ? "financial_complete" : "unknown", observed_at: now, deals: state.deals, charges: rows.Charges, payments: rows.Payments, expenses: rows.Expenses, projects: rows.Projects});
  return Object.assign(result, {mode: "TEST", deals: state.deals, charges: rows.Charges, payments: rows.Payments, expenses: rows.Expenses, projects: rows.Projects});
}

function guardScheduledDealChange_(state, deal, config) {
  const financial = readFinanceState_(config, state);
  const current = state.byId[deal.id];
  if (current && financial.financeRows.Charges.some(function(record) { return record.deal_id === deal.id; })) {
    ["amount_minor", "currency", "payment_plan", "start_date", "status", "project_id"].forEach(function(key) {
      if (deal[key] !== current[key]) throw ledgerError_("financial_adjustment_required");
    });
  }
  return financial;
}

function financeOperation_(request, config) {
  const state = readFinanceState_(config);
  const now = new Date().toISOString();
  if (request.operation === "finance_snapshot") {
    const snapshot = financeSnapshotFromState_(state, now);
    if (snapshotEnvelopeBytes_(snapshot, config) > MAX_SIGNED_RESPONSE_BYTES) return {status: "unavailable", result: {reason_code: "snapshot_capacity_exceeded"}};
    return {status: "ok", result: snapshot};
  }
  const data = request.data;
  const hash = sha256Hex_(BusinessLedgerCore.stableStringify({operation: request.operation, data: data}));
  const existing = state.receipts[data.request_id];
  if (existing) {
    if (existing.owner_id !== request.owner_id || existing.data_hash !== hash) return {status: "invalid", result: {reason_code: "idempotency_key_reused"}};
    return {status: "ok", result: existing.receipt};
  }
  let records, anchor;
  if (request.operation === "save_schedule") {
    const deal = state.byId[data.deal_id];
    if (!deal || deal.revision !== data.expected_revision) return {status: "conflict", result: {reason_code: "revision_conflict"}};
    if (state.financeRows.Charges.some(function(record) { return record.deal_id === deal.id; })) return {status: "conflict", result: {reason_code: "financial_adjustment_required"}};
    if (data.charge_ids.some(function(id) { return state.financeById.Charges[id]; })) return {status: "conflict", result: {reason_code: "duplicate_charge_id"}};
    anchor = BusinessLedgerFinanceCore.canonicalDeal(Object.assign({}, deal, {revision: deal.revision + 1, updated_at: now}));
    const plan = BusinessLedgerFinanceCore.generateChargePlan({deal: anchor, number_of_charges: data.number_of_charges, first_due_date: data.first_due_date, charge_ids: data.charge_ids, updated_at: now});
    records = [{type: "Deals", snapshot: anchor}].concat(plan.map(function(charge) { return {type: "Charges", snapshot: charge}; }));
  } else if (request.operation === "record_payment") {
    const charge = state.financeById.Charges[data.payment.charge_id];
    if (!charge || charge.revision !== data.expected_charge_revision || charge.status !== "planned") return {status: "conflict", result: {reason_code: "revision_conflict"}};
    if (state.financeById.Payments[data.payment.id]) return {status: "conflict", result: {reason_code: "financial_adjustment_required"}};
    if (data.payment.received_at > now) throw ledgerError_("invalid_received_at");
    anchor = BusinessLedgerFinanceCore.canonicalPayment(Object.assign({revision: 1, status: "posted", updated_at: now}, data.payment));
    const updatedCharge = BusinessLedgerFinanceCore.canonicalCharge(Object.assign({}, charge, {revision: charge.revision + 1, updated_at: now}));
    records = [{type: "Payments", snapshot: anchor}, {type: "Charges", snapshot: updatedCharge}];
  } else if (request.operation === "record_expense") {
    if (state.financeById.Expenses[data.expense.id]) return {status: "conflict", result: {reason_code: "financial_adjustment_required"}};
    anchor = BusinessLedgerFinanceCore.canonicalExpense(Object.assign({revision: 1, updated_at: now}, data.expense));
    records = [{type: "Expenses", snapshot: anchor}];
  } else {
    const current = state.financeById.Projects[data.project.id];
    if ((current ? current.revision : 0) !== data.expected_revision) return {status: "conflict", result: {reason_code: "revision_conflict"}};
    anchor = BusinessLedgerFinanceCore.canonicalProject(Object.assign({revision: data.expected_revision + 1, updated_at: now}, data.project));
    records = [{type: "Projects", snapshot: anchor}];
  }
  return commitFinance_(state, records, anchor, data.request_id, request.owner_id, hash, now, config);
}

function commitFinance_(state, records, anchor, requestId, ownerId, hash, now, config) {
  const next = Object.assign({}, state, {deals: state.deals.slice(), financeRows: {}});
  FINANCE_NAMES.forEach(function(name) { next.financeRows[name] = state.financeRows[name].slice(); });
  const rowCounters = Object.assign({}, state.financeNextRow), requests = [];
  records.forEach(function(entry) {
    const name = entry.type, record = entry.snapshot;
    const target = name === "Deals" ? next.deals : next.financeRows[name];
    const index = target.findIndex(function(item) { return item.id === record.id; });
    if (index === -1) target.push(record); else target[index] = record;
    const row = name === "Deals" ? state.rowById[record.id] : (state.financeRowById[name][record.id] || rowCounters[name]++);
    requests.push(updateRowRequest_(state.sheetIds[name], row, LEDGER_TABS[name].map(function(key) { return record[key] === null ? "" : record[key]; })));
  });
  if (state.auditCount >= MAX_AUDIT_ROWS || FINANCE_NAMES.some(function(name) { return next.financeRows[name].length > MAX_FINANCE_ROWS; })) return {status: "conflict", result: {reason_code: "coverage_limit_exceeded"}};
  const snapshot = financeSnapshotFromState_(next, now);
  if (snapshotEnvelopeBytes_(snapshot, config) > MAX_SIGNED_RESPONSE_BYTES) return {status: "conflict", result: {reason_code: "snapshot_capacity_exceeded"}};
  const receipt = {status: "committed", request_id: requestId, id: anchor.id, revision: anchor.revision, receipt_id: Utilities.getUuid().toLowerCase(), committed_at: now, source: "google_sheets", mode: "TEST"};
  const bundle = BusinessLedgerCore.stableStringify({records: records});
  if (utf8Bytes_(bundle) > 40000) return {status: "conflict", result: {reason_code: "audit_capacity_exceeded"}};
  requests.push(updateRowRequest_(state.sheetIds.Audit, state.nextAuditRow, ["accepted", "finance", anchor.id, anchor.revision, requestId, ownerId, hash, bundle, JSON.stringify(receipt), now]));
  Sheets.Spreadsheets.batchUpdate({requests: requests}, config.workbookId);
  return {status: "ok", result: receipt};
}
