/* global AssafOsSchema, AssafOsRuntime, ContentService, LockService, PropertiesService, SpreadsheetApp, Sheets, Utilities */
"use strict";

var ASSAF_OS_MAX_BODY_BYTES = 32768;
var ASSAF_OS_MAX_RESPONSE_BYTES = 262144;
var ASSAF_OS_MAX_ROWS = 2000;
var ASSAF_OS_PROPOSAL_TTL_MS = 10 * 60 * 1000;
var ASSAF_OS_BUSINESS_TIMEZONE = "Asia/Jerusalem";
var ASSAF_OS_APPROVAL_GUARD_VERSION = 1;
var ASSAF_OS_REQUEST_READ_CACHE = null;
var ASSAF_OS_REFERENCE_FINGERPRINT_PROPERTY = "ASSAF_OS_REFERENCE_VALIDATION_FINGERPRINT";
var ASSAF_OS_REFERENCE_DIRTY_PROPERTY = "ASSAF_OS_REFERENCE_VALIDATION_DIRTY";
var ASSAF_OS_META = {
  accepted: { name: "_assaf_os_accepted", headers: ["entity", "record_id", "revision", "input_fingerprint", "raw_fingerprint", "snapshot_json", "accepted_at", "source", "request_id"] },
  proposals: { name: "_assaf_os_proposals", headers: ["proposal_id", "request_id", "owner_id", "actor_source", "status", "entity", "record_id", "expected_revision", "target_fingerprint", "proposal_json", "created_at", "expires_at", "decided_at"] },
  receipts: { name: "_assaf_os_receipts", headers: ["request_id", "owner_id", "proposal_id", "status", "receipt_json", "committed_at"] },
  replay: { name: "_assaf_os_replay", headers: ["request_id", "owner_id", "operation", "actor_source", "request_hash", "seen_at"] },
  issues: { name: "_assaf_os_issues", headers: ["issue_id", "detected_at", "code", "entity", "record_id", "row_number", "status"] },
};

function doPost(event) {
  var request = null, config = null;
  try {
    config = assafOsConfig_();
    var raw = event && event.postData && event.postData.contents;
    if (typeof raw !== "string" || assafOsBytes_(raw) > ASSAF_OS_MAX_BODY_BYTES) throw assafOsError_("invalid_transport");
    var wrapper = JSON.parse(raw);
    if (!assafOsPlain_(wrapper) || Object.keys(wrapper).sort().join("|") !== "payload|signature" || typeof wrapper.payload !== "string" || !/^[0-9a-f]{64}$/.test(wrapper.signature)) throw assafOsError_("invalid_transport");
    if (!assafOsConstantTime_(wrapper.signature, assafOsHmac_(wrapper.payload, config.secret))) throw assafOsError_("invalid_signature");
    request = JSON.parse(wrapper.payload);
    AssafOsRuntime.validateEnvelope(request, config, Math.floor(Date.now() / 1000));
    var result = assafOsRun_(request, config);
    return assafOsSignedResponse_(request, true, result, config);
  } catch (caught) {
    var code = caught && (caught.assafOsCode || caught.code);
    return assafOsSignedResponse_(assafOsSafeIdentity_(request, config), false, { error: { code: code || "invalid_request" } }, config, true);
  }
}

function assafOsConfig_() {
  var props = PropertiesService.getScriptProperties();
  var config = {
    owner_id: props.getProperty("ASSAF_OS_OWNER_ID"),
    workspace_id: props.getProperty("ASSAF_OS_WORKSPACE_ID"),
    workbook_id: props.getProperty("ASSAF_OS_WORKBOOK_ID"),
    mode: props.getProperty("ASSAF_OS_MODE"),
    secret: props.getProperty("ASSAF_OS_HMAC_SECRET"),
  };
  if (!/^[1-9][0-9]{0,19}$/.test(config.owner_id || "")) throw assafOsError_("unavailable");
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(config.workspace_id || "")) throw assafOsError_("unavailable");
  if (!/^[A-Za-z0-9_-]{20,}$/.test(config.workbook_id || "")) throw assafOsError_("unavailable");
  if (config.mode !== "TEST" && config.mode !== "LIVE") throw assafOsError_("unavailable");
  if (typeof config.secret !== "string" || config.secret.length < 32) throw assafOsError_("unavailable");
  return config;
}

function assafOsRun_(request, config) {
  ASSAF_OS_REQUEST_READ_CACHE = null;
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw assafOsError_("runtime_busy");
  try {
    var workbook = SpreadsheetApp.openById(config.workbook_id);
    assafOsBeginRequestReadCache_(workbook);
    assafOsVerifyMetadata_(workbook);
    var replay = assafOsFindBy_(workbook.getSheetByName(ASSAF_OS_META.replay.name), 0, request.request_id);
    var requestHash = assafOsIdempotencyHash_(request);
    var actorReplayAllowed = replay && (String(replay[3]) === request.actor_source || ((request.operation === "approve" || request.operation === "reject") && ["OS", "TELEGRAM_APPROVAL"].indexOf(String(replay[3])) !== -1 && ["OS", "TELEGRAM_APPROVAL"].indexOf(request.actor_source) !== -1));
    if (replay && (String(replay[1]) !== request.owner_id || String(replay[2]) !== request.operation || !actorReplayAllowed || String(replay[4]) !== requestHash)) throw assafOsError_("request_id_reused");
    if (request.operation === "schema") return assafOsSchema_(config);
    if (request.operation === "snapshot") return assafOsSnapshot_(workbook, config);
    if (request.operation === "proposal") return assafOsGetProposal_(workbook, request, config);
    if (request.operation === "receipt") return assafOsGetReceipt_(workbook, request, config);
    if (request.operation === "propose") return assafOsPropose_(workbook, request, config, requestHash, replay);
    if (request.operation === "approve") return assafOsApprove_(workbook, request, config, requestHash, replay);
    if (request.operation === "reject") return assafOsReject_(workbook, request, config, requestHash, replay);
    throw assafOsError_("invalid_operation");
  } finally { ASSAF_OS_REQUEST_READ_CACHE = null; lock.releaseLock(); }
}

function assafOsSchema_(config) {
  return { api_version: AssafOsRuntime.API_VERSION, source: "google_sheets", mode: config.mode, workbook_id: config.workbook_id, workspace_id: config.workspace_id, schema_version: AssafOsSchema.version, money_unit: "minor", tabs: AssafOsSchema.tabs, enums: AssafOsSchema.enums };
}

function assafOsPropose_(workbook, request, config, requestHash, replay) {
  var data = request.data;
  AssafOsRuntime.exact(data, ["request_id", "entity", "record_id", "expected_revision", "input"], "invalid_propose_shape");
  if (data.request_id !== request.request_id) throw assafOsError_("request_id_mismatch");
  if (!Number.isSafeInteger(data.expected_revision) || data.expected_revision < 0) throw assafOsError_("invalid_expected_revision");
  if (replay) {
    var existing = assafOsFindBy_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), 1, request.request_id);
    if (!existing) throw assafOsError_("request_state_missing");
    return assafOsPublicProposal_(JSON.parse(existing[9]), config);
  }
  var state = assafOsReadEntity_(workbook, data.entity === "promote_lead" ? "leads" : data.entity);
  var now = new Date(), before = null, after = null, recordId = data.record_id;
  if (data.entity === "promote_lead") {
    if (typeof recordId !== "string" || Object.keys(data.input).length !== 0) throw assafOsError_("invalid_promotion_input");
    var lead = state.byId[recordId];
    if (!lead || lead.revision !== data.expected_revision) throw assafOsError_("target_changed");
    var clients = assafOsReadEntity_(workbook, "clients");
    var duplicates = clients.records.filter(function (client) { return client.person_id === lead.person_id; });
    if (duplicates.length > 1) throw assafOsError_("ambiguous_identity");
    before = { lead: lead, client: duplicates[0] || null };
    after = { lead: Object.assign({}, lead, { status: "הפך ללקוח" }), client: duplicates[0] || { person_id: lead.person_id, name: lead.name, phone: lead.phone || null, email: lead.email || null, status: "פעיל", notes: null } };
  } else {
    if (data.record_id === null) {
      if (data.expected_revision !== 0) throw assafOsError_("invalid_expected_revision");
      recordId = assafOsNewRecordId_(data.entity);
      after = AssafOsRuntime.canonicalInput(data.entity, data.input, true);
      if (data.entity === "leads") after.person_id = assafOsNewRecordId_("persons");
    } else {
      before = state.byId[data.record_id] || null;
      if (!before || before.revision !== data.expected_revision) throw assafOsError_("target_changed");
      after = Object.assign({}, before, AssafOsRuntime.canonicalInput(data.entity, data.input, false));
    }
    after[assafOsIdKey_(data.entity)] = recordId;
    if (before) {
      AssafOsSchema.tab(data.entity).fields.filter(function (field) { return field.immutableAfterCreate; }).forEach(function (field) { if (after[field.key] !== before[field.key]) throw assafOsError_("immutable_field_changed"); });
    }
    assafOsValidateDependencies_(workbook, data.entity, after);
    if (data.entity === "leads" && data.record_id === null && assafOsLeadContactDependencies_(workbook, recordId, after).length) throw assafOsError_("lead_already_exists");
    if (data.entity === "payments") AssafOsRuntime.validatePaymentGraph(assafOsProspectivePayments_(workbook, after));
    if (data.entity === "expenses") AssafOsRuntime.dedupeExpense(after, assafOsReadEntity_(workbook, "expenses").records);
  }
  var target = assafOsTarget_(workbook, data.entity, recordId, after), created = now.toISOString(), expires = new Date(now.getTime() + ASSAF_OS_PROPOSAL_TTL_MS).toISOString();
  var proposal = AssafOsRuntime.makeProposal({ proposal_id: "PR_" + Utilities.getUuid().replace(/-/g, ""), request_id: request.request_id, approval_request_id: Utilities.getUuid().toLowerCase(), rejection_request_id: Utilities.getUuid().toLowerCase(), owner_id: request.owner_id, source: request.actor_source, entity: data.entity, record_id: recordId, expected_revision: data.expected_revision, input: data.input, before: before, after: after, target_fingerprint: AssafOsRuntime.proposalFingerprint(target), created_at: created, expires_at: expires });
  var pRow = [proposal.proposal_id, proposal.request_id, proposal.owner_id, proposal.source, "pending", proposal.entity, proposal.record_id, proposal.expected_revision, proposal.target_fingerprint, JSON.stringify(proposal), created, expires, ""];
  var requests = [assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), pRow), assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.replay.name), [request.request_id, request.owner_id, request.operation, request.actor_source, requestHash, created])];
  Sheets.Spreadsheets.batchUpdate({ requests: requests }, config.workbook_id);
  assafOsInvalidateMetadataCaches_(workbook, ["proposals", "replay"]);
  return assafOsPublicProposal_(proposal, config);
}

