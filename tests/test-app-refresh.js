// Reload and update for the installed app (48-app-refresh.js), run against
// the ACTUAL shipped modules through the Node DOM harness:
//   • pull to refresh: the threshold, the indicator text, and every place a
//     pull must NOT start (composer, drawers, dialogs, form fields, the
//     long-press time copy, a scrolled or loading list, a text selection,
//     two fingers, sideways or upward drags, desktop browsers);
//   • the "Update available" pill when the served build differs;
//   • the "Reload app" item in the account menu;
//   • drafts surviving the reload, and a reload refused rather than losing
//     an unsent image.
// The harness never dispatches events, so the gesture handlers are driven
// directly with event-shaped objects, which is what the listeners receive.
// Usage: node tests/test-app-refresh.js
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { load, FakeElement } = require('./dom-harness');

const failures = []; let passed = 0;
async function check(name, fn) {
  try { await fn(); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + e.message); }
}
const tick = () => new Promise(resolve => setTimeout(resolve, 5));

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

let reloads = 0;
win.location.reload = () => { reloads++; };
const toasts = [];
Trio.ui.toast = message => { toasts.push(String(message)); };

// An installed app on a touch phone unless a check says otherwise.
function phone({ displayMode = 'standalone', touchPoints = 5 } = {}) {
  win.matchMedia = query => ({
    matches: query === `(display-mode: ${displayMode})`,
    addEventListener() {}, removeEventListener() {}, addListener() {},
  });
  win.navigator.maxTouchPoints = touchPoints;
}

let buildMeta = 'build-loaded';
doc.querySelector = selector => (selector === 'meta[name="nth-build"]'
  ? { getAttribute: name => (name === 'content' ? buildMeta : null) }
  : null);

function el(tag, className, parent) {
  const node = new FakeElement(tag);
  if (className) node.className = className;
  if (parent) parent.appendChild(node);
  return node;
}
// A tiny page: header with a button, a message list holding a message with a
// time, the composer, the sidebar, the channel drawer and a dialog.
const shell = el('main', 'conversation-shell');
const header = el('header', 'conversation-header topbar', shell);
const headerButton = el('button', 'icon-btn', header);
const list = el('section', 'messages msgs', shell);
const message = el('article', 'message msg', list);
const messageBody = el('div', 'message-body', message);
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

function reset() {
  pull.cancel();
  phone();
  list.scrollTop = 0;
  list.removeAttribute('aria-busy');
  doc.getElementById('app').classList.remove('nav-open', 'channel-details-open');
  state.activeMessageActions = null;
  delete win.getSelection;
  state.attachmentStore = {};
  state.pendingAttachments = [];
  reloads = 0;
  toasts.length = 0;
}

