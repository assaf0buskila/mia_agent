(function (root, factory) {
  const api = factory();
  root.BusinessLedgerCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const API_VERSION = "business-ledger.v1";
  const MODE = "TEST";
  const SOURCE = "google_sheets";
  const MAX_AMOUNT_MINOR = 1000000000000;
  const MAX_TITLE = 160;
  const MAX_NOTES = 1000;
  const MAX_REVISION = 1000000;
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const OWNER_RE = /^[0-9]+$/;
  const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
  const CONTROL_RE = /[\u0000-\u001f\u007f-\u009f]/;
  const FORMULA_RE = /^\s*[=+\-@]/;
  const DEAL_INPUT_KEYS = ["id", "title", "amount_minor", "currency", "payment_plan", "start_date", "status", "project_id", "notes"];
  const DEAL_KEYS = ["id", "revision"].concat(DEAL_INPUT_KEYS.slice(1)).concat(["updated_at"]);

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
    if (actual.length !== expected.length || actual.some((key, i) => key !== expected[i])) fail(code, "unexpected object shape");
  }

  function uuid(value, code) {
    if (typeof value !== "string" || !UUID_RE.test(value)) fail(code || "invalid_uuid", "UUID required");
    return value;
  }

  function literal(value, max, required, code) {
    if (typeof value !== "string") fail(code, "string required");
    if ((required && value.length === 0) || value !== value.trim() || value.length > max || CONTROL_RE.test(value) || FORMULA_RE.test(value)) {
      fail(code, "unsafe or out-of-range literal");
    }
    return value;
  }

  function validDate(value) {
    const match = typeof value === "string" && DATE_RE.exec(value);
    if (!match) return false;
    const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
    return date.getUTCFullYear() === Number(match[1]) && date.getUTCMonth() === Number(match[2]) - 1 && date.getUTCDate() === Number(match[3]);
  }

  function canonicalDealInput(raw) {
    assertExactKeys(raw, DEAL_INPUT_KEYS, "invalid_deal_shape");
    const amount = raw.amount_minor;
    if (!Number.isSafeInteger(amount) || amount <= 0 || amount > MAX_AMOUNT_MINOR) fail("invalid_amount_minor");
    if (raw.currency !== "ILS") fail("invalid_currency");
    if (!["full", "installments", "monthly"].includes(raw.payment_plan)) fail("invalid_payment_plan");
    if (!validDate(raw.start_date)) fail("invalid_start_date");
    if (!["active", "cancelled"].includes(raw.status)) fail("invalid_status");
    if (raw.project_id !== null && raw.project_id !== "") fail("project_links_not_supported");
    return {
      id: uuid(raw.id, "invalid_deal_id"),
      title: literal(raw.title, MAX_TITLE, true, "invalid_title"),
      amount_minor: amount,
      currency: "ILS",
      payment_plan: raw.payment_plan,
      start_date: raw.start_date,
      status: raw.status,
      project_id: null,
      notes: literal(raw.notes, MAX_NOTES, false, "invalid_notes"),
    };
  }

  function canonicalPersistedDeal(raw) {
    assertExactKeys(raw, DEAL_KEYS, "invalid_persisted_deal_shape");
    if (!Number.isSafeInteger(raw.revision) || raw.revision <= 0 || raw.revision > MAX_REVISION) fail("invalid_revision");
    const input = {};
    DEAL_INPUT_KEYS.forEach((key) => { input[key] = raw[key]; });
    if (typeof raw.updated_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(raw.updated_at) || new Date(raw.updated_at).toISOString() !== raw.updated_at) fail("invalid_updated_at");
    return Object.assign({ id: raw.id, revision: raw.revision }, canonicalDealInput(input), { updated_at: raw.updated_at });
  }

  // JSON.stringify follows insertion order; recursively sorting makes hashes portable.
  function stableStringify(value) {
    if (Array.isArray(value)) return "[" + value.map(stableStringify).join(",") + "]";
    if (isPlainObject(value)) return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + stableStringify(value[key])).join(",") + "}";
    return JSON.stringify(value);
  }

  function validateSaveData(data) {
    assertExactKeys(data, ["request_id", "expected_revision", "deal"], "invalid_save_shape");
    const requestId = uuid(data.request_id, "invalid_idempotency_key");
    if (!Number.isSafeInteger(data.expected_revision) || data.expected_revision < 0 || data.expected_revision > MAX_REVISION) fail("invalid_expected_revision");
    return { request_id: requestId, expected_revision: data.expected_revision, deal: canonicalDealInput(data.deal) };
  }

  function validateReceiptData(data) {
    assertExactKeys(data, ["request_id"], "invalid_receipt_shape");
    return { request_id: uuid(data.request_id, "invalid_idempotency_key") };
  }

  function decideSave(args) {
    const input = validateSaveData(args.data);
    if (!OWNER_RE.test(args.owner_id || "")) fail("invalid_owner_id");
    if (args.existingReceipt) {
      if (args.existingReceipt.owner_id !== args.owner_id || args.existingReceipt.data_hash !== args.data_hash) {
        return { kind: "invalid", reason_code: "idempotency_key_reused" };
      }
      return { kind: "replay", receipt: args.existingReceipt.receipt };
    }
    const current = args.currentDeal || null;
    if (current) {
      let canonicalCurrent;
      try { canonicalCurrent = canonicalPersistedDeal(current); } catch (_) {
        return { kind: "conflict", reason_code: "deal_row_invalid" };
      }
      if (!args.acceptedSnapshot || stableStringify(canonicalCurrent) !== stableStringify(args.acceptedSnapshot)) {
        return { kind: "conflict", reason_code: "deal_row_tampered" };
      }
      if (input.expected_revision !== canonicalCurrent.revision) return { kind: "conflict", reason_code: "revision_mismatch" };
    } else if (args.acceptedSnapshot) {
      return { kind: "conflict", reason_code: "accepted_deal_missing" };
    } else if (input.expected_revision !== 0) {
      return { kind: "conflict", reason_code: "revision_mismatch" };
    }
    if (typeof args.committed_at !== "string") fail("invalid_committed_at");
    const next = Object.assign({ id: input.deal.id, revision: current ? current.revision + 1 : 1 }, input.deal, { updated_at: args.committed_at });
    return { kind: "commit", deal: canonicalPersistedDeal(next), request_id: input.request_id };
  }

  function snapshot(deals, observedAt) {
    const canonical = deals.map(canonicalPersistedDeal);
    let total = 0;
    canonical.forEach((deal) => {
      if (deal.status === "active") total += deal.amount_minor;
      if (!Number.isSafeInteger(total)) fail("deal_value_overflow");
    });
    return {
      api_version: API_VERSION,
      mode: MODE,
      source: SOURCE,
      observed_at: observedAt,
      coverage: "deals_only",
      deals: canonical,
      totals: { deal_value_minor: total, received_minor: null, outstanding_minor: null, expense_minor: null },
      reason_codes: ["financial_coverage_incomplete"],
    };
  }

  return {
    API_VERSION, MODE, SOURCE, DEAL_INPUT_KEYS, DEAL_KEYS, MAX_REVISION,
    canonicalDealInput, canonicalPersistedDeal, decideSave, snapshot,
    stableStringify, validateReceiptData, validateSaveData, uuid,
  };
});
