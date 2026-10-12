"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {runtime, event, inner, DEAL_ID, NOW, OWNER, hmac} = require("./runtime-fixture.cjs");
const uuid = n => `${String(n).padStart(8,"0")}-1111-4111-8111-${String(n).padStart(12,"0")}`;
const call = (rt, operation, data) => inner(rt.context.doPost(event(operation, data)));
const deal = {id: DEAL_ID, title: "עסקה סינתטית", amount_minor: 10000, currency: "ILS", payment_plan: "installments", start_date: "2026-01-01", status: "active", project_id: null, notes: ""};
const schedule = {request_id: uuid(2), expected_revision: 1, deal_id: DEAL_ID, number_of_charges: 3, first_due_date: "2026-01-31", charge_ids: [uuid(3),uuid(4),uuid(5)]};
function setup() {
  const rt = runtime({applyBatch:true, requireExplicitHmacCharset:true});
  assert.equal(call(rt,"save_deal",{request_id:uuid(1),expected_revision:0,deal}).status,"ok");
  return rt;
}
const payment = () => ({request_id:uuid(6),expected_charge_revision:1,confirmed_received:true,payment:{id:uuid(7),charge_id:uuid(3),amount_minor:1200,currency:"ILS",received_at:NOW}});
const expense = () => ({request_id:uuid(8),expected_revision:0,expense:{id:uuid(9),project_id:null,amount_minor:500,currency:"ILS",occurred_on:"2026-01-15",notes:"הוצאה סינתטית"}});

test("one atomic schedule uses entire deal total; manual partial receipt and expense produce exact sourced totals", () => {
  const rt=setup();
  const initial=call(rt,"finance_snapshot",{});
  assert.equal(initial.result.coverage,"unknown");
  assert.ok(Object.values(initial.result.totals).every(value=>value===null));
  const planned=call(rt,"save_schedule",schedule);
  assert.equal(planned.status,"ok"); assert.equal(planned.result.id,DEAL_ID); assert.equal(planned.result.revision,2);
  const batch=rt.batchBodies.at(-1);
  assert.equal(batch.requests.length,5,"deal + three charges + accepted audit/receipt in one batch");
  let result=call(rt,"finance_snapshot",{}).result;
  assert.deepEqual(result.charges.map(row=>row.amount_minor),[3334,3333,3333]);
  assert.deepEqual(result.charges.map(row=>row.due_date),["2026-01-31","2026-02-28","2026-03-31"]);
  assert.equal(result.coverage,"financial_complete"); assert.equal(result.totals.received_minor,0,"a complete accepted payment read proves actual zero");
  assert.equal(call(rt,"record_payment",payment()).status,"ok");
  assert.equal(call(rt,"record_expense",expense()).status,"ok");
  result=call(rt,"finance_snapshot",{}).result;
  assert.deepEqual(result.totals,{deal_value_minor:10000,scheduled_minor:10000,received_minor:1200,outstanding_minor:8800,expense_minor:500,net_cash_minor:700});
  assert.equal(result.charges[0].revision,2);
  assert.equal(result.payments[0].status,"posted"); assert.equal(result.payments[0].revision,1);
  assert.equal(call(rt,"snapshot",{}).result.deals[0].revision,2,"A/B reads include the schedule's accepted deal revision");
  const receipt=call(rt,"receipt",{request_id:uuid(6)});
  assert.equal(receipt.status,"ok"); assert.equal(receipt.result.id,uuid(7));
});

test("exact retries and lost responses recover the same durable receipt without another financial batch", () => {
  const rt=setup();
  const original=call(rt,"save_schedule",schedule);
  const count=rt.batchCalls();
  assert.deepEqual(call(rt,"save_schedule",schedule).result,original.result);
  assert.equal(rt.batchCalls(),count);
  assert.deepEqual(call(rt,"receipt",{request_id:schedule.request_id}).result,original.result);
  assert.equal(rt.batchCalls(),count);
  const changed=call(rt,"save_schedule",{...schedule,first_due_date:"2026-02-01"});
  assert.equal(changed.status,"invalid"); assert.equal(rt.batchCalls(),count);
  call(rt,"record_payment",payment());
  const paymentCount=rt.batchCalls();
  assert.equal(call(rt,"record_payment",payment()).status,"ok");
  assert.equal(rt.batchCalls(),paymentCount);
  const other=event("receipt",{request_id:uuid(6)});
  const parsed=JSON.parse(other.postData.contents); const body=JSON.parse(parsed.payload); body.owner_id="987654321"; parsed.payload=JSON.stringify(body);parsed.signature=hmac(parsed.payload);other.postData.contents=JSON.stringify(parsed);
  assert.equal(inner(rt.context.doPost(other)).status,"invalid");
  assert.equal(rt.batchCalls(),paymentCount,"unallowlisted owner must be rejected before any batch");
});

