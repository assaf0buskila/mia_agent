"use strict";

// Independent oracle: evaluate the production Apps Script function in its VM
// harness.  It performs no workbook calls because assafOsSummary_ is pure.
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "../..");
process.chdir(repoRoot);
const { createHarness } = require(path.join(repoRoot, "tests/assaf-os/apps-script-harness.js"));

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const harness = createHarness();
const result = harness.context.assafOsSummary_(
  input.records,
  input.coverage,
  input.aggregate_coverage,
);
process.stdout.write(JSON.stringify(result));