function assafOsValidateDependencies_(workbook, entity, record) {
  if (["works", "payments", "time", "tasks"].indexOf(entity) === -1) return;
  if (record.person_id && !assafOsReadEntity_(workbook, "clients").byId[record.person_id]) throw assafOsError_("client_not_found");
  if (record.work_id && entity !== "works") {
    var work = assafOsReadEntity_(workbook, "works").byId[record.work_id];
    if (!work) throw assafOsError_("work_not_found");
    if (record.person_id && work.person_id !== record.person_id) throw assafOsError_("work_person_mismatch");
    if (record.path && work.path !== record.path) throw assafOsError_("work_path_mismatch");
    if (record.currency && work.currency !== record.currency) throw assafOsError_("work_currency_mismatch");
  }
}
function assafOsProspectivePayments_(workbook, candidate) { var found = false, rows = assafOsReadEntity_(workbook, "payments").records.map(function (payment) { if (payment.payment_id === candidate.payment_id) { found = true; return candidate; } return payment; }); if (!found) rows.push(candidate); return rows; }

function assafOsApprove_(workbook, request, config, requestHash, replay) {
  var data = request.data;
  AssafOsRuntime.exact(data, ["proposal_id", "request_id"], "invalid_approve_shape");
  if (data.request_id !== request.request_id) throw assafOsError_("request_id_mismatch");
  if (replay) return assafOsGetReceipt_(workbook, { data: { request_id: request.request_id }, owner_id: request.owner_id }, config);
  var found = assafOsFindBy_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), 0, data.proposal_id, true);
  if (!found) throw assafOsError_("proposal_not_found");
  if (String(found.values[4]) !== "pending") {
    var terminal = assafOsFindBy_(workbook.getSheetByName(ASSAF_OS_META.receipts.name), 2, data.proposal_id);
    if (terminal && String(terminal[1]) === request.owner_id) return JSON.parse(terminal[4]);
    throw assafOsError_("proposal_not_pending");
  }
  var proposal = JSON.parse(found.values[9]);
  if (request.request_id !== proposal.approval_request_id) throw assafOsError_("approval_request_mismatch");
  var target = assafOsTarget_(workbook, proposal.entity, proposal.record_id, proposal.after);
  var decision = AssafOsRuntime.approveDecision(proposal, { owner_id: request.owner_id, now: new Date().toISOString(), current_revision: target.revision, current_fingerprint: AssafOsRuntime.proposalFingerprint(target) });
  if (decision.status !== "commit") return assafOsTerminalProposal_(workbook, found, proposal, decision.status, request, config, requestHash);
  assafOsValidateDependencies_(workbook, proposal.entity, proposal.entity === "promote_lead" ? proposal.after.client : proposal.after);
  if (proposal.entity === "payments") AssafOsRuntime.validatePaymentGraph(assafOsProspectivePayments_(workbook, proposal.after));
  if (proposal.entity === "expenses") AssafOsRuntime.dedupeExpense(proposal.after, assafOsReadEntity_(workbook, "expenses").records);
  var now = new Date().toISOString(), receipt = { api_version: AssafOsRuntime.API_VERSION, source: "google_sheets", actor_source: proposal.source, mode: config.mode, workbook_id: config.workbook_id, workspace_id: config.workspace_id, status: "committed", request_id: request.request_id, proposal_id: proposal.proposal_id, entity: proposal.entity, record_id: proposal.record_id, revision: decision.revision, committed_at: now, reason_codes: [] };
  var changes = assafOsCommitRequests_(workbook, proposal, decision.revision, request.request_id, now);
  if (assafOsCanProject_(workbook, proposal)) changes = changes.concat(assafOsProjectionRequests_(workbook, proposal));
  changes.push(assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.receipts.name), [request.request_id, request.owner_id, proposal.proposal_id, "committed", JSON.stringify(receipt), now]));
  changes.push(assafOsCellRequest_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), found.row, 5, "committed"));
  changes.push(assafOsCellRequest_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), found.row, 13, now));
  changes.push(assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.replay.name), [request.request_id, request.owner_id, request.operation, request.actor_source, requestHash, now]));
  Sheets.Spreadsheets.batchUpdate({ requests: changes }, config.workbook_id);
  assafOsInvalidateMetadataCaches_(workbook, ["accepted", "proposals", "receipts", "replay"]);
  if (proposal.entity === "promote_lead") { assafOsInvalidateEntityCache_(workbook, "leads"); assafOsInvalidateEntityCache_(workbook, "clients"); }
  else assafOsInvalidateEntityCache_(workbook, proposal.entity);
  assafOsInvalidateEntityCache_(workbook, "clients"); assafOsInvalidateEntityCache_(workbook, "works");
  if (["clients", "works", "payments", "promote_lead"].indexOf(proposal.entity) !== -1) assafOsRefreshReferenceValidationsSafely_(workbook);
  return receipt;
}

function assafOsReject_(workbook, request, config, requestHash, replay) {
  AssafOsRuntime.exact(request.data, ["proposal_id", "request_id"], "invalid_reject_shape");
  if (request.data.request_id !== request.request_id) throw assafOsError_("request_id_mismatch");
  if (replay) return assafOsGetReceipt_(workbook, { data: { request_id: request.request_id }, owner_id: request.owner_id }, config);
  var found = assafOsFindBy_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), 0, request.data.proposal_id, true);
  if (!found) throw assafOsError_("proposal_not_found");
  if (String(found.values[4]) !== "pending") {
    var terminal = assafOsFindBy_(workbook.getSheetByName(ASSAF_OS_META.receipts.name), 2, request.data.proposal_id);
    if (terminal && String(terminal[1]) === request.owner_id) return JSON.parse(terminal[4]);
    throw assafOsError_("proposal_not_pending");
  }
  var proposal = JSON.parse(found.values[9]);
  if (request.request_id !== proposal.rejection_request_id) throw assafOsError_("rejection_request_mismatch");
  if (proposal.owner_id !== request.owner_id) throw assafOsError_("proposal_not_pending");
  return assafOsTerminalProposal_(workbook, found, proposal, "rejected", request, config, requestHash);
}

function assafOsTerminalProposal_(workbook, found, proposal, status, request, config, requestHash) {
  var now = new Date().toISOString(), receipt = { api_version: AssafOsRuntime.API_VERSION, source: "google_sheets", actor_source: proposal.source, mode: config.mode, workbook_id: config.workbook_id, workspace_id: config.workspace_id, status: status, request_id: request.request_id, proposal_id: proposal.proposal_id, entity: proposal.entity, record_id: proposal.record_id, revision: proposal.expected_revision, committed_at: now, reason_codes: [status === "expired" ? "proposal_expired" : status] };
  Sheets.Spreadsheets.batchUpdate({ requests: [
    assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.receipts.name), [request.request_id, request.owner_id, proposal.proposal_id, status, JSON.stringify(receipt), now]),
    assafOsCellRequest_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), found.row, 5, status), assafOsCellRequest_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), found.row, 13, now),
    assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.replay.name), [request.request_id, request.owner_id, request.operation, request.actor_source, requestHash, now]),
  ] }, config.workbook_id);
  assafOsInvalidateMetadataCaches_(workbook, ["proposals", "receipts", "replay"]);
  return receipt;
}

function assafOsGetProposal_(workbook, request, config) {
  AssafOsRuntime.exact(request.data, ["proposal_id"], "invalid_proposal_lookup");
  var row = assafOsFindBy_(workbook.getSheetByName(ASSAF_OS_META.proposals.name), 0, request.data.proposal_id);
  if (!row || String(row[2]) !== request.owner_id) throw assafOsError_("proposal_not_found");
  var proposal = JSON.parse(row[9]); proposal.status = String(row[4]);
  if (proposal.status === "pending" && Date.parse(proposal.expires_at) <= Date.now()) proposal.status = "expired";
  return assafOsPublicProposal_(proposal, config);
}

