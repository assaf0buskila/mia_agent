/* global Sheets, openVerifiedWorkbook_, resolveSheet_, assertResolvedHeaders_, ledgerError_, sha256Hex_, plainObject_ */
"use strict";

// This is a TEST-only sink. The signed parent operation may call saveCrmProjection_ later;
// this file deliberately does not add an HTTP route, menu, trigger, or scheduler.
const CRM_PROJECTION_API_VERSION = "owner-os.crm-projection.v1";
const CRM_PROJECTION_MAX_CONTACTS = 100;
const CRM_PROJECTION_MAX_AGE_SECONDS = 300;
const CRM_PROJECTION_KNOWN = "מקור קנוני TEST";
const CRM_PROJECTION_UNKNOWN = "מצב CRM לא ידוע";
const CRM_PROJECTION_PREFIX = "CRM_PROJECTION_V1";
const CRM_PROJECTION_MAX_SHEET_ROWS = CRM_PROJECTION_MAX_CONTACTS + 2;
const CRM_PROJECTION_INITIAL_PLACEHOLDER = ["unavailable", "טרם חובר למקור CRM המאומת"];

function crmProjectionExact_(value, keys, code) {
  if (!plainObject_(value) || Object.keys(value).sort().join(",") !== keys.slice().sort().join(",")) throw ledgerError_(code);
  return value;
}

function crmProjectionLiteral_(value, maximum, nullable, code) {
  if (nullable && value === null) return null;
  if (typeof value !== "string" || !value || value !== value.trim() || value.length > maximum || /[\u0000-\u001f\u007f-\u009f]/.test(value)) throw ledgerError_(code);
  return value;
}

function crmProjectionStable_(value) {
  if (Array.isArray(value)) return "[" + value.map(crmProjectionStable_).join(",") + "]";
  if (plainObject_(value)) return "{" + Object.keys(value).sort().map(function (key) { return JSON.stringify(key) + ":" + crmProjectionStable_(value[key]); }).join(",") + "}";
  return JSON.stringify(value);
}

function crmProjectionCanonicalObserved_(value, code) {
  if (typeof value !== "string") throw ledgerError_(code);
  const observed = Date.parse(value);
  if (!Number.isFinite(observed) || new Date(observed).toISOString().replace(".000Z", "Z") !== value.replace(".000Z", "Z")) throw ledgerError_(code);
  return observed;
}

function crmProjectionAcceptedState_(data, body) {
  return {
    workspace_id: data.workspace_id,
    projection_status: data.projection_status,
    projection_revision: data.projection_revision,
    snapshot_id: data.source.snapshot_id,
    snapshot_hash: data.snapshot_hash,
    expected_snapshot_hash: data.expected_snapshot_hash,
    expected_projection_revision: data.expected_projection_revision,
    observed_at: data.source.observed_at,
    coverage: data.source.coverage,
    coverage_scope: data.source.coverage_scope,
    rows: body,
  };
}

function crmProjectionAcceptedDigest_(data, body) {
  return sha256Hex_(crmProjectionStable_(crmProjectionAcceptedState_(data, body)));
}

function crmProjectionSource_(raw, status) {
  const source = crmProjectionExact_(raw, ["api_version", "source", "snapshot_id", "observed_at", "coverage", "coverage_scope"], "invalid_crm_projection_source");
  if (source.api_version !== "owner-os.v1" || source.source !== "mia_postgresql" || source.coverage_scope !== "stored_pg_contacts") throw ledgerError_("invalid_crm_projection_source");
  if (status === "known") {
    crmProjectionLiteral_(source.snapshot_id, 128, false, "invalid_crm_projection_source");
    if (!/^[A-Za-z0-9_-]+$/.test(source.snapshot_id) || source.coverage !== "complete") throw ledgerError_("invalid_crm_projection_source");
  } else if (source.snapshot_id !== null || source.coverage !== "unknown") throw ledgerError_("invalid_crm_projection_source");
  const observed = crmProjectionCanonicalObserved_(source.observed_at, "invalid_crm_projection_observed_at");
  const age = (Date.now() - observed) / 1000;
  if (age < -5 || age > CRM_PROJECTION_MAX_AGE_SECONDS) throw ledgerError_("stale_crm_projection_source");
  return source;
}

