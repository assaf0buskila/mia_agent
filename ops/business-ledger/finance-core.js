(function (root, factory) {
  const api = factory();
  root.BusinessLedgerFinanceCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const API_VERSION = "business-ledger.finance.v1";
  const SOURCE = "google_sheets";
  const CURRENCY = "ILS";
  const MAX_AMOUNT_MINOR = 1000000000000;
  const MAX_REVISION = 1000000;
  const MAX_ROWS_PER_SET = 5000;
  const MAX_TITLE = 160;
  const MAX_NOTES = 1000;
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
  const TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
  const CONTROL_RE = /[\u0000-\u001f\u007f-\u009f]/;
  const FORMULA_RE = /^\s*[=+\-@]/;

  const DEAL_KEYS = ["id", "revision", "title", "amount_minor", "currency", "payment_plan", "start_date", "status", "project_id", "notes", "updated_at"];
  const CHARGE_KEYS = ["id", "revision", "deal_id", "status", "amount_minor", "currency", "due_date", "updated_at"];
  const PAYMENT_KEYS = ["id", "revision", "charge_id", "status", "amount_minor", "currency", "received_at", "updated_at"];
  const EXPENSE_KEYS = ["id", "revision", "project_id", "amount_minor", "currency", "occurred_on", "notes", "updated_at"];
  const PROJECT_KEYS = ["id", "revision", "title", "status", "deadline", "notes", "updated_at"];

  function fail(code, message) {
    const error = new Error(message || code);
    error.code = code;
    throw error;
  }

  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
  }

  function assertExactKeys(value, keys, code) {
    if (!isPlainObject(value)) fail(code, "object required");
    const actual = Object.keys(value).sort();
    const expected = keys.slice().sort();
    if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) fail(code, "unexpected object shape");
  }

  function canonicalUuid(value, code) {
    if (typeof value !== "string" || !UUID_RE.test(value)) fail(code || "invalid_uuid", "canonical UUID required");
    return value;
  }

  function canonicalRevision(value) {
    if (!Number.isSafeInteger(value) || value < 1 || value > MAX_REVISION) fail("invalid_revision");
    return value;
  }

  function canonicalAmount(value, code) {
    if (!Number.isSafeInteger(value) || value <= 0 || value > MAX_AMOUNT_MINOR) fail(code || "invalid_amount_minor");
    return value;
  }

  function canonicalLiteral(value, max, required, code) {
    if (typeof value !== "string" || value !== value.trim() || value.length > max || (required && value.length === 0) || CONTROL_RE.test(value) || FORMULA_RE.test(value)) fail(code);
    return value;
  }

  function canonicalDate(value, nullable, code) {
    if (nullable && value === null) return null;
    const match = typeof value === "string" && DATE_RE.exec(value);
    if (!match) fail(code || "invalid_date");
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const date = new Date(Date.UTC(year, month - 1, day));
    if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) fail(code || "invalid_date");
    return value;
  }

  function canonicalTimestamp(value, code) {
    if (typeof value !== "string" || !TIMESTAMP_RE.test(value)) fail(code || "invalid_timestamp");
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime()) || parsed.toISOString() !== value) fail(code || "invalid_timestamp");
    return value;
  }

  function canonicalCurrency(value) {
    if (value !== CURRENCY) fail("invalid_currency");
    return value;
  }

  function canonicalDeal(raw) {
    assertExactKeys(raw, DEAL_KEYS, "invalid_deal_shape");
    if (!["full", "installments", "monthly"].includes(raw.payment_plan)) fail("invalid_payment_plan");
    if (!["active", "cancelled"].includes(raw.status)) fail("invalid_deal_status");
    return {
      id: canonicalUuid(raw.id, "invalid_deal_id"),
      revision: canonicalRevision(raw.revision),
      title: canonicalLiteral(raw.title, MAX_TITLE, true, "invalid_title"),
      amount_minor: canonicalAmount(raw.amount_minor),
      currency: canonicalCurrency(raw.currency),
      payment_plan: raw.payment_plan,
      start_date: canonicalDate(raw.start_date, false, "invalid_start_date"),
      status: raw.status,
      project_id: raw.project_id === null ? null : canonicalUuid(raw.project_id, "invalid_project_id"),
      notes: canonicalLiteral(raw.notes, MAX_NOTES, false, "invalid_notes"),
      updated_at: canonicalTimestamp(raw.updated_at, "invalid_updated_at"),
    };
  }

  function canonicalCharge(raw) {
    assertExactKeys(raw, CHARGE_KEYS, "invalid_charge_shape");
    if (!["planned", "cancelled"].includes(raw.status)) fail("invalid_charge_status");
    return {
      id: canonicalUuid(raw.id, "invalid_charge_id"),
      revision: canonicalRevision(raw.revision),
      deal_id: canonicalUuid(raw.deal_id, "invalid_deal_id"),
      status: raw.status,
      amount_minor: canonicalAmount(raw.amount_minor),
      currency: canonicalCurrency(raw.currency),
      due_date: canonicalDate(raw.due_date, false, "invalid_due_date"),
      updated_at: canonicalTimestamp(raw.updated_at, "invalid_updated_at"),
    };
  }

  function canonicalPayment(raw) {
    assertExactKeys(raw, PAYMENT_KEYS, "invalid_payment_shape");
    // Slice C records only immutable, manually confirmed receipts. Corrections must
    // be separate audited records in a later slice; a cancelled payment cannot erase cash.
    if (raw.status !== "posted") fail("invalid_payment_status");
    return {
      id: canonicalUuid(raw.id, "invalid_payment_id"),
      revision: canonicalRevision(raw.revision),
      charge_id: canonicalUuid(raw.charge_id, "invalid_charge_id"),
      status: "posted",
      amount_minor: canonicalAmount(raw.amount_minor),
      currency: canonicalCurrency(raw.currency),
      received_at: canonicalTimestamp(raw.received_at, "invalid_received_at"),
      updated_at: canonicalTimestamp(raw.updated_at, "invalid_updated_at"),
    };
  }

  function canonicalExpense(raw) {
    assertExactKeys(raw, EXPENSE_KEYS, "invalid_expense_shape");
    return {
      id: canonicalUuid(raw.id, "invalid_expense_id"),
      revision: canonicalRevision(raw.revision),
      project_id: raw.project_id === null ? null : canonicalUuid(raw.project_id, "invalid_project_id"),
      amount_minor: canonicalAmount(raw.amount_minor),
      currency: canonicalCurrency(raw.currency),
      occurred_on: canonicalDate(raw.occurred_on, false, "invalid_occurred_on"),
      notes: canonicalLiteral(raw.notes, MAX_NOTES, false, "invalid_notes"),
      updated_at: canonicalTimestamp(raw.updated_at, "invalid_updated_at"),
    };
  }

  function canonicalProject(raw) {
    assertExactKeys(raw, PROJECT_KEYS, "invalid_project_shape");
    if (!["planned", "active", "completed", "cancelled"].includes(raw.status)) fail("invalid_project_status");
    return {
      id: canonicalUuid(raw.id, "invalid_project_id"),
      revision: canonicalRevision(raw.revision),
      title: canonicalLiteral(raw.title, MAX_TITLE, true, "invalid_title"),
      status: raw.status,
      deadline: canonicalDate(raw.deadline, true, "invalid_deadline"),
      notes: canonicalLiteral(raw.notes, MAX_NOTES, false, "invalid_notes"),
      updated_at: canonicalTimestamp(raw.updated_at, "invalid_updated_at"),
    };
  }

  function daysInMonth(year, monthIndex) {
    return new Date(Date.UTC(year, monthIndex + 1, 0)).getUTCDate();
  }

  function addCalendarMonths(value, months) {
    canonicalDate(value, false, "invalid_date");
    if (!Number.isSafeInteger(months) || months < 0 || months > 35) fail("invalid_month_offset");
    const match = DATE_RE.exec(value);
    const originalDay = Number(match[3]);
    const absoluteMonth = Number(match[1]) * 12 + Number(match[2]) - 1 + months;
    const year = Math.floor(absoluteMonth / 12);
    if (year > 9999) fail("date_out_of_range");
    const monthIndex = absoluteMonth % 12;
    const day = Math.min(originalDay, daysInMonth(year, monthIndex));
    return String(year).padStart(4, "0") + "-" + String(monthIndex + 1).padStart(2, "0") + "-" + String(day).padStart(2, "0");
  }

  function chargeCountForDeal(deal, count) {
    if (!Number.isSafeInteger(count)) fail("invalid_number_of_charges");
    if (deal.payment_plan === "full" && count !== 1) fail("invalid_number_of_charges");
    if (deal.payment_plan !== "full" && (count < 2 || count > 36)) fail("invalid_number_of_charges");
    if (count > deal.amount_minor) fail("charge_amount_too_small");
    return count;
  }

  function safeAdd(left, right, code) {
    const value = left + right;
    if (!Number.isSafeInteger(value)) fail(code || "total_overflow");
    return value;
  }

  function assertUnique(records, code) {
    const ids = new Set();
    records.forEach(function (record) {
      if (ids.has(record.id)) fail(code || "duplicate_id");
      ids.add(record.id);
    });
    return ids;
  }

  function validateChargePlan(rawDeal, rawCharges) {
    const deal = canonicalDeal(rawDeal);
    if (!Array.isArray(rawCharges) || rawCharges.length < 1 || rawCharges.length > 36) fail("invalid_charge_plan");
    const charges = rawCharges.map(canonicalCharge);
    assertUnique(charges, "duplicate_charge_id");
    if (charges[0].due_date < deal.start_date) fail("first_due_before_start");
    let total = 0;
    charges.forEach(function (charge, index) {
      if (charge.deal_id !== deal.id || charge.currency !== deal.currency) fail("charge_plan_mismatch");
      if (index > 0 && charge.due_date !== addCalendarMonths(charges[0].due_date, index)) fail("invalid_charge_cadence");
      total = safeAdd(total, charge.amount_minor, "charge_total_overflow");
    });
    chargeCountForDeal(deal, charges.length);
    if (total !== deal.amount_minor) fail("charge_total_mismatch");
    return charges;
  }

  function generateChargePlan(args) {
    assertExactKeys(args, ["deal", "number_of_charges", "first_due_date", "charge_ids", "updated_at"], "invalid_plan_shape");
    const deal = canonicalDeal(args.deal);
    if (deal.status !== "active") fail("cancelled_deal");
    const count = chargeCountForDeal(deal, args.number_of_charges);
    const firstDueDate = canonicalDate(args.first_due_date, false, "invalid_first_due_date");
    canonicalTimestamp(args.updated_at, "invalid_updated_at");
    if (!Array.isArray(args.charge_ids) || args.charge_ids.length !== count) fail("invalid_charge_ids");
    const ids = args.charge_ids.map(function (id) { return canonicalUuid(id, "invalid_charge_id"); });
    if (new Set(ids).size !== ids.length) fail("duplicate_charge_id");
    const base = Math.floor(deal.amount_minor / count);
    const remainder = deal.amount_minor % count;
    const charges = ids.map(function (id, index) {
      return {
        id: id,
        revision: 1,
        deal_id: deal.id,
        status: "planned",
        amount_minor: base + (index < remainder ? 1 : 0),
        currency: CURRENCY,
        due_date: addCalendarMonths(firstDueDate, index),
        updated_at: args.updated_at,
      };
    });
    return validateChargePlan(deal, charges);
  }

  function boundedSet(raw, canonicalizer, name) {
    if (!Array.isArray(raw) || raw.length > MAX_ROWS_PER_SET) fail("invalid_" + name + "_set");
    const records = raw.map(canonicalizer);
    assertUnique(records, "duplicate_" + name + "_id");
    return records;
  }

  function financialSnapshot(args) {
    assertExactKeys(args, ["coverage", "observed_at", "deals", "charges", "payments", "expenses", "projects"], "invalid_snapshot_shape");
    const observedAt = canonicalTimestamp(args.observed_at, "invalid_observed_at");
    if (!["financial_complete", "unknown"].includes(args.coverage)) fail("invalid_coverage");
    const deals = boundedSet(args.deals, canonicalDeal, "deal");
    const charges = boundedSet(args.charges, canonicalCharge, "charge");
    const payments = boundedSet(args.payments, canonicalPayment, "payment");
    const expenses = boundedSet(args.expenses, canonicalExpense, "expense");
    const projects = boundedSet(args.projects, canonicalProject, "project");
    const counts = { deals: deals.length, charges: charges.length, payments: payments.length, expenses: expenses.length, projects: projects.length };

    if (args.coverage === "unknown") {
      return {
        api_version: API_VERSION, source: SOURCE, currency: CURRENCY, observed_at: observedAt,
        coverage: "unknown", counts: counts,
        totals: { deal_value_minor: null, scheduled_minor: null, received_minor: null, outstanding_minor: null, expense_minor: null, net_cash_minor: null },
        reason_codes: ["financial_coverage_incomplete"],
      };
    }

    const projectIds = new Set(projects.map(function (project) { return project.id; }));
    const dealById = new Map(deals.map(function (deal) { return [deal.id, deal]; }));
    const chargeById = new Map(charges.map(function (charge) { return [charge.id, charge]; }));
    deals.forEach(function (deal) {
      if (deal.project_id !== null && !projectIds.has(deal.project_id)) fail("orphan_deal_project");
      const plan = charges.filter(function (charge) { return charge.deal_id === deal.id; });
      if (deal.status === "cancelled" && plan.length === 0) return;
      validateChargePlan(deal, plan);
      if (deal.status === "cancelled" && plan.some(function (charge) { return charge.status !== "cancelled"; })) fail("cancelled_deal_has_planned_charge");
    });
    charges.forEach(function (charge) { if (!dealById.has(charge.deal_id)) fail("orphan_charge"); });
    expenses.forEach(function (expense) { if (expense.project_id !== null && !projectIds.has(expense.project_id)) fail("orphan_expense_project"); });

    const paidByCharge = new Map();
    payments.forEach(function (payment) {
      const charge = chargeById.get(payment.charge_id);
      if (!charge) fail("orphan_payment");
      if (charge.status === "cancelled") fail("payment_for_cancelled_charge");
      const paid = safeAdd(paidByCharge.get(charge.id) || 0, payment.amount_minor, "payment_total_overflow");
      if (paid > charge.amount_minor) fail("payment_exceeds_charge");
      paidByCharge.set(charge.id, paid);
    });

    let dealValue = 0;
    deals.forEach(function (deal) { if (deal.status === "active") dealValue = safeAdd(dealValue, deal.amount_minor); });
    let scheduled = 0;
    let received = 0;
    let outstanding = 0;
    charges.forEach(function (charge) {
      if (charge.status !== "planned") return;
      const paid = paidByCharge.get(charge.id) || 0;
      scheduled = safeAdd(scheduled, charge.amount_minor);
      received = safeAdd(received, paid);
      outstanding = safeAdd(outstanding, Math.max(charge.amount_minor - paid, 0));
    });
    let expense = 0;
    expenses.forEach(function (record) { expense = safeAdd(expense, record.amount_minor); });
    const netCash = received - expense;
    if (!Number.isSafeInteger(netCash)) fail("net_cash_overflow");
    return {
      api_version: API_VERSION, source: SOURCE, currency: CURRENCY, observed_at: observedAt,
      coverage: "financial_complete", counts: counts,
      totals: {
        deal_value_minor: dealValue,
        scheduled_minor: scheduled,
        received_minor: received,
        outstanding_minor: outstanding,
        expense_minor: expense,
        net_cash_minor: netCash,
      },
      reason_codes: [],
    };
  }

  return {
    API_VERSION, SOURCE, CURRENCY, MAX_AMOUNT_MINOR, MAX_REVISION, MAX_ROWS_PER_SET,
    DEAL_KEYS, CHARGE_KEYS, PAYMENT_KEYS, EXPENSE_KEYS, PROJECT_KEYS,
    addCalendarMonths, canonicalCharge, canonicalDeal, canonicalExpense, canonicalPayment,
    canonicalProject, financialSnapshot, generateChargePlan, validateChargePlan,
  };
});
