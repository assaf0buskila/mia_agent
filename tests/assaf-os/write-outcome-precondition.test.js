"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { createHarness } = require("./apps-script-harness.js");

const source = fs.readFileSync("ops/assaf-os/UnifiedRuntime.gs", "utf8");

test("the only doPost-reachable runtime_busy site is tryLock before openById", () => {
  // The Script uses top-level named functions. Conservative textual reachability includes
  // references passed as callbacks as well as calls, so an added helper reference fails closed.
  const starts = [...source.matchAll(/^function (\w+)\(/gm)];
  const functions = new Map(starts.map((match, i) => [match[1],
    source.slice(match.index, starts[i + 1]?.index ?? source.length)]));
  const busySites = [...functions].filter(([, body]) => body.includes('"runtime_busy"'));
  assert.deepEqual(busySites.map(([name]) => name), ["assafOsRun_", "reconcileAssafOsWorkbook"]);
  assert.equal((source.match(/"runtime_busy"/g) || []).length, 2);
  const reachable = new Set();
  function visit(name) {
    if (reachable.has(name)) return;
    reachable.add(name);
    const body = functions.get(name).replace(/^function \w+\(/, "(");
    for (const candidate of functions.keys()) {
      if (new RegExp(`\\b${candidate}\\b`).test(body)) visit(candidate);
    }
  }
  visit("doPost");
  assert.ok(reachable.has("assafOsRun_"));
  assert.equal(reachable.has("reconcileAssafOsWorkbook"), false);
  const run = functions.get("assafOsRun_");
  assert.match(run, /if \(!lock\.tryLock\(5000\)\) throw assafOsError_\("runtime_busy"\);/);
  assert.ok(run.indexOf('"runtime_busy"') < run.indexOf("SpreadsheetApp.openById"));
  // Pin the other throw's different ordering; it is not a doPost non-start proof.
  const reconcile = functions.get("reconcileAssafOsWorkbook");
  assert.ok(reconcile.indexOf("SpreadsheetApp.openById") < reconcile.indexOf('"runtime_busy"'));
});

for (const operation of ["propose", "approve", "reject"]) {
  test(`${operation}: doPost signs busy with the exact request identity before opening a workbook`, () => {
    const h = createHarness();
    let opened = 0, locks = 0;
    h.context.SpreadsheetApp.openById = () => { opened += 1; throw new Error("must_not_open"); };
    h.context.LockService.getScriptLock = () => ({
      tryLock: () => { locks += 1; return false; },
      releaseLock: () => assert.fail("an unacquired lock must not be released"),
    });
    h.context.reconcileAssafOsWorkbook = () => assert.fail("not reachable from doPost");
    const request = h.request(operation, {});
    const response = h.call(request); // harness verifies the response signature
    assert.equal(response.ok, false);
    assert.equal(response.data.error.code, "runtime_busy");
    for (const key of Object.keys(request).filter((key) => key !== "data")) {
      assert.deepEqual(response[key], request[key], key);
    }
    assert.equal(opened, 0);
    assert.equal(locks, 1);
    assert.equal(h.batches.length, 0);
  });
}

test("runtime_busy after openById belongs to direct reconciliation, not a signed doPost response", () => {
  const h = createHarness();
  const order = [];
  const open = h.context.SpreadsheetApp.openById;
  h.context.SpreadsheetApp.openById = (id) => { order.push("open"); return open(id); };
  h.context.LockService.getScriptLock = () => ({
    tryLock: () => { order.push("lock"); return false; },
  });
  assert.throws(() => h.context.reconcileAssafOsWorkbook(),
    (error) => error.assafOsCode === "runtime_busy");
  assert.deepEqual(order, ["open", "lock"]);
  assert.equal(h.batches.length, 0);
});
