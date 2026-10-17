"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const OWNER = "123456789";
const WORKSPACE = "assaf-ledger-TEST";

function signedBytes(buffer) { return [...buffer].map((value) => value > 127 ? value - 256 : value); }

function runtime(options = {}) {
  const crmName = options.hebrew ? "תצוגת CRM" : "CRM View";
  const rows = {
    Settings: [["key", "value"], ["mode", "TEST"], ["workspace_id", WORKSPACE]],
    [crmName]: options.crmRows || [["status", "message"]],
  };
  const sheetIds = { Settings: 1, [crmName]: 2 };
  let batches = 0;
  let crmRangeReads = 0;
  function lastRow(name) {
    for (let index = rows[name].length - 1; index >= 0; index -= 1) {
      if (rows[name][index].some((value) => value !== "" && value !== null && value !== undefined)) return index + 1;
    }
    return 0;
  }
  function lastColumn(name) {
    return rows[name].reduce((maximum, row) => {
      for (let index = row.length - 1; index >= 0; index -= 1) if (row[index] !== "" && row[index] !== null && row[index] !== undefined) return Math.max(maximum, index + 1);
      return maximum;
    }, 0);
  }
  function sheet(name) {
    return {
      getDataRange: () => ({ getValues: () => rows[name] }),
      getLastRow: () => lastRow(name),
      getLastColumn: () => lastColumn(name),
      getRange: (row, column, rowCount, columnCount) => {
        if (name === crmName) crmRangeReads += 1;
        return { getValues: () => rows[name].slice(row - 1, row - 1 + rowCount).map((values) => values.slice(column - 1, column - 1 + columnCount)) };
      },
      getSheetId: () => sheetIds[name],
    };
  }
  const spreadsheet = {
    getName: () => "Assaf Business Ledger TEST",
    getSheetByName: (name) => Object.prototype.hasOwnProperty.call(rows, name) ? sheet(name) : null,
  };
  function apply(body) {
    assert.equal(body.requests.length, 1, "projection and provenance must share one batch request");
    const update = body.requests[0].updateCells;
    assert.equal(update.range.sheetId, 2);
    update.rows.forEach((row, offset) => {
      const index = update.range.startRowIndex + offset;
      while (rows[crmName].length <= index) rows[crmName].push([]);
      rows[crmName][index] = row.values.map((cell) => cell.userEnteredValue.stringValue);
    });
  }
  const RealDate = Date;
  class RuntimeDate extends RealDate { static now() { return options.nowMs === undefined ? RealDate.now() : options.nowMs; } }
  const context = vm.createContext({
    console,
    Date: RuntimeDate,
    SpreadsheetApp: { openById: () => spreadsheet },
    Sheets: { Spreadsheets: { batchUpdate: (body) => { batches += 1; apply(body); } } },
    Utilities: {
      newBlob: (value) => ({ getBytes: () => [...Buffer.from(value)] }),
      computeDigest: (_algorithm, value) => signedBytes(crypto.createHash("sha256").update(value).digest()),
      computeHmacSha256Signature: (value, secret) => signedBytes(crypto.createHmac("sha256", secret).update(value).digest()),
      DigestAlgorithm: { SHA_256: "SHA_256" }, Charset: { UTF_8: "UTF_8" },
    },
  });
  vm.runInContext(fs.readFileSync("ops/business-ledger/core.js", "utf8"), context, { filename: "core.js" });
  vm.runInContext(fs.readFileSync("ops/business-ledger/Code.gs", "utf8"), context, { filename: "Code.gs" });
  vm.runInContext(fs.readFileSync("ops/business-ledger/crm-projection.gs", "utf8"), context, { filename: "crm-projection.gs" });
  return { context, rows, crmName, batches: () => batches, crmRangeReads: () => crmRangeReads, config: { workbookId: "test-workbook", workspaceId: WORKSPACE, owners: [OWNER], secret: "x".repeat(32) } };
}