function crmProjectionContact_(raw) {
  const item = crmProjectionExact_(raw, ["contact_id", "revision", "name", "source_label", "next_action_state"], "invalid_crm_projection_contact");
  crmProjectionLiteral_(item.contact_id, 128, false, "invalid_crm_projection_contact");
  if (!/^[A-Za-z0-9_-]+$/.test(item.contact_id) || !Number.isSafeInteger(item.revision) || item.revision < 1 || item.revision > 1000000) throw ledgerError_("invalid_crm_projection_contact");
  crmProjectionLiteral_(item.name, 160, true, "invalid_crm_projection_contact");
  crmProjectionLiteral_(item.source_label, 160, true, "invalid_crm_projection_contact");
  if (["recorded", "not_collected"].indexOf(item.next_action_state) === -1) throw ledgerError_("invalid_crm_projection_contact");
  return item;
}

function validateCrmProjection_(raw, config) {
  const data = crmProjectionExact_(raw, [
    "api_version", "mode", "synthetic", "workspace_id", "projection_status", "reason_code",
    "expected_snapshot_hash", "expected_projection_revision", "projection_revision", "source", "contacts", "snapshot_hash",
  ], "invalid_crm_projection_shape");
  if (data.api_version !== CRM_PROJECTION_API_VERSION || data.mode !== "TEST" || data.synthetic !== true) throw ledgerError_("crm_projection_test_scope_required");
  if (data.workspace_id !== config.workspaceId) throw ledgerError_("invalid_workspace");
  if (["known", "unknown"].indexOf(data.projection_status) === -1) throw ledgerError_("invalid_crm_projection_status");
  if (data.projection_status === "known" ? data.reason_code !== null : ["source_unavailable", "source_coverage_incomplete", "source_snapshot_stale"].indexOf(data.reason_code) === -1) throw ledgerError_("invalid_crm_projection_reason");
  if (data.expected_snapshot_hash !== null && (typeof data.expected_snapshot_hash !== "string" || !/^[0-9a-f]{64}$/.test(data.expected_snapshot_hash))) throw ledgerError_("invalid_expected_snapshot_hash");
  if (!Number.isSafeInteger(data.expected_projection_revision) || data.expected_projection_revision < 0 || data.expected_projection_revision > 1000000 || data.projection_revision !== data.expected_projection_revision + 1) throw ledgerError_("invalid_projection_revision");
  if ((data.expected_projection_revision === 0) !== (data.expected_snapshot_hash === null)) throw ledgerError_("inconsistent_expected_projection");
  if (typeof data.snapshot_hash !== "string" || !/^[0-9a-f]{64}$/.test(data.snapshot_hash)) throw ledgerError_("invalid_snapshot_hash");
  const source = crmProjectionSource_(data.source, data.projection_status);
  if (!Array.isArray(data.contacts) || data.contacts.length > CRM_PROJECTION_MAX_CONTACTS || (data.projection_status === "unknown" && data.contacts.length !== 0)) throw ledgerError_("crm_projection_contact_limit");
  const contacts = data.contacts.map(crmProjectionContact_);
  const ids = {};
  contacts.forEach(function (item) { if (ids[item.contact_id]) throw ledgerError_("duplicate_crm_projection_contact"); ids[item.contact_id] = true; });
  const identity = { source: source, contacts: contacts, projection_status: data.projection_status, reason_code: data.reason_code };
  if (sha256Hex_(crmProjectionStable_(identity)) !== data.snapshot_hash) throw ledgerError_("crm_projection_hash_mismatch");
  return data;
}

function crmProjectionMetadata_(data, bodyHash) {
  return [
    CRM_PROJECTION_PREFIX,
    "workspace=" + data.workspace_id,
    "status=" + data.projection_status,
    "revision=" + data.projection_revision,
    "snapshot=" + (data.source.snapshot_id || "unknown"),
    "hash=" + data.snapshot_hash,
    "body=" + bodyHash,
    "predecessor=" + (data.expected_snapshot_hash || "none"),
    "predecessor_revision=" + data.expected_projection_revision,
    "observed=" + data.source.observed_at,
    "coverage=" + data.source.coverage,
    "scope=" + data.source.coverage_scope,
  ].join(";");
}

