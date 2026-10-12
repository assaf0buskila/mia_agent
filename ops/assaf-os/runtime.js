/* global globalThis */
"use strict";

(function (root, factory) {
  const value = factory(root.AssafOsSchema || (typeof require === "function" ? require("./schema.js") : null));
  if (typeof module === "object" && module.exports) module.exports = value;
  root.AssafOsRuntime = value;
})(typeof globalThis !== "undefined" ? globalThis : this, function (Schema) {
  const API_VERSION = "assaf-os.v1";
  const OPERATIONS = Object.freeze(["schema", "snapshot", "propose", "proposal", "approve", "reject", "receipt"]);
  const ENTITIES = Object.freeze(["leads", "clients", "works", "payments", "time", "expenses", "tasks", "promote_lead"]);
  const ID_RE = /^[A-Za-z][A-Za-z0-9_-]{2,63}$/;
  const REQUEST_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const MAX_TEXT = 2000;

  function error(code) { const value = new Error(code); value.code = code; throw value; }
  function plain(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function exact(value, keys, code) {
    if (!plain(value) || Object.keys(value).sort().join("|") !== keys.slice().sort().join("|")) error(code);
    return value;
  }
  function stableStringify(value) {
    if (Array.isArray(value)) return "[" + value.map(stableStringify).join(",") + "]";
    if (plain(value)) return "{" + Object.keys(value).sort().map(function (key) { return JSON.stringify(key) + ":" + stableStringify(value[key]); }).join(",") + "}";
    return JSON.stringify(value);
  }
  function copy(value) { return value == null ? value : JSON.parse(JSON.stringify(value)); }
  function validDate(value) {
    if (typeof value !== "string" || !DATE_RE.test(value)) return false;
    const parsed = new Date(value + "T00:00:00Z");
    return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
  }
  function fieldValue(field, value) {
    if (value === null || value === "") return null;
    if (field.type === "text") {
      if (typeof value !== "string" || value.length > MAX_TEXT) error("invalid_" + field.key);
      return value.trim();
    }
    if (field.type === "id") {
      if (typeof value !== "string" || !ID_RE.test(value)) error("invalid_" + field.key);
      return value;
    }
    if (field.type === "date") {
      if (!validDate(value)) error("invalid_" + field.key);
      return value;
    }
    if (field.type === "integer") {
      if (!Number.isSafeInteger(value) || value < (field.minimum || 0)) error("invalid_" + field.key);
      return value;
    }
    if (field.type === "decimal") {
      if (typeof value !== "number" || !Number.isFinite(value)) error("invalid_" + field.key);
      return value;
    }
    if (field.type === "boolean") {
      if (typeof value !== "boolean") error("invalid_" + field.key);
      return value;
    }
    if (field.type === "money_ils") {
      if (!Number.isSafeInteger(value) || value < 0) error("invalid_" + field.key);
      return value;
    }
    if (field.type === "enum") {
      const values = field.values || Schema.enums[field.enum] || [];
      if (typeof value !== "string" || values.indexOf(value) === -1) error("invalid_" + field.key);
      return value;
    }
    error("unsupported_field_type");
  }
  function entityTab(entity) { return Schema.tab(entity === "promote_lead" ? "leads" : entity); }
  function inputFields(entity) {
    const tab = entityTab(entity);
    if (!tab || tab.kind !== "input" || entity === "settings") error("invalid_entity");
    return tab.fields.filter(function (field) { return field.role === "input"; });
  }
  function canonicalInput(entity, raw, creating) {
    if (ENTITIES.indexOf(entity) === -1 || entity === "promote_lead") error("invalid_entity");
    if (!plain(raw)) error("invalid_input_shape");
    const fields = inputFields(entity), allowed = fields.map(function (field) { return field.key; });
    if (Object.keys(raw).some(function (key) { return allowed.indexOf(key) === -1; })) error("non_input_field");
    const output = {};
    fields.forEach(function (field) {
      if (!Object.prototype.hasOwnProperty.call(raw, field.key)) {
        if (creating && field.required) error("missing_" + field.key);
        return;
      }
      const value = fieldValue(field, raw[field.key]);
      if (field.required && value === null) error("missing_" + field.key);
      output[field.key] = value;
    });
    if (entity === "payments") {
      if (output.kind === "החזר" && !output.original_payment_id) error("refund_original_required");
      if (output.kind !== "החזר" && output.original_payment_id) error("original_payment_only_for_refund");
      if (output.actual_amount_ils !== undefined && output.actual_amount_ils <= 0) error("invalid_actual_amount_ils");
    }
    if (entity === "expenses" && output.amount_ils !== undefined && output.amount_ils <= 0) error("invalid_amount_ils");
    return output;
  }
  function minorToMajor(value) {
    if (value === null) return null;
    if (!Number.isSafeInteger(value)) error("invalid_minor_amount");
    return value / 100;
  }
  function majorToMinor(value) {
    if (value === null || value === "") return null;
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) error("invalid_major_amount");
    const rounded = Math.round(value * 100);
    if (!Number.isSafeInteger(rounded) || Math.abs(value * 100 - rounded) > 1e-7) error("money_precision_exceeded");
    return rounded;
  }
  function inputToNative(entity, input) {
    const result = copy(input);
    inputFields(entity).forEach(function (field) {
      if (field.type === "money_ils" && Object.prototype.hasOwnProperty.call(result, field.key)) result[field.key] = minorToMajor(result[field.key]);
    });
    return result;
  }
  function nativeToInput(entity, input) {
    const result = copy(input);
    inputFields(entity).forEach(function (field) {
      if (field.type === "money_ils" && Object.prototype.hasOwnProperty.call(result, field.key)) result[field.key] = majorToMinor(result[field.key]);
    });
    return canonicalInput(entity, result, false);
  }
  function validateEnvelope(value, binding, nowSeconds) {
    exact(value, ["api_version", "operation", "actor_source", "owner_id", "workspace_id", "workbook_id", "request_id", "timestamp", "data"], "invalid_payload_shape");
    if (value.api_version !== API_VERSION) error("invalid_api_version");
    if (OPERATIONS.indexOf(value.operation) === -1) error("invalid_operation");
    if (["OS", "MIA", "TELEGRAM_APPROVAL"].indexOf(value.actor_source) === -1) error("invalid_actor_source");
    if ((value.operation === "approve" || value.operation === "reject") && ["OS", "TELEGRAM_APPROVAL"].indexOf(value.actor_source) === -1) error("actor_cannot_decide");
    if (value.actor_source === "TELEGRAM_APPROVAL" && value.operation !== "approve" && value.operation !== "reject") error("actor_operation_forbidden");
    if (value.operation === "propose" && value.actor_source !== "OS" && value.actor_source !== "MIA") error("actor_cannot_propose");
    if (value.owner_id !== binding.owner_id) error("unauthorized_owner");
    if (value.workspace_id !== binding.workspace_id) error("invalid_workspace");
    if (value.workbook_id !== binding.workbook_id) error("invalid_workbook");
    if (binding.mode !== "TEST" && binding.mode !== "LIVE") error("invalid_mode");
    if (typeof value.request_id !== "string" || !REQUEST_RE.test(value.request_id)) error("invalid_request_id");
    if (!Number.isSafeInteger(value.timestamp) || Math.abs(nowSeconds - value.timestamp) > 60) error("stale_request");
    if (!plain(value.data)) error("invalid_data");
    return value;
  }
  function proposalFingerprint(target) { return stableStringify(target === null ? null : target); }
  function makeProposal(args) {
    exact(args, ["proposal_id", "request_id", "approval_request_id", "rejection_request_id", "owner_id", "source", "entity", "record_id", "expected_revision", "input", "before", "after", "target_fingerprint", "created_at", "expires_at"], "invalid_proposal_shape");
    if (args.source !== "OS" && args.source !== "MIA") error("invalid_source");
    if (!UUID_RE.test(args.approval_request_id) || !UUID_RE.test(args.rejection_request_id) || args.approval_request_id === args.rejection_request_id) error("invalid_decision_request_id");
    if (ENTITIES.indexOf(args.entity) === -1) error("invalid_entity");
    if (!Number.isSafeInteger(args.expected_revision) || args.expected_revision < 0) error("invalid_expected_revision");
    if (!(Date.parse(args.expires_at) > Date.parse(args.created_at))) error("invalid_expiry");
    const proposal = copy(args);
    proposal.status = "pending";
    return Object.freeze(proposal);
  }
  function approveDecision(proposal, context) {
    if (!proposal || proposal.status !== "pending") return { status: proposal && proposal.status || "not_found", reason_code: "proposal_not_pending" };
    if (proposal.owner_id !== context.owner_id) return { status: "invalid", reason_code: "unauthorized_owner" };
    if (Date.parse(context.now) >= Date.parse(proposal.expires_at)) return { status: "expired", reason_code: "proposal_expired" };
    if (context.current_revision !== proposal.expected_revision || context.current_fingerprint !== proposal.target_fingerprint) return { status: "target_changed", reason_code: "target_changed" };
    return { status: "commit", record: copy(proposal.after), revision: proposal.expected_revision + 1 };
  }
  function validateRefund(candidate, payments) {
    if (candidate.kind !== "החזר") return;
    const original = payments.find(function (p) { return p.payment_id === candidate.original_payment_id; });
    if (!original || original.kind !== "התקבל") error("refund_original_not_found");
    if (original.person_id !== candidate.person_id || original.work_id !== candidate.work_id || original.path !== candidate.path || original.currency !== candidate.currency) error("refund_original_mismatch");
    if (candidate.date < original.date) error("refund_before_original");
    const prior = payments.filter(function (p) { return p.kind === "החזר" && p.original_payment_id === original.payment_id && p.payment_id !== candidate.payment_id; })
      .reduce(function (sum, p) { return sum + p.actual_amount_ils; }, 0);
    if (!Number.isSafeInteger(prior + candidate.actual_amount_ils) || prior + candidate.actual_amount_ils > original.actual_amount_ils) error("refund_exceeds_original");
  }
  function validatePaymentGraph(payments) {
    if (!Array.isArray(payments)) error("invalid_payments");
    const ids = new Set();
    payments.forEach(function (payment) { if (!payment || typeof payment.payment_id !== "string" || ids.has(payment.payment_id)) error("duplicate_payment_id"); ids.add(payment.payment_id); });
    payments.forEach(function (payment) { validateRefund(payment, payments); });
    return payments;
  }
  function dedupeExpense(candidate, expenses) {
    if (expenses.some(function (e) { return e.expense_id !== candidate.expense_id && e.source_type === candidate.source_type && e.source_key === candidate.source_key; })) error("duplicate_expense_source");
  }
  function reconcileNativeRow(args) {
    exact(args, ["entity", "native_input", "record_id", "revision", "accepted", "new_id"], "invalid_reconcile_shape");
    const input = nativeToInput(args.entity, args.native_input);
    canonicalInput(args.entity, input, true);
    if (args.accepted && (args.record_id !== args.accepted.record_id || args.revision !== args.accepted.revision)) return { kind: "issue", reason_code: "system_column_drift" };
    const fingerprint = proposalFingerprint(input);
    if (args.accepted && args.accepted.input_fingerprint === fingerprint) return { kind: "unchanged", record_id: args.record_id, revision: args.revision };
    const recordId = args.record_id || args.new_id;
    if (typeof recordId !== "string" || !ID_RE.test(recordId)) error("invalid_new_record_id");
    return { kind: "accept", record_id: recordId, revision: args.accepted ? args.accepted.revision + 1 : 1, input: input, input_fingerprint: fingerprint };
  }

  return Object.freeze({
    API_VERSION: API_VERSION, operations: OPERATIONS, entities: ENTITIES, exact: exact, stableStringify: stableStringify,
    canonicalInput: canonicalInput, inputToNative: inputToNative, nativeToInput: nativeToInput,
    minorToMajor: minorToMajor, majorToMinor: majorToMinor, validateEnvelope: validateEnvelope,
    proposalFingerprint: proposalFingerprint, makeProposal: makeProposal, approveDecision: approveDecision,
    validateRefund: validateRefund, validatePaymentGraph: validatePaymentGraph, dedupeExpense: dedupeExpense, reconcileNativeRow: reconcileNativeRow,
  });
});
