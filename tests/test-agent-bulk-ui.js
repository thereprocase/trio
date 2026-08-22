'use strict';

// Client coverage for bulk agent management on the roster page: the per-card
// selection checkbox, the action bar it summons, select-all scoping, the
// payload each bulk control posts, and how partial failures are reported.
const assert = require('assert');
const { load, FakeElement } = require('./dom-harness');
const cx = load();
const H = cx.hooks;
let failures = 0;
function check(name, fn) { try { fn(); console.log('PASS: ' + name); } catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); } }

const agents = H.Trio.agents;

function seed(list) {
  // Same order refresh() writes them in: state.agents first, then the store
  // slice. (state.agents is overwritten with the raw array in production, so
  // seeding the other way round leaves store.get('agents.list') stale.)
  H.Trio.state.agents = list;
  H.Trio.store.set('agents.list', list);
  H.Trio.state.agentFilter = 'all';
  H.Trio.state.agentsSearch = '';
  agents.selection.clear();
}
function page() {
  const panel = new FakeElement('section');
  agents.renderPage(panel);
  return panel;
}
// renderBulkBar only paints into the LIVE roster view (document lookup), so
// point the harness's #trio-roster-view at the panel under test.
function livePage() {
  const panel = cx.document.getElementById('trio-roster-view');
  panel.replaceChildren();
  panel.hidden = false;
  agents.renderPage(panel);
  return panel;
}
// The fake DOM resolves '.class' and element-scoped tag queries, but not a
// '.class tag' descendant pair — query in two steps.
function tick(card) { return card.querySelector('.ac-pick')?.querySelector('input'); }

const THREE = [
  { id: 'ag_1', name: 'Alpha', provider: 'claude', model: 'sonnet', live: true, state: 'idle' },
  { id: 'ag_2', name: 'Beta', provider: 'claude', model: 'haiku', live: false, state: 'sleeping' },
  { id: 'ag_3', name: 'Gamma', provider: 'codex', model: 'fake-codex', live: false, state: 'stopped' },
];

check('every roster card carries a selection checkbox', () => {
  seed(THREE);
  const cards = page().querySelectorAll('.agent-card');
  assert.strictEqual(cards.length, 3);
  cards.forEach(card => assert.ok(tick(card), 'card is missing its checkbox'));
});

check('ticking a card selects that agent without opening its details', () => {
  seed(THREE);
  const originalModal = H.Trio.ui.modal;
  let opened = 0;
  H.Trio.ui.modal = () => { opened++; };
  try {
    const card = livePage().querySelector('.agent-card');
    const box = tick(card);
    box.checked = true;
    box._listeners.change[0]({ target: box });
    assert.deepStrictEqual([...agents.selection], ['ag_1']);
    assert.strictEqual(opened, 0, 'ticking must not open the manage dialog');
  } finally { H.Trio.ui.modal = originalModal; }
});

check('the bulk bar is hidden until something is selected', () => {
  seed(THREE);
  assert.strictEqual(livePage().querySelector('.bulk-bar'), null);
  agents.toggleSelected('ag_2', true);
  const bar = cx.document.getElementById('trio-roster-view').querySelector('.bulk-bar');
  assert.ok(bar);
  assert.strictEqual(bar.querySelector('.bulk-count').textContent, '1 selected');
});

check('select all covers only the agents the filter and search leave visible', () => {
  seed(THREE);
  H.Trio.state.agentsSearch = 'alp';
  const panel = livePage();
  const selectAll = panel.querySelector('.roster-toolbar').querySelectorAll('button').find(b => b.textContent.startsWith('Select all'));
  assert.ok(selectAll, 'toolbar is missing the select-all control');
  assert.strictEqual(selectAll.textContent, 'Select all (1)');
  selectAll._listeners.click[0]({});
  assert.deepStrictEqual([...agents.selection], ['ag_1']);
  H.Trio.state.agentsSearch = '';
});

check('clear selection empties the set and retires the bar', () => {
  seed(THREE);
  agents.toggleSelected('ag_1', true);
  agents.toggleSelected('ag_2', true);
  const bar = livePage().querySelector('.bulk-bar');
  bar.querySelector('.bulk-clear')._listeners.click[0]({});
  assert.strictEqual(agents.selection.size, 0);
  assert.strictEqual(cx.document.getElementById('trio-roster-view').querySelector('.bulk-bar'), null);
});

