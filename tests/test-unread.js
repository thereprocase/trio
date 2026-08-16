// The unread divider — "New since your last visit" — placed by
// Trio.conversation.render().
//
// This file used to test twelve functions of the single-pane client
// (isHiddenMsg, firstVisibleUnreadDom, updateNewBar, …). None of them survive
// in the workspace client, which computes the divider inline in render()
// instead, so the old assertions could only have been kept red or deleted.
// They are replaced here rather than dropped: the rules are the same ones, and
// each of them is a way the divider has actually been wrong before.
//
// The subtle one is the interaction with age-based collapse. render() may hide
// messages older than the history threshold behind an "older messages" toggle,
// and the divider index is computed over the FULL list but used to index the
// RENDERED slice. Those two are different arrays whenever anything is hidden,
// and an unmapped index puts the divider at an arbitrary point in the
// conversation — or past the end, where it silently vanishes.
//
// Usage: node tests/test-unread.js
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
const Trio = H.Trio;
const document = cx.document;   // the sandbox's DOM, not a Node global
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

const DAY = 86400000;

// Build `count` messages, the newest `recent` of them inside the age cutoff so
// the rest collapse behind the "older messages" toggle.
function seed({ count, recent = count, lastSeenId }) {
  // 11-conversation binds the ROOT Trio.state (const { state } = Trio), not
  // the store's `conversation` slice — seeding the slice renders an empty
  // conversation and every assertion below passes vacuously.
  const state = Trio.state;
  state.messages = new Map();
  for (let i = 1; i <= count; i++) {
    const age = i > count - recent ? 0 : 30 * DAY;
    state.messages.set(i, {
      id: i, member_id: 'a', member_name: 'Ada', content: 'm' + i,
      created_at: new Date(Date.now() - age).toISOString(),
      mentions: [], refs: [], bangs: [], recipients: [],
    });
  }
  state.lastSeenId = lastSeenId;
  state.messageDomById = new Map();
  state.olderExpanded = {};
  Trio.state.channel = 'test';
  // Age-based collapse is off unless messageHistoryDays is a positive number,
  // and under the harness preferences.read() supplies no value — so a fixture
  // that merely backdates messages collapses NOTHING and the two tests below
  // pass while exercising the uncollapsed path. Set it explicitly.
  Trio.state.preferences = Object.assign({}, Trio.state.preferences,
    { messageHistoryDays: recent === count ? 0 : 3 });
}

// Guard the fixture itself: if collapse silently stops happening again, these
// tests must fail rather than quietly re-test the ordinary path.
function assertCollapsed(expectedVisible) {
  const list = document.getElementById('messages');
  const cards = new Set(Trio.state.messageDomById.values());
  const visible = list.children.filter(el => cards.has(el)).length;
  assert.strictEqual(visible, expectedVisible,
    `fixture did not collapse: ${visible} cards rendered, expected ` +
    `${expectedVisible} — the age cutoff is not in effect, so this test is ` +
    'not exercising the collapsed path at all');
}

function renderAndFind() {
  Trio.conversation.render();
  const list = document.getElementById('messages');
  const kids = list ? list.children : [];
  return kids.findIndex(el => el.className === 'unread-divider');
}

// Index of the divider among the CARDS only, which is what a reader sees: the
// list also holds day separators and the "older messages" toggle. Cards are
// identified by the DOM map render() populates, not by guessing at class
// names — an earlier version counted the older-messages toggle as a card and
// reported the divider one position late.
function dividerBeforeMessage() {
  Trio.conversation.render();
  const list = document.getElementById('messages');
  const cards = new Set(Trio.state.messageDomById.values());
  let seenCards = 0;
  for (const el of list.children) {
    if (el.className === 'unread-divider') return seenCards;
    if (cards.has(el)) seenCards++;
  }
  return -1;
}

check('no divider on a first-ever visit (lastSeenId 0), however much history', () => {
  seed({ count: 5, lastSeenId: 0 });
  assert.strictEqual(renderAndFind(), -1,
    'a first visit marked the entire history as unread');
});

check('no divider when everything has been seen', () => {
  seed({ count: 5, lastSeenId: 5 });
  assert.strictEqual(renderAndFind(), -1,
    'a fully-read conversation still showed "New since your last visit"');
});

check('a divider appears when there is anything unread', () => {
  seed({ count: 5, lastSeenId: 3 });
  assert.notStrictEqual(renderAndFind(), -1, 'unread messages produced no divider');
});

check('the divider sits before the first UNREAD message, not at the top', () => {
  seed({ count: 5, lastSeenId: 3 });
  assert.strictEqual(dividerBeforeMessage(), 3,
    'the divider was not placed before message 4 — the first one past lastSeenId');
});

check('a lastSeenId ahead of every message shows no divider', () => {
  seed({ count: 5, lastSeenId: 99 });
  assert.strictEqual(renderAndFind(), -1,
    'a watermark past the newest message still reported unread');
});

// ── the collapse interaction ────────────────────────────────────────────────
check('with old messages collapsed, the divider still renders', () => {
  // 8 messages, only the newest 3 inside the cutoff: 5 collapse away. The
  // unread boundary (id 6) is INSIDE the visible slice.
  seed({ count: 8, recent: 3, lastSeenId: 5 });
  const at = renderAndFind();
  assertCollapsed(3);
  assert.notStrictEqual(at, -1,
    'collapsing old messages lost the divider entirely — the index was ' +
    'computed over the full list but used against the rendered slice');
});

check('an unread boundary inside the collapsed range pins the divider to the top', () => {
  // Unread starts at id 2, which is hidden. There is nowhere earlier to put
  // the divider in the visible slice, so it belongs at position 0 — not at
  // index 1, which would point at the wrong message.
  seed({ count: 8, recent: 3, lastSeenId: 1 });
  const pos = dividerBeforeMessage();
  assertCollapsed(3);
  assert.strictEqual(pos, 0,
    'the divider was placed by an index into the full list rather than the ' +
    'rendered slice');
});

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
process.exit(failures.length ? 1 : 0);
