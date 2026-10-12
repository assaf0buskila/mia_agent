"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const vm = require("node:vm");
const Schema = require("../../ops/assaf-os/schema.js");
const Runtime = require("../../ops/assaf-os/runtime.js");

const binding = { owner_id: "123456", workspace_id: "ws-main", workbook_id: "Workbook_12345678901234567890", mode: "TEST" };
function envelope(overrides = {}) {
  return Object.assign({ api_version: "assaf-os.v1", operation: "snapshot", actor_source: "OS", owner_id: binding.owner_id, workspace_id: binding.workspace_id, workbook_id: binding.workbook_id, request_id: "request-0001", timestamp: 1700000000, data: {} }, overrides);
}

test("transport rejects cross-owner, stale, extra keys, and decision actors", () => {
  assert.throws(() => Runtime.validateEnvelope(envelope({ owner_id: "999" }), binding, 1700000000), /unauthorized_owner/);
  assert.throws(() => Runtime.validateEnvelope(envelope({ timestamp: 1699999000 }), binding, 1700000000), /stale_request/);
  assert.throws(() => Runtime.validateEnvelope(Object.assign(envelope(), { mode: "LIVE" }), binding, 1700000000), /invalid_payload_shape/);
  assert.throws(() => Runtime.validateEnvelope(envelope({ operation: "approve", actor_source: "MIA" }), binding, 1700000000), /actor_cannot_decide/);
  assert.doesNotThrow(() => Runtime.validateEnvelope(envelope({ operation: "approve", actor_source: "TELEGRAM_APPROVAL" }), binding, 1700000000));
  assert.throws(() => Runtime.validateEnvelope(envelope({ operation: "snapshot", actor_source: "TELEGRAM_APPROVAL" }), binding, 1700000000), /actor_operation_forbidden/);
  assert.throws(() => Runtime.validateEnvelope(envelope(), Object.assign({}, binding, { mode: "STAGING" }), 1700000000), /invalid_mode/);
});

test("real schema accepts input roles and rejects system or calculated injection", () => {
  const lead = Runtime.canonicalInput("leads", { name: "דנה", path: "לימוד", status: "חדש", phone: "0520000000" }, true);
  assert.equal(lead.name, "דנה");
  assert.throws(() => Runtime.canonicalInput("leads", { name: "דנה", path: "לימוד", status: "חדש", revision: 9 }, true), /non_input_field/);
  assert.throws(() => Runtime.canonicalInput("clients", { name: "דנה", status: "פעיל", total_received_ils: 999 }, true), /non_input_field/);
  assert.throws(() => Runtime.canonicalInput("works", { person_id: "PER_1", title: "X", path: "לימוד", status: "פעילה", currency: "NIS" }, true), /invalid_currency/);
});

test("money crosses the boundary once and rejects fractional agorot", () => {
  const input = Runtime.canonicalInput("expenses", { date: "2026-10-12", category: "כלים", platform: "אחר", path: "משותף", amount_ils: 12345, currency: "ILS", payment_status: "שולם", source_type: "תשלום בפועל", source_key: "invoice-1" }, true);
  const native = Runtime.inputToNative("expenses", input);
  assert.equal(native.amount_ils, 123.45);
  assert.equal(Runtime.nativeToInput("expenses", native).amount_ils, 12345);
  assert.throws(() => Runtime.majorToMinor(1.001), /money_precision_exceeded/);
});

