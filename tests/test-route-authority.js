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
let pushedUrls = [];
cx.context.history = {
  pushState(_route, _title, url) { pushedUrls.push(url); },
  replaceState(_route, _title, url) { pushedUrls.push(url); },
};

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

// Audit and archived-private URLs are distinct capabilities. They used to
// share archived=1, so serialize(parse(url)) could turn either one into the
// other on reload.
const parsedAudit = Trio.router.parse('?dm=agent-pair&audit=1', '/');
check('audit URLs parse as read-only audits',
      parsedAudit.name === 'audit' && parsedAudit.readOnly === true);
check('audit routes serialize with an explicit audit marker',
      Trio.router.serialize({ name: 'audit', params: { key: 'agent-pair' } }) === '/?dm=agent-pair&audit=1');
const parsedArchivedDm = Trio.router.parse('?dm=operator-dm&archived=1', '/');
check('archived-private URLs remain private DMs, not audits',
      parsedArchivedDm.name === 'dm' && parsedArchivedDm.params.archived === true
      && parsedArchivedDm.readOnly === true);
check('archived-private routes round-trip without acquiring audit identity',
      Trio.router.serialize(parsedArchivedDm) === '/?dm=operator-dm&archived=1');

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

  // DM and audit routes cannot know their physical transport until the thread
  // metadata arrives. They must claim feed selection before that await, then
  // open only the resolved channel — never the default channel in between.
  async function bootDm(search, thread, listName) {
    cx.context.location.search = search;
    cx.context.location.pathname = '/';
    state.channel = 'default-room';
    state.dmKey = '';
    state.dmAudit = false;
    let historyCalls = 0;
    Trio.api.get = async path => {
      if (path === '/api/meta') return { operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'default-room' };
      if (path.startsWith('/api/dms?with=')) {
        historyCalls++;
        if (historyCalls === 1) return { your_dms: listName === 'your_dms' ? [thread] : [], agent_dms: listName === 'agent_dms' ? [thread] : [] };
        return { messages: [] };
      }
      return {};
    };
    reset();
    await Trio.boot(mountFeatures);
    await Promise.resolve(); await Promise.resolve();
    Trio.router.unmount(); Trio.workspace.unmount();
  }

  await bootDm('?dm=dm-direct',
    { key: 'dm-direct', name: 'Direct', channel: 'private-inbox', member_ids: ['agent-a'] },
    'your_dms');
  check('a direct DM never opens the default channel during metadata lookup',
        !starts.includes('default-room'));
  check('a direct DM opens exactly one resolved transport feed',
        starts.length === 1 && starts[0] === 'private-inbox');
  check('a direct DM resolves as writable, not audit',
        state.dmKey === 'dm-direct' && state.readOnly === false && state.dmAudit === false);

  await bootDm('?dm=audit-direct&audit=1',
    { key: 'audit-direct', name: 'Audit', channel: 'agent-inbox', member_ids: ['agent-a', 'agent-b'] },
    'agent_dms');
  check('a direct audit never opens the default channel during metadata lookup',
        !starts.includes('default-room'));
  check('a direct audit opens exactly one resolved transport feed',
        starts.length === 1 && starts[0] === 'agent-inbox');
  check('a direct audit is read-only and retains audit identity',
        state.dmKey === 'audit-direct' && state.readOnly === true && state.dmAudit === true);

  await bootDm('?dm=operator-dm&audit=1',
    { key: 'operator-dm', name: 'Operator DM', channel: 'private-inbox', member_ids: ['agent-a'] },
    'your_dms');
  check('an explicit audit cannot degrade into a writable operator DM',
        starts.length === 0 && state.readOnly === true && state.dmAudit === true
        && state.dmError === 'Audit conversation not found');

  // The pre-marker client emitted bare ?dm= links for audits. Metadata still
  // identifies that thread safely, then navigation canonicalizes the URL.
  pushedUrls = [];
  await bootDm('?dm=legacy-audit',
    { key: 'legacy-audit', name: 'Legacy audit', channel: 'agent-inbox', member_ids: ['agent-a', 'agent-b'] },
    'agent_dms');
  check('a bare legacy audit resolves read-only on its one exact feed',
        starts.length === 1 && starts[0] === 'agent-inbox'
        && state.readOnly === true && state.dmAudit === true
        && state.dmLoading === false);
  check('a bare legacy audit canonicalizes to the explicit audit marker',
        pushedUrls.includes('/?dm=legacy-audit&audit=1'));

  const archivedThread = { key: 'archived-dm', name: 'Archived', channel: 'private-inbox', member_ids: ['agent-a'] };
  cx.context.location.search = '?dm=archived-dm&archived=1';
  state.channel = 'default-room'; state.dmKey = ''; state.dmAudit = false;
  Trio.api.get = async path => {
    if (path === '/api/meta') return { operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'default-room' };
    if (path.startsWith('/api/dms?archived=1&with=')) return { your_dms: [archivedThread], agent_dms: [] };
    if (path.startsWith('/api/dms?with=') && path.includes('&archived=1')) return { messages: [] };
    return {};
  };
  reset();
  await Trio.boot(mountFeatures);
  await Promise.resolve(); await Promise.resolve();
  check('an archived private DM opens one exact feed without becoming audit',
        starts.length === 1 && starts[0] === 'private-inbox'
        && state.readOnly === true && state.dmAudit === false);
  Trio.router.unmount(); Trio.workspace.unmount();

  // Hold metadata unresolved and try the exact privacy failure: type into a UI
  // labelled private and press Send while the previous public channel is still
  // the only channel the client knows. No POST is permitted, and stale public
  // routing/composer state must already be gone before the promise resolves.
  let resolveDeferred;
  const deferredLookup = new Promise(resolve => { resolveDeferred = resolve; });
  let posts = 0;
  const deferredThread = { key: 'deferred-dm', name: 'Deferred', channel: 'private-inbox', member_ids: ['agent-a'] };
  cx.context.location.search = '?dm=deferred-dm';
  state.channel = 'public-room'; state.dmKey = ''; state.dmAudit = false;
  state.members = new Map([['stale-agent', { id: 'stale-agent', name: 'Stale' }]]);
  state.drafts['public-room'] = 'public draft';
  Trio.api.get = async path => {
    if (path === '/api/meta') return { operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'public-room' };
    if (path.startsWith('/api/dms?with=deferred-dm')) return deferredLookup;
    return {};
  };
  Trio.api.post = async () => { posts++; return { ok: true }; };
  reset();
  await Trio.boot(mountFeatures);
  const input = cx.document.getElementById('input');
  check('an unresolved DM clears the stale public transport and roster',
        state.channel === '' && state.members.size === 0);
  check('an unresolved DM clears the visible public draft and locks editing',
        input.textContent !== 'public draft' && input.contentEditable === 'false');
  input.textContent = 'private secret';
  const sentWhileResolving = await Trio.composer.send();
  check('Send during unresolved DM lookup performs no public POST',
        sentWhileResolving === false && posts === 0);
  resolveDeferred({ your_dms: [deferredThread], agent_dms: [] });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  check('composer unlocks only after DM transport and recipients resolve',
        state.dmRouteResolved === true && state.dmMemberIds[0] === 'agent-a'
        && state.channel === 'private-inbox' && input.contentEditable === 'true');
  check('the resolved deferred DM opens one exact feed',
        starts.length === 1 && starts[0] === 'private-inbox');
  Trio.router.unmount(); Trio.workspace.unmount();

  state.dmKey = 'thin-dm'; state.dmRouteResolved = true;
  state.dmMemberIds = []; state.dmTargetId = ''; state.readOnly = false;
  input.textContent = 'must stay private'; posts = 0;
  const sentWithoutRecipients = await Trio.composer.send();
  let payloadRefused = false;
  try { Trio.composer.buildSendPayload(); } catch { payloadRefused = true; }
  check('a resolved-looking DM with no recipients still performs no POST',
        sentWithoutRecipients === false && posts === 0);
  check('the payload builder itself refuses a recipient-less declared DM',
        payloadRefused === true);

  let resolveAbandoned;
  const abandonedLookup = new Promise(resolve => { resolveAbandoned = resolve; });
  const abandonedThread = { key: 'slow-dm', name: 'Slow', channel: 'private-inbox', member_ids: ['agent-a'] };
  cx.context.location.search = '?dm=slow-dm';
  state.channel = 'public-room'; state.dmKey = ''; state.dmAudit = false;
  Trio.api.get = async path => {
    if (path === '/api/meta') return { operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'public-room' };
    if (path.startsWith('/api/dms?with=slow-dm')) return abandonedLookup;
    return {};
  };
  reset();
  await Trio.boot(mountFeatures);
  Trio.router.navigate('tasks');
  resolveAbandoned({ your_dms: [abandonedThread], agent_dms: [] });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  check('leaving an unresolved DM prevents its late lookup from reopening it',
        Trio.router.current()?.name === 'tasks' && state.view === 'tasks'
        && state.dmKey === '' && starts.length === 0);
  Trio.router.unmount(); Trio.workspace.unmount();

  let resolveForChannel;
  const channelRaceLookup = new Promise(resolve => { resolveForChannel = resolve; });
  cx.context.location.search = '?dm=slow-channel-race';
  state.channel = 'public-room'; state.dmKey = ''; state.dmAudit = false;
  Trio.api.get = async path => {
    if (path === '/api/meta') return { operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'public-room' };
    if (path.startsWith('/api/dms?with=slow-channel-race')) return channelRaceLookup;
    return {};
  };
  reset();
  await Trio.boot(mountFeatures);
  Trio.router.navigate('channel', { code: 'fresh-room' });
  resolveForChannel({ your_dms: [{ key: 'slow-channel-race', channel: 'private-inbox', member_ids: ['agent-a'] }], agent_dms: [] });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  check('a channel route invalidates a late DM lookup without feed takeover',
        Trio.router.current()?.name === 'channel' && state.channel === 'fresh-room'
        && state.dmKey === '' && starts.length === 1 && starts[0] === 'fresh-room');
  Trio.router.unmount(); Trio.workspace.unmount();

  let resolveForDm;
  const dmRaceLookup = new Promise(resolve => { resolveForDm = resolve; });
  const fastThread = { key: 'fast-dm', name: 'Fast', channel: 'fast-inbox', member_ids: ['agent-b'] };
  let fastCalls = 0;
  cx.context.location.search = '?dm=slow-dm-race';
  state.channel = 'public-room'; state.dmKey = ''; state.dmAudit = false;
  Trio.api.get = async path => {
    if (path === '/api/meta') return { operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'public-room' };
    if (path.startsWith('/api/dms?with=slow-dm-race')) return dmRaceLookup;
    if (path.startsWith('/api/dms?with=fast-dm')) {
      fastCalls++;
      return fastCalls === 1 ? { your_dms: [fastThread], agent_dms: [] } : { messages: [] };
    }
    return {};
  };
  reset();
  await Trio.boot(mountFeatures);
  Trio.router.navigate('dm', { key: 'fast-dm' });
  await Promise.resolve(); await Promise.resolve();
  resolveForDm({ your_dms: [{ key: 'slow-dm-race', channel: 'slow-inbox', member_ids: ['agent-a'] }], agent_dms: [] });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  check('a newer DM route invalidates an older lookup without feed takeover',
        Trio.router.current()?.name === 'dm' && state.dmKey === 'fast-dm'
        && starts.length === 1 && starts[0] === 'fast-inbox');
  Trio.router.unmount(); Trio.workspace.unmount();

  cx.context.location.search = '?dm=missing';
  state.channel = 'default-room'; state.dmKey = ''; state.dmAudit = false;
  Trio.api.get = async path => {
    if (path === '/api/meta') return { operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'default-room' };
    if (path.startsWith('/api/dms?with=')) throw new Error('lookup unavailable');
    return {};
  };
  reset();
  await Trio.boot(mountFeatures);
  await Promise.resolve(); await Promise.resolve();
  check('a failed DM lookup never falls through to the default channel feed',
        starts.length === 0);
  check('a failed DM lookup resolves its loading state with an inline error',
        state.dmLoading === false && state.dmError === 'lookup unavailable'
        && state.dmRouteResolved === false
        && cx.document.getElementById('input').contentEditable === 'false');
  check('a failed DM lookup stops claiming it is still resolving',
        cx.document.getElementById('input').dataset.placeholder === 'Private conversation unavailable.');
  Trio.router.unmount(); Trio.workspace.unmount();

  // Even on a DM URL, a workspace mount failure makes no ownership claim, so
  // core still supplies the only safe fallback it can know.
  cx.context.location.search = '?dm=unmounted';
  state.channel = 'fallback-room'; state.dmKey = '';
  Trio.api.get = async () => ({ operator: { id: 'op', name: 'op', source: 'loopback' }, channel: 'fallback-room' });
  reset();
  await Trio.boot(() => Trio.router.mount());
  check('a failed workspace mount on a DM URL still opens core fallback',
        starts.length === 1 && starts[0] === 'fallback-room');
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
const bootSrc = require('fs').readFileSync(
  require('path').join(__dirname, '..', 'server', 'web', 'js', '90-boot.js'), 'utf8');
check('boot has no second, manual DM bootstrap outside the router',
      !/workspace\?\.openDmByKey|workspace\.openDmByKey/.test(bootSrc));

function finish() {
  console.log();
  if (failures.length) {
    console.log(`FAILED — ${failures.length} of ${failures.length + passed}`);
    failures.forEach(f => console.log('  - ' + f));
    process.exit(1);
  }
  console.log(`OK — ${passed} passed`);
}
