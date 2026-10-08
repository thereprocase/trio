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
    // No show_text: nobody touched the checkbox and no row says otherwise,
    // so the hub's default (hidden) applies.
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

  await check('a 409 replaces the browser subscription, retries, and moves the identity\'s other channels server-side', async () => {
    pushCapable(ANDROID);
    win.Notification.permission = 'granted';
    current = subscription;
    serverSubs = [];                 // this identity has no row on this channel yet
    let unsubscribed = 0, conflicted = false;
    const second = { ...subscription, endpoint: ENDPOINT + '-new', toJSON() { return { endpoint: ENDPOINT + '-new', keys: { p256dh: 'BPUB', auth: 'AUTH' } }; } };
    subscription.unsubscribe = () => { unsubscribed++; current = null; return Promise.resolve(true); };
    pushManager.subscribe = opts => { second.options = { applicationServerKey: opts.applicationServerKey.buffer }; current = second; return Promise.resolve(second); };
    const realFetch = win.fetch;
    const reply = (status, data) => { const text = JSON.stringify(data); return { ok: status < 300, status, text: async () => text, json: async () => data }; };
    win.fetch = async (url, init = {}) => {
      const body = init.body ? JSON.parse(init.body) : null;
      if (url === '/api/push/subscribe' && body.subscription.endpoint === ENDPOINT && !conflicted) {
        conflicted = true;
        calls.push({ url, method: 'POST', body });
        return reply(409, { error: 'this device is subscribed under another identity' });
      }
      if (url === '/api/push/move') {
        calls.push({ url, method: 'POST', body });
        return reply(200, { ok: true, moved: ['dev'] });
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
      assert.deepStrictEqual(posts.map(p => p.body.subscription.endpoint), [ENDPOINT, ENDPOINT + '-new']);
      // The identity's other channels move in ONE server-side call, so a
      // member at its quota cannot lose one in between.
      const move = calls.filter(c => c.url === '/api/push/move');
      assert.strictEqual(move.length, 1);
      assert.strictEqual(move[0].body.old_endpoint, ENDPOINT);
      assert.strictEqual(move[0].body.subscription.endpoint, ENDPOINT + '-new');
      assert.ok(!calls.some(c => c.url === '/api/push/unsubscribe'), 'no unsubscribe-then-resubscribe dance');
      assert.deepStrictEqual(pressed(s), ['all']);
      assert.match(s.querySelector('.push-status').textContent, /#dev/);
    } finally { win.fetch = realFetch; }
  });

  await check('opening the control quietly renews a subscribed device with its current mode', async () => {
    pushCapable(ANDROID);
    win.Notification.permission = 'granted';
    current = subscription;
    subscription.unsubscribe = () => Promise.resolve(true);
    serverSubs = [{ endpoint: ENDPOINT, mode: 'every5m' }];
    permissionCalls = 0;
    calls.length = 0;
    const s = new FakeElement('section');
    push.render(s, 'ops');
    await settle();
    const posts = calls.filter(c => c.url === '/api/push/subscribe');
    assert.strictEqual(permissionCalls, 0, 'no permission prompt');
    assert.strictEqual(posts.length, 1);
    assert.deepStrictEqual([posts[0].body.channel, posts[0].body.mode, posts[0].body.subscription.endpoint],
                           ['ops', 'every5m', ENDPOINT]);
    assert.deepStrictEqual(pressed(s), ['every5m']);
  });

  await check('a device that is not subscribed here is not renewed', async () => {
    serverSubs = [];
    calls.length = 0;
    const s = new FakeElement('section');
    push.render(s, 'ops');
    await settle();
    assert.ok(!calls.some(c => c.url === '/api/push/subscribe'));
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

  // ── Per-device controls: lock-screen text, last delivery, Send test ──
  const fire = (el, type) => (el._listeners[type] || []).forEach(fn => fn({ type }));
  const visible = el => el && el.hidden === false;

  await check('droppedNotice: only when this device was subscribed here and the row is gone', () => {
    const base = { rememberedMode: 'all', subscribedHere: false, named: true, blocked: false };
    assert.strictEqual(push.droppedNotice(base), true);
    assert.strictEqual(push.droppedNotice({ ...base, subscribedHere: true }), false, 'still subscribed');
    assert.strictEqual(push.droppedNotice({ ...base, rememberedMode: '' }), false, 'never subscribed on this device');
    assert.strictEqual(push.droppedNotice({ ...base, rememberedMode: 'off' }), false, 'turned off on purpose');
    assert.strictEqual(push.droppedNotice({ ...base, named: false }), false, 'an unnamed visitor has no rows to show');
    assert.strictEqual(push.droppedNotice({ ...base, blocked: true }), false, 'the blocker already explains');
  });

  await check('lastDelivered: never, a time today, and a date on another day', () => {
    const now = new Date(2026, 2, 3, 15, 0);
    assert.strictEqual(push.lastDelivered(null, now), 'never');
    assert.strictEqual(push.lastDelivered(0, now), 'never');
    assert.strictEqual(push.lastDelivered(new Date(2026, 2, 3, 9, 5).getTime() / 1000, now), '09:05');
    const other = push.lastDelivered(new Date(2026, 2, 1, 14, 30).getTime() / 1000, now);
    assert.match(other, /^14:30, \S/);
    assert.doesNotMatch(other, /2026/, 'same year needs no year');
    assert.match(push.lastDelivered(new Date(2025, 11, 31, 8, 0).getTime() / 1000, now), /2025/);
  });

  // A fetch for these tests: rows carry show_text / last_ok_at like the real
  // status, and each endpoint the page calls is recorded.
  function deviceServer() {
    const state = { rows: [], calls: [], testStatus: 200 };
    const reply = (status, data) => { const text = JSON.stringify(data); return { ok: status < 300, status, text: async () => text, json: async () => data }; };
    win.fetch = async (url, init = {}) => {
      const body = init.body ? JSON.parse(init.body) : null;
      state.calls.push({ url, body });
      if (url.startsWith('/api/push/status')) return reply(200, { enabled: true, named: true, subscriptions: state.rows, secure_url: '' });
      if (url.startsWith('/api/push/vapid-public-key')) return reply(200, { enabled: true, publicKey: VAPID });
      if (url === '/api/push/subscribe') {
        const prior = state.rows.find(r => r.endpoint === body.subscription.endpoint);
        state.rows = state.rows.filter(r => r.endpoint !== body.subscription.endpoint);
        const row = { endpoint: body.subscription.endpoint, mode: body.mode,
                      show_text: body.show_text ?? prior?.show_text ?? false, last_ok_at: prior?.last_ok_at ?? null };
        state.rows.push(row);
        return reply(200, { ok: true, show_text: row.show_text });
      }
      if (url === '/api/push/settings') {
        const row = state.rows.find(r => r.endpoint === body.endpoint);
        if (!row) return reply(404, { error: 'this device is not subscribed to notifications for #dev' });
        row.show_text = body.show_text;
        return reply(200, { ok: true, show_text: body.show_text });
      }
      if (url === '/api/push/test') {
        if (state.testStatus === 429) return reply(429, { error: 'wait 7 s before sending another test' });
        return reply(200, { ok: true, last_ok_at: new Date(2030, 0, 1, 12, 34).getTime() / 1000 });
      }
      if (url === '/api/push/unsubscribe') { state.rows = state.rows.filter(r => r.endpoint !== body.endpoint); return reply(200, { ok: true, removed: 1 }); }
      if (url === '/api/push/move') return reply(200, { ok: true, moved: [] });
      return reply(404, { error: 'unexpected ' + url });
    };
    return state;
  }
  function devicePhone() {
    pushCapable(ANDROID);
    win.Notification.permission = 'granted';
    pushManager.subscribe = opts => { subscription.options.applicationServerKey = opts.applicationServerKey.buffer; current = subscription; return Promise.resolve(current); };
    subscription.unsubscribe = () => { current = null; return Promise.resolve(true); };
    win.localStorage.clear();
  }
  const realFetchForDevice = win.fetch;

  await check('the text checkbox is off by default and goes out with the first subscribe', async () => {
    devicePhone();
    current = null;
    const server = deviceServer();
    try {
      const s = new FakeElement('section');
      push.render(s, 'dev');
      await settle();
      const box = s.querySelector('.push-show-text');
      assert.ok(box, 'no checkbox');
      assert.match(s.querySelector('.push-option').textContent, /Show message text on the lock screen/);
      assert.strictEqual(box.checked, false);
      assert.ok(!visible(s.querySelector('.push-device')), 'device line shown before subscribing');
      box.checked = true;
      fire(box, 'change');
      await settle();
      assert.ok(!server.calls.some(c => c.url === '/api/push/settings'), 'nothing to change on the server yet');
      assert.match(s.querySelector('.push-status').textContent, /once notifications are on/);
      click(button(s, 'all'));
      await settle();
      const post = server.calls.find(c => c.url === '/api/push/subscribe');
      assert.strictEqual(post.body.show_text, true);
      assert.ok(visible(s.querySelector('.push-device')), 'device line hidden after subscribing');
      assert.strictEqual(s.querySelector('.push-last').textContent, 'Last delivered: never');
    } finally { win.fetch = realFetchForDevice; }
  });

  await check('a subscribed device shows its stored choice and last delivery; the checkbox saves at once', async () => {
    devicePhone();
    current = subscription;
    const server = deviceServer();
    const when = new Date(); when.setHours(7, 42, 0, 0);
    server.rows = [{ endpoint: ENDPOINT, mode: 'mentions', show_text: true, last_ok_at: when.getTime() / 1000 }];
    try {
      const s = new FakeElement('section');
      push.render(s, 'dev');
      await settle();
      const box = s.querySelector('.push-show-text');
      assert.strictEqual(box.checked, true);
      assert.strictEqual(s.querySelector('.push-last').textContent, 'Last delivered: 07:42');
      assert.ok(visible(s.querySelector('.push-device')));
      assert.ok(!visible(s.querySelector('.push-dropped')), 'no dropped notice while subscribed');
      server.calls.length = 0;
      box.checked = false;
      fire(box, 'change');
      await settle();
      const post = server.calls.find(c => c.url === '/api/push/settings');
      assert.deepStrictEqual(post.body, { endpoint: ENDPOINT, channel: 'dev', show_text: false });
      assert.ok(!server.calls.some(c => c.url === '/api/push/subscribe'), 'no re-subscribe');
      assert.strictEqual(server.rows[0].show_text, false);
      assert.match(s.querySelector('.push-status').textContent, /hide message text/);
    } finally { win.fetch = realFetchForDevice; }
  });

  await check('Send test posts this device only and updates the last-delivered line; a refusal is shown', async () => {
    devicePhone();
    current = subscription;
    const server = deviceServer();
    server.rows = [{ endpoint: ENDPOINT, mode: 'all', show_text: false, last_ok_at: null }];
    try {
      const s = new FakeElement('section');
      push.render(s, 'dev');
      await settle();
      assert.strictEqual(s.querySelector('.push-test').textContent, 'Send test');
      server.calls.length = 0;
      click(s.querySelector('.push-test'));
      await settle();
      const post = server.calls.find(c => c.url === '/api/push/test');
      assert.deepStrictEqual(post.body, { endpoint: ENDPOINT, channel: 'dev' });
      assert.match(s.querySelector('.push-last').textContent, /^Last delivered: 12:34, /);
      assert.match(s.querySelector('.push-status').textContent, /Test sent/);
      server.testStatus = 429;
      click(s.querySelector('.push-test'));
      await settle();
      assert.match(s.querySelector('.push-status').textContent, /Wait 7 s/);
      assert.strictEqual(s.querySelector('.push-test').disabled, false, 'button stays usable');
    } finally { win.fetch = realFetchForDevice; }
  });

  await check('a device the hub dropped is told so, and "Turn back on" re-subscribes with a fresh endpoint', async () => {
    devicePhone();
    current = subscription;
    const server = deviceServer();
    server.rows = [{ endpoint: ENDPOINT, mode: 'mentions', show_text: false, last_ok_at: null }];
    const s = new FakeElement('section');
    try {
      push.render(s, 'dev');
      await settle();
      assert.ok(!visible(s.querySelector('.push-dropped')));
      // The hub gives up on the endpoint after repeated refusals.
      server.rows = [];
      const again = new FakeElement('section');
      push.render(again, 'dev');
      await settle();
      const notice = again.querySelector('.push-dropped');
      assert.ok(visible(notice), 'no notice after the hub dropped the device');
      assert.match(notice.textContent, /This device no longer gets notifications for #dev\. Turn them back on\?/);
      assert.deepStrictEqual(pressed(again), ['off']);
      // The resubscribe replaces the browser endpoint the push service refused.
      const fresh = { ...subscription, endpoint: ENDPOINT + '-fresh', toJSON() { return { endpoint: ENDPOINT + '-fresh', keys: { p256dh: 'BPUB', auth: 'AUTH' } }; } };
      let unsubscribed = 0;
      subscription.unsubscribe = () => { unsubscribed++; current = null; return Promise.resolve(true); };
      pushManager.subscribe = opts => { fresh.options = { applicationServerKey: opts.applicationServerKey.buffer }; current = fresh; return Promise.resolve(fresh); };
      server.calls.length = 0;
      click(again.querySelector('.push-resubscribe'));
      await settle();
      const posts = server.calls.filter(c => c.url === '/api/push/subscribe');
      assert.strictEqual(unsubscribed, 1, 'old browser subscription kept');
      assert.deepStrictEqual(posts.map(p => [p.body.subscription.endpoint, p.body.mode]), [[ENDPOINT + '-fresh', 'mentions']]);
      assert.strictEqual(server.calls.filter(c => c.url === '/api/push/move').length, 1, 'other channels not moved along');
      assert.ok(!visible(notice), 'notice still shown after turning back on');
      assert.deepStrictEqual(pressed(again), ['mentions']);
    } finally {
      win.fetch = realFetchForDevice;
      current = subscription;
      pushManager.subscribe = opts => { subscription.options.applicationServerKey = opts.applicationServerKey.buffer; current = subscription; return Promise.resolve(current); };
    }
  });

  await check('a tap before the status loads leaves show_text out, so a stored "show" survives', async () => {
    devicePhone();
    current = subscription;
    const server = deviceServer();
    server.rows = [{ endpoint: ENDPOINT, mode: 'all', show_text: true, last_ok_at: null }];
    const realDeviceFetch = win.fetch;
    // The status request fails; every other call reaches the fake hub.
    win.fetch = async (url, init) => {
      if (url.startsWith('/api/push/status')) {
        const text = JSON.stringify({ error: 'hub busy' });
        return { ok: false, status: 503, text: async () => text, json: async () => JSON.parse(text) };
      }
      return realDeviceFetch(url, init);
    };
    try {
      const s = new FakeElement('section');
      push.render(s, 'dev');
      await settle();
      assert.strictEqual(s.querySelector('.push-show-text').checked, false, 'unknown state shows unticked');
      server.calls.length = 0;
      click(button(s, 'mentions'));
      await settle();
      const post = server.calls.find(c => c.url === '/api/push/subscribe');
      assert.ok(post, 'no subscribe');
      assert.ok(!('show_text' in post.body), 'an unknown checkbox state was sent: ' + JSON.stringify(post.body));
      assert.strictEqual(server.rows[0].show_text, true, 'stored choice overwritten');
      assert.strictEqual(s.querySelector('.push-show-text').checked, true, 'the page shows the stored choice afterwards');
    } finally { win.fetch = realFetchForDevice; }
  });

  await check('a loaded row or a touched checkbox does go out with the subscribe', async () => {
    devicePhone();
    current = subscription;
    const server = deviceServer();
    server.rows = [{ endpoint: ENDPOINT, mode: 'all', show_text: true, last_ok_at: null }];
    try {
      const s = new FakeElement('section');
      push.render(s, 'dev');
      await settle();
      server.calls.length = 0;
      click(button(s, 'mentions'));
      await settle();
      assert.strictEqual(server.calls.find(c => c.url === '/api/push/subscribe').body.show_text, true, 'loaded row');
      // Not subscribed, status loaded, checkbox touched: the tick is sent.
      server.rows = [];
      const t = new FakeElement('section');
      push.render(t, 'dev');
      await settle();
      const box = t.querySelector('.push-show-text');
      box.checked = true;
      fire(box, 'change');
      await settle();
      server.calls.length = 0;
      click(button(t, 'all'));
      await settle();
      assert.strictEqual(server.calls.find(c => c.url === '/api/push/subscribe').body.show_text, true, 'touched');
    } finally { win.fetch = realFetchForDevice; }
  });

  await check('no dropped notice on a channel this device never subscribed to, even with a browser subscription', async () => {
    devicePhone();
    current = subscription;
    const server = deviceServer();
    server.rows = [];
    try {
      const s = new FakeElement('section');
      push.render(s, 'never-here');
      await settle();
      assert.ok(!visible(s.querySelector('.push-dropped')));
    } finally { win.fetch = realFetchForDevice; }
  });

  await check('"Not now" and Off both stop the dropped notice from coming back', async () => {
    devicePhone();
    current = subscription;
    const server = deviceServer();
    try {
      for (const how of ['dismiss', 'off']) {
        win.localStorage.setItem('nth.push.subscribed.dev', 'all');
        server.rows = [];
        const s = new FakeElement('section');
        push.render(s, 'dev');
        await settle();
        assert.ok(visible(s.querySelector('.push-dropped')), how + ': notice not shown');
        click(how === 'dismiss' ? s.querySelector('.push-dismiss') : button(s, 'off'));
        await settle();
        assert.ok(!visible(s.querySelector('.push-dropped')), how + ': notice still shown');
        const next = new FakeElement('section');
        push.render(next, 'dev');
        await settle();
        assert.ok(!visible(next.querySelector('.push-dropped')), how + ': notice came back');
      }
    } finally { win.fetch = realFetchForDevice; }
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
