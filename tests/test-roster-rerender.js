// A roster tick must not rebuild the whole conversation.
//
// "roster" does not mean someone joined. The server broadcasts whenever its
// snapshot differs, and that snapshot includes messenger_heartbeat,
// watchdog_heartbeat and last_seen — which nth_monitor rewrites every ~10s per
// member with no message traffic at all. In a ten-agent room the snapshot
// therefore changes every second or two, indefinitely.
//
// render() clears #messages and rebuilds every visible card through
// cardFor -> paintBody -> decorateSigils (a TreeWalker + regex per text node)
// -> decorateFilePaths (a second TreeWalker). LOTC/Legolas measured ~116ms for
// 500 messages. Re-running that on presence churn discards the entire benefit
// of the incremental upsert() path.
//
// This is a REGRESSION THE FIX CAUSED, which is why it is pinned. Before the
// channel field was stamped on roster events, the guard in onRoster rejected
// every tick and render() was never reached from here — so making the roster
// work switched on a full re-render loop that had been dormant.
//
// The contract: apply the members either way (names must stay fresh), but only
// repaint when something this view actually draws from a member has changed —
// their id or their display name.
//
// Usage: node tests/test-roster-rerender.js
'use strict';

const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, cond, detail = '') {
  if (cond) { passed++; console.log('PASS: ' + name); }
  else { failures.push(name); console.log('FAIL: ' + name + (detail ? ' — ' + detail : '')); }
}

const cx = load();
const Trio = cx.hooks.Trio;
const document = cx.document;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

const state = Trio.state;
state.channel = 'rr';
state.messages = new Map();
for (let i = 1; i <= 20; i++) {
  state.messages.set(i, {
    id: i, member_id: 'a1', member_name: 'Ada', content: 'm' + i,
    created_at: new Date().toISOString(),
    mentions: [], refs: [], bangs: [], recipients: [],
  });
}
state.members = new Map();
state.messageDomById = new Map();

// The harness deliberately stops 90-boot from auto-running, so no feature is
// mounted and no listener is attached. onRoster is registered by the
// conversation feature's init(), so mount it — otherwise every roster
// dispatched below goes nowhere and the assertions pass vacuously.
Trio.lifecycle.mount('conversation', Trio.conversation);

// Count renders by watching the DOM nodes render() creates: it calls
// replaceChildren() and rebuilds every card, so the card objects are new
// identities each time. Comparing identity is what distinguishes "repainted"
// from "left alone" — a length check cannot, since the count is the same.
function cardIdentities() {
  Trio.conversation.render();
  return [...Trio.state.messageDomById.values()];
}

function fireRoster(members, channel = 'rr') {
  Trio.events.dispatchEvent(new cx.window.CustomEvent('roster', {
    detail: { type: 'roster', channel, members },
  }));
}

const ROSTER = [
  { id: 'a1', name: 'Ada', last_seen: 't0', messenger_heartbeat: 'h0' },
  { id: 'b2', name: 'Bo', last_seen: 't0', messenger_heartbeat: 'h0' },
];

// Seed: first roster is a real change (empty -> two members), so it must paint.
const before = cardIdentities();
fireRoster(ROSTER);
check('the first roster is applied to state.members',
      Trio.state.members.size === 2);
const afterFirst = [...Trio.state.messageDomById.values()];
check('…and it repaints, because the members genuinely changed',
      afterFirst.length && afterFirst[0] !== before[0]);

// THE CASE THAT MATTERS: identical people, moved heartbeats. This is what
// arrives every ~10s per member, forever.
const marker = [...Trio.state.messageDomById.values()];
fireRoster(ROSTER.map(m => ({ ...m, last_seen: 't1',
                              messenger_heartbeat: 'h1',
                              watchdog_heartbeat: 'w1' })));
const afterHeartbeat = [...Trio.state.messageDomById.values()];
check('a heartbeat-only roster tick does NOT rebuild the message list',
      afterHeartbeat[0] === marker[0],
      'every card was recreated for a tick that changed no name');
check('…but the fresher member data IS still applied',
      Trio.state.members.get('a1').last_seen === 't1');

// A rename DOES change what the conversation paints (author lines, @mention
// chips resolve through nameFor), so it must repaint.
fireRoster([{ id: 'a1', name: 'Adaline' }, { id: 'b2', name: 'Bo' }]);
const afterRename = [...Trio.state.messageDomById.values()];
check('a renamed member DOES repaint — author lines and mention chips resolve '
      + 'through the roster',
      afterRename[0] !== marker[0]);

// So does someone arriving or leaving.
const beforeJoin = [...Trio.state.messageDomById.values()];
fireRoster([{ id: 'a1', name: 'Adaline' }, { id: 'b2', name: 'Bo' },
            { id: 'c3', name: 'Cy' }]);
check('a member joining repaints',
      [...Trio.state.messageDomById.values()][0] !== beforeJoin[0]);

const beforeLeave = [...Trio.state.messageDomById.values()];
fireRoster([{ id: 'a1', name: 'Adaline' }]);
check('a member leaving repaints',
      [...Trio.state.messageDomById.values()][0] !== beforeLeave[0]);

// The cross-channel guard still holds, and still costs nothing.
const beforeForeign = [...Trio.state.messageDomById.values()];
const keptMembers = Trio.state.members.size;
fireRoster([{ id: 'z9', name: 'Zed' }], 'some-other-channel');
check('a roster for another channel is ignored entirely',
      Trio.state.members.size === keptMembers
      && !Trio.state.members.has('z9'));
check('…and does not repaint either',
      [...Trio.state.messageDomById.values()][0] === beforeForeign[0]);

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
process.exit(failures.length ? 1 : 0);