function payload(context, changes = {}) {
  const observed = new Date().toISOString();
  const source = { api_version: "owner-os.v1", source: "mia_postgresql", snapshot_id: "snap_synthetic_1", observed_at: observed, coverage: "complete", coverage_scope: "stored_pg_contacts" };
  const contacts = [{ contact_id: "crm_contact_1", revision: 3, name: "דנה", source_label: "AssafWeb form / home", next_action_state: "recorded" }];
  const base = {
    api_version: "owner-os.crm-projection.v1", mode: "TEST", synthetic: true, workspace_id: WORKSPACE,
    projection_status: "known", reason_code: null, expected_snapshot_hash: null,
    expected_projection_revision: 0, projection_revision: 1, source, contacts,
  };
  Object.assign(base, changes);
  const identity = { source: base.source, contacts: base.contacts, projection_status: base.projection_status, reason_code: base.reason_code };
  base.snapshot_hash = context.sha256Hex_(context.crmProjectionStable_(identity));
  return base;
}

test("commits bounded two-column CRM View and provenance in one atomic batch", () => {
  const r = runtime();
  const data = payload(r.context);
  const result = r.context.saveCrmProjection_(OWNER, data, r.config);
  assert.equal(result.status, "committed");
  assert.equal(r.batches(), 1);
  assert.deepEqual(r.rows["CRM View"][0], ["status", "message"]);
  assert.equal(r.rows["CRM View"][1][0], "מקור קנוני TEST");
  assert.match(r.rows["CRM View"][1][1], /CRM_PROJECTION_V1;workspace=assaf-ledger-TEST;status=known;revision=1/);
  assert.deepEqual([...r.rows["CRM View"][2]], ["יש צעד הבא מתועד", "שם: דנה | מזהה: crm_contact_1 | גרסה: 3"]);
  const rendered = JSON.stringify(r.rows["CRM View"]);
  for (const forbidden of ["phone", "email", "visitor", "amount_minor", "AssafWeb form / home"]) assert.ok(!rendered.includes(forbidden));
});

test("allows only the exact installed Hebrew TEST placeholder as revision zero", () => {
  const installed = runtime({ hebrew: true, crmRows: [["סטטוס", "הודעה"], ["unavailable", "טרם חובר למקור CRM המאומת"]] });
  assert.equal(installed.context.saveCrmProjection_(OWNER, payload(installed.context), installed.config).status, "committed");
  assert.equal(installed.batches(), 1);
  assert.equal(installed.rows[installed.crmName][1][0], "מקור קנוני TEST");

  const changed = runtime({ hebrew: true, crmRows: [["סטטוס", "הודעה"], ["unavailable", "טקסט אחר"]] });
  assert.throws(() => changed.context.saveCrmProjection_(OWNER, payload(changed.context), changed.config), /crm_projection_state_unrecognized/);
  assert.equal(changed.batches(), 0);

  const nonzero = runtime({ hebrew: true, crmRows: [["סטטוס", "הודעה"], ["unavailable", "טרם חובר למקור CRM המאומת"]] });
  const mismatched = payload(nonzero.context, { expected_snapshot_hash: "a".repeat(64), expected_projection_revision: 1, projection_revision: 2 });
  assert.throws(() => nonzero.context.saveCrmProjection_(OWNER, mismatched, nonzero.config), /crm_projection_revision_mismatch/);
  assert.equal(nonzero.batches(), 0);
});

test("accepts the Python builder fixture normalized from Owner OS microseconds", () => {
  const observed = "2026-10-12T08:00:00.123Z";
  const r = runtime({ nowMs: Date.parse(observed) });
  const source = { api_version: "owner-os.v1", source: "mia_postgresql", snapshot_id: "snap_synthetic_1", observed_at: observed, coverage: "complete", coverage_scope: "stored_pg_contacts" };
  const contacts = [{ contact_id: "crm_contact_1", revision: 3, name: "דנה", source_label: "AssafWeb form / home", next_action_state: "recorded" }];
  const data = payload(r.context, { source, contacts });
  assert.equal(data.snapshot_hash, "e00aee6fb16498b87759be5f79c70cc584dbc20cfa07fd9cbe2dc7d4e85e6513");
  assert.equal(r.context.saveCrmProjection_(OWNER, data, r.config).status, "committed");
});

