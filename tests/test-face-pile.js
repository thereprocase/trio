// The header face pile — "who is in this room right now".
//
// The pile carried that contract as a comment for a long time while filtering
// only `archived`. It sliced four faces off an unclassified roster and then
// reported `+N` over every member the channel had ever held, so the operator
// saw "four faces +7" in a room with three live sessions: the faces were
// arbitrary and the badge counted the departed. Liveness has to choose WHO is
// shown, not merely how their dot is painted.
//
// The rules that matter here are the ones about what the NUMBER means, because
// a badge that overcounts is worse than no badge — it invents colleagues:
//   * a stale or dead member is not here, and does not get a face or a tally
//   * `+N` counts only members the pile is about, never the absent
//   * the operator is here by definition and must survive classification —
//     their bare record has no status to read, so it normalises to `offline`
//     unless it is special-cased BEFORE the filter, not after the slice
//   * the cap sheds the least informative faces, not an arbitrary tail
//   * liveness that has not loaded yet must not empty the pile
//
// The model is tested directly rather than through the rendered header because
// dom-harness stubs matchMedia to a permanent { matches: false } — a viewport
// limit asserted through the DOM would silently exercise the desktop branch.
//
// Usage: node tests/test-face-pile.js
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
const model = Trio.workspace.facePileModel;

const named = list => list.faces.map(f => f.member.name);
// A plain roster record, as the roster SSE delivers it. member_status() on the
// server emits exactly: blocked / working / active / idle / stale / dead.
const member = (name, status) => ({ id: name, name, status });
// A supervisor record, as /api/agents delivers it — {live, state, busy} win
// over the heartbeat status when both are present.
const agent = (name, live, agentState, busy = false) => ({ id: name, name, live, state: agentState, busy });

// ── the reported bug ────────────────────────────────────────────────────────
// Three live sessions in an eleven-member room showed four faces and "+7".
const elevenMembers = [
  member('alice', 'working'), member('bob', 'active'), member('carol', 'idle'),
  member('d', 'stale'), member('e', 'stale'), member('f', 'dead'),
  member('g', 'dead'), member('h', 'stale'), member('i', 'dead'),
  member('j', 'stale'), member('k', 'dead'),
];
const reported = model(elevenMembers, [], null, 4);
check('three live sessions in an eleven-member room show three faces',
      reported.faces.length === 3);
check('and no overflow badge at all — the "+7" was the departed',
      reported.overflow === 0);
check('the faces are the live members, not the first four records',
      named(reported).sort().join() === 'alice,bob,carol');

// ── absence is per-status, not per-archived ─────────────────────────────────
check('a stale member gets no face',
      !named(model([member('gone', 'stale')], [], null, 4)).length);
check('a dead member gets no face',
      !named(model([member('gone', 'dead')], [], null, 4)).length);
check('an archived member gets no face',
      !named(model([{ id: 'x', name: 'x', status: 'active', archived: true }], [], null, 4)).length);
// Every status member_status() can emit as live must survive, or the pile
// under-reports — the opposite failure, and just as untrue.
['working', 'blocked', 'active', 'idle'].forEach(status => {
  check(`a ${status} member is present`,
        named(model([member('m', status)], [], null, 4)).join() === 'm');
});
check('a compacting agent is present',
      named(model([member('c', 'idle')], [agent('c', false, 'compacting')], null, 4)).join() === 'c');

// ── the operator survives classification ────────────────────────────────────
// The bare {id,name,source} operator record carries no status/live/state, so
// channelStatus normalises it to 'offline'. Classifying before the filter is
// exactly what would drop it, so this is the regression for that ordering.
const operator = { id: 'op1', name: 'Operator', source: 'web' };
check('the operator has a face even with no status on their record',
      named(model([operator], [], operator, 4)).join() === 'Operator');
check('the operator is not filtered out among live agents',
      named(model([member('alice', 'working'), operator], [], operator, 4)).includes('Operator'));
// A record the server positively reports as gone IS absent when it is not the
// operator — proving the operator's face comes from the special case and not
// from the unknown-status leniency below.
check('a dead record is absent when it is not the operator',
      !named(model([{ id: 'op1', name: 'Operator', status: 'dead' }], [], null, 4)).length);
check('...and present when it IS the operator, who is here by definition',
      named(model([{ id: 'op1', name: 'Operator', status: 'dead' }], [], operator, 4)).join() === 'Operator');

