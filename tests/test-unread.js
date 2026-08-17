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
// The watermark is per conversation and frozen at entry: `dividerBaseByConv`
// records where the operator had read when they opened it, while
// `lastSeenByConv` advances as they read. One global scalar used to serve both,
// so switching channels carried a foreign watermark and the entry burst — which
// calls markRead() on every message while the view sits at the bottom — erased
// the divider before it could be drawn.
//
// The divider used to be indexed over the FULL message list while being applied
// to a RENDERED slice, because render() collapsed messages older than a
// threshold. That collapse is gone — the preference it belonged to now hides
// stale THREADS in the sidebar instead (test-stale-threads.js) — so the two
// arrays are one array again and the remapping it needed went with it.
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

// Build `count` messages, all of them rendered — the conversation no longer
// collapses by age (that preference now hides whole stale THREADS in the
// sidebar; see test-stale-threads.js).
function seed({ count, lastSeenId, channel = 'test' }) {
  // 11-conversation binds the ROOT Trio.state (const { state } = Trio), not
  // the store's `conversation` slice — seeding the slice renders an empty
  // conversation and every assertion below passes vacuously.
  const state = Trio.state;
  state.messages = new Map();
  for (let i = 1; i <= count; i++) {
    state.messages.set(i, {
      id: i, member_id: 'a', member_name: 'Ada', content: 'm' + i,
      created_at: new Date().toISOString(),
      mentions: [], refs: [], bangs: [], recipients: [],
    });
  }
  state.messageDomById = new Map();
  Trio.state.channel = channel;
  // The watermark is per conversation and FROZEN at entry, so each fixture
  // clears both maps and re-seeds — mirroring what loadConversation does
  // before a conversation's history arrives.
  state.lastSeenByConv = {};
  state.dividerBaseByConv = {};
  Trio.conversation.seedWatermark(lastSeenId);
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

// ── the two failures the global scalar caused ──────────────────────────────
check('switching conversations does not carry the previous watermark', () => {
  // A busy channel read up to id 900, then a quiet one whose newest is 5.
  // Under one global scalar, findIndex(id > 900) returned -1 here and the
  // divider could never appear in the second conversation again.
  Trio.state.channel = 'busy';
  Trio.state.lastSeenByConv = {}; Trio.state.dividerBaseByConv = {};
  Trio.conversation.seedWatermark(900);

  Trio.state.channel = 'quiet';
  seed({ count: 5, lastSeenId: 2 });
  assert.strictEqual(dividerBeforeMessage(), 2,
    'the divider was positioned by the other conversation\'s watermark');
});

check('each conversation keeps its own watermark', () => {
  Trio.state.lastSeenByConv = {}; Trio.state.dividerBaseByConv = {};
  Trio.state.channel = 'a';
  Trio.conversation.seedWatermark(10);
  Trio.state.channel = 'b';
  Trio.conversation.seedWatermark(20);
  assert.strictEqual(Trio.state.dividerBaseByConv['a'], 10);
  assert.strictEqual(Trio.state.dividerBaseByConv['b'], 20);
  // Both watermarks are per conversation, not just the divider one — a global
  // READ watermark would flush the wrong conversation's ids to the server.
  assert.strictEqual(Trio.state.lastSeenByConv['a'], 10);
  assert.strictEqual(Trio.state.lastSeenByConv['b'], 20);
});

check('reading one conversation does not advance another\'s read watermark', () => {
  // Exercises the ACCESSORS, not just the map: asserting the stored values
  // alone still passed with seenId()/setSeenId() reading a single global,
  // because seedWatermark writes the map either way.
  // seed() clears both maps by design, so the second conversation is set up
  // by hand rather than through it — otherwise this fixture erases the very
  // value it is checking.
  seed({ count: 3, lastSeenId: 1, channel: 'right' });
  Trio.state.lastSeenByConv['left'] = 1;
  Trio.state.dividerBaseByConv['left'] = 1;

  // Read to the bottom of 'right'.
  Trio.conversation.upsert({
    id: 99, member_id: 'a', member_name: 'Ada', content: 'newest',
    created_at: new Date().toISOString(),
    mentions: [], refs: [], bangs: [], recipients: [],
  });
  assert.strictEqual(Trio.state.lastSeenByConv['right'], 99,
    "the read conversation's watermark should advance to the newest message");
  assert.strictEqual(Trio.state.lastSeenByConv['left'], 1,
    "reading one conversation moved another's watermark — the accessors are "
    + 'not per conversation');
});

check('re-entering a conversation re-freezes the divider at what you have now read', () => {
  Trio.state.lastSeenByConv = {}; Trio.state.dividerBaseByConv = {};
  Trio.state.channel = 'again';
  Trio.conversation.seedWatermark(3);            // first visit: read to 3
  assert.strictEqual(Trio.state.dividerBaseByConv['again'], 3);
  Trio.conversation.seedWatermark(9);            // came back, now read to 9
  assert.strictEqual(Trio.state.dividerBaseByConv['again'], 9,
    'the divider stayed at the first visit, so a return visit would show '
    + 'messages already read as new');
});

check('seeding never rewinds a read watermark that already advanced locally', () => {
  Trio.state.lastSeenByConv = {}; Trio.state.dividerBaseByConv = {};
  Trio.state.channel = 'ahead';
  Trio.conversation.seedWatermark(50);
  Trio.state.lastSeenByConv['ahead'] = 80;       // read further since
  Trio.conversation.seedWatermark(50);           // stale server value returns
  assert.strictEqual(Trio.state.lastSeenByConv['ahead'], 80,
    'a stale server watermark rewound local progress, which would re-send '
    + 'reads the server already has');
});

check('reading the conversation does not erase the divider you opened it to see', () => {
  seed({ count: 6, lastSeenId: 3 });
  const at = dividerBeforeMessage();
  assert.strictEqual(at, 3, 'divider should start before message 4');
  // markRead() runs on every upsert while the view is at the bottom — the
  // entry burst does exactly this. The READ watermark must advance while the
  // DIVIDER stays put.
  Trio.conversation.upsert({
    id: 7, member_id: 'a', member_name: 'Ada', content: 'new',
    created_at: new Date().toISOString(),
    mentions: [], refs: [], bangs: [], recipients: [],
  });
  assert.ok(Trio.state.lastSeenByConv['test'] >= 3,
    'the read watermark should advance as messages arrive at the bottom');
  assert.strictEqual(dividerBeforeMessage(), 3,
    'the divider moved when the conversation was read — it must stay where '
    + 'it was when the conversation was opened');
});

check('an unseeded conversation draws no divider rather than marking all unread', () => {
  Trio.state.lastSeenByConv = {}; Trio.state.dividerBaseByConv = {};
  Trio.state.channel = 'fresh';
  seed({ count: 4, lastSeenId: 0 });
  assert.strictEqual(renderAndFind(), -1,
    'with no server watermark the whole history was marked new');
});

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
process.exit(failures.length ? 1 : 0);
