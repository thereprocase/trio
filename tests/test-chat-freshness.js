'use strict';

// Bottom-of-chat freshness strip: the published snapshot, the recovery
// lifecycle, and the single-live-region rule.
//
// Two kinds of assertion live here on purpose. document.getElementById in the
// DOM harness AUTO-CREATES a div on miss and never returns null, so a purely
// behavioural test would pass identically against an index.html containing no
// strip at all. The markup contract is therefore asserted against the FILE,
// and only the behaviour is asserted through the harness.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { load } = require('./dom-harness');
const cx = load();
const H = cx.hooks;
let failures = 0;
function check(name, fn) { try { fn(); console.log('PASS: ' + name); } catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); } }

const INDEX = fs.readFileSync(path.join(__dirname, '..', 'server', 'web', 'index.html'), 'utf8');

/* ---------- markup contract (asserted against the file, not the harness) ---------- */

check('markup: the strip exists in index.html', () => {
  assert.ok(/id="chat-freshness"/.test(INDEX), 'no #chat-freshness element in index.html');
});
check('markup: the strip sits between the message list and the composer', () => {
  const messages = INDEX.indexOf('id="messages"');
  const strip = INDEX.indexOf('id="chat-freshness"');
  const composer = INDEX.indexOf('composer-shell');
  assert.ok(messages > -1 && strip > -1 && composer > -1, 'anchors missing');
  assert.ok(strip > messages, 'strip must come after the message list, not inside/above it');
  assert.ok(strip < composer, 'strip must come before the composer');
});
check('markup: the strip starts hidden', () => {
  const tag = INDEX.slice(INDEX.indexOf('id="chat-freshness"'));
  assert.ok(/^[^>]*\shidden/.test(tag), 'strip must ship hidden, not visible-then-cleared');
});
check('markup: the strip is a polite live region', () => {
  const tag = INDEX.slice(INDEX.indexOf('id="chat-freshness"'), INDEX.indexOf('>', INDEX.indexOf('id="chat-freshness"')));
  assert.ok(/aria-live="polite"/.test(tag) || /role="status"/.test(tag), 'strip must announce politely');
});
check('markup: the connection pill is no longer a competing live region', () => {
  // Two live regions describing the same transition announce it twice. The
  // strip owns the announcement; the pill stays a visual indicator.
  const pill = INDEX.slice(INDEX.indexOf('id="h-conn"'), INDEX.indexOf('>', INDEX.indexOf('id="h-conn"')));
  assert.ok(!/aria-live/.test(pill), '#h-conn must not carry aria-live');
});

/* ---------- published snapshot ---------- */

function reset() {
  H.Trio.stopEvents?.(); H.Trio.stopWorkspaceEvents?.();
  cx.window.EventSource.instances.length = 0;
  H.Trio.state.channel = 'a-channel';
  H.Trio.state.dmKey = '';
}

check('snapshot: the store carries the CURRENT state, not the previous one', () => {
  // The regression this feature is built on: setConnection() published the
  // module `state` its own caller was about to overwrite, so consumers saw
  // every transition one late and never saw 'live' at all.
  reset();
  H.Trio.startWorkspaceEvents();
  const ws = cx.window.EventSource.instances[0];
  ws.fireOpen();
  assert.strictEqual(H.Trio.store.get('connection').state, 'workspace:connected',
    'open should publish connected, not the prior connecting');
  ws.fireHeartbeat();
  assert.strictEqual(H.Trio.store.get('connection').state, 'workspace:live',
    "'live' must actually reach the store");
  H.Trio.stopWorkspaceEvents();
});
check('snapshot: the store and the snapshot accessor agree', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  assert.deepStrictEqual(H.Trio.store.get('connection'), H.Trio.freshnessSnapshot());
  H.Trio.stopWorkspaceEvents();
});

/* ---------- recovery lifecycle ---------- */

