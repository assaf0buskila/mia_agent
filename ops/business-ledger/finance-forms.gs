/* global BusinessLedgerCore, BusinessLedgerFinanceCore, HtmlService, LockService, PropertiesService, SpreadsheetApp, Utilities */
"use strict";

const FINANCE_FORM_REVIEW_KEY = "BUSINESS_LEDGER_FINANCE_REVIEW_V1";
const FINANCE_FORM_PENDING_KEY = "BUSINESS_LEDGER_FINANCE_PENDING_V1";
const FINANCE_FORM_REVIEW_MS = 120000;

function financeFormExactKeys_(value, keys, code) {
  if (!plainObject_(value) || Object.keys(value).sort().join(",") !== keys.slice().sort().join(",")) throw ledgerError_(code);
}

function financeFormHash_(operation, data) {
  return sha256Hex_(BusinessLedgerCore.stableStringify({operation: operation, data: data}));
}

function financeFormAnchor_(operation, data) {
  if (operation === "save_schedule") return {target_id: data.deal_id, target_revision: data.expected_revision, receipt_id: data.deal_id, receipt_revision: data.expected_revision + 1};
  if (operation === "record_payment") return {target_id: data.payment.charge_id, target_revision: data.expected_charge_revision, receipt_id: data.payment.id, receipt_revision: 1};
  if (operation === "record_expense") return {target_id: data.expense.id, target_revision: 0, receipt_id: data.expense.id, receipt_revision: 1};
  if (operation === "save_project") return {target_id: data.project.id, target_revision: data.expected_revision, receipt_id: data.project.id, receipt_revision: data.expected_revision + 1};
  throw ledgerError_("invalid_operation");
}

function assertNoCrossFormPending_() {
  const properties = PropertiesService.getUserProperties();
  if (properties.getProperty(DEAL_FORM_PENDING_KEY)) throw ledgerError_("deal_form_recovery_required");
  if (properties.getProperty(FINANCE_FORM_PENDING_KEY)) throw ledgerError_("finance_form_recovery_required");
}

function financeFormCurrent_(state, operation, data) {
  if (operation === "save_schedule") {
    const deal = state.byId[data.deal_id];
    if (!deal || deal.revision !== data.expected_revision) throw ledgerError_("revision_conflict");
    if (state.financeRows.Charges.some(function(charge) { return charge.deal_id === deal.id; })) throw ledgerError_("financial_adjustment_required");
    if (data.charge_ids.some(function(id) { return state.financeById.Charges[id]; })) throw ledgerError_("duplicate_charge_id");
    return {deal: deal, plan: BusinessLedgerFinanceCore.generateChargePlan({deal: deal, number_of_charges: data.number_of_charges, first_due_date: data.first_due_date, charge_ids: data.charge_ids, updated_at: new Date().toISOString()})};
  }
  if (operation === "record_payment") {
    const charge = state.financeById.Charges[data.payment.charge_id];
    if (!charge || charge.revision !== data.expected_charge_revision || charge.status !== "planned") throw ledgerError_("revision_conflict");
    if (state.financeById.Payments[data.payment.id]) throw ledgerError_("financial_adjustment_required");
    const received = state.financeRows.Payments.filter(function(payment) { return payment.charge_id === charge.id; }).reduce(function(sum, payment) { return sum + payment.amount_minor; }, 0);
    if (!Number.isSafeInteger(received) || received + data.payment.amount_minor > charge.amount_minor) throw ledgerError_("payment_exceeds_charge");
    if (data.payment.received_at > new Date().toISOString()) throw ledgerError_("invalid_received_at");
    return {charge: charge, remaining_minor: charge.amount_minor - received - data.payment.amount_minor};
  }
  if (operation === "record_expense") {
    if (state.financeById.Expenses[data.expense.id]) throw ledgerError_("financial_adjustment_required");
    if (data.expense.project_id !== null && !state.financeById.Projects[data.expense.project_id]) throw ledgerError_("orphan_expense_project");
    return {};
  }
  const current = state.financeById.Projects[data.project.id];
  if ((current ? current.revision : 0) !== data.expected_revision) throw ledgerError_("revision_conflict");
  return {project: current || null};
}

