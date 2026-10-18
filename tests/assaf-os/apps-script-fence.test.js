"use strict";

// V2-31 (V2_PG_CONTRACTS.md section 16): the Script Properties fence, proved on the real Apps Script
// source. T3 of the cutover sets the Script Property ASSAF_OS_MODE to FENCED (no republish). From then
// on, every entry point that can read or write the workbook must refuse with `unavailable` before it
// opens the workbook or takes a lock, and setting the property back to LIVE must bring all of them back.

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const { createHarness } = require("./apps-script-harness.js");

// The exact unsigned reply a Script with no readable config gives. tests/workers/test_os_authority_probes.py
// reads this literal back from this file and compares it with the constant `mia os-authority` probes
// with, so the CLI's `sheets_not_fenced` check and the real Script cannot drift apart.
// FENCED_REPLY:begin
const FENCED_REPLY = {
  "api_version": "assaf-os.v1",
  "operation": "invalid",
  "actor_source": "OS",
  "owner_id": "0",
  "workspace_id": "unavailable",
  "workbook_id": "unavailable",
  "request_id": "invalid-request",
  "timestamp": 0,
  "mode": "UNKNOWN",
  "ok": false,
  "data": { "error": { "code": "unavailable" } }
};
// FENCED_REPLY:end

// The four entry points the contract names, plus the tab-picker helper they share.
const ENTRY_POINTS = ["doPost", "reconcileAssafOsEdit", "reconcileAssafOsWorkbook", "refreshAssafOsReferenceValidations"];
// Every Script Property value the Script does not accept as a live mode.
const NOT_LIVE_MODES = ["FENCED", "fenced", "Live", "", " LIVE", "LIVE ", "ARCHIVE", "READ_ONLY", "UNKNOWN"];

function newHarness(mode, extra = {}) {
  const h = createHarness({ maxRows: 25, withSheets: true, mode, ...extra });
  // Spies on the two things a fenced entry point must never reach: the workbook and the script lock.
  const touched = { openById: 0, lock: 0, batchUpdate: 0 };
  const realOpen = h.context.SpreadsheetApp.openById;
  h.context.SpreadsheetApp.openById = (id) => { touched.openById += 1; return realOpen(id); };
  const realLock = h.context.LockService.getScriptLock;
  h.context.LockService.getScriptLock = () => { touched.lock += 1; return realLock(); };
  const realBatch = h.context.Sheets.Spreadsheets.batchUpdate;
  h.context.Sheets.Spreadsheets.batchUpdate = (body, id) => { touched.batchUpdate += 1; return realBatch(body, id); };
  return { h, touched };
}

function signedBody(h, request) {
  const payload = JSON.stringify(request);
  const signature = crypto.createHmac("sha256", h.secret).update(payload).digest("hex");
  return JSON.stringify({ payload, signature });
}

// The raw reply of doPost, with no signature check (a fenced Script gives none).
function rawPost(h, request) {
  const output = h.context.doPost({ postData: { contents: signedBody(h, request) } });
  const wrapper = JSON.parse(output.text);
  assert.deepEqual(Object.keys(wrapper).sort(), ["payload", "signature"]);
  return { signature: wrapper.signature, payload: JSON.parse(wrapper.payload), text: wrapper.payload };
}

function workbookRows(h) {
  return JSON.stringify([...h.workbook.sheets.values()].map((sheet) => [sheet.getName(), sheet.rows, sheet.formulas]));
}

function assertUntouched(h, touched, rowsBefore, label) {
  assert.deepEqual(touched, { openById: 0, lock: 0, batchUpdate: 0 }, `${label}: no workbook, lock or batch write`);
  assert.deepEqual(h.metrics(), {
    getValues: 0, getFormulas: 0, getLastRow: 0, getLastColumn: 0, getDataRange: 0, setDataValidation: 0, batchUpdate: 0,
  }, `${label}: no sheet read or validation write`);
  assert.equal(workbookRows(h), rowsBefore, `${label}: the workbook is exactly as it was`);
}

function inputTabEvent(h, key = "tasks") {
  const sheet = h.workbook.getSheetByName(h.Schema.tab(key).name);
  return { range: sheet.getRange(2, 2) };
}

test("the pinned FENCED_REPLY is what the real Script answers under FENCED", () => {
  const { h } = newHarness("FENCED");
  const reply = rawPost(h, h.request("schema", {}));
  assert.equal(reply.signature, "", "a fenced Script has no secret to sign with");
  assert.deepEqual(reply.payload, FENCED_REPLY);
});