function assafOsGetReceipt_(workbook, request, config) {
  AssafOsRuntime.exact(request.data, ["request_id"], "invalid_receipt_lookup");
  var row = assafOsFindBy_(workbook.getSheetByName(ASSAF_OS_META.receipts.name), 0, request.data.request_id);
  if (!row || String(row[1]) !== request.owner_id) return { api_version: AssafOsRuntime.API_VERSION, source: "google_sheets", actor_source: request.actor_source, mode: config.mode, workbook_id: config.workbook_id, workspace_id: config.workspace_id, status: "unknown", request_id: request.data.request_id, proposal_id: null, entity: null, record_id: null, revision: null, committed_at: null, reason_codes: ["receipt_not_found"] };
  return JSON.parse(row[4]);
}

function assafOsPublicProposal_(proposal, config) {
  return { api_version: AssafOsRuntime.API_VERSION, source: "google_sheets", actor_source: proposal.source, mode: config.mode, workbook_id: config.workbook_id, workspace_id: config.workspace_id, approval_guard_version: ASSAF_OS_APPROVAL_GUARD_VERSION, status: proposal.status, proposal_id: proposal.proposal_id, request_id: proposal.request_id, approval_request_id: proposal.approval_request_id, rejection_request_id: proposal.rejection_request_id, expires_at: proposal.expires_at, entity: proposal.entity, record_id: proposal.record_id, expected_revision: proposal.expected_revision, input: proposal.input, before: proposal.before, after: proposal.after };
}

function assafOsCommitRequests_(workbook, proposal, revision, requestId, now) {
  if (proposal.entity === "promote_lead") {
    assafOsAssertAppendCapacity_(workbook.getSheetByName(ASSAF_OS_META.accepted.name), 2);
    return assafOsWriteRecordRequests_(workbook, "leads", proposal.after.lead, revision, proposal.source, requestId, now)
      .concat(assafOsWriteRecordRequests_(workbook, "clients", proposal.after.client, proposal.after.client.revision ? proposal.after.client.revision + 1 : 1, proposal.source, requestId, now));
  }
  return assafOsWriteRecordRequests_(workbook, proposal.entity, proposal.after, revision, proposal.source, requestId, now);
}

function assafOsProjectionRequests_(workbook, proposal) {
  var entities = ["clients", "works", "payments", "time", "expenses"], records = {}, states = {};
  entities.forEach(function (entity) { states[entity] = assafOsReadEntity_(workbook, entity); records[entity] = states[entity].records.slice(); });
  function replace(entity, record) {
    var key = assafOsIdKey_(entity), found = false;
    records[entity] = records[entity].map(function (current) { if (current[key] === record[key]) { found = true; return Object.assign({}, current, record); } return current; });
    if (!found) records[entity].push(record);
  }
  if (proposal.entity === "promote_lead") replace("clients", proposal.after.client);
  else if (entities.indexOf(proposal.entity) !== -1) replace(proposal.entity, proposal.after);
  var requests = [], works = {};
  records.works.forEach(function (work) {
    var payments = records.payments.filter(function (row) { return row.work_id === work.work_id && row.kind !== "בוטל"; });
    var times = records.time.filter(function (row) { return row.work_id === work.work_id && row.status !== "בוטל"; });
    var minutes = times.reduce(function (sum, row) { return sum + (row.minutes || 0); }, 0);
    var received = work.currency === "ILS" ? payments.filter(function (row) { return row.currency === "ILS"; }).reduce(function (sum, row) { return sum + (row.kind === "החזר" ? -row.actual_amount_ils : row.actual_amount_ils); }, 0) : null;
    works[work.work_id] = { received: received, minutes: minutes, hourly: received !== null && minutes > 0 ? Math.round(received * 60 / minutes) : null, foreign: payments.some(function (row) { return row.currency !== "ILS"; }) };
    var sheet = workbook.getSheetByName(AssafOsSchema.tab("works").name), rowNumber = states.works.rowById[work.work_id];
    if (!rowNumber && proposal.entity === "works" && proposal.record_id === work.work_id) rowNumber = Math.max(sheet.getLastRow() + 1, 2);
    var desired = [received === null ? "" : AssafOsRuntime.minorToMajor(received), minutes / 60, works[work.work_id].hourly === null ? "" : AssafOsRuntime.minorToMajor(works[work.work_id].hourly), works[work.work_id].foreign];
    var current = rowNumber && states.works.rows[rowNumber - 1] ? states.works.rows[rowNumber - 1].slice(13, 17) : [];
    if (rowNumber && !assafOsProjectionValuesEqual_(current, desired)) requests.push(assafOsRangeRequest_(sheet, rowNumber, 14, desired));
  });
  records.clients.forEach(function (client) {
    var owned = records.works.filter(function (work) { return work.person_id === client.person_id && work.currency === "ILS"; });
    var learning = 0, automation = 0, minutes = 0, missing = false;
    owned.forEach(function (work) { var calc = works[work.work_id] || { received: 0, minutes: 0 }; if (work.path === "לימוד") learning += calc.received || 0; else automation += calc.received || 0; minutes += calc.minutes; if (calc.received && !calc.minutes) missing = true; });
    var total = learning + automation, hourly = !missing && minutes > 0 ? Math.round(total * 60 / minutes) : null;
    var sheet = workbook.getSheetByName(AssafOsSchema.tab("clients").name), rowNumber = states.clients.rowById[client.person_id];
    if (!rowNumber && ((proposal.entity === "clients" && proposal.record_id === client.person_id) || (proposal.entity === "promote_lead" && proposal.after.client.person_id === client.person_id))) rowNumber = Math.max(sheet.getLastRow() + 1, 2);
    var desired = [AssafOsRuntime.minorToMajor(learning), AssafOsRuntime.minorToMajor(automation), AssafOsRuntime.minorToMajor(total), minutes / 60, hourly === null ? "" : AssafOsRuntime.minorToMajor(hourly), records.payments.some(function (row) { return row.person_id === client.person_id && row.currency !== "ILS" && row.kind !== "בוטל"; })];
    var current = rowNumber && states.clients.rows[rowNumber - 1] ? states.clients.rows[rowNumber - 1].slice(11, 17) : [];
    if (rowNumber && !assafOsProjectionValuesEqual_(current, desired)) requests.push(assafOsRangeRequest_(sheet, rowNumber, 12, desired));
  });
  var completeCoverage = { leads: "complete", clients: "complete", works: "complete", payments: "complete", time: "complete", expenses: "complete", tasks: "complete" };
  var summary = assafOsSummary_(records, completeCoverage, "complete"), summaryTab = AssafOsSchema.tab("summary"), summarySheet = workbook.getSheetByName(summaryTab.name);
  var summaryBusinessRows = summary.monthly.map(function (row) { return ["business", row.month, "", "תזרים עסקי", "", row.currency, row.paid_income_ils === null ? "" : AssafOsRuntime.minorToMajor(row.paid_income_ils), row.recorded_minutes === null ? "" : row.recorded_minutes / 60, row.effective_hourly_ils === null ? "" : AssafOsRuntime.minorToMajor(row.effective_hourly_ils), row.reason_codes.indexOf("missing_time") !== -1, row.paid_expenses_ils === null ? "" : AssafOsRuntime.minorToMajor(row.paid_expenses_ils), row.net_cash_ils === null ? "" : AssafOsRuntime.minorToMajor(row.net_cash_ils), row.coverage]; });
  var summaryRaw = assafOsReadSheetOnce_(summarySheet), currentSummary = summaryRaw.values.slice(1), summaryMatches = currentSummary.length === summaryBusinessRows.length;
  if (summaryMatches) summaryBusinessRows.forEach(function (row, index) { if (!assafOsProjectionValuesEqual_(currentSummary[index].slice(0, 13), row) || !currentSummary[index][13] || (summaryRaw.formulas[index + 1] || []).some(function (formula) { return formula !== ""; })) summaryMatches = false; });
  if (!summaryMatches) {
    var calculatedAt = new Date().toISOString(), summaryRows = summaryBusinessRows.map(function (row) { return row.concat([calculatedAt]); });
    requests.push({ repeatCell: { range: { sheetId: summarySheet.getSheetId(), startRowIndex: 1, endRowIndex: summarySheet.getMaxRows(), startColumnIndex: 0, endColumnIndex: summaryTab.fields.length }, cell: { userEnteredValue: null }, fields: "userEnteredValue" } });
    summaryRows.forEach(function (row, index) { requests.push(assafOsRowRequest_(summarySheet, index + 2, row)); });
  }
  return requests;
}

function assafOsProjectionValuesEqual_(current, desired) { return AssafOsRuntime.stableStringify(current.map(function (value) { return value === "" ? null : value; })) === AssafOsRuntime.stableStringify(desired.map(function (value) { return value === "" ? null : value; })); }

