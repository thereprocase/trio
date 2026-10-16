(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const events = [], feeds = {};
  const started = Date.now();
  const random = new Uint8Array(8);
  window.crypto?.getRandomValues?.(random);
  const session = [...random].map(n => n.toString(16).padStart(2, '0')).join('');
  const errors = new Set(['Error', 'TypeError', 'SyntaxError', 'RangeError', 'ReferenceError', 'AbortError', 'TimeoutError']);
  let mounted = false, lastReport = 0, timer, dialog, output;
  let lastResult = 'No report sent yet';
  const bindings = [];
  const errorName = name => errors.has(name) ? name : 'Other';
  function record(kind, details = {}) {
    events.push({ kind, at_ms: Date.now() - started, ...details });
    if (events.length > 60) events.shift();
  }
  function receipt(feed) {
    if (!['channel', 'workspace'].includes(feed)) return;
    const previous = feeds[feed] || { count: 0 };
    feeds[feed] = { count: previous.count + 1, at: Date.now() };
  }
  function request(method, path, status, duration, failure) {
    const resource = String(path).match(/^\/api\/[a-z-]{1,32}(?=[/?]|$)/)?.[0];
    if (!resource || resource === '/api/client-diagnostics') return;
    record('request', { method, path: resource, status, duration_ms: Math.max(0, duration),
      ...(failure ? { error: errorName(failure) } : {}) });
  }
  function snapshot(reason = 'manual') {
    const ua = navigator.userAgent || '';
    return {
      session, reason, build: document.querySelector('meta[name="nth-build"]')?.content || '',
      online: navigator.onLine !== false, hidden: !!document.hidden,
      browser: /Firefox\//.test(ua) ? 'Firefox' : /Chrome\/|Chromium\//.test(ua) ? 'Chromium' : /Safari\//.test(ua) ? 'Safari' : 'Other',
      platform: /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : 'Desktop',
      width: window.innerWidth, height: window.innerHeight, uptime_ms: Date.now() - started,
      feeds: Object.fromEntries(Object.entries(feeds).map(([k, f]) => [k, { count: f.count, age_ms: Math.max(0, Date.now() - f.at) }])),
      events: events.map(e => ({ ...e })),
    };
  }
  // Independent, bounded request: reporting failure never changes chat behaviour.
  async function report(reason = 'manual', beacon = false) {
    if (Date.now() - lastReport < 10000) {
      if (reason === 'manual') lastResult = 'Please wait ten seconds between reports';
      return false;
    }
    lastReport = Date.now();
    let timeout;
    try {
      const body = JSON.stringify(snapshot(reason));
      if (body.length > 16384) return false;
      if (beacon && navigator.sendBeacon) {
        lastResult = navigator.sendBeacon('/api/client-diagnostics', new Blob([body], { type: 'application/json' }))
          ? 'Report queued; receipt unconfirmed' : 'Report could not be queued';
        return false;
      }
      const controller = new AbortController();
      timeout = setTimeout(() => controller.abort(), 5000);
      const response = await fetch('/api/client-diagnostics', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body, signal: controller.signal, cache: 'no-store',
      });
      lastResult = response.ok ? 'Report received by hub' : `Report rejected (${response.status})`;
      return response.ok;
    } catch { lastResult = 'Report could not reach hub'; return false; }
    finally { clearTimeout(timeout); if (output) output.textContent = JSON.stringify({ ...snapshot(), report: lastResult }, null, 2); }
  }
  function bind(target, event, fn) { target.addEventListener(event, fn); bindings.push(() => target.removeEventListener(event, fn)); }
  function mount() {
    if (mounted) return;
    mounted = true;
    bind(Trio.events, 'connection', e => {
      record('connection', { state: e.detail?.state });
      if (/stale|reconnecting/.test(e.detail?.state || '')) void report('connection');
    });
    for (const name of ['online', 'offline', 'pageshow', 'pagehide']) bind(window, name, () => {
      record(name); void report(name, name === 'pagehide');
    });
    bind(document, 'visibilitychange', () => {
      const kind = document.hidden ? 'hidden' : 'visible';
      record(kind); void report(kind, document.hidden);
    });
    // Messages, stack traces and source URLs may contain private data. Names
    // and line numbers are sufficient to distinguish browser failure classes.
    bind(window, 'error', e => { record('error', { error: errorName(e.error?.name), line: e.lineno || 0 }); void report('error'); });
    bind(window, 'unhandledrejection', e => { record('rejection', { error: errorName(e.reason?.name) }); void report('rejection'); });
    let lastTick = Date.now();
    const stopTimer = () => { clearInterval(timer); timer = null; };
    const startTimer = () => {
      if (document.hidden || timer) return;
      lastTick = Date.now();
      timer = setInterval(() => {
      const lag = Math.max(0, Date.now() - lastTick - 60000); lastTick = Date.now();
      if (!document.hidden) { record('visible', { lag_ms: lag }); void report('interval'); }
      }, 60000);
      timer.unref?.();
    };
    bind(document, 'visibilitychange', () => document.hidden ? stopTimer() : startTimer());
    bind(window, 'pagehide', stopTimer);
    bind(window, 'pageshow', startTimer);
    startTimer();
    void report('startup');
  }
  function unmount() { bindings.splice(0).forEach(fn => fn()); clearInterval(timer); timer = null; mounted = false; }
  function show() {
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.setAttribute('aria-label', 'Connection diagnostics');
      dialog.style.cssText = 'width:min(600px,90vw);max-height:85dvh;box-sizing:border-box;background:var(--bg,#151515);color:inherit;border:1px solid #666;border-radius:12px;padding:16px';
      const title = document.createElement('h2'); title.textContent = 'Connection diagnostics'; dialog.appendChild(title);
      const note = document.createElement('p'); note.textContent = 'Connection events, request timings and error classes. No message text, drafts or credentials. The hub keeps its last 100 reports until restart.'; dialog.appendChild(note);
      output = document.createElement('pre'); output.style.cssText = 'max-height:45dvh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px'; dialog.appendChild(output);
      for (const [label, action] of [
        ['Copy', async () => { try { await navigator.clipboard.writeText(output.textContent); } catch { lastResult = 'Copy unavailable — use Download'; } }],
        ['Download', () => { const link = document.createElement('a'); const url = URL.createObjectURL(new Blob([output.textContent], { type: 'application/json' })); link.href = url; link.download = 'quartet-connection-diagnostics.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }],
        ['Send report', async () => { await report('manual'); }],
        ['Close', () => dialog.close()],
      ]) {
        const button = document.createElement('button'); button.textContent = label; button.style.cssText = 'margin:4px;padding:10px';
        button.addEventListener('click', async () => { await action(); output.textContent = JSON.stringify({ ...snapshot(), report: lastResult }, null, 2); }); dialog.appendChild(button);
      }
      document.body.appendChild(dialog);
    }
    output.textContent = JSON.stringify({ ...snapshot(), report: lastResult }, null, 2);
    dialog.showModal();
  }
  Trio.diagnostics = { mount, unmount, receipt, request, snapshot, report, show };
})();