check('a selected agent that leaves the roster drops out of the selection', () => {
  seed(THREE);
  agents.toggleSelected('ag_3', true);
  seedRemaining();
  function seedRemaining() {
    const rest = THREE.filter(a => a.id !== 'ag_3');
    H.Trio.state.agents = rest;
    H.Trio.store.set('agents.list', rest);
  }
  livePage();
  assert.strictEqual(agents.selection.has('ag_3'), false);
});

// ── payloads ────────────────────────────────────────────────────────────────
function capturePost(fn) {
  const originalPost = H.Trio.api.post;
  const originalConfirm = cx.window.confirm;
  const calls = [];
  H.Trio.api.post = (path, body) => { calls.push({ path, body }); return Promise.resolve({ ok: true, count: (body.agent_ids || []).length, results: (body.agent_ids || []).map(id => ({ agent_id: id, ok: true })) }); };
  cx.window.confirm = () => true;
  try { fn(); } finally { H.Trio.api.post = originalPost; cx.window.confirm = originalConfirm; }
  return calls;
}
function barButton(label) {
  const bar = cx.document.getElementById('trio-roster-view').querySelector('.bulk-bar');
  const button = bar.querySelector('.bulk-actions').querySelectorAll('button').find(b => b.textContent === label);
  assert.ok(button, 'bulk bar is missing the ' + label + ' control');
  return button;
}

check('archive posts one bulk request naming every selected agent', () => {
  seed(THREE);
  agents.toggleSelected('ag_1', true);
  agents.toggleSelected('ag_3', true);
  livePage();
  const calls = capturePost(() => barButton('Archive')._listeners.click[0]({}));
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].path, '/api/agents/bulk');
  assert.strictEqual(calls[0].body.action, 'archive');
  assert.deepStrictEqual(Array.from(calls[0].body.agent_ids), ['ag_1', 'ag_3']);
});

check('the archived filter offers unarchive instead of the live-agent controls', () => {
  seed(THREE);
  H.Trio.state.agentFilter = 'archived';
  const archivedOnly = [{ id: 'ag_1', name: 'Alpha', archived_at: '2026-08-13T00:00:00Z' }];
  H.Trio.state.agents = archivedOnly;
  H.Trio.store.set('agents.list', archivedOnly);
  agents.selection.add('ag_1');
  livePage();
  const bar = cx.document.getElementById('trio-roster-view').querySelector('.bulk-bar');
  const labels = bar.querySelector('.bulk-actions').querySelectorAll('button').map(b => b.textContent);
  assert.deepStrictEqual(Array.from(labels), ['Unarchive']);
  const calls = capturePost(() => barButton('Unarchive')._listeners.click[0]({}));
  assert.strictEqual(calls[0].body.action, 'unarchive');
  H.Trio.state.agentFilter = 'all';
});

check('the attribute editor offers every field, each defaulting to unchanged', () => {
  seed(THREE);
  agents.selection.add('ag_1');
  agents.selection.add('ag_2');
  const originalModal = H.Trio.ui.modal;
  let submit = null;
  H.Trio.ui.modal = (title, body, onSubmit) => { submit = { title, body, onSubmit }; };
  try { agents.showBulkAttributes(); } finally { H.Trio.ui.modal = originalModal; }
  assert.ok(submit.title.startsWith('Attributes: 2 agents'));
  ['model', 'effort', 'wake', 'permission_profile', 'cwd'].forEach(field =>
    assert.ok(submit.body.includes(`name="${field}"`), 'missing field ' + field));
  // One "Leave unchanged" per select (model, effort, wake, permissions).
  assert.strictEqual(submit.body.split('Leave unchanged').length - 1, 4);
});

