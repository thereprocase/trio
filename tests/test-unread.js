// Unread-watermark tests. Extracts the real functions from the shipped
// dashboard bundle in nth_web.py and runs them against a fake message map, so
// the read-state rules are pinned without a browser.
//
// Usage: node tests/test-unread.js
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const WEB_PY = path.join(__dirname, '..', 'server', 'nth_web.py');
const src = fs.readFileSync(WEB_PY, 'utf8');

function grab(name) {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`could not find function ${name} in nth_web.py`);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error(`unbalanced braces reading ${name}`);
}

// Fake message node: only classList is exercised by the read-state rules.
function node(...classes) {
  const set = new Set(classes);
  return { classList: { contains: c => set.has(c), add: c => set.add(c), remove: c => set.delete(c) } };
}

let removedDivider = false, newBarText = null;
const sandbox = {
  state: null,
  document: { getElementById: () => (removedDivider ? null : { remove() { removedDivider = true; } }) },
  newBar: { classList: { add() {}, remove() {} }, set textContent(v) { newBarText = v; } },
  chat: { insertBefore() {} },
  Math, console,
};
const code = [grab('isHiddenMsg'), grab('firstVisibleUnreadDom'), grab('unreadCountVisible'),
              grab('markCaughtUp'), grab('seedBaseline')].join('\n');
const fn = new Function('sandbox', `with (sandbox) { ${code};
  return { isHiddenMsg, firstVisibleUnreadDom, unreadCountVisible, markCaughtUp, seedBaseline,
           refreshUnreadDivider: () => {}, updateNewBar: () => {} }; }`);
// refreshUnreadDivider/updateNewBar are stubbed via the returned closure below
sandbox.refreshUnreadDivider = () => {};
sandbox.updateNewBar = () => {};
const H = fn(sandbox);
sandbox.refreshUnreadDivider = H.refreshUnreadDivider;
sandbox.updateNewBar = H.updateNewBar;

let passed = 0; const failures = [];
function check(name, f) {
  try { f(); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + e.message); }
}
function mkState(over) {
  return Object.assign({ lastSeenId: 0, messageDomById: new Map(), members: new Map(),
                         operator: { id: 'op' }, suppressCatchUp: false }, over);
}

// ── the regression Frodo and Sauron both found ───────────────────────────────
check('markCaughtUp stops at a filtered-out unread (does not sweep it)', () => {
  const m = new Map();
  m.set(10, node()); m.set(11, node('filtered-out')); m.set(12, node('filtered-out'));
  m.set(205, node());
  sandbox.state = mkState({ lastSeenId: 10, messageDomById: m });
  H.markCaughtUp();
  assert.strictEqual(sandbox.state.lastSeenId, 10,
    'watermark must not jump over messages the filter is hiding');
});

check('markCaughtUp advances through dm-hidden (structurally invisible)', () => {
  const m = new Map();
  m.set(1, node()); m.set(2, node('dm-hidden')); m.set(3, node());
  sandbox.state = mkState({ lastSeenId: 0, messageDomById: m });
  H.markCaughtUp();
  assert.strictEqual(sandbox.state.lastSeenId, 3,
    'dm-hidden must not deadlock the watermark');
});

check('markCaughtUp advances over a fully visible tail', () => {
  const m = new Map();
  m.set(1, node()); m.set(2, node()); m.set(3, node());
  sandbox.state = mkState({ lastSeenId: 1, messageDomById: m });
  H.markCaughtUp();
  assert.strictEqual(sandbox.state.lastSeenId, 3);
});

check('clearing the filter restores the still-unread message', () => {
  const m = new Map();
  const hidden = node('filtered-out');
  m.set(876, node()); m.set(3523, hidden); m.set(3524, node());
  sandbox.state = mkState({ lastSeenId: 876, messageDomById: m });
  H.markCaughtUp();                       // user is "at bottom" of the 1-row filtered list
  hidden.classList.remove('filtered-out'); // filter cleared
  assert.strictEqual(sandbox.state.lastSeenId, 876);
  assert.strictEqual(H.unreadCountVisible(), 2, '3523 and 3524 are both still unread');
});

// ── baseline seeding (the background-tab case) ───────────────────────────────
check('seedBaseline with no server last_read → arrive caught up', () => {
  const m = new Map(); m.set(5, node()); m.set(6, node()); m.set(7, node());
  sandbox.state = mkState({ messageDomById: m });
  H.seedBaseline();
  assert.strictEqual(sandbox.state.lastSeenId, 7);
  assert.strictEqual(H.unreadCountVisible(), 0);
});

check('seedBaseline honours the server watermark → history stays unread', () => {
  const m = new Map(); m.set(5, node()); m.set(6, node()); m.set(7, node());
  const members = new Map([['op', { last_read: 5 }]]);
  sandbox.state = mkState({ messageDomById: m, members });
  H.seedBaseline();
  assert.strictEqual(sandbox.state.lastSeenId, 5);
  assert.strictEqual(H.unreadCountVisible(), 2, '6 and 7 arrived while away');
});

check('seedBaseline is idempotent once a watermark exists', () => {
  const m = new Map(); m.set(9, node());
  sandbox.state = mkState({ lastSeenId: 3, messageDomById: m });
  H.seedBaseline();
  assert.strictEqual(sandbox.state.lastSeenId, 3);
});

check('seedBaseline clamps a server watermark ahead of loaded history', () => {
  const m = new Map(); m.set(2, node());
  const members = new Map([['op', { last_read: 999 }]]);
  sandbox.state = mkState({ messageDomById: m, members });
  H.seedBaseline();
  assert.strictEqual(sandbox.state.lastSeenId, 2);
});

check('firstVisibleUnreadDom skips hidden and returns the lowest unread', () => {
  const m = new Map();
  const want = node();
  m.set(1, node()); m.set(2, node('dm-hidden')); m.set(3, want); m.set(4, node());
  sandbox.state = mkState({ lastSeenId: 1, messageDomById: m });
  assert.strictEqual(H.firstVisibleUnreadDom(), want);
});

console.log('');
console.log((failures.length ? 'FAILED' : 'OK') + ` — ${passed} passed, ${failures.length} failure(s)`);
process.exit(failures.length ? 1 : 0);
