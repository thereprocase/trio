// Reload and update for the installed app (48-app-refresh.js), run against
// the ACTUAL shipped modules through the Node DOM harness:
//   • pull to refresh: the threshold, the indicator text, every place a pull
//     must NOT start (composer, drawers, dialogs, form fields, the long-press
//     time copy, a scrolled or loading list, a text selection, two fingers,
//     sideways or upward drags, desktop browsers), a second finger mid-pull,
//     and the click guard after a pull;
//   • the "Update available" pill, and what triggers a check and how often;
//   • the "Reload app" item in the account menu;
//   • what a reload refuses to lose: unsent images, dictation in progress
//     (driven through the composer's real recorder path), and the whole app
//     when the hub is unreachable;
//   • drafts surviving the reload, down to the composer showing them.
// The harness never dispatches DOM events to elements, so the gesture handlers
// are driven directly with event-shaped objects, which is what the listeners
// receive; document/window/Trio.events listeners do fire through dispatchEvent.
// Usage: node tests/test-app-refresh.js
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { load, FakeElement } = require('./dom-harness');

const failures = []; let passed = 0;
// A check that never settles must fail, not end the process quietly: Node
// exits 0 once nothing is left to wait on, which would read as a pass.
const CHECK_TIMEOUT_MS = 8000;
async function check(name, fn) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('did not finish within ' + CHECK_TIMEOUT_MS + 'ms')), CHECK_TIMEOUT_MS);
  });
  try { await Promise.race([fn(), timeout]); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + e.message); }
  finally { clearTimeout(timer); }
}
let finished = false;
process.on('exit', code => {
  if (!finished && code === 0) { console.log('FAIL: the test run stopped before its summary'); process.exitCode = 1; }
});
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// A promise rejected inside the page and never handled is a bug the browser
// only logs; count them so the tests can say there were none.
let unhandled = 0;
process.on('unhandledRejection', () => { unhandled++; });

function makeSessionStorage(seed = {}) {
  const m = new Map(Object.entries(seed));
  return {
    getItem: k => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: k => m.delete(k),
    _map: m,
  };
}

const session = makeSessionStorage();
const cx = load({ setup: w => { w.sessionStorage = session; } });
const win = cx.window;
const doc = cx.document;
const Trio = cx.hooks.Trio;
const refresh = Trio.appRefresh;
const { pull, PULL } = refresh;
const state = Trio.state;

// The page's clock, movable by the tests. Only Date.now moves; new Date()
// still reads the real time.
let clockOffset = 0;
const RealDate = win.Date;
win.Date = class extends RealDate { static now() { return RealDate.now() + clockOffset; } };

let reloads = 0;
win.location.reload = () => { reloads++; };
const toasts = [];
Trio.ui.toast = message => { toasts.push(String(message)); };

// The hub, as /api/version answers it. `hub.down` makes every request fail.
const hub = { build: 'build-loaded', down: false, hang: false, fetches: 0 };
function defaultFetch(url) {
  hub.fetches++;
  assert.strictEqual(url, '/api/version');
  if (hub.hang) return new Promise(() => {});
  if (hub.down) return Promise.reject(new TypeError('Failed to fetch'));
  return Promise.resolve({ ok: true, json: async () => ({ build: hub.build }) });
}
win.fetch = defaultFetch;

// An installed app on a touch phone unless a check says otherwise.
function phone({ displayMode = 'standalone', touchPoints = 5 } = {}) {
  win.matchMedia = query => ({
    matches: query === `(display-mode: ${displayMode})`,
    addEventListener() {}, removeEventListener() {}, addListener() {},
  });
  win.navigator.maxTouchPoints = touchPoints;
}

const buildMeta = 'build-loaded';
doc.querySelector = selector => (selector === 'meta[name="nth-build"]'
  ? { getAttribute: name => (name === 'content' ? buildMeta : null) }
  : null);