function assafOsWriteRecordRequests_(workbook, entity, record, revision, source, requestId, now) {
  var tab = AssafOsSchema.tab(entity), sheet = workbook.getSheetByName(tab.name), state = assafOsReadEntity_(workbook, entity), idKey = assafOsIdKey_(entity);
  var persisted = Object.assign({}, record); persisted[idKey] = record[idKey]; persisted.revision = revision; persisted.write_source = source; persisted.request_id = requestId;
  if (tab.fields.some(function (f) { return f.key === "created_at"; }) && !persisted.created_at) persisted.created_at = now;
  if (tab.fields.some(function (f) { return f.key === "updated_at"; })) persisted.updated_at = now;
  var row = state.rowById[persisted[idKey]] || Math.max(sheet.getLastRow() + 1, 2), values = assafOsRecordToRow_(tab, persisted, workbook);
  var current = state.rowById[persisted[idKey]] ? sheet.getRange(row, 1, 1, tab.fields.length).getValues()[0] : tab.fields.map(function () { return ""; });
  var input = {}; inputFieldsFor_(tab).forEach(function (field) { input[field.key] = persisted[field.key] == null ? null : persisted[field.key]; });
  var snap = { entity: entity, record_id: persisted[idKey], revision: revision, input: input };
  var requests = [];
  tab.fields.forEach(function (field, index) { if (field.role !== "calculated" && AssafOsRuntime.stableStringify(current[index] === "" ? null : current[index]) !== AssafOsRuntime.stableStringify(values[index] === "" ? null : values[index])) requests.push(assafOsCellRequest_(sheet, row, index + 1, values[index])); });
  requests.push(assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.accepted.name), [entity, persisted[idKey], revision, assafOsSha_(AssafOsRuntime.stableStringify(input)), assafOsRawOwnedFingerprint_(tab, values, workbook), JSON.stringify(snap), now, source, requestId]));
  return requests;
}

function assafOsReadEntity_(workbook, entity) {
  var cached = assafOsCachedEntity_("strict", entity);
  if (cached) return cached;
  var tab = AssafOsSchema.tab(entity), sheet = tab && workbook.getSheetByName(tab.name);
  if (!tab || !sheet) throw assafOsError_("missing_entity_sheet");
  var raw = assafOsReadSheetOnce_(sheet), values = raw.values;
  if (values.length > ASSAF_OS_MAX_ROWS + 1) throw assafOsError_("row_limit_exceeded");
  assafOsAssertHeaders_(sheet, tab, values[0], raw.last_column);
  var records = [], byId = {}, rowById = {}, idKey = assafOsIdKey_(entity);
  var formulas = raw.formulas;
  for (var i = 1; i < values.length; i += 1) {
    if (values[i].every(function (cell) { return cell === ""; })) continue;
    if ((formulas[i] || []).some(function (formula) { return formula !== ""; })) throw assafOsError_("formula_drift");
    var record = assafOsRowToRecord_(tab, values[i], workbook);
    if (!record[idKey] || byId[record[idKey]]) throw assafOsError_("invalid_or_duplicate_record_id");
    records.push(record); byId[record[idKey]] = record; rowById[record[idKey]] = i + 1;
  }
  return assafOsStoreCachedEntity_("strict", entity, { records: records, byId: byId, rowById: rowById, rows: values });
}

function assafOsReadEntityTolerant_(workbook, entity) {
  var cached = assafOsCachedEntity_("tolerant", entity);
  if (cached) return cached;
  var tab = AssafOsSchema.tab(entity), sheet = tab && workbook.getSheetByName(tab.name), result = { records: [], coverage: "complete", issues: [] };
  if (!tab || !sheet) return { records: [], coverage: "unknown", issues: [{ code: "missing_entity_sheet", entity: entity, record_id: null }] };
  var raw = assafOsReadSheetOnce_(sheet), values = raw.values;
  try { if (values.length > ASSAF_OS_MAX_ROWS + 1) throw assafOsError_("row_limit_exceeded"); assafOsAssertHeaders_(sheet, tab, values[0], raw.last_column); }
  catch (error) { return { records: [], coverage: "unknown", issues: [{ code: error.code || "header_drift", entity: entity, record_id: null }] }; }
  var seen = {}, idKey = assafOsIdKey_(entity), formulas = raw.formulas;
  for (var i = 1; i < values.length; i += 1) {
    if (values[i].every(function (cell) { return cell === ""; })) continue;
    try {
      if ((formulas[i] || []).some(function (formula) { return formula !== ""; })) throw assafOsError_("formula_drift");
      var record = assafOsRowToRecord_(tab, values[i], workbook);
      if (!record[idKey] || seen[record[idKey]]) throw assafOsError_(seen[record[idKey]] ? "duplicate_record_id" : "invalid_record_id");
      seen[record[idKey]] = true; result.records.push(record);
    } catch (error) { result.coverage = "partial"; result.issues.push({ code: error.assafOsCode || error.code || "invalid_row", entity: entity, record_id: null }); }
  }
  return assafOsStoreCachedEntity_("tolerant", entity, result);
}

function assafOsBeginRequestReadCache_(workbook) { if (!ASSAF_OS_REQUEST_READ_CACHE || ASSAF_OS_REQUEST_READ_CACHE.workbook_id !== workbook.getId()) ASSAF_OS_REQUEST_READ_CACHE = { workbook_id: workbook.getId(), sheets: {}, values: {}, strict: {}, tolerant: {} }; }
function assafOsReadSheetOnce_(sheet) {
  var cache = ASSAF_OS_REQUEST_READ_CACHE, key = String(sheet.getSheetId());
  if (cache && cache.workbook_id === sheet.getParent().getId() && cache.sheets[key]) return cache.sheets[key];
  var range = sheet.getDataRange(), raw = { values: range.getValues(), formulas: range.getFormulas(), last_column: sheet.getLastColumn() };
  if (cache && cache.workbook_id === sheet.getParent().getId()) cache.sheets[key] = raw;
  return raw;
}
function assafOsReadValuesOnce_(sheet) {
  var cache = ASSAF_OS_REQUEST_READ_CACHE, key = String(sheet.getSheetId());
  if (cache && cache.workbook_id === sheet.getParent().getId()) {
    if (cache.sheets[key]) return cache.sheets[key].values;
    if (cache.values[key]) return cache.values[key];
  }
  var values = sheet.getDataRange().getValues();
  if (cache && cache.workbook_id === sheet.getParent().getId()) cache.values[key] = values;
  return values;
}
function assafOsCachedEntity_(kind, entity) { return ASSAF_OS_REQUEST_READ_CACHE && ASSAF_OS_REQUEST_READ_CACHE[kind][entity] || null; }
function assafOsStoreCachedEntity_(kind, entity, value) { if (ASSAF_OS_REQUEST_READ_CACHE) ASSAF_OS_REQUEST_READ_CACHE[kind][entity] = value; return value; }
function assafOsInvalidateEntityCache_(workbook, entity) {
  if (!ASSAF_OS_REQUEST_READ_CACHE || ASSAF_OS_REQUEST_READ_CACHE.workbook_id !== workbook.getId()) return;
  var tab = AssafOsSchema.tab(entity), sheet = tab && workbook.getSheetByName(tab.name);
  if (sheet) delete ASSAF_OS_REQUEST_READ_CACHE.sheets[String(sheet.getSheetId())];
  delete ASSAF_OS_REQUEST_READ_CACHE.strict[entity]; delete ASSAF_OS_REQUEST_READ_CACHE.tolerant[entity];
}
function assafOsInvalidateSheetCache_(workbook, sheet) {
  if (ASSAF_OS_REQUEST_READ_CACHE && ASSAF_OS_REQUEST_READ_CACHE.workbook_id === workbook.getId() && sheet) { delete ASSAF_OS_REQUEST_READ_CACHE.sheets[String(sheet.getSheetId())]; delete ASSAF_OS_REQUEST_READ_CACHE.values[String(sheet.getSheetId())]; }
}
function assafOsInvalidateMetadataCaches_(workbook, keys) { keys.forEach(function (key) { var spec = ASSAF_OS_META[key]; if (spec) assafOsInvalidateSheetCache_(workbook, workbook.getSheetByName(spec.name)); }); }

function assafOsLeadPhoneIdentity_(value) {
  var raw = String(value || "").trim();
  if (!raw || !/^\+?[0-9][0-9 ().-]*$/.test(raw)) return "";
  if ((raw.match(/\(/g) || []).length !== (raw.match(/\)/g) || []).length) return "";
  var digits = raw.replace(/[^0-9]/g, "");
  if (raw.charAt(0) === "+") {
    if (!/^[1-9][0-9]{6,14}$/.test(digits) || /^9720/.test(digits)) return "";
    if (/^9725/.test(digits) && !/^9725[0-9]{8}$/.test(digits)) return "";
    return "+" + digits;
  }
  if (/^05/.test(digits)) return /^05[0-9]{8}$/.test(digits) ? "+972" + digits.slice(1) : "";
  return /^[0-9]{7,15}$/.test(digits) ? digits : "";
}

function assafOsLeadContactDependencies_(workbook, recordId, candidate) {
  if (!candidate) return [];
  var phone = assafOsLeadPhoneIdentity_(candidate.phone), email = String(candidate.email || "").trim().toLowerCase();
  if (!phone && !email) return [];
  return assafOsReadEntity_(workbook, "leads").records.filter(function (lead) {
    if (lead.lead_id === recordId) return false;
    var samePhone = phone && assafOsLeadPhoneIdentity_(lead.phone) === phone;
    var sameEmail = email && String(lead.email || "").trim().toLowerCase() === email;
    return Boolean(samePhone || sameEmail);
  });
}

