"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fixture = require("./runtime-fixture.cjs");

function projection(rt) {
  const source = {api_version:"owner-os.v1", source:"mia_postgresql", snapshot_id:"synthetic_snapshot", observed_at:new Date().toISOString(), coverage:"complete", coverage_scope:"stored_pg_contacts"};
  const contacts = [{contact_id:"synthetic_contact", revision:1, name:"פונה סינתטי", source_label:"AssafWeb form", next_action_state:"recorded"}];
  const identity = {source, contacts, projection_status:"known", reason_code:null};
  return {api_version:"owner-os.crm-projection.v1", mode:"TEST", synthetic:true, workspace_id:fixture.WORKSPACE, projection_status:"known", reason_code:null, expected_snapshot_hash:null, expected_projection_revision:0, projection_revision:1, source, contacts, snapshot_hash:rt.context.sha256Hex_(rt.context.crmProjectionStable_(identity))};
}

function post(rt, operation, data) {
  return fixture.inner(rt.context.doPost(fixture.event(operation, data)));
}

test("signed synthetic projection and receipt run under the shared script lock", () => {
  const rt = fixture.runtime({applyBatch:true}), data = projection(rt);
  const saved = post(rt, "save_crm_projection", data);
  assert.equal(saved.status, "ok");
  assert.equal(saved.result.status, "committed");
  assert.equal(rt.batchCalls(), 1);
  assert.equal(rt.released(), 1);
  const receipt = post(rt, "crm_projection_receipt", {snapshot_hash:data.snapshot_hash, projection_revision:1});
  assert.deepEqual(receipt.result, {status:"committed", snapshot_hash:data.snapshot_hash, projection_revision:1});
  assert.equal(rt.batchCalls(), 1);
  assert.equal(rt.released(), 2);
  assert.equal(post(rt, "save_crm_projection", data).result.status, "replayed");
  assert.equal(rt.batchCalls(), 1);
  assert.equal(rt.sheets.Deals.getLastRow(), 1);
  assert.equal(rt.sheets.Audit.getLastRow(), 1);
  assert.match(rt.sheets["CRM View"].getRange(3,1,1,2).getValues()[0][1], /פונה סינתטי/);
});

test("projection guard rejects signature, owner, workspace and non-synthetic input before writes", () => {
  for (const change of [data => {data.synthetic=false;}, data => {data.mode="LIVE";}, data => {data.workspace_id="other";}]) {
    const rt = fixture.runtime({applyBatch:true}), data = projection(rt);
    change(data);
    assert.equal(post(rt, "save_crm_projection", data).status, "invalid");
    assert.equal(rt.batchCalls(), 0);
  }
  const rt = fixture.runtime({applyBatch:true});
  const ownerEvent = fixture.event("save_crm_projection", projection(rt));
  const wrapper = JSON.parse(ownerEvent.postData.contents), request = JSON.parse(wrapper.payload);
  request.owner_id = "987654321";
  wrapper.payload = JSON.stringify(request); wrapper.signature = fixture.hmac(wrapper.payload);
  ownerEvent.postData.contents = JSON.stringify(wrapper);
  assert.equal(fixture.inner(rt.context.doPost(ownerEvent)).status, "invalid");
  const invalid = fixture.event("save_crm_projection", projection(rt));
  const bad = JSON.parse(invalid.postData.contents); bad.signature = "0".repeat(64);
  invalid.postData.contents = JSON.stringify(bad);
  assert.equal(fixture.inner(rt.context.doPost(invalid)).status, "invalid");
  assert.equal(rt.batchCalls(), 0);
});

test("missing or altered projection receipt cannot authorize a write or expose contact data", () => {
  const rt = fixture.runtime({applyBatch:true}), data = projection(rt);
  const key = {snapshot_hash:data.snapshot_hash, projection_revision:1};
  assert.equal(post(rt, "crm_projection_receipt", key).status, "not_found");
  post(rt, "save_crm_projection", data);
  assert.equal(post(rt, "crm_projection_receipt", {...key, snapshot_hash:"0".repeat(64)}).status, "not_found");
  const changed = rt.sheets["CRM View"].getRange(3,1,1,2).getValues();
  changed[0][1] = "altered synthetic body";
  rt.sheets["CRM View"].getRange(3,1,1,2).setValues(changed);
  const refused = post(rt, "crm_projection_receipt", key);
  assert.equal(refused.status, "invalid");
  assert.equal(rt.batchCalls(), 1);
  assert.doesNotMatch(JSON.stringify(refused), /altered synthetic body|פונה סינתטי/);
});

test("unknown projection write transport is classified as unknown without resend", () => {
  const rt = fixture.runtime({throwBatch:true}), data = projection(rt);
  const result = post(rt, "save_crm_projection", data);
  assert.equal(result.status, "unavailable");
  assert.equal(rt.batchCalls(), 1);
  assert.equal(rt.released(), 1);
});