test("manual confirmation, overpayment, stale charge revision and immutable corrections reject before persistence", () => {
  const rt=setup(); call(rt,"save_schedule",schedule); const count=rt.batchCalls();
  assert.equal(call(rt,"record_payment",{...payment(),confirmed_received:false}).status,"invalid");
  assert.equal(call(rt,"record_payment",{...payment(),payment:{...payment().payment,amount_minor:3335}}).status,"invalid");
  assert.equal(call(rt,"record_payment",{...payment(),expected_charge_revision:9}).status,"conflict");
  assert.equal(rt.batchCalls(),count);
  const receipt=call(rt,"record_payment",payment());assert.equal(receipt.status,"ok");
  const paidCount=rt.batchCalls();
  assert.equal(call(rt,"record_payment",{...payment(),request_id:uuid(10)}).status,"conflict");
  assert.equal(rt.batchCalls(),paidCount);
  const changedDeal={...deal,amount_minor:9999};
  assert.equal(call(rt,"save_deal",{request_id:uuid(11),expected_revision:2,deal:changedDeal}).status,"invalid");
  assert.equal(rt.batchCalls(),paidCount,"a paid/scheduled deal cannot silently rewrite financial history");
  call(rt,"record_expense",expense());const expenseCount=rt.batchCalls();
  assert.equal(call(rt,"record_expense",{...expense(),request_id:uuid(12),expected_revision:1}).status,"invalid");
  assert.equal(rt.batchCalls(),expenseCount);
});

test("accepted financial history detects raw edits or deletion without silently changing cash", () => {
  for(const mutate of [rows=>{rows[1][4]=999;},rows=>{rows.pop();}]) {
    const rt=setup();call(rt,"save_schedule",schedule);call(rt,"record_payment",payment());
    const count=rt.batchCalls();mutate(rt.sheets.Payments.getDataRange().getValues());
    assert.equal(call(rt,"finance_snapshot",{}).status,"conflict");
    assert.equal(rt.batchCalls(),count,"read-only conflict detection cannot mutate Sheets");
  }
});

test("projects are revisioned and project-linked expenses require an accepted target", () => {
  const rt=setup();const project={request_id:uuid(20),expected_revision:0,project:{id:uuid(21),title:"פרויקט סינתטי",status:"active",deadline:"2026-12-31",notes:""}};
  assert.equal(call(rt,"save_project",project).result.revision,1);
  assert.equal(call(rt,"save_project",{...project,request_id:uuid(22),expected_revision:1,project:{...project.project,status:"completed"}}).result.revision,2);
  const linked={...expense(),expense:{...expense().expense,project_id:uuid(21)}};
  assert.equal(call(rt,"record_expense",linked).status,"ok");
  const count=rt.batchCalls();
  assert.equal(call(rt,"save_project",{...project,request_id:uuid(23)}).status,"conflict");
  assert.equal(call(rt,"record_expense",{...expense(),request_id:uuid(24),expense:{...expense().expense,id:uuid(25),project_id:uuid(26)}}).status,"invalid");
  assert.equal(rt.batchCalls(),count);
});

test("incomplete or oversized financial sources fail closed and cannot claim complete zero counts", () => {
  const rt=setup(); const snapshot=call(rt,"finance_snapshot",{});
  assert.equal(snapshot.result.coverage,"unknown");
  assert.ok(Object.values(snapshot.result.totals).every(value=>value===null));
  const rows=rt.sheets.Projects.getDataRange().getValues();
  while(rows.length<502) rows.push([]);
  const count=rt.batchCalls();
  assert.equal(call(rt,"finance_snapshot",{}).status,"unavailable");
  assert.equal(rt.batchCalls(),count);
});

test("deal create and edit cannot bypass the combined financial response capacity", () => {
  const rt=setup();
  for(let i=100;i<181;i+=1) {
    assert.equal(call(rt,"save_project",{request_id:uuid(i+1000),expected_revision:0,project:{id:uuid(i),title:"פרויקט",status:"active",deadline:null,notes:"漢".repeat(1000)}}).status,"ok");
  }
  assert.equal(call(rt,"finance_snapshot",{}).status,"ok");
  const acceptedCount=rt.sheets.Audit.getDataRange().getValues().length;
  const original=JSON.stringify(rt.sheets.Deals.getDataRange().getValues());
  for(const edit of [false,true]) {
    const value={...deal,id:edit?DEAL_ID:uuid(999),title:"漢".repeat(160),notes:"漢".repeat(1000)};
    const rejected=call(rt,"save_deal",{request_id:uuid(edit?1998:1999),expected_revision:edit?1:0,deal:value});
    assert.equal(rejected.status,"conflict");
    assert.equal(rejected.result.reason_code,"snapshot_capacity_exceeded");
    assert.equal(JSON.stringify(rt.sheets.Deals.getDataRange().getValues()),original,"rejected deal must not alter accepted records");
    assert.equal(rt.sheets.Audit.getDataRange().getValues().length,acceptedCount,"no committed receipt is created for rejected writes");
    assert.equal(call(rt,"finance_snapshot",{}).status,"ok","the previously readable financial snapshot stays readable");
  }
});
