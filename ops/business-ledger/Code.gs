/* global BusinessLedgerCore, Sheets */
"use strict";

const LEDGER_TABS = {
  Deals: ["id", "revision", "title", "amount_minor", "currency", "payment_plan", "start_date", "status", "project_id", "notes", "updated_at"],
  Charges: ["id", "revision", "deal_id", "status", "amount_minor", "currency", "due_date", "updated_at"],
  Payments: ["id", "revision", "charge_id", "status", "amount_minor", "currency", "received_at", "updated_at"],
  Expenses: ["id", "revision", "project_id", "amount_minor", "currency", "occurred_on", "notes", "updated_at"],
  Projects: ["id", "revision", "title", "status", "deadline", "notes", "updated_at"],
  Settings: ["key", "value"],
  Audit: ["event_type", "entity_type", "entity_id", "revision", "request_id", "owner_id", "data_hash", "snapshot_json", "receipt_json", "committed_at"],
  Conflicts: ["id", "detected_at", "entity_type", "entity_id", "reason_code", "observed_json"],
  "CRM View": ["status", "message"],
};
const LEDGER_HEBREW = {
  Deals: { tab: "עסקאות", headers: ["מזהה", "גרסה", "כותרת", "סכום באגורות", "מטבע", "תוכנית תשלום", "תאריך התחלה", "סטטוס", "מזהה פרויקט", "הערות", "עודכן בתאריך"] },
  Charges: { tab: "חיובים צפויים", headers: ["מזהה", "גרסה", "מזהה עסקה", "סטטוס", "סכום באגורות", "מטבע", "תאריך חיוב", "עודכן בתאריך"] },
  Payments: { tab: "תשלומים שהתקבלו", headers: ["מזהה", "גרסה", "מזהה חיוב", "סטטוס", "סכום באגורות", "מטבע", "התקבל בתאריך", "עודכן בתאריך"] },
  Expenses: { tab: "הוצאות", headers: ["מזהה", "גרסה", "מזהה פרויקט", "סכום באגורות", "מטבע", "תאריך הוצאה", "הערות", "עודכן בתאריך"] },
  Projects: { tab: "פרויקטים", headers: ["מזהה", "גרסה", "כותרת", "סטטוס", "תאריך יעד", "הערות", "עודכן בתאריך"] },
  Settings: { tab: "הגדרות", headers: ["מפתח", "ערך"] },
  Audit: { tab: "יומן שינויים", headers: ["סוג אירוע", "סוג ישות", "מזהה ישות", "גרסה", "מזהה בקשה", "מזהה בעלים", "גיבוב נתונים", "תמונת מצב JSON", "קבלה JSON", "נשמר בתאריך"] },
  Conflicts: { tab: "קונפליקטים", headers: ["מזהה", "זוהה בתאריך", "סוג ישות", "מזהה ישות", "קוד סיבה", "נתונים שנצפו"] },
  "CRM View": { tab: "תצוגת CRM", headers: ["סטטוס", "הודעה"] },
};
const MAX_DEAL_ROWS = 500;
const MAX_AUDIT_ROWS = 5000;
const MAX_SIGNED_RESPONSE_BYTES = 262144;
const MAX_SETTINGS_ROWS = 100;
const TEST_NAME_RE = /(^|[^A-Z0-9])TEST([^A-Z0-9]|$)/i;
const BUSINESS_WORKBOOK_TITLE = "AssafWeb OS Business";
const DEAL_FORM_PENDING_KEY = "BUSINESS_LEDGER_PENDING_V1";

function doPost(event) {
  let transport = null;
  let config = null;
  try {
    config = ledgerConfig_();
    const raw = event && event.postData && event.postData.contents;
    if (typeof raw !== "string" || Utilities.newBlob(raw).getBytes().length > 16384) throw ledgerError_("invalid_transport");
    transport = JSON.parse(raw);
    if (!plainObject_(transport) || Object.keys(transport).sort().join(",") !== "payload,signature" || typeof transport.payload !== "string" || !/^[0-9a-f]{64}$/i.test(transport.signature)) throw ledgerError_("invalid_transport");
    const expected = hmacHex_(transport.payload, config.secret);
    if (!constantTimeEqual_(transport.signature.toLowerCase(), expected)) throw ledgerError_("invalid_signature");
    const request = JSON.parse(transport.payload);
    validateTransportPayload_(request, config);
    const result = runOperation_(request, config);
    return signedResponse_(request, result.status, result.result, config);
  } catch (error) {
    const request = safeRequestIdentity_(transport, config);
    const knownCode = error && (error.ledgerCode || error.code);
    const knownUnavailable = knownCode === "unavailable" || knownCode === "coverage_limit_exceeded";
    const status = knownCode === "deal_row_tampered" || knownCode === "finance_row_tampered" ? "conflict" : (knownUnavailable || !knownCode && !(error instanceof SyntaxError) ? "unavailable" : "invalid");
    const reason = status === "unavailable" && !knownUnavailable ? "provider_result_unknown" : (knownCode || "invalid_request");
    return signedResponse_(request, status, { reason_code: reason }, config, true);
  }
}

function ledgerConfig_() {
  const props = PropertiesService.getScriptProperties();
  const config = {
    workbookId: props.getProperty("TEST_WORKBOOK_ID"),
    workspaceId: props.getProperty("TEST_WORKSPACE_ID"),
    owners: (props.getProperty("OWNER_NUMERIC_IDS") || "").split(",").map(function (v) { return v.trim(); }).filter(Boolean),
    secret: props.getProperty("LEDGER_HMAC_SECRET"),
    operatorMap: props.getProperty("OPERATOR_OWNER_MAP"),
  };
  if (!config.workbookId || typeof config.workspaceId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(config.workspaceId)) throw ledgerError_("unavailable");
  if (typeof config.secret !== "string" || config.secret.length < 32) throw ledgerError_("unavailable");
  if (!config.owners.length || config.owners.some(function (v) { return !/^[1-9][0-9]{0,19}$/.test(v); }) || new Set(config.owners).size !== config.owners.length) throw ledgerError_("unavailable");
  return config;
}