function assafOsTarget_(workbook, entity, recordId, candidate) {
  if (entity === "promote_lead") {
    var lead = assafOsReadEntity_(workbook, "leads").byId[recordId] || null;
    var client = lead ? assafOsReadEntity_(workbook, "clients").records.filter(function (row) { return row.person_id === lead.person_id; }) : [];
    return { revision: lead ? lead.revision : 0, lead: lead, lead_accepted: lead ? assafOsLatestAccepted_(workbook, "leads", recordId) : null, clients: client, client_accepted: client.map(function (row) { return assafOsLatestAccepted_(workbook, "clients", row.person_id); }) };
  }
  var row = assafOsReadEntity_(workbook, entity).byId[recordId] || null;
  var result = row ? { revision: row.revision || 0, record: row, accepted: assafOsLatestAccepted_(workbook, entity, recordId) } : { revision: 0, record: null, accepted: null };
  if (entity === "leads" && candidate) result.lead_contact_dependencies = assafOsLeadContactDependencies_(workbook, recordId, candidate);
  if (candidate && candidate.person_id) result.client_dependency = { record: assafOsReadEntity_(workbook, "clients").byId[candidate.person_id] || null, accepted: assafOsLatestAccepted_(workbook, "clients", candidate.person_id) };
  if (candidate && candidate.work_id && entity !== "works") result.work_dependency = { record: assafOsReadEntity_(workbook, "works").byId[candidate.work_id] || null, accepted: assafOsLatestAccepted_(workbook, "works", candidate.work_id) };
  if (entity === "payments" && candidate) {
    var payments = assafOsReadEntity_(workbook, "payments").records;
    result.payment_dependencies = payments.filter(function (payment) { return payment.payment_id === candidate.payment_id || payment.original_payment_id === candidate.payment_id || (candidate.original_payment_id && (payment.payment_id === candidate.original_payment_id || payment.original_payment_id === candidate.original_payment_id)); });
  }
  if (entity === "expenses" && candidate) result.source_dependencies = assafOsReadEntity_(workbook, "expenses").records.filter(function (expense) { return expense.source_type === candidate.source_type && expense.source_key === candidate.source_key; });
  return result;
}

function assafOsSnapshot_(workbook, config) {
  var reconcile = assafOsReconcileAll_(workbook);
  var issues = assafOsOpenIssues_(workbook).concat(reconcile.header_issues || []), records = {}, entityCoverage = {}, keys = ["leads", "clients", "works", "payments", "time", "expenses", "tasks"];
  keys.forEach(function (entity) {
    var read = assafOsReadEntityTolerant_(workbook, entity);
    records[entity] = read.records; entityCoverage[entity] = read.coverage; issues = issues.concat(read.issues);
  });
  var pending = assafOsReadValuesOnce_(workbook.getSheetByName(ASSAF_OS_META.proposals.name)).slice(1).filter(function (row) { return row[4] === "pending" && Date.parse(row[11]) > Date.now(); }).map(function (row) { return assafOsPublicProposal_(JSON.parse(row[9]), config); });
  issues = issues.filter(function (issue, index, all) { return all.findIndex(function (item) { return item.code === issue.code && item.entity === issue.entity && item.record_id === issue.record_id; }) === index; });
  keys.forEach(function (entity) { if (issues.some(function (issue) { return issue.entity === entity; }) && entityCoverage[entity] === "complete") entityCoverage[entity] = "partial"; });
  if (entityCoverage.payments !== "complete" || entityCoverage.time !== "complete") entityCoverage.works = entityCoverage.works === "unknown" ? "unknown" : "partial";
  if (entityCoverage.works !== "complete" || entityCoverage.payments !== "complete" || entityCoverage.time !== "complete") entityCoverage.clients = entityCoverage.clients === "unknown" ? "unknown" : "partial";
  if (entityCoverage.works !== "complete") records.works = assafOsWithoutCalculated_("works", records.works);
  if (entityCoverage.clients !== "complete") records.clients = assafOsWithoutCalculated_("clients", records.clients);
  var coverage = keys.every(function (entity) { return entityCoverage[entity] === "complete"; }) ? "complete" : (keys.some(function (entity) { return entityCoverage[entity] !== "unknown"; }) ? "partial" : "unknown");
  return { api_version: AssafOsRuntime.API_VERSION, source: "google_sheets", mode: config.mode, workbook_id: config.workbook_id, workspace_id: config.workspace_id, observed_at: new Date().toISOString(), coverage: coverage, entity_coverage: entityCoverage, records: records, summary: assafOsSummary_(records, entityCoverage, coverage), issues: issues, pending_proposals: pending };
}

function assafOsWithoutCalculated_(entity, records) {
  var calculated = AssafOsSchema.tab(entity).fields.filter(function (field) { return field.role === "calculated"; });
  return records.map(function (record) { var safe = Object.assign({}, record); calculated.forEach(function (field) { safe[field.key] = null; }); return safe; });
}

function assafOsSummary_(records, entityCoverage, overallCoverage) {
  var months = {}, workById = {};
  records.works.forEach(function (work) { workById[work.work_id] = work; });
  function month(date) { return String(date || "").slice(0, 7); }
  function row(monthValue, currency) { var key = monthValue + "|" + currency; return months[key] || (months[key] = { month: monthValue, currency: currency, income: 0, expenses: 0, minutes: 0 }); }
  records.payments.forEach(function (row) {
    var item = row_(month(row.date), row.currency);
    if (row.kind !== "בוטל") item.income += row.kind === "החזר" ? -row.actual_amount_ils : row.actual_amount_ils;
  });
  records.expenses.forEach(function (expense) {
    var item = row_(month(expense.date), expense.currency);
    if (expense.payment_status === "שולם" && expense.source_type === "תשלום בפועל") item.expenses += expense.amount_ils;
  });
  records.time.forEach(function (entry) { var work = workById[entry.work_id], item = row_(month(entry.date), work && work.currency || "ILS"); if (entry.status !== "בוטל") item.minutes += entry.minutes || 0; });
  function row_(monthValue, currency) { return row(monthValue, currency); }
  var monthly = Object.keys(months).sort().map(function (key) {
    var value = months[key], reasons = [];
    var income = entityCoverage.payments === "complete" ? value.income : null;
    var expenses = entityCoverage.expenses === "complete" ? value.expenses : null;
    var minutes = entityCoverage.time === "complete" && entityCoverage.works === "complete" ? value.minutes : null;
    var evidence = minutes === null ? null : AssafOsCore.paidWorkTimeEvidence(
      records.payments.map(function (payment) { return { payment_id: payment.payment_id, person_id: payment.person_id, work_id: payment.work_id, path: payment.path, date: payment.date, amount_agorot: payment.actual_amount_ils, currency: payment.currency, kind: payment.kind, original_payment_id: payment.original_payment_id }; }),
      records.time.map(function (entry) { return { time_id: entry.time_id, person_id: entry.person_id, work_id: entry.work_id, path: entry.path, date: entry.date, minutes: entry.minutes, status: entry.status }; }),
      records.works.map(function (work) { return { work_id: work.work_id, person_id: work.person_id, path: work.path, currency: work.currency }; }),
      { month: value.month, currency: value.currency }
    );
    if (income === null) reasons.push("payments_coverage_partial");
    if (expenses === null) reasons.push("expenses_coverage_partial");
    if (minutes === null) reasons.push("time_coverage_partial");
    else if (evidence.missing_time || (income !== null && income !== 0 && minutes === 0)) reasons.push("missing_time");
    var net = income === null || expenses === null ? null : income - expenses;
    var hourly = income !== null && minutes !== null && minutes > 0 && !evidence.missing_time ? Math.round(income * 60 / minutes) : null;
    return { month: value.month, currency: value.currency, paid_income_ils: income, paid_expenses_ils: expenses, net_cash_ils: net, recorded_minutes: minutes, effective_hourly_ils: hourly, coverage: reasons.length ? "partial" : "complete", reason_codes: reasons };
  });
  return { coverage: overallCoverage, monthly: monthly };
}

function reconcileAssafOsEdit(event) {
  ASSAF_OS_REQUEST_READ_CACHE = null;
  var config = assafOsConfig_(), workbook = SpreadsheetApp.openById(config.workbook_id);
  if (!event || !event.range || event.range.getSheet().getParent().getId() !== config.workbook_id) return;
  var sheet = event.range.getSheet(), tab = AssafOsSchema.tabs.filter(function (item) { return item.name === sheet.getName() && item.kind === "input" && item.key !== "settings"; })[0];
  if (!tab || event.range.getRow() <= 1) return;
  var lock = LockService.getScriptLock(); if (!lock.tryLock(5000)) return;
  try {
    assafOsBeginRequestReadCache_(workbook);
    assafOsAssertHeaders_(sheet, tab);
    assafOsReconcileRow_(workbook, tab, event.range.getRow(), "trigger");
    if (["clients", "works", "payments"].indexOf(tab.key) !== -1) assafOsRefreshReferenceValidationsSafely_(workbook);
  } finally { ASSAF_OS_REQUEST_READ_CACHE = null; lock.releaseLock(); }
}

