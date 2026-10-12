"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const finance = require("../../ops/business-ledger/finance-core.js");

const NOW = "2026-10-12T10:11:12.000Z";
const IDS = Array.from({ length: 12 }, (_, index) => `${String(index + 1).padStart(8, "0")}-1111-4111-8111-${String(index + 1).padStart(12, "0")}`);

function deal(overrides = {}) {
  return Object.assign({
    id: IDS[0], revision: 1, title: "Website project", amount_minor: 10000,
    currency: "ILS", payment_plan: "installments", start_date: "2026-01-01",
    status: "active", project_id: null, notes: "Approved total", updated_at: NOW,
  }, overrides);
}

function plan(rawDeal = deal(), overrides = {}) {
  return finance.generateChargePlan(Object.assign({
    deal: rawDeal, number_of_charges: 3, first_due_date: "2026-01-31",
    charge_ids: IDS.slice(1, 4), updated_at: NOW,
  }, overrides));
}

function project(overrides = {}) {
  return Object.assign({
    id: IDS[8], revision: 1, title: "Delivery", status: "active",
    deadline: "2026-03-31", notes: "", updated_at: NOW,
  }, overrides);
}

function payment(overrides = {}) {
  return Object.assign({
    id: IDS[4], revision: 1, charge_id: IDS[1], status: "posted",
    amount_minor: 1200, currency: "ILS", received_at: NOW, updated_at: NOW,
  }, overrides);
}

function expense(overrides = {}) {
  return Object.assign({
    id: IDS[6], revision: 1, project_id: null, amount_minor: 500,
    currency: "ILS", occurred_on: "2026-01-15", notes: "Hosting", updated_at: NOW,
  }, overrides);
}

function snapshotArgs(overrides = {}) {
  const savedDeal = deal();
  return Object.assign({
    coverage: "financial_complete", observed_at: NOW, deals: [savedDeal],
    charges: plan(savedDeal), payments: [payment()], expenses: [expense()], projects: [],
  }, overrides);
}

test("splits the entire deal total deterministically without losing an agora", () => {
  const charges = plan(deal({ payment_plan: "monthly" }));
  assert.deepEqual(charges.map((charge) => charge.amount_minor), [3334, 3333, 3333]);
  assert.equal(charges.reduce((sum, charge) => sum + charge.amount_minor, 0), 10000);
  assert.equal(charges.every((charge) => charge.revision === 1 && charge.status === "planned"), true);
});

test("full requires exactly one charge and recurring plans require 2 through 36", () => {
  const full = deal({ payment_plan: "full" });
  const one = plan(full, { number_of_charges: 1, charge_ids: [IDS[1]] });
  assert.equal(one[0].amount_minor, 10000);
  assert.throws(() => plan(full), /invalid_number_of_charges/);
  assert.throws(() => plan(deal({ payment_plan: "monthly" }), { number_of_charges: 1, charge_ids: [IDS[1]] }), /invalid_number_of_charges/);
  assert.throws(() => plan(deal({ payment_plan: "monthly" }), { number_of_charges: 37, charge_ids: Array(37).fill(IDS[1]) }), /invalid_number_of_charges/);
});

test("calendar monthly cadence clamps to each target month including leap February", () => {
  assert.deepEqual(plan().map((charge) => charge.due_date), ["2026-01-31", "2026-02-28", "2026-03-31"]);
  const leap = plan(deal({ start_date: "2024-01-01" }), { first_due_date: "2024-01-31" });
  assert.deepEqual(leap.map((charge) => charge.due_date), ["2024-01-31", "2024-02-29", "2024-03-31"]);
  assert.throws(() => finance.addCalendarMonths("9999-12-31", 1), /date_out_of_range/);
});

test("caller supplies stable unique UUIDs and generation has no hidden random write", () => {
  const original = deal();
  const frozenIds = Object.freeze(IDS.slice(1, 4));
  const frozenDeal = Object.freeze(original);
  const first = finance.generateChargePlan({ deal: frozenDeal, number_of_charges: 3, first_due_date: "2026-01-31", charge_ids: frozenIds, updated_at: NOW });
  const second = finance.generateChargePlan({ deal: frozenDeal, number_of_charges: 3, first_due_date: "2026-01-31", charge_ids: frozenIds, updated_at: NOW });
  assert.deepEqual(first, second);
  assert.deepEqual(first.map((charge) => charge.id), frozenIds);
  assert.throws(() => plan(undefined, { charge_ids: [IDS[1], IDS[1], IDS[2]] }), /duplicate_charge_id/);
});

test("complete totals distinguish deal value, schedule, received cash, expenses and net cash", () => {
  const result = finance.financialSnapshot(snapshotArgs());
  assert.deepEqual(result.totals, {
    deal_value_minor: 10000,
    scheduled_minor: 10000,
    received_minor: 1200,
    outstanding_minor: 8800,
    expense_minor: 500,
    net_cash_minor: 700,
  });
  assert.equal(result.coverage, "financial_complete");
});

test("multiple posted partial payments may reach but never exceed their charge", () => {
  const second = payment({ id: IDS[5], amount_minor: 2134 });
  const exact = finance.financialSnapshot(snapshotArgs({ payments: [payment(), second] }));
  assert.equal(exact.totals.received_minor, 3334);
  assert.equal(exact.totals.outstanding_minor, 6666);
  assert.throws(() => finance.financialSnapshot(snapshotArgs({ payments: [payment(), second, payment({ id: IDS[7], amount_minor: 1 })] })), /payment_exceeds_charge/);
});