test("an exact accepted payload replay is write-free and unknown cannot replace known", () => {
  const r = runtime();
  const known = payload(r.context);
  r.context.saveCrmProjection_(OWNER, known, r.config);
  assert.equal(r.context.saveCrmProjection_(OWNER, known, r.config).status, "replayed");
  assert.equal(r.batches(), 1);

  const unknownSource = { ...known.source, snapshot_id: null, coverage: "unknown", observed_at: new Date(Date.now() + 1000).toISOString() };
  const unknown = payload(r.context, {
    projection_status: "unknown", reason_code: "source_unavailable", contacts: [], source: unknownSource,
    expected_snapshot_hash: known.snapshot_hash, expected_projection_revision: 1, projection_revision: 2,
  });
  assert.throws(() => r.context.saveCrmProjection_(OWNER, unknown, r.config), /unknown_cannot_replace_known_projection/);
  assert.equal(r.batches(), 1);
});

test("rendered body drift fails closed before replay or a newer update", () => {
  const r = runtime();
  const known = payload(r.context);
  r.context.saveCrmProjection_(OWNER, known, r.config);
  r.rows["CRM View"][2][1] = "שורה ששונתה ידנית";
  assert.throws(() => r.context.saveCrmProjection_(OWNER, known, r.config), /crm_projection_body_tampered/);
  const newer = payload(r.context, {
    expected_snapshot_hash: known.snapshot_hash,
    expected_projection_revision: 1,
    projection_revision: 2,
    source: { ...known.source, snapshot_id: "snap_synthetic_2", observed_at: new Date(Date.now() + 1000).toISOString() },
    contacts: [{ ...known.contacts[0], revision: 4 }],
  });
  assert.throws(() => r.context.saveCrmProjection_(OWNER, newer, r.config), /crm_projection_body_tampered/);
  assert.equal(r.batches(), 1);
  assert.equal(r.rows["CRM View"][2][1], "שורה ששונתה ידנית");
});

test("accepted provenance mutations fail closed before exact replay", () => {
  const cases = [
    [/scope=stored_pg_contacts/, "scope=invented_scope"],
    [/coverage=complete/, "coverage=invented_coverage"],
    [/snapshot=snap_synthetic_1/, "snapshot=invalid snapshot"],
    [/observed=[^;]+/, "observed=2000-01-01T00:00:00.000Z"],
  ];
  cases.forEach(([pattern, replacement]) => {
    const r = runtime();
    const known = payload(r.context);
    r.context.saveCrmProjection_(OWNER, known, r.config);
    r.rows["CRM View"][1][1] = r.rows["CRM View"][1][1].replace(pattern, replacement);
    assert.throws(() => r.context.saveCrmProjection_(OWNER, known, r.config), /crm_projection_(state_unrecognized|body_tampered)/);
    assert.equal(r.batches(), 1);
  });
});

test("rejects oversized CRM View before any bounded range read", () => {
  const thousandRows = [["status", "message"], ...Array.from({ length: 1000 }, (_, index) => ["existing", `row-${index}`])];
  const tooTall = runtime({ crmRows: thousandRows });
  assert.throws(() => tooTall.context.saveCrmProjection_(OWNER, payload(tooTall.context), tooTall.config), /crm_projection_view_capacity_exceeded/);
  assert.equal(tooTall.crmRangeReads(), 0);
  assert.equal(tooTall.batches(), 0);

  const tooWide = runtime({ crmRows: [["status", "message", "unexpected"]] });
  assert.throws(() => tooWide.context.saveCrmProjection_(OWNER, payload(tooWide.context), tooWide.config), /crm_projection_view_capacity_exceeded/);
  assert.equal(tooWide.crmRangeReads(), 0);
  assert.equal(tooWide.batches(), 0);
});

