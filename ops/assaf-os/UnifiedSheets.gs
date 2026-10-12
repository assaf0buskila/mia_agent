/* global ASSAF_OS_META, AssafOsSchema, SpreadsheetApp, LockService, Session, ScriptApp, PropertiesService */
"use strict";

var ASSAF_OS_INSTALL_ROWS = 2001;

/**
 * Safe default entrypoint. It never mutates unless applyChanges is exactly true.
 * The workbook id and schema confirmation are always explicit; there is no active-sheet fallback.
 */
function setupUnifiedWorkbook(workbookId, schemaConfirmation, applyChanges) {
  if (applyChanges === true) return installUnifiedWorkbook(workbookId, schemaConfirmation);
  return preflightUnifiedWorkbook(workbookId, schemaConfirmation);
}

function preflightConfiguredUnifiedWorkbook() {
  var props = PropertiesService.getScriptProperties();
  return preflightUnifiedWorkbook(props.getProperty("ASSAF_OS_WORKBOOK_ID"), AssafOsSchema.installConfirmation);
}

function installConfiguredUnifiedWorkbook() {
  var props = PropertiesService.getScriptProperties();
  return installUnifiedWorkbook(props.getProperty("ASSAF_OS_WORKBOOK_ID"), props.getProperty("ASSAF_OS_INSTALL_CONFIRMATION"));
}

function configureAssafOsBinding(workbookId, workspaceId, ownerId, mode) {
  assertInstallerArguments_(workbookId, AssafOsSchema.installConfirmation);
  if (typeof workspaceId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(workspaceId)) throw installerError_("invalid_workspace_id");
  if (typeof ownerId !== "string" || !/^[1-9][0-9]{0,19}$/.test(ownerId)) throw installerError_("invalid_owner_id");
  if (mode !== "TEST" && mode !== "LIVE") throw installerError_("invalid_mode");
  PropertiesService.getScriptProperties().setProperties({ ASSAF_OS_WORKBOOK_ID: workbookId, ASSAF_OS_WORKSPACE_ID: workspaceId, ASSAF_OS_OWNER_ID: ownerId, ASSAF_OS_MODE: mode }, false);
  return { workbook_id: workbookId, workspace_id: workspaceId, owner_id: ownerId, mode: mode, hmac_secret_configured: Boolean(PropertiesService.getScriptProperties().getProperty("ASSAF_OS_HMAC_SECRET")) };
}

function configureAssafOsBindingFromUserProperties() {
  var props = PropertiesService.getUserProperties();
  return configureAssafOsBinding(props.getProperty("ASSAF_OS_SETUP_WORKBOOK_ID"), props.getProperty("ASSAF_OS_SETUP_WORKSPACE_ID"), props.getProperty("ASSAF_OS_SETUP_OWNER_ID"), props.getProperty("ASSAF_OS_SETUP_MODE"));
}