function validateTransportPayload_(request, config) {
  if (!plainObject_(request) || Object.keys(request).sort().join(",") !== "data,operation,owner_id,request_id,timestamp,workspace_id") throw ledgerError_("invalid_payload_shape");
  BusinessLedgerCore.uuid(request.request_id, "invalid_request_id");
  if (!Number.isSafeInteger(request.timestamp) || Math.abs(Math.floor(Date.now() / 1000) - request.timestamp) > 60) throw ledgerError_("stale_request");
  if (request.workspace_id !== config.workspaceId) throw ledgerError_("invalid_workspace");
  if (typeof request.owner_id !== "string" || config.owners.indexOf(request.owner_id) === -1) throw ledgerError_("unauthorized_owner");
  if (["snapshot", "save_deal", "receipt", "finance_snapshot", "save_schedule", "record_payment", "record_expense", "save_project", "save_crm_projection", "crm_projection_receipt"].indexOf(request.operation) === -1 || !plainObject_(request.data)) throw ledgerError_("invalid_operation");
  if (request.operation === "snapshot" && Object.keys(request.data).length !== 0) throw ledgerError_("invalid_snapshot_shape");
  if (request.operation === "save_deal") BusinessLedgerCore.validateSaveData(request.data);
  if (request.operation === "receipt") BusinessLedgerCore.validateReceiptData(request.data);
  if (["finance_snapshot", "save_schedule", "record_payment", "record_expense", "save_project"].indexOf(request.operation) !== -1) validateFinanceData_(request.operation, request.data);
  if (request.operation === "save_crm_projection") validateCrmProjection_(request.data, config);
  if (request.operation === "crm_projection_receipt") validateCrmProjectionReceiptData_(request.data);
}

function runOperation_(request, config) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return { status: "unavailable", result: { reason_code: "ledger_busy" } };
  try {
    if (request.operation === "snapshot") return snapshotOperation_(config);
    if (request.operation === "receipt") return receiptOperation_(request, config);
    if (request.operation === "save_crm_projection") return {status: "ok", result: saveCrmProjection_(request.owner_id, request.data, config)};
    if (request.operation === "crm_projection_receipt") return crmProjectionReceiptOperation_(request, config);
    if (request.operation !== "save_deal") return financeOperation_(request, config);
    return saveDealOperation_(request.owner_id, request.data, config);
  } finally {
    lock.releaseLock();
  }
}

function validateCrmProjectionReceiptData_(data) {
  if (!plainObject_(data) || Object.keys(data).sort().join(",") !== "projection_revision,snapshot_hash" || typeof data.snapshot_hash !== "string" || !/^[0-9a-f]{64}$/.test(data.snapshot_hash) || !Number.isSafeInteger(data.projection_revision) || data.projection_revision < 1 || data.projection_revision > 1000001) throw ledgerError_("invalid_crm_projection_receipt");
}

function crmProjectionReceiptOperation_(request, config) {
  const spreadsheet = openVerifiedWorkbook_(config, false);
  const resolved = resolveSheet_(spreadsheet, "CRM View");
  const lastRow = resolved.sheet.getLastRow(), lastColumn = resolved.sheet.getLastColumn();
  if (lastRow < 1 || lastRow > CRM_PROJECTION_MAX_SHEET_ROWS || lastColumn !== 2) throw ledgerError_("crm_projection_view_capacity_exceeded");
  const rows = resolved.sheet.getRange(1, 1, lastRow, 2).getValues();
  assertResolvedHeaders_(rows[0], resolved, "CRM View");
  const current = crmProjectionInitialPlaceholder_(rows) ? null : parseCrmProjectionMetadata_(rows, config);
  if (!current || current.hash !== request.data.snapshot_hash || current.revision !== request.data.projection_revision) return {status: "not_found", result: {reason_code: "receipt_not_found"}};
  return {status: "ok", result: {status: "committed", snapshot_hash: current.hash, projection_revision: current.revision}};
}

function snapshotOperation_(config) {
  const state = readState_(config);
  const tamper = firstTamper_(state);
  if (tamper) return { status: "conflict", result: tamper };
  const result = BusinessLedgerCore.snapshot(state.deals, new Date().toISOString());
  if (snapshotEnvelopeBytes_(result, config) > MAX_SIGNED_RESPONSE_BYTES) return { status: "unavailable", result: { reason_code: "snapshot_capacity_exceeded" } };
  return { status: "ok", result: result };
}

function receiptOperation_(request, config) {
  const key = BusinessLedgerCore.validateReceiptData(request.data).request_id;
  const state = readState_(config);
  const stored = state.receipts[key];
  if (!stored || stored.owner_id !== request.owner_id) return { status: "not_found", result: { reason_code: "receipt_not_found" } };
  return { status: "ok", result: stored.receipt };
}