// ── the filter is fail-closed, and that is deliberate ───────────────────────
// A record the classifier cannot place gets no face. The roster boundary
// always supplies a status (member_status emits one of blocked/working/active/
// idle/stale/dead, and no heartbeat at all reads `dead`), so the only
// status-free record the client legitimately holds is the operator's own —
// which is forced present above. Anything else unclassifiable is a client-state
// bug, and showing it as a participant with an offline dot would be the pile
// contradicting itself. Pinned so the leniency is not reintroduced by
// sympathy for a thin test fixture.
const noSignal = { id: 'ag_live', name: 'Alpha', kind: 'agent' };
check('a member with no status at all gets no face',
      !named(model([noSignal], [], null, 4)).length);
check('an unrecognised status gets no face either',
      !named(model([{ ...noSignal, status: 'banana' }], [], null, 4)).length);

// ── the cap sheds the least informative face ────────────────────────────────
const mixed = [member('sleepy', 'idle'), member('busy', 'working'),
               member('stuck', 'blocked'), member('here', 'active')];
// Ranking follows member_status()'s own order, where `blocked` sits ABOVE
// `working`: a blocked session looks busy from outside but waits forever until
// a human answers it, so it is the costliest face to lose behind a "+N".
check('blocked outranks working when only one face fits',
      named(model(mixed, [], null, 1)).join() === 'stuck');
check('working outranks active and idle',
      named(model(mixed, [], null, 2)).join() === 'stuck,busy');
check('a lone worker still wins over idle',
      named(model([member('sleepy', 'idle'), member('busy', 'working')], [], null, 1)).join() === 'busy');
check('compacting sits between working and active',
      named(model([member('a', 'active'), member('c', 'idle'), member('w', 'working')],
                  [agent('c', false, 'compacting')], null, 2)).join() === 'w,c');
check('overflow counts the present members that did not fit',
      model(mixed, [], null, 2).overflow === 2);
check('a limit at or above the roster produces no overflow',
      model(mixed, [], null, 4).overflow === 0);
// Ranking must not drop anyone — reordering is not filtering.
check('every present member is still accounted for across faces + overflow',
      (r => r.faces.length + r.overflow === 4)(model(mixed, [], null, 2)));

// ── blocked survives the supervisor overlay ─────────────────────────────────
// Once /api/agents supplies {live, state}, channelStatus takes its live-agent
// branch. That branch used to answer the working/idle question only, so a
// blocked session — live, fresh heartbeats, mid-turn, but frozen waiting for a
// human — collapsed to `idle` and lost both its rank here and its label in the
// drawer. The roster-only case above cannot catch this: it never enters the
// branch. Both paths are real (the roster arrives before /api/agents), and they
// fail differently, so both are pinned.
const blockedLive = { id: 'stuck', name: 'Stuck', status: 'blocked' };
const liveOverlay = { id: 'stuck', name: 'Stuck', live: true, state: 'running', busy: false };
check('the classifier keeps blocked through the live-agent overlay',
      Trio.workspace.channelStatus({ ...blockedLive, ...liveOverlay }) === 'blocked');
check('a blocked agent still gets a face once agent data has loaded',
      named(model([blockedLive], [liveOverlay], null, 4)).join() === 'Stuck');
check('and is still ranked first, not collapsed into idle',
      named(model([blockedLive, member('busy', 'working')],
                  [liveOverlay, agent('busy', true, 'running', true)], null, 1)).join() === 'Stuck');
check('the drawer row says Blocked, not Idle, for the same member',
      /Blocked/.test(Trio.workspace.detailMember({ ...blockedLive, ...liveOverlay })));
// The overlay must not turn everything into `blocked` — only a blocked roster.
check('a working agent through the same overlay is still working',
      Trio.workspace.channelStatus({ id: 'w', status: 'working', live: true, state: 'running', busy: false }) === 'working');

// ── supervisor state wins over the heartbeat status ─────────────────────────
// A roster member can look alive by heartbeat while the supervisor knows the
// process is gone. The drawer already believes the supervisor; so must the pile.
check('an agent the supervisor reports not-live is absent despite a live heartbeat',
      !named(model([member('ghost', 'active')], [agent('ghost', false, 'stopped')], null, 4)).length);
check('an errored agent is absent — it is not in the room',
      !named(model([member('boom', 'active')], [agent('boom', false, 'error')], null, 4)).length);

