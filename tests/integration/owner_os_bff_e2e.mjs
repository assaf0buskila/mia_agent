// Real local Node BFF -> FastAPI -> disposable PostgreSQL; no Telegram network.
import assert from 'node:assert/strict';
import http from 'node:http';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import {createServer} from '../../integrations/assafweb-os/bff.mjs';

const [backend, frontendRoot] = process.argv.slice(2);
let input='';for await(const chunk of process.stdin)input+=chunk;
const {proof}=JSON.parse(input);
const origin='https://os.example.test', jar=new Map();
const {OwnerClient}=await import(pathToFileURL(path.join(frontendRoot,'client.mjs')));
const config={origin,backend,frontendRoot,telegramBotUsername:'SyntheticOwnerBot',telegramBotId:'123456',localTest:true};
const server=await createServer(config);
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let port=server.address().port;

function transport(route,options={}) {
  return new Promise((resolve,reject)=>{
    const headers={...options.headers,Host:options.host||'os.example.test','Sec-Fetch-Site':options.fetchSite||'same-origin'};
    if(options.method==='POST') headers.Origin=options.origin||origin;
    if(jar.size) headers.Cookie=[...jar].map(([k,v])=>`${k}=${v}`).join('; ');
    const request=http.request({hostname:'127.0.0.1',port,path:route,method:options.method||'GET',headers},response=>{
      for(const value of response.headers['set-cookie']||[]) {
        assert.match(value,/HttpOnly/i);assert.match(value,/Secure/i);assert.doesNotMatch(value,/Domain=/i);
        const [key,val]=value.split(';',1)[0].split('=');
        if(val && val!=='""' && !/Max-Age=0(?:;|$)/i.test(value)) jar.set(key,val);
        else jar.delete(key);
      }
      const chunks=[];response.on('data',chunk=>chunks.push(chunk));response.on('end',()=>{
        resolve(new Response(response.statusCode===204?null:Buffer.concat(chunks),{status:response.statusCode,headers:response.headers}));
      });
    });
    request.on('error',reject);request.end(options.body);
  });
}

try {
  const client=new OwnerClient(transport);
  assert.equal((await transport('/auth/start',{method:'POST',origin:'https://evil.example.test'})).status,403);
  assert.equal((await transport('/api/contacts',{fetchSite:'cross-site'})).status,403);
  assert.equal((await transport('/api/contacts',{host:'evil.example.test'})).status,403);
  await assert.rejects(client.session(),error=>error.code==='unauthorized');
  const start=await transport('/auth/start',{method:'POST'});
  assert.equal((await start.json()).authorization_url,origin+'/auth/telegram');
  const html=await transport('/auth/telegram');
  assert.equal(html.status,200);assert.ok(!html.headers.get('content-security-policy').includes('unsafe-eval'));
  const page=await html.text();assert.ok(page.includes('Telegram.Login.auth'));assert.ok(!page.includes('data-onauth'));
  const challenge=await transport('/auth/challenge');
  assert.equal(challenge.status,200);
  const login=await transport('/auth/login',{method:'POST',headers:{'Content-Type':'application/json',
    'X-CSRF-Token':(await challenge.json()).csrf_token},body:JSON.stringify({login_proof:proof})});
  assert.equal(login.status,200,await login.clone().text());
  await client.session();
  for(const kind of ['today','contacts','reminders']) {
    const result=await client.read(kind);assert.equal(result.source,'mia_postgresql');assert.equal(result.synthetic,false);
    if(kind==='reminders'){assert.equal(result.records.length,1);assert.ok(result.records[0].title.endsWith('…'));assert.ok(result.records[0].title.length<=500);}
  }
  const detail=await client.read('contact','seed_contact');assert.equal(detail.record.contact_id,'seed_contact');
  assert.equal(detail.record.next_action.due_date,null);
  const assets=await transport('/styles.css');assert.equal(assets.status,200);
  const blocked=await transport('/tests/fixtures/owner-os.json');assert.equal(blocked.status,404);
  // A fresh BFF instance shares no Map/session cache. Mia's persisted cookie is sufficient.
  const second=await createServer(config);await new Promise(resolve=>second.listen(0,'127.0.0.1',resolve));
  port=second.address().port;
  try {await client.session();await client.read('contacts');await client.signOut();
    await assert.rejects(client.session(),error=>error.code==='unauthorized');}
  finally {await new Promise(resolve=>second.close(resolve));}
  process.stdout.write('PASS frozen OwnerClient/DTO -> Node BFF -> FastAPI -> PostgreSQL; two-instance session, logout, no fixture serving\n');
} finally {await new Promise(resolve=>server.close(resolve));}
