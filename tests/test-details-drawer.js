// Regression test for the right-side channel details drawer open/close, run
// against the ACTUAL shipped 20-workspace.js via the Node DOM harness.
//
// THE BUG THIS GUARDS (regression from ff105ad):
//   showDetails(refresh) grew an early-return `if (refresh && !drawer.open) return`
//   so an already-CLOSED drawer wouldn't repaint on a background conversation
//   switch. But mount() binds `showDetails` DIRECTLY as the details-btn click
//   handler — so a real click passes the click Event as `refresh`. That truthy
//   Event tripped the guard and the drawer never opened: the button looked dead.
//
// The fix coerces `refresh = refresh === true`, so only the explicit
// showDetails(true) refresh caller is treated as a refresh; a click Event (or
// any other truthy value) opens the drawer as a user expects.
//
// We reproduce the EXACT production wiring: bind showDetails as a click handler
// and fire an Event through it — not just call showDetails() with no args, which
// would pass even with the bug present.
//
// Usage: node tests/test-details-drawer.js
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
const ws = H.Trio.workspace;
const state = H.Trio.state;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

// Minimal state so showDetails() can render without throwing (mirrors an open
// channel with an empty roster — the drawer template tolerates empties).
function seed() {
  state.channels = [{ code: 'test', topic: 'A topic' }];
  state.channel = 'test';
  state.dms = { your_dms: [] };
  state.dmKey = ''; state.dmThread = null;
}
const drawer = () => doc.getElementById('channel-drawer');
const isOpen = () => drawer().classList.contains('open');
function closeDrawer() { drawer().classList.remove('open'); doc.getElementById('app').classList.remove('channel-details-open'); }

// Fire a stored listener with an event, the way a real click would. The fake
// DOM records listeners but never dispatches, so we invoke them directly.
function fireClick(el, ev) { (el._listeners.click || []).forEach(fn => fn(ev)); }

check('showDetails() opens the drawer (baseline sanity)', () => {
  seed(); closeDrawer();
  ws.showDetails();
  assert.strictEqual(isOpen(), true);
});

check('clicking the details button opens the drawer (regression)', () => {
  seed(); closeDrawer();
  // Reproduce mount()'s exact wiring: showDetails IS the click handler.
  const btn = doc.getElementById('details-btn');
  btn._listeners.click = [];            // start clean
  btn.addEventListener('click', ws.showDetails);
  assert.strictEqual((btn._listeners.click || []).length, 1, 'handler should be bound');
  fireClick(btn, { type: 'click', currentTarget: btn, preventDefault() {} });
  assert.strictEqual(isOpen(), true, 'a real click Event must open the drawer, not trip the refresh guard');
  assert.strictEqual(doc.getElementById('app').classList.contains('channel-details-open'), true);
  assert.strictEqual(drawer().getAttribute('aria-hidden'), 'false');
});

check('showDetails(true) on a CLOSED drawer stays closed (guard preserved)', () => {
  seed(); closeDrawer();
  ws.showDetails(true);   // background conversation-switch refresh: must NOT force-open
  assert.strictEqual(isOpen(), false);
});

check('showDetails(true) on an OPEN drawer refreshes it (stays open)', () => {
  seed(); closeDrawer();
  ws.showDetails();                       // open first
  assert.strictEqual(isOpen(), true);
  ws.showDetails(true);                    // refresh path
  assert.strictEqual(isOpen(), true, 'refresh should keep an open drawer open');
});

check('a truthy non-boolean (e.g. Event) is treated as open, not refresh', () => {
  seed(); closeDrawer();
  ws.showDetails({ type: 'click' });       // truthy, but not === true
  assert.strictEqual(isOpen(), true);
});

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) { console.error('FAILURES: ' + failures.join(', ')); process.exit(1); }