// ── cold boot must not empty the pile ───────────────────────────────────────
// /api/agents is slower than the roster SSE, so for the first moments there is
// no supervisor data at all. Classifying from the roster status alone has to
// keep working, or the header blanks on every channel open.
check('faces render from roster status before any agent data has loaded',
      named(model([member('alice', 'working'), member('bob', 'idle')], [], null, 4)).sort().join() === 'alice,bob');
check('an undefined agent list is treated as no agent data, not a crash',
      named(model([member('alice', 'working')], undefined, null, 4)).join() === 'alice');

// ── degenerate inputs stay inert ────────────────────────────────────────────
check('no members produces no faces and no overflow',
      (r => !r.faces.length && !r.overflow)(model([], [], null, 4)));
check('a zero limit still shows one face rather than an empty pile with a badge',
      model([member('a', 'working')], [], null, 0).faces.length === 1);

// ── the rendered header ─────────────────────────────────────────────────────
// The badge is the thing that lied, so assert the text a human actually reads.
state.channel = 'demo';
state.dmKey = '';
state.operator = null;
state.agents = [];
state.members = new Map(elevenMembers.map(m => [m.id, m]));
state.loaded = {};
Trio.workspace.renderFacePile();
const pile = cx.document.getElementById('face-pile');
check('cold metadata keeps fallback initials out of the supposedly-final pile',
      pile.classList.contains('hidden') && pile.querySelectorAll('.av').length === 0);
Trio.workspace.renderChannelMetadataState();
check('cold channel metadata says Loading workspace, not Live',
      cx.document.getElementById('h-meta').textContent === 'Loading workspace…');

state.loaded = { meta: true, agents: true };
Trio.workspace.renderChannelMetadataState();
check('settled channel and agent metadata promotes the subtitle to Live',
      cx.document.getElementById('h-meta').textContent === 'Live agent workspace');
Trio.workspace.renderFacePile();
check('the header paints one face per live member',
      pile.querySelectorAll('.av').length === 3);
state.members.get('alice').avatar_url = '/avatars/Atlas/avatar.svg';
Trio.workspace.renderFacePile();
check('settled roster buddy metadata replaces initials with its image',
      pile.querySelector('img')?.getAttribute('src') === '/avatars/Atlas/avatar.svg');
state.sliceErrors = { agents: new Error('offline') };
Trio.workspace.renderChannelMetadataState();
Trio.workspace.renderFacePile();
check('failed agent metadata does not promote the workspace to Live',
      cx.document.getElementById('h-meta').textContent === 'Workspace metadata unavailable');
check('failed agent metadata cannot resurrect provisional initials',
      pile.classList.contains('hidden') && pile.querySelectorAll('.av').length === 0);
state.sliceErrors = { meta: new Error('offline') };
Trio.workspace.renderChannelMetadataState();
Trio.workspace.renderFacePile();
check('failed channel metadata independently prevents a Live claim',
      cx.document.getElementById('h-meta').textContent === 'Workspace metadata unavailable'
      && pile.classList.contains('hidden'));
state.sliceErrors = {};
Trio.workspace.renderChannelMetadataState();
Trio.workspace.renderFacePile();
check('the header shows no overflow badge when nobody is hidden',
      !pile.querySelector('.more'));

state.members = new Map([...elevenMembers, member('extra1', 'working'),
                         member('extra2', 'working')].map(m => [m.id, m]));
Trio.workspace.renderFacePile();
check('the badge appears once live members exceed the cap',
      pile.querySelector('.more')?.textContent === '+1');
check('the badge describes what it counts',
      pile.querySelector('.more')?.getAttribute('aria-label') === '1 more here');

// ── the mobile cap ──────────────────────────────────────────────────────────
// 360px is the width that decides this, not 390: three faces still leave a
// readable ~70px of title at 390px but only ~40px at 360px. Two faces is what
// the narrowest supported header can afford; the badge is what stops the
// smaller cap from becoming a smaller lie.
//
// The cap is asserted through the MODEL with an explicit limit, never through
// the render path, because dom-harness pins matchMedia to { matches: false } —
// a "mobile" assertion made through renderFacePile would quietly exercise the
// desktop branch and pass.
const fivePresent = [member('a', 'working'), member('b', 'working'), member('c', 'working'),
                     member('d', 'working'), member('e', 'working')];
check('the narrow cap shows two faces',
      model(fivePresent, [], null, 2).faces.length === 2);
check('and the badge accounts for the other three',
      model(fivePresent, [], null, 2).overflow === 3);
check('the wide cap still shows four',
      model(fivePresent, [], null, 4).faces.length === 4);
