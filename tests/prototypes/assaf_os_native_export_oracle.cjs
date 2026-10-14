"use strict";

// Pure local parity oracle. It evaluates the checked-in Apps Script runtime in
// the existing VM harness; it never contacts Sheets or any other provider.
const fs = require("node:fs");
const path = require("node:path");

const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 16 * 1024 * 1024;
const MAX_TOTAL_ROWS = 2000;
const MAX_JSON_DEPTH = 20;
const MAX_JSON_NODES = 200000;
const MAX_CELL_TEXT = 4000;

const repoRoot = path.resolve(__dirname, "../..");
process.chdir(repoRoot);
const { createHarness } = require(path.join(
  repoRoot,
  "tests/assaf-os/apps-script-harness.js",
));

function fail(code) {
  process.stderr.write(JSON.stringify({ error: code }) + "\n");
  process.exitCode = 1;
}

function plain(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value, keys, code) {
  const actual = plain(value) ? Object.keys(value).sort() : [];
  const expected = keys.slice().sort();
  if (
    !plain(value)
    || actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])
  ) {
    throw Object.assign(new Error(code), { code });
  }
  return value;
}

function assertJsonBounds(root) {
  const pending = [[root, 0]];
  let nodes = 0;
  while (pending.length) {
    const [value, depth] = pending.pop();
    nodes += 1;
    if (nodes > MAX_JSON_NODES || depth > MAX_JSON_DEPTH) {
      throw Object.assign(new Error("input_json_too_complex"), {
        code: "input_json_too_complex",
      });
    }
    if (value === null || typeof value === "boolean") continue;
    if (typeof value === "string") {
      if (value.length > MAX_CELL_TEXT || value.includes("\0")) {
        throw Object.assign(new Error("input_string_invalid"), {
          code: "input_string_invalid",
        });
      }
      continue;
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value)) {
        throw Object.assign(new Error("input_number_invalid"), {
          code: "input_number_invalid",
        });
      }
      continue;
    }
    if (Array.isArray(value)) {
      for (const item of value) pending.push([item, depth + 1]);
      continue;
    }
    if (plain(value)) {
      for (const [key, item] of Object.entries(value)) {
        if (key.length > 128 || key.includes("\0")) {
          throw Object.assign(new Error("input_key_invalid"), {
            code: "input_key_invalid",
          });
        }
        pending.push([item, depth + 1]);
      }
      continue;
    }
    throw Object.assign(new Error("input_json_invalid"), { code: "input_json_invalid" });
  }
}

function deepFreeze(root) {
  const pending = [root];
  while (pending.length) {
    const value = pending.pop();
    if (!value || typeof value !== "object" || Object.isFrozen(value)) continue;
    Object.freeze(value);
    for (const item of Object.values(value)) pending.push(item);
  }
  return root;
}

function validateInput(input, harness) {
  const topKeys = Object.prototype.hasOwnProperty.call(input, "display_refs")
    ? ["synthetic_only", "records", "native_matrices", "display_refs"]
    : ["synthetic_only", "records", "native_matrices"];
  exactKeys(input, topKeys, "invalid_input_shape");
  if (input.synthetic_only !== true) {
    throw Object.assign(new Error("synthetic_fixture_required"), {
      code: "synthetic_fixture_required",
    });
  }

  const entities = harness.Schema.tabs
    .filter((tab) => tab.kind === "input" && tab.key !== "settings")
    .map((tab) => tab.key);
  exactKeys(input.records, entities, "invalid_fixture_records");
  if (!plain(input.native_matrices)) {
    throw Object.assign(new Error("invalid_native_matrices"), {
      code: "invalid_native_matrices",
    });
  }
  const suppliedMatrixEntities = Object.keys(input.native_matrices);
  if (
    suppliedMatrixEntities.length < 1
    || suppliedMatrixEntities.some((entity) => !entities.includes(entity))
  ) {
    throw Object.assign(new Error("invalid_native_matrices"), {
      code: "invalid_native_matrices",
    });
  }
  const matrixEntities = entities.filter((entity) => Object.prototype.hasOwnProperty.call(
    input.native_matrices,
    entity,
  ));

  let totalRows = 0;
  for (const entity of entities) {
    const tab = harness.Schema.tab(entity);
    const records = input.records[entity];
    if (!Array.isArray(records)) {
      throw Object.assign(new Error("invalid_fixture_records"), {
        code: "invalid_fixture_records",
      });
    }
    totalRows += records.length;
    const allowed = new Set(tab.fields.map((field) => field.key));
    for (const record of records) {
      if (!plain(record) || Object.keys(record).some((key) => !allowed.has(key))) {
        throw Object.assign(new Error("invalid_fixture_record"), {
          code: "invalid_fixture_record",
        });
      }
    }
  }

  for (const entity of matrixEntities) {
    const tab = harness.Schema.tab(entity);
    const matrix = exactKeys(
      input.native_matrices[entity],
      ["headers", "rows"],
      "invalid_native_matrix",
    );
    const expectedHeaders = tab.fields.map((field) => field.label);
    if (
      !Array.isArray(matrix.headers)
      || JSON.stringify(matrix.headers) !== JSON.stringify(expectedHeaders)
      || !Array.isArray(matrix.rows)
    ) {
      throw Object.assign(new Error("native_header_drift"), {
        code: "native_header_drift",
      });
    }
    totalRows += matrix.rows.length;
    for (const row of matrix.rows) {
      if (
        !Array.isArray(row)
        || row.length !== tab.fields.length
        || row.some(
          (cell) => cell !== null && !["string", "number", "boolean"].includes(typeof cell),
        )
      ) {
        throw Object.assign(new Error("invalid_native_row"), {
          code: "invalid_native_row",
        });
      }
    }
  }
  if (totalRows > MAX_TOTAL_ROWS) {
    throw Object.assign(new Error("row_budget_exceeded"), {
      code: "row_budget_exceeded",
    });
  }

  const displayRefs = input.display_refs === undefined ? [] : input.display_refs;
  if (!Array.isArray(displayRefs) || displayRefs.length > MAX_TOTAL_ROWS) {
    throw Object.assign(new Error("invalid_display_refs"), {
      code: "invalid_display_refs",
    });
  }
  for (const item of displayRefs) {
    exactKeys(item, ["entity", "row_index", "field"], "invalid_display_ref");
    const matrix = input.native_matrices[item.entity];
    const tab = entities.includes(item.entity) ? harness.Schema.tab(item.entity) : null;
    const field = tab && tab.fields.find((candidate) => candidate.key === item.field);
    if (
      !matrix
      || !Number.isSafeInteger(item.row_index)
      || item.row_index < 0
      || item.row_index >= matrix.rows.length
      || !field
      || !field.ref
    ) {
      throw Object.assign(new Error("invalid_display_ref"), {
        code: "invalid_display_ref",
      });
    }
  }
  return { entities, matrixEntities, displayRefs };
}

