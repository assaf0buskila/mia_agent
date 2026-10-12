/* global AssafOsSchema, SpreadsheetApp, LockService, Session */
"use strict";

var ASSAF_OS_INSTALL_ROWS = 1000;

/**
 * Safe default entrypoint. It never mutates unless applyChanges is exactly true.
 * The workbook id and schema confirmation are always explicit; there is no active-sheet fallback.
 */
function setupUnifiedWorkbook(workbookId, schemaConfirmation, applyChanges) {
  if (applyChanges === true) return installUnifiedWorkbook(workbookId, schemaConfirmation);
  return preflightUnifiedWorkbook(workbookId, schemaConfirmation);
}

function preflightUnifiedWorkbook(workbookId, schemaConfirmation) {
  assertInstallerArguments_(workbookId, schemaConfirmation);
  var workbook = SpreadsheetApp.openById(workbookId);
  var existing = workbook.getSheets().map(function (sheet) {
    return { name: sheet.getName(), lastRow: sheet.getLastRow(), lastColumn: sheet.getLastColumn() };
  });
  var desired = AssafOsSchema.tabs.map(function (tab) { return tab.name; });
  var conflicts = existing.filter(function (sheet) { return desired.indexOf(sheet.name) !== -1; });
  return {
    mode: "DRY_RUN",
    workbook_id: workbookId,
    schema_version: AssafOsSchema.version,
    ready: conflicts.length === 0,
    existing_sheets: existing,
    sheets_to_create: desired,
    conflicts: conflicts,
    warnings: existing.some(function (sheet) { return sheet.lastRow > 0 || sheet.lastColumn > 0; })
      ? ["Existing unrelated sheets remain untouched; verify this is the intended dedicated workbook."] : [],
  };
}

function installUnifiedWorkbook(workbookId, schemaConfirmation) {
  assertInstallerArguments_(workbookId, schemaConfirmation);
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) throw installerError_("installer_busy");
  try {
    var check = preflightUnifiedWorkbook(workbookId, schemaConfirmation);
    if (!check.ready) throw installerError_("target_tab_already_exists");
    var workbook = SpreadsheetApp.openById(workbookId);
    var created = [];
    AssafOsSchema.tabs.forEach(function (tab) {
      // A second lookup immediately before creation closes the operator/race window.
      if (workbook.getSheetByName(tab.name)) throw installerError_("target_tab_already_exists");
      var sheet = workbook.insertSheet(tab.name, workbook.getNumSheets());
      configureSheet_(sheet, tab);
      created.push(tab.name);
    });
    workbook.setActiveSheet(workbook.getSheetByName("סיכום"));
    workbook.moveActiveSheet(1);
    return { mode: "INSTALLED", workbook_id: workbookId, schema_version: AssafOsSchema.version, created_sheets: created, untouched_existing_sheets: check.existing_sheets.map(function (sheet) { return sheet.name; }) };
  } finally {
    lock.releaseLock();
  }
}

function configureSheet_(sheet, tab) {
  var columnCount = tab.fields.length;
  if (sheet.getMaxColumns() < columnCount) sheet.insertColumnsAfter(sheet.getMaxColumns(), columnCount - sheet.getMaxColumns());
  if (sheet.getMaxRows() < ASSAF_OS_INSTALL_ROWS) sheet.insertRowsAfter(sheet.getMaxRows(), ASSAF_OS_INSTALL_ROWS - sheet.getMaxRows());
  sheet.setRightToLeft(true);
  sheet.setFrozenRows(tab.frozenRows || 1);
  sheet.getRange(1, 1, 1, columnCount).setValues([tab.fields.map(function (field) { return field.label; })])
    .setFontWeight("bold").setBackground("#17324d").setFontColor("#ffffff").setHorizontalAlignment("right");
  protectRange_(sheet.getRange(1, 1, 1, columnCount), "Assaf OS canonical headers");
  sheet.setRowHeight(1, 34);
  tab.fields.forEach(function (field, index) {
    var column = index + 1;
    var range = sheet.getRange(2, column, ASSAF_OS_INSTALL_ROWS - 1, 1);
    applyFormat_(range, field.type);
    if (field.role === "input") applyValidation_(range, field, column);
    if (field.role === "system" || field.role === "calculated") {
      protectRange_(range, "Assaf OS " + field.role + ": " + field.key);
      range.setBackground(field.role === "calculated" ? "#eef5fb" : "#f3f4f6");
    }
  });
  if (tab.kind === "calculated") protectRange_(sheet.getRange(1, 1, ASSAF_OS_INSTALL_ROWS, columnCount), "Assaf OS calculated sheet");
  sheet.autoResizeColumns(1, columnCount);
}

function applyFormat_(range, type) {
  // Currency is an explicit adjacent field. A fixed symbol here would mislabel USD/EUR/GBP.
  if (type === "id" || type === "text") range.setNumberFormat("@");
  else if (type === "money_ils") range.setNumberFormat("#,##0.00;[Red]-#,##0.00");
  else if (type === "date") range.setNumberFormat("dd/mm/yyyy");
  else if (type === "month") range.setNumberFormat("yyyy-mm");
  else if (type === "datetime") range.setNumberFormat("dd/mm/yyyy hh:mm:ss");
  else if (type === "integer") range.setNumberFormat("0");
  else if (type === "decimal") range.setNumberFormat("0.00");
}

function applyValidation_(range, field, column) {
  var values = field.values || (field.enum ? AssafOsSchema.enums[field.enum] : null);
  if (values && values.length) {
    range.setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(values.slice(), true).setAllowInvalid(false).build());
  } else if (field.type === "date") {
    range.setDataValidation(SpreadsheetApp.newDataValidation().requireDate().setAllowInvalid(false).build());
  } else if (field.type === "integer") {
    var cell = columnLetter_(column) + "2";
    range.setDataValidation(SpreadsheetApp.newDataValidation().requireFormulaSatisfied("=AND(ISNUMBER(" + cell + ")," + cell + ">=" + (field.minimum || 0) + ",INT(" + cell + ")=" + cell + ")").setAllowInvalid(false).build());
  } else if (field.type === "money_ils") {
    range.setDataValidation(SpreadsheetApp.newDataValidation().requireNumberGreaterThanOrEqualTo(0).setAllowInvalid(false).build());
  }
}

function columnLetter_(column) {
  var out = "";
  while (column > 0) { var remainder = (column - 1) % 26; out = String.fromCharCode(65 + remainder) + out; column = Math.floor((column - 1) / 26); }
  return out;
}

function protectRange_(range, description) {
  var protection = range.protect().setDescription(description).setWarningOnly(false);
  var operator = Session.getEffectiveUser();
  protection.addEditor(operator);
  var operatorEmail = operator.getEmail();
  var otherEditors = protection.getEditors().filter(function (editor) { return editor.getEmail() !== operatorEmail; });
  if (otherEditors.length) protection.removeEditors(otherEditors);
  if (protection.canDomainEdit()) protection.setDomainEdit(false);
  return protection;
}

function assertInstallerArguments_(workbookId, schemaConfirmation) {
  if (typeof workbookId !== "string" || !/^[A-Za-z0-9_-]{20,}$/.test(workbookId)) throw installerError_("invalid_workbook_id");
  if (schemaConfirmation !== AssafOsSchema.installConfirmation) throw installerError_("schema_confirmation_required");
}

function installerError_(code) { var error = new Error(code); error.code = code; return error; }

