// Which messages the DM view will actually RENDER.
//
// Trio.conversation.upsert() is the gate. In a DM it drops anything it cannot
// confirm the viewer is a party to, which is right — the workspace-wide SSE
// stream multiplexes every channel through this same function, so a DM between
// two other people must never appear in your thread.
//
// But the check read `state.operator?.id` directly, while every other call site
// in the workspace reads `state.operator || state.meta.operator`. A boot that
// failed to populate the first (in landing mode /api/meta 400'd without a
// channel, so boot set it to null for the whole session) turned "am I a party
// to this?" into an unconditional NO. The thread still listed — that list is
// built server-side — and opening it rendered nothing at all: not the agent's
// messages, not the operator's own. It reads as an agent that never replied.
//
// So: both directions of a 1:1 must render, someone else's DM must not, and
// neither rule may depend on which of the two identity slots is populated.
//
// Usage: node tests/test-dm-visibility.js
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

const OP = '_op_me';
const AGENT = 'ag_smith';
const OTHER = 'ag_other';

// Open a 1:1 DM with AGENT, with identity supplied via `slot`.
function openDmAs(slot) {
  state.messages = new Map();
  state.messageDomById = new Map();
  state.lastSeenByConv = {};
  state.dividerBaseByConv = {};
  state.dmAudit = false;
  state.dmKey = OP + ',' + AGENT;
  state.dmMemberIds = [AGENT];
  state.channel = 'nth-agent-inbox';
  state.operator = null;
  state.meta = {};
  if (slot === 'operator') state.operator = { id: OP, name: 'me' };
  if (slot === 'meta') state.meta = { operator: { id: OP, name: 'me' } };
  // slot === 'none' leaves both empty
}

function msg(id, from, to) {
  return { id, channel: 'nth-agent-inbox', member_id: from, recipients: to,
           member_name: from, content: 'm' + id, created_at: new Date().toISOString() };
}

function rendered() { return [...state.messages.keys()].sort((a, b) => a - b); }

// ── the reported bug: both directions, under each identity slot ─────────────
for (const slot of ['operator', 'meta']) {
  openDmAs(slot);
  Trio.conversation.upsert(msg(1, OP, [AGENT]));      // operator -> agent
  Trio.conversation.upsert(msg(2, AGENT, [OP]));      // agent -> operator
  check(`identity in state.${slot}: the operator's own DM renders`, rendered().includes(1));
  check(`identity in state.${slot}: the agent's reply renders`, rendered().includes(2));
}

// ── the security rule the gate exists for ───────────────────────────────────
for (const slot of ['operator', 'meta']) {
  openDmAs(slot);
  Trio.conversation.upsert(msg(3, OTHER, [AGENT]));   // two other parties
  check(`identity in state.${slot}: someone else's DM is excluded`,
        !rendered().includes(3));
}

// A DM to the operator but from a DIFFERENT agent belongs to a different
// thread, so it must not bleed into this one.
openDmAs('operator');
Trio.conversation.upsert(msg(4, OTHER, [OP]));
check('a DM from another agent stays out of this thread', !rendered().includes(4));

// ── identity unknown entirely ───────────────────────────────────────────────
// Cannot verify membership, so fall back to the thread's own participants —
// which is what the audit view already does. Rendering the server's own
// answer is not a disclosure (it scoped /api/dms to what this caller may
// read); rendering NOTHING is the worse failure, because an empty thread
// looks like a conversation that never happened.
openDmAs('none');
Trio.conversation.upsert(msg(5, OP, [AGENT]));
Trio.conversation.upsert(msg(6, AGENT, [OP]));
check('identity unknown: the thread still renders rather than looking empty',
      rendered().includes(5) && rendered().includes(6));
openDmAs('none');
Trio.conversation.upsert(msg(7, OTHER, ['ag_third']));
check('identity unknown: an unrelated pair is still excluded', !rendered().includes(7));

console.log();
if (failures.length) {
  console.log(`FAILED — ${failures.length} of ${failures.length + passed}`);
  failures.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log(`OK — ${passed} passed`);
