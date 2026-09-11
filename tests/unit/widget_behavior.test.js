const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/ask_mia.js', 'utf8');
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.attributes = {}; this.listeners = {}; this.style = {}; this.hidden = false;
    this.value = ''; this.textContent = ''; this.className = ''; this.disabled = false;
    this.classList = {
      add: (...ns) => {this.className = [...new Set([...this.className.split(' '), ...ns])].join(' ');},
      remove: (...ns) => {this.className = this.className.split(' ').filter(n => !ns.includes(n)).join(' ');},
      contains: n => this.className.split(' ').includes(n),
      toggle: (n, on) => {if (on) this.classList.add(n); else this.classList.remove(n);},
    };
  }
  get firstChild() {return this.children[0] || null;}
  get lastElementChild() {return this.children.at(-1) || null;}
  appendChild(c) {c.parentNode = this; this.children.push(c); return c;}
  removeChild(c) {this.children = this.children.filter(x => x !== c); c.parentNode = null; return c;}
  setAttribute(k,v) {this.attributes[k] = String(v); if (k === 'id') this.id = String(v);}
  removeAttribute(k) {delete this.attributes[k];}
  getAttribute(k) {return this.attributes[k] ?? null;}
  hasAttribute(k) {return Object.hasOwn(this.attributes, k);}
  addEventListener(t,fn) {(this.listeners[t] ||= []).push(fn);}
  dispatchEvent(e) {e.target ||= this; for (const fn of this.listeners[e.type] || []) fn(e);}
  click() {if (!this.disabled) this.dispatchEvent({type: 'click', preventDefault() {}});}
  focus() {this.focused = true;}
  matches(s) {
    if (s.startsWith('#')) return this.id === s.slice(1);
    if (s.startsWith('.')) return this.className.split(' ').includes(s.slice(1));
    if (s.startsWith('[')) return this.hasAttribute(s.slice(1,-1).split('=')[0]);
    return this.tagName.toLowerCase() === s.toLowerCase();
  }
  querySelectorAll(s) {
    const parts = s.split(' '), out = [];
    const walk = e => {for (const c of e.children) {
      if (c.matches(parts.at(-1)) && (parts.length === 1 || c.parentNode.closest(parts[0]))) out.push(c);
      walk(c);
    }}; walk(this); return out;
  }
  querySelector(s) {return this.querySelectorAll(s)[0] || null;}
  closest(s) {let e = this; while (e) {if (e.matches(s)) return e; e = e.parentNode;} return null;}
}
const allText = e => e.textContent + e.children.map(allText).join('');
async function settle() {for (let i = 0; i < 5; i++) await new Promise(setImmediate);}
function world({storage = new Map(), apple = false, rejectTimeslice = false, sessionResponse = null, inlineHost = false} = {}) {
  const document = new Element('document');
  document.head = new Element('head'); document.body = new Element('body');
  document.appendChild(document.head); document.appendChild(document.body);
  let inlineMount = null;
  if (inlineHost) {
    inlineMount = new Element('div');
    inlineMount.setAttribute('data-mia-inline', '');
    document.body.appendChild(inlineMount);
  }
  document.createElement = tag => new Element(tag);
  document.createElementNS = (_,tag) => new Element(tag);
  document.getElementById = id => document.querySelector('#' + id);
  document.currentScript = new Element('script');
  document.currentScript.src = 'https://test.invalid/v1/website/widget.js';
  document.currentScript.setAttribute('data-mia-api', 'https://test.invalid');
  document.currentScript.setAttribute('data-mia-page-section', 'voice-agent');
  document.referrer = ''; document.visibilityState = 'visible';
  const window = new Element('window'); window.open = () => {};
  const state = {document, window, storage, inlineMount, calls: [], recorders: [], streams: [], timers: new Map(),
    rejectContact: false, expireContact: false, sessionCount: 0, stopDeferred: false,
    contactUrl: 'https://wa.me/972501234567', deferHandoff: false, unauthorizedMessages: 0};
  class Recorder {
    static isTypeSupported() {return true;}
    constructor(stream, options = {}) {this.stream = stream; this.mimeType = options.mimeType; this.state = 'inactive'; this.starts = []; state.recorders.push(this);}
    start(ms) {this.starts.push(ms); if (ms && rejectTimeslice) throw Error('timeslice refused'); this.state = 'recording';}
    emit(text) {this.ondataavailable({data: new Blob([text], {type: this.mimeType})});}
    requestData() {}
    stop() {this.state = 'inactive'; if (!state.stopDeferred) queueMicrotask(() => this.onstop());}
  }
  class FormData {constructor() {this.parts = [];} append(name,value,filename) {this.parts.push({name,value,filename});}}
  const ok = data => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(data)});
  let timerId = 0;
  const context = {
    document, window, navigator: {userAgent: apple ? 'iPhone Safari' : 'Chrome', mediaDevices: {
      getUserMedia: () => {const stream = {stopped: false}; stream.getTracks = () => [{stop() {stream.stopped = true;}}]; state.streams.push(stream); return Promise.resolve(stream);},
    }}, location: {search: '', pathname: '/voice'},
    localStorage: {getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k)},
    fetch(url, options = {}) {
      state.calls.push({url, options});
      if (url.endsWith('/config')) return ok({opening: 'שלום', session_lifetime_ms: 86400000});
      if (url.includes('/sessions?')) {
        state.sessionCount++;
        return ok(sessionResponse || {
          session_id: '12345678-1234-4234-8234-' + String(state.sessionCount).padStart(12,'0'),
          session_credential: 'credential-' + state.sessionCount,
        });
      }
      if (url.includes('/messages')) {
        if (state.unauthorizedMessages > 0) {
          state.unauthorizedMessages--;
          return Promise.resolve({ok: false, status: 401, json: () => Promise.resolve({})});
        }
        const payload = JSON.parse(options.body);
        if (state.expireContact && (payload.phone || payload.email)) {state.expireContact = false; return Promise.resolve({ok: false, status: 404, json: () => Promise.resolve({})});}
        if ((payload.phone || payload.email) && !state.rejectContact) return ok({message: 'הפרטים התקבלו', next_action: 'confirm_contact', whatsapp_url: state.contactUrl});
        return ok({message: 'השאירו טלפון או אימייל', next_action: 'ask_contact'});
      }
      if (url.endsWith('/voice')) return ok({message: 'אפשר להמשיך', next_action: 'answer', heard: 'שלום בעברית'});
      if (url.endsWith('/handoff')) return state.deferHandoff
        ? new Promise(resolve => {state.resolveHandoff = () => resolve(ok({notification_status: 'delivered'}));})
        : ok({notification_status: 'delivered'});
      if (url.endsWith('/end')) return ok({accepted: true, finalized: true});
      return ok({accepted: true});
    },
    URL, URLSearchParams, FormData, Blob, AbortSignal, MediaRecorder: Recorder,
    setTimeout: (fn,ms) => {const id = ++timerId; state.timers.set(id,{fn,ms}); return id;},
    clearTimeout: id => state.timers.delete(id), console,
  };
  vm.runInNewContext(source, context);
  state.el = id => document.getElementById('ask-mia-' + id);
  state.open = async () => {state.el('launcher').click(); await settle();};
  state.send = async text => {
    state.el('input').value = text; state.el('send').click(); await settle();
    for (const [id,timer] of [...state.timers]) if (timer.ms <= 1000) {state.timers.delete(id); timer.fn();}
    await settle();
  };
  state.submit = async ({name = '', phone = '', email = ''} = {}) => {
    const form = document.querySelector('.ask-mia-contact'); assert.ok(form);
    for (const input of form.querySelectorAll('input')) input.value = ({name,phone,email})[input.name];
    form.dispatchEvent({type: 'submit', preventDefault() {}}); await settle(); return form;
  };
  return state;
}
async function main() {
  // Floating mode stays the default: no inline host means launcher on body, panel closed.
  const floating = world(); await settle();
  assert.equal(floating.document.getElementById('ask-mia-root').parentNode, floating.document.body);
  assert.equal(floating.el('panel').hidden, true, 'floating panel starts closed');
  assert.equal(floating.sessionCount, 0, 'floating mode does not open a session before the launcher is clicked');

  // Inline mode: mounts into the page container, always open, no launcher, cannot be closed.
  const boxed = world({inlineHost: true}); await settle();
  const boxedRoot = boxed.document.getElementById('ask-mia-root');
  assert.equal(boxedRoot.parentNode, boxed.inlineMount, 'inline widget mounts into [data-mia-inline]');
  assert.ok(boxedRoot.classList.contains('ask-mia-inline'));
  assert.equal(boxed.el('panel').hidden, false, 'inline panel is open with no launcher click');
  assert.equal(boxed.el('panel').getAttribute('role'), 'region');
  assert.equal(boxed.el('panel').hasAttribute('aria-modal'), false, 'an always-open region is not a modal');
  assert.equal(boxed.sessionCount, 1, 'inline mode starts its session on mount');
  // Escape and the close button must not strand the visitor with no way to reopen.
  boxed.document.dispatchEvent({type: 'keydown', key: 'Escape', preventDefault() {}});
  await settle();
  assert.equal(boxed.el('panel').hidden, false, 'Escape does not close the inline box');
  boxed.el('close').click(); await settle();
  assert.equal(boxed.el('panel').hidden, false, 'the close button does not close the inline box');

  const accessible = world(); await accessible.open();
  assert.equal(accessible.el('panel').getAttribute('role'), 'dialog');
  assert.equal(accessible.el('panel').getAttribute('aria-labelledby'), 'ask-mia-title');
  assert.equal(accessible.el('transcript').getAttribute('role'), 'log');
  assert.equal(accessible.el('transcript').getAttribute('aria-live'), 'polite');
  assert.ok(allText(accessible.el('header')).includes('עוזרת AI של אסף'));
  const css = accessible.document.head.children[0].textContent;
  assert.ok(css.includes('width: min(400px, calc(100vw - 24px))'));
  assert.ok(css.includes('--ask-mia-viewport-height: 100dvh'));
  assert.ok(css.includes('overflow-x: hidden'));
  assert.equal(css.includes('.whatsapp-fab'), false, 'widget must not restyle host WhatsApp controls');
  assert.equal(css.includes('\n    .ask-mia-'), false, 'component classes stay rooted under #ask-mia-root');
  accessible.document.dispatchEvent({type: 'keydown', key: 'Escape', preventDefault() {}});
  await settle();
  assert.equal(accessible.el('panel').hidden, true, 'Escape closes the panel');
  assert.equal(accessible.el('launcher').focused, true, 'closing returns focus to launcher');

  const w = world(); await w.open(); await w.send('אני צריכה עזרה');
  const form = await w.submit();
  assert.equal(w.calls.filter(c => c.url.includes('/messages')).length, 1, 'empty form never posts');
  w.rejectContact = true; await w.submit({phone: 'bad'});
  assert.equal(form.hidden, false, 'rejected contact stays visible');
  assert.equal(form.querySelector('button').disabled, false, 'rejected contact can be corrected');
  w.rejectContact = false; await w.submit({name: 'דנה', email: 'dana@example.test'});
  const payload = JSON.parse(w.calls.filter(c => c.url.includes('/messages')).at(-1).options.body);
  assert.equal(payload.text, 'רוצה להמשיך עם אסף'); assert.equal(payload.email, 'dana@example.test');
  assert.equal(payload.phone, ''); assert.equal(payload.name, 'דנה');
  assert.equal(allText(w.el('transcript')).includes('dana@example.test'), false);
  const cta = w.document.querySelector('.ask-mia-handoff-cta'); assert.ok(cta, 'confirmation immediately paints CTA');
  assert.equal(cta.href, 'https://wa.me/972501234567'); cta.click(); await settle();
  assert.equal(w.storage.has('askMia.sessionId'), true, 'delivered handoff keeps the resumable session');
  await w.send('שיחה חדשה'); assert.equal(w.sessionCount, 1, 'conversation continues after handoff');
  assert.equal(allText(w.el('transcript')).includes('אני צריכה עזרה'), true, 'continued chat keeps its transcript');
  const expiredContact = world(); await expiredContact.open(); await expiredContact.send('צריכה עזרה');
  expiredContact.expireContact = true; await expiredContact.submit({email: 'dana@example.test'});
  assert.equal(expiredContact.sessionCount, 2, 'expired contact session retries with a new session');
  assert.ok(expiredContact.document.querySelector('.ask-mia-handoff-cta'));
  const delayed = world(); await delayed.open(); await delayed.send('השיחה הישנה');
  await delayed.submit({phone: '0501234567'}); delayed.deferHandoff = true;
  delayed.document.querySelector('.ask-mia-handoff-cta').click(); await settle();
  await delayed.send('שיחה חדשה');
  assert.equal(delayed.sessionCount, 1, 'pending handoff holds queued send');
  delayed.resolveHandoff(); await settle();
  for (const [id,timer] of [...delayed.timers]) if (timer.ms <= 1000) {delayed.timers.delete(id); timer.fn();}
  await settle();
  assert.equal(delayed.sessionCount, 1, 'queued send continues the resumable session');
  assert.equal(allText(delayed.el('transcript')).includes('השיחה הישנה'), true);
  const queued = delayed.calls.filter(c => c.url.includes('/messages')).at(-1);
  assert.ok(queued.url.includes('12345678-1234-4234-8234-000000000001'));
  assert.equal(JSON.parse(queued.options.body).text, 'שיחה חדשה');
  for (const url of ['', 'https://evil.example/972501234567']) {
    const missing = world(); missing.contactUrl = url;
    await missing.open(); await missing.send('צריכה עזרה'); await missing.submit({phone: '0501234567'});
    assert.equal(missing.document.querySelector('.ask-mia-handoff-cta'), null);
    assert.equal(missing.el('status').textContent, 'וואטסאפ לא זמין כרגע.');
  }
  for (const apple of [false, true]) {
    const v = world({apple}); await v.open(); v.el('mic').click(); await settle();
    const recorder = v.recorders.at(-1); assert.deepEqual(recorder.starts, [1000]);
    recorder.emit('one'); recorder.emit('two'); v.el('mic').click(); await settle();
    const upload = v.calls.find(c => c.url.endsWith('/voice')); assert.ok(upload, 'widget uploads recorded bytes');
    const file = upload.options.body.parts[0]; assert.equal(await file.value.text(), 'onetwo');
    assert.equal(file.filename, apple ? 'note.mp4' : 'note.webm'); assert.equal(v.streams[0].stopped, true);
    assert.ok(allText(v.el('transcript')).includes('שלום בעברית'));
  }
  const fallback = world({apple: true, rejectTimeslice: true}); await fallback.open();
  fallback.el('mic').click(); await settle(); assert.deepEqual(fallback.recorders[0].starts, [1000, undefined]);
  fallback.el('mic').click(); await settle();
  assert.equal(fallback.calls.some(c => c.url.endsWith('/voice')), false, 'no chunks never uploads');
  assert.ok(fallback.calls.some(c => c.options.body && String(c.options.body).includes('no_chunks')), 'no_chunks telemetry');
  const active = new Map([
    ['askMia.sessionId', 'web_0123456789abcdef'],
    ['askMia.sessionMeta', JSON.stringify({updatedAt: Date.now(), credential: 'legacy-credential'})],
    ['askMia.transcript', JSON.stringify([{role: 'user', text: 'active conversation'}])],
  ]);
  const resumed = world({storage: new Map(active)}); await resumed.open();
  assert.equal(resumed.sessionCount, 0); assert.ok(allText(resumed.el('transcript')).includes('active conversation'));
  await resumed.send('Continue');
  assert.equal(resumed.calls.find(c => c.url.includes('/messages')).options.headers['X-Mia-Session-Credential'], 'legacy-credential');
  const uuidSession = '12345678-1234-4234-8234-000000000001';
  assert.equal(w.storage.get('askMia.sessionId'), uuidSession, 'actual API UUID shape is retained');
  const uuidResumed = world({storage: new Map(w.storage)}); await uuidResumed.open();
  assert.equal(uuidResumed.sessionCount, 0, 'credentialed UUID reload keeps the same session');
  await uuidResumed.send('Continue UUID');
  const uuidRequest = uuidResumed.calls.find(c => c.url.includes('/messages'));
  assert.ok(uuidRequest.url.includes(uuidSession));
  assert.equal(uuidRequest.options.headers['X-Mia-Session-Credential'], 'credential-1');
  for (const credential of [undefined, '', '   ']) {
    const storage = new Map(active);
    storage.set('askMia.sessionMeta', JSON.stringify({updatedAt: Date.now(), credential}));
    const cutover = world({storage}); await cutover.open();
    assert.equal(cutover.sessionCount, 1, 'credentialless legacy session is replaced');
    assert.equal(allText(cutover.el('transcript')).includes('active conversation'), false);
    await cutover.send('New authenticated turn');
    assert.equal(cutover.calls.find(c => c.url.includes('/messages')).options.headers['X-Mia-Session-Credential'], 'credential-1');
  }
  const retryAuth = world(); await retryAuth.open(); retryAuth.unauthorizedMessages = 1;
  await retryAuth.send('Retry the same message');
  const retried = retryAuth.calls.filter(c => c.url.includes('/messages'));
  assert.equal(retryAuth.sessionCount, 2);
  assert.equal(retried.length, 2);
  assert.equal(JSON.parse(retried[0].options.body).client_message_id, JSON.parse(retried[1].options.body).client_message_id);
  assert.equal(retried[1].options.headers['X-Mia-Session-Credential'], 'credential-2');
  const deniedAgain = world(); await deniedAgain.open(); deniedAgain.unauthorizedMessages = 3;
  await deniedAgain.send('Bounded retry');
  assert.equal(deniedAgain.sessionCount, 2);
  assert.equal(deniedAgain.calls.filter(c => c.url.includes('/messages')).length, 2, '401 retries only once');
  for (const response of [
    {session_id: '../owner', session_credential: 'x'},
    {session_id: uuidSession, session_credential: ''},
  ]) {
    const invalid = world({sessionResponse: response}); await invalid.open(); await invalid.send('Hello');
    assert.equal(invalid.calls.some(c => c.url.includes('/messages')), false);
    assert.equal(invalid.storage.has('askMia.sessionId'), false);
  }
  for (const meta of [null, JSON.stringify({updatedAt: 1})]) {
    const storage = new Map(active); if (meta) storage.set('askMia.sessionMeta', meta); else storage.delete('askMia.sessionMeta');
    const expired = world({storage}); await expired.open(); assert.equal(expired.sessionCount, 1);
    assert.equal(allText(expired.el('transcript')).includes('active conversation'), false);
  }
  process.stdout.write('widget behavioral checks passed\n');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