function financeFormSummary_(operation, data, current) {
  if (operation === "save_schedule") {
    const lines = ["תוכנית חיובים לעסקה: " + current.deal.title, "סכום העסקה הכולל באגורות: " + current.deal.amount_minor, "מספר חיובים: " + current.plan.length];
    current.plan.forEach(function(charge, index) { lines.push((index + 1) + ". " + charge.due_date + " — " + charge.amount_minor + " אגורות"); });
    return lines.join("\n");
  }
  if (operation === "record_payment") return ["אישור ידני של תשלום שהתקבל", "מזהה חיוב: " + data.payment.charge_id, "סכום באגורות: " + data.payment.amount_minor, "התקבל בתאריך: " + data.payment.received_at, "יתרה בחיוב לאחר הרישום: " + current.remaining_minor + " אגורות"].join("\n");
  if (operation === "record_expense") return ["רישום הוצאה חדשה", "סכום באגורות: " + data.expense.amount_minor, "תאריך: " + data.expense.occurred_on, "פרויקט: " + (data.expense.project_id || "הוצאה כללית"), "הערות: " + (data.expense.notes || "ללא")].join("\n");
  return [data.expected_revision ? "עדכון פרויקט" : "יצירת פרויקט", "כותרת: " + data.project.title, "סטטוס: " + data.project.status, "תאריך יעד: " + (data.project.deadline || "ללא"), "הערות: " + (data.project.notes || "ללא"), "גרסה צפויה: " + data.expected_revision].join("\n");
}

function writeFinanceReview_(review) {
  const properties = PropertiesService.getUserProperties();
  const serialized = JSON.stringify(review);
  properties.setProperty(FINANCE_FORM_REVIEW_KEY, serialized);
  if (properties.getProperty(FINANCE_FORM_REVIEW_KEY) !== serialized) throw ledgerError_("review_persistence_failed");
}

function readFinanceReview_(config, owner) {
  const properties = PropertiesService.getUserProperties();
  const raw = properties.getProperty(FINANCE_FORM_REVIEW_KEY);
  if (!raw) return null;
  let review, data;
  try { review = JSON.parse(raw); } catch (_) { throw ledgerError_("review_corrupt"); }
  financeFormExactKeys_(review, ["review_id", "workspace_id", "owner_id", "operation", "payload_json", "data_hash", "target_id", "target_revision", "receipt_id", "receipt_revision", "summary", "created_at", "expires_at"], "review_corrupt");
  if (review.workspace_id !== config.workspaceId || review.owner_id !== owner) throw ledgerError_("review_binding_mismatch");
  BusinessLedgerCore.uuid(review.review_id, "review_corrupt");
  BusinessLedgerCore.uuid(review.target_id, "review_corrupt");
  BusinessLedgerCore.uuid(review.receipt_id, "review_corrupt");
  if (!Number.isSafeInteger(review.target_revision) || !Number.isSafeInteger(review.receipt_revision) || typeof review.summary !== "string" || !/^[0-9a-f]{64}$/.test(review.data_hash)) throw ledgerError_("review_corrupt");
  try { data = JSON.parse(review.payload_json); } catch (_) { throw ledgerError_("review_corrupt"); }
  validateFinanceData_(review.operation, data);
  const anchor = financeFormAnchor_(review.operation, data);
  if (financeFormHash_(review.operation, data) !== review.data_hash || anchor.target_id !== review.target_id || anchor.target_revision !== review.target_revision || anchor.receipt_id !== review.receipt_id || anchor.receipt_revision !== review.receipt_revision) throw ledgerError_("review_corrupt");
  const created = new Date(review.created_at), expires = new Date(review.expires_at);
  if (!Number.isFinite(created.getTime()) || !Number.isFinite(expires.getTime()) || expires.getTime() - created.getTime() !== FINANCE_FORM_REVIEW_MS) throw ledgerError_("review_corrupt");
  review.data = data;
  return review;
}