function preflightUnifiedWorkbook(workbookId, schemaConfirmation) {
  assertInstallerArguments_(workbookId, schemaConfirmation);
  var workbook = SpreadsheetApp.openById(workbookId);
  var existing = workbook.getSheets().map(function (sheet) {
    return { name: sheet.getName(), lastRow: sheet.getLastRow(), lastColumn: sheet.getLastColumn() };
  });
  var desired = AssafOsSchema.tabs.map(function (tab) { return tab.name; });
  var metadataNames = typeof ASSAF_OS_META === "object" ? Object.keys(ASSAF_OS_META).map(function (key) { return ASSAF_OS_META[key].name; }) : [];
  var settings = typeof workbook.getSheetByName === "function" ? workbook.getSheetByName("הגדרות") : null, compatibleSettings = false;
  if (settings) {
    var settingsHeaders = settings.getRange(1, 1, 1, Math.max(settings.getLastColumn(), 2)).getValues()[0];
    compatibleSettings = settingsHeaders[0] === "מפתח" && settingsHeaders[1] === "ערך" && settingsHeaders.slice(2).every(function (value, i) { return value === "" || value === AssafOsSchema.headers("settings")[i + 2]; });
  }
  var conflicts = existing.filter(function (sheet) { return metadataNames.indexOf(sheet.name) !== -1 || (desired.indexOf(sheet.name) !== -1 && !(sheet.name === "הגדרות" && compatibleSettings)); });
  var recovery = conflicts.map(function (conflict) {
    var sheet = workbook.getSheetByName(conflict.name), tab = AssafOsSchema.tabs.filter(function (item) { return item.name === conflict.name; })[0];
    var metaKey = Object.keys(ASSAF_OS_META).filter(function (key) { return ASSAF_OS_META[key].name === conflict.name; })[0];
    var expected = tab ? tab.fields.map(function (field) { return field.label; }) : (metaKey ? ASSAF_OS_META[metaKey].headers : []);
    var actual = expected.length ? sheet.getRange(1, 1, 1, expected.length).getValues()[0] : [];
    var exactHeaders = expected.length > 0 && sheet.getLastColumn() === expected.length && expected.every(function (value, index) { return actual[index] === value; });
    return { name: conflict.name, exact_headers: exactHeaders, data_rows: Math.max(sheet.getLastRow() - 1, 0), recoverable_empty_created_tab: exactHeaders && sheet.getLastRow() <= 1 };
  });
  return {
    mode: "DRY_RUN",
    workbook_id: workbookId,
    schema_version: AssafOsSchema.version,
    ready: conflicts.length === 0,
    existing_sheets: existing,
    sheets_to_create: desired.concat(metadataNames),
    conflicts: conflicts,
    compatible_legacy_settings: compatibleSettings,
    partial_installation: conflicts.length > 0 && recovery.some(function (item) { return item.recoverable_empty_created_tab; }),
    partial_installation_diagnostics: recovery,
    recovery_instruction: recovery.some(function (item) { return item.recoverable_empty_created_tab; }) ? "Inspect the listed exact-schema empty tabs. Remove only explicitly confirmed installer-created empty tabs, then rerun preflight. This function never deletes or overwrites them." : null,
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
      if (tab.key === "settings" && check.compatible_legacy_settings) {
        var legacy = workbook.getSheetByName(tab.name);
        if (legacy.getMaxColumns() < tab.fields.length) legacy.insertColumnsAfter(legacy.getMaxColumns(), tab.fields.length - legacy.getMaxColumns());
        legacy.getRange(1, 3, 1, tab.fields.length - 2).setValues([tab.fields.slice(2).map(function (field) { return field.label; })]);
        legacy.setRightToLeft(true); legacy.setFrozenRows(1);
        created.push(tab.name + ":extended");
        return;
      }
      // A second lookup immediately before creation closes the operator/race window.
      if (workbook.getSheetByName(tab.name)) throw installerError_("target_tab_already_exists");
      var sheet = workbook.insertSheet(tab.name, workbook.getNumSheets());
      configureSheet_(sheet, tab);
      created.push(tab.name);
    });
    Object.keys(ASSAF_OS_META).forEach(function (key) {
      var spec = ASSAF_OS_META[key];
      if (workbook.getSheetByName(spec.name)) throw installerError_("metadata_tab_already_exists");
      var sheet = workbook.insertSheet(spec.name, workbook.getNumSheets());
      if (sheet.getMaxColumns() < spec.headers.length) sheet.insertColumnsAfter(sheet.getMaxColumns(), spec.headers.length - sheet.getMaxColumns());
      if (sheet.getMaxRows() < ASSAF_OS_INSTALL_ROWS) sheet.insertRowsAfter(sheet.getMaxRows(), ASSAF_OS_INSTALL_ROWS - sheet.getMaxRows());
      sheet.getRange(1, 1, 1, spec.headers.length).setValues([spec.headers]);
      sheet.hideSheet(); protectRange_(sheet.getRange(1, 1, sheet.getMaxRows(), spec.headers.length), "Assaf OS internal metadata");
      created.push(spec.name);
    });
    installAssafOsEditTrigger_(workbookId);
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
    sheet.getRange(1, column).setNote(assafOsHeaderNote_(field));
    applyFormat_(range, field.type);
    if (field.role === "input") {
      applyValidation_(range, field, column, sheet.getParent());
      if (field.required) range.setBackground("#fff7d6");
    }
    if (field.role === "system" || field.role === "calculated") {
      protectRange_(range, "Assaf OS " + field.role + ": " + field.key);
      range.setBackground(field.role === "calculated" ? "#eef5fb" : "#f3f4f6");
    }
  });
  assafOsHideAuditColumns_(sheet, tab);
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