function saveDealOperation_(ownerId, rawData, config) {
  const data = BusinessLedgerCore.validateSaveData(rawData);
  const state = readState_(config);
  const tamper = firstTamper_(state);
  if (tamper) {
    recordConflict_(state, tamper, config);
    return { status: "conflict", result: tamper };
  }
  const dataHash = sha256Hex_(BusinessLedgerCore.stableStringify(rawData));
  const now = new Date().toISOString();
  const decision = BusinessLedgerCore.decideSave({
    data: data,
    owner_id: ownerId,
    data_hash: dataHash,
    currentDeal: state.byId[data.deal.id] || null,
    acceptedSnapshot: state.accepted[data.deal.id] || null,
    existingReceipt: state.receipts[data.request_id] || null,
    committed_at: now,
  });
  if (decision.kind === "replay") return { status: "ok", result: decision.receipt };
  if (decision.kind === "invalid") return { status: "invalid", result: { reason_code: decision.reason_code } };
  if (decision.kind === "conflict") {
    const conflict = { reason_code: decision.reason_code, id: data.deal.id };
    recordConflict_(state, conflict, config);
    return { status: "conflict", result: conflict };
  }
  const financial = guardScheduledDealChange_(state, data.deal, config);
  const isCreate = !state.byId[data.deal.id];
  if (isCreate && state.dealCount >= MAX_DEAL_ROWS) return capacityConflict_(state, data.deal.id, "deal_capacity_exceeded", config);
  if (state.auditCount >= MAX_AUDIT_ROWS) return capacityConflict_(state, data.deal.id, "audit_capacity_exceeded", config);
  const prospective = state.deals.filter(function (deal) { return deal.id !== decision.deal.id; }).concat([decision.deal]);
  const prospectiveSnapshot = BusinessLedgerCore.snapshot(prospective, now);
  if (snapshotEnvelopeBytes_(prospectiveSnapshot, config) > MAX_SIGNED_RESPONSE_BYTES) return capacityConflict_(state, data.deal.id, "snapshot_capacity_exceeded", config);
  const prospectiveFinance = financeSnapshotFromState_(Object.assign({}, financial, {deals: prospective}), now);
  if (snapshotEnvelopeBytes_(prospectiveFinance, config) > MAX_SIGNED_RESPONSE_BYTES) return capacityConflict_(state, data.deal.id, "snapshot_capacity_exceeded", config);

  const receipt = {
    status: "committed", request_id: data.request_id, id: decision.deal.id,
    revision: decision.deal.revision, receipt_id: Utilities.getUuid().toLowerCase(),
    committed_at: now, source: "google_sheets", mode: "TEST",
  };
  const dealValues = dealToRow_(decision.deal, now);
  const auditValues = ["accepted", "deal", decision.deal.id, decision.deal.revision, data.request_id, ownerId, dataHash, BusinessLedgerCore.stableStringify(decision.deal), JSON.stringify(receipt), now];
  const dealRow = state.rowById[decision.deal.id] || state.nextDealRow;
  const requests = [updateRowRequest_(state.sheetIds.Deals, dealRow, dealValues), updateRowRequest_(state.sheetIds.Audit, state.nextAuditRow, auditValues)];
  Sheets.Spreadsheets.batchUpdate({ requests: requests }, config.workbookId);
  return { status: "ok", result: receipt };
}

function readState_(config) {
  const spreadsheet = openVerifiedWorkbook_(config, false);
  const dealsResolved = resolveSheet_(spreadsheet, "Deals");
  const auditResolved = resolveSheet_(spreadsheet, "Audit");
  const conflictsResolved = resolveSheet_(spreadsheet, "Conflicts");
  const dealsSheet = dealsResolved.sheet;
  const auditSheet = auditResolved.sheet;
  const dealRows = dealsSheet.getDataRange().getValues();
  const auditRows = auditSheet.getDataRange().getValues();
  if (dealRows.length > MAX_DEAL_ROWS + 1 || auditRows.length > MAX_AUDIT_ROWS + 1) throw ledgerError_("coverage_limit_exceeded");
  assertResolvedHeaders_(dealRows[0], dealsResolved, "Deals");
  assertResolvedHeaders_(auditRows[0], auditResolved, "Audit");
  const deals = [], byId = {}, rowById = {};
  for (let i = 1; i < dealRows.length; i += 1) {
    if (dealRows[i].every(function (cell) { return cell === ""; })) continue;
    let deal;
    try { deal = rowToDeal_(dealRows[i]); } catch (_) { throw ledgerError_("deal_row_tampered"); }
    deals.push(deal);
    if (byId[deal.id]) throw ledgerError_("duplicate_deal_id");
    byId[deal.id] = deal;
    rowById[deal.id] = i + 1;
  }
  const accepted = {}, receipts = {}, financeAccepted = { Charges: {}, Payments: {}, Expenses: {}, Projects: {} };
  for (let j = 1; j < auditRows.length; j += 1) {
    const row = auditRows[j];
    if (row[0] === "accepted" && row[1] === "finance") { mergeFinanceAuditRow_(row, accepted, receipts, financeAccepted); continue; }
    if (row[0] !== "accepted" || row[1] !== "deal") continue;
    let snap, receipt;
    try { snap = JSON.parse(row[7]); receipt = JSON.parse(row[8]); } catch (_) { throw ledgerError_("audit_corrupt"); }
    snap = BusinessLedgerCore.canonicalPersistedDeal(snap);
    if (!accepted[snap.id] || snap.revision > accepted[snap.id].revision) accepted[snap.id] = snap;
    if (receipts[String(row[4])]) throw ledgerError_("duplicate_receipt_key");
    receipts[String(row[4])] = { owner_id: String(row[5]), data_hash: String(row[6]), receipt: receipt };
  }
  return {
    deals: deals, dealCount: deals.length, auditCount: Math.max(auditRows.length - 1, 0), byId: byId, rowById: rowById, accepted: accepted, receipts: receipts, financeAccepted: financeAccepted,
    nextDealRow: Math.max(dealsSheet.getLastRow() + 1, 2), nextAuditRow: Math.max(auditSheet.getLastRow() + 1, 2),
    nextConflictRow: Math.max(conflictsResolved.sheet.getLastRow() + 1, 2),
    sheetIds: { Deals: dealsSheet.getSheetId(), Audit: auditSheet.getSheetId(), Conflicts: conflictsResolved.sheet.getSheetId() },
  };
}

function openVerifiedWorkbook_(config, allowMissingWorkspace) {
  const spreadsheet = SpreadsheetApp.openById(config.workbookId);
  const workbookName = spreadsheet.getName();
  if (workbookName !== BUSINESS_WORKBOOK_TITLE && !TEST_NAME_RE.test(workbookName)) throw ledgerError_("workbook_not_test");
  const settingsResolved = resolveSheet_(spreadsheet, "Settings");
  const settings = settingsResolved.sheet;
  const rows = settings.getDataRange().getValues();
  assertResolvedHeaders_(rows[0], settingsResolved, "Settings");
  const values = settingsMap_(rows);
  if (values.mode !== "TEST") throw ledgerError_("settings_mode_not_test");
  if (!allowMissingWorkspace && values.workspace_id !== config.workspaceId) throw ledgerError_("settings_workspace_mismatch");
  if (allowMissingWorkspace && values.workspace_id && values.workspace_id !== config.workspaceId) throw ledgerError_("settings_workspace_mismatch");
  return spreadsheet;
}

