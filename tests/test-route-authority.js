// Who loads a channel — the router, or the caller?
//
// Both did. `openChannel()` called `router.navigate()` and then
// `loadConversation()`; `03-router.js`'s `apply()` invokes every route handler
// SYNCHRONOUSLY, so `onRoute()` had already loaded by the time navigate()
// returned. Every channel click therefore cleared the message map twice,
// re-seeded the unread watermark twice, and closed the EventSource that the
// first load had opened a moment earlier — which is a plausible contributor to
// "the chat UI doesn't stay live".
//
// The obvious repair is wrong, and that is the point of this file. Simply
// deleting openChannel's direct call left `onRoute()` in charge, and onRoute
// had a SAME-CODE branch that skipped loadConversation and hand-rolled a subset
// of it: it dropped the DM's identity but kept the DM's message map and never
// restarted channel events. That branch is reached by the real case of leaving
// a DM whose backing transport IS the channel you land on — so the "cleanup"
// would have produced a channel showing a DM's history over a stream still
// scoped to the DM. Neither participant was complete; that was the actual
// defect, not the duplication.
//
// So: the router is the single authority, and onRoute always performs one full
// load — including same-code.
//
// Usage: node tests/test-route-authority.js
'use strict';

const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log('PASS: ' + name); }
  else { failures.push(name); console.log('FAIL: ' + name); }
}

const cx = load();
const Trio = cx.hooks.Trio;
const state = Trio.state;
cx.context.history = { pushState() {}, replaceState() {} };

// Count stream restarts. startEvents() is what tears down and recreates the
// EventSource, so it is the honest proxy for "how many times did we churn the
// live connection".
let starts = [];
Trio.startEvents = channel => { starts.push(channel); };
Trio.stopEvents = () => {};

// The workspace registers onRoute with the router on mount; the router then
// drives it. Mount both so the real wiring is exercised rather than simulated.
Trio.workspace.mount();
Trio.router.mount();

const reset = () => { starts = []; };

// ── one load per navigation ─────────────────────────────────────────────────
reset();
state.channel = '';
Trio.workspace.openChannel('alpha');
check('opening a channel restarts the stream exactly once',
      starts.length === 1);
check('...for the channel that was asked for',
      starts[0] === 'alpha');
check('and the channel is actually open afterwards',
      state.channel === 'alpha');

reset();
Trio.workspace.openChannel('beta');
check('switching to a different channel also restarts exactly once',
      starts.length === 1 && starts[0] === 'beta');

// ── the same-code case is a FULL load, not a partial one ────────────────────
// Leaving a DM for the channel that carries it. The old partial branch left
// these behind; the assertions are written against the leak, not the mechanism,
// so they stay meaningful if the implementation changes again.
reset();
state.channel = 'beta';
state.dmKey = 'dm:someone';
state.dmTargetId = 'agent-1';
state.dmMemberIds = ['agent-1'];
state.messages = new Map([[1, { id: 1, content: 'private' }]]);
Trio.workspace.openChannel('beta');
check('re-entering the same channel still restarts the stream once',
      starts.length === 1 && starts[0] === 'beta');
check('the DM message map does not survive into the channel',
      state.messages.size === 0);
check('the DM target identity is cleared, so a post cannot be rescoped private',
      state.dmTargetId === '' && state.dmMemberIds.length === 0);
check('the DM key is cleared', state.dmKey === '');

// ── the archived flag survives the handoff ──────────────────────────────────
reset();
state.channel = '';
Trio.workspace.openChannel('gamma', 'archived');
check('an archived channel opens read-only through the router',
      state.readOnly === true);
check('and still restarts the stream once', starts.length === 1);

reset();
state.channel = '';
Trio.workspace.openChannel('gamma');
check('a live channel is not read-only', state.readOnly === false);