check('faces + overflow is the present count at either cap',
      [2, 4].every(cap => (r => r.faces.length + r.overflow === 5)(model(fivePresent, [], null, cap))));

// The rendered badge at the narrow cap, since the number and its label are the
// part a person actually reads.
state.members = new Map(fivePresent.map(m => [m.id, m]));
Trio.workspace.renderFacePile();
const wideBadge = pile.querySelector('.more');
check('the desktop render is unchanged at four faces plus one over',
      pile.querySelectorAll('.av').length === 4 && wideBadge?.textContent === '+1');

// The model assertions above prove the cap ARITHMETIC but not that the render
// path consults the viewport at all — hardcoding the wide limit back into
// renderFacePile passes every one of them. Overriding the harness's permanently
// -false matchMedia is what closes that gap, and it is the only way to reach
// the narrow branch here.
let added = 0, removed = 0, narrow = false;
let onChange = null, removedFn = null;
const realMatchMedia = cx.context.window.matchMedia;
cx.context.window.matchMedia = () => ({
  get matches() { return narrow; },
  addEventListener: (_type, fn) => { added++; onChange = fn; },
  removeEventListener: (_type, fn) => { removed++; removedFn = fn; },
});

narrow = true;
Trio.workspace.renderFacePile();
check('a narrow viewport renders two faces, not four',
      pile.querySelectorAll('.av').length === 2);
check('and the badge counts the three it dropped',
      pile.querySelector('.more')?.textContent === '+3');
check('the badge still says what it counts',
      pile.querySelector('.more')?.getAttribute('aria-label') === '3 more here');

narrow = false;
Trio.workspace.renderFacePile();
check('crossing back to wide repaints to four immediately',
      pile.querySelectorAll('.av').length === 4 && pile.querySelector('.more')?.textContent === '+1');

// A media-query listener has to exist, DO something, and be surrendered — or
// rotating a phone leaves the wrong count until an unrelated repaint. Counting
// registrations alone is not enough: a listener wired to a no-op would satisfy
// that and change nothing on rotation, so the callback is captured and fired.
Trio.workspace.mount();
check('mounting registers exactly one viewport listener', added === 1);

// The pile is not decoration: it is the direct channel-roster affordance.
// Exercise the recorded DOM listeners explicitly because dom-harness records
// element handlers but deliberately does not synthesize browser click events.
const drawer = cx.document.getElementById('channel-drawer');
const prevent = () => {};
pile._listeners.click.at(-1)({ type: 'click', preventDefault: prevent });
check('clicking the face pile opens the existing channel drawer',
      drawer.classList.contains('open') && pile.getAttribute('aria-expanded') === 'true');
cx.document.getElementById('channel-drawer-close')._listeners.click.at(-1)({ type: 'click' });
check('closing the drawer resets face-pile aria-expanded',
      !drawer.classList.contains('open') && pile.getAttribute('aria-expanded') === 'false');
pile._listeners.keydown.at(-1)({ type: 'keydown', key: 'Enter', preventDefault: prevent });
check('Enter opens the channel roster from the face pile', drawer.classList.contains('open'));
cx.document.getElementById('channel-drawer-close')._listeners.click.at(-1)({ type: 'click' });
pile._listeners.keydown.at(-1)({ type: 'keydown', key: ' ', preventDefault: prevent });
check('Space opens the channel roster from the face pile', drawer.classList.contains('open'));
cx.document.getElementById('channel-drawer-close')._listeners.click.at(-1)({ type: 'click' });

narrow = true;
onChange?.({ matches: true });
check('the listener repaints to the narrow cap when the breakpoint is crossed',
      pile.querySelectorAll('.av').length === 2 && pile.querySelector('.more')?.textContent === '+3');
narrow = false;
onChange?.({ matches: false });
check('and back to four crossing the other way',
      pile.querySelectorAll('.av').length === 4 && pile.querySelector('.more')?.textContent === '+1');

Trio.workspace.unmount();
check('unmounting removes it again', removed === 1);
check('and removes the same callback it registered', removedFn === onChange);
check('unmounting removes face-pile click and keyboard handlers',
      pile._listeners.click.length === 0 && pile._listeners.keydown.length === 0);
cx.context.window.matchMedia = realMatchMedia;

console.log();
if (failures.length) {
  console.log(`FAILED — ${failures.length} of ${failures.length + passed}`);
  failures.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log(`OK — ${passed} passed`);