function settingsMap_(rows) {
  if (!rows.length || rows.length > MAX_SETTINGS_ROWS + 1) throw ledgerError_("invalid_settings");
  const values = {};
  for (let i = 1; i < rows.length; i += 1) {
    const key = String(rows[i][0]);
    if (!key && rows[i][1] === "") continue;
    if (!key || Object.prototype.hasOwnProperty.call(values, key)) throw ledgerError_("invalid_settings");
    values[key] = String(rows[i][1]);
  }
  if (!Object.prototype.hasOwnProperty.call(values, "mode")) throw ledgerError_("settings_mode_missing");
  return values;
}

function recordConflict_(state, conflict, config) {
  const values = [Utilities.getUuid().toLowerCase(), new Date().toISOString(), "deal", conflict.id || "unknown", conflict.reason_code, "redacted"];
  Sheets.Spreadsheets.batchUpdate({ requests: [updateRowRequest_(state.sheetIds.Conflicts, state.nextConflictRow, values)] }, config.workbookId);
}

function capacityConflict_(state, id, reasonCode, config) {
  const conflict = { reason_code: reasonCode, id: id };
  recordConflict_(state, conflict, config);
  return { status: "conflict", result: conflict };
}

function firstTamper_(state) {
  const ids = Object.keys(state.byId).concat(Object.keys(state.accepted).filter(function (id) { return !state.byId[id]; }));
  for (let i = 0; i < ids.length; i += 1) {
    const id = ids[i], current = state.byId[id], accepted = state.accepted[id];
    if (!current || !accepted || BusinessLedgerCore.stableStringify(current) !== BusinessLedgerCore.stableStringify(accepted)) {
      return { reason_code: "deal_row_tampered", id: id };
    }
  }
  return null;
}

function rowToDeal_(row) {
  return BusinessLedgerCore.canonicalPersistedDeal({
    id: String(row[0]), revision: Number(row[1]), title: String(row[2]), amount_minor: Number(row[3]),
    currency: String(row[4]), payment_plan: String(row[5]), start_date: sheetDate_(row[6]),
    status: String(row[7]), project_id: row[8] === "" ? null : String(row[8]), notes: String(row[9]), updated_at: String(row[10]),
  });
}

function dealToRow_(deal, now) {
  return [deal.id, deal.revision, deal.title, deal.amount_minor, deal.currency, deal.payment_plan, deal.start_date, deal.status, deal.project_id || "", deal.notes, now];
}

function sheetDate_(value) {
  if (value instanceof Date) return Utilities.formatDate(value, "Etc/UTC", "yyyy-MM-dd");
  return String(value);
}

function updateRowRequest_(sheetId, oneBasedRow, values) {
  return {
    updateCells: {
      range: { sheetId: sheetId, startRowIndex: oneBasedRow - 1, endRowIndex: oneBasedRow, startColumnIndex: 0, endColumnIndex: values.length },
      rows: [{ values: values.map(cellData_) }], fields: "userEnteredValue",
    },
  };
}

function cellData_(value) {
  if (typeof value === "number") return { userEnteredValue: { numberValue: value } };
  if (typeof value === "boolean") return { userEnteredValue: { boolValue: value } };
  return { userEnteredValue: { stringValue: String(value) } };
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu("AssafWeb OS Business")
    .addItem("יצירה או עדכון עסקה", "showDealDialog")
    .addItem("חיובים, תקבולים, הוצאות ופרויקטים", "showFinanceDialog")
    .addSeparator().addItem("הגדרת סביבת בדיקה", "installBusinessLedgerTest")
    .addToUi();
}

function showDealDialog() {
  operatorOwner_();
  const html = HtmlService.createHtmlOutput(dealDialogHtml_()).setWidth(560).setHeight(690);
  SpreadsheetApp.getUi().showModalDialog(html, "AssafWeb OS Business");
}