function parseCrmProjectionMetadata_(rows, config) {
  const nonempty = rows.slice(1).some(function (row) { return row[0] !== "" || row[1] !== ""; });
  if (!nonempty) return null;
  const status = rows[1] && rows[1][0];
  const message = rows[1] && rows[1][1];
  if ([CRM_PROJECTION_KNOWN, CRM_PROJECTION_UNKNOWN].indexOf(status) === -1 || typeof message !== "string" || message.indexOf(CRM_PROJECTION_PREFIX + ";") !== 0) throw ledgerError_("crm_projection_state_unrecognized");
  const values = {};
  message.split(";").slice(1).forEach(function (part) { const at = part.indexOf("="); const key = part.slice(0, at); if (at < 1 || Object.prototype.hasOwnProperty.call(values, key)) throw ledgerError_("crm_projection_state_unrecognized"); values[key] = part.slice(at + 1); });
  if (Object.keys(values).sort().join(",") !== "body,coverage,hash,observed,predecessor,predecessor_revision,revision,scope,snapshot,status,workspace" || values.workspace !== config.workspaceId || !/^[0-9a-f]{64}$/.test(values.hash) || !/^[0-9a-f]{64}$|^none$/.test(values.predecessor) || !/^[0-9a-f]{64}$/.test(values.body) || !/^[1-9][0-9]{0,6}$/.test(values.revision) || !/^(0|[1-9][0-9]{0,6})$/.test(values.predecessor_revision) || ["known", "unknown"].indexOf(values.status) === -1 || (values.status === "known") !== (status === CRM_PROJECTION_KNOWN)) throw ledgerError_("crm_projection_state_unrecognized");
  const revision = Number(values.revision);
  const predecessorRevision = Number(values.predecessor_revision);
  // The status disambiguates the display sentinel from the legitimate canonical
  // known snapshot id "unknown".
  const snapshotId = values.status === "unknown" ? null : values.snapshot;
  const predecessor = values.predecessor === "none" ? null : values.predecessor;
  if (revision > 1000001 || predecessorRevision > 1000000 || predecessorRevision + 1 !== revision || (predecessorRevision === 0) !== (predecessor === null) || values.scope !== "stored_pg_contacts") throw ledgerError_("crm_projection_state_unrecognized");
  if (values.status === "known") {
    if (snapshotId === null || !/^[A-Za-z0-9_-]{1,128}$/.test(snapshotId) || values.coverage !== "complete") throw ledgerError_("crm_projection_state_unrecognized");
  } else if (values.snapshot !== "unknown" || snapshotId !== null || values.coverage !== "unknown") throw ledgerError_("crm_projection_state_unrecognized");
  const observed = crmProjectionCanonicalObserved_(values.observed, "crm_projection_state_unrecognized");
  const currentBody = rows.slice(2);
  const accepted = {
    workspace_id: values.workspace,
    projection_status: values.status,
    projection_revision: revision,
    source: { snapshot_id: snapshotId, observed_at: values.observed, coverage: values.coverage, coverage_scope: values.scope },
    snapshot_hash: values.hash,
    expected_snapshot_hash: predecessor,
    expected_projection_revision: predecessorRevision,
  };
  if (crmProjectionAcceptedDigest_(accepted, currentBody) !== values.body) throw ledgerError_("crm_projection_body_tampered");
  return { hash: values.hash, revision: revision, status: values.status, observed: observed, predecessor: predecessor, predecessorRevision: predecessorRevision };
}

function crmProjectionInitialPlaceholder_(rows) {
  return rows.length === 2 && rows[1].length === 2 && rows[1][0] === CRM_PROJECTION_INITIAL_PLACEHOLDER[0] && rows[1][1] === CRM_PROJECTION_INITIAL_PLACEHOLDER[1];
}

