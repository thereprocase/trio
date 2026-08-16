'use strict';

// An archived agent keeps its channel placement so unarchiving can restore it,
// but it must not read as a live member that happens to be down. These checks
// pin the "Archived, not Offline" rendering in the channel details drawer and
// its absence from the face-pile.
//
// Usage: node tests/test-archived-member-ui.js
const assert = require('assert');
const { load } = require('./dom-harness');

const cx = load();
const H = cx.hooks;
const ws = H.Trio.workspace;
const state = H.Trio.state;
let failures = 0;
function check(name, fn) { try { fn(); console.log('PASS: ' + name); } catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); } }

check('an archived member reads archived, never offline', () => {
  assert.strictEqual(ws.channelStatus({ id: 'ag_1', archived: true }), 'archived');
  // The server also sends status:"archived" — honoured even without the flag.
  assert.strictEqual(ws.channelStatus({ id: 'ag_1', status: 'archived' }), 'archived');
  // A live-looking supervisor record must not override the archive fact: the
  // {live,state} branch would otherwise call this one "idle".
  assert.strictEqual(
    ws.channelStatus({ id: 'ag_1', archived: true, live: true, state: 'running' }),
    'archived');
});

check('a non-archived member is unaffected', () => {
  assert.strictEqual(ws.channelStatus({ id: 'ag_2', archived: false, live: true, state: 'running' }), 'idle');
  assert.strictEqual(ws.channelStatus({ id: 'ag_3' }), 'offline');
});

check('the drawer row is labelled Archived and dimmed', () => {
  const html = ws.detailMember({ id: 'ag_1', name: 'Horizon', archived: true });
  assert.ok(html.includes('is-archived'), 'row should carry the dimming class');
  assert.ok(html.includes('channel-status-chip archived'), 'chip should read archived');
  assert.ok(html.includes('Archived'), 'row should say Archived');
  assert.strictEqual(html.includes('Offline'), false, 'row must not say Offline');
});

check('a stale status_text cannot outrank the archive fact', () => {
  // The agent's last words before it was archived would otherwise render as
  // its current status, reading as though it were still working.
  const html = ws.detailMember({
    id: 'ag_1', name: 'Horizon', archived: true,
    status_text: 'idle — standing by for dispatch',
  });
  assert.ok(html.includes('Archived — restore to rejoin'));
  assert.strictEqual(html.includes('standing by for dispatch'), false);
});

check('an archived agent gets no face in the face-pile', () => {
  state.channel = 'room';
  state.dmKey = ''; state.dmMemberIds = [];
  state.operator = { id: 'op_1', name: 'operator' };
  state.agents = [];
  state.members = new Map([
    ['ag_live', { id: 'ag_live', name: 'Alpha', kind: 'agent' }],
    ['ag_gone', { id: 'ag_gone', name: 'Horizon', kind: 'agent', archived: true }],
  ]);
  const pile = cx.document.getElementById('face-pile');
  pile.replaceChildren();
  ws.renderFacePile();
  const html = pile.childNodes.map(n => n.innerHTML || '').join(' ');
  assert.ok(html.includes('A'), 'the live agent should still have a face');
  assert.strictEqual(/st-archived/.test(html), false, 'no archived face in the pile');
  // Two faces, not three: the live agent + the operator.
  assert.strictEqual(pile.childNodes.length, 2);
});

console.log();
console.log(failures ? `FAILED — ${failures} failure(s)` : 'OK — 0 failure(s)');
process.exit(failures ? 1 : 0);