function dealDialogHtml_() {
  return `<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><style>
  *{box-sizing:border-box}body{font-family:Rubik,Arial,sans-serif;line-height:1.65;margin:0;padding:18px;color:#172033;background:#fff}h2{margin:0 0 4px}p{margin:0 0 14px;color:#526078}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.full{grid-column:1/-1}label{display:block;font-weight:600;margin-block-end:4px}input,select{width:100%;min-height:44px;border:1px solid #b8c2d1;border-radius:9px;padding:9px 11px;font:inherit;background:#fff}button{min-height:46px;border:0;border-radius:10px;padding:10px 18px;font:inherit;font-weight:700;cursor:pointer;background:#1255cc;color:white}.secondary{background:#e8eef8;color:#17345f}.actions{display:flex;gap:10px;justify-content:flex-start;margin-block-start:18px}.status{min-height:48px;margin-block-start:14px;padding:10px;border-radius:8px;background:#f4f7fb;white-space:pre-wrap}.hint{font-size:13px}.ltr{direction:ltr;text-align:right}@media(max-width:520px){.grid{grid-template-columns:1fr}}
  </style></head><body><h2>יצירה או עדכון עסקה</h2><p><strong>סביבת בדיקה / נתונים סינתטיים</strong></p><p>הטופס שומר עסקה בלבד. הוא לא יוצר חיוב או תשלום.</p>
  <div class="grid"><div class="full"><label for="dealSelect">עסקה</label><select id="dealSelect"><option value="">עסקה חדשה</option></select></div>
  <div class="full"><label for="title">כותרת העסקה</label><input id="title" dir="auto" maxlength="160" required></div>
  <div><label for="amount">סכום העסקה (₪)</label><input id="amount" class="ltr" inputmode="decimal" placeholder="1250.00" required></div>
  <div><label for="start">תאריך התחלה</label><input id="start" type="date" required></div>
  <div><label for="plan">אופן תשלום</label><select id="plan"><option value="full">תשלום מלא</option><option value="installments">תשלומים</option><option value="monthly">חודשי</option></select></div>
  <div><label for="status">סטטוס</label><select id="status"><option value="active">פעילה</option><option value="cancelled">מבוטלת</option></select></div>
  <div class="full"><label for="notes">הערות</label><input id="notes" dir="auto" maxlength="1000"></div></div>
  <p class="hint">מזהה העסקה, הגרסה ומזהה הבקשה מנוהלים אוטומטית. אין בטופס סוד או מזהה בעלים.</p>
  <div class="actions"><button id="save" onclick="saveDeal()">בדיקה לפני שמירה</button><button class="secondary" onclick="google.script.host.close()">סגירה</button></div>
  <section id="review" hidden aria-label="אישור העסקה"><h3>לאשר שמירת העסקה בדיוק כך?</h3><div id="reviewSummary" class="status"></div><p class="hint">זהו סכום העסקה הכולל. האישור תקף לשתי דקות; לפני שמירה ניתן לחזור ולתקן.</p><div class="actions"><button id="confirmSave" onclick="confirmSaveDeal()">אישור ושמירה</button><button id="cancelReview" class="secondary" onclick="cancelDealReview()">חזרה לעריכה</button></div></section><div id="out" class="status" role="status"></div>
  <script>
  const byId={};let locked=false,pendingReview=null;const ids=["dealSelect","title","amount","start","plan","status","notes"];
  function uuid(){if(crypto.randomUUID)return crypto.randomUUID();const b=new Uint8Array(16);crypto.getRandomValues(b);b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;const h=[...b].map(x=>x.toString(16).padStart(2,"0")).join("");return h.slice(0,8)+"-"+h.slice(8,12)+"-"+h.slice(12,16)+"-"+h.slice(16,20)+"-"+h.slice(20)}
  function setLocked(value){locked=value;ids.forEach(id=>document.getElementById(id).disabled=value);document.getElementById("save").disabled=value}
  document.getElementById("dealSelect").addEventListener("change",function(){if(locked)return;const d=byId[this.value];document.getElementById("title").value=d?d.title:"";document.getElementById("amount").value=d?(d.amount_minor/100).toFixed(2):"";document.getElementById("start").value=d?d.start_date:"";document.getElementById("plan").value=d?d.payment_plan:"full";document.getElementById("status").value=d?d.status:"active";document.getElementById("notes").value=d?d.notes:""});
  function loadDeals(rows){const s=document.getElementById("dealSelect");rows.forEach(d=>{byId[d.id]=d;const o=document.createElement("option");o.value=d.id;o.textContent=d.title+" · גרסה "+d.revision;s.appendChild(o)})}
  function receiptText(r){return ["העסקה נשמרה בהצלחה.","מזהה עסקה: "+r.id,"גרסה: "+r.revision,"מזהה קבלה: "+r.receipt_id,"נשמר בתאריך: "+r.committed_at].join("\\n")}
  function acknowledge(r){setLocked(true);document.getElementById("out").textContent=receiptText(r);google.script.run.withSuccessHandler(a=>{if(a.status==="ok"){document.getElementById("out").textContent=receiptText(a.result)+"\\n\\nהחלון ייסגר.";setTimeout(()=>google.script.host.close(),2200)}else{document.getElementById("out").textContent=receiptText(r)+"\\n\\nהקבלה נשמרה, אך האישור המקומי לא הושלם. פתחו שוב את הטופס לבדיקה."}}).withFailureHandler(()=>{document.getElementById("out").textContent=receiptText(r)+"\\n\\nהקבלה נשמרה, אך האישור המקומי לא הושלם. פתחו שוב את הטופס לבדיקה."}).acknowledgeDealFormReceipt(r.request_id,r.receipt_id)}
  function recoveryView(pending,receipt){setLocked(true);if(receipt){document.getElementById("out").textContent=receiptText(receipt);const b=document.createElement("button");b.textContent="אישור קבלה וסגירה";b.onclick=()=>acknowledge(receipt);document.getElementById("out").appendChild(document.createElement("br"));document.getElementById("out").appendChild(b);return}document.getElementById("out").textContent="קיימת שמירה שתוצאתה עדיין לא אושרה. לא ניתן לשנות שדות או לבצע שמירה נוספת.\\nמזהה בקשה: "+pending.request_id;const check=document.createElement("button");check.textContent="בדיקת קבלה";check.onclick=()=>checkReceipt(pending);document.getElementById("out").appendChild(document.createElement("br"));document.getElementById("out").appendChild(check)}
  function checkReceipt(pending){google.script.run.withSuccessHandler(result=>{if(result.status==="ok"){acknowledge(result.result);return}if(result.status==="not_found"){document.getElementById("out").textContent="עדיין לא נמצאה קבלה עבור מזהה הבקשה. מצב השחזור נשאר נעול כדי למנוע עסקה כפולה. ניתן לבדוק שוב מאוחר יותר.";const again=document.createElement("button");again.textContent="בדיקה חוזרת";again.onclick=()=>checkReceipt(pending);document.getElementById("out").appendChild(document.createElement("br"));document.getElementById("out").appendChild(again);return}document.getElementById("out").textContent="בדיקת הקבלה אינה זמינה כרגע. נסו שוב מאוחר יותר."}).withFailureHandler(()=>{document.getElementById("out").textContent="בדיקת הקבלה אינה זמינה כרגע. נסו שוב מאוחר יותר."}).checkPendingDealReceipt()}
  function boot(result){if(result.mode==="edit"){loadDeals(result.deals);setLocked(false);return}if(result.mode==="finance_recovery"){setLocked(true);document.getElementById("out").textContent="קיימת פעולה פיננסית שממתינה לבדיקת קבלה. סגרו חלון זה ובחרו בתפריט חיובים, תקבולים, הוצאות ופרויקטים כדי להשלים את השחזור.";return}recoveryView(result.pending,result.receipt)}
  function failed(){setLocked(true);document.getElementById("out").textContent="לא ניתן לטעון את העסקאות כרגע."}setLocked(true);google.script.run.withSuccessHandler(boot).withFailureHandler(failed).getDealFormBootstrap();
  function saveDeal(){if(locked)return;const selected=document.getElementById("dealSelect").value,d=byId[selected];const f={request_id:uuid(),deal_id:d?d.id:uuid(),expected_revision:d?d.revision:0,title:document.getElementById("title").value,amount_ils:document.getElementById("amount").value,payment_plan:document.getElementById("plan").value,start_date:document.getElementById("start").value,status:document.getElementById("status").value,notes:document.getElementById("notes").value};
  const plans={full:"תשלום מלא",installments:"תשלומים",monthly:"חודשי"},statuses={active:"פעילה",cancelled:"מבוטלת"};const summary=["כותרת: "+f.title,"סכום העסקה הכולל: ₪"+f.amount_ils,"תאריך התחלה: "+f.start_date,"אופן תשלום: "+plans[f.payment_plan],"סטטוס: "+statuses[f.status],"הערות: "+(f.notes||"ללא"),"מזהה עסקה: "+f.deal_id,"גרסה צפויה: "+f.expected_revision].join("\\n");pendingReview={form:Object.freeze(f),expiresAt:Date.now()+120000};setLocked(true);document.getElementById("reviewSummary").textContent=summary;document.getElementById("review").hidden=false;document.getElementById("out").textContent="טרם בוצעה שמירה. יש לאשר את הפרטים שמוצגים כאן."}
  function cancelDealReview(){if(!pendingReview)return;pendingReview=null;document.getElementById("review").hidden=true;document.getElementById("reviewSummary").textContent="";setLocked(false);document.getElementById("out").textContent="אפשר לערוך את הפרטים ולבדוק שוב לפני שמירה."}
  function confirmSaveDeal(){if(!pendingReview)return;if(Date.now()>pendingReview.expiresAt){cancelDealReview();document.getElementById("out").textContent="תוקף האישור פג. בדקו שוב את הפרטים לפני שמירה.";return}const f=pendingReview.form;pendingReview=null;document.getElementById("review").hidden=true;setLocked(true);document.getElementById("out").textContent="השמירה נשלחה. אין לשלוח שוב עד לבדיקת הקבלה.";google.script.run.withSuccessHandler(result=>{if(result.status==="ok"){acknowledge(result.result);return}if(result.status==="unavailable"||result.status==="recovery_required"){document.getElementById("out").textContent="תוצאת השמירה אינה ידועה. סגרו ופתחו מחדש את הטופס כדי לבדוק קבלה.\\nמזהה בקשה: "+f.request_id;return}document.getElementById("out").textContent="השמירה נדחתה: "+result.status+" / "+result.result.reason_code;setTimeout(()=>google.script.host.close(),2200)}).withFailureHandler(()=>{document.getElementById("out").textContent="תוצאת השמירה אינה ידועה. סגרו ופתחו מחדש את הטופס כדי לבדוק קבלה.\\nמזהה בקשה: "+f.request_id}).controlledSaveDealForm(f)}
  </script></body></html>`;
}