test("general and project-linked expenses are accepted only with a valid project reference", () => {
  const linked = expense({ id: IDS[7], project_id: IDS[8] });
  const result = finance.financialSnapshot(snapshotArgs({ projects: [project()], expenses: [expense(), linked] }));
  assert.equal(result.totals.expense_minor, 1000);
  assert.throws(() => finance.financialSnapshot(snapshotArgs({ expenses: [linked] })), /orphan_expense_project/);
  assert.throws(() => finance.canonicalProject(project({ status: "blocked" })), /invalid_project_status/);
  assert.throws(() => finance.canonicalProject(project({ deadline: "2026-02-30" })), /invalid_deadline/);
});

test("unknown coverage returns null totals rather than turning missing records into zero", () => {
  const result = finance.financialSnapshot(snapshotArgs({ coverage: "unknown", charges: [], payments: [], expenses: [] }));
  assert.equal(Object.values(result.totals).every((value) => value === null), true);
  assert.deepEqual(result.reason_codes, ["financial_coverage_incomplete"]);
});

test("complete coverage rejects orphan references and duplicate identifiers", () => {
  assert.throws(() => finance.financialSnapshot(snapshotArgs({ payments: [payment({ charge_id: IDS[10] })] })), /orphan_payment/);
  assert.throws(() => finance.financialSnapshot(snapshotArgs({ charges: plan().concat(plan()[0]) })), /duplicate_charge_id/);
  assert.throws(() => finance.financialSnapshot(snapshotArgs({ deals: [deal({ project_id: IDS[8] })] })), /orphan_deal_project/);
});

test("cancelled charges contribute no schedule and cannot hide already received cash", () => {
  const charges = plan().map((charge) => Object.assign({}, charge, { status: "cancelled", revision: 2 }));
  const cancelledDeal = deal({ status: "cancelled", revision: 2 });
  const result = finance.financialSnapshot(snapshotArgs({ deals: [cancelledDeal], charges, payments: [] }));
  assert.equal(result.totals.deal_value_minor, 0);
  assert.equal(result.totals.scheduled_minor, 0);
  assert.equal(result.totals.outstanding_minor, 0);
  assert.throws(() => finance.financialSnapshot(snapshotArgs({ deals: [cancelledDeal], charges, payments: [payment()] })), /payment_for_cancelled_charge/);
  assert.throws(() => finance.canonicalPayment(payment({ status: "cancelled" })), /invalid_payment_status/);
});

test("strict schemas reject formulas, invalid dates, floats, negatives, bad revisions and extra keys", () => {
  assert.throws(() => finance.canonicalExpense(expense({ notes: "=SUM(A:A)" })), /invalid_notes/);
  assert.throws(() => finance.canonicalExpense(expense({ occurred_on: "2026-02-30" })), /invalid_occurred_on/);
  assert.throws(() => finance.canonicalPayment(payment({ amount_minor: 1.5 })), /invalid_amount_minor/);
  assert.throws(() => finance.canonicalPayment(payment({ amount_minor: -1 })), /invalid_amount_minor/);
  assert.throws(() => finance.canonicalPayment(payment({ revision: 0 })), /invalid_revision/);
  assert.throws(() => finance.canonicalPayment(Object.assign(payment(), { memo: "extra" })), (error) => error.code === "invalid_payment_shape");
  assert.throws(() => plan(deal({ amount_minor: 2 })), /charge_amount_too_small/);
});

test("charge-plan validation detects changed totals, cadence and foreign deal IDs", () => {
  const charges = plan();
  assert.throws(() => finance.validateChargePlan(deal(), charges.map((charge, index) => Object.assign({}, charge, { amount_minor: charge.amount_minor + (index === 0 ? 1 : 0) }))), /charge_total_mismatch/);
  assert.throws(() => finance.validateChargePlan(deal(), charges.map((charge, index) => Object.assign({}, charge, { due_date: index === 1 ? "2026-03-01" : charge.due_date }))), /invalid_charge_cadence/);
  assert.throws(() => finance.validateChargePlan(deal(), charges.map((charge, index) => Object.assign({}, charge, { deal_id: index === 0 ? IDS[9] : charge.deal_id }))), /charge_plan_mismatch/);
});

test("complete snapshots reject a charge plan that begins before the deal", () => {
  const dates = ["2020-01-31", "2020-02-29", "2020-03-31"];
  const historical = plan().map((charge, index) => Object.assign({}, charge, { due_date: dates[index] }));
  assert.throws(() => finance.financialSnapshot(snapshotArgs({ charges: historical, payments: [] })), /first_due_before_start/);
});

test("a deal cancelled before scheduling is complete with zero schedule", () => {
  const result = finance.financialSnapshot(snapshotArgs({
    deals: [deal({ status: "cancelled", revision: 2 })], charges: [], payments: [],
  }));
  assert.equal(result.coverage, "financial_complete");
  assert.deepEqual(result.totals, {
    deal_value_minor: 0,
    scheduled_minor: 0,
    received_minor: 0,
    outstanding_minor: 0,
    expense_minor: 500,
    net_cash_minor: -500,
  });
});

test("an active deal without a plan cannot claim complete coverage", () => {
  assert.throws(() => finance.financialSnapshot(snapshotArgs({ charges: [], payments: [] })), /invalid_charge_plan/);
  const unknown = finance.financialSnapshot(snapshotArgs({
    coverage: "unknown", charges: [], payments: [],
  }));
  assert.equal(unknown.coverage, "unknown");
  assert.equal(unknown.totals.outstanding_minor, null);
});
