// Local import artifact only. This command never creates or edits a Google resource.
import fs from 'node:fs/promises';
import path from 'node:path';
import {Workbook, SpreadsheetFile} from '@oai/artifact-tool';

const output = path.resolve('.cache/business-ledger');
await fs.mkdir(output, {recursive: true});
const schema = {
  Deals: ['id','revision','title','amount_minor','currency','payment_plan','start_date','status','project_id','notes','updated_at'],
  Charges: ['id','revision','deal_id','status','amount_minor','currency','due_date','updated_at'],
  Payments: ['id','revision','charge_id','status','amount_minor','currency','received_at','updated_at'],
  Expenses: ['id','revision','project_id','amount_minor','currency','occurred_on','notes','updated_at'],
  Projects: ['id','revision','title','status','deadline','notes','updated_at'],
  Settings: ['key','value'],
  Audit: ['event_type','entity_type','entity_id','revision','request_id','owner_id','data_hash','snapshot_json','receipt_json','committed_at'],
  Conflicts: ['id','detected_at','entity_type','entity_id','reason_code','observed_json'],
  'CRM View': ['status','message'],
};
const workbook = Workbook.create();
for (const [name, headers] of Object.entries(schema)) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const last = String.fromCharCode(64 + headers.length);
  const area = sheet.getRange(`A1:${last}18`);
  area.format.font.name = 'Arial';
  area.format.font.size = 10;
  area.format.font.color = '#202B3A';
  area.format.verticalAlignment = 'center';
  area.format.rowHeightPx = 25;
  area.format.columnWidthPx = 156;
  sheet.getRange(`A1:${last}1`).values = [headers];
  const header = sheet.getRange(`A1:${last}1`);
  header.format.fill = '#24394B';
  header.format.font.bold = true;
  header.format.font.color = '#FFFFFF';
  header.format.horizontalAlignment = 'center';
  header.format.rowHeightPx = 31;
  sheet.freezePanes.freezeRows(1);
  for (let column = 0; column < headers.length; column++) {
    const col = String.fromCharCode(65 + column);
    const range = sheet.getRange(`${col}2:${col}18`);
    if (headers[column].includes('minor') || headers[column] === 'revision') {
      range.setNumberFormat('#,##0');
      range.format.horizontalAlignment = 'right';
    } else {
      range.setNumberFormat('@');
    }
    if (headers[column].includes('json')) range.format.columnWidthPx = 280;
    if (headers[column] === 'notes') range.format.columnWidthPx = 220;
  }
}
workbook.worksheets.getItem('Settings').getRange('A2:B9').values = [
  ['mode','TEST'], ['currency','ILS'], ['vat_rate',0], ['coverage','deals_only'],
  ['live_launch_date','not_configured'], ['financial_source','google_sheets'],
  ['crm_source','mia_postgresql'], ['financial_units','integer_agorot'],
];
workbook.worksheets.getItem('Settings').getRange('B2:B9').format.columnWidthPx = 220;
workbook.worksheets.getItem('CRM View').getRange('A2:B2').values = [
  ['unavailable','טרם חובר למקור CRM המאומת'],
];
workbook.worksheets.getItem('CRM View').getRange('B1:B3').format.columnWidthPx = 320;
workbook.worksheets.getItem('Deals').getRange('F2:F18').dataValidation = {
  rule:{type:'list',values:['full','installments','monthly']},
};
workbook.worksheets.getItem('Deals').getRange('H2:H18').dataValidation = {
  rule:{type:'list',values:['active','cancelled']},
};
await workbook.recalculate();
const inspected = await workbook.inspect({kind:'sheet',include:'id,name',maxChars:4000});
await fs.writeFile(path.join(output,'workbook-inspection.json'),JSON.stringify(inspected,null,2));
for (const name of Object.keys(schema)) {
  const preview = await workbook.render({sheetName:name,range:`A1:${name==='Settings'?'B':'F'}10`,scale:1,format:'png'});
  await fs.writeFile(path.join(output,`${name.replaceAll(' ','-')}.png`),new Uint8Array(await preview.arrayBuffer()));
}
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(path.join(output,'AssafWeb-OS-Business-Ledger-TEST.xlsx'));
console.log(JSON.stringify({local_artifact:'AssafWeb-OS-Business-Ledger-TEST.xlsx',tabs:Object.keys(schema),synthetic_records:0,google_resources_created:0}));