function getDealFormBootstrap() {
  const config = ledgerConfig_();
  const owner = operatorOwner_();
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw ledgerError_("ledger_busy");
  try {
    const financePending = readFinancePending_(config, owner);
    if (financePending) return { mode: "finance_recovery" };
    const state = readState_(config);
    const tamper = firstTamper_(state);
    if (tamper) throw ledgerError_(tamper.reason_code);
    const pending = readPendingDealForm_(config, owner);
    if (!pending) return { mode: "edit", deals: state.deals };
    const stored = state.receipts[pending.request_id];
    if (stored) {
      assertPendingReceiptBinding_(pending, stored, owner);
      return { mode: "committed", pending: pendingClientView_(pending), receipt: stored.receipt };
    }
    return { mode: "recovery", pending: pendingClientView_(pending) };
  } finally { lock.releaseLock(); }
}

function controlledSaveDealForm(form) {
  const config = ledgerConfig_();
  const owner = operatorOwner_();
  const data = formToSaveData_(form);
  const dataHash = sha256Hex_(BusinessLedgerCore.stableStringify(data));
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return { status: "unavailable", result: { reason_code: "ledger_busy" } };
  try {
    const financePending = readFinancePending_(config, owner);
    if (financePending) return { status: "recovery_required", result: { reason_code: "finance_form_recovery_required", request_id: financePending.request_id, id: financePending.receipt_id } };
    const existing = readPendingDealForm_(config, owner);
    if (existing) return { status: "recovery_required", result: { reason_code: "pending_receipt_check_required", request_id: existing.request_id, id: existing.deal_id } };
    writePendingDealForm_(config, owner, data, dataHash);
    const result = saveDealOperation_(owner, data, config);
    if (result.status !== "ok" && result.status !== "unavailable") clearPendingDealForm_();
    return result;
  } finally { lock.releaseLock(); }
}

function checkPendingDealReceipt() {
  const config = ledgerConfig_();
  const owner = operatorOwner_();
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return { status: "unavailable", result: { reason_code: "ledger_busy" } };
  try {
    const pending = readPendingDealForm_(config, owner);
    if (!pending) return { status: "not_found", result: { reason_code: "pending_marker_not_found" } };
    const state = readState_(config);
    const stored = state.receipts[pending.request_id];
    if (!stored) return { status: "not_found", result: { reason_code: "receipt_not_found", request_id: pending.request_id } };
    assertPendingReceiptBinding_(pending, stored, owner);
    return { status: "ok", result: stored.receipt };
  } finally { lock.releaseLock(); }
}

function acknowledgeDealFormReceipt(requestId, receiptId) {
  const config = ledgerConfig_();
  const owner = operatorOwner_();
  BusinessLedgerCore.uuid(requestId, "invalid_request_id");
  BusinessLedgerCore.uuid(receiptId, "invalid_receipt_id");
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return { status: "unavailable", result: { reason_code: "ledger_busy" } };
  try {
    const pending = readPendingDealForm_(config, owner);
    if (!pending || pending.request_id !== requestId) return { status: "invalid", result: { reason_code: "pending_marker_mismatch" } };
    const state = readState_(config);
    const stored = state.receipts[requestId];
    if (!stored) return { status: "not_found", result: { reason_code: "receipt_not_found" } };
    assertPendingReceiptBinding_(pending, stored, owner);
    if (stored.receipt.receipt_id !== receiptId) return { status: "not_found", result: { reason_code: "receipt_not_found" } };
    clearPendingDealForm_();
    return { status: "ok", result: stored.receipt };
  } finally { lock.releaseLock(); }
}