// ── popstate / direct link go through the same one path ────────────────────
// The route handler is what a Back button and a pasted URL both reach, so it
// must load on its own without openChannel having been called at all.
reset();
state.channel = '';
Trio.router.navigate('channel', { code: 'delta' });
check('a route change with no openChannel call still loads the channel',
      state.channel === 'delta' && starts.length === 1);

// ── direct link / page boot opens exactly one stream ────────────────────────
// The click path and the boot path are different owners, and fixing one moved
// the bug into the other: with onRoute always loading, `?channel=x` opened the
// stream during router mount and then `06-core.js` opened it AGAIN after
// mountFeatures() returned, closing the first. Same tear-down/reopen, now on
// every direct channel link instead of every click. Core therefore asks the
// router what it already did.
// This drives the REAL Trio.boot(), not a copy of its logic. An earlier draft
// re-implemented core's decision inside the test, which would have passed just
// as happily with the fix reverted — the same blind spot that let the drawer's
// pre-boot ordering defect through.
Trio.router.unmount();
Trio.workspace.unmount();

// The mountFeatures callback boot() receives, matching 90-boot.js's order.
const mountFeatures = () => { Trio.workspace.mount(); Trio.router.mount(); };

async function bootWith(search, pathname, channel) {
  cx.context.location.search = search;
  cx.context.location.pathname = pathname;
  state.channel = channel;
  state.dmKey = '';
  // /api/meta echoes back the channel the URL asked for.
  Trio.api.get = async () => ({ operator: { id: 'op', name: 'op', source: 'loopback' }, channel });
  reset();
  await Trio.boot(mountFeatures);
  Trio.router.unmount();
  Trio.workspace.unmount();
}

(async () => {
  await bootWith('?channel=epsilon', '/', 'epsilon');
  check('a direct channel link opens exactly one stream through a real boot',
        starts.length === 1);
  check('...and it is the linked channel', starts[0] === 'epsilon');

  // The no-channel boot must still reach startEvents, or the connection pill
  // never leaves "connecting" on the Home view.
  await bootWith('', '/', '');
  check('booting with no channel still calls startEvents, so the pill resolves',
        starts.length === 1 && !starts[0]);

  // 90-boot isolates per-feature mount failures on purpose, so the router can
  // apply a channel route while the workspace module is not mounted at all.
  // Boot must notice nothing was opened and open it — reading the route name
  // instead would say "already handled" and leave the page with no stream,
  // which is a worse failure than the double-open this whole change removes.
  cx.context.location.search = '?channel=theta';
  cx.context.location.pathname = '/';
  state.channel = 'theta';
  Trio.api.get = async () => ({ operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'theta' });
  reset();
  await Trio.boot(() => {
    try { throw new Error('workspace mount failed'); } catch { /* as 90-boot does */ }
    Trio.router.mount();
  });
  check('a failed workspace mount still leaves boot opening the stream',
        starts.length === 1 && starts[0] === 'theta');
  Trio.router.unmount();

  finish();
})();

// ── the duplicate cannot come back ──────────────────────────────────────────
// Pinned by source, because the runtime count above would also pass if someone
// re-added the direct call inside a `state.channel !== code` guard — which is
// exactly the shape that was there before.
const src = require('fs').readFileSync(
  require('path').join(__dirname, '..', 'server', 'web', 'js', '20-workspace.js'), 'utf8');
const openChannelBody = (src.match(/function openChannel\([\s\S]*?\n  \}/) || [''])[0];
check('openChannel navigates rather than loading, when a router exists',
      /Trio\.router\?\.navigate/.test(openChannelBody)
      && /else loadConversation/.test(openChannelBody));
check('openChannel has no unconditional loadConversation call',
      !/^\s*loadConversation\(/m.test(openChannelBody));

function finish() {
  console.log();
  if (failures.length) {
    console.log(`FAILED — ${failures.length} of ${failures.length + passed}`);
    failures.forEach(f => console.log('  - ' + f));
    process.exit(1);
  }
  console.log(`OK — ${passed} passed`);
}