function reconcileAssafOsWorkbook() {
  ASSAF_OS_REQUEST_READ_CACHE = null;
  var config = assafOsConfig_(), workbook = SpreadsheetApp.openById(config.workbook_id), result = { accepted: 0, issues: 0 };
  var lock = LockService.getScriptLock(); if (!lock.tryLock(5000)) throw assafOsError_("runtime_busy");
  try {
    return assafOsReconcileAll_(workbook);
  } finally { ASSAF_OS_REQUEST_READ_CACHE = null; lock.releaseLock(); }
}

function assafOsReconcileAll_(workbook) {
  assafOsBeginRequestReadCache_(workbook);
  var result = { accepted: 0, resolved: 0, issues: 0, header_issues: [] }, referenceChanged = false;
  var tabs = AssafOsSchema.tabs.filter(function (tab) { return tab.kind === "input" && tab.key !== "settings"; });
  tabs.forEach(function (tab) { try { var sheet = workbook.getSheetByName(tab.name), raw = assafOsReadSheetOnce_(sheet); assafOsAssertHeaders_(sheet, tab, raw.values[0], raw.last_column); } catch (error) { result.header_issues.push({ code: error.code || "header_drift", entity: tab.key, record_id: null }); } });
  if (result.header_issues.length) return result;
  tabs.forEach(function (tab) {
    var sheet = workbook.getSheetByName(tab.name), raw = assafOsReadSheetOnce_(sheet), last = Math.min(raw.values.length, ASSAF_OS_MAX_ROWS + 1);
    for (var row = 2; row <= last; row += 1) {
      var status = assafOsReconcileRow_(workbook, tab, row, "read", raw.values[row - 1], raw.formulas[row - 1] || []);
      if (status === "accepted") { result.accepted += 1; if (["clients", "works", "payments"].indexOf(tab.key) !== -1) referenceChanged = true; }
      else if (status === "resolved") result.resolved += 1;
      else if (status === "issue") result.issues += 1;
    }
  });
  if (assafOsCanProject_(workbook)) {
    var requests = assafOsProjectionRequests_(workbook, { entity: "tasks", record_id: null, after: {} });
    if (requests.length) {
      Sheets.Spreadsheets.batchUpdate({ requests: requests }, workbook.getId());
      assafOsInvalidateEntityCache_(workbook, "clients"); assafOsInvalidateEntityCache_(workbook, "works");
    }
  }
  if (referenceChanged || assafOsReferenceValidationsNeedRefresh_(workbook)) assafOsRefreshReferenceValidationsSafely_(workbook);
  return result;
}

function assafOsRefreshReferenceValidationsSafely_(workbook) {
  if (typeof refreshAssafOsReferenceValidations_ !== "function") return false;
  try { refreshAssafOsReferenceValidations_(workbook); assafOsMarkReferenceValidationsFresh_(workbook); return true; }
  catch (_) { try { PropertiesService.getScriptProperties().setProperty(ASSAF_OS_REFERENCE_DIRTY_PROPERTY, "true"); } catch (_) {} if (typeof console !== "undefined" && console.warn) console.warn("assaf_os_reference_validation_refresh_failed"); return false; }
}

function assafOsReferenceCatalogFingerprint_(workbook) {
  var payload = ["clients", "works", "payments"].map(function (entity) {
    var tab = AssafOsSchema.tab(entity), raw = assafOsReadSheetOnce_(workbook.getSheetByName(tab.name));
    var indexes = tab.fields.map(function (field, index) { return field.role === "calculated" ? -1 : index; }).filter(function (index) { return index !== -1; });
    return { entity: entity, rows: raw.values.slice(1).map(function (row, index) { return { row: row, raw_index: index + 1 }; }).filter(function (item) { return item.row.some(function (cell) { return cell !== ""; }); }).map(function (item) { return indexes.map(function (index) { return [assafOsNativeValue_(tab.fields[index], item.row[index]), (raw.formulas[item.raw_index] || [])[index] || ""]; }); }) };
  });
  return assafOsSha_(AssafOsRuntime.stableStringify(payload));
}
function assafOsReferenceValidationsNeedRefresh_(workbook) {
  var props = PropertiesService.getScriptProperties(), current = assafOsReferenceCatalogFingerprint_(workbook), stored = props.getProperty(ASSAF_OS_REFERENCE_FINGERPRINT_PROPERTY);
  if (props.getProperty(ASSAF_OS_REFERENCE_DIRTY_PROPERTY) === "true") return true;
  if (!stored) return true;
  return stored !== current;
}
function assafOsMarkReferenceValidationsFresh_(workbook) {
  var props = PropertiesService.getScriptProperties();
  props.setProperty(ASSAF_OS_REFERENCE_FINGERPRINT_PROPERTY, assafOsReferenceCatalogFingerprint_(workbook));
  props.deleteProperty(ASSAF_OS_REFERENCE_DIRTY_PROPERTY);
}

function assafOsReconcileRow_(workbook, tab, rowNumber, source, suppliedRow, suppliedFormulas) {
  var sheet = workbook.getSheetByName(tab.name), row = suppliedRow || sheet.getRange(rowNumber, 1, 1, tab.fields.length).getValues()[0];
  if (row.every(function (cell) { return cell === ""; })) return "empty";
  var entity = tab.key, idKey = assafOsIdKey_(entity), record = null, originalRecordId = null, input = {};
  try {
    var formulas = suppliedFormulas || sheet.getRange(rowNumber, 1, 1, tab.fields.length).getFormulas()[0];
    if (formulas.some(function (formula) { return formula !== ""; })) throw assafOsError_("formula_drift");
    record = assafOsRowToRecord_(tab, row, workbook);
    originalRecordId = record[idKey] || null;
    inputFieldsFor_(tab).forEach(function (field) { var index = tab.fields.indexOf(field), value = assafOsNativeValue_(field, row[index]); input[field.key] = value === "" ? null : assafOsResolveReferenceValue_(workbook, field, value); });
    input = AssafOsRuntime.nativeToInput(entity, input); AssafOsRuntime.canonicalInput(entity, input, true);
    if (!record[idKey]) record[idKey] = assafOsNewRecordId_(entity);
    assafOsValidateDependencies_(workbook, entity, record);
    if (entity === "payments") AssafOsRuntime.validatePaymentGraph(assafOsOtherRecords_(workbook, sheet, tab, rowNumber).filter(function (payment) { return !!payment.payment_id; }).concat([record]));
    if (entity === "expenses") AssafOsRuntime.dedupeExpense(record, assafOsOtherRecords_(workbook, sheet, tab, rowNumber));
  } catch (caught) { assafOsRecordIssue_(workbook, caught.assafOsCode || caught.code || "invalid_row", entity, originalRecordId, rowNumber); return "issue"; }
  var accepted = record[idKey] ? assafOsLatestAccepted_(workbook, entity, record[idKey]) : null;
  if (accepted && Number(record.revision) !== Number(accepted.revision)) { assafOsRecordIssue_(workbook, "system_column_drift", entity, record[idKey], rowNumber); return "issue"; }
  var inputFingerprint = assafOsSha_(AssafOsRuntime.stableStringify(input));
  if (accepted && inputFingerprint === accepted.input_fingerprint && assafOsRawOwnedFingerprint_(tab, row, workbook) !== accepted.raw_fingerprint) { assafOsRecordIssue_(workbook, "system_column_drift", entity, record[idKey], rowNumber); return "issue"; }
  if (accepted && inputFingerprint === accepted.input_fingerprint) return assafOsResolveIssue_(workbook, entity, rowNumber, record[idKey]) ? "resolved" : "unchanged";
  var id = record[idKey], revision = accepted ? accepted.revision + 1 : 1, now = new Date().toISOString();
  record[idKey] = id; record.revision = revision; record.write_source = "משתמש"; record.request_id = "EDIT_" + Utilities.getUuid().replace(/-/g, "");
  if (entity === "leads" && !record.person_id) record.person_id = assafOsNewRecordId_("persons");
  if (tab.fields.some(function (f) { return f.key === "created_at"; }) && !record.created_at) record.created_at = now;
  if (tab.fields.some(function (f) { return f.key === "updated_at"; })) record.updated_at = now;
  var next = assafOsRecordToRow_(tab, record, workbook), snapshot = { entity: entity, record_id: id, revision: revision, input: input };
  var requests = [];
  tab.fields.forEach(function (field, index) { if (field.role === "system" && AssafOsRuntime.stableStringify(row[index] === "" ? null : row[index]) !== AssafOsRuntime.stableStringify(next[index] === "" ? null : next[index])) requests.push(assafOsCellRequest_(sheet, rowNumber, index + 1, next[index])); });
  requests.push(assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.accepted.name), [entity, id, revision, inputFingerprint, assafOsRawOwnedFingerprint_(tab, next, workbook), JSON.stringify(snapshot), now, "משתמש", record.request_id]));
  var openIssue = assafOsLatestIssue_(workbook, entity, rowNumber);
  if (openIssue && openIssue.status === "open") requests.push(assafOsAppendRequest_(workbook.getSheetByName(ASSAF_OS_META.issues.name), ["ISS_" + Utilities.getUuid().replace(/-/g, ""), now, openIssue.code, entity, id, rowNumber, "resolved"]));
  Sheets.Spreadsheets.batchUpdate({ requests: requests }, workbook.getId());
  assafOsInvalidateEntityCache_(workbook, entity);
  assafOsInvalidateSheetCache_(workbook, workbook.getSheetByName(ASSAF_OS_META.accepted.name));
  assafOsInvalidateSheetCache_(workbook, workbook.getSheetByName(ASSAF_OS_META.issues.name));
  if (source !== "read" && assafOsCanProject_(workbook)) { var projection = assafOsProjectionRequests_(workbook, { entity: entity, record_id: id, after: record }); if (projection.length) { Sheets.Spreadsheets.batchUpdate({ requests: projection }, workbook.getId()); assafOsInvalidateEntityCache_(workbook, "clients"); assafOsInvalidateEntityCache_(workbook, "works"); } }
  return "accepted";
}