function readPendingDealForm_(config, owner) {
  const raw = PropertiesService.getUserProperties().getProperty(DEAL_FORM_PENDING_KEY);
  if (!raw) return null;
  let pending;
  try { pending = JSON.parse(raw); } catch (_) { throw ledgerError_("pending_marker_corrupt"); }
  if (!plainObject_(pending) || Object.keys(pending).sort().join(",") !== "created_at,data_hash,deal_id,expected_revision,owner_id,request_id,workspace_id") throw ledgerError_("pending_marker_corrupt");
  if (pending.workspace_id !== config.workspaceId || pending.owner_id !== owner) throw ledgerError_("pending_binding_mismatch");
  BusinessLedgerCore.uuid(pending.request_id, "pending_marker_corrupt");
  BusinessLedgerCore.uuid(pending.deal_id, "pending_marker_corrupt");
  if (!/^[0-9a-f]{64}$/.test(pending.data_hash) || typeof pending.created_at !== "string" || !Number.isSafeInteger(pending.expected_revision) || pending.expected_revision < 0 || pending.expected_revision > BusinessLedgerCore.MAX_REVISION) throw ledgerError_("pending_marker_corrupt");
  return pending;
}

function writePendingDealForm_(config, owner, data, dataHash) {
  const pending = { workspace_id: config.workspaceId, owner_id: owner, request_id: data.request_id, deal_id: data.deal.id, expected_revision: data.expected_revision, data_hash: dataHash, created_at: new Date().toISOString() };
  const serialized = JSON.stringify(pending);
  const properties = PropertiesService.getUserProperties();
  properties.setProperty(DEAL_FORM_PENDING_KEY, serialized);
  if (properties.getProperty(DEAL_FORM_PENDING_KEY) !== serialized) throw ledgerError_("pending_marker_persistence_failed");
}

function pendingClientView_(pending) {
  return { request_id: pending.request_id, deal_id: pending.deal_id, expected_revision: pending.expected_revision, created_at: pending.created_at };
}

function clearPendingDealForm_() {
  PropertiesService.getUserProperties().deleteProperty(DEAL_FORM_PENDING_KEY);
}

function assertPendingReceiptBinding_(pending, stored, owner) {
  const receipt = stored.receipt;
  const expectedKeys = "committed_at,id,mode,receipt_id,request_id,revision,source,status";
  let validTimestamp = false;
  try { validTimestamp = receipt && typeof receipt.committed_at === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(receipt.committed_at) && new Date(receipt.committed_at).toISOString() === receipt.committed_at; } catch (_) { validTimestamp = false; }
  if (
    stored.owner_id !== owner || stored.data_hash !== pending.data_hash || !plainObject_(receipt) ||
    Object.keys(receipt).sort().join(",") !== expectedKeys || receipt.status !== "committed" ||
    receipt.request_id !== pending.request_id || receipt.id !== pending.deal_id ||
    receipt.revision !== pending.expected_revision + 1 || receipt.source !== "google_sheets" || receipt.mode !== "TEST" ||
    !validTimestamp
  ) throw ledgerError_("pending_receipt_mismatch");
  try { BusinessLedgerCore.uuid(receipt.receipt_id, "pending_receipt_mismatch"); } catch (_) { throw ledgerError_("pending_receipt_mismatch"); }
}

function formToSaveData_(form) {
  if (!plainObject_(form) || Object.keys(form).sort().join(",") !== "amount_ils,deal_id,expected_revision,notes,payment_plan,request_id,start_date,status,title") throw ledgerError_("invalid_form_shape");
  if (typeof form.amount_ils !== "string" || !/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/.test(form.amount_ils)) throw ledgerError_("invalid_amount_ils");
  const amountMinor = Math.round(Number(form.amount_ils) * 100);
  const data = {
    request_id: form.request_id,
    expected_revision: form.expected_revision,
    deal: { id: form.deal_id, title: form.title, amount_minor: amountMinor, currency: "ILS", payment_plan: form.payment_plan, start_date: form.start_date, status: form.status, project_id: null, notes: form.notes },
  };
  return BusinessLedgerCore.validateSaveData(data);
}

function operatorOwner_() {
  const config = ledgerConfig_();
  const email = Session.getActiveUser().getEmail();
  if (!email || !config.operatorMap) throw ledgerError_("operator_identity_unavailable");
  let map;
  try { map = JSON.parse(config.operatorMap); } catch (_) { throw ledgerError_("operator_mapping_invalid"); }
  const owner = map[email.toLowerCase()];
  if (typeof owner !== "string" || config.owners.indexOf(owner) === -1) throw ledgerError_("operator_not_allowlisted");
  return owner;
}

function installBusinessLedgerTest() {
  const config = ledgerConfig_();
  operatorOwner_();
  const ui = SpreadsheetApp.getUi();
  if (ui.alert("AssafWeb OS Business", "להגדיר את סביבת הבדיקה והנתונים הסינתטיים? ההגדרות העסקיות הקיימות יישמרו.", ui.ButtonSet.YES_NO) !== ui.Button.YES) return;
  const spreadsheet = openVerifiedWorkbook_(config, true);
  const verifiedSheets = {};
  Object.keys(LEDGER_TABS).forEach(function (name) {
    const resolved = resolveSheet_(spreadsheet, name);
    assertResolvedHeaders_(resolved.sheet.getRange(1, 1, 1, LEDGER_TABS[name].length).getValues()[0], resolved, name);
    verifiedSheets[name] = resolved;
  });
  upsertSettings_(verifiedSheets.Settings.sheet, config);
  applyHebrewPresentation_(verifiedSheets, config);
  Object.keys(verifiedSheets).forEach(function (name) {
    const sheet = verifiedSheets[name].sheet;
    if (sheet.getProtections(SpreadsheetApp.ProtectionType.SHEET).length === 0) {
      const protection = sheet.protect().setDescription("AssafWeb OS Business - סביבת בדיקה / נתונים סינתטיים");
      protection.removeEditors(protection.getEditors());
      if (protection.canDomainEdit()) protection.setDomainEdit(false);
    }
  });
  ui.alert("AssafWeb OS Business הוגדר בעברית כסביבת בדיקה עם נתונים סינתטיים. ליצירה או עדכון עסקה השתמשו בתפריט AssafWeb OS Business.");
}