function prepareFinanceFormReview(operation, data) {
  const config = ledgerConfig_(), owner = operatorOwner_();
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return {status: "unavailable", result: {reason_code: "ledger_busy"}};
  try {
    assertNoCrossFormPending_();
    validateFinanceData_(operation, data);
    const frozen = JSON.parse(JSON.stringify(data));
    const state = readFinanceState_(config);
    const current = financeFormCurrent_(state, operation, frozen);
    const anchor = financeFormAnchor_(operation, frozen);
    const now = new Date(), review = {
      review_id: Utilities.getUuid().toLowerCase(), workspace_id: config.workspaceId, owner_id: owner,
      operation: operation, payload_json: JSON.stringify(frozen), data_hash: financeFormHash_(operation, frozen),
      target_id: anchor.target_id, target_revision: anchor.target_revision, receipt_id: anchor.receipt_id,
      receipt_revision: anchor.receipt_revision, summary: financeFormSummary_(operation, frozen, current),
      created_at: now.toISOString(), expires_at: new Date(now.getTime() + FINANCE_FORM_REVIEW_MS).toISOString(),
    };
    writeFinanceReview_(review);
    return {status: "review", result: {review_id: review.review_id, summary: review.summary, expires_at: review.expires_at}};
  } finally { lock.releaseLock(); }
}

function financePendingExact_(pending, config, owner) {
  financeFormExactKeys_(pending, ["workspace_id", "owner_id", "operation", "request_id", "target_id", "target_revision", "receipt_id", "receipt_revision", "data_hash", "created_at"], "pending_marker_corrupt");
  if (pending.workspace_id !== config.workspaceId || pending.owner_id !== owner) throw ledgerError_("pending_binding_mismatch");
  BusinessLedgerCore.uuid(pending.request_id, "pending_marker_corrupt");
  BusinessLedgerCore.uuid(pending.target_id, "pending_marker_corrupt");
  BusinessLedgerCore.uuid(pending.receipt_id, "pending_marker_corrupt");
  if (!/^[0-9a-f]{64}$/.test(pending.data_hash) || !Number.isSafeInteger(pending.target_revision) || !Number.isSafeInteger(pending.receipt_revision) || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(pending.created_at)) throw ledgerError_("pending_marker_corrupt");
  return pending;
}

function readFinancePending_(config, owner) {
  const raw = PropertiesService.getUserProperties().getProperty(FINANCE_FORM_PENDING_KEY);
  if (!raw) return null;
  let pending;
  try { pending = JSON.parse(raw); } catch (_) { throw ledgerError_("pending_marker_corrupt"); }
  return financePendingExact_(pending, config, owner);
}

function writeFinancePending_(review) {
  const pending = {workspace_id: review.workspace_id, owner_id: review.owner_id, operation: review.operation, request_id: review.data.request_id, target_id: review.target_id, target_revision: review.target_revision, receipt_id: review.receipt_id, receipt_revision: review.receipt_revision, data_hash: review.data_hash, created_at: new Date().toISOString()};
  const properties = PropertiesService.getUserProperties(), serialized = JSON.stringify(pending);
  properties.setProperty(FINANCE_FORM_PENDING_KEY, serialized);
  if (properties.getProperty(FINANCE_FORM_PENDING_KEY) !== serialized) throw ledgerError_("pending_marker_persistence_failed");
  return pending;
}

function financePendingClient_(pending) {
  return {operation: pending.operation, request_id: pending.request_id, id: pending.receipt_id, expected_revision: pending.receipt_revision, created_at: pending.created_at};
}

function assertFinancePendingReceipt_(pending, stored, owner) {
  const receipt = stored && stored.receipt;
  const keys = "committed_at,id,mode,receipt_id,request_id,revision,source,status";
  if (!stored || stored.owner_id !== owner || stored.data_hash !== pending.data_hash || !plainObject_(receipt) || Object.keys(receipt).sort().join(",") !== keys || receipt.status !== "committed" || receipt.request_id !== pending.request_id || receipt.id !== pending.receipt_id || receipt.revision !== pending.receipt_revision || receipt.source !== "google_sheets" || receipt.mode !== "TEST" || typeof receipt.committed_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(receipt.committed_at) || new Date(receipt.committed_at).toISOString() !== receipt.committed_at) throw ledgerError_("pending_receipt_mismatch");
  BusinessLedgerCore.uuid(receipt.receipt_id, "pending_receipt_mismatch");
  return receipt;
}