test("proposal has no business side effect and stale raw target cannot commit", () => {
  const business = { LEA_1: { lead_id: "LEA_1", revision: 1, name: "דנה" } };
  const target = { revision: 1, record: business.LEA_1 };
  const proposal = Runtime.makeProposal({ proposal_id: "PR_123", request_id: "proposal-request-1", approval_request_id: "12c91c31-7410-45b2-8baf-000000000001", rejection_request_id: "12c91c31-7410-45b2-8baf-000000000002", owner_id: binding.owner_id, source: "MIA", entity: "leads", record_id: "LEA_1", expected_revision: 1, input: { name: "דנה החדשה" }, before: business.LEA_1, after: Object.assign({}, business.LEA_1, { name: "דנה החדשה" }), target_fingerprint: Runtime.proposalFingerprint(target), created_at: "2026-10-12T10:00:00.000Z", expires_at: "2026-10-12T10:10:00.000Z" });
  assert.equal(business.LEA_1.name, "דנה");
  assert.equal(Runtime.approveDecision(proposal, { owner_id: binding.owner_id, now: "2026-10-12T10:05:00.000Z", current_revision: 1, current_fingerprint: Runtime.proposalFingerprint({ revision: 1, record: Object.assign({}, business.LEA_1, { name: "owner edit" }) }) }).status, "target_changed");
  const decision = Runtime.approveDecision(proposal, { owner_id: binding.owner_id, now: "2026-10-12T10:05:00.000Z", current_revision: 1, current_fingerprint: proposal.target_fingerprint });
  assert.equal(decision.status, "commit");
  business.LEA_1 = decision.record;
  assert.equal(business.LEA_1.name, "דנה החדשה");
  assert.equal(Runtime.approveDecision(proposal, { owner_id: binding.owner_id, now: "2026-10-12T10:11:00.000Z", current_revision: 1, current_fingerprint: proposal.target_fingerprint }).status, "expired");
});

test("native direct edits allocate IDs, version accepted changes, and surface system drift", () => {
  const native = { name: "נועם", phone: "0521234567", email: null, path: "אוטומציה", status: "חדש", source_note: null };
  const first = Runtime.reconcileNativeRow({ entity: "leads", native_input: native, record_id: null, revision: null, accepted: null, new_id: "LEA_NEW" });
  assert.deepEqual({ kind: first.kind, record_id: first.record_id, revision: first.revision }, { kind: "accept", record_id: "LEA_NEW", revision: 1 });
  const accepted = { record_id: "LEA_NEW", revision: 1, input_fingerprint: first.input_fingerprint };
  assert.equal(Runtime.reconcileNativeRow({ entity: "leads", native_input: native, record_id: "LEA_NEW", revision: 1, accepted, new_id: "UNUSED" }).kind, "unchanged");
  assert.equal(Runtime.reconcileNativeRow({ entity: "leads", native_input: Object.assign({}, native, { status: "רלוונטי" }), record_id: "LEA_NEW", revision: 1, accepted, new_id: "UNUSED" }).revision, 2);
  assert.equal(Runtime.reconcileNativeRow({ entity: "leads", native_input: native, record_id: "LEA_NEW", revision: 99, accepted, new_id: "UNUSED" }).reason_code, "system_column_drift");
});

test("refund and external source constraints fail closed", () => {
  const original = { payment_id: "PAY_1", person_id: "PER_1", work_id: "WOR_1", path: "לימוד", currency: "ILS", kind: "התקבל", date: "2026-10-01", actual_amount_ils: 10000 };
  const refund = { payment_id: "PAY_2", person_id: "PER_1", work_id: "WOR_1", path: "לימוד", currency: "ILS", kind: "החזר", date: "2026-10-02", actual_amount_ils: 6000, original_payment_id: "PAY_1" };
  assert.doesNotThrow(() => Runtime.validateRefund(refund, [original]));
  assert.throws(() => Runtime.validateRefund(Object.assign({}, refund, { actual_amount_ils: 10001 }), [original]), /refund_exceeds_original/);
  const expense = { expense_id: "EXP_2", source_type: "תשלום בפועל", source_key: "stripe:abc" };
  assert.throws(() => Runtime.dedupeExpense(expense, [{ expense_id: "EXP_1", source_type: "תשלום בפועל", source_key: "stripe:abc" }]), /duplicate_expense_source/);
});

