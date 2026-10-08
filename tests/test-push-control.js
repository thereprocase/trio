// The "Phone notifications" control (server/web/js/47-push.js), run as shipped
// through the Node DOM harness.
//
// What matters most here is ORDER inside the tap handler: iOS shows the
// notification prompt only while the user's gesture is still active, so
// Notification.requestPermission() has to be called before the handler awaits
// anything. A version that fetched the VAPID key first would work on Android
// and silently never prompt on an iPhone. The click test below checks the
// prompt was requested synchronously, before any promise had a chance to run.
//
// Also covered: the iOS "add to Home Screen first" hint, the https hint, the
// subscribe and unsubscribe requests the control sends, and that the channel
// drawer carries the control for channels and not for DMs.
//
// Usage: node tests/test-push-control.js
'use strict';

const assert = require('assert');
const { load, FakeElement } = require('./dom-harness');

const failures = [];
let passed = 0;
async function check(name, fn) {
  try { await fn(); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + (e && e.message)); }
}

const cx = load();
const win = cx.window;
const Trio = cx.hooks.Trio;
const push = Trio.push;
win.atob = atob;            // a browser global the harness does not provide
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function settle() { for (let i = 0; i < 20; i++) await tick(); }

const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1';
const ANDROID = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36';

// A browser that supports web push, with a recording fetch and service worker.
const calls = [];
let permissionCalls = 0;
const ENDPOINT = 'https://fcm.googleapis.com/fcm/send/device-1';
const subscription = {
  endpoint: ENDPOINT,
  options: { applicationServerKey: null },
  toJSON() { return { endpoint: ENDPOINT, keys: { p256dh: 'BPUB', auth: 'AUTH' } }; },
  unsubscribe: () => Promise.resolve(true),
};
let current = null;            // the browser's subscription, once made
const pushManager = {
  getSubscription: () => Promise.resolve(current),
  subscribe: opts => { subscription.options.applicationServerKey = opts.applicationServerKey.buffer; current = subscription; return Promise.resolve(current); },
};
function pushCapable(ua) {
  win.navigator.userAgent = ua;
  win.navigator.standalone = undefined;
  win.navigator.maxTouchPoints = 0;
  win.isSecureContext = true;
  win.PushManager = function PushManager() {};
  win.navigator.serviceWorker = {
    register: () => Promise.resolve({}),
    ready: Promise.resolve({ pushManager }),
    addEventListener() {}, removeEventListener() {},
  };
  win.Notification.permission = 'default';
  win.Notification.requestPermission = () => { permissionCalls++; win.Notification.permission = 'granted'; return Promise.resolve('granted'); };
}
const VAPID = 'BAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKiss' + 'LS4vMDEyMzQ1Njc4OTo7PD0-P0BBQg';
let serverSubs = [];
win.fetch = async (url, init = {}) => {
  const method = init.method || 'GET';
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push({ url, method, body });
  let data = { ok: true };
  if (url.startsWith('/api/push/status')) data = { enabled: true, named: true, subscriptions: serverSubs, secure_url: '' };
  else if (url.startsWith('/api/push/vapid-public-key')) data = { enabled: true, publicKey: VAPID };
  else if (url === '/api/push/subscribe') { serverSubs = [{ endpoint: body.subscription.endpoint, mode: body.mode }]; }
  else if (url === '/api/push/unsubscribe') { serverSubs = []; data = { ok: true, removed: 1 }; }
  const text = JSON.stringify(data);
  return { ok: true, status: 200, text: async () => text, json: async () => data };
};

const buttons = section => section.querySelectorAll('.push-mode');
const button = (section, mode) => buttons(section).find(b => b.getAttribute('data-push-mode') === mode);
const pressed = section => buttons(section).filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.getAttribute('data-push-mode'));
const click = el => (el._listeners.click || []).forEach(fn => fn({ type: 'click' }));