function assafOsCanProject_(workbook, proposal) {
  var required = ["clients", "works", "payments", "time", "expenses"], reads = {};
  if (assafOsOpenIssues_(workbook).some(function (issue) { return required.indexOf(issue.entity) !== -1; })) return false;
  for (var i = 0; i < required.length; i += 1) {
    reads[required[i]] = assafOsReadEntityTolerant_(workbook, required[i]);
    if (reads[required[i]].coverage !== "complete") return false;
  }
  try {
    if (proposal) {
      var candidateEntity = proposal.entity === "promote_lead" ? "clients" : proposal.entity;
      var candidate = proposal.entity === "promote_lead" ? proposal.after.client : proposal.after;
      if (required.indexOf(candidateEntity) !== -1 && candidate) {
        var idKey = assafOsIdKey_(candidateEntity), replaced = false;
        reads[candidateEntity].records = reads[candidateEntity].records.map(function (record) { if (record[idKey] === candidate[idKey]) { replaced = true; return candidate; } return record; });
        if (!replaced) reads[candidateEntity].records.push(candidate);
      }
    }
    required.forEach(function (entity) { reads[entity].records.forEach(function (record) {
      var input = {}, tab = AssafOsSchema.tab(entity);
      inputFieldsFor_(tab).forEach(function (field) { input[field.key] = record[field.key] == null ? null : record[field.key]; });
      AssafOsRuntime.canonicalInput(entity, input, true);
      assafOsValidateDependencies_(workbook, entity, record);
    }); });
    AssafOsRuntime.validatePaymentGraph(reads.payments.records);
    reads.expenses.records.forEach(function (record, index) { AssafOsRuntime.dedupeExpense(record, reads.expenses.records.filter(function (_, other) { return other !== index; })); });
  } catch (error) { return false; }
  return true;
}

function assafOsVerifyMetadata_(workbook) {
  Object.keys(ASSAF_OS_META).forEach(function (key) {
    var spec = ASSAF_OS_META[key], sheet = workbook.getSheetByName(spec.name);
    if (!sheet) throw assafOsError_("runtime_not_installed");
    var lastRow = sheet.getLastRow();
    if (lastRow > ASSAF_OS_MAX_ROWS + 1) throw assafOsError_("metadata_capacity_exceeded");
    var rows = sheet.getRange(1, 1, Math.max(lastRow, 1), spec.headers.length).getValues();
    var headers = rows[0] || [];
    if (spec.headers.some(function (value, index) { return headers[index] !== value; })) throw assafOsError_("metadata_header_drift");
    var cache = ASSAF_OS_REQUEST_READ_CACHE, cacheKey = String(sheet.getSheetId());
    if (cache && cache.workbook_id === workbook.getId()) cache.values[cacheKey] = rows;
  });
}

function assafOsReferenceCatalog_(workbook, target) {
  var tab = AssafOsSchema.tab(target), sheet = tab && workbook.getSheetByName(tab.name);
  if (!tab || !sheet) throw assafOsError_("missing_reference_sheet");
  var rows = assafOsReadSheetOnce_(sheet).values, idKey = assafOsIdKey_(target), idIndex = tab.fields.findIndex(function (field) { return field.key === idKey; });
  var revisionIndex = tab.fields.findIndex(function (field) { return field.key === "revision"; }), catalog = {}, clientNames = {};
  if (target !== "clients") assafOsReferenceCatalog_(workbook, "clients").forEach(function (item) { clientNames[item.id] = item.primary; });
  for (var i = 1; i < rows.length; i += 1) {
    var id = rows[i][idIndex] == null ? "" : String(rows[i][idIndex]);
    if (!id || (revisionIndex !== -1 && !(Number(rows[i][revisionIndex]) > 0))) continue;
    if (catalog[id]) throw assafOsError_("ambiguous_reference_id");
    var primary = "";
    if (target === "clients") primary = rows[i][tab.fields.findIndex(function (field) { return field.key === "name"; })];
    else if (target === "works") primary = rows[i][tab.fields.findIndex(function (field) { return field.key === "title"; })];
    else if (target === "payments") {
      var kind = rows[i][tab.fields.findIndex(function (field) { return field.key === "kind"; })];
      if (kind !== "התקבל") continue;
      var date = rows[i][tab.fields.findIndex(function (field) { return field.key === "date"; })];
      primary = "תקבול " + assafOsReferenceText_(date);
    }
    var personIndex = tab.fields.findIndex(function (field) { return field.key === "person_id"; }), personId = personIndex === -1 ? "" : assafOsEmbeddedReferenceId_(rows[i][personIndex]);
    var secondary = target === "clients" ? "" : (clientNames[personId] || personId);
    catalog[id] = { id: id, primary: assafOsReferenceText_(primary) || "ללא שם", secondary: assafOsReferenceText_(secondary) };
  }
  return Object.keys(catalog).sort().map(function (id) { return catalog[id]; });
}

function assafOsReferenceText_(value) { return String(value == null ? "" : value).replace(/[\r\n\t]+/g, " ").replace(/[⟦⟧]/g, "").trim().slice(0, 100); }
function assafOsEmbeddedReferenceId_(value) { var text = String(value == null ? "" : value), match = text.match(/ ⟦([^⟦⟧]+)⟧$/); return match ? match[1] : text; }
function assafOsReferenceTarget_(field) { return field.ref ? String(field.ref).split(".")[0] : null; }
function assafOsReferenceLabel_(item) { return item.primary + (item.secondary ? " · " + item.secondary : "") + " ⟦" + item.id + "⟧"; }
function assafOsReferenceChoices_(workbook, field) { return assafOsReferenceCatalog_(workbook, assafOsReferenceTarget_(field)).map(assafOsReferenceLabel_); }
function assafOsResolveReferenceValue_(workbook, field, value) {
  if (!field.ref || value === null || value === "") return value;
  var id = assafOsEmbeddedReferenceId_(value), matches = assafOsReferenceCatalog_(workbook, assafOsReferenceTarget_(field)).filter(function (item) { return item.id === id; });
  if (matches.length !== 1) throw assafOsError_(matches.length > 1 ? "ambiguous_reference_id" : "unknown_reference");
  return matches[0].id;
}
function assafOsDisplayReference_(workbook, field, value) {
  var id = assafOsResolveReferenceValue_(workbook, field, value), item = assafOsReferenceCatalog_(workbook, assafOsReferenceTarget_(field)).filter(function (candidate) { return candidate.id === id; })[0];
  return assafOsReferenceLabel_(item);
}