check('recovery: a clean first connect is not an outage', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  const ws = cx.window.EventSource.instances[0];
  assert.strictEqual(H.Trio.freshnessSnapshot().recovering, false, 'connecting is not recovering');
  ws.fireOpen();
  assert.strictEqual(H.Trio.freshnessSnapshot().recovering, false, 'open is not recovering');
  ws.fireHeartbeat();
  assert.strictEqual(H.Trio.freshnessSnapshot().recovering, false);
  assert.strictEqual(H.Trio.freshnessSnapshot().staleSince, null);
  H.Trio.stopWorkspaceEvents();
});
check('recovery: losing proof starts an outage and stamps staleSince', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  const ws = cx.window.EventSource.instances[0];
  ws.fireOpen(); ws.fireHeartbeat();
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  const snap = H.Trio.freshnessSnapshot();
  assert.strictEqual(snap.recovering, true);
  assert.ok(typeof snap.staleSince === 'number' && snap.staleSince > 0, 'staleSince must be stamped');
  H.Trio.stopWorkspaceEvents();
});
check('recovery: a replacement stream OPENING does not end the outage', () => {
  // The whole point. An open socket proves a connection exists, never that
  // data is flowing — clearing here would flash "recovered" at a user whose
  // feed is still silent.
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  cx.window.EventSource.instances[0].fireHeartbeat();
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  const replacement = cx.window.EventSource.instances[cx.window.EventSource.instances.length - 1];
  replacement.fireOpen();
  assert.strictEqual(H.Trio.freshnessSnapshot().recovering, true,
    'onopen must NOT clear the outage');
  H.Trio.stopWorkspaceEvents();
});
check('recovery: only receipt proof ends the outage', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  cx.window.EventSource.instances[0].fireHeartbeat();
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  const replacement = cx.window.EventSource.instances[cx.window.EventSource.instances.length - 1];
  replacement.fireOpen();
  replacement.fireHeartbeat();
  const snap = H.Trio.freshnessSnapshot();
  assert.strictEqual(snap.recovering, false, 'a heartbeat is proof; it must clear');
  assert.strictEqual(snap.staleSince, null);
  H.Trio.stopWorkspaceEvents();
});
check('recovery: staleSince marks the START of the outage, not the latest retry', () => {
  // The clock is pinned and then advanced, because both stamps would otherwise
  // land in the same millisecond and this test would pass against code that
  // restamps on every retry — the exact bug it exists to catch.
  reset();
  const realNow = cx.window.Date.now;
  let now = 1_000_000;
  cx.window.Date.now = () => now;
  try {
    H.Trio.startWorkspaceEvents();
    cx.window.EventSource.instances[0].fireOpen();
    cx.window.EventSource.instances[0].fireHeartbeat();
    H.Trio.checkEventFreshness(now + 46_000);
    const first = H.Trio.freshnessSnapshot().staleSince;
    assert.strictEqual(first, 1_000_000, 'outage should be stamped at the moment proof was lost');
    now = 1_030_000;                     // 30s later, same outage still running
    const replacement = cx.window.EventSource.instances[cx.window.EventSource.instances.length - 1];
    replacement.fireError();
    assert.strictEqual(H.Trio.freshnessSnapshot().staleSince, first,
      'a second failure inside one outage must not restamp the clock');
  } finally { cx.window.Date.now = realNow; H.Trio.stopWorkspaceEvents(); }
});
check('recovery: an error with no prior liveness still reports an outage', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireError();
  assert.strictEqual(H.Trio.freshnessSnapshot().recovering, true,
    'a first connect that fails is still worth telling the user about');
  H.Trio.stopWorkspaceEvents();
});

/* ---------- the strip itself ---------- */

function strip() { return cx.document.getElementById('chat-freshness'); }