function confirmFinanceFormReview(reviewId, explicitApproval) {
  const config = ledgerConfig_(), owner = operatorOwner_();
  BusinessLedgerCore.uuid(reviewId, "invalid_review_id");
  if (explicitApproval !== true) throw ledgerError_("explicit_approval_required");
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return {status: "unavailable", result: {reason_code: "ledger_busy"}};
  try {
    assertNoCrossFormPending_();
    const review = readFinanceReview_(config, owner);
    if (!review || review.review_id !== reviewId) throw ledgerError_("review_not_found");
    if (new Date(review.expires_at).getTime() <= Date.now()) {
      PropertiesService.getUserProperties().deleteProperty(FINANCE_FORM_REVIEW_KEY);
      return {status: "invalid", result: {reason_code: "review_expired"}};
    }
    const state = readFinanceState_(config);
    financeFormCurrent_(state, review.operation, review.data);
    PropertiesService.getUserProperties().deleteProperty(FINANCE_FORM_REVIEW_KEY);
    const pending = writeFinancePending_(review);
    const result = financeOperation_({owner_id: owner, operation: review.operation, data: review.data}, config);
    if (result.status === "ok") assertFinancePendingReceipt_(pending, {owner_id: owner, data_hash: review.data_hash, receipt: result.result}, owner);
    if (result.status !== "ok" && result.status !== "unavailable") PropertiesService.getUserProperties().deleteProperty(FINANCE_FORM_PENDING_KEY);
    return result;
  } finally { lock.releaseLock(); }
}

function getFinanceFormBootstrap() {
  const config = ledgerConfig_(), owner = operatorOwner_();
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw ledgerError_("ledger_busy");
  try {
    if (PropertiesService.getUserProperties().getProperty(DEAL_FORM_PENDING_KEY)) return {mode: "blocked", reason_code: "deal_form_recovery_required"};
    const pending = readFinancePending_(config, owner);
    const state = readFinanceState_(config);
    if (pending) {
      const stored = state.receipts[pending.request_id];
      if (!stored) return {mode: "recovery", pending: financePendingClient_(pending)};
      return {mode: "committed", pending: financePendingClient_(pending), receipt: assertFinancePendingReceipt_(pending, stored, owner)};
    }
    const review = readFinanceReview_(config, owner);
    if (review && new Date(review.expires_at).getTime() > Date.now()) return {mode: "review", review: {review_id: review.review_id, summary: review.summary, expires_at: review.expires_at}};
    if (review) PropertiesService.getUserProperties().deleteProperty(FINANCE_FORM_REVIEW_KEY);
    return {mode: "edit", deals: state.deals, charges: state.financeRows.Charges, projects: state.financeRows.Projects};
  } finally { lock.releaseLock(); }
}

function checkPendingFinanceReceipt() {
  const config = ledgerConfig_(), owner = operatorOwner_();
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return {status: "unavailable", result: {reason_code: "ledger_busy"}};
  try {
    const pending = readFinancePending_(config, owner);
    if (!pending) return {status: "not_found", result: {reason_code: "pending_marker_not_found"}};
    const state = readFinanceState_(config), stored = state.receipts[pending.request_id];
    if (!stored) return {status: "not_found", result: {reason_code: "receipt_not_found", request_id: pending.request_id}};
    return {status: "ok", result: assertFinancePendingReceipt_(pending, stored, owner)};
  } finally { lock.releaseLock(); }
}

function acknowledgeFinanceFormReceipt(requestId, receiptId) {
  const config = ledgerConfig_(), owner = operatorOwner_();
  BusinessLedgerCore.uuid(requestId, "invalid_request_id");
  BusinessLedgerCore.uuid(receiptId, "invalid_receipt_id");
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return {status: "unavailable", result: {reason_code: "ledger_busy"}};
  try {
    const pending = readFinancePending_(config, owner);
    if (!pending || pending.request_id !== requestId) return {status: "invalid", result: {reason_code: "pending_marker_mismatch"}};
    const state = readFinanceState_(config), stored = state.receipts[requestId];
    if (!stored) return {status: "not_found", result: {reason_code: "receipt_not_found"}};
    const receipt = assertFinancePendingReceipt_(pending, stored, owner);
    if (receipt.receipt_id !== receiptId) return {status: "not_found", result: {reason_code: "receipt_not_found"}};
    PropertiesService.getUserProperties().deleteProperty(FINANCE_FORM_PENDING_KEY);
    return {status: "ok", result: receipt};
  } finally { lock.releaseLock(); }
}

