// Opt-in real TEST path. The Python launcher owns all resource and identity gates.
import assert from 'node:assert/strict';
import http from 'node:http';
import {existsSync, readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {pathToFileURL} from 'node:url';

const [backend, frontendRoot, pendingPath, runMode] = process.argv.slice(2);
if (!new Set(['ab-write-original', 'ab-readonly', 'slice-c', 'sheet-readonly']).has(runMode)) {
  process.stderr.write('{"stage":"startup","code":"validation_error","status":422,"fields":[]}\n');
  process.exit(2);
}
let input = '';
for await (const chunk of process.stdin) input += chunk;
const {proof} = JSON.parse(input);
const origin = 'https://os.example.test';
const jar = new Map();
const frontend = name => pathToFileURL(path.join(frontendRoot, name)).href;
const {createServer} = await import(frontend('bff.mjs'));
const {OwnerClient, PendingFinanceStore} = await import(frontend('client.mjs'));
const {
  business, contacts, dealConfirmation, dealForm, expenseConfirmation,
  paymentConfirmation, projectConfirmation, projectForm, scheduleConfirmation,
} = await import(frontend('views.mjs'));
const config = {
  origin,
  backend,
  frontendRoot,
  telegramBotUsername: 'SyntheticOwnerBot',
  telegramBotId: '123456',
  localTest: true,
  businessLedgerEnabled: true,
};
const server = await createServer(config);
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
let dealPosts = 0;
let businessPosts = 0;
let dropResponseOnceFor = null;

function transport(route, options = {}) {
  return new Promise((resolve, reject) => {
    const headers = {
      ...options.headers,
      Host: options.host || 'os.example.test',
      'Sec-Fetch-Site': options.fetchSite || 'same-origin',
    };
    if (options.method === 'POST') headers.Origin = options.origin || origin;
    if (jar.size) headers.Cookie = [...jar].map(([key, value]) => `${key}=${value}`).join('; ');
    if (route === '/api/business/deals' && options.method === 'POST') dealPosts += 1;
    if (route.startsWith('/api/business/') && options.method === 'POST') businessPosts += 1;
    const request = http.request(
      {hostname: '127.0.0.1', port, path: route, method: options.method || 'GET', headers},
      response => {
        for (const value of response.headers['set-cookie'] || []) {
          assert.match(value, /HttpOnly/i);
          assert.match(value, /Secure/i);
          assert.match(value, /SameSite=Strict/i);
          assert.doesNotMatch(value, /Domain=/i);
          const [key, val] = value.split(';', 1)[0].split('=');
          if (val && val !== '""' && !/Max-Age=0(?:;|$)/i.test(value)) jar.set(key, val);
          else jar.delete(key);
        }
        const chunks = [];
        response.on('data', chunk => chunks.push(chunk));
        response.on('end', () => {
          if (dropResponseOnceFor === route) {
            dropResponseOnceFor = null;
            reject(new Error('synthetic lost response'));
            return;
          }
          resolve(new Response(
            response.statusCode === 204 ? null : Buffer.concat(chunks),
            {status: response.statusCode, headers: response.headers},
          ));
        });
      },
    );
    request.on('error', reject);
    request.end(options.body);
  });
}

class FileStorage {
  constructor(file) {
    this.file = file;
    this.values = {};
    if (existsSync(file)) {
      const parsed = JSON.parse(readFileSync(file, 'utf8'));
      assert.ok(parsed && typeof parsed === 'object' && !Array.isArray(parsed));
      this.values = parsed;
    }
  }
  getItem(key) { return Object.hasOwn(this.values, key) ? this.values[key] : null; }
  setItem(key, value) { this.values[key] = value; this.flush(); }
  removeItem(key) { delete this.values[key]; this.flush(); }
  flush() {
    const temporary = `${this.file}.tmp`;
    writeFileSync(temporary, JSON.stringify(this.values), {encoding: 'utf8', mode: 0o600});
    renameSync(temporary, this.file);
  }
  empty() { return Object.keys(this.values).length === 0; }
}

const storage = new FileStorage(pendingPath);
const pending = new PendingFinanceStore(storage);
const client = new OwnerClient(transport);
const dealInput = deal => Object.fromEntries(
  ['id', 'title', 'amount_minor', 'currency', 'payment_plan', 'start_date', 'status', 'project_id', 'notes']
    .map(key => [key, deal[key]]),
);

async function sendWithDurableMarker(write) {
  const marker = pending.save(session.business_binding, {
    kind: 'deal',
    request_id: write.request_id,
    entity_id: write.deal.id,
    expected_receipt_revision: write.expected_revision + 1,
    submitted_at: new Date().toISOString(),
  });
  let receipt;
  try {
    receipt = await client.saveDeal(write);
  } catch (error) {
    if ([409, 422].includes(error?.status)) {
      // These are definitive rejections and match the existing app marker policy.
      pending.clear(session.business_binding, marker);
      throw error;
    }
    if (error?.code !== 'unknown') throw error;
    // A single receipt read is the only permitted recovery; the write is never retried.
    receipt = await client.businessReceipt(marker);
  }
  pending.clear(session.business_binding, marker);
  return receipt;
}

const markerForFinance = (kind, write) => ({
  kind,
  request_id: write.request_id,
  entity_id: kind === 'schedule' ? write.deal_id : write[kind].id,
  expected_receipt_revision: kind === 'schedule' ? write.expected_revision + 1 : kind === 'project' ? write.expected_revision + 1 : 1,
  submitted_at: new Date().toISOString(),
});

async function sendFinanceWithDurableMarker(kind, write, {loseResponse = false} = {}) {
  const marker = pending.save(session.business_binding, markerForFinance(kind, write));
  if (loseResponse) dropResponseOnceFor = `/api/business/${({schedule:'schedules',payment:'payments',expense:'expenses',project:'projects'})[kind]}`;
  let receipt;
  try {
    receipt = await client.saveFinance(kind, write);
  } catch (error) {
    if ([409, 422].includes(error?.status)) {
      pending.clear(session.business_binding, marker);
      throw error;
    }
    if (error?.code !== 'unknown') throw error;
    receipt = await client.businessReceipt(marker);
  }
  return {marker, receipt};
}

function finishMarkedWrite(marker) {
  pending.clear(session.business_binding, marker);
}

const uuidFields = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const campaignKey = 'slice_c_campaign_v1';
const campaignIdNames = ['deal','deal_request','original_schedule_request','original_charge','schedule_request','charge_1','charge_2','charge_3','payment_request','payment','project_request','project','expense_request','expense','stale_request','stale_payment','overpay_request','overpay_payment','duplicate_request'];
function loadSliceCCampaign() {
  const value = storage.values[campaignKey];
  if (value === undefined) return null;
  assert.ok(value && typeof value === 'object' && !Array.isArray(value));
  assert.deepEqual(Object.keys(value).sort(), ['completed', 'ids', 'inflight'].sort());
  assert.ok(value.ids && Object.keys(value.ids).sort().join(',') === campaignIdNames.slice().sort().join(',') && Object.values(value.ids).every(uuidFields));
  assert.ok(Array.isArray(value.completed) && new Set(value.completed).size === value.completed.length && value.completed.every(item => ['deal','original_schedule','schedule','payment','project','expense','schedule_replay','stale','overpay','duplicate'].includes(item)));
  assert.ok(value.inflight === null || ['deal','original_schedule','schedule','payment','project','expense','schedule_replay','stale','overpay','duplicate'].includes(value.inflight));
  return value;
}
function saveSliceCCampaign(value) {
  storage.values[campaignKey] = structuredClone(value);
  storage.flush();
}
function newSliceCCampaign() {
  const value = {ids:Object.fromEntries(campaignIdNames.map(name => [name, randomUUID()])), completed:[], inflight:null};
  saveSliceCCampaign(value);
  return value;
}
function beginCampaignStep(campaign, step) {
  assert.equal(campaign.inflight, null, 'another campaign step is unresolved');
  campaign.inflight = step;
  saveSliceCCampaign(campaign);
}
function completeCampaignStep(campaign, step, marker) {
  assert.equal(campaign.inflight, step, 'completed step must match durable in-flight metadata');
  if (!campaign.completed.includes(step)) campaign.completed.push(step);
  saveSliceCCampaign(campaign);
  finishMarkedWrite(marker);
  campaign.inflight = null;
  saveSliceCCampaign(campaign);
}

async function recoverPendingBeforeWrites() {
  const campaign = loadSliceCCampaign();
  const orphan = pending.load(session.business_binding);
  if (!orphan) {
    if (campaign?.inflight !== null && campaign?.inflight !== undefined) {
      if (!campaign.completed.includes(campaign.inflight)) throw new Error('campaign step is unresolved without a pending marker');
      campaign.inflight = null;
      saveSliceCCampaign(campaign);
    }
    return;
  }
  if (!campaign) throw new Error('pending write has no local campaign');
  const id = campaign.ids;
  const expected = {
    deal:['deal',id.deal_request,id.deal,1],
    original_schedule:['schedule',id.original_schedule_request,'34d5f6c3-f176-4830-88f9-3e23899d61cc',4],
    schedule:['schedule',id.schedule_request,id.deal,2],
    schedule_replay:['schedule',id.schedule_request,id.deal,2],
    payment:['payment',id.payment_request,id.payment,1],
    project:['project',id.project_request,id.project,1],
    expense:['expense',id.expense_request,id.expense,1],
    stale:['payment',id.stale_request,id.stale_payment,1],
    overpay:['payment',id.overpay_request,id.overpay_payment,1],
    duplicate:['payment',id.duplicate_request,id.payment,1],
  }[campaign.inflight];
  if (!expected || orphan.kind !== expected[0] || orphan.request_id !== expected[1] || orphan.entity_id !== expected[2] || orphan.expected_receipt_revision !== expected[3]) throw new Error('pending marker is not bound to the exact in-flight campaign step');
  const receipt = await client.businessReceipt(orphan);
  const step = campaign.inflight;
  if (['stale','overpay','duplicate'].includes(step)) throw new Error('a rejection-only request returned a committed receipt');
  completeCampaignStep(campaign, step, orphan);
  throw new Error(`prior ${step} receipt recovered; rerun the harness`);
}

function exactDeal(record, expected) {
  assert.deepEqual(Object.fromEntries(['id','revision','title','amount_minor','currency','payment_plan','start_date','status','project_id','notes'].map(key => [key, record[key]])), expected);
}

async function runAbReadonly() {
  assert.equal(pending.load(session.business_binding), null, 'read-only closeout refuses while a write outcome is pending');
  stage = 'contacts_read';
  const crm = await client.read('contacts');
  assert.equal(crm.source, 'mia_postgresql');
  assert.ok(contacts(crm).includes('לקוח PostgreSQL סינתטי'));
  stage = 'business_initial_read';
  const snapshot = await client.read('business');
  const original = snapshot.deals.find(deal => deal.id === '34d5f6c3-f176-4830-88f9-3e23899d61cc');
  const edited = snapshot.deals.find(deal => deal.id === '201e561f-2ff7-4c35-90f1-c7a4b558f41a');
  assert.ok(original);
  assert.equal(original.revision, 3);
  assert.equal(original.amount_minor, 34567);
  assert.equal(original.currency, 'ILS');
  assert.equal(original.payment_plan, 'full');
  assert.equal(original.status, 'active');
  assert.equal(original.project_id, null);
  assert.equal(typeof original.title, 'string');
  assert.equal(typeof original.notes, 'string');
  assert.match(original.start_date, /^\d{4}-\d{2}-\d{2}$/);
  assert.ok(Number.isFinite(Date.parse(original.updated_at)));
  exactDeal(edited, {
    id: '201e561f-2ff7-4c35-90f1-c7a4b558f41a', revision: 2,
    title: 'בדיקת E2E סינתטית 201e561f ערוכה', amount_minor: 23456,
    currency: 'ILS', payment_plan: 'installments', start_date: '2026-10-11',
    status: 'cancelled', project_id: null,
    notes: 'העריכה משאירה את סכומי הגבייה וההוצאות לא ידועים',
  });
  assert.deepEqual(snapshot.totals, {deal_value_minor:34567,received_minor:null,outstanding_minor:null,expense_minor:null});
  const rendered = business(snapshot, null, true, 'read-only closeout');
  assert.ok(rendered.includes(edited.title));
  assert.ok(rendered.includes(original.title));
  const quickLead = await transport('/api/quick-leads/proposals', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':client.csrf}, body:JSON.stringify({name:'אסור',need:'אסור',source:'other'})});
  assert.equal(quickLead.status, 503);
  const reminderWrite = await transport('/api/reminders', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':client.csrf}, body:JSON.stringify({text:'אסור'})});
  assert.equal(reminderWrite.status, 405);
  stage = 'logout';
  await client.signOut();
  await assert.rejects(client.session(), error => error?.status === 401 && error?.code === 'unauthorized');
  process.stdout.write('PASS READ-ONLY A/B closeout; accepted deals/totals/views and disabled writes/logout revocation verified\n');
}

async function runSheetReadonly() {
  assert.equal(pending.load(session.business_binding), null, 'read-only acceptance refuses while a write outcome is pending');
  const storageBefore = JSON.stringify(storage.values);
  assert.equal(businessPosts, 0);
  stage = 'sheet_readonly_finance';
  const snapshot = await client.read('business');
  const finance = await client.readFinance();
  assert.equal(finance.mode, 'TEST');
  assert.equal(finance.source, 'google_sheets');
  assert.equal(finance.coverage, 'financial_complete');
  assert.equal(finance.currency, 'ILS');
  assert.deepEqual(finance.counts, {deals:3, charges:4, payments:1, expenses:1, projects:1});
  assert.deepEqual(finance.totals, {
    deal_value_minor:44567, scheduled_minor:44567, received_minor:1200,
    outstanding_minor:43367, expense_minor:500, net_cash_minor:700,
  });
  assert.equal(finance.deals.length, 3);
  assert.equal(finance.charges.length, 4);
  assert.equal(finance.payments.length, 1);
  assert.equal(finance.expenses.length, 1);
  assert.equal(finance.projects.length, 1);
  const project = finance.projects.find(item => item.id === 'd2103f88-37f7-42b4-9326-7fde8030cdc8');
  assert.deepEqual(project && {
    id:project.id, revision:project.revision, title:project.title, status:project.status,
    deadline:project.deadline, notes:project.notes,
  }, {
    id:'d2103f88-37f7-42b4-9326-7fde8030cdc8', revision:3,
    title:'פרויקט Slice C סינתטי', status:'active', deadline:'2026-12-31',
    notes:'בדיקת עריכת פרויקט מבוקרת מהגיליון',
  });
  stage = 'sheet_readonly_render';
  const rendered = business(snapshot, finance, true, null);
  assert.ok(rendered.includes(project.title));
  assert.ok(rendered.includes('תזרים נטו מתועד'));
  assert.ok(rendered.includes('31.12.2026'));
  const form = projectForm(project);
  assert.ok(form.includes(project.title));
  assert.ok(form.includes(project.notes));
  assert.ok(form.includes('value="2026-12-31"'));
  assert.ok(form.includes('value="active" selected'));
  assert.equal(businessPosts, 0, 'read-only acceptance must issue zero business POSTs');
  assert.equal(JSON.stringify(storage.values), storageBefore, 'read-only acceptance must not alter campaign markers');
  stage = 'sheet_readonly_logout';
  await client.signOut();
  await assert.rejects(client.session(), error => error?.status === 401 && error?.code === 'unauthorized');
  assert.equal(businessPosts, 0);
  process.stdout.write('PASS READ-ONLY Sheet/mobile/BFF/Mia/Google TEST finance acceptance; project, totals, views and logout verified\n');
}

async function sendDealForCampaign(write) {
  const marker = pending.save(session.business_binding, {
    kind:'deal', request_id:write.request_id, entity_id:write.deal.id,
    expected_receipt_revision:write.expected_revision + 1, submitted_at:new Date().toISOString(),
  });
  let receipt;
  try { receipt = await client.saveDeal(write); }
  catch (error) {
    if ([409, 422].includes(error?.status)) { pending.clear(session.business_binding, marker); throw error; }
    if (error?.code !== 'unknown') throw error;
    receipt = await client.businessReceipt(marker);
  }
  return {marker, receipt};
}

async function rejectedFinance(kind, write, expectedStatus) {
  const marker = pending.save(session.business_binding, markerForFinance(kind, write));
  try {
    await client.saveFinance(kind, write);
    assert.fail('financial rejection unexpectedly committed');
  } catch (error) {
    if (error?.status !== expectedStatus || ![409, 422].includes(error.status)) throw error;
    return marker;
  }
}

async function runSliceC() {
  stage = 'pending_receipt_recovery';
  await recoverPendingBeforeWrites();
  const campaign = loadSliceCCampaign() || newSliceCCampaign();
  const id = campaign.ids;
  const originalId = '34d5f6c3-f176-4830-88f9-3e23899d61cc';
  const deal = {
    id:id.deal, title:'עסקת Slice C סינתטית', amount_minor:10000, currency:'ILS',
    payment_plan:'installments', start_date:'2026-10-12', status:'active', project_id:null,
    notes:'בדיקת תשלומים והוצאות בסביבת TEST',
  };
  const create = {request_id:id.deal_request, expected_revision:0, deal};
  assert.ok(dealConfirmation(create).includes(deal.title));
  if (!campaign.completed.includes('deal')) {
    stage = 'slice_c_deal';
    beginCampaignStep(campaign, 'deal');
    const accepted = await sendDealForCampaign(create);
    assert.equal(accepted.receipt.revision, 1);
    completeCampaignStep(campaign, 'deal', accepted.marker);
  }

  stage = 'slice_c_initial_finance';
  let finance = await client.readFinance();
  const original = finance.deals.find(item => item.id === originalId);
  assert.ok(original && original.revision === (campaign.completed.includes('original_schedule') ? 4 : 3) && original.payment_plan === 'full' && original.status === 'active');
  const originalSchedule = {
    request_id:id.original_schedule_request, expected_revision:3, deal_id:originalId,
    number_of_charges:1, first_due_date:'2026-10-31', charge_ids:[id.original_charge],
  };
  assert.ok(original.start_date <= originalSchedule.first_due_date, 'existing deal start must precede the proposed full-payment charge');
  if (!campaign.completed.includes('original_schedule')) {
    stage = 'slice_c_original_schedule';
    beginCampaignStep(campaign, 'original_schedule');
    const accepted = await sendFinanceWithDurableMarker('schedule', originalSchedule);
    assert.equal(accepted.receipt.revision, 4);
    completeCampaignStep(campaign, 'original_schedule', accepted.marker);
  }

  const schedule = {
    request_id:id.schedule_request, expected_revision:1, deal_id:id.deal,
    number_of_charges:3, first_due_date:'2026-10-31',
    charge_ids:[id.charge_1,id.charge_2,id.charge_3],
  };
  let scheduleReceipt;
  if (!campaign.completed.includes('schedule')) {
    stage = 'slice_c_schedule';
    beginCampaignStep(campaign, 'schedule');
    const accepted = await sendFinanceWithDurableMarker('schedule', schedule);
    scheduleReceipt = accepted.receipt;
    assert.equal(scheduleReceipt.revision, 2);
    completeCampaignStep(campaign, 'schedule', accepted.marker);
  } else {
    scheduleReceipt = await client.businessReceipt(markerForFinance('schedule', schedule));
  }
  if (!campaign.completed.includes('schedule_replay')) {
    stage = 'slice_c_schedule_replay';
    beginCampaignStep(campaign, 'schedule_replay');
    const replay = await sendFinanceWithDurableMarker('schedule', schedule);
    assert.deepEqual(replay.receipt, scheduleReceipt);
    completeCampaignStep(campaign, 'schedule_replay', replay.marker);
  }

  const payment = {
    request_id:id.payment_request, expected_charge_revision:1, confirmed_received:true,
    payment:{id:id.payment,charge_id:id.charge_1,amount_minor:1200,currency:'ILS',received_at:'2026-10-11T12:00:00.000Z'},
  };
  if (!campaign.completed.includes('payment')) {
    stage = 'slice_c_payment';
    beginCampaignStep(campaign, 'payment');
    const accepted = await sendFinanceWithDurableMarker('payment', payment);
    assert.equal(accepted.receipt.revision, 1);
    completeCampaignStep(campaign, 'payment', accepted.marker);
  }

  const project = {
    request_id:id.project_request, expected_revision:0,
    project:{id:id.project,title:'פרויקט Slice C סינתטי',status:'active',deadline:'2026-12-31',notes:'פרויקט בדיקה בלבד'},
  };
  if (!campaign.completed.includes('project')) {
    stage = 'slice_c_project';
    beginCampaignStep(campaign, 'project');
    const accepted = await sendFinanceWithDurableMarker('project', project);
    assert.equal(accepted.receipt.revision, 1);
    completeCampaignStep(campaign, 'project', accepted.marker);
  }

  const expense = {
    request_id:id.expense_request, expected_revision:0,
    expense:{id:id.expense,project_id:id.project,amount_minor:500,currency:'ILS',occurred_on:'2026-10-12',notes:'הוצאה סינתטית לפרויקט'},
  };
  if (!campaign.completed.includes('expense')) {
    stage = 'slice_c_expense_lost_response';
    beginCampaignStep(campaign, 'expense');
    const accepted = await sendFinanceWithDurableMarker('expense', expense, {loseResponse:true});
    assert.equal(accepted.receipt.revision, 1);
    completeCampaignStep(campaign, 'expense', accepted.marker);
  }

  if (!campaign.completed.includes('stale')) {
    stage = 'slice_c_stale';
    beginCampaignStep(campaign, 'stale');
    const marker = await rejectedFinance('payment', {
      request_id:id.stale_request, expected_charge_revision:1, confirmed_received:true,
      payment:{id:id.stale_payment,charge_id:id.charge_1,amount_minor:1,currency:'ILS',received_at:'2026-10-11T12:01:00.000Z'},
    }, 409);
    completeCampaignStep(campaign, 'stale', marker);
  }
  if (!campaign.completed.includes('overpay')) {
    stage = 'slice_c_overpay';
    beginCampaignStep(campaign, 'overpay');
    const marker = await rejectedFinance('payment', {
      request_id:id.overpay_request, expected_charge_revision:2, confirmed_received:true,
      payment:{id:id.overpay_payment,charge_id:id.charge_1,amount_minor:2135,currency:'ILS',received_at:'2026-10-11T12:02:00.000Z'},
    }, 422);
    completeCampaignStep(campaign, 'overpay', marker);
  }
  if (!campaign.completed.includes('duplicate')) {
    stage = 'slice_c_duplicate';
    beginCampaignStep(campaign, 'duplicate');
    const marker = await rejectedFinance('payment', {
      request_id:id.duplicate_request, expected_charge_revision:2, confirmed_received:true,
      payment:{...payment.payment,amount_minor:1,received_at:'2026-10-11T12:03:00.000Z'},
    }, 409);
    completeCampaignStep(campaign, 'duplicate', marker);
  }

  stage = 'slice_c_final_read';
  const snapshot = await client.read('business');
  finance = await client.readFinance();
  assert.equal(finance.coverage, 'financial_complete');
  assert.deepEqual(finance.totals, {
    deal_value_minor:44567, scheduled_minor:44567, received_minor:1200,
    outstanding_minor:43367, expense_minor:500, net_cash_minor:700,
  });
  exactDeal(finance.deals.find(item => item.id === id.deal), {...deal,revision:2});
  assert.deepEqual(finance.charges.filter(item => item.deal_id === id.deal).map(item => item.amount_minor), [3334,3333,3333]);
  assert.equal(finance.payments.find(item => item.id === id.payment).amount_minor, 1200);
  assert.equal(finance.expenses.find(item => item.id === id.expense).project_id, id.project);
  assert.equal(finance.projects.find(item => item.id === id.project).title, project.project.title);
  const firstCharge = finance.charges.find(item => item.id === id.charge_1);
  const html = business(snapshot, finance, true);
  assert.ok(html.includes(deal.title) && html.includes(project.project.title) && html.includes('תזרים נטו מתועד'));
  assert.ok(scheduleConfirmation(finance.deals.find(item => item.id === id.deal), schedule, finance.charges.filter(item => item.deal_id === id.deal)).includes(deal.title));
  assert.ok(paymentConfirmation(firstCharge, payment, finance.deals.find(item => item.id === id.deal)).includes('\u200f12\u00a0\u200f₪'));
  assert.ok(projectConfirmation(project).includes(project.project.title));
  assert.ok(expenseConfirmation(expense, project.project).includes('\u200f5\u00a0\u200f₪'));
  for (const write of [create, originalSchedule, schedule, payment, project, expense]) {
    const marker = write.deal ? {kind:'deal',request_id:write.request_id,entity_id:write.deal.id,expected_receipt_revision:1,submitted_at:new Date().toISOString()} : markerForFinance(write.number_of_charges ? 'schedule' : write.payment ? 'payment' : write.project ? 'project' : 'expense', write);
    const receipt = await client.businessReceipt(marker);
    assert.equal(receipt.request_id, write.request_id);
  }
  assert.equal(pending.load(session.business_binding), null);
  process.stdout.write('PASS Slice C OwnerClient/BFF/Mia/Google TEST finance; durable writes, receipts, conflicts, totals and live DTO views\n');
}

let session;
let stage = 'startup';
try {
  stage = 'auth_challenge';
  const challenge = await transport('/auth/challenge');
  assert.equal(challenge.status, 200);
  stage = 'auth_login';
  const login = await transport('/auth/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': (await challenge.json()).csrf_token},
    body: JSON.stringify({login_proof: proof}),
  });
  assert.equal(login.status, 200);
  stage = 'session_client';
  session = await client.session();
  assert.deepEqual(
    [...session.capabilities].sort(),
    ['activities.read', 'business.read', 'business.write', 'contacts.read', 'reminders.read'],
  );
  assert.match(session.business_binding, /^[0-9a-f]{64}$/);

  stage = 'session_raw';
  const rawSession = await transport('/api/session');
  const rawScopes = (await rawSession.json()).scopes;
  assert.deepEqual(
    [...rawScopes].sort(),
    [
      'business-ledger:read',
      'business-ledger:write',
      'owner-os:read:activities',
      'owner-os:read:contacts',
      'owner-os:read:reminders',
    ],
  );
  assert.ok(!rawScopes.includes('quick_leads.create'));
  assert.ok(!rawScopes.includes('reminders.write'));

  if (runMode === 'ab-readonly') {
    await runAbReadonly();
  } else if (runMode === 'sheet-readonly') {
    await runSheetReadonly();
  } else if (runMode === 'slice-c') {
    await runSliceC();
  } else {
    assert.equal(runMode, 'ab-write-original');
  const orphan = pending.load(session.business_binding);
  if (orphan) {
    stage = 'pending_receipt_recovery';
    // Recover a prior ambiguous dispatch by receipt only, then require a fresh invocation.
    await client.businessReceipt(orphan);
    pending.clear(session.business_binding, orphan);
    throw new Error('prior pending receipt recovered; rerun the harness');
  }

  stage = 'contacts_read';
  const crm = await client.read('contacts');
  assert.equal(crm.source, 'mia_postgresql');
  assert.equal(crm.synthetic, false);
  assert.equal(crm.records.length, 1);
  assert.ok(contacts(crm).includes('לקוח PostgreSQL סינתטי'));

  stage = 'business_initial_read';
  const initial = await client.read('business');
  assert.equal(initial.source, 'google_sheets');
  assert.equal(initial.mode, 'TEST');
  assert.equal(initial.coverage, 'deals_only');
  assert.equal(initial.totals.received_minor, null);
  assert.equal(initial.totals.outstanding_minor, null);
  assert.equal(initial.totals.expense_minor, null);

  const dealId = randomUUID();
  const createRequest = {
    request_id: randomUUID(),
    expected_revision: 0,
    deal: {
      id: dealId,
      title: `בדיקת E2E סינתטית ${dealId.slice(0, 8)}`,
      amount_minor: 12345,
      currency: 'ILS',
      payment_plan: 'full',
      start_date: new Date().toISOString().slice(0, 10),
      status: 'active',
      project_id: null,
      notes: 'OwnerClient אל Google Sheets TEST בלבד',
    },
  };
  assert.ok(dealConfirmation(createRequest).includes(createRequest.deal.title));
  stage = 'create_deal';
  const created = await sendWithDurableMarker(createRequest);
  assert.equal(created.request_id, createRequest.request_id);
  assert.equal(created.id, dealId);
  assert.equal(created.revision, 1);

  const recovered = await client.businessReceipt({
    kind: 'deal',
    request_id: createRequest.request_id,
    entity_id: dealId,
    expected_receipt_revision: 1,
    submitted_at: new Date().toISOString(),
  });
  stage = 'replay_create';
  assert.deepEqual(recovered, created);
  const replayed = await sendWithDurableMarker(createRequest);
  assert.deepEqual(replayed, created);

  const afterCreate = await client.read('business');
  const accepted = afterCreate.deals.find(deal => deal.id === dealId);
  assert.ok(accepted);
  assert.equal(accepted.revision, 1);
  assert.deepEqual(dealInput(accepted), createRequest.deal);
  assert.equal(
    afterCreate.totals.deal_value_minor,
    initial.totals.deal_value_minor + createRequest.deal.amount_minor,
  );
  assert.equal(afterCreate.totals.received_minor, null);
  assert.equal(afterCreate.totals.outstanding_minor, null);
  assert.equal(afterCreate.totals.expense_minor, null);
  const createView = business(afterCreate, null, true);
  assert.ok(createView.includes(createRequest.deal.title));
  assert.ok(createView.includes('טרם נאסף'));
  assert.ok(dealForm(accepted).includes(createRequest.deal.title));

  const editRequest = {
    request_id: randomUUID(),
    expected_revision: accepted.revision,
    deal: {
      id: dealId,
      title: `${createRequest.deal.title} ערוכה`,
      amount_minor: 23456,
      currency: 'ILS',
      payment_plan: 'installments',
      start_date: createRequest.deal.start_date,
      status: 'cancelled',
      project_id: null,
      notes: 'העריכה משאירה את סכומי הגבייה וההוצאות לא ידועים',
    },
  };
  stage = 'edit_deal';
  const edited = await sendWithDurableMarker(editRequest);
  assert.equal(edited.revision, 2);
  const editReceipt = await client.businessReceipt({
    kind: 'deal',
    request_id: editRequest.request_id,
    entity_id: dealId,
    expected_receipt_revision: 2,
    submitted_at: new Date().toISOString(),
  });
  assert.deepEqual(editReceipt, edited);

  const afterEdit = await client.read('business');
  const acceptedEdit = afterEdit.deals.find(deal => deal.id === dealId);
  assert.equal(acceptedEdit.revision, 2);
  assert.deepEqual(dealInput(acceptedEdit), editRequest.deal);
  assert.equal(
    afterEdit.totals.deal_value_minor,
    initial.totals.deal_value_minor,
    'cancelled deal must be excluded from the active deal total',
  );
  assert.equal(afterEdit.totals.received_minor, null);
  assert.equal(afterEdit.totals.outstanding_minor, null);
  assert.equal(afterEdit.totals.expense_minor, null);
  assert.ok(business(afterEdit, null, true).includes(editRequest.deal.title));
  assert.ok(dealForm(acceptedEdit).includes(editRequest.deal.title));

  const stale = {
    request_id: randomUUID(),
    expected_revision: 1,
    deal: {...editRequest.deal, title: `${editRequest.deal.title} stale`},
  };
  const staleMarker = pending.save(session.business_binding, {
    kind: 'deal',
    request_id: stale.request_id,
    entity_id: stale.deal.id,
    expected_receipt_revision: stale.expected_revision + 1,
    submitted_at: new Date().toISOString(),
  });
  stage = 'stale_conflict';
  await assert.rejects(client.saveDeal(stale), error => error?.status === 409 && error?.code === 'conflict');
  pending.clear(session.business_binding, staleMarker);
  const afterConflict = await client.read('business');
  assert.deepEqual(afterConflict.deals.find(deal => deal.id === dealId), acceptedEdit);
  assert.equal(afterConflict.totals.deal_value_minor, initial.totals.deal_value_minor);
  assert.equal(afterConflict.totals.received_minor, null);
  assert.equal(afterConflict.totals.outstanding_minor, null);
  assert.equal(afterConflict.totals.expense_minor, null);

  const quickLead = await transport('/api/quick-leads/proposals', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': client.csrf},
    body: JSON.stringify({name: 'אסור', need: 'אסור', source: 'other'}),
  });
  assert.equal(quickLead.status, 503);
  const reminderWrite = await transport('/api/reminders', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': client.csrf},
    body: JSON.stringify({text: 'אסור'}),
  });
  assert.equal(reminderWrite.status, 405);
  assert.equal(dealPosts, 4, 'create, exact replay, edit, and stale conflict only');
  assert.ok(storage.empty());
  rmSync(pendingPath, {force: true});
  process.stdout.write(
    'PASS OwnerClient -> current mobile BFF -> FastAPI -> disposable PostgreSQL -> real Google TEST ledger; create/edit/replay/receipt/conflict and live DTO views\n',
  );
  }
} catch (error) {
  const safeStages = new Set(['startup', 'auth_challenge', 'auth_login', 'session_client', 'session_raw', 'contacts_read', 'business_initial_read', 'pending_receipt_recovery', 'create_deal', 'replay_create', 'edit_deal', 'stale_conflict', 'logout', 'sheet_readonly_finance', 'sheet_readonly_render', 'sheet_readonly_logout', 'slice_c_deal', 'slice_c_initial_finance', 'slice_c_original_schedule', 'slice_c_schedule', 'slice_c_schedule_replay', 'slice_c_payment', 'slice_c_project', 'slice_c_expense_lost_response', 'slice_c_stale', 'slice_c_overpay', 'slice_c_duplicate', 'slice_c_final_read']);
  const safeCodes = new Set(['conflict', 'forbidden', 'integration_unavailable', 'invalid_response', 'operation_unavailable', 'rate_limited', 'record_not_found', 'source_unavailable', 'timeout', 'unauthorized', 'unknown', 'validation_error']);
  const safeHttpStatuses = new Set([400, 401, 403, 404, 409, 413, 415, 422, 429, 500, 502, 503, 504]);
  const safeFields = new Set(['id', 'title', 'amount_minor', 'currency', 'payment_plan', 'start_date', 'status', 'project_id', 'notes']);
  const safeStage = safeStages.has(stage) ? stage : 'unknown';
  const safeCode = safeCodes.has(error?.code) ? error.code : 'unexpected';
  const safeStatus = safeHttpStatuses.has(error?.status) ? error.status : null;
  const fields = error?.fields && typeof error.fields === 'object' && !Array.isArray(error.fields)
    ? Object.keys(error.fields).filter(key => safeFields.has(key)).sort()
    : [];
  process.stderr.write(JSON.stringify({stage: safeStage, code: safeCode, status: safeStatus, fields}) + '\n');
  process.exitCode = 1;
} finally {
  await new Promise(resolve => server.close(resolve));
}
