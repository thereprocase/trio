// Client-side tests for the collapsible left sidebar (channels + DMs rail),
// run against the ACTUAL shipped `08-sidebar.js` via the Node DOM harness
// (tests/dom-harness.js).
//
// This is the sidebar's first automated coverage — before this, the harness
// omitted 08-sidebar.js entirely, so the toggle button that opens/closes the
// channel/DM rail could silently break with no test catching it. The regression
// this guards against: the `#sidebar-toggle` button losing its click wiring, or
// `toggle()` no longer flipping the `.sidebar-collapsed` state + persistence.
//
// What the harness CAN exercise here (no network, no layout):
//   • the real click wiring on #sidebar-toggle and the #sidebar-resize dblclick
//     shortcut (listeners are recorded by the fake DOM; we invoke them),
//   • toggle() flipping `.app.sidebar-collapsed` + the `--sidebar-width` var +
//     localStorage persistence + the button's aria/icon sync,
//   • read() clamping the stored width and defaulting garbage,
//   • apply() reflecting a given state onto the DOM.
//
// What it does NOT cover (deliberate harness gaps — see dom-harness.js header):
//   • pointer-drag resize + snap-to-rail (needs live pointer events + geometry),
//   • the mobile off-canvas #nav-toggle (wired in 90-boot.js, which the harness
//     intentionally keeps from auto-running).
//
// Usage: node tests/test-sidebar.js
'use strict';

const assert = require('assert');
const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + e.message); }
}

const cx = load();
const H = cx.hooks;
const doc = cx.document;
const store = cx.window.localStorage;
const KEY = 'trio.sidebar.v1';
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

// The fake DOM records listeners but never dispatches; invoke them directly to
// simulate the user activating a control. This exercises the SAME function the
// browser would call on a real click/dblclick — i.e. the real toggle wiring.
function fire(el, type, ev = {}) {
  const ls = el._listeners && el._listeners[type];
  assert(ls && ls.length, `no "${type}" listener bound on #${el.id || el.tagName}`);
  ls.forEach(fn => fn(ev));
}
const app = () => doc.getElementById('app');
const btn = () => doc.getElementById('sidebar-toggle');
const width = () => app().style.getPropertyValue('--sidebar-width');
const persisted = () => JSON.parse(store.getItem(KEY) || '{}');
const isCollapsed = () => app().classList.contains('sidebar-collapsed');

// Put the sidebar into a known baseline (storage + DOM) so each test is
// independent of the order the others ran in — the click handler derives the
// next state from localStorage, so seeding it is enough.
function reset(collapsed, w = 300) {
  store.setItem(KEY, JSON.stringify({ collapsed, width: w }));
  H.sidebar.apply(H.sidebar.read());
}

// ── module surface ───────────────────────────────────────────────────────────
check('module publishes Trio.sidebar API', () => {
  assert.ok(H.sidebar, 'Trio.sidebar missing — 08-sidebar.js did not load');
  ['apply', 'toggle', 'read'].forEach(m =>
    assert.strictEqual(typeof H.sidebar[m], 'function', `sidebar.${m} not a function`));
});

// ── the wiring the user is asking about ──────────────────────────────────────
check('toggle button is wired to exactly one click handler', () => {
  const ls = btn()._listeners.click || [];
  assert.strictEqual(ls.length, 1, `expected 1 click listener, got ${ls.length}`);
});

check('resize handle offers dblclick collapse shortcut', () => {
  const ls = doc.getElementById('sidebar-resize')._listeners.dblclick || [];
  assert.strictEqual(ls.length, 1, `expected 1 dblclick listener, got ${ls.length}`);
});

// ── click behaviour: open ⇄ closed ───────────────────────────────────────────
check('clicking the toggle collapses an expanded sidebar', () => {
  reset(false);
  assert.strictEqual(isCollapsed(), false, 'baseline should be expanded');
  fire(btn(), 'click');
  assert.strictEqual(isCollapsed(), true, 'app should gain .sidebar-collapsed');
  assert.strictEqual(width(), '56px', '--sidebar-width should snap to the rail');
  assert.strictEqual(persisted().collapsed, true, 'collapsed state should persist');
  assert.strictEqual(btn().getAttribute('aria-expanded'), 'false');
  assert.strictEqual(btn().getAttribute('aria-label'), 'Expand sidebar');
});