function el(tag, className, parent) {
  const node = new FakeElement(tag);
  if (className) node.className = className;
  if (parent) parent.appendChild(node);
  return node;
}
// A tiny page: header with buttons, a message list holding a message with a
// time and a code block, the composer, the sidebar, the drawer and a dialog.
const shell = el('main', 'conversation-shell');
const header = el('header', 'conversation-header topbar', shell);
const headerButton = el('button', 'icon-btn', header);
const navToggle = el('button', 'icon-btn nav-toggle hamb', header);
const list = el('section', 'messages msgs', shell);
const message = el('article', 'message msg', list);
const messageBody = el('div', 'message-body', message);
const codeBlock = el('pre', '', messageBody);
const codeLine = el('code', '', codeBlock);
const msgTime = el('time', 'msg-time', message);
msgTime.setAttribute('datetime', '2026-10-08T12:00:00Z');
const composer = el('section', 'composer-shell composer-wrap', shell);
const composerInput = el('div', 'composer-input', composer);
const sidebarItem = el('button', 'nav-item', el('aside', 'sidebar'));
const drawerItem = el('div', 'channel-member', el('aside', 'channel-drawer'));
const dialogButton = el('button', '', el('dialog', ''));
const prefsSelect = el('select', 'pref-select', el('section', 'workspace-view'));
// Blocked elements that live INSIDE a pull surface, so only the blocklist
// stands between them and a pull: an ask card's free-text answer and the
// edit/delete menu on your own message.
const askInput = el('input', 'ask-custom', el('div', 'ask-card', message));
const actionsButton = el('button', '', el('div', 'message-actions-menu', message));

const at = (x, y) => [{ clientX: x, clientY: y }];
const startOn = (target, x = 200, y = 100, touches = at(x, y)) => pull.start({ target, touches });
const moveTo = (x, y) => pull.move({ touches: at(x, y) });
const lift = () => pull.end({ touches: [], changedTouches: at(0, 0) });
const indicator = () => doc.getElementById('pull-refresh');
const label = () => indicator().querySelector('.pull-refresh-label').textContent;
// Finger travel that lands exactly on a damped distance.
const travel = damped => PULL.deadZone + damped / PULL.damping;
const clickOn = target => {
  const event = { target, prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {} };
  pull.swallowClick(event);
  return event.prevented;
};
const settle = async () => { for (let i = 0; i < 4; i++) await tick(); };

function reset() {
  pull.cancel();
  phone();
  list.scrollTop = 0;
  codeBlock.scrollTop = 0;
  list.removeAttribute('aria-busy');
  doc.getElementById('app').classList.remove('nav-open', 'channel-details-open');
  state.activeMessageActions = null;
  delete win.getSelection;
  delete win.navigator.serviceWorker;
  state.attachmentStore = {};
  state.pendingAttachments = [];
  Object.assign(hub, { build: 'build-loaded', down: false, hang: false, fetches: 0 });
  win.fetch = defaultFetch;
  reloads = 0;
  toasts.length = 0;
}

