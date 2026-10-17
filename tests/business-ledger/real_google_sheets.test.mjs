import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

const enabled = process.env.RUN_BUSINESS_LEDGER_GOOGLE_TEST === "YES_I_APPROVE_TEST_WRITES";
const required = ["MIA_BUSINESS_LEDGER_TEST_ENABLED", "MIA_BUSINESS_LEDGER_SCRIPT_URL", "MIA_BUSINESS_LEDGER_WORKSPACE_ID", "BUSINESS_LEDGER_TEST_OWNER_ID", "MIA_BUSINESS_LEDGER_HMAC_SECRET"];

function uuid() { return crypto.randomUUID(); }
function sign(payload) { return crypto.createHmac("sha256", process.env.MIA_BUSINESS_LEDGER_HMAC_SECRET).update(payload).digest("hex"); }
function assertReceipt(response, change, revision) {
  assert.equal(response.status, "ok");
  const receipt = response.result;
  assert.equal(receipt.status, "committed");
  assert.equal(receipt.request_id, change.request_id);
  assert.equal(receipt.id, change.deal.id);
  assert.equal(receipt.revision, revision);
  assert.equal(receipt.source, "google_sheets");
  assert.equal(receipt.mode, "TEST");
  assert.match(receipt.receipt_id, /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.ok(Number.isFinite(Date.parse(receipt.committed_at)));
}
async function call(operation, data) {
  const request = {
    request_id: uuid(), timestamp: Math.floor(Date.now() / 1000),
    workspace_id: process.env.MIA_BUSINESS_LEDGER_WORKSPACE_ID,
    owner_id: process.env.BUSINESS_LEDGER_TEST_OWNER_ID, operation, data,
  };
  const payload = JSON.stringify(request);
  const response = await fetch(process.env.MIA_BUSINESS_LEDGER_SCRIPT_URL, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ payload, signature: sign(payload) }), redirect: "follow", signal: AbortSignal.timeout(30000),
  });
  assert.equal(response.status, 200);
  const envelope = await response.json();
  assert.equal(typeof envelope.payload, "string");
  assert.equal(typeof envelope.signature, "string");
  const expected = sign(envelope.payload);
  assert.equal(envelope.signature.length, expected.length);
  assert.ok(crypto.timingSafeEqual(Buffer.from(envelope.signature), Buffer.from(expected)));
  const result = JSON.parse(envelope.payload);
  assert.equal(result.request_id, request.request_id);
  assert.equal(result.workspace_id, request.workspace_id);
  assert.equal(result.owner_id, request.owner_id);
  return result;
}

test("explicit operator-approved TEST workbook roundtrip, receipt replay, and conflict", { skip: !enabled }, async () => {
  for (const key of required) assert.ok(process.env[key], `${key} is required`);
  assert.match(process.env.BUSINESS_LEDGER_TEST_OWNER_ID, /^[0-9]+$/);
  assert.equal(process.env.MIA_BUSINESS_LEDGER_TEST_ENABLED, "true", "backend TEST gate must also be enabled explicitly");
  const endpoint = new URL(process.env.MIA_BUSINESS_LEDGER_SCRIPT_URL);
  assert.equal(endpoint.origin, "https://script.google.com");
  assert.match(endpoint.pathname, /^\/macros\/s\/[A-Za-z0-9_-]{20,256}\/exec$/);
  assert.equal(endpoint.search + endpoint.hash + endpoint.username + endpoint.password, "");
  assert.ok(!/prod/i.test(process.env.MIA_BUSINESS_LEDGER_WORKSPACE_ID), "workspace id must not identify production");
  const dealId = uuid(), key = uuid();
  const create = { request_id: key, expected_revision: 0, deal: { id: dealId, title: "Synthetic TEST deal", amount_minor: 12345, currency: "ILS", payment_plan: "full", start_date: new Date().toISOString().slice(0, 10), status: "active", project_id: null, notes: "Synthetic integration test" } };
  const committed = await call("save_deal", create);
  assertReceipt(committed, create, 1);
  const replay = await call("save_deal", create);
  assertReceipt(replay, create, 1);
  assert.deepEqual(replay.result, committed.result);
  const receipt = await call("receipt", { request_id: key });
  assertReceipt(receipt, create, 1);
  assert.deepEqual(receipt.result, committed.result);
  const edited = { ...create, request_id: uuid(), expected_revision: 1, deal: { ...create.deal, amount_minor: 23456, notes: "Synthetic accepted revision edit" } };
  const updated = await call("save_deal", edited);
  assertReceipt(updated, edited, 2);
  const editReplay = await call("save_deal", edited);
  assertReceipt(editReplay, edited, 2);
  assert.deepEqual(editReplay.result, updated.result);
  const editReceipt = await call("receipt", { request_id: edited.request_id });
  assertReceipt(editReceipt, edited, 2);
  assert.deepEqual(editReceipt.result, updated.result);
  const stale = await call("save_deal", { ...create, request_id: uuid(), expected_revision: 0, deal: { ...create.deal, notes: "stale update" } });
  assert.equal(stale.status, "conflict");
  const snapshot = await call("snapshot", {});
  assert.equal(snapshot.status, "ok");
  const rows = snapshot.result.deals.filter((row) => row.id === dealId);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].revision, 2);
  const { revision, updated_at, ...canonicalDeal } = rows[0];
  assert.equal(revision, 2);
  assert.ok(Number.isFinite(Date.parse(updated_at)));
  assert.deepEqual(canonicalDeal, edited.deal);
  const activeTotal = snapshot.result.deals.filter((row) => row.status === "active").reduce((sum, row) => sum + row.amount_minor, 0);
  assert.equal(snapshot.result.totals.deal_value_minor, activeTotal);
  assert.equal(snapshot.result.totals.received_minor, null);
  assert.equal(snapshot.result.totals.outstanding_minor, null);
  assert.equal(snapshot.result.totals.expense_minor, null);
});