check('clicking the toggle re-expands a collapsed sidebar', () => {
  reset(true);
  assert.strictEqual(isCollapsed(), true, 'baseline should be collapsed');
  fire(btn(), 'click');
  assert.strictEqual(isCollapsed(), false, 'app should lose .sidebar-collapsed');
  assert.strictEqual(width(), '300px', '--sidebar-width should restore stored width');
  assert.strictEqual(persisted().collapsed, false, 'expanded state should persist');
  assert.strictEqual(btn().getAttribute('aria-expanded'), 'true');
  assert.strictEqual(btn().getAttribute('aria-label'), 'Collapse sidebar');
});

check('dblclick on the resize handle also toggles', () => {
  reset(false);
  fire(doc.getElementById('sidebar-resize'), 'dblclick');
  assert.strictEqual(isCollapsed(), true, 'dblclick should collapse');
  fire(doc.getElementById('sidebar-resize'), 'dblclick');
  assert.strictEqual(isCollapsed(), false, 'second dblclick should expand');
});

check('toggle() API flips and persists without a DOM event', () => {
  reset(false);
  H.sidebar.toggle();
  assert.strictEqual(H.sidebar.read().collapsed, true);
  H.sidebar.toggle();
  assert.strictEqual(H.sidebar.read().collapsed, false);
});

// ── the collapse/expand chevron icon swaps ───────────────────────────────────
check('button icon swaps between collapse and expand chevrons', () => {
  reset(false);
  fire(btn(), 'click');                             // now collapsed → shows EXPAND (»)
  assert.ok(btn().innerHTML.includes('m13 9 3 3'), 'collapsed state should show expand chevron');
  fire(btn(), 'click');                             // now expanded → shows COLLAPSE («)
  assert.ok(btn().innerHTML.includes('m11 9-3 3'), 'expanded state should show collapse chevron');
});

// ── read() hardening ─────────────────────────────────────────────────────────
check('read() clamps an over-wide stored width to the ceiling', () => {
  store.setItem(KEY, JSON.stringify({ width: 9999 }));
  assert.strictEqual(H.sidebar.read().width, 480);
});
check('read() clamps a too-narrow stored width to the floor', () => {
  store.setItem(KEY, JSON.stringify({ width: 10 }));
  assert.strictEqual(H.sidebar.read().width, 220);
});
check('read() defaults a garbage stored width', () => {
  store.setItem(KEY, JSON.stringify({ width: 'nonsense' }));
  assert.strictEqual(H.sidebar.read().width, 300);
});
check('read() survives a corrupt JSON blob', () => {
  store.setItem(KEY, '{not json');
  const s = H.sidebar.read();
  assert.strictEqual(s.collapsed, false);
  assert.strictEqual(s.width, 300);
});
check('read() treats a zero width as the falsy default (not the floor)', () => {
  // Number(0) is falsy, so read() takes the DEFAULTS.width branch, not the clamp.
  store.setItem(KEY, JSON.stringify({ width: 0 }));
  assert.strictEqual(H.sidebar.read().width, 300);
});
check('read() clamps a negative width up to the floor', () => {
  store.setItem(KEY, JSON.stringify({ width: -50 }));
  assert.strictEqual(H.sidebar.read().width, 220);
});
check('read() preserves the exact floor and ceiling widths', () => {
  store.setItem(KEY, JSON.stringify({ width: 220 }));
  assert.strictEqual(H.sidebar.read().width, 220);
  store.setItem(KEY, JSON.stringify({ width: 480 }));
  assert.strictEqual(H.sidebar.read().width, 480);
});

// ── apply() reflects state onto the DOM ──────────────────────────────────────
check('apply(collapsed) sets the icon-rail width and class', () => {
  H.sidebar.apply({ collapsed: true, width: 300 });
  assert.strictEqual(isCollapsed(), true);
  assert.strictEqual(width(), '56px');
  assert.strictEqual(doc.getElementById('sidebar-resize').getAttribute('aria-valuenow'), '56');
});
check('apply(expanded) restores the stored width', () => {
  H.sidebar.apply({ collapsed: false, width: 340 });
  assert.strictEqual(isCollapsed(), false);
  assert.strictEqual(width(), '340px');
  assert.strictEqual(doc.getElementById('sidebar-resize').getAttribute('aria-valuenow'), '340');
});

// ── summary ──────────────────────────────────────────────────────────────────
console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) { console.error('FAILURES: ' + failures.join(', ')); process.exit(1); }