test("rejects stale, forged, over-capacity and incorrectly bound payloads before writing", () => {
  const r = runtime();
  const stale = payload(r.context, { source: { api_version: "owner-os.v1", source: "mia_postgresql", snapshot_id: "snap_old", observed_at: new Date(Date.now() - 301000).toISOString(), coverage: "complete", coverage_scope: "stored_pg_contacts" } });
  assert.throws(() => r.context.saveCrmProjection_(OWNER, stale, r.config), /stale_crm_projection_source/);
  const forged = payload(r.context); forged.contacts[0].phone = "+972501234567";
  assert.throws(() => r.context.saveCrmProjection_(OWNER, forged, r.config), /invalid_crm_projection_contact/);
  const many = payload(r.context); many.contacts = Array.from({ length: 101 }, (_, i) => ({ contact_id: `crm_${i}`, revision: 1, name: null, source_label: null, next_action_state: "not_collected" }));
  many.snapshot_hash = r.context.sha256Hex_(r.context.crmProjectionStable_({ source: many.source, contacts: many.contacts, projection_status: many.projection_status, reason_code: many.reason_code }));
  assert.throws(() => r.context.saveCrmProjection_(OWNER, many, r.config), /crm_projection_contact_limit/);
  assert.throws(() => r.context.saveCrmProjection_("999", payload(r.context), r.config), /unauthorized_owner/);
  assert.throws(() => r.context.saveCrmProjection_(OWNER, payload(r.context, { workspace_id: "other" }), r.config), /invalid_workspace/);
  assert.equal(r.batches(), 0);
});

test("unknown is explicit on an empty view and does not invent an empty success", () => {
  const r = runtime();
  const source = { api_version: "owner-os.v1", source: "mia_postgresql", snapshot_id: null, observed_at: new Date().toISOString(), coverage: "unknown", coverage_scope: "stored_pg_contacts" };
  const unknown = payload(r.context, { projection_status: "unknown", reason_code: "source_coverage_incomplete", contacts: [], source });
  r.context.saveCrmProjection_(OWNER, unknown, r.config);
  assert.equal(r.rows["CRM View"][1][0], "מצב CRM לא ידוע");
  assert.deepEqual([...r.rows["CRM View"][2]], ["לא ידוע", "כיסוי מקור ה-CRM אינו מלא ולכן לא פורסמו רשומות."]);
  assert.ok(!JSON.stringify(r.rows["CRM View"]).includes('"0"'));
});

test("known snapshot id unknown commits, signed receipt-recovers, and exact-replays once", () => {
  const fixture = require("./runtime-fixture.cjs");
  const r = fixture.runtime({ applyBatch: true });
  const source = { api_version: "owner-os.v1", source: "mia_postgresql", snapshot_id: "unknown", observed_at: new Date().toISOString(), coverage: "complete", coverage_scope: "stored_pg_contacts" };
  const contacts = [{ contact_id: "synthetic_contact", revision: 1, name: "פונה סינתטי", source_label: "AssafWeb form", next_action_state: "recorded" }];
  const data = {
    api_version: "owner-os.crm-projection.v1", mode: "TEST", synthetic: true, workspace_id: fixture.WORKSPACE,
    projection_status: "known", reason_code: null, expected_snapshot_hash: null, expected_projection_revision: 0,
    projection_revision: 1, source, contacts,
  };
  data.snapshot_hash = r.context.sha256Hex_(r.context.crmProjectionStable_({ source, contacts, projection_status: "known", reason_code: null }));
  const post = (operation, body) => fixture.inner(r.context.doPost(fixture.event(operation, body)));
  assert.equal(post("save_crm_projection", data).result.status, "committed");
  assert.deepEqual(post("crm_projection_receipt", { snapshot_hash: data.snapshot_hash, projection_revision: 1 }).result, { status: "committed", snapshot_hash: data.snapshot_hash, projection_revision: 1 });
  assert.equal(post("save_crm_projection", data).result.status, "replayed");
  assert.equal(r.batchCalls(), 1);
});