check('strip: shown while recovering on a conversation route', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  cx.window.EventSource.instances[0].fireHeartbeat();
  assert.strictEqual(strip().hidden, true, 'nothing to say while live');
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  assert.strictEqual(strip().hidden, false, 'an outage must be visible');
  assert.ok(strip().textContent.length > 0);
  H.Trio.stopWorkspaceEvents();
});
check('strip: hidden on workspace routes even mid-outage', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  cx.window.EventSource.instances[0].fireHeartbeat();
  H.Trio.state.channel = ''; H.Trio.state.dmKey = '';   // showView() clears both
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  assert.strictEqual(strip().hidden, true, 'the strip is conversation-only');
  assert.strictEqual(H.Trio.freshnessSnapshot().recovering, true,
    'the outage is still real, it is just not rendered here');
  H.Trio.stopWorkspaceEvents();
});
check('strip: a DM route counts as a conversation', () => {
  reset();
  H.Trio.state.channel = ''; H.Trio.state.dmKey = 'dm:someone';
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  cx.window.EventSource.instances[0].fireHeartbeat();
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  assert.strictEqual(strip().hidden, false);
  H.Trio.stopWorkspaceEvents();
});
check('strip: does not claim to be polling', () => {
  // The client is an EventSource. It reconnects; it never polls. Saying
  // otherwise would be a lie about the transport in the one UI element whose
  // entire job is telling the truth about the transport.
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  cx.window.EventSource.instances[0].fireHeartbeat();
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  assert.ok(!/poll/i.test(strip().textContent), 'copy must not claim polling: ' + strip().textContent);
  H.Trio.stopWorkspaceEvents();
});
check('strip: is cleared when the outage ends', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  cx.window.EventSource.instances[0].fireHeartbeat();
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  const replacement = cx.window.EventSource.instances[cx.window.EventSource.instances.length - 1];
  replacement.fireOpen(); replacement.fireHeartbeat();
  assert.strictEqual(strip().hidden, true);
  assert.strictEqual(strip().textContent, '');
  H.Trio.stopWorkspaceEvents();
});
check('strip: an unchanged message is not rewritten (no repeat announcements)', () => {
  reset();
  H.Trio.startWorkspaceEvents();
  cx.window.EventSource.instances[0].fireOpen();
  cx.window.EventSource.instances[0].fireHeartbeat();
  H.Trio.checkEventFreshness(Date.now() + 46_000);
  const el = strip();
  let value = el.textContent;
  let writes = 0;
  Object.defineProperty(el, 'textContent', {
    configurable: true,
    get() { return value; },
    set(v) { writes++; value = v; },
  });
  const replacement = cx.window.EventSource.instances[cx.window.EventSource.instances.length - 1];
  replacement.fireError();
  replacement.fireError();
  assert.strictEqual(writes, 0, 'still-recovering must not re-announce the same sentence');
  delete el.textContent;
  el.textContent = value;
  H.Trio.stopWorkspaceEvents();
});

/* ---------- single live region ---------- */

check('announce: the shared announcer stays quiet while the strip is speaking', () => {
  reset();
  const spoken = [];
  const original = H.Trio.ui.setLive;
  H.Trio.ui.setLive = msg => spoken.push(msg);
  try {
    H.Trio.startWorkspaceEvents();
    cx.window.EventSource.instances[0].fireOpen();
    cx.window.EventSource.instances[0].fireHeartbeat();
    spoken.length = 0;
    H.Trio.checkEventFreshness(Date.now() + 46_000);
    assert.strictEqual(strip().hidden, false, 'precondition: the strip is showing');
    assert.deepStrictEqual(spoken, [], 'the strip announces; the announcer must not duplicate it');
  } finally { H.Trio.ui.setLive = original; H.Trio.stopWorkspaceEvents(); }
});
check('announce: the shared announcer still speaks where the strip is not rendered', () => {
  reset();
  H.Trio.state.channel = ''; H.Trio.state.dmKey = '';   // workspace route: no strip
  const spoken = [];
  const original = H.Trio.ui.setLive;
  H.Trio.ui.setLive = msg => spoken.push(msg);
  try {
    H.Trio.startWorkspaceEvents();
    cx.window.EventSource.instances[0].fireOpen();
    cx.window.EventSource.instances[0].fireHeartbeat();
    spoken.length = 0;
    H.Trio.checkEventFreshness(Date.now() + 46_000);
    assert.strictEqual(strip().hidden, true, 'precondition: the strip is not showing');
    assert.ok(spoken.length > 0, 'losing the pill live region must not make outages silent here');
  } finally { H.Trio.ui.setLive = original; H.Trio.stopWorkspaceEvents(); }
});

console.log(failures ? `\n${failures} failure(s)` : '\nOK — 0 failure(s)');
process.exit(failures ? 1 : 0);