(async () => {
  await check('modes are the server contract: all, mentions, every5m, off', () => {
    assert.deepStrictEqual([...push.MODES.map(([id]) => id)], ['all', 'mentions', 'every5m', 'off']);
  });

  await check('keyBytes decodes base64url', () => {
    assert.deepStrictEqual([...push.keyBytes('AQID_-8')], [1, 2, 3, 255, 239]);
  });

  await check('iOS Safari outside the Home Screen is told to install first', () => {
    pushCapable(IPHONE);
    const why = push.blocker('');
    assert.match(why, /Home Screen/);
    assert.match(why, /iOS 16\.4/);
  });

  await check('iPadOS (reports as a Mac, has touch) gets the same hint', () => {
    pushCapable('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15');
    win.navigator.maxTouchPoints = 5;
    assert.match(push.blocker(''), /Home Screen/);
  });

  await check('an installed iOS app has no blocker', () => {
    pushCapable(IPHONE);
    win.navigator.standalone = true;
    assert.strictEqual(push.blocker(''), '');
  });

  await check('plain http names the https address to use', () => {
    pushCapable(ANDROID);
    win.isSecureContext = false;
    assert.match(push.blocker('https://hub.example.ts.net:8765/'), /https:\/\/hub\.example\.ts\.net:8765\//);
  });

  await check('blocked notifications say where to allow them', () => {
    pushCapable(ANDROID);
    win.Notification.permission = 'denied';
    assert.match(push.blocker(''), /blocked/);
  });

  const section = new FakeElement('section');
  await check('render draws a heading and the four mode buttons', async () => {
    pushCapable(ANDROID);
    calls.length = 0;
    push.render(section, 'ops');
    await settle();
    assert.strictEqual(section.querySelector('h3').textContent, 'Phone notifications');
    assert.deepStrictEqual(buttons(section).map(b => b.textContent), ['Every message', 'Mentions', 'Every 5 min', 'Off']);
    assert.ok(calls.some(c => c.url === '/api/push/status?channel=ops'), 'status was not fetched');
    assert.deepStrictEqual(pressed(section), ['off']);
    assert.match(section.querySelector('.push-status').textContent, /Off/);
  });

  await check('a tap asks for permission synchronously, then subscribes with the mode', async () => {
    calls.length = 0; permissionCalls = 0;
    click(button(section, 'mentions'));
    // Nothing has been awaited yet: the prompt must already have been requested.
    assert.strictEqual(permissionCalls, 1, 'requestPermission was not called inside the gesture');
    await settle();
    const post = calls.find(c => c.url === '/api/push/subscribe');
    assert.ok(post, 'no subscribe request');
    assert.strictEqual(post.method, 'POST');
    assert.deepStrictEqual(post.body, { subscription: { endpoint: ENDPOINT, keys: { p256dh: 'BPUB', auth: 'AUTH' } }, channel: 'ops', mode: 'mentions' });
    assert.deepStrictEqual(pressed(section), ['mentions']);
    assert.match(section.querySelector('.push-status').textContent, /Mentions/);
  });

  await check('reopening shows the stored mode for this device', async () => {
    const again = new FakeElement('section');
    push.render(again, 'ops');
    await settle();
    assert.deepStrictEqual(pressed(again), ['mentions']);
  });

  await check('changing mode does not prompt again once granted', async () => {
    calls.length = 0; permissionCalls = 0;
    click(button(section, 'every5m'));
    await settle();
    assert.strictEqual(permissionCalls, 0);
    assert.strictEqual(calls.find(c => c.url === '/api/push/subscribe').body.mode, 'every5m');
    assert.deepStrictEqual(pressed(section), ['every5m']);
  });

  await check('Off removes only this channel, without prompting', async () => {
    calls.length = 0; permissionCalls = 0;
    click(button(section, 'off'));
    await settle();
    assert.strictEqual(permissionCalls, 0);
    const post = calls.find(c => c.url === '/api/push/unsubscribe');
    assert.ok(post, 'no unsubscribe request');
    assert.deepStrictEqual(post.body, { endpoint: ENDPOINT, channel: 'ops' });
    assert.deepStrictEqual(pressed(section), ['off']);
  });

  await check('a refused prompt changes nothing on the server', async () => {
    pushCapable(ANDROID);
    win.Notification.requestPermission = () => { win.Notification.permission = 'denied'; return Promise.resolve('denied'); };
    const s = new FakeElement('section');
    push.render(s, 'ops');
    await settle();
    calls.length = 0;
    click(button(s, 'all'));
    await settle();
    assert.ok(!calls.some(c => c.url === '/api/push/subscribe'), 'subscribed without permission');
    assert.match(s.querySelector('.push-status').textContent, /not allowed/);
  });

  await check('iOS outside the Home Screen shows the hint and disables the buttons', async () => {
    pushCapable(IPHONE);
    const s = new FakeElement('section');
    push.render(s, 'ops');
    await settle();
    const hint = s.querySelector('.push-hint');
    assert.strictEqual(hint.hidden, false);
    assert.match(hint.textContent, /Home Screen/);
    assert.ok(buttons(s).every(b => b.disabled));
  });

  await check('the channel drawer carries the control for a channel, not for a DM', async () => {
    pushCapable(ANDROID);
    const state = Trio.state;
    state.channels = [{ code: 'ops', topic: 'Ops' }];
    state.channel = 'ops';
    state.dms = { your_dms: [] };
    state.dmKey = ''; state.dmThread = null;
    Trio.workspace.showDetails();
    const body = cx.document.getElementById('channel-drawer-body');
    assert.match(body.innerHTML, /push-section/);
    state.dmKey = 'dm-1';
    state.dmThread = { key: 'dm-1', name: 'Ada' };
    Trio.workspace.showDetails();
    assert.doesNotMatch(body.innerHTML, /push-section/);
    state.dmKey = ''; state.dmThread = null;
  });

  await check('a 409 replaces the browser subscription, retries, and moves the identity\'s other channels', async () => {
    pushCapable(ANDROID);
    win.Notification.permission = 'granted';
    current = subscription;
    let unsubscribed = 0, n = 0;
    const second = { ...subscription, endpoint: ENDPOINT + '-new', toJSON() { return { endpoint: ENDPOINT + '-new', keys: { p256dh: 'BPUB', auth: 'AUTH' } }; } };
    subscription.unsubscribe = () => { unsubscribed++; current = null; return Promise.resolve(true); };
    pushManager.subscribe = opts => { second.options = { applicationServerKey: opts.applicationServerKey.buffer }; current = second; return Promise.resolve(second); };
    const realFetch = win.fetch;
    win.fetch = async (url, init = {}) => {
      if (url === '/api/push/subscribe' && n++ === 0) {
        calls.push({ url, method: 'POST', body: JSON.parse(init.body) });
        const text = JSON.stringify({ error: 'this device is subscribed under another identity' });
        return { ok: false, status: 409, text: async () => text, json: async () => JSON.parse(text) };
      }
      if (url.startsWith('/api/push/status') && n > 0) {
        // This identity also had #dev on the old endpoint.
        calls.push({ url, method: 'GET', body: null });
        const data = { enabled: true, delivering: true, named: true, subscriptions: [],
                       mine: [{ channel: 'dev', endpoint: ENDPOINT, mode: 'mentions' },
                              { channel: 'ops', endpoint: ENDPOINT + '-new', mode: 'all' }] };
        const text = JSON.stringify(data);
        return { ok: true, status: 200, text: async () => text, json: async () => data };
      }
      return realFetch(url, init);
    };
    try {
      const s = new FakeElement('section');
      push.render(s, 'ops');
      await settle();
      calls.length = 0;
      click(button(s, 'all'));
      await settle();
      const posts = calls.filter(c => c.url === '/api/push/subscribe');
      assert.strictEqual(unsubscribed, 1);
      assert.strictEqual(posts.length, 3);
      assert.strictEqual(posts[1].body.subscription.endpoint, ENDPOINT + '-new');
      // The other channel moves to the new endpoint with its own mode...
      assert.deepStrictEqual([posts[2].body.channel, posts[2].body.mode, posts[2].body.subscription.endpoint],
                             ['dev', 'mentions', ENDPOINT + '-new']);
      // ...and the old endpoint's rows are cleared.
      const unsub = calls.find(c => c.url === '/api/push/unsubscribe');
      assert.deepStrictEqual(unsub && unsub.body, { endpoint: ENDPOINT });
      assert.deepStrictEqual(pressed(s), ['all']);
      assert.match(s.querySelector('.push-status').textContent, /#dev/);
    } finally { win.fetch = realFetch; }
  });

  await check('a server with no sending hub says so', async () => {
    pushCapable(ANDROID);
    const realFetch = win.fetch;
    win.fetch = async (url, init) => {
      if (url.startsWith('/api/push/status')) {
        const text = JSON.stringify({ enabled: true, delivering: false, named: true, subscriptions: [] });
        return { ok: true, status: 200, text: async () => text, json: async () => JSON.parse(text) };
      }
      return realFetch(url, init);
    };
    try {
      const s = new FakeElement('section');
      push.render(s, 'ops');
      await settle();
      const hint = s.querySelector('.push-hint');
      assert.strictEqual(hint.hidden, false);
      assert.match(hint.textContent, /no hub is sending/);
      assert.ok(buttons(s).every(b => !b.disabled), 'the choice can still be saved');
    } finally { win.fetch = realFetch; }
  });

  // The service worker, run as shipped against a fake worker global.
  function loadWorker(windows) {
    const listeners = {}; const opened = []; const focused = []; const posted = [];
    const self = {
      location: { origin: 'https://hub.example.ts.net:8765' },
      addEventListener: (type, fn) => { listeners[type] = fn; },
      skipWaiting() {},
      registration: { showNotification: () => Promise.resolve() },
      clients: {
        claim: () => Promise.resolve(),
        matchAll: () => Promise.resolve(windows.map(url => ({ url, focus() { focused.push(url); return Promise.resolve(this); }, postMessage(m) { posted.push(m); } }))),
        openWindow: url => { opened.push(url); return Promise.resolve(null); },
      },
    };
    const src = require('fs').readFileSync(require('path').resolve(__dirname, '..', 'server', 'web', 'sw.js'), 'utf8');
    require('vm').runInNewContext(src, { self, URL });
    async function clickNotification(data) {
      let done;
      listeners.notificationclick({ notification: { data, close() {} }, waitUntil: p => { done = p; } });
      await done;
    }
    return { clickNotification, opened, focused, posted };
  }

  await check('service worker: a click opens the channel when no window is open', async () => {
    const w = loadWorker([]);
    await w.clickNotification({ url: '/?channel=ops', channel: 'ops' });
    assert.deepStrictEqual(w.opened, ['https://hub.example.ts.net:8765/?channel=ops']);
  });

  await check('service worker: a foreign URL in the payload opens the app home instead', async () => {
    const w = loadWorker([]);
    await w.clickNotification({ url: 'https://evil.example.com/phish', channel: 'ops' });
    assert.deepStrictEqual(w.opened, ['https://hub.example.ts.net:8765/']);
  });

  await check('service worker: an open window on the channel is focused, not duplicated', async () => {
    const w = loadWorker(['https://hub.example.ts.net:8765/?channel=ops']);
    await w.clickNotification({ url: '/?channel=ops', channel: 'ops' });
    assert.deepStrictEqual(w.focused, ['https://hub.example.ts.net:8765/?channel=ops']);
    assert.strictEqual(w.opened.length, 0);
  });

  await check('service worker: an app open elsewhere is told to switch channel', async () => {
    const w = loadWorker(['https://hub.example.ts.net:8765/tasks']);
    await w.clickNotification({ url: '/?channel=ops', channel: 'ops' });
    assert.strictEqual(w.opened.length, 0);
    assert.strictEqual(w.posted[0]?.type, 'nth-open-channel');
    assert.strictEqual(w.posted[0]?.channel, 'ops');
  });

  console.log(`\n${failures.length ? 'FAILED' : 'OK'} — ${passed} passed, ${failures.length} failed`);
  process.exit(failures.length ? 1 : 0);
})();
