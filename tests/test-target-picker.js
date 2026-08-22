// Alt+1..9 / Alt+A / Alt+0 — the composer's keyboard target picker.
//
// Restored from the single-pane client this workspace UI replaced. Addressing
// a message at specific agents is the core operator gesture in this app, and
// the port dropped it: the new composer offered only typed "@" autocomplete,
// so selecting a recipient went from one keystroke to typing a sigil, waiting
// for the list, arrowing and tabbing.
//
// The part worth pinning is the NUMBERING. The digits are only usable if a
// given agent keeps the same one — order by roster insertion or by a hash and
// the numbers shuffle as agents come and go, which is worse than no shortcut
// because it silently addresses the wrong agent.
//
// Usage: node tests/test-target-picker.js
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
const Trio = cx.hooks.Trio;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

const state = Trio.state;
const C = Trio.composer;

function roster(names) {
  state.members = new Map(names.map(([id, name]) => [id, { id, name }]));
}
// Joined rather than compared as arrays: these are built inside the vm
// sandbox, so their prototype is that realm's Array and deepStrictEqual — which
// checks prototypes — fails on structurally identical values.
function selected() { return [...state.selectedTargets].sort().join(','); }
function order() { return C.targetOrder().join(','); }

state.operator = { id: 'op', name: 'exampleuser' };
state.channel = 'picker';
state.dmKey = '';
state.dmTargetId = '';
state.selectedTargets = new Set();
state.targetDrafts = {};
state.drafts = {};

// Deliberately inserted out of alphabetical order, so a roster-order
// implementation and a name-order one disagree.
roster([['z9', 'Zed'], ['a1', 'Ada'], ['m5', 'Mo'], ['op', 'exampleuser']]);

check('the numbered order is by NAME, so a digit keeps meaning the same agent', () => {
  assert.strictEqual(order(), 'a1,m5,z9');
});

check('the operator is not addressable — you cannot direct a message at yourself', () => {
  assert.ok(!C.targetOrder().includes('op'));
});

check('a member joining does not renumber the ones already there', () => {
  const before = C.targetOrder().join(',').split(',');
  roster([['z9', 'Zed'], ['a1', 'Ada'], ['m5', 'Mo'], ['op', 'exampleuser'],
          ['w1', 'Wren']]);
  const after = C.targetOrder().join(',').split(',');
  // Wren sorts between Mo and Zed, so Zed's digit legitimately moves — but
  // everyone BEFORE the insertion point must keep theirs. That is the property
  // that makes the shortcut safe to use without looking.
  assert.strictEqual(after.slice(0, 2).join(','), before.slice(0, 2).join(','));
});

roster([['z9', 'Zed'], ['a1', 'Ada'], ['m5', 'Mo'], ['op', 'exampleuser']]);

check('toggling adds, then removes, the same target', () => {
  state.selectedTargets = new Set();
  C.toggleTarget('a1');
  assert.strictEqual(selected(), 'a1');
  C.toggleTarget('a1');
  assert.strictEqual(selected(), '');
});

check('Alt+A selects everyone addressable', () => {
  state.selectedTargets = new Set();
  C.toggleAllTargets();
  assert.strictEqual(selected(), 'a1,m5,z9');
});

check('Alt+A again returns to a broadcast rather than leaving all selected', () => {
  C.toggleAllTargets();
  assert.strictEqual(selected(), '',
    'a second Alt+A left every agent targeted — it must toggle');
});

check('Alt+0 clears a partial selection', () => {
  state.selectedTargets = new Set(['a1', 'm5']);
  C.clearTargets();
  assert.strictEqual(selected(), '');
});

check('the selection is persisted per conversation, not globally', () => {
  state.channel = 'one';
  state.selectedTargets = new Set();
  C.toggleTarget('a1');
  assert.strictEqual((state.targetDrafts['one'] || []).join(','), 'a1');
  state.channel = 'two';
  state.selectedTargets = new Set();
  C.toggleTarget('m5');
  assert.strictEqual((state.targetDrafts['two'] || []).join(','), 'm5');
  assert.strictEqual((state.targetDrafts['one'] || []).join(','), 'a1',
    "targeting in one conversation rewrote another's chips");
  state.channel = 'picker';
});

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
process.exit(failures.length ? 1 : 0);