function evaluate(input) {
  assertJsonBounds(input);
  const harness = createHarness();
  const plan = validateInput(input, harness);
  deepFreeze(input);

  for (const entity of plan.entities) {
    for (const record of input.records[entity]) harness.addRecord(entity, record);
  }

  const matrices = {};
  for (const entity of plan.matrixEntities) {
    matrices[entity] = input.native_matrices[entity].rows.map((row) => row.slice());
  }
  for (const item of plan.displayRefs) {
    const tab = harness.Schema.tab(item.entity);
    const fieldIndex = tab.fields.findIndex((field) => field.key === item.field);
    const field = tab.fields[fieldIndex];
    const exactId = matrices[item.entity][item.row_index][fieldIndex];
    if (typeof exactId !== "string" || exactId.length === 0) {
      throw Object.assign(new Error("display_ref_id_required"), {
        code: "display_ref_id_required",
      });
    }
    matrices[item.entity][item.row_index][fieldIndex]
      = harness.context.assafOsDisplayReference_(harness.workbook, field, exactId);
  }

  const entities = {};
  for (const entity of plan.matrixEntities) {
    const tab = harness.Schema.tab(entity);
    const idKey = harness.context.assafOsIdKey_(entity);
    const rows = matrices[entity].map((row, index) => {
      const record = harness.context.assafOsRowToRecord_(tab, row, harness.workbook);
      const nativeInput = {};
      harness.context.inputFieldsFor_(tab).forEach((field) => {
        const fieldIndex = tab.fields.indexOf(field);
        let value = harness.context.assafOsNativeValue_(field, row[fieldIndex]);
        if (value === "") value = null;
        if (value !== null && field.ref) {
          value = harness.context.assafOsResolveReferenceValue_(
            harness.workbook,
            field,
            value,
          );
        }
        nativeInput[field.key] = value;
      });
      const converted = harness.Runtime.nativeToInput(entity, nativeInput);
      const canonicalInput = harness.Runtime.canonicalInput(entity, converted, true);
      const rawOwnedValues = tab.fields.map((field, fieldIndex) => {
        if (field.role === "calculated") return null;
        let value = harness.context.assafOsNativeValue_(field, row[fieldIndex]);
        if (value !== null && value !== "" && field.ref) {
          value = harness.context.assafOsResolveReferenceValue_(
            harness.workbook,
            field,
            value,
          );
        }
        return value;
      });
      const rawFingerprint = harness.context.assafOsRawOwnedFingerprint_(
        tab,
        row,
        harness.workbook,
      );
      const descriptorFingerprint = harness.context.assafOsSha_(
        harness.Runtime.stableStringify(rawOwnedValues),
      );
      if (rawFingerprint !== descriptorFingerprint) {
        throw Object.assign(new Error("runtime_fingerprint_mismatch"), {
          code: "runtime_fingerprint_mismatch",
        });
      }
      return {
        row_number: index + 2,
        native_row: row,
        record_id: record[idKey] == null ? null : record[idKey],
        raw_owned_values: rawOwnedValues,
        raw_fingerprint: rawFingerprint,
        canonical_input: canonicalInput,
        input_fingerprint: harness.context.assafOsSha_(
          harness.Runtime.stableStringify(canonicalInput),
        ),
      };
    });
    entities[entity] = {
      field_keys: tab.fields.map((field) => field.key),
      headers: tab.fields.map((field) => field.label),
      rows,
    };
  }
  return {
    synthetic_only: true,
    schema_version: harness.Schema.version,
    entities,
  };
}

let inputBytes = 0;
let stopped = false;
const chunks = [];
process.stdin.on("data", (chunk) => {
  if (stopped) return;
  inputBytes += chunk.length;
  if (inputBytes > MAX_INPUT_BYTES) {
    stopped = true;
    fail("input_too_large");
    process.stdin.destroy();
    return;
  }
  chunks.push(chunk);
});
process.stdin.on("end", () => {
  if (stopped) return;
  try {
    const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const output = JSON.stringify(evaluate(input));
    if (Buffer.byteLength(output, "utf8") > MAX_OUTPUT_BYTES) {
      throw Object.assign(new Error("output_too_large"), { code: "output_too_large" });
    }
    process.stdout.write(output);
  } catch (error) {
    fail(error && (error.assafOsCode || error.code) || "oracle_failed");
  }
});
process.stdin.on("error", () => {
  if (!stopped) fail("stdin_failed");
});