(async () => {
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
    await tick();
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
    await tick();
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
    await tick();
    assert.strictEqual(reloads, 0);
    assert.strictEqual(indicator().hidden, true);
  });

  await check('the message list is a pull surface when it is already at its top', async () => {
    reset();
    assert.ok(startOn(messageBody, 200, 300));
    moveTo(200, 300 + travel(PULL.threshold));
    assert.strictEqual(lift(), true);
    await tick();
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
      await tick();
      assert.strictEqual(reloads, 0);
    });
  }

  await check('no pull from a message list that is scrolled down (normal scrolling)', async () => {
    reset();
    list.scrollTop = 240;
    assert.strictEqual(startOn(messageBody), false);
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
    pull.current().at -= PULL.holdMs + 100;     // the finger has been down that long
    assert.strictEqual(moveTo(200, 300 + travel(PULL.max)), null);
    assert.strictEqual(lift(), false);
    await tick();
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

  await check('reduced motion stops the spin and the arrow animation', async () => {
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
    assert.ok(/@keyframes updatePillIn\{[^}]*opacity:0;[^}]*\}[^}]*opacity:1;[^}]*\}\s*\}/.test(css)
      && !/@keyframes updatePillIn\{[^@]*transform/.test(css.split('@keyframes updatePillIn')[1].split('}\n')[0]),
      'updatePillIn animates opacity only');
  });

  // ── Version mismatch pill ───────────────────────────────────────────
  let served = 'build-loaded';
  let fetches = 0;
  win.fetch = async url => {
    fetches++;
    assert.strictEqual(url, '/api/version');
    return { ok: true, json: async () => ({ build: served, version: '8.3.0' }) };
  };
  const pill = () => doc.getElementById('update-pill');

  await check('no pill while the served build matches the loaded one', async () => {
    reset();
    served = 'build-loaded';
    assert.strictEqual(await refresh.checkForUpdate(), false);
    assert.ok(pill().hidden !== false, 'pill stays hidden');
    assert.strictEqual(fetches, 1);
  });

  await check('a different served build shows "Update available" with a Reload button', async () => {
    reset();
    served = 'build-newer';
    assert.strictEqual(await refresh.checkForUpdate(), true);
    assert.strictEqual(pill().hidden, false);
    assert.strictEqual(pill().parentNode, doc.getElementById('app'), 'the pill is on the page, inside #app');
    assert.ok(/Update available/.test(pill().textContent));
    const reload = pill().querySelector('.update-pill-reload');
    assert.strictEqual(reload.textContent, 'Reload');
    reload._listeners.click[0]();
    await tick();
    assert.strictEqual(reloads, 1, 'Reload reloads');
  });

  await check('dismissing hides the pill until a newer build appears', async () => {
    reset();
    served = 'build-newer';
    await refresh.checkForUpdate();
    pill().querySelector('.update-pill-dismiss')._listeners.click[0]();
    assert.strictEqual(pill().hidden, true);
    await refresh.checkForUpdate();
    assert.strictEqual(pill().hidden, true, 'the same build stays dismissed');
    served = 'build-newest';
    await refresh.checkForUpdate();
    assert.strictEqual(pill().hidden, false, 'a later build shows it again');
  });

  await check('an unreachable hub shows no pill and no error', async () => {
    reset();
    pill().hidden = true;
    win.fetch = async () => { throw new TypeError('Failed to fetch'); };
    assert.strictEqual(await refresh.checkForUpdate(), false);
    assert.strictEqual(pill().hidden, true);
    assert.strictEqual(toasts.length, 0);
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
    served = 'build-loaded';
    win.fetch = async () => ({ ok: true, json: async () => ({ build: served }) });
    await refresh.checkForUpdate();
    const items = openMenu();
    const item = items.querySelectorAll('.account-action')
      .find(b => b.getAttribute('data-account-action') === 'reload');
    assert.ok(item, 'the reload item is present');
    assert.ok(/Reload app/.test(item.textContent));
    item._listeners.click[0]();
    await tick();
    assert.strictEqual(reloads, 1);
    assert.strictEqual(doc.getElementById('account-trigger').getAttribute('aria-expanded'), 'false',
      'the menu closes first');
  });

  await check('with an update waiting the item says so', async () => {
    reset();
    served = 'build-newer';
    await refresh.checkForUpdate();
    const items = openMenu();
    const item = items.querySelectorAll('.account-action')[0];
    assert.ok(/Update and reload/.test(item.textContent));
    assert.ok(/new/.test(item.textContent));
  });

  // ── Drafts across the reload ────────────────────────────────────────
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

  await check('the next page load restores them before the composer mounts, once', async () => {
    const carried = makeSessionStorage(Object.fromEntries(session._map));
    const next = load({ setup: w => { w.sessionStorage = carried; } });
    const s = next.hooks.Trio.state;
    assert.strictEqual(s.drafts.ops, 'half-typed message to ops');
    assert.strictEqual(s.drafts['dm:ada'], 'reply to Ada later');
    assert.deepStrictEqual(s.targetDrafts.ops, ['m-1']);
    assert.strictEqual(carried.getItem(next.hooks.Trio.appRefresh.DRAFTS_KEY), null, 'consumed, not left behind');
  });

  await check('a snapshot from an earlier visit is ignored', async () => {
    const old = makeSessionStorage({
      [refresh.DRAFTS_KEY]: JSON.stringify({ at: Date.now() - 60 * 60 * 1000, drafts: { ops: 'from an hour ago' } }),
    });
    const next = load({ setup: w => { w.sessionStorage = old; } });
    assert.strictEqual(next.hooks.Trio.state.drafts.ops, undefined);
  });

  await check('drafts are also saved when the page goes away for any other reason', async () => {
    reset();
    session._map.clear();
    state.drafts = { ops: 'kept on pagehide' };
    doc.getElementById('input').textContent = '';
    assert.strictEqual(refresh.saveDrafts(), true);
    assert.strictEqual(JSON.parse(session.getItem(refresh.DRAFTS_KEY)).drafts.ops, 'kept on pagehide');
  });

  await check('an unsent image refuses the reload with a reason instead of losing it', async () => {
    reset();
    state.attachmentStore = { ops: [{ name: 'photo.png' }] };
    assert.strictEqual(await refresh.reloadApp(), false);
    await tick();
    assert.strictEqual(reloads, 0);
    assert.ok(/image is still waiting to be sent/.test(toasts[0] || ''), toasts[0]);
    // The pull hides its indicator when the reload is refused.
    startOn(headerButton, 200, 100);
    moveTo(200, 100 + travel(PULL.threshold));
    lift();
    await tick(); await tick();
    assert.strictEqual(reloads, 0);
    assert.strictEqual(indicator().hidden, true);
  });

  console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
  if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
  process.exit(failures.length ? 1 : 0);
})();
