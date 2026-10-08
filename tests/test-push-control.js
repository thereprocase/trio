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

  console.log(`\n${failures.length ? 'FAILED' : 'OK'} — ${passed} passed, ${failures.length} failed`);
  process.exit(failures.length ? 1 : 0);
})();