function showFinanceDialog() {
  operatorOwner_();
  const html = HtmlService.createHtmlOutput(financeDialogHtml_()).setWidth(620).setHeight(760);
  SpreadsheetApp.getUi().showModalDialog(html, "AssafWeb OS Business — כספים");
}

function financeDialogHtml_() {
  return `<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><style>
  *{box-sizing:border-box}body{font-family:Rubik,Arial,sans-serif;margin:0;padding:18px;color:#172033;line-height:1.55}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.full{grid-column:1/-1}label{display:block;font-weight:600}input,select{width:100%;min-height:42px;padding:8px;border:1px solid #b8c2d1;border-radius:8px;font:inherit}button{min-height:44px;padding:9px 16px;border:0;border-radius:9px;background:#1255cc;color:#fff;font:inherit;font-weight:700}.secondary{background:#e8eef8;color:#17345f}.status{white-space:pre-wrap;background:#f4f7fb;padding:10px;margin-top:12px}.ltr{direction:ltr}.actions{display:flex;gap:8px;margin-top:14px}@media(max-width:520px){.grid{grid-template-columns:1fr}}
  </style></head><body><h2>ניהול כספים</h2><p>סביבת בדיקה / נתונים סינתטיים. עסקה אינה כסף שהתקבל. תוכנית החיובים מחלקת את סכום העסקה הכולל.</p>
  <div class="grid"><div class="full"><label for="operation">פעולה</label><select id="operation" onchange="selectedProject()"><option value="save_schedule">יצירת תוכנית חיובים</option><option value="record_payment">אישור תשלום שהתקבל</option><option value="record_expense">רישום הוצאה</option><option value="save_project">יצירה או עדכון פרויקט</option></select></div>
  <div><label for="deal">עסקה</label><select id="deal"></select></div><div><label for="charge">חיוב</label><select id="charge"></select></div><div><label for="count">מספר חיובים</label><input id="count" type="number" min="1" max="36" value="1"></div><div><label for="firstDue">תאריך חיוב ראשון</label><input id="firstDue" type="date"></div>
  <div><label for="amount">סכום (₪)</label><input id="amount" class="ltr" inputmode="decimal"></div><div><label for="receivedAt">מועד קבלה</label><input id="receivedAt" type="datetime-local" step="1"></div><div class="full"><label><input id="confirmed" type="checkbox" style="width:auto;min-height:auto"> אני מאשר ידנית שהכסף התקבל בפועל</label></div>
  <div><label for="expenseDate">תאריך הוצאה</label><input id="expenseDate" type="date"></div><div><label for="project">פרויקט (ריק = כללי / חדש)</label><select id="project" onchange="selectedProject()"><option value="">ללא / חדש</option></select></div>
  <div class="full"><label for="title">כותרת פרויקט</label><input id="title" maxlength="160" dir="auto"></div><div><label for="projectStatus">סטטוס פרויקט</label><select id="projectStatus"><option value="planned">מתוכנן</option><option value="active">פעיל</option><option value="completed">הושלם</option><option value="cancelled">בוטל</option></select></div><div><label for="deadline">תאריך יעד</label><input id="deadline" type="date"></div><div class="full"><label for="notes">הערות</label><input id="notes" maxlength="1000" dir="auto"></div></div>
  <div class="actions"><button id="prepare" onclick="prepareReview()">בדיקה לפני שמירה</button><button class="secondary" onclick="google.script.host.close()">סגירה</button></div><section id="review" hidden><h3>לאשר בדיוק את הפעולה הבאה?</h3><div id="summary" class="status"></div><div class="actions"><button id="confirm" onclick="confirmReview()">אישור ושמירה</button></div></section><section id="recovery" hidden aria-label="שחזור קבלה"><div class="actions"><button id="receiptCheck" onclick="checkFinanceReceipt()">בדיקת קבלה בלבד</button><button id="receiptAck" onclick="acknowledgeFinanceReceipt()" hidden>אישור קבלה וסגירה</button></div></section><div id="out" class="status" role="status"></div>
  <script>
  const byDeal={},byCharge={},byProject={};let locked=true,reviewId=null,pendingReceipt=null;const fields=["operation","deal","charge","count","firstDue","amount","receivedAt","confirmed","expenseDate","project","title","projectStatus","deadline","notes","prepare"];
  function uuid(){if(crypto.randomUUID)return crypto.randomUUID();const b=new Uint8Array(16);crypto.getRandomValues(b);b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;const h=[...b].map(x=>x.toString(16).padStart(2,"0")).join("");return h.slice(0,8)+"-"+h.slice(8,12)+"-"+h.slice(12,16)+"-"+h.slice(16,20)+"-"+h.slice(20)}
  function lock(value){locked=value;fields.forEach(id=>document.getElementById(id).disabled=value)}function add(select,row,label){const o=document.createElement("option");o.value=row.id;o.textContent=label;select.appendChild(o)}function minor(value){if(typeof value!=="string"||!/^(?:0|[1-9][0-9]*)(?:\\.[0-9]{1,2})?$/.test(value))throw Error("invalid_amount");return Math.round(Number(value)*100)}function utc(value){const result=new Date(value).toISOString();if(!/^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$/.test(result))throw Error("invalid_received_at");return result}
  function receiptText(r){return ["נמצאה קבלה לשמירה מאושרת.","מזהה רשומה: "+r.id,"גרסה: "+r.revision,"מזהה בקשה: "+r.request_id,"מזהה קבלה: "+r.receipt_id,"נשמר בתאריך: "+r.committed_at].join("\\n")}
  function selectedProject(){if(locked||document.getElementById("operation").value!=="save_project")return;const p=byProject[document.getElementById("project").value];document.getElementById("title").value=p?p.title:"";document.getElementById("projectStatus").value=p?p.status:"planned";document.getElementById("deadline").value=p?(p.deadline||""):"";document.getElementById("notes").value=p?p.notes:""}
  function recovery(receipt){lock(true);reviewId=null;document.getElementById("review").hidden=true;document.getElementById("summary").textContent="";document.getElementById("recovery").hidden=false;pendingReceipt=receipt||null;document.getElementById("receiptCheck").disabled=false;document.getElementById("receiptAck").hidden=!receipt;document.getElementById("receiptAck").disabled=false;document.getElementById("out").textContent=receipt?receiptText(receipt):"תוצאת השמירה אינה ידועה. ניתן לבדוק קבלה בלבד; אין לשלוח שוב."}
  function checkFinanceReceipt(){document.getElementById("receiptCheck").disabled=true;google.script.run.withSuccessHandler(r=>{if(r.status==="ok"){recovery(r.result);return}recovery(null);document.getElementById("out").textContent=r.status==="not_found"?"עדיין לא נמצאה קבלה. מצב השחזור נשאר נעול. אפשר לבדוק שוב מאוחר יותר.":"בדיקת הקבלה אינה זמינה כרגע. מצב השחזור נשאר נעול."}).withFailureHandler(()=>{recovery(null);document.getElementById("out").textContent="בדיקת הקבלה אינה זמינה כרגע. מצב השחזור נשאר נעול."}).checkPendingFinanceReceipt()}
  function acknowledgeFinanceReceipt(){if(!pendingReceipt)return;const r=pendingReceipt;pendingReceipt=null;document.getElementById("receiptAck").disabled=true;document.getElementById("receiptCheck").disabled=true;google.script.run.withSuccessHandler(a=>{if(a.status==="ok"){document.getElementById("out").textContent=receiptText(a.result)+"\\nהקבלה אושרה. החלון ייסגר.";google.script.host.close();return}recovery(r);document.getElementById("out").textContent=receiptText(r)+"\\nאישור הקבלה לא הושלם; מצב השחזור נשאר נעול."}).withFailureHandler(()=>{recovery(r);document.getElementById("out").textContent=receiptText(r)+"\\nאישור הקבלה לא הושלם; מצב השחזור נשאר נעול."}).acknowledgeFinanceFormReceipt(r.request_id,r.receipt_id)}
  function boot(result){if(result.mode==="edit"){result.deals.forEach(d=>{byDeal[d.id]=d;add(document.getElementById("deal"),d,d.title+" · גרסה "+d.revision)});result.charges.filter(c=>c.status==="planned").forEach(c=>{byCharge[c.id]=c;add(document.getElementById("charge"),c,c.due_date+" · "+c.amount_minor+" אגורות")});result.projects.forEach(p=>{byProject[p.id]=p;add(document.getElementById("project"),p,p.title+" · גרסה "+p.revision)});lock(false);return}if(result.mode==="review"){reviewId=result.review.review_id;document.getElementById("summary").textContent=result.review.summary;document.getElementById("review").hidden=false;lock(true);document.getElementById("confirm").disabled=false;return}if(result.mode==="blocked"){lock(true);document.getElementById("out").textContent="קיימת עסקה שממתינה לשחזור. סגרו חלון זה ובחרו יצירה או עדכון עסקה בתפריט.";return}recovery(result.receipt)}
  function data(){const op=document.getElementById("operation").value,request_id=uuid();if(op==="save_schedule"){const d=byDeal[document.getElementById("deal").value],n=Number(document.getElementById("count").value);return {op,data:{request_id,expected_revision:d.revision,deal_id:d.id,number_of_charges:n,first_due_date:document.getElementById("firstDue").value,charge_ids:Array.from({length:n},uuid)}}}if(op==="record_payment"){const c=byCharge[document.getElementById("charge").value];return {op,data:{request_id,expected_charge_revision:c.revision,confirmed_received:document.getElementById("confirmed").checked,payment:{id:uuid(),charge_id:c.id,amount_minor:minor(document.getElementById("amount").value),currency:"ILS",received_at:utc(document.getElementById("receivedAt").value)}}}}if(op==="record_expense")return {op,data:{request_id,expected_revision:0,expense:{id:uuid(),project_id:document.getElementById("project").value||null,amount_minor:minor(document.getElementById("amount").value),currency:"ILS",occurred_on:document.getElementById("expenseDate").value,notes:document.getElementById("notes").value}}};const p=byProject[document.getElementById("project").value];return {op,data:{request_id,expected_revision:p?p.revision:0,project:{id:p?p.id:uuid(),title:document.getElementById("title").value,status:document.getElementById("projectStatus").value,deadline:document.getElementById("deadline").value||null,notes:document.getElementById("notes").value}}}}
  function prepareReview(){if(locked)return;let request;try{request=data()}catch(_){document.getElementById("out").textContent="הנתונים אינם תקינים.";return}lock(true);google.script.run.withSuccessHandler(r=>{if(r.status!=="review"){document.getElementById("out").textContent="הפעולה נדחתה: "+r.status;return}reviewId=r.result.review_id;document.getElementById("summary").textContent=r.result.summary;document.getElementById("review").hidden=false;document.getElementById("confirm").disabled=false}).withFailureHandler(()=>{document.getElementById("out").textContent="לא ניתן להכין את האישור כרגע."}).prepareFinanceFormReview(request.op,request.data)}
  function confirmReview(){if(!reviewId)return;const id=reviewId;reviewId=null;document.getElementById("confirm").disabled=true;document.getElementById("out").textContent="הפעולה נשלחה פעם אחת. אין לשלוח שוב עד לבדיקת קבלה.";google.script.run.withSuccessHandler(r=>{if(r.status==="ok"){recovery(r.result);return}if(r.status==="invalid"||r.status==="conflict"){lock(true);document.getElementById("review").hidden=true;document.getElementById("summary").textContent="";document.getElementById("out").textContent="הפעולה נדחתה. פתחו מחדש כדי לבדוק פרטים מעודכנים.";return}recovery(null)}).withFailureHandler(()=>recovery(null)).confirmFinanceFormReview(id,true)}
  google.script.run.withSuccessHandler(boot).withFailureHandler(()=>{lock(true);document.getElementById("out").textContent="לא ניתן לטעון נתונים כרגע."}).getFinanceFormBootstrap();
  </script></body></html>`;
}
