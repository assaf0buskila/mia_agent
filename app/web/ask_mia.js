(function () {
  'use strict';
  function resolveWidgetScript() {
    var tagged = document.querySelectorAll('script[data-mia-api]');
    if (tagged.length) return tagged[tagged.length - 1];
    var current = document.currentScript;
    if (current && current.src) return current;
    var bySrc = document.querySelectorAll('script[src*="/v1/website/widget.js"]');
    return bySrc.length ? bySrc[bySrc.length - 1] : null;
  }
  function resolveApiOrigin(script) {
    var explicit = script.getAttribute('data-mia-api');
    if (explicit) return explicit.replace(/\/$/, '');
    if (script.src) return new URL(script.src).origin;
    return '';
  }
  var script = resolveWidgetScript();
  if (!script) return;
  var api = resolveApiOrigin(script);
  if (!api) return;
  var sessionId = null;
  var opened = false;
  var busy = false;
  var recording = false;
  var mediaRecorder = null;
  var audioChunks = [];
  var recordStarted = 0;
  var recordStream = null;
  var MAX_RECORD_MS = 60000;
  var MIC_IDLE = 'הקלטה';
  var MIC_LIVE = 'מקליטה… לחצו שוב לשליחה';
  var MIC_ERR = 'לא שמעתי טוב. נסו שוב או כתבו.';
  var MIC_PERM = 'לא קיבלתי גישה למיקרופון. אפשר גם לכתוב.';
  var MIC_NA = 'ההקלטה לא זמינה כאן. אפשר לכתוב.';
  var ERR = 'משהו השתבש. נסו שוב.';
  var WA_NA = 'וואטסאפ לא זמין כרגע.';
  var eventQueue = [];
  var MAX_QUEUE = 10;
  var seenSections = {};
  var formStates = [];
  var boundForms = [];
  var formAbandonPosted = false;
  var formStartedPosted = false;
  var FORBIDDEN = ['token', 'secret', 'password'];
  var SLUG_RE = /^[a-zA-Z0-9_\-\u0590-\u05FF]+$/;
  var SESSION_KEY = 'askMia.sessionId';
  var TRANSCRIPT_KEY = 'askMia.transcript';
  var SESSION_RE = /^web_[a-f0-9]{16}$/;
  var storedTranscript = [];
  var sessionEnded = false;
  var burstParts = [];
  var burstTimer = 0;
  var BURST_MS = 800;
  // Only populated from the server's config response. Never infer a destination
  // from a Mia reply, a page link, or visitor text.
  var configuredWhatsAppUrl = '';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var MIA_MARK_PATH =
    'M7 23V8h4.2L16 16.8 20.8 8H25v15h-3.4V13.1L16 21.2l-5.6-8.1V23H7z';

  function svgNode(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    var key;
    for (key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, attrs[key]);
      }
    }
    return node;
  }

  function miaMarkSvg() {
    if (typeof document.createElementNS !== 'function') return null;
    var svg = svgNode('svg', { viewBox: '0 0 32 32', focusable: 'false' });
    svg.setAttribute('aria-hidden', 'true');
    svg.appendChild(
      svgNode('path', { fill: 'currentColor', d: MIA_MARK_PATH })
    );
    svg.appendChild(
      svgNode('rect', {
        x: '8',
        y: '25.4',
        width: '16',
        height: '2.2',
        rx: '1.1',
        fill: '#2563eb',
      })
    );
    return svg;
  }

  function paintBrandMark(host) {
    host.setAttribute('aria-hidden', 'true');
    try {
      var svg = miaMarkSvg();
      if (svg && svg.namespaceURI === SVG_NS) {
        host.appendChild(svg);
        return;
      }
    } catch (err) {}
    host.textContent = 'מ';
  }

  function sparkSvg() {
    if (typeof document.createElementNS !== 'function') return null;
    var svg = svgNode('svg', { viewBox: '0 0 32 32', focusable: 'false' });
    svg.setAttribute('aria-hidden', 'true');
    svg.appendChild(
      svgNode('path', {
        fill: 'currentColor',
        d: 'M16 4l2.4 8.1L26 14.5l-7.6 2.4L16 25l-2.4-8.1L6 14.5l7.6-2.4z',
      })
    );
    return svg;
  }

  function paintSpark(host) {
    host.setAttribute('aria-hidden', 'true');
    try {
      var svg = sparkSvg();
      if (svg && svg.namespaceURI === SVG_NS) {
        host.appendChild(svg);
        return;
      }
    } catch (err) {}
    paintBrandMark(host);
  }

  function sendPlaneSvg() {
    if (typeof document.createElementNS !== 'function') return null;
    var svg = svgNode('svg', { viewBox: '0 0 24 24', focusable: 'false' });
    svg.setAttribute('aria-hidden', 'true');
    svg.appendChild(
      svgNode('path', {
        fill: 'currentColor',
        d: 'M3.4 11.2 20.1 4.1c.7-.3 1.4.4 1.1 1.1L14.1 21.8c-.3.7-1.3.7-1.6 0l-2.6-7.1-7.1-2.6c-.7-.3-.7-1.3 0-1.6z',
      })
    );
    return svg;
  }

  var style = document.createElement('style');
  style.textContent =
    '#ask-mia-root{position:fixed;inset-inline-end:1.1rem;bottom:max(1.1rem,env(safe-area-inset-bottom,0px));z-index:9999;display:flex;flex-direction:column-reverse;align-items:flex-end;gap:.65rem;font:16px/1.55 Assistant,system-ui,sans-serif;color:#061b35;color-scheme:light;-webkit-font-smoothing:antialiased}' +
    '#ask-mia-launcher{display:inline-flex;align-items:center;justify-content:center;gap:.45rem;border:1px solid #ffffff59;border-radius:999px;padding-block:0;padding-inline-start:.4rem;padding-inline-end:1.05rem;height:56px;min-height:56px;min-width:56px;background:linear-gradient(135deg,#2f5f93,#2563eb);color:#fff;cursor:pointer;box-shadow:0 18px 44px #2563eb59;transition:transform .16s ease,box-shadow .16s ease;font:inherit;font-weight:800;line-height:1;animation:ask-mia-glow 2.8s ease-in-out infinite}' +
    '#ask-mia-launcher:hover{transform:translateY(-2px)}' +
    '#ask-mia-launcher:focus-visible{outline:2px solid #2563eb;outline-offset:2px}' +
    '#ask-mia-panel[hidden],#ask-mia-wa[hidden]{display:none!important}' +
    '.whatsapp-fab{display:none!important}' +
    '.ask-mia-launch-mark{width:2rem;height:2rem;border-radius:999px;background:#d9eeff;color:#061b35;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:1rem;flex:0 0 auto}' +
    '.ask-mia-launch-mark svg,.ask-mia-avatar svg,.ask-mia-bubble-avatar svg{width:1.2rem;height:1.2rem;display:block}' +
    '#ask-mia-launch-label{white-space:nowrap;font-size:.92rem;font-weight:800;color:#fff}' +
    '#ask-mia-panel{width:min(24rem,calc(100vw - 1.5rem));background:linear-gradient(180deg,#F8FBFF,#eef7ff);color:#061b35;border:1px solid #ffffff59;border-radius:1.35rem;box-shadow:0 28px 70px rgba(6,27,53,.28),0 0 0 1px #2f5f9321,inset 0 1px 0 #ffffff59;display:flex;flex-direction:column;overflow:hidden;isolation:isolate;backdrop-filter:saturate(1.25) blur(18px);-webkit-backdrop-filter:saturate(1.25) blur(18px)}' +
    '#ask-mia-panel:not([hidden]){animation:ask-mia-rise .28s ease}' +
    '#ask-mia-header{display:flex;align-items:center;gap:.7rem;padding:.9rem 1rem;background:linear-gradient(135deg,#061b35,#2f5f93);color:#fff;border-bottom:3px solid #2563eb}' +
    '#ask-mia-close{margin-inline-start:auto;border:0;background:#2f5f93;color:#fff;border-radius:.65rem;padding:.35rem .7rem;min-height:44px;cursor:pointer;font:inherit;font-size:.78rem;font-weight:700}' +
    '#ask-mia-close:focus-visible{outline:2px solid #2563eb;outline-offset:2px}' +
    '.ask-mia-avatar{width:2.25rem;height:2.25rem;border-radius:999px;background:#d9eeff;color:#061b35;display:inline-flex;align-items:center;justify-content:center;font-weight:700;flex:0 0 auto;box-shadow:0 0 0 3px #2563eb59}' +
    '.ask-mia-title{display:flex;align-items:center;gap:.4rem}' +
    '.ask-mia-name{display:block;color:#fff;font-weight:700;font-size:1rem;line-height:1.2;letter-spacing:.01em}' +
    '.ask-mia-live{width:.42rem;height:.42rem;border-radius:999px;background:#2563eb;box-shadow:0 0 0 .22rem #2563eb59;animation:ask-mia-pulse 1.8s ease-in-out infinite}' +
    '.ask-mia-sub{display:block;color:#d9eeff;font-size:.74rem;font-weight:500}' +
    '#ask-mia-transcript{max-height:17.5rem;overflow:auto;padding:1rem;display:flex;flex-direction:column;gap:.85rem}' +
    '.ask-mia-row{display:flex;align-items:flex-end;gap:.5rem;max-width:100%}' +
    '.ask-mia-row-mia{align-self:flex-start}' +
    '.ask-mia-row-user{align-self:flex-end;flex-direction:row-reverse}' +
    '.ask-mia-bubble-avatar{width:2rem;height:2rem;border-radius:999px;flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:.72rem}' +
    '.ask-mia-row-mia .ask-mia-bubble-avatar{background:#d9eeff;color:#061b35}' +
    '.ask-mia-row-user .ask-mia-bubble-avatar{background:#2f5f93;color:#fff}' +
    '.ask-mia-msg{padding:.65rem .8rem;border-radius:.85rem;white-space:pre-wrap;word-break:break-word}' +
    '.ask-mia-mia{background:#eef7ff;color:#061b35;max-width:min(90%,16rem);border:1px solid #2f5f9321;border-end-start-radius:.2rem;box-shadow:0 8px 20px rgba(6,27,53,.06)}' +
    '.ask-mia-user{background:#2f5f93;color:#fff;max-width:min(90%,16rem);border-end-end-radius:.2rem;box-shadow:0 8px 20px rgba(6,27,53,.12)}' +
    '.ask-mia-dots{display:inline-flex;align-items:center;gap:.2rem;height:1.1rem}' +
    '.ask-mia-dots span{width:.35rem;height:.35rem;border-radius:999px;background:#061b35;display:block;animation:ask-mia-bounce .6s ease-in-out infinite}' +
    '.ask-mia-dots span:nth-child(2){animation-delay:.1s}' +
    '.ask-mia-dots span:nth-child(3){animation-delay:.2s}' +
    '@keyframes ask-mia-bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}' +
    '@keyframes ask-mia-pulse{0%,100%{opacity:1}50%{opacity:.45}}' +
    '@keyframes ask-mia-glow{0%,100%{box-shadow:0 18px 44px #2563eb59}50%{box-shadow:0 22px 56px #2563eb59}}' +
    '@keyframes ask-mia-rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}' +
    '#ask-mia-compose{display:flex;flex-direction:column;gap:.45rem;padding:1rem;border-top:1px solid #2f5f9321;background:#F8FBFF}' +
    '#ask-mia-input{resize:vertical;min-height:2.75rem;max-height:8rem;padding:.65rem .75rem;border:1px solid #7ba7d3;border-radius:.85rem;font:inherit;font-size:16px;color:#061b35;background:#fff}' +
    '#ask-mia-input:focus{outline:2px solid #2563eb;outline-offset:1px;border-color:#2563eb}' +
    '#ask-mia-hint{margin:0;font-size:.75rem;color:#2f5f93}' +
    '#ask-mia-actions{display:flex;gap:.35rem;flex-wrap:wrap}' +
    '#ask-mia-actions button{border:0;border-radius:.65rem;padding:.45rem .75rem;min-height:44px;cursor:pointer;font:inherit;color:#061b35}' +
    '#ask-mia-send{background:#2f5f93;color:#fff}' +
    '#ask-mia-mic{background:#d9eeff;color:#061b35}' +
    '#ask-mia-mic.recording{background:#b00;color:#fff}' +
    '#ask-mia-mic:focus-visible{outline:2px solid #2563eb;outline-offset:2px}' +
    '#ask-mia-wa{background:#25d366;color:#fff}' +
    '.ask-mia-handoff{display:flex;flex-direction:column;gap:.45rem;max-width:min(90%,16rem)}' +
    '.ask-mia-handoff-title{font-weight:700;color:#061b35}' +
    '.ask-mia-handoff-note{font-size:.85rem;color:#2f5f93}' +
    '.ask-mia-handoff-cta{display:inline-flex;align-items:center;justify-content:center;' +
    'gap:.35rem;margin-top:.15rem;padding:.5rem .75rem;min-height:44px;width:100%;' +
    'box-sizing:border-box;border-radius:.65rem;cursor:pointer;' +
    'background:#25d366;color:#fff;font-weight:700;text-decoration:none}' +
    '.ask-mia-handoff-cta:focus-visible{outline:2px solid #2563eb;outline-offset:2px}' +
    '#ask-mia-wa.offer{box-shadow:0 0 0 2px #2563eb}' +
    '#ask-mia-status{min-height:1rem;padding:0 .75rem .5rem;color:#b00;font-size:.85rem}' +
    '@media (prefers-reduced-motion:reduce){#ask-mia-launcher,#ask-mia-send,#ask-mia-mic,#ask-mia-wa,#ask-mia-input,#ask-mia-panel:not([hidden]){transition:none;animation:none}#ask-mia-launcher:hover{transform:none}.ask-mia-dots span,.ask-mia-live{animation:none}}';

  var root = document.createElement('div');
  root.id = 'ask-mia-root';

  var launcher = document.createElement('button');
  launcher.id = 'ask-mia-launcher';
  launcher.type = 'button';
  launcher.setAttribute('aria-expanded', 'false');
  launcher.setAttribute('aria-controls', 'ask-mia-panel');
  launcher.setAttribute('aria-label', 'שאלו את מיה');
  var launchMark = document.createElement('span');
  launchMark.className = 'ask-mia-launch-mark';
  paintBrandMark(launchMark);
  var launchLabel = document.createElement('span');
  launchLabel.id = 'ask-mia-launch-label';
  launchLabel.textContent = 'שאלו את מיה';
  launcher.appendChild(launchMark);
  launcher.appendChild(launchLabel);

  var panel = document.createElement('div');
  panel.id = 'ask-mia-panel';
  panel.hidden = true;
  panel.dir = 'rtl';

  var header = document.createElement('div');
  header.id = 'ask-mia-header';
  var avatar = document.createElement('span');
  avatar.className = 'ask-mia-avatar';
  paintBrandMark(avatar);
  var brand = document.createElement('div');
  var titleRow = document.createElement('span');
  titleRow.className = 'ask-mia-title';
  var nameEl = document.createElement('strong');
  nameEl.className = 'ask-mia-name';
  nameEl.textContent = 'מיה';
  var live = document.createElement('span');
  live.className = 'ask-mia-live';
  live.setAttribute('aria-hidden', 'true');
  titleRow.appendChild(nameEl);
  titleRow.appendChild(live);
  var subEl = document.createElement('span');
  subEl.className = 'ask-mia-sub';
  subEl.textContent = 'שאלו. מיה תבין.';
  brand.appendChild(titleRow);
  brand.appendChild(subEl);
  var closeBtn = document.createElement('button');
  closeBtn.id = 'ask-mia-close';
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'סגירה');
  closeBtn.textContent = 'סגירה';
  header.appendChild(avatar);
  header.appendChild(brand);
  header.appendChild(closeBtn);

  var transcript = document.createElement('div');
  transcript.id = 'ask-mia-transcript';

  var compose = document.createElement('div');
  compose.id = 'ask-mia-compose';

  var input = document.createElement('textarea');
  input.id = 'ask-mia-input';
  input.setAttribute('rows', '2');
  input.setAttribute('maxlength', '4000');
  input.setAttribute('aria-label', 'הודעה למיה');
  input.setAttribute('aria-describedby', 'ask-mia-hint');

  var hint = document.createElement('p');
  hint.id = 'ask-mia-hint';
  hint.textContent = 'אפשר גם להקליט. זה יותר קל מלכתוב.';

  var actions = document.createElement('div');
  actions.id = 'ask-mia-actions';

  var sendBtn = document.createElement('button');
  sendBtn.id = 'ask-mia-send';
  sendBtn.type = 'button';
  sendBtn.textContent = 'שליחה';

  var micBtn = document.createElement('button');
  micBtn.id = 'ask-mia-mic';
  micBtn.type = 'button';
  micBtn.setAttribute('aria-label', 'הקלטה למיה');
  micBtn.setAttribute('aria-pressed', 'false');
  micBtn.textContent = 'הקלטה';

  var waBtn = document.createElement('button');
  waBtn.id = 'ask-mia-wa';
  waBtn.type = 'button';
  waBtn.hidden = true;
  waBtn.textContent = 'המשיכו בוואטסאפ';

  var status = document.createElement('div');
  status.id = 'ask-mia-status';

  function lastMiaText() {
    var nodes = transcript.querySelectorAll('.ask-mia-mia');
    var i;
    for (i = nodes.length - 1; i >= 0; i--) {
      if (nodes[i].closest('#ask-mia-loading')) continue;
      return nodes[i].textContent || '';
    }
    return '';
  }

  function bubbleAvatar(role) {
    var face = document.createElement('span');
    face.className = 'ask-mia-bubble-avatar';
    face.setAttribute('aria-hidden', 'true');
    if (role === 'mia') {
      paintBrandMark(face);
    } else {
      face.textContent = 'א';
    }
    return face;
  }

  function paintMsg(role, text) {
    var row = document.createElement('div');
    row.className = 'ask-mia-row ask-mia-row-' + role;
    var el = document.createElement('div');
    el.className = 'ask-mia-msg ask-mia-' + role;
    el.textContent = text;
    row.appendChild(bubbleAvatar(role));
    row.appendChild(el);
    transcript.appendChild(row);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function hideLoading() {
    var el = document.getElementById('ask-mia-loading');
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function showLoading() {
    hideLoading();
    var row = document.createElement('div');
    row.id = 'ask-mia-loading';
    row.className = 'ask-mia-row ask-mia-row-mia';
    row.setAttribute('aria-label', 'מיה כותבת');
    var bubble = document.createElement('div');
    bubble.className = 'ask-mia-msg ask-mia-mia';
    var dots = document.createElement('span');
    dots.className = 'ask-mia-dots';
    dots.setAttribute('aria-hidden', 'true');
    dots.appendChild(document.createElement('span'));
    dots.appendChild(document.createElement('span'));
    dots.appendChild(document.createElement('span'));
    bubble.appendChild(dots);
    row.appendChild(bubbleAvatar('mia'));
    row.appendChild(bubble);
    transcript.appendChild(row);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function persistTranscript() {
    try {
      localStorage.setItem(
        TRANSCRIPT_KEY,
        JSON.stringify(storedTranscript.slice(-16))
      );
    } catch (err) {}
  }

  function loadStoredSession() {
    try {
      var value = localStorage.getItem(SESSION_KEY);
      if (typeof value === 'string' && SESSION_RE.test(value)) return value;
    } catch (err) {}
    return null;
  }

  function saveStoredSession(id) {
    try {
      localStorage.setItem(SESSION_KEY, id);
    } catch (err) {}
  }

  function restoreTranscript() {
    try {
      var raw = localStorage.getItem(TRANSCRIPT_KEY);
      var rows = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(rows)) return false;
      storedTranscript = [];
      rows.forEach(function (row) {
        if (!row || (row.role !== 'mia' && row.role !== 'user')) return;
        if (typeof row.text !== 'string' || !row.text) return;
        var text = stripWaMeUrls(row.text.slice(0, 4000));
        if (!text) return;
        storedTranscript.push({ role: row.role, text: text });
        paintMsg(row.role, text);
      });
      return storedTranscript.length > 0;
    } catch (err) {
      return false;
    }
  }

  function appendMsg(role, text) {
    if (typeof text !== 'string' || !text) return false;
    if (role === 'mia' && text === lastMiaText()) return false;
    paintMsg(role, text);
    storedTranscript.push({ role: role, text: text });
    persistTranscript();
    return true;
  }

  function isWaMeUrl(url) {
    try {
      var parsed = new URL(url);
      return parsed.protocol === 'https:' && parsed.hostname === 'wa.me';
    } catch (err) {
      return false;
    }
  }

  var WA_ME_IN_TEXT = /https?:\/\/(?:www\.)?wa\.me\/[^\s]*/gi;

  function stripWaMeUrls(text) {
    if (typeof text !== 'string' || !text) return '';
    var cleaned = text.replace(WA_ME_IN_TEXT, ' ');
    cleaned = cleaned.replace(/\bwa\.me\/[^\s]+/gi, ' ');
    return cleaned
      .replace(/[ \t]+/g, ' ')
      .replace(/ *\n */g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function hasWhatsAppCta() {
    return !!transcript.querySelector('.ask-mia-handoff-cta');
  }

  function notifyHandoffIssued() {
    if (!sessionId) return;
    fetch(
      api + '/v1/website/sessions/' + encodeURIComponent(sessionId) + '/handoff',
      { method: 'POST', credentials: 'omit', keepalive: true }
    )
      .then(function (response) {
        if (!response.ok) throw new Error('handoff failed');
        return response.json();
      })
      .then(function (data) {
        if (data.notification_status === 'delivered') {
          status.textContent = 'אסף קיבל את תקציר השיחה.';
        } else if (data.notification_status === 'failed') {
          status.textContent = 'לא הצלחתי להעביר את השיחה לאסף כרגע.';
        }
      })
      .catch(function () {
        status.textContent = 'לא הצלחתי להעביר את השיחה לאסף כרגע.';
      });
  }

  function openConfiguredWhatsApp() {
    if (!isWaMeUrl(configuredWhatsAppUrl)) {
      status.textContent = WA_NA;
      return;
    }
    // This runs directly in the visitor's click handler, so WhatsApp opens even if
    // the best-effort notification request is slow or fails.
    window.open(configuredWhatsAppUrl, '_blank', 'noopener,noreferrer');
    notifyHandoffIssued();
  }

  function showConfiguredWhatsApp(url) {
    configuredWhatsAppUrl = isWaMeUrl(url) ? url : '';
    waBtn.hidden = !configuredWhatsAppUrl;
    waBtn.classList.toggle('offer', !!configuredWhatsAppUrl);
  }

  function makeWhatsAppCta(url) {
    if (!isWaMeUrl(url)) return null;
    var link = document.createElement('a');
    link.className = 'ask-mia-handoff-cta';
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'נעבור לוואטסאפ';
    link.addEventListener('click', notifyHandoffIssued);
    return link;
  }

  function placeWhatsAppCta(url, ontoLastBubble) {
    var link = makeWhatsAppCta(url);
    if (!link) return;
    if (hasWhatsAppCta()) return;
    if (ontoLastBubble) {
      var nodes = transcript.querySelectorAll('.ask-mia-mia');
      var last = nodes.length ? nodes[nodes.length - 1] : null;
      if (last && !last.closest('#ask-mia-loading')) {
        last.classList.add('ask-mia-handoff');
        last.appendChild(link);
        transcript.scrollTop = transcript.scrollHeight;
        link.focus();
        return;
      }
    }
    paintHandoffCard(url);
  }

  function hasForbiddenSubstring(value) {
    var lower = value.toLowerCase();
    for (var i = 0; i < FORBIDDEN.length; i++) {
      if (lower.indexOf(FORBIDDEN[i]) >= 0) return true;
    }
    return false;
  }

  function validateSlug(value) {
    if (typeof value !== 'string') return null;
    var cleaned = value.trim();
    if (!cleaned) return null;
    if (cleaned.indexOf('\n') >= 0 || cleaned.indexOf('\r') >= 0) return null;
    if (cleaned.indexOf('@') >= 0) return null;
    if (cleaned.indexOf(' ') >= 0) return null;
    if (hasForbiddenSubstring(cleaned)) return null;
    if (cleaned.length > 80) cleaned = cleaned.slice(0, 80);
    if (!SLUG_RE.test(cleaned)) return null;
    return cleaned;
  }

  function sendEvent(payload) {
    if (!sessionId) return;
    fetch(
      api + '/v1/website/sessions/' + encodeURIComponent(sessionId) + '/events',
      {
        method: 'POST',
        credentials: 'omit',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }
    ).catch(function () {});
  }

  function hasUserTurn() {
    for (var i = 0; i < storedTranscript.length; i++) {
      if (storedTranscript[i] && storedTranscript[i].role === 'user') return true;
    }
    return false;
  }

  function endSession() {
    if (sessionEnded || !sessionId || !hasUserTurn()) return;
    sessionEnded = true;
    var url = api + '/v1/website/sessions/' + encodeURIComponent(sessionId) + '/end';
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(url, new Blob([], { type: 'text/plain' }));
        return;
      }
    } catch (err) {}
    fetch(url, { method: 'POST', keepalive: true, credentials: 'omit' }).catch(function () {});
  }

  function postEvent(kind, extra) {
    var payload = { kind: kind };
    if (extra) {
      for (var k in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, k)) payload[k] = extra[k];
      }
    }
    if (!sessionId) {
      eventQueue.push(payload);
      while (eventQueue.length > MAX_QUEUE) eventQueue.shift();
      return;
    }
    sendEvent(payload);
  }

  function flushEventQueue() {
    while (eventQueue.length) sendEvent(eventQueue.shift());
  }

  function sessionQuery() {
    var q = new URLSearchParams(location.search);
    var p = new URLSearchParams();
    ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content'].forEach(function (k) {
      var v = q.get(k);
      if (v) p.set(k, v);
    });
    p.set('landing_page', location.pathname);
    if (document.referrer) p.set('referrer', document.referrer);
    return p.toString();
  }

  function fetchJson(url, opts) {
    return fetch(url, Object.assign({ credentials: 'omit' }, opts || {})).then(function (r) {
      if (!r.ok) {
        var err = new Error('fail');
        err.status = r.status;
        throw err;
      }
      return r.json();
    });
  }

  function openPanel() {
    panel.hidden = false;
    launcher.setAttribute('aria-expanded', 'true');
    if (!opened) {
      opened = true;
      initSession();
    }
    input.focus();
  }

  function closePanel() {
    if (recording) finishRecording(false);
    panel.hidden = true;
    launcher.setAttribute('aria-expanded', 'false');
    endSession();
  }

  function togglePanel() {
    if (panel.hidden) openPanel();
    else closePanel();
  }

  function createWebsiteSession() {
    return fetchJson(api + '/v1/website/sessions?' + sessionQuery(), { method: 'POST' }).then(
      function (data) {
        if (typeof data.session_id === 'string' && SESSION_RE.test(data.session_id)) {
          sessionId = data.session_id;
          saveStoredSession(sessionId);
        }
        if (sessionId) {
          postEvent('page_viewed', { path: location.pathname });
          flushEventQueue();
        }
        return sessionId;
      }
    );
  }

  function initSession() {
    busy = true;
    status.textContent = '';
    fetchJson(api + '/v1/website/config')
      .then(function (cfg) {
        var existing = loadStoredSession();
        var resumed = restoreTranscript();
        if (!existing && !resumed && typeof cfg.opening === 'string') {
          appendMsg('mia', cfg.opening);
        }
        if (existing) {
          sessionId = existing;
          postEvent('page_viewed', { path: location.pathname });
          flushEventQueue();
          return existing;
        }
        return createWebsiteSession().then(function (id) {
          // WhatsApp is offered only after a reply that already has phone or email.
          return id;
        });
      })
      .catch(function () {
        status.textContent = ERR;
      })
      .finally(function () {
        busy = false;
      });
  }

  function applyReply(data) {
    hideLoading();
    if (typeof data.heard === 'string' && data.heard) {
      var users = transcript.querySelectorAll('.ask-mia-row-user .ask-mia-user');
      if (users.length) users[users.length - 1].textContent = data.heard;
      var lastStored = storedTranscript[storedTranscript.length - 1];
      if (lastStored && lastStored.role === 'user') {
        lastStored.text = data.heard;
        persistTranscript();
      }
    }
    var raw = typeof data.message === 'string' ? data.message : '';
    var visible = stripWaMeUrls(raw);
    var offering =
      data.next_action === 'offer_whatsapp' || data.next_action === 'handoff';
    var replyUrl =
      typeof data.whatsapp_url === 'string' && isWaMeUrl(data.whatsapp_url)
        ? data.whatsapp_url
        : '';
    var painted = visible ? appendMsg('mia', visible) : false;
    if (!visible) status.textContent = ERR;
    if (offering) {
      waBtn.hidden = true;
      waBtn.classList.remove('offer');
      if (replyUrl) {
        placeWhatsAppCta(replyUrl, painted);
      } else {
        status.textContent = WA_NA;
      }
    } else {
      waBtn.classList.remove('offer');
    }
  }

  function retryOnce(run) {
    return run().catch(function (err) {
      if (!err || err.status !== 404) throw err;
      return createWebsiteSession().then(function (id) {
        if (!id) throw new Error('fail');
        return run();
      });
    });
  }

  function postText(text) {
    return fetchJson(
      api + '/v1/website/sessions/' + encodeURIComponent(sessionId) + '/messages',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text }),
      }
    );
  }

  function flushBurst() {
    burstTimer = 0;
    if (!burstParts.length || !sessionId) return;
    if (busy) {
      burstTimer = setTimeout(flushBurst, BURST_MS);
      return;
    }
    var text = burstParts.join(' ').trim();
    burstParts = [];
    if (!text) return;
    if (text.length > 4000) text = text.slice(0, 4000);
    busy = true;
    status.textContent = '';
    showLoading();
    retryOnce(function () {
      return postText(text);
    })
      .then(applyReply)
      .catch(function () {
        hideLoading();
        status.textContent = ERR;
      })
      .finally(function () {
        hideLoading();
        busy = false;
      });
  }

  function sendMessage() {
    var text = input.value.trim();
    if (!text || !sessionId) return;
    if (text.length > 4000) text = text.slice(0, 4000);
    status.textContent = '';
    appendMsg('user', text);
    input.value = '';
    burstParts.push(text);
    if (burstTimer) clearTimeout(burstTimer);
    burstTimer = setTimeout(flushBurst, BURST_MS);
  }

  function postVoice(blob) {
    var form = new FormData();
    var mime = blob.type || 'audio/webm';
    var name = mime.indexOf('mp4') >= 0 ? 'note.mp4' : 'note.webm';
    form.append('file', blob, name);
    return fetchJson(
      api + '/v1/website/sessions/' + encodeURIComponent(sessionId) + '/voice',
      { method: 'POST', body: form }
    );
  }

  function sendVoice(blob) {
    if (busy || !sessionId || !blob || !blob.size) return;
    busy = true;
    status.textContent = '';
    appendMsg('user', 'הקלטה');
    showLoading();
    retryOnce(function () {
      return postVoice(blob);
    })
      .then(applyReply)
      .catch(function () {
        hideLoading();
        status.textContent = MIC_ERR;
      })
      .finally(function () {
        hideLoading();
        busy = false;
      });
  }

  function pickMime() {
    if (typeof MediaRecorder === 'undefined') return '';
    var types = ['audio/webm', 'audio/webm;codecs=opus', 'audio/mp4'];
    var i;
    if (typeof MediaRecorder.isTypeSupported === 'function') {
      for (i = 0; i < types.length; i++) {
        if (MediaRecorder.isTypeSupported(types[i])) return types[i];
      }
    }
    return 'audio/webm';
  }

  function setMicLive(on) {
    recording = on;
    if (on) {
      micBtn.classList.add('recording');
      micBtn.textContent = MIC_LIVE;
      micBtn.setAttribute('aria-pressed', 'true');
    } else {
      micBtn.classList.remove('recording');
      micBtn.textContent = MIC_IDLE;
      micBtn.setAttribute('aria-pressed', 'false');
    }
  }

  function stopTracks() {
    if (!recordStream) return;
    recordStream.getTracks().forEach(function (t) {
      t.stop();
    });
    recordStream = null;
  }

  function finishRecording(send) {
    var rec = mediaRecorder;
    if (!rec && !recording) return;
    recording = false;
    mediaRecorder = null;
    if (!rec) {
      stopTracks();
      setMicLive(false);
      return;
    }
    rec.ondataavailable = function (e) {
      if (e.data && e.data.size) audioChunks.push(e.data);
    };
    rec.onerror = null;
    rec.onstop = function () {
      var chunks = audioChunks;
      audioChunks = [];
      stopTracks();
      setMicLive(false);
      if (!send) return;
      if (!chunks.length) {
        status.textContent = MIC_ERR;
        return;
      }
      var blob = new Blob(chunks, { type: chunks[0].type || 'audio/webm' });
      if (!blob.size) {
        status.textContent = MIC_ERR;
        return;
      }
      sendVoice(blob);
    };
    try {
      if (rec.state !== 'inactive') rec.stop();
      else rec.onstop();
    } catch (err) {
      stopTracks();
      setMicLive(false);
      if (send) status.textContent = MIC_ERR;
    }
  }

  function toggleRecord() {
    if (busy || !sessionId) return;
    if (recording) {
      finishRecording(true);
      return;
    }
    if (
      typeof MediaRecorder !== 'function' ||
      !navigator.mediaDevices ||
      typeof navigator.mediaDevices.getUserMedia !== 'function'
    ) {
      status.textContent = MIC_NA;
      return;
    }
    status.textContent = '';
    var mime = pickMime();
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then(function (stream) {
        if (busy || !sessionId || recording) {
          stream.getTracks().forEach(function (t) {
            t.stop();
          });
          return;
        }
        recordStream = stream;
        audioChunks = [];
        try {
          mediaRecorder = mime
            ? new MediaRecorder(stream, { mimeType: mime })
            : new MediaRecorder(stream);
        } catch (err) {
          stream.getTracks().forEach(function (t) {
            t.stop();
          });
          recordStream = null;
          status.textContent = MIC_NA;
          return;
        }
        recordStarted = Date.now();
        mediaRecorder.ondataavailable = function (e) {
          if (e.data && e.data.size) audioChunks.push(e.data);
          if (recording && Date.now() - recordStarted >= MAX_RECORD_MS) {
            finishRecording(true);
          }
        };
        mediaRecorder.onerror = function () {
          finishRecording(false);
          status.textContent = MIC_ERR;
        };
        setMicLive(true);
        try {
          mediaRecorder.start(1000);
        } catch (err) {
          finishRecording(false);
          status.textContent = MIC_NA;
        }
      })
      .catch(function () {
        status.textContent = MIC_PERM;
      });
  }

  function paintHandoffCard(url) {
    var link = makeWhatsAppCta(url);
    if (!link) return;
    var row = document.createElement('div');
    row.className = 'ask-mia-row ask-mia-row-mia';
    var avatar = document.createElement('span');
    avatar.className = 'ask-mia-bubble-avatar';
    paintBrandMark(avatar);
    var card = document.createElement('div');
    card.className = 'ask-mia-msg ask-mia-mia ask-mia-handoff';

    var title = document.createElement('strong');
    title.className = 'ask-mia-handoff-title';
    title.textContent = 'ממשיכים עם אסף בוואטסאפ';
    var note = document.createElement('span');
    note.className = 'ask-mia-handoff-note';
    note.textContent = 'בלחיצה תיפתח שיחה עם אסף בוואטסאפ. מיה לא עונה שם.';

    card.appendChild(title);
    card.appendChild(note);
    card.appendChild(link);
    row.appendChild(avatar);
    row.appendChild(card);
    transcript.appendChild(row);
    transcript.scrollTop = transcript.scrollHeight;
    link.focus();
  }

  function handoff() {
    openConfiguredWhatsApp();
  }

  function onCtaClick(e) {
    if (!e.target || !e.target.closest) return;
    if (e.target.closest('#ask-mia-root')) return;
    var el = e.target.closest('[data-mia-cta]');
    if (!el) return;
    var slug = validateSlug(el.getAttribute('data-mia-cta'));
    if (!slug) return;
    postEvent('cta_click', { cta: slug });
  }

  function onHostOpenClick(e) {
    if (!e.target || !e.target.closest) return;
    if (e.target.closest('#ask-mia-root')) return;
    if (!e.target.closest('[data-mia-open]')) return;
    e.preventDefault();
    openPanel();
  }

  function bindForm(form) {
    if (!form || boundForms.indexOf(form) >= 0) return;
    boundForms.push(form);
    var state = { dirty: false, submitted: false };
    formStates.push(state);
    form.addEventListener('focusin', function (ev) {
      var t = ev.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) {
        state.dirty = true;
        if (!formStartedPosted) {
          formStartedPosted = true;
          postEvent('form_started', {});
        }
      }
    });
    form.addEventListener('submit', function () {
      state.submitted = true;
      state.dirty = false;
    });
  }

  function bindForms(rootNode) {
    if (!rootNode) return;
    if (rootNode.nodeType === 1 && rootNode.matches && rootNode.matches('form[data-mia-form]')) {
      bindForm(rootNode);
    }
    if (!rootNode.querySelectorAll) return;
    rootNode.querySelectorAll('form[data-mia-form]').forEach(bindForm);
  }

  function checkFormAbandon() {
    if (formAbandonPosted) return;
    for (var i = 0; i < formStates.length; i++) {
      if (formStates[i].dirty && !formStates[i].submitted) {
        formAbandonPosted = true;
        postEvent('form_abandoned', {});
        return;
      }
    }
  }

  function observeSection(el, sectionObserver) {
    if (!el || !sectionObserver) return;
    sectionObserver.observe(el);
  }

  function bindSections(rootNode, sectionObserver) {
    if (!rootNode || !sectionObserver) return;
    if (rootNode.nodeType === 1 && rootNode.hasAttribute && rootNode.hasAttribute('data-mia-section')) {
      observeSection(rootNode, sectionObserver);
    }
    if (!rootNode.querySelectorAll) return;
    rootNode.querySelectorAll('[data-mia-section]').forEach(function (el) {
      observeSection(el, sectionObserver);
    });
  }

  function setupFunnelTracking() {
    document.addEventListener('click', onCtaClick, true);
    document.addEventListener('click', onHostOpenClick, true);
    bindForms(document);
    window.addEventListener('pagehide', function () {
      checkFormAbandon();
      endSession();
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') checkFormAbandon();
    });
    function onSpaNav() {
      postEvent('page_viewed', { path: location.pathname });
    }
    window.addEventListener('popstate', onSpaNav);
    window.addEventListener('hashchange', onSpaNav);
    var sectionObserver = null;
    if (typeof IntersectionObserver !== 'undefined') {
      sectionObserver = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting || entry.intersectionRatio <= 0.4) return;
            var slug = validateSlug(entry.target.getAttribute('data-mia-section'));
            if (!slug || seenSections[slug]) return;
            seenSections[slug] = true;
            postEvent('section_viewed', { section: slug });
          });
        },
        { threshold: [0, 0.4, 1] }
      );
      bindSections(document, sectionObserver);
    }
    if (typeof MutationObserver !== 'undefined' && document.body) {
      new MutationObserver(function (mutations) {
        mutations.forEach(function (m) {
          m.addedNodes.forEach(function (node) {
            if (node.nodeType !== 1) return;
            bindForms(node);
            if (sectionObserver) bindSections(node, sectionObserver);
          });
        });
      }).observe(document.body, { childList: true, subtree: true });
    }
  }

  launcher.addEventListener('click', togglePanel);
  closeBtn.addEventListener('click', closePanel);
  sendBtn.addEventListener('click', sendMessage);
  micBtn.addEventListener('click', toggleRecord);
  waBtn.addEventListener('click', handoff);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  actions.appendChild(sendBtn);
  actions.appendChild(micBtn);
  actions.appendChild(waBtn);
  compose.appendChild(input);
  compose.appendChild(hint);
  compose.appendChild(actions);
  panel.appendChild(header);
  panel.appendChild(transcript);
  panel.appendChild(compose);
  panel.appendChild(status);
  root.appendChild(launcher);
  root.appendChild(panel);
  function mount() {
    if (!document.body) {
      setTimeout(mount, 0);
      return;
    }
    document.head.appendChild(style);
    document.body.appendChild(root);
    setupFunnelTracking();
    fetchJson(api + '/v1/website/config')
      .then(function (cfg) {
        if (cfg.demo === true) {
          launchLabel.textContent = 'שאלו את מיה (דמו)';
          launcher.setAttribute('aria-label', 'שאלו את מיה (דמו)');
        }
      })
      .catch(function () {});
  }
  mount();
})();