function applyValidation_(range, field, column, workbook) {
  var values = field.values || (field.enum ? AssafOsSchema.enums[field.enum] : null);
  var builder = SpreadsheetApp.newDataValidation().setAllowInvalid(false).setHelpText(assafOsHeaderNote_(field));
  if (field.ref) {
    var choices = typeof assafOsReferenceChoices_ === "function" && workbook ? assafOsReferenceChoices_(workbook, field) : [];
    if (choices.length) builder.requireValueInList(choices, true);
    else builder.requireFormulaSatisfied(field.required ? "=FALSE" : "=LEN(" + columnLetter_(column) + "2)=0");
    range.setDataValidation(builder.build());
  } else if (values && values.length) {
    range.setDataValidation(builder.requireValueInList(values.slice(), true).build());
  } else if (field.type === "date") {
    range.setDataValidation(builder.requireDate().build());
  } else if (field.type === "integer") {
    var cell = columnLetter_(column) + "2";
    range.setDataValidation(builder.requireFormulaSatisfied("=AND(ISNUMBER(" + cell + ")," + cell + ">=" + (field.minimum || 0) + ",INT(" + cell + ")=" + cell + ")").build());
  } else if (field.type === "money_ils") {
    range.setDataValidation(builder.requireNumberGreaterThanOrEqualTo(0).build());
  }
}

function refreshAssafOsReferenceValidations() {
  var config = assafOsConfig_(), workbook = SpreadsheetApp.openById(config.workbook_id);
  ASSAF_OS_REQUEST_READ_CACHE = null;
  try {
    assafOsBeginRequestReadCache_(workbook);
    refreshAssafOsReferenceValidations_(workbook);
    assafOsMarkReferenceValidationsFresh_(workbook);
    return { refreshed: true, workbook_id: config.workbook_id };
  } finally { ASSAF_OS_REQUEST_READ_CACHE = null; }
}

function refreshAssafOsReferenceValidations_(workbook) {
  AssafOsSchema.tabs.filter(function (tab) { return tab.kind === "input"; }).forEach(function (tab) {
    var sheet = workbook.getSheetByName(tab.name);
    if (!sheet) return;
    tab.fields.forEach(function (field, index) {
      if (!field.ref || field.role !== "input") return;
      var range = sheet.getRange(2, index + 1, Math.min(sheet.getMaxRows() - 1, ASSAF_OS_INSTALL_ROWS - 1), 1);
      try { applyValidation_(range, field, index + 1, workbook); }
      catch (error) {
        var code = error && (error.assafOsCode || error.code);
        if (["ambiguous_reference_id", "missing_reference_sheet"].indexOf(code) === -1) throw error;
        applyValidation_(range, field, index + 1, null);
      }
    });
  });
}

function assafOsHeaderNote_(field) {
  var parts = [field.required ? "שדה חובה." : "שדה רשות."];
  if (field.ref) parts.push("יש לבחור מהרשימה; המזהה היציב שבסוגריים נשמר גם כשיש שמות זהים.");
  if (field.key === "source_key") parts.push("מזהה ייחודי ממסמך המקור, למשל invoice-2026-001; אותו מפתח מונע רישום כפול.");
  else if (field.note) parts.push(field.note + (/[.!?]$/.test(field.note) ? "" : "."));
  else if (field.type === "date") parts.push("דוגמה: 12/10/2026.");
  else if (field.type === "money_ils") parts.push("דוגמה: 123.45; הסכום בגיליון הוא ביחידות מטבע מלאות.");
  return parts.join(" ");
}

function assafOsHideAuditColumns_(sheet, tab) {
  var hidden = tab.fields.map(function (field, index) {
    return field.role === "system" && (/_id$/.test(field.key) || ["created_at", "updated_at", "revision", "write_source", "request_id", "calculated_at"].indexOf(field.key) !== -1) ? index + 1 : 0;
  }).filter(function (column) { return column > 0; });
  if (!hidden.length) return;
  var start = hidden[0], previous = hidden[0];
  for (var i = 1; i <= hidden.length; i += 1) {
    if (i < hidden.length && hidden[i] === previous + 1) { previous = hidden[i]; continue; }
    sheet.hideColumns(start, previous - start + 1);
    if (i < hidden.length) { start = hidden[i]; previous = hidden[i]; }
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

function installAssafOsEditTrigger_(workbookId) {
  var existing = ScriptApp.getProjectTriggers().filter(function (trigger) { return trigger.getHandlerFunction() === "reconcileAssafOsEdit"; });
  if (existing.length > 1) throw installerError_("duplicate_edit_trigger");
  if (existing.length === 0) ScriptApp.newTrigger("reconcileAssafOsEdit").forSpreadsheet(workbookId).onEdit().create();
}

