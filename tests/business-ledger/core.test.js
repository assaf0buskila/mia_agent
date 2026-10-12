"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const core = require("../../ops/business-ledger/core.js");

const OWNER = "123456789";
const DEAL_ID = "11111111-1111-4111-8111-111111111111";
const REQUEST_ID = "22222222-2222-4222-8222-222222222222";
const NOW = "2026-10-11T00:00:00.000Z";

function deal(overrides = {}) {
  return Object.assign({
    id: DEAL_ID, title: "Website build", amount_minor: 250000, currency: "ILS",
    payment_plan: "installments", start_date: "2026-10-11", status: "active",
    project_id: null, notes: "Confirmed scope",
  }, overrides);
}

function saveData(overrides = {}) {
  return Object.assign({ request_id: REQUEST_ID, expected_revision: 0, deal: deal() }, overrides);
}

function persisted(overrides = {}) {
  return Object.assign({ revision: 1 }, deal(), { updated_at: NOW }, overrides);
}

function digest(data) {
  core.validateSaveData(data);
  return crypto.createHash("sha256").update(core.stableStringify(data)).digest("hex");
}

test("create yields revision one and preserves only canonical literals", () => {
  const data = saveData();
  const result = core.decideSave({ data, owner_id: OWNER, data_hash: digest(data), currentDeal: null, acceptedSnapshot: null, existingReceipt: null, committed_at: NOW });
  assert.equal(result.kind, "commit");
  assert.equal(result.deal.revision, 1);
  assert.deepEqual(Object.keys(result.deal), core.DEAL_KEYS);
});

test("update requires exact accepted snapshot and expected revision", () => {
  const current = persisted({ revision: 3 });
  const data = saveData({ expected_revision: 3, deal: deal({ title: "Revised website" }) });
  const result = core.decideSave({ data, owner_id: OWNER, data_hash: digest(data), currentDeal: current, acceptedSnapshot: current, existingReceipt: null, committed_at: NOW });
  assert.equal(result.kind, "commit");
  assert.equal(result.deal.revision, 4);
  const stale = core.decideSave({ data: Object.assign({}, data, { expected_revision: 2 }), owner_id: OWNER, data_hash: "x", currentDeal: current, acceptedSnapshot: current, existingReceipt: null, committed_at: NOW });
  assert.deepEqual(stale, { kind: "conflict", reason_code: "revision_mismatch" });
});

test("changed raw sheet row fails closed against immutable accepted snapshot", () => {
  const accepted = persisted();
  const changed = Object.assign({}, accepted, { amount_minor: 1 });
  const data = saveData({ expected_revision: 1 });
  const result = core.decideSave({ data, owner_id: OWNER, data_hash: digest(data), currentDeal: changed, acceptedSnapshot: accepted, existingReceipt: null, committed_at: NOW });
  assert.deepEqual(result, { kind: "conflict", reason_code: "deal_row_tampered" });
});

test("lost response replay returns the same receipt for the same owner and payload", () => {
  const data = saveData();
  const receipt = { status: "committed", request_id: REQUEST_ID, receipt_id: "33333333-3333-4333-8333-333333333333" };
  const existingReceipt = { owner_id: OWNER, data_hash: digest(data), receipt };
  const replay = core.decideSave({ data, owner_id: OWNER, data_hash: digest(data), currentDeal: null, acceptedSnapshot: null, existingReceipt });
  assert.equal(replay.kind, "replay");
  assert.strictEqual(replay.receipt, receipt);
});

test("idempotency key cannot cross owner or bind changed payload", () => {
  const data = saveData();
  const stored = { owner_id: OWNER, data_hash: digest(data), receipt: { status: "committed" } };
  assert.equal(core.decideSave({ data, owner_id: "987654321", data_hash: digest(data), existingReceipt: stored }).reason_code, "idempotency_key_reused");
  const changed = saveData({ deal: deal({ notes: "changed" }) });
  assert.equal(core.decideSave({ data: changed, owner_id: OWNER, data_hash: digest(changed), existingReceipt: stored }).reason_code, "idempotency_key_reused");
});

test("rejects formulas, controls, invalid calendar dates, excess and non-integer money", () => {
  const bad = [
    deal({ title: "=IMPORTDATA(\"https://example.test\")" }),
    deal({ notes: "line\nbreak" }),
    deal({ start_date: "2026-02-30" }),
    deal({ amount_minor: 1000000000001 }),
    deal({ amount_minor: 12.5 }),
    deal({ title: " padded" }),
    deal({ notes: "x".repeat(1001) }),
    deal({ id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".toUpperCase() }),
  ];
  bad.forEach((value) => assert.throws(() => core.canonicalDealInput(value)));
});

test("cross-contract revision and accepted timestamp bounds are enforced", () => {
  assert.throws(() => core.validateSaveData(saveData({ expected_revision: 1000001 })), /invalid_expected_revision/);
  assert.throws(() => core.canonicalPersistedDeal(persisted({ revision: 1000001 })), /invalid_revision/);
  assert.throws(() => core.canonicalPersistedDeal(persisted({ updated_at: "2026-10-11" })), /invalid_updated_at/);
});

test("Slice A/B rejects project links until Projects is implemented", () => {
  assert.throws(() => core.canonicalDealInput(deal({ project_id: "55555555-5555-4555-8555-555555555555" })), /project_links_not_supported/);
});

test("snapshot states deals-only coverage and never invents cash totals", () => {
  const snap = core.snapshot([persisted(), persisted({ id: "44444444-4444-4444-8444-444444444444", status: "cancelled" })], NOW);
  assert.equal(snap.totals.deal_value_minor, 250000);
  assert.equal(snap.totals.received_minor, null);
  assert.equal(snap.totals.outstanding_minor, null);
  assert.equal(snap.totals.expense_minor, null);
  assert.deepEqual(snap.reason_codes, ["financial_coverage_incomplete"]);
});
