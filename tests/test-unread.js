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

let dividerPresent = true, newBarText = null;
function armDivider() { dividerPresent = true; }
const sandbox = {
  state: null,
  document: { getElementById: () => (dividerPresent ? { remove() { dividerPresent = false; } } : null) },
  newBar: { classList: { add() {}, remove() {} }, set textContent(v) { newBarText = v; } },
  chat: { insertBefore() {}, scrollHeight: 10000, clientHeight: 800, scrollTop: 0 },
  jumpBtn: { classList: { add() {}, remove() {} } },
  jumpCount: { style: {}, textContent: '' },
  USER_INTENT_MS: 1500,
  Date,
  Math, console,
};
const code = [grab('isHiddenMsg'), grab('firstVisibleUnreadDom'), grab('unreadCountVisible'),
              grab('markCaughtUp'), grab('seedBaseline'), grab('updateJumpButton'),
              grab('noteIntent'), grab('scrollIsUsers'), grab('sustainIntent'),
              grab('disownScroll'), grab('updateNewBar')].join('\n');
const fn = new Function('sandbox', `with (sandbox) { ${code};
  return { isHiddenMsg, firstVisibleUnreadDom, unreadCountVisible, markCaughtUp, seedBaseline,
           updateJumpButton, noteIntent, scrollIsUsers, sustainIntent, disownScroll, updateNewBar,
           refreshUnreadDivider: () => {} }; }`);
// refreshUnreadDivider/updateNewBar are stubbed via the returned closure below
sandbox.refreshUnreadDivider = () => {};
const H = fn(sandbox);
sandbox.refreshUnreadDivider = H.refreshUnreadDivider;

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

// ── the two criticals: who caused the scroll ────────────────────────────────
function atBottom() { sandbox.chat.scrollTop = sandbox.chat.scrollHeight - sandbox.chat.clientHeight; }

check('a programmatic scroll to the bottom does NOT mark caught up', () => {
  const m = new Map(); m.set(1, node()); m.set(2, node()); m.set(3, node());
  sandbox.state = mkState({ lastSeenId: 1, messageDomById: m, userIntentAt: 0 });
  armDivider(); atBottom();
  H.updateJumpButton();                       // settle / jump-to-unread, no gesture
  assert.strictEqual(sandbox.state.lastSeenId, 1, 'watermark must not move');
  assert.strictEqual(dividerPresent, true, 'divider must survive');
});

check('a user-driven scroll to the bottom DOES mark caught up', () => {
  const m = new Map(); m.set(1, node()); m.set(2, node()); m.set(3, node());
  sandbox.state = mkState({ lastSeenId: 1, messageDomById: m, userIntentAt: Date.now() });
  armDivider(); atBottom();
  H.updateJumpButton();
  assert.strictEqual(sandbox.state.lastSeenId, 3);
});

check('a stale gesture no longer counts as intent', () => {
  const m = new Map(); m.set(1, node()); m.set(2, node());
  sandbox.state = mkState({ lastSeenId: 1, messageDomById: m,
                            userIntentAt: Date.now() - 5000 });
  armDivider(); atBottom();
  H.updateJumpButton();
  assert.strictEqual(sandbox.state.lastSeenId, 1);
});

check('a long smooth scroll still cannot mark caught up at any point', () => {
  const m = new Map(); m.set(1, node()); m.set(2, node()); m.set(3, node());
  sandbox.state = mkState({ lastSeenId: 1, messageDomById: m, userIntentAt: 0 });
  armDivider();
  for (let i = 0; i < 174; i++) {            // the measured event count
    sandbox.chat.scrollTop = (sandbox.chat.scrollHeight - sandbox.chat.clientHeight) * (i / 173);
    H.updateJumpButton();
  }
  assert.strictEqual(sandbox.state.lastSeenId, 1, 'no frame may mark caught up');
  assert.strictEqual(dividerPresent, true);
});

check('a moving user scroll keeps its attribution past the window', () => {
  sandbox.state = mkState({ userIntentAt: Date.now() - 1400 });
  for (let i = 0; i < 40; i++) H.sustainIntent();   // frames of a long fling
  assert.strictEqual(H.scrollIsUsers(), true, 'momentum must not lose attribution');
});

check('a programmatic scroll can never bootstrap attribution', () => {
  sandbox.state = mkState({ userIntentAt: 0 });
  for (let i = 0; i < 200; i++) H.sustainIntent();
  assert.strictEqual(H.scrollIsUsers(), false, 'stale must stay stale');
});

check('appendMessage-style advance uses the walk, not a bare max', () => {
  const m = new Map();
  m.set(3573, node()); m.set(3578, node('filtered-out')); m.set(3579, node());
  sandbox.state = mkState({ lastSeenId: 3573, messageDomById: m });
  H.markCaughtUp();
  assert.strictEqual(sandbox.state.lastSeenId, 3573,
    'a later visible message must not leapfrog an earlier hidden one');
});

check('a warm gesture cannot donate attribution to a page-issued scroll', () => {
  // scroll up to notice the bar, then click it — the primary interaction
  sandbox.state = mkState({ userIntentAt: Date.now() - 300 });
  assert.strictEqual(H.scrollIsUsers(), true, 'gesture is warm before the click');
  H.disownScroll();                                  // what the click handler does
  for (let i = 0; i < 180; i++) H.sustainIntent();   // frames of the glide
  assert.strictEqual(H.scrollIsUsers(), false,
    'the page-issued glide must not inherit the wheel that preceded it');
});

check('disowning does not block the next real gesture', () => {
  sandbox.state = mkState({ userIntentAt: Date.now() - 300 });
  H.disownScroll();
  H.noteIntent();                                    // user touches the scroller again
  assert.strictEqual(H.scrollIsUsers(), true);
});

check('the new-bar makes no claim while the user is at the bottom', () => {
  const m = new Map(); m.set(1, node()); m.set(2, node());
  sandbox.state = mkState({ lastSeenId: 1, messageDomById: m });
  armDivider();
  let shown = null;
  sandbox.newBar.classList = { add: () => { shown = true; }, remove: () => { shown = false; } };
  sandbox.chat.scrollTop = sandbox.chat.scrollHeight - sandbox.chat.clientHeight;
  H.updateNewBar();
  assert.strictEqual(shown, false, 'no "messages below" claim when already at the bottom');
  assert.strictEqual(sandbox.state.lastSeenId, 1, 'and nothing marked read to achieve it');
});

check('the new-bar still reports unread when scrolled up', () => {
  const m = new Map(); m.set(1, node()); m.set(2, node());
  sandbox.state = mkState({ lastSeenId: 1, messageDomById: m });
  armDivider();
  let shown = null;
  sandbox.newBar.classList = { add: () => { shown = true; }, remove: () => { shown = false; } };
  sandbox.chat.scrollTop = 0;
  H.updateNewBar();
  assert.strictEqual(shown, true);
});

console.log('');
console.log((failures.length ? 'FAILED' : 'OK') + ` — ${passed} passed, ${failures.length} failure(s)`);
process.exit(failures.length ? 1 : 0);