(async () => {
  for (const status of [400, 401, 403, 404, 429, 499, 500, 502, 503]) {
    await check('reload reachability for HTTP ' + status, async () => {
      reset();
      win.fetch = () => Promise.resolve({ ok: false, status, json: () => { throw new Error('not JSON'); } });
      assert.strictEqual(await refresh.reloadApp(), status < 500);
      assert.strictEqual(reloads, status < 500 ? 1 : 0);
      assert.strictEqual(await refresh.checkForUpdate(), false, 'no invented build or update');
    });
  }
  // ── Pull threshold ──────────────────────────────────────────────────
  await check('a pull from the top bar shows "Pull to refresh" below the threshold and does not reload', async () => {
    reset();
    assert.ok(startOn(headerButton, 200, 100), 'the top bar is a pull surface');
    assert.strictEqual(moveTo(200, 100 + PULL.deadZone - 2), 'armed', 'inside the dead zone nothing is shown yet');
    assert.strictEqual(moveTo(200, 100 + travel(PULL.threshold - 10)), 'pulling');
    assert.strictEqual(indicator().hidden, false, 'the indicator is visible');
    assert.strictEqual(label(), 'Pull to refresh');
    assert.strictEqual(indicator().style.getPropertyValue('--pull'), (PULL.threshold - 10) + 'px',
      'the indicator follows the finger, damped');
    assert.strictEqual(lift(), false, 'released short of the threshold');
    await settle();
    assert.strictEqual(reloads, 0, 'no reload');
    assert.strictEqual(indicator().hidden, true, 'the indicator goes away');
  });

  await check('past the threshold it reads "Release to refresh", and releasing reloads', async () => {
    reset();
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold));
    assert.strictEqual(label(), 'Release to refresh');
    assert.ok(indicator().classList.contains('ready'));
    assert.strictEqual(lift(), true);
    assert.strictEqual(label(), 'Refreshing…');
    await settle();
    assert.strictEqual(reloads, 1, 'location.reload() was called once');
  });

  await check('the indicator stops at its maximum however far the finger goes', async () => {
    reset();
    startOn(headerButton, 200, 100);
    moveTo(200, 900);
    assert.strictEqual(indicator().style.getPropertyValue('--pull'), PULL.max + 'px');
    pull.cancel();
  });

  await check('going back above the start cancels the pull', async () => {
    reset();
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold + 10));
    assert.strictEqual(moveTo(200, 95), null);
    assert.strictEqual(lift(), false);
    await settle();
    assert.strictEqual(reloads, 0);
    assert.strictEqual(indicator().hidden, true);
  });

  await check('the message list is a pull surface when it is already at its top', async () => {
    reset();
    assert.ok(startOn(messageBody, 200, 300));
    moveTo(200, 300 + travel(PULL.threshold));
    assert.strictEqual(lift(), true);
    await settle();
    assert.strictEqual(reloads, 1);
  });

  // ── Places a pull must not start ────────────────────────────────────
  const refused = [
    ['inside the composer', composerInput],
    ['on the sidebar (the nav drawer)', sidebarItem],
    ['inside the channel details drawer', drawerItem],
    ['inside a dialog', dialogButton],
    ['on a message time (the long-press copy)', msgTime],
    ['on a form field on a workspace page', prefsSelect],
    ['in an ask card\'s text answer inside the message list', askInput],
    ['on a message\'s edit/delete menu inside the message list', actionsButton],
  ];
  for (const [where, target] of refused) {
    await check('no pull ' + where, async () => {
      reset();
      assert.strictEqual(startOn(target), false);
      assert.strictEqual(moveTo(200, 100 + travel(PULL.max)), null);
      assert.strictEqual(lift(), false);
      await settle();
      assert.strictEqual(reloads, 0);
    });
  }

  await check('no pull from a message list that is scrolled down (normal scrolling)', async () => {
    reset();
    list.scrollTop = 240;
    assert.strictEqual(startOn(messageBody), false);
  });

  await check('no pull from inside a scrolled code block, even with the list at its top', async () => {
    reset();
    codeBlock.scrollTop = 30;
    assert.strictEqual(startOn(codeLine), false);
    reset();
    assert.ok(startOn(codeLine, 200, 300), 'the same block at its top is fine');
    codeBlock.scrollTop = 12;
    assert.strictEqual(moveTo(200, 300 + travel(PULL.threshold)), null, 'and it cancels if it scrolls mid-gesture');
  });

  await check('a list that scrolls away from the top mid-gesture cancels', async () => {
    reset();
    assert.ok(startOn(messageBody, 200, 300));
    list.scrollTop = 12;
    assert.strictEqual(moveTo(200, 300 + travel(PULL.threshold)), null);
  });

  await check('no pull from a list that is still loading (aria-busy)', async () => {
    reset();
    list.setAttribute('aria-busy', 'true');
    assert.strictEqual(startOn(messageBody), false);
  });

  await check('no pull while the nav drawer, the details drawer or a message menu is open', async () => {
    reset();
    doc.getElementById('app').classList.add('nav-open');
    assert.strictEqual(startOn(headerButton), false, 'nav drawer');
    reset();
    doc.getElementById('app').classList.add('channel-details-open');
    assert.strictEqual(startOn(headerButton), false, 'details drawer');
    reset();
    state.activeMessageActions = el('div', 'message-actions-menu');
    assert.strictEqual(startOn(headerButton), false, 'message actions');
  });

  await check('no pull while text is selected', async () => {
    reset();
    win.getSelection = () => ({ toString: () => 'some selected words' });
    assert.strictEqual(startOn(messageBody), false);
  });

  await check('no pull with two fingers (pinch)', async () => {
    reset();
    assert.strictEqual(startOn(headerButton, 200, 100, [...at(200, 100), ...at(260, 140)]), false);
    reset();
    assert.ok(startOn(headerButton, 200, 100));
    assert.strictEqual(pull.move({ touches: [...at(200, 200), ...at(260, 240)] }), null,
      'a second finger mid-pull cancels');
  });

  await check('a second finger landing mid-pull clears the indicator; lifting both does nothing', async () => {
    reset();
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold + 20));
    assert.strictEqual(indicator().hidden, false);
    // The second finger's touchstart arrives before any further move.
    assert.strictEqual(startOn(headerButton, 200, 100, [...at(200, 100 + travel(PULL.threshold + 20)), ...at(280, 180)]), false);
    assert.strictEqual(indicator().hidden, true, 'the indicator is not left on screen');
    assert.strictEqual(pull.end({ touches: at(280, 180) }), false, 'first finger up');
    assert.strictEqual(lift(), false, 'second finger up');
    await settle();
    assert.strictEqual(reloads, 0);
    assert.strictEqual(indicator().hidden, true);
  });

  await check('a sideways swipe or an upward scroll is not a pull', async () => {
    reset();
    startOn(headerButton, 200, 100);
    assert.strictEqual(moveTo(260, 108), null, 'sideways');
    reset();
    startOn(messageBody, 200, 300);
    assert.strictEqual(moveTo(200, 270), null, 'upward');
  });

  await check('a finger that rests before moving is a long press, not a pull', async () => {
    reset();
    startOn(messageBody, 200, 300);
    pull.current().at -= PULL.holdMs + 100;     // touchdown was that long ago
    assert.strictEqual(moveTo(200, 300 + travel(PULL.max)), null);
    assert.strictEqual(lift(), false);
    await settle();
    assert.strictEqual(reloads, 0);
  });

  await check('off in a desktop browser tab and on a touch-less installed app', async () => {
    reset();
    phone({ displayMode: 'browser' });
    assert.strictEqual(startOn(headerButton), false, 'browser tab');
    reset();
    phone({ touchPoints: 0 });
    assert.strictEqual(startOn(headerButton), false, 'installed but no touch');
  });

  // ── The click after a pull ──────────────────────────────────────────
  await check('after a short pull, a quick tap on the nav toggle goes through', async () => {
    reset();
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold - 20));
    lift();
    // The tap: its own touchstart, no movement, then the click.
    startOn(navToggle, 40, 34);
    lift();
    assert.strictEqual(clickOn(navToggle), false, 'the tap is not eaten');
    // A deliberate tap on the very button the pull started on is a new touch.
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold - 20));
    lift();
    startOn(headerButton, 200, 100);
    lift();
    assert.strictEqual(clickOn(headerButton), false, 'a new tap on the start element is not eaten either');
  });

  await check('the click a browser fires on the pull\'s own start element is swallowed, nothing else is', async () => {
    reset();
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold - 20));
    lift();
    assert.strictEqual(clickOn(navToggle), false, 'a click elsewhere (keyboard, mouse) passes');
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold - 20));
    lift();
    assert.strictEqual(clickOn(headerButton), true, 'the release click on the start element is swallowed');
    assert.strictEqual(clickOn(headerButton), false, 'only once');
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold - 20));
    lift();
    clockOffset += 700;
    try { assert.strictEqual(clickOn(headerButton), false, 'and only briefly'); }
    finally { clockOffset -= 700; }
  });

  await check('reduced motion stops the spin and the arrow animation; the pill fits phones', async () => {
    const css = fs.readFileSync(path.join(__dirname, '..', 'server', 'web', 'css', '10-shell.css'), 'utf8');
    const block = css.match(/@media \(prefers-reduced-motion: reduce\)\{\s*\.pull-refresh-icon\{ transition:none; \}[\s\S]*?\}\s*\}/);
    assert.ok(block, 'a reduced-motion block covers the pull indicator');
    assert.ok(/\.pull-refresh\.refreshing \.pull-refresh-icon\{ animation:none; \}/.test(block[0]));
    assert.ok(/\.update-pill\{ animation:none; \}/.test(block[0]));
    assert.ok(/\.pull-refresh\[hidden\]\{ display:none; \}/.test(css) && /\.update-pill\[hidden\]\{ display:none; \}/.test(css),
      '[hidden] still hides both elements');
    assert.ok(/\.app\.nav-open \.update-pill\{ display:none; \}/.test(css), 'the pill steps aside for the open nav drawer');
    const pillAnimation = css.match(/\.update-pill\{[^}]*?animation:([^;]+);/)[1];
    assert.ok(/updatePillIn/.test(pillAnimation), 'the pill animation leaves its centring transform alone');
    const keyframes = css.split('@keyframes updatePillIn')[1].split('\n')[0];
    assert.ok(/opacity:0/.test(keyframes) && !/transform/.test(keyframes), 'updatePillIn animates opacity only');
    const coarse = css.match(/@media \(pointer:coarse\)\{[^@]*?\.update-pill-dismiss::after\{([^}]*)\}/);
    assert.ok(coarse && /inset:-8px/.test(coarse[1]), 'the dismiss button reaches 28 + 2x8 = 44px on touch');
    assert.ok(/\.update-pill\{ gap:8px; \}/.test(coarse[0]), 'with the gap widened so it does not cover Reload');
  });

  // ── Version mismatch pill ───────────────────────────────────────────
  const pill = () => doc.getElementById('update-pill');

  await check('no pill while the served build matches the loaded one', async () => {
    reset();
    assert.strictEqual(await refresh.checkForUpdate(), false);
    assert.ok(pill().hidden !== false, 'pill stays hidden');
    assert.strictEqual(hub.fetches, 1);
  });

  await check('a different served build shows "Update available" with a Reload button', async () => {
    reset();
    hub.build = 'build-newer';
    assert.strictEqual(await refresh.checkForUpdate(), true);
    assert.strictEqual(pill().hidden, false);
    assert.strictEqual(pill().parentNode, doc.getElementById('app'), 'the pill is on the page, inside #app');
    assert.ok(/Update available/.test(pill().textContent));
    const reload = pill().querySelector('.update-pill-reload');
    assert.strictEqual(reload.textContent, 'Reload');
    reload._listeners.click[0]();
    await settle();
    assert.strictEqual(reloads, 1, 'Reload reloads');
  });

  await check('dismissing hides the pill until a newer build appears', async () => {
    reset();
    hub.build = 'build-newer';
    await refresh.checkForUpdate();
    pill().querySelector('.update-pill-dismiss')._listeners.click[0]();
    assert.strictEqual(pill().hidden, true);
    await refresh.checkForUpdate();
    assert.strictEqual(pill().hidden, true, 'the same build stays dismissed');
    hub.build = 'build-newest';
    await refresh.checkForUpdate();
    assert.strictEqual(pill().hidden, false, 'a later build shows it again');
  });

  await check('an unreachable hub shows no pill and no error', async () => {
    reset();
    pill().hidden = true;
    hub.down = true;
    assert.strictEqual(await refresh.checkForUpdate(), false);
    assert.strictEqual(pill().hidden, true);
    assert.strictEqual(toasts.length, 0);
  });

  await check('checks run on return to the foreground, on reconnect and when back online, at most every 30s', async () => {
    reset();
    const cleanups = [];
    refresh.mount({ onUnmount: fn => cleanups.push(fn) });
    const gap = refresh.MIN_GAP_MS;
    try {
      clockOffset += gap + 1000;
      doc.dispatchEvent({ type: 'visibilitychange' });
      assert.strictEqual(hub.fetches, 1, 'visibilitychange checks');
      win.dispatchEvent({ type: 'online' });
      Trio.events.dispatchEvent(new CustomEvent('connection', { detail: { state: 'connected' } }));
      assert.strictEqual(hub.fetches, 1, 'nothing more inside the 30s gap');
      clockOffset += gap + 1000;
      win.dispatchEvent({ type: 'online' });
      assert.strictEqual(hub.fetches, 2, 'online checks');
      clockOffset += gap + 1000;
      Trio.events.dispatchEvent(new CustomEvent('connection', { detail: { state: 'live' } }));
      assert.strictEqual(hub.fetches, 2, 'a live tick is not a reconnect');
      Trio.events.dispatchEvent(new CustomEvent('connection', { detail: { state: 'workspace:connected' } }));
      assert.strictEqual(hub.fetches, 3, 'a reconnected stream checks');
      clockOffset += gap + 1000;
      doc.hidden = true;
      doc.dispatchEvent({ type: 'visibilitychange' });
      assert.strictEqual(hub.fetches, 3, 'a hidden page does not check');
    } finally {
      doc.hidden = false;
      cleanups.forEach(fn => fn());
    }
    clockOffset += gap + 1000;
    win.dispatchEvent({ type: 'online' });
    assert.strictEqual(hub.fetches, 3, 'unmounted: no listeners left behind');
    await settle();
  });

  // ── Account menu item ───────────────────────────────────────────────
  const ws = Trio.workspace;
  function openMenu() {
    doc.getElementById('account-trigger').setAttribute('aria-expanded', 'false');
    ws.openAccountMenu();
    return doc.getElementById('account-items');
  }
  await check('the account menu has a "Reload app" item that reloads', async () => {
    reset();
    await refresh.checkForUpdate();
    const items = openMenu();
    const item = items.querySelectorAll('.account-action')
      .find(b => b.getAttribute('data-account-action') === 'reload');
    assert.ok(item, 'the reload item is present');
    assert.ok(/Reload app/.test(item.textContent));
    item._listeners.click[0]();
    await settle();
    assert.strictEqual(reloads, 1);
    assert.strictEqual(doc.getElementById('account-trigger').getAttribute('aria-expanded'), 'false',
      'the menu closes first');
  });

  await check('with an update waiting the item says so', async () => {
    reset();
    hub.build = 'build-newer';
    await refresh.checkForUpdate();
    const items = openMenu();
    const item = items.querySelectorAll('.account-action')[0];
    assert.ok(/Update and reload/.test(item.textContent));
    assert.ok(/new/.test(item.textContent));
  });

  // ── What a reload refuses to lose ───────────────────────────────────
  await check('an unsent image refuses the reload with a reason instead of losing it', async () => {
    reset();
    state.attachmentStore = { ops: [{ name: 'photo.png' }] };
    assert.strictEqual(await refresh.reloadApp(), false);
    assert.strictEqual(reloads, 0);
    assert.ok(/image is still waiting to be sent/.test(toasts[0] || ''), toasts[0]);
    // The pull hides its indicator when the reload is refused.
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold));
    lift();
    await settle();
    assert.strictEqual(reloads, 0);
    assert.strictEqual(indicator().hidden, true);
  });

  await check('an unreachable hub refuses the reload instead of showing the browser\'s offline page', async () => {
    reset();
    session._map.clear();
    state.drafts = { ops: 'keep me' };
    hub.down = true;
    assert.strictEqual(await refresh.reloadApp(), false);
    assert.strictEqual(reloads, 0);
    assert.ok(/Can't reach the hub right now/.test(toasts[0] || ''), toasts[0]);
    assert.strictEqual(state.drafts.ops, 'keep me', 'the page and its draft stay as they were');
    toasts.length = 0;
    hub.down = false;
    hub.hang = true;     // a hub that accepts the connection and never answers
    const began = Date.now();
    assert.strictEqual(await refresh.reloadApp(), false);
    const waited = Date.now() - began;
    assert.ok(waited >= 1800 && waited < 3500, 'gives up after about 2s, took ' + waited + 'ms');
    assert.ok(/Can't reach the hub/.test(toasts[0] || ''));
    assert.strictEqual(reloads, 0);
  });

  await check('the service-worker wait is capped when update() never settles', async () => {
    reset();
    win.navigator.serviceWorker = {
      getRegistration: () => Promise.resolve({ update: () => new Promise(() => {}) }),
    };
    const began = Date.now();
    assert.strictEqual(await refresh.reloadApp(), true);
    const waited = Date.now() - began;
    assert.ok(waited >= refresh.WORKER_WAIT_MS - 50 && waited < refresh.WORKER_WAIT_MS + 800,
      'reloads after about ' + refresh.WORKER_WAIT_MS + 'ms, took ' + waited + 'ms');
    assert.strictEqual(reloads, 1);
  });

  await check('a late update() rejection after the cap is handled', async () => {
    reset();
    const before = unhandled;
    win.navigator.serviceWorker = {
      getRegistration: () => Promise.resolve({
        update: () => new Promise((_, reject) => setTimeout(() => reject(new Error('late')), refresh.WORKER_WAIT_MS + 200)),
      }),
    };
    const began = Date.now();
    assert.strictEqual(await refresh.reloadApp(), true);
    const waited = Date.now() - began;
    assert.ok(waited >= refresh.WORKER_WAIT_MS - 50 && waited < refresh.WORKER_WAIT_MS + 800,
      'reloads after about ' + refresh.WORKER_WAIT_MS + 'ms, took ' + waited + 'ms');
    assert.strictEqual(reloads, 1);
    await sleep(400);    // past the late rejection
    assert.strictEqual(unhandled, before, 'no unhandled rejection');
  });

  await check('an image attached during the service-worker wait still stops the reload', async () => {
    reset();
    win.navigator.serviceWorker = {
      getRegistration: () => Promise.resolve({
        update: () => { state.attachmentStore = { ops: [{ name: 'late.png' }] }; return Promise.resolve(); },
      }),
    };
    assert.strictEqual(await refresh.reloadApp(), false);
    assert.strictEqual(reloads, 0);
    assert.ok(/image is still waiting/.test(toasts[0] || ''), toasts[0]);
  });

  await check('dictation that is listening or transcribing refuses the reload (real composer recorder)', async () => {
    reset();
    const C = Trio.composer;
    let recorder = null;
    let finishTranscribe = null;
    const savedFetch = win.fetch;
    win.isSecureContext = true;
    win.MediaRecorder = function () {
      recorder = this; this.state = 'inactive';
      this.start = () => { this.state = 'recording'; };
      this.stop = () => { this.state = 'inactive'; this.onstop(); };
    };
    win.navigator.mediaDevices = { getUserMedia: () => Promise.resolve({ getTracks: () => [] }) };
    win.Blob = function () { this.type = 'audio/webm'; };
    win.fetch = url => {
      if (/transcribe/.test(url)) return new Promise(resolve => { finishTranscribe = () => resolve({ ok: true, json: () => Promise.resolve({ ok: true, text: 'hello' }) }); });
      if (/stt\/health/.test(url)) return Promise.resolve({ ok: true, json: () => Promise.resolve({ available: true, detail: 'ok' }) });
      return defaultFetch(url);
    };
    try {
      assert.strictEqual(C.dictationState(), '', 'idle');
      await C.refreshSttHealth();
      Trio.preferences.save({ sttMode: 'local' });
      await C.toggleDictation();
      assert.strictEqual(recorder?.state, 'recording');
      assert.strictEqual(C.dictationState(), 'recording');
      assert.strictEqual(await refresh.reloadApp(), false);
      assert.ok(/Dictation is still listening/.test(toasts.at(-1) || ''), toasts.at(-1));
      recorder.stop();                     // the recording goes off to be transcribed
      await tick();
      assert.strictEqual(C.dictationState(), 'transcribing');
      assert.strictEqual(await refresh.reloadApp(), false);
      assert.ok(/still being transcribed/.test(toasts.at(-1) || ''), toasts.at(-1));
      assert.strictEqual(reloads, 0);
      finishTranscribe();
      await settle();
      assert.strictEqual(C.dictationState(), '', 'idle once the text is back');
      assert.strictEqual(await refresh.reloadApp(), true);
      assert.strictEqual(reloads, 1);
    } finally {
      win.fetch = savedFetch;
      delete win.MediaRecorder;
    }
  });

  // ── Drafts across the reload ────────────────────────────────────────
  await check('the draft key is the composer\'s own conversation id', async () => {
    const cases = [{ channel: 'ops', dmKey: '' }, { channel: 'ops', dmKey: 'ada' }, { channel: '', dmKey: '' }];
    for (const c of cases) {
      Object.assign(state, c);
      assert.strictEqual(refresh.conversationKey(), Trio.composer.conversationId(), JSON.stringify(c));
    }
  });

  await check('reloading saves every draft, including the text in the box right now', async () => {
    reset();
    session._map.clear();
    state.channel = 'ops'; state.dmKey = '';
    state.drafts = { 'dm:ada': 'reply to Ada later', ops: 'stale copy', empty: '   ' };
    state.targetDrafts = { ops: ['m-1'] };
    doc.getElementById('input').textContent = 'half-typed message to ops';
    await refresh.reloadApp();
    assert.strictEqual(reloads, 1);
    const saved = JSON.parse(session.getItem(refresh.DRAFTS_KEY));
    assert.deepStrictEqual(saved.drafts, { 'dm:ada': 'reply to Ada later', ops: 'half-typed message to ops' });
    assert.deepStrictEqual(saved.targets, { ops: ['m-1'] });
  });

  await check('the next page load restores them, once, and the composer shows the draft', async () => {
    const carried = makeSessionStorage(Object.fromEntries(session._map));
    const next = load({ setup: w => { w.sessionStorage = carried; } });
    const T = next.hooks.Trio;
    const s = T.state;
    assert.strictEqual(s.drafts.ops, 'half-typed message to ops');
    assert.strictEqual(s.drafts['dm:ada'], 'reply to Ada later');
    assert.deepStrictEqual(s.targetDrafts.ops, ['m-1']);
    assert.strictEqual(carried.getItem(T.appRefresh.DRAFTS_KEY), null, 'consumed, not left behind');
    // The URL carried the channel through the reload; the composer's first
    // loadDraft() is what puts the text back in front of the user.
    s.channel = 'ops'; s.dmKey = '';
    T.composer.init();
    assert.strictEqual(next.document.getElementById('input').textContent, 'half-typed message to ops');
  });

  await check('a snapshot from an earlier visit is ignored', async () => {
    const old = makeSessionStorage({
      [refresh.DRAFTS_KEY]: JSON.stringify({ at: Date.now() - 60 * 60 * 1000, drafts: { ops: 'from an hour ago' } }),
    });
    const next = load({ setup: w => { w.sessionStorage = old; } });
    assert.strictEqual(next.hooks.Trio.state.drafts.ops, undefined);
  });

  await check('drafts are also saved when the page goes away, and dropped when it comes back from the bfcache', async () => {
    reset();
    session._map.clear();
    state.drafts = { ops: 'kept on pagehide' };
    doc.getElementById('input').textContent = '';
    assert.strictEqual(refresh.saveDrafts(), true);
    assert.strictEqual(JSON.parse(session.getItem(refresh.DRAFTS_KEY)).drafts.ops, 'kept on pagehide');
    refresh.onPageShow({ persisted: false });
    assert.ok(session.getItem(refresh.DRAFTS_KEY), 'an ordinary pageshow leaves it');
    refresh.onPageShow({ persisted: true });
    assert.strictEqual(session.getItem(refresh.DRAFTS_KEY), null, 'a bfcache restore clears it');
  });

  await check('no unhandled promise rejections anywhere above', async () => {
    await sleep(50);
    assert.strictEqual(unhandled, 0);
  });

  finished = true;
  console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
  if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
  process.exit(failures.length ? 1 : 0);
})();