function assafOsRowToRecord_(tab, row, workbook) {
  var record = {};
  tab.fields.forEach(function (field, i) {
    var value = row[i];
    if (value === "") value = null;
    if (value instanceof Date) value = field.type === "date" ? Utilities.formatDate(value, ASSAF_OS_BUSINESS_TIMEZONE, "yyyy-MM-dd") : value.toISOString();
    if (value !== null && field.ref && workbook) value = assafOsResolveReferenceValue_(workbook, field, value);
    if (field.type === "money_ils" && value !== null) value = AssafOsRuntime.majorToMinor(Number(value));
    record[field.key] = value;
  });
  return record;
}
function assafOsNativeValue_(field, value) { if (!(value instanceof Date)) return value; if (field.type === "date") return Utilities.formatDate(value, ASSAF_OS_BUSINESS_TIMEZONE, "yyyy-MM-dd"); if (field.type === "datetime") return value.toISOString(); return value; }
function assafOsAssertHeaders_(sheet, tab, supplied, suppliedLastColumn) { if (!sheet) throw assafOsError_("missing_entity_sheet"); var values = supplied || sheet.getRange(1, 1, 1, tab.fields.length).getValues()[0], lastColumn = suppliedLastColumn == null ? sheet.getLastColumn() : suppliedLastColumn; if (!values || values.length !== tab.fields.length || tab.fields.some(function (field, index) { return values[index] !== field.label; }) || lastColumn !== tab.fields.length) throw assafOsError_("header_drift"); }
function assafOsRecordToRow_(tab, record, workbook) { return tab.fields.map(function (field) { var value = record[field.key]; if (field.type === "money_ils" && value != null) value = AssafOsRuntime.minorToMajor(value); if (value != null && field.ref && workbook) value = assafOsDisplayReference_(workbook, field, value); return value == null ? "" : value; }); }
function inputFieldsFor_(tab) { return tab.fields.filter(function (field) { return field.role === "input"; }); }
function assafOsIdKey_(entity) { return entity === "clients" ? "person_id" : entity.replace(/s$/, "") + "_id"; }
function assafOsNewRecordId_(entity) { return (entity === "clients" ? "PER" : entity.slice(0, 3).toUpperCase()) + "_" + Utilities.getUuid().replace(/-/g, ""); }
function assafOsLatestAccepted_(workbook, entity, recordId) { var rows = assafOsReadValuesOnce_(workbook.getSheetByName(ASSAF_OS_META.accepted.name)), result = null; for (var i = 1; i < rows.length; i += 1) if (rows[i][0] === entity && rows[i][1] === recordId && (!result || Number(rows[i][2]) > result.revision)) result = { revision: Number(rows[i][2]), input_fingerprint: String(rows[i][3]), raw_fingerprint: String(rows[i][4]), row: rows[i] }; return result; }
function assafOsRawOwnedFingerprint_(tab, row, workbook) { return assafOsSha_(AssafOsRuntime.stableStringify(tab.fields.map(function (field, index) { var value = field.role === "calculated" ? null : assafOsNativeValue_(field, row[index]); return value !== null && value !== "" && field.ref && workbook ? assafOsResolveReferenceValue_(workbook, field, value) : value; }))); }
function assafOsRecordIssue_(workbook, code, entity, recordId, row) { var sheet = workbook.getSheetByName(ASSAF_OS_META.issues.name), current = assafOsLatestIssue_(workbook, entity, row); if (current && current.status === "open" && current.code === code) return; var now = new Date().toISOString(); Sheets.Spreadsheets.batchUpdate({ requests: [assafOsAppendRequest_(sheet, ["ISS_" + Utilities.getUuid().replace(/-/g, ""), now, code, entity, recordId || "", row, "open"])] }, workbook.getId()); assafOsInvalidateSheetCache_(workbook, sheet); }
function assafOsLatestIssue_(workbook, entity, row) { var rows = assafOsReadValuesOnce_(workbook.getSheetByName(ASSAF_OS_META.issues.name)), result = null; for (var i = 1; i < rows.length; i += 1) if (rows[i][3] === entity && Number(rows[i][5]) === Number(row)) result = { code: String(rows[i][2]), record_id: rows[i][4] ? String(rows[i][4]) : null, status: String(rows[i][6]) }; return result; }
function assafOsResolveIssue_(workbook, entity, row, recordId) { var sheet = workbook.getSheetByName(ASSAF_OS_META.issues.name), issue = assafOsLatestIssue_(workbook, entity, row); if (!issue || issue.status !== "open") return false; var now = new Date().toISOString(); Sheets.Spreadsheets.batchUpdate({ requests: [assafOsAppendRequest_(sheet, ["ISS_" + Utilities.getUuid().replace(/-/g, ""), now, issue.code, entity, recordId || issue.record_id || "", row, "resolved"])] }, workbook.getId()); assafOsInvalidateSheetCache_(workbook, sheet); return true; }
function assafOsOpenIssues_(workbook) { var rows = assafOsReadValuesOnce_(workbook.getSheetByName(ASSAF_OS_META.issues.name)), latest = {}; for (var i = 1; i < rows.length; i += 1) latest[String(rows[i][3]) + "|" + String(rows[i][5])] = rows[i]; return Object.keys(latest).map(function (key) { return latest[key]; }).filter(function (row) { return row[6] === "open"; }).map(function (row) { return { code: String(row[2]), entity: String(row[3]), record_id: row[4] ? String(row[4]) : null }; }); }
function assafOsFindBy_(sheet, column, value, withRow) { var rows = assafOsReadValuesOnce_(sheet); for (var i = 1; i < rows.length; i += 1) if (String(rows[i][column]) === String(value)) return withRow ? { values: rows[i], row: i + 1 } : rows[i]; return null; }
function assafOsOtherRecords_(workbook, sheet, tab, excludedRow) { var rows = assafOsReadSheetOnce_(sheet).values, records = []; for (var i = 1; i < rows.length; i += 1) { if (i + 1 === excludedRow || rows[i].every(function (cell) { return cell === ""; })) continue; try { records.push(assafOsRowToRecord_(tab, rows[i], workbook)); } catch (_) {} } return records; }
function assafOsAppendRequest_(sheet, values) { assafOsAssertAppendCapacity_(sheet, 1); return { appendCells: { sheetId: sheet.getSheetId(), rows: [{ values: values.map(assafOsExtendedValue_) }], fields: "userEnteredValue" } }; }
function assafOsAssertAppendCapacity_(sheet, count) { if (sheet.getLastRow() + count > sheet.getMaxRows()) throw assafOsError_("metadata_capacity_exceeded"); }
function assafOsRowRequest_(sheet, row, values) { return { updateCells: { range: { sheetId: sheet.getSheetId(), startRowIndex: row - 1, endRowIndex: row, startColumnIndex: 0, endColumnIndex: values.length }, rows: [{ values: values.map(assafOsExtendedValue_) }], fields: "userEnteredValue" } }; }
function assafOsRangeRequest_(sheet, row, startColumn, values) { return { updateCells: { range: { sheetId: sheet.getSheetId(), startRowIndex: row - 1, endRowIndex: row, startColumnIndex: startColumn - 1, endColumnIndex: startColumn - 1 + values.length }, rows: [{ values: values.map(assafOsExtendedValue_) }], fields: "userEnteredValue" } }; }
function assafOsCellRequest_(sheet, row, column, value) { return { updateCells: { range: { sheetId: sheet.getSheetId(), startRowIndex: row - 1, endRowIndex: row, startColumnIndex: column - 1, endColumnIndex: column }, rows: [{ values: [assafOsExtendedValue_(value)] }], fields: "userEnteredValue" } }; }
function assafOsExtendedValue_(value) { if (typeof value === "number") return { userEnteredValue: { numberValue: value } }; if (typeof value === "boolean") return { userEnteredValue: { boolValue: value } }; return { userEnteredValue: { stringValue: value == null ? "" : String(value) } }; }
function assafOsBytes_(value) { return Utilities.newBlob(value).getBytes().length; }
function assafOsHmac_(value, secret) { return assafOsHex_(Utilities.computeHmacSha256Signature(value, secret, Utilities.Charset.UTF_8)); }
function assafOsSha_(value) { return assafOsHex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, value, Utilities.Charset.UTF_8)); }
function assafOsIdempotencyHash_(request) { var stable = { api_version: request.api_version, operation: request.operation, actor_source: request.actor_source, owner_id: request.owner_id, workspace_id: request.workspace_id, workbook_id: request.workbook_id, request_id: request.request_id, data: request.data }; if (request.operation === "approve" || request.operation === "reject") delete stable.actor_source; return assafOsSha_(AssafOsRuntime.stableStringify(stable)); }
function assafOsHex_(bytes) { return bytes.map(function (byte) { var value = byte < 0 ? byte + 256 : byte; return ("0" + value.toString(16)).slice(-2); }).join(""); }
function assafOsConstantTime_(left, right) { if (left.length !== right.length) return false; var diff = 0; for (var i = 0; i < left.length; i += 1) diff |= left.charCodeAt(i) ^ right.charCodeAt(i); return diff === 0; }
function assafOsPlain_(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
function assafOsError_(code) { var error = new Error(code); error.assafOsCode = code; return error; }
function assafOsSafeIdentity_(request, config) { request = assafOsPlain_(request) ? request : {}; return { api_version: AssafOsRuntime && AssafOsRuntime.API_VERSION || "assaf-os.v1", operation: typeof request.operation === "string" ? request.operation : "invalid", actor_source: ["OS", "MIA", "TELEGRAM_APPROVAL"].indexOf(request.actor_source) !== -1 ? request.actor_source : "OS", owner_id: typeof request.owner_id === "string" ? request.owner_id : (config && config.owner_id || "0"), workspace_id: typeof request.workspace_id === "string" ? request.workspace_id : (config && config.workspace_id || "unavailable"), workbook_id: typeof request.workbook_id === "string" ? request.workbook_id : (config && config.workbook_id || "unavailable"), request_id: typeof request.request_id === "string" ? request.request_id : "invalid-request", timestamp: Number.isSafeInteger(request.timestamp) ? request.timestamp : 0 } ; }
function assafOsSignedResponse_(request, ok, data, config, tolerate) { var inner = JSON.stringify({ api_version: request.api_version, operation: request.operation, actor_source: request.actor_source, owner_id: request.owner_id, workspace_id: request.workspace_id, workbook_id: request.workbook_id, request_id: request.request_id, timestamp: request.timestamp, mode: config && config.mode || "UNKNOWN", ok: ok, data: data }); if (assafOsBytes_(inner) > ASSAF_OS_MAX_RESPONSE_BYTES) { ok = false; inner = JSON.stringify({ api_version: request.api_version, operation: request.operation, actor_source: request.actor_source, owner_id: request.owner_id, workspace_id: request.workspace_id, workbook_id: request.workbook_id, request_id: request.request_id, timestamp: request.timestamp, mode: config && config.mode || "UNKNOWN", ok: false, data: { error: { code: "response_too_large" } } }); } var signature = config && config.secret ? assafOsHmac_(inner, config.secret) : ""; if (!signature && !tolerate) throw assafOsError_("unavailable"); return ContentService.createTextOutput(JSON.stringify({ payload: inner, signature: signature })).setMimeType(ContentService.MimeType.JSON); }