check('only the attribute fields the operator touched become bulk calls', () => {
  const unchanged = '__unchanged__';
  // Values cross the vm-sandbox realm boundary, so compare serialized shapes
  // rather than deepStrictEqual (which fails on prototype identity alone).
  const shape = fields => JSON.stringify(agents.bulkAttributeJobs(fields).map(j => [j[0], j[1]]));
  assert.strictEqual(
    shape({ model: unchanged, effort: 'low', wake: unchanged,
            permission_profile: 'observe', cwd_apply: false, cwd: '' }),
    JSON.stringify([['effort', { effort: 'low' }],
                    ['permissions', { permission_profile: 'observe' }]]));
  // Untouched everything -> nothing is sent at all.
  assert.strictEqual(shape({ model: unchanged, effort: unchanged, wake: unchanged,
                             permission_profile: unchanged, cwd_apply: false, cwd: '' }), '[]');
  // "Model default" is an empty effort, and must still be sent.
  assert.strictEqual(shape({ effort: '' }), JSON.stringify([['effort', { effort: '' }]]));
  // An empty cwd clears it, but only when the operator opted in.
  assert.strictEqual(shape({ cwd_apply: true, cwd: '  ' }), JSON.stringify([['cwd', { cwd: '' }]]));
  assert.strictEqual(shape({ cwd_apply: false, cwd: '/tmp' }), '[]');
  // A model change carries the model through untouched.
  assert.strictEqual(shape({ model: 'haiku' }), JSON.stringify([['model', { model: 'haiku' }]]));
});

check('a mixed-provider selection cannot pick a model', () => {
  seed(THREE);
  agents.selection.add('ag_1');   // claude
  agents.selection.add('ag_3');   // codex
  const originalModal = H.Trio.ui.modal;
  let body = '';
  H.Trio.ui.modal = (title, html) => { body = html; };
  try { agents.showBulkAttributes(); } finally { H.Trio.ui.modal = originalModal; }
  assert.strictEqual(body.includes('name="model"'), false);
  assert.ok(body.includes('spans claude + codex'));
});

check('the channel editor posts the picked channels with an add/remove flag', () => {
  seed(THREE);
  agents.selection.add('ag_1');
  H.Trio.state.channels = [{ code: 'build' }, { code: 'design' }];
  const originalModal = H.Trio.ui.modal;
  let submit = null;
  H.Trio.ui.modal = (title, body, onSubmit) => { submit = { body, onSubmit }; };
  try { agents.showBulkChannels(); } finally { H.Trio.ui.modal = originalModal; }
  assert.ok(submit.body.includes('data-code="build"'));
  const node = new FakeElement('div');
  const mode = new FakeElement('input');
  mode.setAttribute('name', 'mode'); mode.value = 'remove'; mode.checked = true;
  node.append(mode);
  // No channel ticked -> refuses rather than posting an empty membership edit.
  let toasted = '';
  const originalToast = H.Trio.ui.toast;
  H.Trio.ui.toast = msg => { toasted = msg; };
  try {
    const none = capturePost(() => submit.onSubmit(node));
    assert.strictEqual(none.length, 0);
    assert.ok(/at least one channel/i.test(toasted));
  } finally { H.Trio.ui.toast = originalToast; }
});

check('partial failures are surfaced, not swallowed', () => {
  const originalModal = H.Trio.ui.modal;
  const originalToast = H.Trio.ui.toast;
  let opened = null; let toasted = null;
  H.Trio.ui.modal = (title, body) => { opened = { title, body }; };
  H.Trio.ui.toast = msg => { toasted = msg; };
  try {
    seed(THREE);
    agents.reportBulk({ count: 3, results: [
      { agent_id: 'ag_1', ok: true },
      { agent_id: 'ag_2', ok: false, status: 409, error: 'agent is archived — unarchive first' },
      { agent_id: 'ag_3', ok: false, status: 404, error: 'agent not found or no-op' },
    ] }, 'Archived');
    assert.ok(opened, 'a partial failure must open a report');
    assert.strictEqual(opened.title, 'Archived: 1 of 3 succeeded');
    assert.ok(opened.body.includes('Beta'));
    assert.ok(opened.body.includes('unarchive first'));
    opened = null;
    agents.reportBulk({ count: 2, results: [
      { agent_id: 'ag_1', ok: true }, { agent_id: 'ag_2', ok: true },
    ] }, 'Archived');
    assert.strictEqual(opened, null, 'a clean run should not open a modal');
    assert.strictEqual(toasted, 'Archived: 2 agents');
  } finally { H.Trio.ui.modal = originalModal; H.Trio.ui.toast = originalToast; }
});

console.log();
console.log(failures ? `FAILED — ${failures} failure(s)` : 'OK — 0 failure(s)');
process.exit(failures ? 1 : 0);