function transportRuntime() {
  const secret = "s".repeat(40);
  const output = (text) => ({ text, setMimeType() { return this; } });
  const context = {
    console, Date, JSON, Math, Set,
    AssafOsSchema: Schema, AssafOsRuntime: Runtime,
    ContentService: { MimeType: { JSON: "json" }, createTextOutput: output },
    PropertiesService: { getScriptProperties: () => ({ getProperty: (key) => ({ ASSAF_OS_OWNER_ID: binding.owner_id, ASSAF_OS_WORKSPACE_ID: binding.workspace_id, ASSAF_OS_WORKBOOK_ID: binding.workbook_id, ASSAF_OS_MODE: binding.mode, ASSAF_OS_HMAC_SECRET: secret }[key] || null) }) },
    Utilities: {
      Charset: { UTF_8: "UTF_8" }, DigestAlgorithm: { SHA_256: "SHA_256" },
      newBlob: (value) => ({ getBytes: () => [...Buffer.from(value, "utf8")] }),
      computeHmacSha256Signature: (value, key) => [...crypto.createHmac("sha256", key).update(value, "utf8").digest()].map((v) => v > 127 ? v - 256 : v),
      computeDigest: (_algorithm, value) => [...crypto.createHash("sha256").update(value, "utf8").digest()].map((v) => v > 127 ? v - 256 : v),
      getUuid: () => "11111111-1111-4111-8111-111111111111",
    },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock() {} }) },
    SpreadsheetApp: { openById: () => ({}) }, Sheets: {},
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("ops/assaf-os/UnifiedRuntime.gs", "utf8"), context, { filename: "UnifiedRuntime.gs" });
  context.assafOsRun_ = (request, config) => ({ observed_actor: request.actor_source, source: "google_sheets", mode: config.mode });
  return { context, secret };
}
function call(rt, request, signature) {
  const payload = JSON.stringify(request);
  const sig = signature || crypto.createHmac("sha256", rt.secret).update(payload).digest("hex");
  return JSON.parse(rt.context.doPost({ postData: { contents: JSON.stringify({ payload, signature: sig }) } }).text);
}

test("mocked Apps Script verifies inbound HMAC and signs exact echoed response metadata", () => {
  const rt = transportRuntime(), now = Math.floor(Date.now() / 1000), request = envelope({ request_id: "transport-0001", timestamp: now, actor_source: "MIA" });
  const wrapped = call(rt, request), response = JSON.parse(wrapped.payload);
  assert.equal(response.ok, true);
  assert.deepEqual(Object.keys(response).sort(), ["actor_source", "api_version", "data", "mode", "ok", "operation", "owner_id", "request_id", "timestamp", "workbook_id", "workspace_id"].sort());
  assert.equal(response.timestamp, request.timestamp);
  assert.equal(response.mode, "TEST");
  assert.equal(wrapped.signature, crypto.createHmac("sha256", rt.secret).update(wrapped.payload).digest("hex"));
  const bad = JSON.parse(call(rt, request, "0".repeat(64)).payload);
  assert.equal(bad.ok, false);
  assert.equal(bad.data.error.code, "invalid_signature");
});

test("lost approval response recovers immutable receipt and cross-channel repeat cannot write twice", () => {
  const approvalRequestId = "12c91c31-7410-45b2-8baf-000000000001", receipts = new Map(); let writes = 0;
  function approve(actor) {
    Runtime.validateEnvelope(envelope({ operation: "approve", actor_source: actor, request_id: approvalRequestId, data: { proposal_id: "PR_123", request_id: approvalRequestId } }), binding, 1700000000);
    if (receipts.has(approvalRequestId)) return receipts.get(approvalRequestId);
    writes += 1;
    const receipt = Object.freeze({ status: "committed", request_id: approvalRequestId, proposal_id: "PR_123", revision: 2 });
    receipts.set(approvalRequestId, receipt); return receipt;
  }
  const lost = approve("TELEGRAM_APPROVAL");
  assert.equal(writes, 1);
  assert.strictEqual(approve("OS"), lost);
  assert.strictEqual(receipts.get(approvalRequestId), lost);
  assert.equal(writes, 1);
});