function applyHebrewPresentation_(verifiedSheets, config) {
  const requests = [];
  Object.keys(verifiedSheets).forEach(function (logicalName) {
    const resolved = verifiedSheets[logicalName];
    const sheetId = resolved.sheet.getSheetId();
    const presentation = LEDGER_HEBREW[logicalName];
    const properties = { sheetId: sheetId, title: presentation.tab, rightToLeft: true, gridProperties: { frozenRowCount: 1 } };
    requests.push({ updateSheetProperties: { properties: properties, fields: "title,rightToLeft,gridProperties.frozenRowCount" } });
    requests.push(updateRowRequest_(sheetId, 1, presentation.headers));
    requests.push({
      repeatCell: {
        range: { sheetId: sheetId },
        cell: { userEnteredFormat: { textFormat: { fontFamily: "Rubik" } } },
        fields: "userEnteredFormat.textFormat.fontFamily",
      },
    });
    requests.push({
      repeatCell: {
        range: { sheetId: sheetId, startRowIndex: 0, endRowIndex: 1, startColumnIndex: 0, endColumnIndex: presentation.headers.length },
        cell: { userEnteredFormat: { textFormat: { fontFamily: "Rubik", bold: true, foregroundColor: { red: 1, green: 1, blue: 1 } }, horizontalAlignment: "RIGHT" } },
        fields: "userEnteredFormat.textFormat.fontFamily,userEnteredFormat.textFormat.bold,userEnteredFormat.textFormat.foregroundColor,userEnteredFormat.horizontalAlignment",
      },
    });
  });
  Sheets.Spreadsheets.batchUpdate({ requests: requests }, config.workbookId);
}

function upsertSettings_(sheet, config) {
  const rows = sheet.getDataRange().getValues();
  const values = settingsMap_(rows);
  const updates = { workspace_id: config.workspaceId, financial_coverage: "deals_only" };
  const missing = Object.keys(updates).filter(function (key) { return !Object.prototype.hasOwnProperty.call(values, key); });
  if (rows.length - 1 + missing.length > MAX_SETTINGS_ROWS) throw ledgerError_("settings_capacity_exceeded");
  const nextRows = rows.map(function (row) { return [row[0], row[1]]; });
  Object.keys(updates).forEach(function (key) {
    if (Object.prototype.hasOwnProperty.call(values, key)) {
      const index = rows.findIndex(function (row, i) { return i > 0 && String(row[0]) === key; });
      nextRows[index][1] = updates[key];
    } else {
      nextRows.push([key, updates[key]]);
    }
  });
  sheet.getRange(1, 1, nextRows.length, 2).setValues(nextRows);
}

function resolveSheet_(spreadsheet, logicalName) {
  const english = spreadsheet.getSheetByName(logicalName);
  const hebrew = spreadsheet.getSheetByName(LEDGER_HEBREW[logicalName].tab);
  if (english && hebrew) throw ledgerError_("ambiguous_sheet_alias_" + logicalName.replace(/\s+/g, "_").toLowerCase());
  if (!english && !hebrew) throw ledgerError_("missing_sheet_" + logicalName.replace(/\s+/g, "_").toLowerCase());
  return { sheet: english || hebrew, schema: english ? "english" : "hebrew" };
}

function assertHeaders_(actual, expected, name) {
  if (!actual || expected.some(function (value, i) { return actual[i] !== value; })) throw ledgerError_("invalid_headers_" + name.replace(/\s+/g, "_").toLowerCase());
}

function assertResolvedHeaders_(actual, resolved, logicalName) {
  const expected = resolved.schema === "english" ? LEDGER_TABS[logicalName] : LEDGER_HEBREW[logicalName].headers;
  assertHeaders_(actual, expected, logicalName);
}

function hmacHex_(value, secret) { return bytesHex_(Utilities.computeHmacSha256Signature(value, secret, Utilities.Charset.UTF_8)); }
function sha256Hex_(value) { return bytesHex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, value, Utilities.Charset.UTF_8)); }
function bytesHex_(bytes) { return bytes.map(function (b) { const v = b < 0 ? b + 256 : b; return ("0" + v.toString(16)).slice(-2); }).join(""); }
function utf8Bytes_(value) { return Utilities.newBlob(value).getBytes().length; }
function snapshotEnvelopeBytes_(snapshotResult, config) {
  const longestOwner = config.owners.reduce(function (longest, owner) { return owner.length > longest.length ? owner : longest; }, "0");
  const inner = JSON.stringify({
    request_id: "00000000-0000-4000-8000-000000000000",
    workspace_id: config.workspaceId,
    owner_id: longestOwner,
    status: "ok",
    result: snapshotResult,
  });
  return utf8Bytes_(JSON.stringify({ payload: inner, signature: new Array(65).join("0") }));
}
function constantTimeEqual_(left, right) { if (left.length !== right.length) return false; let diff = 0; for (let i = 0; i < left.length; i += 1) diff |= left.charCodeAt(i) ^ right.charCodeAt(i); return diff === 0; }
function plainObject_(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.prototype.toString.call(value) === "[object Object]"; }
function ledgerError_(code) { const error = new Error(code); error.ledgerCode = code; return error; }

function safeRequestIdentity_(transport, config) {
  let raw = {};
  try { raw = transport && typeof transport.payload === "string" ? JSON.parse(transport.payload) : {}; } catch (_) { raw = {}; }
  return {
    request_id: typeof raw.request_id === "string" ? raw.request_id : "00000000-0000-4000-8000-000000000000",
    workspace_id: config && config.workspaceId ? config.workspaceId : "TEST-unavailable",
    owner_id: typeof raw.owner_id === "string" && /^[0-9]+$/.test(raw.owner_id) ? raw.owner_id : "0",
  };
}

function signedResponse_(request, status, result, config, tolerateMissingConfig) {
  const inner = JSON.stringify({ request_id: request.request_id, workspace_id: request.workspace_id, owner_id: request.owner_id, status: status, result: result });
  const signature = config && config.secret ? hmacHex_(inner, config.secret) : "";
  if (!signature && !tolerateMissingConfig) throw ledgerError_("unavailable");
  return ContentService.createTextOutput(JSON.stringify({ payload: inner, signature: signature })).setMimeType(ContentService.MimeType.JSON);
}