test("doPost answers `unavailable`, unsigned and identically, for every operation under FENCED", () => {
  const { h, touched } = newHarness("FENCED");
  const rowsBefore = workbookRows(h);
  const operations = {
    schema: {},
    snapshot: {},
    proposal: { proposal_id: "PR_12345678901234567890" },
    receipt: { request_id: "receipt-12345678" },
    propose: { request_id: "proposal-fenced-1", entity: "leads", record_id: null, expected_revision: 0, input: { name: "X", phone: "+972501234567", path: "לימוד", status: "חדש", source_note: "synthetic" } },
    approve: { proposal_id: "PR_12345678901234567890", request_id: "approve-12345678" },
    reject: { proposal_id: "PR_12345678901234567890", request_id: "reject-123456789" },
  };
  for (const [operation, data] of Object.entries(operations)) {
    for (const actor of ["OS", "MIA", "TELEGRAM_APPROVAL"]) {
      const reply = rawPost(h, h.request(operation, data, { actor_source: actor }));
      assert.equal(reply.signature, "", `${operation}/${actor} is unsigned`);
      assert.deepEqual(reply.payload, FENCED_REPLY, `${operation}/${actor} reveals nothing of the request`);
    }
  }
  assertUntouched(h, touched, rowsBefore, "doPost");
});

test("doPost refuses a malformed or badly signed body the same way under FENCED (config is read first)", () => {
  const { h, touched } = newHarness("FENCED");
  const rowsBefore = workbookRows(h);
  for (const contents of ["", "not json", "{}", JSON.stringify({ payload: "{}", signature: "0".repeat(64) }), undefined]) {
    const output = h.context.doPost({ postData: { contents } });
    const wrapper = JSON.parse(output.text);
    assert.equal(wrapper.signature, "");
    const code = JSON.parse(wrapper.payload).data.error.code;
    assert.equal(code, "unavailable");
  }
  assert.equal(JSON.parse(h.context.doPost({}).text).signature, "");
  assertUntouched(h, touched, rowsBefore, "malformed doPost");
});

test("reconcileAssafOsEdit, reconcileAssafOsWorkbook and refreshAssafOsReferenceValidations throw `unavailable` under FENCED", () => {
  const { h, touched } = newHarness("FENCED");
  h.addRecord("clients", { name: "לקוחה", status: "פעיל" });
  h.resetMetrics();
  const rowsBefore = workbookRows(h);
  const calls = {
    reconcileAssafOsEdit: () => h.context.reconcileAssafOsEdit(inputTabEvent(h, "clients")),
    reconcileAssafOsWorkbook: () => h.context.reconcileAssafOsWorkbook(),
    refreshAssafOsReferenceValidations: () => h.context.refreshAssafOsReferenceValidations(),
  };
  for (const [name, call] of Object.entries(calls)) {
    assert.throws(call, (error) => error.assafOsCode === "unavailable" && error.message === "unavailable", name);
  }
  assertUntouched(h, touched, rowsBefore, "entry points");
  assert.equal(h.scriptProperties.ASSAF_OS_REFERENCE_VALIDATION_DIRTY, undefined, "no property is written either");
  assert.equal(h.scriptProperties.ASSAF_OS_REFERENCE_VALIDATION_FINGERPRINT, undefined);
});

