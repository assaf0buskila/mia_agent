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
  var sessionCredential = '';
  var opened = false;
  // Inline mode mounts the panel into a page container, always open and with no launcher.
  // Resolved in mount() so the host element exists even if the script runs before it.
  var inline = false;
  var busy = false;
  var recording = false;
  var mediaRecorder = null;
  var recordStream = null;
  var recordStopping = false;
  var MAX_RECORD_MS = 60000;
  var MIC_IDLE = 'הקלטה';
  var MIC_LIVE = 'מקליטה… לחצו שוב לשליחה';
  // One string for every failure is why nobody could tell a denied microphone from a
  // rate limit from a recording that captured silence. Each of these is a different
  // problem with a different thing the visitor should do about it.
  var MIC_ERR = 'לא שמעתי טוב. נסו שוב או כתבו.';
  var MIC_PERM = 'לא קיבלתי גישה למיקרופון. אפשר גם לכתוב.';
  var MIC_NA = 'ההקלטה לא זמינה כאן. אפשר לכתוב.';
  var MIC_EMPTY = 'ההקלטה יצאה ריקה. נסו לדבר קרוב יותר למיקרופון, או כתבו.';
  var MIC_NET = 'ההקלטה לא הגיעה. בדקו חיבור ונסו שוב, או כתבו.';
  // A 429 used to render as "I did not hear you", so the visitor retried, spent more
  // of the quota, and got the same sentence. Telling them to wait is the only advice
  // that can actually work.
  var MIC_BUSY = 'יותר מדי הקלטות בזמן קצר. חכו רגע ונסו שוב, או כתבו.';
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
  var SESSION_META_KEY = 'askMia.sessionMeta';
  var TRANSCRIPT_KEY = 'askMia.transcript';
  var SESSION_RE = /^(?:web_[a-f0-9]{16}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/i;
  var SESSION_LIFETIME_MS = 30 * 24 * 60 * 60 * 1000;
  var configuredSessionLifetimeMs = SESSION_LIFETIME_MS;
  var hostLifetime = Number(script && script.getAttribute('data-mia-session-lifetime-ms'));
  if (Number.isFinite(hostLifetime) && hostLifetime >= 60 * 60 * 1000) {
    configuredSessionLifetimeMs = hostLifetime;
  }
  var storedTranscript = [];
  var sessionEnded = false;
  var conversationFinished = false;
  var handoffPending = false;
  var sendAfterHandoff = false;
  var burstParts = [];
  var burstTimer = 0;
  var BURST_MS = 800;
  // Only populated from the server's config response. Never infer a destination
  // from a Mia reply, a page link, or visitor text.
  var configuredWhatsAppUrl = '';
  function newClientMessageId() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    return 'msg_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2);
  }
  function sessionHeaders(existing) {
    var headers = Object.assign({}, existing || {});
    if (sessionCredential) headers['X-Mia-Session-Credential'] = sessionCredential;
    return headers;
  }
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
  style.textContent = `
    #ask-mia-root {
      --ask-mia-viewport-height: 100dvh;
      position: fixed;
      inset-inline-end: max(16px, env(safe-area-inset-right, 0px));
      bottom: max(16px, env(safe-area-inset-bottom, 0px));
      z-index: 9999;
      display: flex;
      flex-direction: column-reverse;
      align-items: flex-end;
      gap: 12px;
      max-width: calc(100vw - 24px);
      font: 16px/1.5 Assistant, system-ui, sans-serif;
      color: #061b35;
      color-scheme: light;
      direction: rtl;
      -webkit-font-smoothing: antialiased;
    }
    #ask-mia-root, #ask-mia-root * { box-sizing: border-box; }
    #ask-mia-root #ask-mia-panel[hidden],
    #ask-mia-root #ask-mia-wa[hidden] { display: none !important; }
    #ask-mia-root #ask-mia-launcher {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      height: 56px;
      min-width: 56px;
      padding: 0 8px 0 18px;
      border: 1px solid #ffffff59;
      border-radius: 999px;
      background: linear-gradient(135deg, #2f5f93, #2563eb);
      color: #fff;
      box-shadow: 0 18px 44px #2563eb59;
      cursor: pointer;
      font: inherit;
      font-weight: 800;
      transition: transform .18s ease, box-shadow .18s ease;
    }
    #ask-mia-root #ask-mia-launcher:hover { transform: translateY(-2px); }
    #ask-mia-root :is(button, textarea, input, a):focus-visible {
      outline: 3px solid #2563eb;
      outline-offset: 2px;
    }
    #ask-mia-root .ask-mia-launch-mark,
    #ask-mia-root .ask-mia-avatar,
    #ask-mia-root .ask-mia-bubble-avatar {
      display: inline-flex;
      flex: 0 0 auto;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
    }
    #ask-mia-root .ask-mia-launch-mark {
      width: 40px;
      height: 40px;
      background: #d9eeff;
      color: #061b35;
    }
    #ask-mia-root .ask-mia-launch-mark svg,
    #ask-mia-root .ask-mia-avatar svg,
    #ask-mia-root .ask-mia-bubble-avatar svg { width: 20px; height: 20px; display: block; }
    #ask-mia-root #ask-mia-launch-label { color: #fff; white-space: nowrap; font-size: 15px; }
    #ask-mia-root #ask-mia-panel {
      width: min(400px, calc(100vw - 24px));
      height: min(600px, calc(var(--ask-mia-viewport-height) - 96px));
      min-height: min(440px, calc(var(--ask-mia-viewport-height) - 96px));
      max-height: calc(var(--ask-mia-viewport-height) - 96px);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      isolation: isolate;
      border: 1px solid #ffffff59;
      border-radius: 22px;
      background: #f8fbff;
      box-shadow: 0 28px 70px rgba(6, 27, 53, .28), 0 0 0 1px #2f5f9321;
      animation: ask-mia-rise .24s ease-out;
    }
    #ask-mia-root #ask-mia-header {
      display: flex;
      flex: 0 0 auto;
      align-items: center;
      gap: 12px;
      min-height: 76px;
      padding: 12px 16px;
      border-bottom: 3px solid #2563eb;
      background: linear-gradient(135deg, #061b35, #2f5f93);
      color: #fff;
    }
    #ask-mia-root .ask-mia-avatar {
      width: 44px;
      height: 44px;
      background: #d9eeff;
      color: #061b35;
      box-shadow: 0 0 0 3px #2563eb59;
    }
    #ask-mia-root .ask-mia-title { display: flex; align-items: center; gap: 8px; }
    #ask-mia-root .ask-mia-name { color: #fff; font-size: 17px; line-height: 1.2; }
    #ask-mia-root .ask-mia-ai-badge {
      padding: 2px 7px;
      border: 1px solid #ffffff59;
      border-radius: 999px;
      color: #d9eeff;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .04em;
    }
    #ask-mia-root .ask-mia-sub { display: block; margin-top: 3px; color: #d9eeff; font-size: 12px; }
    #ask-mia-root #ask-mia-close {
      width: 44px;
      height: 44px;
      margin-inline-start: auto;
      border: 0;
      border-radius: 12px;
      background: transparent;
      color: #fff;
      cursor: pointer;
      font: 26px/1 system-ui, sans-serif;
    }
    #ask-mia-root #ask-mia-close:hover { background: #2f5f93; }
    #ask-mia-root #ask-mia-transcript {
      flex: 1 1 auto;
      min-height: 0;
      overflow-x: hidden;
      overflow-y: auto;
      overscroll-behavior: contain;
      padding: 20px 16px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      background: radial-gradient(circle at 12% 8%, #d9eeff 0, transparent 34%), #f8fbff;
      scrollbar-color: #7ba7d3 transparent;
    }
    #ask-mia-root .ask-mia-row { display: flex; align-items: flex-end; gap: 8px; max-width: 100%; }
    #ask-mia-root .ask-mia-row-mia { align-self: flex-start; }
    #ask-mia-root .ask-mia-row-user { align-self: flex-end; flex-direction: row-reverse; }
    #ask-mia-root .ask-mia-bubble-avatar { width: 30px; height: 30px; font-size: 11px; }
    #ask-mia-root .ask-mia-row-mia .ask-mia-bubble-avatar { background: #d9eeff; color: #061b35; }
    #ask-mia-root .ask-mia-row-user .ask-mia-bubble-avatar { background: #2f5f93; color: #fff; }
    #ask-mia-root .ask-mia-msg {
      max-width: min(82%, 292px);
      padding: 10px 13px;
      border-radius: 16px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      unicode-bidi: plaintext;
    }
    #ask-mia-root .ask-mia-mia {
      border: 1px solid #2f5f9321;
      border-end-start-radius: 4px;
      background: #fff;
      color: #061b35;
      box-shadow: 0 8px 20px rgba(6, 27, 53, .06);
    }
    #ask-mia-root .ask-mia-user {
      border-end-end-radius: 4px;
      background: #2f5f93;
      color: #fff;
      box-shadow: 0 8px 20px rgba(6, 27, 53, .12);
    }
    #ask-mia-root .ask-mia-dots { display: inline-flex; align-items: center; gap: 4px; height: 18px; }
    #ask-mia-root .ask-mia-dots span {
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: #2f5f93;
      animation: ask-mia-bounce .65s ease-in-out infinite;
    }
    #ask-mia-root .ask-mia-dots span:nth-child(2) { animation-delay: .1s; }
    #ask-mia-root .ask-mia-dots span:nth-child(3) { animation-delay: .2s; }
    #ask-mia-root #ask-mia-compose {
      position: relative;
      flex: 0 0 auto;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      padding: 12px 14px 8px;
      border-top: 1px solid #2f5f9321;
      background: #fff;
    }
    #ask-mia-root #ask-mia-input {
      grid-column: 1 / -1;
      width: 100%;
      min-height: 50px;
      max-height: 112px;
      resize: none;
      padding: 12px 14px;
      border: 1px solid #7ba7d3;
      border-radius: 14px;
      background: #f8fbff;
      color: #061b35;
      font: inherit;
      font-size: 16px;
    }
    #ask-mia-root #ask-mia-input:focus { border-color: #2563eb; }
    #ask-mia-root #ask-mia-hint { align-self: center; margin: 0; color: #2f5f93; font-size: 12px; }
    #ask-mia-root #ask-mia-actions { display: flex; gap: 8px; }
    #ask-mia-root #ask-mia-actions button {
      min-width: 44px;
      min-height: 44px;
      border: 0;
      border-radius: 12px;
      padding: 8px 12px;
      cursor: pointer;
      font: inherit;
      font-weight: 700;
    }
    #ask-mia-root #ask-mia-send { display: inline-flex; align-items: center; gap: 6px; }
    #ask-mia-root #ask-mia-send svg { width: 17px; height: 17px; }
    #ask-mia-root #ask-mia-send { background: #2f5f93; color: #fff; }
    #ask-mia-root #ask-mia-mic { background: #d9eeff; color: #061b35; }
    #ask-mia-root #ask-mia-mic.recording { background: #b00; color: #fff; }
    #ask-mia-root #ask-mia-wa { background: #25d366; color: #fff; }
    #ask-mia-root .ask-mia-handoff { display: flex; flex-direction: column; gap: 8px; max-width: min(86%, 292px); }
    #ask-mia-root .ask-mia-handoff-title { color: #061b35; font-weight: 700; }
    #ask-mia-root .ask-mia-handoff-note { color: #2f5f93; font-size: 14px; }
    #ask-mia-root .ask-mia-handoff-cta {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      min-height: 44px;
      padding: 8px 12px;
      border-radius: 12px;
      background: #25d366;
      color: #fff;
      font-weight: 700;
      text-decoration: none;
    }
    #ask-mia-root #ask-mia-status {
      flex: 0 0 auto;
      min-height: 20px;
      padding: 0 14px max(8px, env(safe-area-inset-bottom, 0px));
      background: #fff;
      color: #b00;
      font-size: 13px;
    }
    @keyframes ask-mia-bounce {
      0%, 100% { transform: translateY(0); }
      50% { transform: translateY(-4px); }
    }
    @keyframes ask-mia-rise {
      from { opacity: 0; transform: translateY(12px) scale(.98); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @media (max-width: 480px) {
      #ask-mia-root:not(.ask-mia-inline) {
        inset-inline: 8px;
        bottom: max(8px, env(safe-area-inset-bottom, 0px));
        max-width: none;
      }
      #ask-mia-root:not(.ask-mia-inline) #ask-mia-panel {
        width: 100%;
        height: min(600px, calc(var(--ask-mia-viewport-height) - 80px));
        min-height: min(380px, calc(var(--ask-mia-viewport-height) - 80px));
        max-height: calc(var(--ask-mia-viewport-height) - 80px);
        border-radius: 18px;
      }
      #ask-mia-root #ask-mia-transcript { padding: 16px 12px; }
      #ask-mia-root .ask-mia-msg { max-width: 84%; }
    }
    /* Inline mode: fill the host container instead of the viewport. These selectors are
       #id.class, so they outrank the base #id rules above without !important. The mobile
       block is scoped with :not(.ask-mia-inline) for the same reason. */
    #ask-mia-root.ask-mia-inline {
      position: static;
      inset: auto;
      z-index: auto;
      display: block;
      width: 100%;
      height: 100%;
      max-width: none;
    }
    #ask-mia-root.ask-mia-inline #ask-mia-launcher,
    #ask-mia-root.ask-mia-inline #ask-mia-close { display: none; }
    #ask-mia-root.ask-mia-inline #ask-mia-panel {
      width: 100%;
      height: 100%;
      min-height: 0;
      max-height: none;
      animation: none;
      box-shadow: 0 18px 48px rgba(6, 27, 53, .14), 0 0 0 1px #2f5f9321;
    }
    @media (prefers-reduced-motion: reduce) {
      #ask-mia-root *, #ask-mia-root *::before, #ask-mia-root *::after {
        scroll-behavior: auto !important;
        animation-duration: .01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: .01ms !important;
      }
      #ask-mia-root #ask-mia-launcher:hover { transform: none; }
    }
  `;

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
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'false');
  panel.setAttribute('aria-labelledby', 'ask-mia-title');

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
  nameEl.id = 'ask-mia-title';
  nameEl.textContent = 'מיה';
  var aiBadge = document.createElement('span');
  aiBadge.className = 'ask-mia-ai-badge';
  aiBadge.textContent = 'AI';
  titleRow.appendChild(nameEl);
  titleRow.appendChild(aiBadge);
  var subEl = document.createElement('span');
  subEl.className = 'ask-mia-sub';
  subEl.textContent = 'עוזרת AI של אסף';
  brand.appendChild(titleRow);
  brand.appendChild(subEl);
  var closeBtn = document.createElement('button');
  closeBtn.id = 'ask-mia-close';
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'סגירה');
  closeBtn.textContent = '×';
  header.appendChild(avatar);
  header.appendChild(brand);
  header.appendChild(closeBtn);

  var transcript = document.createElement('div');
  transcript.id = 'ask-mia-transcript';
  transcript.setAttribute('role', 'log');
  transcript.setAttribute('aria-live', 'polite');
  transcript.setAttribute('aria-relevant', 'additions text');
  transcript.setAttribute('aria-label', 'השיחה עם מיה');

  var compose = document.createElement('div');
  compose.id = 'ask-mia-compose';

  var input = document.createElement('textarea');
  input.id = 'ask-mia-input';
  input.setAttribute('rows', '2');
  input.setAttribute('maxlength', '4000');
  input.setAttribute('aria-label', 'הודעה למיה');
  input.setAttribute('aria-describedby', 'ask-mia-hint');
  input.setAttribute('placeholder', 'כתבו הודעה למיה...');

  var hint = document.createElement('p');
  hint.id = 'ask-mia-hint';
  hint.textContent = 'אפשר גם להקליט. זה יותר קל מלכתוב.';

  var actions = document.createElement('div');
  actions.id = 'ask-mia-actions';

  var sendBtn = document.createElement('button');
  sendBtn.id = 'ask-mia-send';
  sendBtn.type = 'button';
  sendBtn.setAttribute('aria-label', 'שליחת הודעה');
  var sendIcon = sendPlaneSvg();
  if (sendIcon) sendBtn.appendChild(sendIcon);
  var sendLabel = document.createElement('span');
  sendLabel.textContent = 'שליחה';
  sendBtn.appendChild(sendLabel);

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
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');

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

  var TOOL_LEAKS = ['knowledge_search', 'Search Console', 'JSON-LD', 'JSON LD', 'json-ld', 'jsonld'];

  function scrubMia(text) {
    var out = String(text || '');
    var i;
    for (i = 0; i < TOOL_LEAKS.length; i++) {
      out = out.split(TOOL_LEAKS[i]).join(' ');
      out = out.split(TOOL_LEAKS[i].toLowerCase()).join(' ');
    }
    out = out.replace(/לא\s+רץ\s+כלי[^.]*\.?/g, ' ');
    out = out.replace(/(^|[\s.])רץ(\s+\S+)?\.?/g, '$1');
    out = out.replace(/(^|\s)\.(?=[A-Za-z])/g, '$1');
    out = out.replace(/\s+/g, ' ').trim();
    return out.replace(/^\.+\s*/, '').trim();
  }

  function paintMsg(role, text) {
    var row = document.createElement('div');
    row.className = 'ask-mia-row ask-mia-row-' + role;
    var el = document.createElement('div');
    el.className = 'ask-mia-msg ask-mia-' + role;
    el.dir = 'auto';
    el.textContent = role === 'mia' ? scrubMia(text) : text;
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
      if (typeof value !== 'string' || !SESSION_RE.test(value)) {
        clearStoredSession();
        return null;
      }
      var rawMeta = localStorage.getItem(SESSION_META_KEY);
      var meta = rawMeta ? JSON.parse(rawMeta) : null;
      if (!meta || typeof meta.credential !== 'string' || !meta.credential.trim() ||
          !Number.isFinite(meta.updatedAt) ||
          Date.now() - meta.updatedAt > configuredSessionLifetimeMs) {
        clearStoredSession();
        return null;
      }
      sessionCredential = typeof meta.credential === 'string' ? meta.credential : '';
      configuredWhatsAppUrl =
        typeof meta.whatsappUrl === 'string' && isWaMeUrl(meta.whatsappUrl)
          ? meta.whatsappUrl
          : '';
      return value;
    } catch (err) {}
    return null;
  }

  function saveStoredSession(id, credential) {
    try {
      if (typeof credential === 'string') sessionCredential = credential;
      localStorage.setItem(SESSION_KEY, id);
      localStorage.setItem(
        SESSION_META_KEY,
        JSON.stringify({
          updatedAt: Date.now(),
          credential: sessionCredential,
          whatsappUrl: configuredWhatsAppUrl,
        })
      );
    } catch (err) {}
  }

  function clearStoredSession() {
    try {
      localStorage.removeItem(SESSION_KEY);
      localStorage.removeItem(SESSION_META_KEY);
      localStorage.removeItem(TRANSCRIPT_KEY);
    } catch (err) {}
    sessionCredential = '';
    storedTranscript = [];
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
    var shown = role === 'mia' ? scrubMia(text) : text;
    if (!shown) return false;
    if (role === 'mia' && shown === lastMiaText()) return false;
    paintMsg(role, shown);
    storedTranscript.push({ role: role, text: shown });
    persistTranscript();
    return true;
  }

  function isWaMeUrl(url) {
    try {
      var parsed = new URL(url);
      var valid = (
        parsed.protocol === 'https:' &&
        parsed.hostname === 'wa.me' &&
        (!parsed.port || parsed.port === '443') &&
        !parsed.username &&
        !parsed.password &&
        /^\/[0-9]+\/?$/.test(String(parsed.pathname || ''))
      );
      return valid;
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
    if (!sessionId || handoffPending) return;
    var handoffSessionId = sessionId;
    handoffPending = true;
    fetch(
      api + '/v1/website/sessions/' + encodeURIComponent(sessionId) + '/handoff',
      {
        method: 'POST',
        credentials: 'omit',
        keepalive: true,
        headers: sessionHeaders(),
      }
    )
      .then(function (response) {
        if (!response.ok) throw new Error('handoff failed');
        return response.json();
      })
      .then(function (data) {
        if (sessionId !== handoffSessionId) return;
        if (data.notification_status === 'delivered') {
          status.textContent = 'אסף קיבל את תקציר השיחה.';
        } else if (data.notification_status === 'pending') {
          status.textContent = 'הפרטים נשמרו וההעברה לאסף ממתינה.';
        } else if (data.notification_status === 'failed') {
          status.textContent = 'לא הצלחתי להעביר את השיחה לאסף כרגע.';
        }
      })
      .catch(function () {
        status.textContent = 'לא הצלחתי להעביר את השיחה לאסף כרגע.';
      })
      .finally(function () {
        handoffPending = false;
        if (sendAfterHandoff) {
          sendAfterHandoff = false;
          sendMessage();
        }
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
    if (sessionId) saveStoredSession(sessionId);
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
        headers: sessionHeaders({ 'Content-Type': 'application/json' }),
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
      if (!sessionCredential && navigator.sendBeacon) {
        navigator.sendBeacon(url, new Blob([], { type: 'text/plain' }));
        return;
      }
    } catch (err) {}
    fetch(url, {
      method: 'POST',
      keepalive: true,
      credentials: 'omit',
      headers: sessionHeaders(),
    }).catch(function () {});
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
    var pageSection =
      script.getAttribute('data-mia-page-section') ||
      (document.body && document.body.getAttribute('data-mia-page-section')) ||
      '';
    if (SLUG_RE.test(pageSection)) p.set('page_section', pageSection);
    if (document.referrer) p.set('referrer', document.referrer);
    return p.toString();
  }

  function fetchJson(url, opts, timeoutMs) {
    var options = Object.assign({ credentials: 'omit' }, opts || {});
    if (sessionId && url.indexOf('/v1/website/sessions/' + encodeURIComponent(sessionId)) >= 0) {
      options.headers = sessionHeaders(options.headers);
    }
    if (
      timeoutMs &&
      typeof AbortSignal !== 'undefined' &&
      typeof AbortSignal.timeout === 'function'
    ) {
      options.signal = AbortSignal.timeout(timeoutMs);
    }
    return fetch(url, options).then(function (r) {
      if (!r.ok) {
        var err = new Error('fail');
        err.status = r.status;
        throw err;
      }
      return r.json();
    });
  }

  function resetFinishedConversation() {
    clearStoredSession();
    while (transcript.firstChild) transcript.removeChild(transcript.firstChild);
    configuredWhatsAppUrl = '';
    waBtn.hidden = true;
    sessionEnded = false;
    conversationFinished = false;
    opened = false;
    burstParts = [];
    eventQueue = [];
    seenSections = {};
    if (burstTimer) clearTimeout(burstTimer);
    burstTimer = 0;
  }

  function openPanel() {
    panel.hidden = false;
    launcher.setAttribute('aria-expanded', 'true');
    if (conversationFinished) resetFinishedConversation();
    if (!opened) {
      opened = true;
      initSession();
    }
    input.focus();
  }

  function closePanel() {
    // Inline mode has no launcher to reopen from, so closing would strand the visitor.
    if (inline) return;
    if (recording) finishRecording(false);
    panel.hidden = true;
    launcher.setAttribute('aria-expanded', 'false');
    endSession();
    launcher.focus();
  }

  function syncViewportHeight() {
    var viewport = window.visualViewport;
    var height = viewport && Number(viewport.height) > 0 ? viewport.height : window.innerHeight;
    if (!Number(height) || !root.style || typeof root.style.setProperty !== 'function') return;
    root.style.setProperty('--ask-mia-viewport-height', Math.round(height) + 'px');
  }

  function togglePanel() {
    if (panel.hidden) openPanel();
    else closePanel();
  }

  function createWebsiteSession() {
    return fetchJson(api + '/v1/website/sessions?' + sessionQuery(), { method: 'POST' }).then(
      function (data) {
        if (typeof data.session_id !== 'string' || !SESSION_RE.test(data.session_id) ||
            typeof data.session_credential !== 'string' || !data.session_credential.trim()) {
          throw new Error('invalid session response');
        }
        sessionId = data.session_id;
        sessionCredential = data.session_credential;
        sessionEnded = false;
        saveStoredSession(sessionId);
        if (sessionId) {
          postEvent('page_viewed', { path: location.pathname });
          flushEventQueue();
        }
        return sessionId;
      }
    );
  }

  function initSession() {
    opened = true;
    busy = true;
    status.textContent = '';
    return fetchJson(api + '/v1/website/config')
      .then(function (cfg) {
        if (cfg && Number.isFinite(cfg.session_lifetime_ms) && cfg.session_lifetime_ms > 0) {
          configuredSessionLifetimeMs = cfg.session_lifetime_ms;
        }
        var existing = loadStoredSession();
        var resumed = restoreTranscript();
        if (!existing && !resumed && typeof cfg.opening === 'string') {
          appendMsg('mia', cfg.opening);
        }
        if (existing) {
          sessionId = existing;
          saveStoredSession(sessionId);
          if (configuredWhatsAppUrl) showConfiguredWhatsApp(configuredWhatsAppUrl);
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
    var replyUrl =
      typeof data.whatsapp_url === 'string' && isWaMeUrl(data.whatsapp_url)
        ? data.whatsapp_url
        : '';
    var painted = visible ? appendMsg('mia', visible) : false;
    if (!visible) status.textContent = ERR;
    if (data.next_action === 'contact_saved') {
      var deliveryStatus = typeof data.delivery_status === 'string'
        ? data.delivery_status
        : '';
      if (deliveryStatus === 'confirmed') {
        status.textContent = 'הפרטים נשמרו והמסירה לאסף אושרה.';
      } else if (deliveryStatus === 'pending') {
        status.textContent = 'הפרטים נשמרו והמסירה לאסף עדיין ממתינה.';
      } else if (deliveryStatus === 'failed') {
        status.textContent = 'הפרטים נשמרו, אך המסירה לאסף נכשלה.';
      } else {
        status.textContent = 'הפרטים נשמרו.';
      }
      if (replyUrl) {
        placeWhatsAppCta(replyUrl, painted);
        showConfiguredWhatsApp(replyUrl);
      }
    }
    // Unconditional on purpose, and it is what already ran on every real reply: the
    // branch this replaces skipped it only for the two retired actions the server has
    // not been able to emit since 40747c8, so the else arm was the whole live behaviour.
    // The persistent WhatsApp button keeps its own visibility from showConfiguredWhatsApp;
    // only the transient "offer" highlight is cleared once the reply has been painted.
    waBtn.classList.remove('offer');
  }

  function retryOnce(run) {
    return run().catch(function (err) {
      if (!err || (err.status !== 401 && err.status !== 404)) throw err;
      return createWebsiteSession().then(function (id) {
        if (!id) throw new Error('fail');
        return run();
      });
    });
  }

  function postText(text, clientMessageId) {
    return fetchJson(
      api + '/v1/website/sessions/' + encodeURIComponent(sessionId) + '/messages',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, client_message_id: clientMessageId }),
      }
    );
  }

  function flushBurst() {
    burstTimer = 0;
    if (!burstParts.length || !sessionId) return;
    if (busy || recordStopping) {
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
    var clientMessageId = newClientMessageId();
    retryOnce(function () {
      return postText(text, clientMessageId);
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
    if (!text) return;
    if (handoffPending) {
      sendAfterHandoff = true;
      return;
    }
    if (!sessionId) {
      if (conversationFinished && !busy) {
        resetFinishedConversation();
        initSession().then(sendMessage);
      }
      return;
    }
    if (text.length > 4000) text = text.slice(0, 4000);
    status.textContent = '';
    appendMsg('user', text);
    input.value = '';
    burstParts.push(text);
    if (burstTimer) clearTimeout(burstTimer);
    burstTimer = setTimeout(flushBurst, BURST_MS);
  }

  function postVoice(blob, clientMessageId) {
    var form = new FormData();
    var mime = blob.type || 'audio/webm';
    var name = mime.indexOf('mp4') >= 0 ? 'note.mp4' : 'note.webm';
    form.append('file', blob, name);
    form.append('client_message_id', clientMessageId);
    return fetchJson(
      api + '/v1/website/sessions/' + encodeURIComponent(sessionId) + '/voice',
      { method: 'POST', body: form },
      25000
    );
  }

  function voiceFailed(why, msg) {
    // Tell the visitor which problem this is, and leave a trace. A browser-side voice
    // failure used to be invisible: it never reached the voice endpoint, so nothing
    // was logged and every cause produced the same sentence.
    status.textContent = msg;
    appendMsg('mia', msg);
    try {
      postEvent('voice_failed', { section: why });
    } catch (err) {
      /* telemetry must never cost the visitor anything */
    }
  }

  function sendVoice(blob) {
    if (busy || !blob || !blob.size) return;
    if (!sessionId) {
      status.textContent = MIC_ERR;
      appendMsg('mia', MIC_ERR);
      return;
    }
    busy = true;
    status.textContent = '';
    appendMsg('user', 'הקלטה');
    showLoading();
    var clientMessageId = newClientMessageId();
    retryOnce(function () {
      return postVoice(blob, clientMessageId);
    })
      .then(applyReply)
      .catch(function (err) {
        hideLoading();
        // Say which failure this was. 429 in particular must not read as "try again":
        // retrying is exactly what keeps it failing.
        var code = err && err.status;
        var msg = MIC_NET;
        var why = 'upload_' + (code || 'network');
        if (code === 429) {
          msg = MIC_BUSY;
          why = 'rate_limited';
        } else if (code === 415) {
          msg = MIC_NA;
          why = 'unsupported_type';
        } else if (code === 400) {
          msg = MIC_EMPTY;
          why = 'empty_audio';
        }
        voiceFailed(why, msg);
      })
      .finally(function () {
        hideLoading();
        busy = false;
      });
  }

  function isAppleCapture() {
    var ua = navigator.userAgent || '';
    if (/iP(hone|ad|od)/.test(ua)) return true;
    if (/Macintosh/.test(ua) && 'ontouchend' in document) return true;
    return /Safari/.test(ua) && !/Chrome|Chromium|Android/.test(ua);
  }

  function pickMime() {
    if (typeof MediaRecorder === 'undefined') return '';
    var apple = isAppleCapture();
    var types = apple
      ? ['audio/mp4', 'audio/aac', 'audio/webm;codecs=opus', 'audio/webm']
      : ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
    var i;
    if (typeof MediaRecorder.isTypeSupported === 'function') {
      for (i = 0; i < types.length; i++) {
        if (MediaRecorder.isTypeSupported(types[i])) return types[i];
      }
    }
    return apple ? 'audio/mp4' : 'audio/webm';
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
    setMicLive(false);
    if (!rec) {
      stopTracks();
      if (send) {
        status.textContent = MIC_ERR;
        appendMsg('mia', MIC_ERR);
      }
      return;
    }
    rec._miaSend = send;
    recordStopping = true;
    rec.onerror = null;
    try {
      if (rec.state !== 'inactive') {
        if (typeof rec.requestData === 'function') rec.requestData();
        rec.stop();
      } else rec.onstop();
    } catch (err) {
      recordStopping = false;
      stopTracks();
      if (send) {
        status.textContent = MIC_ERR;
        appendMsg('mia', MIC_ERR);
      }
    }
  }

  function toggleRecord() {
    if (busy || recordStopping || handoffPending) return;
    if (!sessionId) {
      if (conversationFinished) {
        resetFinishedConversation();
        initSession().then(toggleRecord);
        return;
      }
      status.textContent = MIC_ERR;
      return;
    }
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
        mediaRecorder._miaMime = mime;
        mediaRecorder._miaStarted = Date.now();
        mediaRecorder._miaStream = stream;
        mediaRecorder._miaChunks = [];
        mediaRecorder.ondataavailable = function (e) {
          if (e.data && e.data.size) {
            this._miaChunks.push(e.data);
          }
          if (mediaRecorder === this && recording && Date.now() - this._miaStarted >= MAX_RECORD_MS) {
            finishRecording(true);
          }
        };
        mediaRecorder._miaSend = false;
        mediaRecorder.onstop = function () {
          var chunks = this._miaChunks || [];
          this._miaChunks = [];
          this._miaStream.getTracks().forEach(function (track) { track.stop(); });
          if (recordStream === this._miaStream) recordStream = null;
          recordStopping = false;
          if (!this._miaSend) return;
          if (!chunks.length) {
            voiceFailed('no_chunks', MIC_EMPTY);
            return;
          }
          var blobType = (chunks[0] && chunks[0].type) || this._miaMime || 'audio/webm';
          var blob = new Blob(chunks, { type: blobType });
          if (!blob.size) {
            voiceFailed('empty_blob', MIC_EMPTY);
            return;
          }
          sendVoice(blob);
        };
        mediaRecorder.onerror = function () {
          finishRecording(false);
          status.textContent = MIC_ERR;
        };
        setMicLive(true);
        try {
          try {
            mediaRecorder.start(1000);
          } catch (sliceErr) {
            // Older Safari builds reject a timeslice; retain the proven periodic
            // path as the default and use a guarded no-timeslice fallback.
            mediaRecorder.start();
          }
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
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !panel.hidden && !inline) {
      e.preventDefault();
      closePanel();
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
  function resolveInlineHost() {
    var selector = script.getAttribute('data-mia-mount') || '[data-mia-inline]';
    try {
      return document.querySelector(selector);
    } catch (err) {
      return null;
    }
  }

  function mount() {
    if (!document.body) {
      setTimeout(mount, 0);
      return;
    }
    document.head.appendChild(style);
    var inlineHost = resolveInlineHost();
    if (inlineHost) {
      inline = true;
      root.classList.add('ask-mia-inline');
      // An always-open region, not a dialog.
      panel.setAttribute('role', 'region');
      panel.removeAttribute('aria-modal');
      panel.hidden = false;
      inlineHost.appendChild(root);
      if (!opened) {
        opened = true;
        initSession();
      }
    } else {
      document.body.appendChild(root);
      // Viewport sizing only matters for the floating panel; inline sizes to its host.
      syncViewportHeight();
      if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', syncViewportHeight);
        window.visualViewport.addEventListener('scroll', syncViewportHeight);
      } else {
        window.addEventListener('resize', syncViewportHeight);
      }
    }
    setupFunnelTracking();
    fetchJson(api + '/v1/website/config')
      .then(function (cfg) {
        if (cfg.demo === true && !inline) {
          launchLabel.textContent = 'שאלו את מיה (דמו)';
          launcher.setAttribute('aria-label', 'שאלו את מיה (דמו)');
        }
      })
      .catch(function () {});
  }
  mount();
})();
