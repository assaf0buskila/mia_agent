// Session adapter for the frozen mobile assets. Mia owns sessions and all records.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {randomBytes} from 'node:crypto';

const assets = new Set(['index.html', 'styles.css', 'app.mjs', 'contracts.mjs', 'views.mjs', 'client.mjs']);
const security = {'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff',
  'X-Frame-Options':'DENY', 'Referrer-Policy':'no-referrer', 'X-Robots-Tag':'noindex, nofollow',
  'Vary':'Cookie, Origin', 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"};
const cookieNames = new Set(['__Host-mia-owner', '__Host-mia-owner-challenge']);
const safeId = value => /^[A-Za-z0-9_-]{1,128}$/.test(value);
const fail = (status, code) => Object.assign(new Error(code), {status, code});
const send = (res, status, data, extra={}) => {
  res.writeHead(status, {...security, 'Content-Type':'application/json; charset=utf-8', ...extra});
  res.end(status === 204 ? undefined : JSON.stringify(data));
};

function cookies(req) {
  const values = new Map();
  for (const part of (req.headers.cookie || '').split(';')) {
    const [key, ...rest] = part.trim().split('=');
    if (!cookieNames.has(key)) continue;
    if (values.has(key)) throw fail(400, 'invalid_cookie');
    const value = rest.join('=');
    if (!/^[A-Za-z0-9_-]{1,256}$/.test(value)) throw fail(400, 'invalid_cookie');
    values.set(key, value);
  }
  return [...values].map(([key, value]) => `${key}=${value}`).join('; ');
}

async function boundedBody(req) {
  let size = 0; const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 16384) throw fail(413, 'validation_error');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

export async function createServer({origin, backend, frontendRoot, telegramBotUsername='', telegramBotId='', localTest=false}) {
  const appURL = new URL(origin), upstream = new URL(backend);
  if (appURL.protocol !== 'https:' || appURL.origin !== origin || appURL.username || appURL.password ||
      upstream.username || upstream.password || upstream.search || upstream.hash || upstream.pathname !== '/' ||
      !(upstream.protocol === 'https:' || (localTest && upstream.protocol === 'http:' && ['127.0.0.1','localhost'].includes(upstream.hostname))))
    throw fail(503, 'integration_unavailable');
  const contractPath = path.resolve(frontendRoot, 'contracts.mjs');
  const {readEnvelope, safeError} = await import('file:///' + contractPath.replaceAll('\\', '/'));

  async function forward(req, res, route, {kind, body}={}) {
    const headers = {'Accept':'application/json', 'Origin':origin};
    const cookie = cookies(req); if (cookie) headers.Cookie = cookie;
    if (req.headers['x-csrf-token']) headers['X-CSRF-Token'] = req.headers['x-csrf-token'];
    if (body?.length) headers['Content-Type'] = req.headers['content-type'] || '';
    let response;
    try { response = await fetch(upstream.origin + route, {method:req.method, headers, body:body?.length ? body : undefined,
      redirect:'error', signal:AbortSignal.timeout(8000), cache:'no-store'}); }
    catch { throw fail(503, 'source_unavailable'); }
    if (response.status !== 204 && !response.headers.get('content-type')?.includes('application/json'))
      throw fail(502, 'invalid_response');
    const chunks=[]; let size=0;
    for await (const chunk of response.body || []) {
      size+=chunk.length;
      if(size>262144) throw fail(502, 'invalid_response');
      chunks.push(chunk);
    }
    let result;
    try { result = response.status === 204 ? null : JSON.parse(Buffer.concat(chunks).toString('utf8')); }
    catch { throw fail(502, 'invalid_response'); }
    if (!response.ok) throw fail(response.status, safeError(result?.error?.code || result?.detail));
    if (kind) result = readEnvelope(result, kind, Date.now(), !new URL(route, origin).searchParams.has('cursor') &&
      !new URL(route, origin).searchParams.has('next_actions_cursor'));
    const setCookies = response.headers.getSetCookie();
    if (setCookies.some(value => !cookieNames.has(value.split('=',1)[0]) || /;\s*Domain=/i.test(value) ||
      !/;\s*Secure(?:;|$)/i.test(value) || !/;\s*HttpOnly(?:;|$)/i.test(value) || !/;\s*Path=\//i.test(value)))
      throw fail(502, 'invalid_response');
    // Frozen OwnerClient expects a JSON logout acknowledgement.
    send(res, response.status === 204 ? 200 : response.status,
      response.status === 204 ? {status:'signed_out'} : result,
      setCookies.length ? {'Set-Cookie':setCookies} : {});
  }

  return http.createServer(async (req, res) => {
    try {
      if (req.headers.host !== appURL.host) throw fail(403, 'forbidden');
      const url = new URL(req.url, origin);
      if (url.origin !== origin) throw fail(403, 'forbidden');
      const api = url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/');
      if (api && req.method === 'POST' && (req.headers.origin !== origin || req.headers['sec-fetch-site'] === 'cross-site'))
        throw fail(403, 'forbidden');
      if (api && req.method === 'GET' && url.pathname !== '/auth/telegram' &&
          req.headers.origin !== origin && req.headers['sec-fetch-site'] !== 'same-origin') throw fail(403, 'forbidden');
      if (url.pathname === '/auth/start' && req.method === 'POST') {
        if (url.search || !/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(telegramBotUsername) || !/^[1-9][0-9]{4,15}$/.test(telegramBotId)) throw fail(503, 'integration_unavailable');
        return send(res, 200, {authorization_url:origin + '/auth/telegram'});
      }
      if (url.pathname === '/auth/telegram' && req.method === 'GET') {
        if (url.search || !/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(telegramBotUsername) || !/^[1-9][0-9]{4,15}$/.test(telegramBotId)) throw fail(503, 'integration_unavailable');
        const nonce=randomBytes(24).toString('base64url');
        const html=`<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>כניסת בעלים</title><h1>כניסה לסביבת העבודה</h1><p id="state">מתכוננים לכניסה</p><div id="login"></div><script nonce="${nonce}">
let csrf;
window.ownerTelegramLogin=async data=>{try{const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({login_proof:JSON.stringify(data)}),cache:'no-store'});if(!r.ok)throw Error();location.replace('/');}catch{document.getElementById('state').textContent='הכניסה לא הושלמה. יש לנסות שוב.';}};
(async()=>{try{const r=await fetch('/auth/challenge',{cache:'no-store'});if(!r.ok)throw Error();csrf=(await r.json()).csrf_token;const s=document.createElement('script');s.src='https://telegram.org/js/telegram-widget.js?22';s.onload=()=>{const b=document.createElement('button');b.textContent='כניסה עם Telegram';b.onclick=()=>window.Telegram.Login.auth({bot_id:Number('${telegramBotId}')},user=>{if(user)window.ownerTelegramLogin(user);});document.getElementById('login').append(b);};document.getElementById('login').append(s);document.getElementById('state').textContent='יש לאמת את חשבון Telegram של הבעלים';}catch{document.getElementById('state').textContent='הכניסה אינה זמינה כרגע';}})();
</script></html>`;
        res.writeHead(200,{...security,'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':
          `default-src 'self'; script-src 'nonce-${nonce}' https://telegram.org; frame-src https://oauth.telegram.org; connect-src 'self' https://oauth.telegram.org; img-src 'self' data: https://telegram.org; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`});
        return res.end(html);
      }
      const authRoutes={'/auth/challenge':['GET','challenge'],'/auth/login':['POST','login'],
        '/api/session':['GET','session'],'/api/logout':['POST','logout'],'/api/revoke-all':['POST','revoke-all']};
      if (authRoutes[url.pathname]) {
        const [method, endpoint] = authRoutes[url.pathname];
        if (req.method !== method || url.search) throw fail(400,'validation_error');
        return await forward(req,res,'/v1/owner/auth/'+endpoint,{body:method==='POST'?await boundedBody(req):undefined});
      }
      const routes={'/api/today':['today','today'],'/api/contacts':['contacts','contacts'],
        '/api/reminders':['reminders','reminders']};
      const detail=url.pathname.match(/^\/api\/contacts\/([A-Za-z0-9_-]{1,64})$/);
      let route=routes[url.pathname]; if(detail) route=['contacts/'+detail[1],'contact'];
      if (route) {
        if(req.method!=='GET') throw fail(405,'operation_unavailable');
        const [endpoint,kind]=route; const params=new URLSearchParams();
        const keys=kind==='today'?['next_actions_cursor']:kind==='contact'?[]:['limit','cursor'];
        for(const key of url.searchParams.keys()) if(!keys.includes(key)||url.searchParams.getAll(key).length!==1) throw fail(400,'validation_error');
        if(kind==='today') params.set('sections','next_actions');
        for(const [key,value] of url.searchParams) {
          if((key==='limit'&&!/^(?:[1-9]|[1-4]\d|50)$/.test(value))||(key!=='limit'&&!safeId(value))) throw fail(400,'validation_error');
          params.set(key,value);
        }
        return await forward(req,res,'/v1/owner/os/'+endpoint+(params.size?'?'+params:''),{kind});
      }
      if(api) throw fail(503,'operation_unavailable'); // action forwarding intentionally off
      if(!['GET','HEAD'].includes(req.method)||url.search) throw fail(404,'operation_unavailable');
      const name=url.pathname.slice(1)||'index.html';
      if(!assets.has(name)) throw fail(404,'operation_unavailable');
      const content=await readFile(path.resolve(frontendRoot,name));
      const types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.mjs':'text/javascript; charset=utf-8'};
      res.writeHead(200,{...security,'Content-Type':types[path.extname(name)]});
      res.end(req.method==='HEAD'?undefined:content);
    } catch(error) { send(res, Number.isInteger(error.status)?error.status:503,
      {error:{code:safeError(error.code)}}); }
  });
}
