/* global globalThis */
"use strict";

(function (root, factory) {
  const value = factory();
  if (typeof module === "object" && module.exports) module.exports = value;
  root.AssafOsCore = value;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const PATHS = ["לימוד", "אוטומציה"];
  const WORK_STATUSES = ["מתוכננת", "פעילה", "הושלמה", "בוטלה"];
  const COVERAGE = ["complete", "unknown"];
  const ID_RE = /^[A-Za-z][A-Za-z0-9_-]{2,63}$/;
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

  function fail(code) { const error = new Error(code); error.code = code; throw error; }
  function object(value, code) { if (!value || typeof value !== "object" || Array.isArray(value)) fail(code); return value; }
  function exactKeys(value, keys, code) {
    object(value, code);
    if (Object.keys(value).sort().join("|") !== keys.slice().sort().join("|")) fail(code);
  }
  function id(value, code) { if (typeof value !== "string" || !ID_RE.test(value)) fail(code); return value; }
  function date(value, code) {
    if (typeof value !== "string" || !DATE_RE.test(value)) fail(code);
    const parsed = new Date(value + "T00:00:00Z");
    if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) fail(code);
    return value;
  }
  function nonnegativeInteger(value, code) { if (!Number.isSafeInteger(value) || value < 0) fail(code); return value; }
  function positiveInteger(value, code) { if (!Number.isSafeInteger(value) || value <= 0) fail(code); return value; }
  function currency(value) { if (typeof value !== "string" || !/^[A-Z]{3}$/.test(value)) fail("invalid_currency"); return value; }
  function path(value) { if (PATHS.indexOf(value) === -1) fail("invalid_path"); return value; }
  function monthOf(value) { return date(value, "invalid_date").slice(0, 7); }
  function safeAdd(a, b) { const result = a + b; if (!Number.isSafeInteger(result)) fail("amount_overflow"); return result; }
  function stableStringify(value) {
    if (Array.isArray(value)) return "[" + value.map(stableStringify).join(",") + "]";
    if (value && typeof value === "object") return "{" + Object.keys(value).sort().map(function (key) { return JSON.stringify(key) + ":" + stableStringify(value[key]); }).join(",") + "}";
    return JSON.stringify(value);
  }
  function assertUnique(records, key, code) {
    const seen = new Set();
    records.forEach(function (record) { if (seen.has(record[key])) fail(code); seen.add(record[key]); });
  }
  function coverage(value) {
    exactKeys(value, ["payments", "time", "expenses"], "invalid_coverage_shape");
    Object.keys(value).forEach(function (key) { if (COVERAGE.indexOf(value[key]) === -1) fail("invalid_coverage"); });
    return value;
  }
  function canonicalPayment(raw) {
    object(raw, "invalid_payment_shape");
    const allowed = ["payment_id", "person_id", "work_id", "path", "date", "amount_agorot", "currency", "kind", "original_payment_id"];
    if (Object.keys(raw).some(function (key) { return allowed.indexOf(key) === -1; }) || allowed.slice(0, 8).some(function (key) { return !Object.prototype.hasOwnProperty.call(raw, key); })) fail("invalid_payment_shape");
    if (["התקבל", "החזר", "בוטל"].indexOf(raw.kind) === -1) fail("invalid_payment_kind");
    const originalPaymentId = raw.original_payment_id == null || raw.original_payment_id === "" ? null : id(raw.original_payment_id, "invalid_original_payment_id");
    if (raw.kind === "החזר" && originalPaymentId === null) fail("refund_original_required");
    if (raw.kind !== "החזר" && originalPaymentId !== null) fail("original_payment_only_for_refund");
    return { payment_id: id(raw.payment_id, "invalid_payment_id"), person_id: id(raw.person_id, "invalid_person_id"), work_id: id(raw.work_id, "invalid_work_id"), path: path(raw.path), date: date(raw.date, "invalid_payment_date"), amount_agorot: positiveInteger(raw.amount_agorot, "invalid_payment_amount"), currency: currency(raw.currency), kind: raw.kind, original_payment_id: originalPaymentId };
  }
  function canonicalTime(raw) {
    exactKeys(raw, ["time_id", "person_id", "work_id", "path", "date", "minutes", "status"], "invalid_time_shape");
    if (["פעיל", "בוטל"].indexOf(raw.status) === -1) fail("invalid_time_status");
    return { time_id: id(raw.time_id, "invalid_time_id"), person_id: id(raw.person_id, "invalid_person_id"), work_id: id(raw.work_id, "invalid_work_id"), path: path(raw.path), date: date(raw.date, "invalid_time_date"), minutes: nonnegativeInteger(raw.minutes, "invalid_minutes"), status: raw.status };
  }
  function canonicalExpense(raw) {
    exactKeys(raw, ["expense_id", "date", "path", "amount_agorot", "currency", "payment_status", "source_type", "source_key"], "invalid_expense_shape");
    if (["לימוד", "אוטומציה", "משותף"].indexOf(raw.path) === -1) fail("invalid_expense_path");
    if (["שולם", "לא שולם", "בוטל"].indexOf(raw.payment_status) === -1) fail("invalid_expense_payment_status");
    if (["תשלום בפועל", "דיווח שימוש בפלטפורמה"].indexOf(raw.source_type) === -1) fail("invalid_expense_source_type");
    if (typeof raw.source_key !== "string" || !raw.source_key.trim()) fail("invalid_expense_source_key");
    return { expense_id: id(raw.expense_id, "invalid_expense_id"), date: date(raw.date, "invalid_expense_date"), path: raw.path, amount_agorot: positiveInteger(raw.amount_agorot, "invalid_expense_amount"), currency: currency(raw.currency), payment_status: raw.payment_status, source_type: raw.source_type, source_key: raw.source_key.trim() };
  }
  function groupKey(record) { return [record.person_id, record.path, monthOf(record.date), record.currency].join("|"); }
  function emptyGroup(record) {
    return { person_id: record.person_id, path: record.path, month: monthOf(record.date), currency: record.currency, net_received_agorot: 0, total_minutes: 0 };
  }
  function weightedHourly(cash, minutes) {
    if (cash === null || minutes === null || minutes === 0) return null;
    const hourly = Math.round((cash / minutes) * 60);
    if (!Number.isSafeInteger(hourly)) fail("weighted_hourly_overflow");
    return hourly;
  }
  function snapshot(raw) {
    exactKeys(raw, ["coverage", "observed_at", "clients", "works", "payments", "time_entries", "expenses"], "invalid_snapshot_shape");
    const cov = coverage(raw.coverage);
    if (typeof raw.observed_at !== "string" || Number.isNaN(Date.parse(raw.observed_at))) fail("invalid_observed_at");
    if (![raw.clients, raw.works, raw.payments, raw.time_entries, raw.expenses].every(Array.isArray)) fail("invalid_record_sets");
    const clients = raw.clients.map(function (client) {
      exactKeys(client, ["person_id", "name", "status"], "invalid_client_shape");
      if (["פעיל", "לא פעיל"].indexOf(client.status) === -1 || typeof client.name !== "string" || !client.name.trim()) fail("invalid_client");
      return { person_id: id(client.person_id, "invalid_person_id"), name: client.name.trim(), status: client.status };
    });
    const works = raw.works.map(function (work) {
      exactKeys(work, ["work_id", "person_id", "path", "agreed_amount_agorot", "currency", "status"], "invalid_work_shape");
      const amount = work.agreed_amount_agorot === null ? null : nonnegativeInteger(work.agreed_amount_agorot, "invalid_agreed_amount");
      if (WORK_STATUSES.indexOf(work.status) === -1) fail("invalid_work_status");
      return { work_id: id(work.work_id, "invalid_work_id"), person_id: id(work.person_id, "invalid_person_id"), path: path(work.path), agreed_amount_agorot: amount, currency: currency(work.currency), status: work.status };
    });
    const payments = raw.payments.map(canonicalPayment), times = raw.time_entries.map(canonicalTime), expenses = raw.expenses.map(canonicalExpense);
    assertUnique(clients, "person_id", "duplicate_person_id"); assertUnique(works, "work_id", "duplicate_work_id");
    assertUnique(payments, "payment_id", "duplicate_payment_id"); assertUnique(times, "time_id", "duplicate_time_id"); assertUnique(expenses, "expense_id", "duplicate_expense_id");
    assertUnique(expenses, "source_key", "duplicate_expense_source_key");
    const clientIds = new Set(clients.map(function (record) { return record.person_id; }));
    const workById = new Map(works.map(function (record) { return [record.work_id, record]; }));
    works.forEach(function (work) { if (!clientIds.has(work.person_id)) fail("orphan_work_client"); });
    function assertLinked(record, kind) {
      if (!clientIds.has(record.person_id)) fail("orphan_" + kind + "_client");
      const work = workById.get(record.work_id);
      if (!work) fail("orphan_" + kind + "_work");
      if (work.person_id !== record.person_id || work.path !== record.path) fail(kind + "_work_mismatch");
      if (record.currency && work.currency !== record.currency) fail(kind + "_work_currency_mismatch");
    }
    payments.forEach(function (record) { assertLinked(record, "payment"); });
    times.forEach(function (record) { assertLinked(record, "time"); });
    const paymentById = new Map(payments.map(function (record) { return [record.payment_id, record]; }));
    const refundedByOriginal = new Map();
    payments.forEach(function (record) {
      if (record.kind !== "החזר") return;
      const original = paymentById.get(record.original_payment_id);
      if (!original || original.kind !== "התקבל") fail("refund_original_not_found");
      if (original.person_id !== record.person_id || original.work_id !== record.work_id || original.path !== record.path || original.currency !== record.currency) fail("refund_original_mismatch");
      if (record.date < original.date) fail("refund_before_original");
      const refunded = safeAdd(refundedByOriginal.get(original.payment_id) || 0, record.amount_agorot);
      if (refunded > original.amount_agorot) fail("refund_exceeds_original");
      refundedByOriginal.set(original.payment_id, refunded);
    });

    const groups = new Map();
    function getGroup(record) { const key = groupKey(record); if (!groups.has(key)) groups.set(key, emptyGroup(record)); return groups.get(key); }
    if (cov.payments === "complete") payments.forEach(function (record) {
      if (record.kind === "בוטל") return;
      const group = getGroup(record), signed = record.kind === "החזר" ? -record.amount_agorot : record.amount_agorot;
      group.net_received_agorot = safeAdd(group.net_received_agorot, signed);
    });
    if (cov.time === "complete") times.forEach(function (record) { if (record.status !== "בוטל") getGroup(Object.assign({ currency: workById.get(record.work_id).currency }, record)).total_minutes = safeAdd(getGroup(Object.assign({ currency: workById.get(record.work_id).currency }, record)).total_minutes, record.minutes); });

    function groupHasMissingWorkTime(group) {
      if (cov.time !== "complete") return true;
      const workCash = new Map();
      payments.forEach(function (record) {
        if (record.kind === "בוטל" || record.person_id !== group.person_id || record.path !== group.path || record.currency !== group.currency || monthOf(record.date) !== group.month) return;
        workCash.set(record.work_id, safeAdd(workCash.get(record.work_id) || 0, record.kind === "החזר" ? -record.amount_agorot : record.amount_agorot));
      });
      return Array.from(workCash.entries()).some(function (entry) {
        if (entry[1] === 0) return false;
        return !times.some(function (record) { return record.status !== "בוטל" && record.work_id === entry[0] && monthOf(record.date) === group.month && record.minutes > 0; });
      });
    }
    const clientById = new Map(clients.map(function (record) { return [record.person_id, record]; }));
    const rows = Array.from(groups.values()).map(function (group) {
      const cash = cov.payments === "complete" ? group.net_received_agorot : null;
      const minutes = cov.time === "complete" ? group.total_minutes : null;
      const missingTime = minutes === null || minutes === 0 || (cash !== null && groupHasMissingWorkTime(group));
      const hourly = missingTime ? null : weightedHourly(cash, minutes);
      const rowReasons = [];
      if (cash === null) rowReasons.push("payments_coverage_unknown");
      if (minutes === null) rowReasons.push("time_coverage_unknown");
      else if (missingTime) rowReasons.push("missing_time");
      return {
        person_id: group.person_id, client_name: clientById.get(group.person_id).name, path: group.path, month: group.month, currency: group.currency,
        net_received_agorot: cash, total_minutes: minutes, total_hours: minutes === null ? null : minutes / 60,
        weighted_hourly_agorot: hourly, missing_time: missingTime,
        reason_codes: rowReasons,
      };
    }).sort(function (a, b) { return [a.month, a.person_id, a.path, a.currency].join("|").localeCompare([b.month, b.person_id, b.path, b.currency].join("|")); });

    const totalsByKey = new Map();
    rows.forEach(function (row) {
      const key = [row.person_id, row.month, row.currency].join("|");
      if (!totalsByKey.has(key)) totalsByKey.set(key, {
        person_id: row.person_id, client_name: row.client_name, month: row.month, currency: row.currency,
        received_learning_agorot: cov.payments === "complete" ? 0 : null,
        received_automation_agorot: cov.payments === "complete" ? 0 : null,
        total_received_agorot: cov.payments === "complete" ? 0 : null,
        total_minutes: cov.time === "complete" ? 0 : null,
        missing_time: cov.time !== "complete",
      });
      const total = totalsByKey.get(key);
      if (total.total_received_agorot !== null) {
        total.total_received_agorot = safeAdd(total.total_received_agorot, row.net_received_agorot);
        if (row.path === "לימוד") total.received_learning_agorot = safeAdd(total.received_learning_agorot, row.net_received_agorot);
        else total.received_automation_agorot = safeAdd(total.received_automation_agorot, row.net_received_agorot);
      }
      if (total.total_minutes !== null) total.total_minutes = safeAdd(total.total_minutes, row.total_minutes);
      if (row.missing_time) total.missing_time = true;
    });
    const foreignByPersonMonth = new Set(rows.filter(function (row) { return row.currency !== "ILS"; }).map(function (row) { return row.person_id + "|" + row.month; }));
    const clientTotals = Array.from(totalsByKey.values()).map(function (total) {
      total.total_hours = total.total_minutes === null ? null : total.total_minutes / 60;
      total.weighted_hourly_agorot = total.missing_time ? null : weightedHourly(total.total_received_agorot, total.total_minutes);
      total.foreign_currency_present = cov.payments === "complete" ? foreignByPersonMonth.has(total.person_id + "|" + total.month) : null;
      total.reason_codes = total.missing_time ? [cov.time === "unknown" ? "time_coverage_unknown" : "missing_time"] : [];
      return total;
    }).sort(function (a, b) { return [a.month, a.person_id, a.currency].join("|").localeCompare([b.month, b.person_id, b.currency].join("|")); });

    const workRollups = works.map(function (work) {
      const workPayments = payments.filter(function (record) { return record.work_id === work.work_id && record.kind !== "בוטל"; });
      const workTimes = times.filter(function (record) { return record.work_id === work.work_id && record.status !== "בוטל"; });
      const received = cov.payments === "complete" ? workPayments.reduce(function (sum, record) { return safeAdd(sum, record.kind === "החזר" ? -record.amount_agorot : record.amount_agorot); }, 0) : null;
      const minutes = cov.time === "complete" ? workTimes.reduce(function (sum, record) { return safeAdd(sum, record.minutes); }, 0) : null;
      const missingTime = minutes === null || minutes === 0;
      return {
        work_id: work.work_id, person_id: work.person_id, path: work.path, currency: work.currency,
        received_agorot: received, total_minutes: minutes, total_hours: minutes === null ? null : minutes / 60,
        weighted_hourly_agorot: missingTime ? null : weightedHourly(received, minutes), missing_time: missingTime,
        foreign_currency: work.currency !== "ILS",
      };
    });
    const clientRollups = clients.map(function (client) {
      const owned = workRollups.filter(function (work) { return work.person_id === client.person_id; });
      const ils = owned.filter(function (work) { return work.currency === "ILS"; });
      const receivedLearning = cov.payments === "complete" ? ils.filter(function (work) { return work.path === "לימוד"; }).reduce(function (sum, work) { return safeAdd(sum, work.received_agorot); }, 0) : null;
      const receivedAutomation = cov.payments === "complete" ? ils.filter(function (work) { return work.path === "אוטומציה"; }).reduce(function (sum, work) { return safeAdd(sum, work.received_agorot); }, 0) : null;
      const totalReceived = receivedLearning === null ? null : safeAdd(receivedLearning, receivedAutomation);
      const totalMinutes = cov.time === "complete" ? ils.reduce(function (sum, work) { return safeAdd(sum, work.total_minutes); }, 0) : null;
      const missingTime = totalMinutes === null || totalMinutes === 0 || ils.some(function (work) { return work.received_agorot !== null && work.received_agorot !== 0 && work.missing_time; });
      return {
        person_id: client.person_id, client_name: client.name,
        received_learning_ils_agorot: receivedLearning, received_automation_ils_agorot: receivedAutomation,
        total_received_ils_agorot: totalReceived, recorded_ils_minutes: totalMinutes, recorded_ils_hours: totalMinutes === null ? null : totalMinutes / 60,
        weighted_hourly_ils_agorot: missingTime ? null : weightedHourly(totalReceived, totalMinutes), missing_time: missingTime,
        foreign_currency_present: cov.payments === "complete" ? payments.some(function (record) { return record.person_id === client.person_id && record.currency !== "ILS" && record.kind !== "בוטל"; }) : null,
      };
    });

    let shared = {}, byPath = { "לימוד": {}, "אוטומציה": {} }, excludedAdUsage = 0;
    if (cov.expenses === "complete") expenses.forEach(function (record) {
      if (record.payment_status !== "שולם" || record.source_type !== "תשלום בפועל") { if (record.source_type === "דיווח שימוש בפלטפורמה") excludedAdUsage += 1; return; }
      const target = record.path === "משותף" ? shared : byPath[record.path];
      target[record.currency] = safeAdd(target[record.currency] || 0, record.amount_agorot);
    });
    return {
      schema_version: "assaf-os-unified.v1", observed_at: raw.observed_at, coverage: Object.assign({}, cov), rows: rows, client_totals: clientTotals, client_rollups: clientRollups, work_rollups: workRollups,
      costs: cov.expenses === "complete" ? { scope: "all_time", by_path_agorot: byPath, shared_unallocated_agorot: shared, excluded_ad_usage_records: excludedAdUsage } : null,
      reason_codes: cov.expenses === "unknown" ? ["expenses_coverage_unknown"] : [],
    };
  }

  function promotionFingerprint(data) { return stableStringify({ person_id: data.person_id, lead_id: data.lead_id }); }
  function decidePromotion(args) {
    exactKeys(args, ["request_id", "person_id", "lead", "existing_client", "existing_receipt", "identity_candidates", "committed_at"], "invalid_promotion_shape");
    const requestId = id(args.request_id, "invalid_request_id"), personId = id(args.person_id, "invalid_person_id");
    object(args.lead, "invalid_lead"); id(args.lead.lead_id, "invalid_lead_id"); id(args.lead.person_id, "invalid_lead_person_id");
    if (args.lead.person_id !== personId) return { kind: "invalid", reason_code: "person_id_mismatch" };
    if (!Array.isArray(args.identity_candidates)) fail("invalid_identity_candidates");
    const candidateIds = Array.from(new Set(args.identity_candidates.map(function (candidate) { return id(candidate, "invalid_identity_candidate"); })));
    if (candidateIds.some(function (candidate) { return candidate !== personId; })) return { kind: "conflict", reason_code: "ambiguous_identity" };
    const fingerprint = promotionFingerprint({ person_id: personId, lead_id: args.lead.lead_id });
    if (args.existing_receipt !== null) {
      object(args.existing_receipt, "invalid_existing_receipt");
      if (args.existing_receipt.request_id !== requestId || args.existing_receipt.fingerprint !== fingerprint) return { kind: "conflict", reason_code: "request_id_reused" };
      return { kind: "replay", receipt: args.existing_receipt };
    }
    if (args.existing_client !== null) {
      object(args.existing_client, "invalid_existing_client");
      if (args.existing_client.person_id !== personId) return { kind: "conflict", reason_code: "ambiguous_identity" };
    }
    if (typeof args.committed_at !== "string" || Number.isNaN(Date.parse(args.committed_at))) fail("invalid_committed_at");
    const receipt = { status: "committed", operation: "promote_lead", request_id: requestId, fingerprint: fingerprint, person_id: personId, lead_id: args.lead.lead_id, committed_at: args.committed_at };
    return { kind: args.existing_client ? "already_promoted" : "commit", person_id: personId, lead_patch: { status: "הפך ללקוח" }, client: args.existing_client || { person_id: personId, status: "פעיל" }, receipt: receipt };
  }

  return Object.freeze({ snapshot: snapshot, decidePromotion: decidePromotion, stableStringify: stableStringify, canonicalPayment: canonicalPayment, canonicalTime: canonicalTime, canonicalExpense: canonicalExpense });
});