function crmProjectionBodyRows_(data) {
  const rows = [];
  if (data.projection_status === "unknown") {
    const messages = {
      source_unavailable: "מקור ה-CRM הקנוני אינו זמין כרגע.",
      source_coverage_incomplete: "כיסוי מקור ה-CRM אינו מלא ולכן לא פורסמו רשומות.",
      source_snapshot_stale: "תמונת מצב ה-CRM אינה עדכנית ולכן לא פורסמו רשומות.",
    };
    rows.push(["לא ידוע", messages[data.reason_code]]);
    return rows;
  }
  data.contacts.forEach(function (item) {
    const name = item.name || "שם לא נאסף";
    const status = item.next_action_state === "recorded" ? "יש צעד הבא מתועד" : "צעד הבא לא נאסף";
    rows.push([status, "שם: " + name + " | מזהה: " + item.contact_id + " | גרסה: " + item.revision]);
  });
  return rows;
}

function crmProjectionRows_(data) {
  const body = crmProjectionBodyRows_(data);
  const bodyHash = crmProjectionAcceptedDigest_(data, body);
  return [[data.projection_status === "known" ? CRM_PROJECTION_KNOWN : CRM_PROJECTION_UNKNOWN, crmProjectionMetadata_(data, bodyHash)]].concat(body);
}

function crmProjectionUpdateRequest_(sheetId, rows, rowCount) {
  if (rowCount < 1 || rowCount > CRM_PROJECTION_MAX_CONTACTS + 1) throw ledgerError_("crm_projection_view_capacity_exceeded");
  const padded = rows.slice();
  while (padded.length < rowCount) padded.push(["", ""]);
  return {
    updateCells: {
      range: { sheetId: sheetId, startRowIndex: 1, endRowIndex: 1 + rowCount, startColumnIndex: 0, endColumnIndex: 2 },
      rows: padded.map(function (row) { return { values: row.map(function (value) { return { userEnteredValue: { stringValue: String(value) } }; }) }; }),
      fields: "userEnteredValue",
    },
  };
}

function saveCrmProjection_(ownerId, rawData, config) {
  if (typeof ownerId !== "string" || config.owners.indexOf(ownerId) === -1) throw ledgerError_("unauthorized_owner");
  const data = validateCrmProjection_(rawData, config);
  const spreadsheet = openVerifiedWorkbook_(config, false);
  const resolved = resolveSheet_(spreadsheet, "CRM View");
  const lastRow = resolved.sheet.getLastRow();
  const lastColumn = resolved.sheet.getLastColumn();
  if (lastRow < 1 || lastRow > CRM_PROJECTION_MAX_SHEET_ROWS || lastColumn !== 2) throw ledgerError_("crm_projection_view_capacity_exceeded");
  const currentRows = resolved.sheet.getRange(1, 1, lastRow, 2).getValues();
  assertResolvedHeaders_(currentRows[0], resolved, "CRM View");
  const initialPlaceholder = crmProjectionInitialPlaceholder_(currentRows);
  const current = initialPlaceholder ? null : parseCrmProjectionMetadata_(currentRows, config);
  if (initialPlaceholder && (data.expected_snapshot_hash !== null || data.expected_projection_revision !== 0)) throw ledgerError_("crm_projection_revision_mismatch");
  if (current) {
    const exactReplay = data.snapshot_hash === current.hash && data.projection_revision === current.revision && data.expected_snapshot_hash === current.predecessor && data.expected_projection_revision === current.predecessorRevision;
    if (exactReplay) return { status: "replayed", snapshot_hash: current.hash, projection_revision: current.revision };
    if (data.expected_snapshot_hash !== current.hash || data.expected_projection_revision !== current.revision) throw ledgerError_("crm_projection_revision_mismatch");
    if (data.projection_status === "unknown" && current.status === "known") throw ledgerError_("unknown_cannot_replace_known_projection");
    if (Date.parse(data.source.observed_at) <= current.observed) throw ledgerError_("stale_crm_projection_update");
  } else if (data.expected_snapshot_hash !== null || data.expected_projection_revision !== 0) throw ledgerError_("crm_projection_revision_mismatch");
  const nextRows = crmProjectionRows_(data);
  const rowCount = Math.max(nextRows.length, Math.max(lastRow - 1, 1));
  Sheets.Spreadsheets.batchUpdate({ requests: [crmProjectionUpdateRequest_(resolved.sheet.getSheetId(), nextRows, rowCount)] }, config.workbookId);
  return { status: "committed", snapshot_hash: data.snapshot_hash, projection_revision: data.projection_revision };
}