test("the contract's four entry points are exactly the Script's globally callable fence surface", () => {
  const { h } = newHarness("FENCED");
  for (const name of ENTRY_POINTS) assert.equal(typeof h.context[name], "function", name);
  // Anything else that could touch the workbook sits behind one of these through assafOsConfig_, or is an
  // installer that needs an explicit workbook id and the schema confirmation (never a trigger).
  const source = fs.readFileSync("ops/assaf-os/UnifiedRuntime.gs", "utf8") + fs.readFileSync("ops/assaf-os/UnifiedSheets.gs", "utf8");
  const publicFunctions = [...source.matchAll(/^function ([A-Za-z][A-Za-z0-9]*)\(/gm)].map((match) => match[1]);
  const configReaders = new Set();
  for (const name of publicFunctions) {
    const body = source.slice(source.indexOf(`function ${name}(`)).split(/\nfunction /)[0];
    if (/assafOsConfig_\(\)/.test(body)) configReaders.add(name);
  }
  for (const name of ENTRY_POINTS) assert.ok(configReaders.has(name), `${name} reads assafOsConfig_ first`);
  // Those four are the only public (non-underscore) functions that read the config.
  assert.deepEqual([...configReaders].sort(), [...ENTRY_POINTS].sort());
});

test("any ASSAF_OS_MODE other than exactly TEST or LIVE fences every entry point", () => {
  for (const mode of NOT_LIVE_MODES) {
    const { h, touched } = newHarness("LIVE");
    h.scriptProperties.ASSAF_OS_MODE = mode;
    const rowsBefore = workbookRows(h);
    assert.deepEqual(rawPost(h, h.request("snapshot", {})).payload, FENCED_REPLY, JSON.stringify(mode));
    assert.throws(() => h.context.reconcileAssafOsWorkbook(), { message: "unavailable" }, JSON.stringify(mode));
    assert.throws(() => h.context.reconcileAssafOsEdit(inputTabEvent(h)), { message: "unavailable" }, JSON.stringify(mode));
    assert.throws(() => h.context.refreshAssafOsReferenceValidations(), { message: "unavailable" }, JSON.stringify(mode));
    assertUntouched(h, touched, rowsBefore, JSON.stringify(mode));
  }
});

test("a missing property fences too: each of the five Script Properties is needed", () => {
  for (const missing of ["ASSAF_OS_OWNER_ID", "ASSAF_OS_WORKSPACE_ID", "ASSAF_OS_WORKBOOK_ID", "ASSAF_OS_MODE", "ASSAF_OS_HMAC_SECRET"]) {
    const { h, touched } = newHarness("LIVE");
    delete h.scriptProperties[missing];
    const rowsBefore = workbookRows(h);
    assert.deepEqual(rawPost(h, h.request("schema", {})).payload, FENCED_REPLY, missing);
    assert.throws(() => h.context.reconcileAssafOsWorkbook(), { message: "unavailable" }, missing);
    assertUntouched(h, touched, rowsBefore, missing);
  }
});

test("TEST and LIVE both answer; flipping the property is the whole fence (no republish)", () => {
  for (const mode of ["TEST", "LIVE"]) {
    const { h } = newHarness(mode);
    const reply = h.call(h.request("schema", {}));
    assert.equal(reply.ok, true, mode);
    assert.equal(reply.mode, mode);
  }
});

test("under LIVE the four entry points work again after a FENCED period, and the workbook is unchanged by the fence", () => {
  const { h, touched } = newHarness("LIVE");
  h.addRecord("clients", { name: "לקוחה", status: "פעיל" });
  const live = h.call(h.request("schema", {}));
  assert.equal(live.ok, true);

  h.scriptProperties.ASSAF_OS_MODE = "FENCED";
  h.resetMetrics();
  const rowsFenced = workbookRows(h);
  assert.deepEqual(rawPost(h, h.request("schema", {})).payload, FENCED_REPLY);
  assert.throws(() => h.context.reconcileAssafOsWorkbook(), { message: "unavailable" });
  assert.throws(() => h.context.reconcileAssafOsEdit(inputTabEvent(h, "clients")), { message: "unavailable" });
  assert.throws(() => h.context.refreshAssafOsReferenceValidations(), { message: "unavailable" });
  assert.equal(workbookRows(h), rowsFenced);

  // Rollback step: the owner sets the Script Property back to LIVE.
  h.scriptProperties.ASSAF_OS_MODE = "LIVE";
  const before = { ...touched };
  const again = h.call(h.request("snapshot", {}));
  assert.equal(again.ok, true, JSON.stringify(again.data));
  assert.equal(again.mode, "LIVE");
  assert.equal(again.data.records.clients.length, 1);
  assert.ok(touched.openById > before.openById, "the workbook is reachable again");

  const reconciled = h.context.reconcileAssafOsWorkbook();
  assert.equal(typeof reconciled, "object");
  assert.equal(reconciled.header_issues.length, 0, "a vm-realm array is not deepStrictEqual to a host one");

  const refreshed = h.context.refreshAssafOsReferenceValidations();
  assert.equal(refreshed.refreshed, true);
  assert.equal(refreshed.workbook_id, h.binding.workbook_id);

  assert.doesNotThrow(() => h.context.reconcileAssafOsEdit(inputTabEvent(h, "clients")));
});

test("a write is possible under LIVE and impossible under FENCED through the same signed request", () => {
  const { h } = newHarness("LIVE");
  const input = { name: "Synthetic fence", phone: "+972501234567", path: "לימוד", status: "חדש", source_note: "synthetic" };
  const body = { request_id: "proposal-fence-test-1", entity: "leads", record_id: null, expected_revision: 0, input };
  const request = h.request("propose", body, { request_id: body.request_id, actor_source: "MIA" });
  const proposal = h.call(request);
  assert.equal(proposal.ok, true, JSON.stringify(proposal.data));
  const proposalsTab = h.workbook.getSheetByName("_assaf_os_proposals");
  const proposalRows = proposalsTab.getLastRow();
  assert.equal(proposalRows, 2, "one proposal row after the live propose");

  h.scriptProperties.ASSAF_OS_MODE = "FENCED";
  const second = h.request("propose", { ...body, request_id: "proposal-fence-test-2" }, { request_id: "proposal-fence-test-2", actor_source: "MIA" });
  assert.deepEqual(rawPost(h, second).payload, FENCED_REPLY);
  const approval = h.request("approve", { proposal_id: proposal.data.proposal_id, request_id: proposal.data.approval_request_id }, { request_id: proposal.data.approval_request_id, actor_source: "TELEGRAM_APPROVAL" });
  assert.deepEqual(rawPost(h, approval).payload, FENCED_REPLY);
  assert.equal(proposalsTab.getLastRow(), proposalRows, "no proposal row was added or decided while fenced");
  assert.equal(h.workbook.getSheetByName("_assaf_os_receipts").getLastRow(), 1, "no receipt while fenced");
});
