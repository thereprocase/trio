// Delivery hints supplement status prose and respect stronger lifecycle states.
'use strict';
process.env.TZ = 'America/New_York';
const assert = require('assert');
const { load } = require('./dom-harness');
const cx = load();
const Trio = cx.hooks.Trio;
const ws = Trio.workspace;
const document = cx.document;
let passed = 0;
function check(name, fn) { fn(); passed++; console.log('PASS: ' + name); }
function render(member) {
  const node = document.createElement('div');
  node.innerHTML = ws.detailMember(member);
  return node;
}
const stamp = '2030-01-02T12:30:00+00:00';
for (const [status, label, expected, chip] of [
  ['idle', 'listening (hooks)', 'idle', 'Idle'],
  ['working', 'working', 'working', 'Working'],
  ['stale', 'unreachable', 'offline', 'Offline'],
]) {
  check('delivery supplements prose with a generic ' + chip + ' chip', () => {
    const member = { id: 'reader', name: 'Reader', status, delivery_label: label,
                     status_text: 'my own words', live: true, state: 'running', busy: true };
    assert.strictEqual(ws.channelStatus(member), expected);
    const node = render(member);
    assert.strictEqual(node.querySelector('.channel-member-status').textContent, 'my own words');
    assert.strictEqual(node.querySelector('.channel-member-delivery').textContent, label);
    assert.strictEqual(node.querySelector('.channel-status-chip').textContent, chip);
  });
}
// Members without a delivery report keep the original supervisor-first path: a working agent
// stays Working even when a reclaim left a stale supervisor state behind.
for (const [member, expected] of [
  [{ live: true, state: 'sleeping', status: 'working', busy: true }, 'working'],
  [{ live: true, state: 'errored', status: 'working' }, 'working'],
  [{ live: true, state: 'sleeping', status: 'idle' }, 'idle'],
  [{ live: false, state: 'stopped', status: 'blocked' }, 'offline'],
]) {
  check('no delivery report: ' + JSON.stringify(member) + ' stays ' + expected, () => {
    assert.strictEqual(ws.channelStatus({ id: 'm', name: 'M', ...member }), expected);
  });
}
for (const [mode, clock] of [['local', '07:30:00'], ['utc', '12:30:00Z']]) {
  check('silent timestamp follows ' + mode + ' preference', () => {
    Trio.preferences.save({messageTimes: mode});
    const member = {id: 'reader', name: 'Reader', status: 'stale', delivery_label: 'silent since',
                    delivery_state_at: stamp, status_text: 'my own words'};
    const node = render(member);
    assert.strictEqual(node.querySelector('.channel-member-delivery').textContent, 'silent since ' + clock);
    assert.strictEqual(node.querySelector('.channel-status-chip').textContent, 'Offline');
    assert.strictEqual(node.querySelector('.channel-member-status').textContent, 'my own words');
  });
}
for (const stronger of [
  {status: 'blocked'}, {status: 'errored'}, {status: 'archived'}, {archived: true},
  {state: 'errored'}, {state: 'error'}, {state: 'sleeping'}, {state: 'compacting'},
]) {
  const expected = stronger.archived ? 'archived' : (stronger.status || (stronger.state === 'error' ? 'errored' : stronger.state));
  check('stronger ' + JSON.stringify(stronger) + ' suppresses delivery hints', () => {
    const member = {id: 'reader', name: 'Reader', status: 'working', delivery_label: 'listening (hooks)',
                    status_text: 'my own words', live: true, state: 'running', ...stronger};
    assert.strictEqual(ws.channelStatus(member), expected);
    const node = render(member);
    assert.strictEqual(node.querySelector('.channel-member-delivery'), null);
    assert.strictEqual(node.querySelector('.channel-status-chip').textContent,
                       expected[0].toUpperCase() + expected.slice(1));
    if (expected !== 'archived') assert.strictEqual(node.querySelector('.channel-member-status').textContent, 'my own words');
  });
}
check('legacy status and chip remain unchanged', () => {
  const node = render({id: 'legacy', name: 'Legacy', status: 'active', status_text: 'ready'});
  assert.strictEqual(node.querySelector('.channel-member-status').textContent, 'ready');
  assert.strictEqual(node.querySelector('.channel-status-chip').textContent, 'Active');
  assert.strictEqual(node.querySelector('.channel-member-delivery'), null);
});
check('archived status and chip remain unchanged', () => {
  const node = render({id: 'archive', name: 'Archived', archived: true, delivery_label: 'silent since', delivery_state_at: stamp});
  assert.strictEqual(node.querySelector('.channel-member-status').textContent, 'Archived — restore to rejoin');
  assert.strictEqual(node.querySelector('.channel-status-chip').textContent, 'Archived');
  assert.strictEqual(node.querySelector('.channel-member-delivery'), null);
});
check('changing time preference repaints an open roster drawer immediately', () => {
  ws.unmount();
  Trio.state.channel = 'room';
  Trio.state.dmKey = '';
  Trio.state.agents = [];
  Trio.state.operator = null;
  Trio.state.members = new Map([['reader', {id: 'reader', name: 'Reader', kind: 'human',
    status: 'stale', delivery_label: 'silent since', delivery_state_at: stamp}]]);
  Trio.preferences.save({messageTimes: 'utc'});
  ws.mount();
  ws.showDetails();
  const drawer = document.getElementById('channel-drawer-body').querySelector('#channel-drawer-members');
  // The harness auto-creates missing ids; map this parsed descendant explicitly.
  document._byId.set('channel-drawer-members', drawer);
  assert.strictEqual(drawer.querySelector('.channel-member-delivery').textContent, 'silent since 12:30:00Z');
  Trio.preferences.save({messageTimes: 'local'});
  assert.strictEqual(drawer.querySelector('.channel-member-delivery').textContent, 'silent since 07:30:00');
  ws.unmount();
});
console.log(`${passed} passed`);
