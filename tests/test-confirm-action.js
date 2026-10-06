'use strict';

// Regression for the broken confirm flow: confirmAction was two-arg, but a
// caller passed (message, description, callback), so the description string
// was bound as `action` and the real callback was discarded — clicking Confirm
// evaluated `'text'?.()` (TypeError) and the action never ran. The helper now
// accepts an optional description.
const assert = require('assert');
const { load } = require('./dom-harness');

const cx = load();
const H = cx.hooks;
let failures = 0;
function check(name, fn) { try { fn(); console.log('PASS: ' + name); } catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); } }

function fireClose(node) {
  // The harness records listeners but never dispatches; fire 'close' by hand.
  const fns = (node._listeners && node._listeners.close) || [];
  for (const fn of fns) fn();
}

check('3-arg confirmAction renders description and runs the real callback', () => {
  let called = 0;
  H.Trio.ui.confirmAction('Archive #foo?', 'The channel will be archived and become read-only.', () => { called++; });
  const node = cx.document.getElementById('trio-control-modal');
  assert.ok(node, 'modal node created');
  // Description rendered as a body line, not bound as the action.
  assert.ok(node.innerHTML.includes('Archive #foo?'), 'prompt text present in modal body');
  assert.ok(node.innerHTML.includes('archived and become read-only'), 'description text present in modal body');
  // Simulate the user clicking Confirm (dialog returnValue 'default').
  node.returnValue = 'default';
  fireClose(node);
  assert.strictEqual(called, 1, 'callback fired exactly once on confirm');
});

check('2-arg confirmAction still works (no description, callback runs)', () => {
  let called = 0;
  H.Trio.ui.confirmAction('Archive this?', () => { called++; });
  const node = cx.document.getElementById('trio-control-modal');
  assert.ok(node.innerHTML.includes('Archive this?'), 'prompt text present');
  assert.ok(!node.innerHTML.includes('modal-desc'), 'no description block for 2-arg call');
  node.returnValue = 'default';
  fireClose(node);
  assert.strictEqual(called, 1, 'callback fired exactly once on confirm');
});

check('cancel does not run the callback', () => {
  let called = 0;
  H.Trio.ui.confirmAction('Sure?', 'desc', () => { called++; });
  const node = cx.document.getElementById('trio-control-modal');
  node.returnValue = 'cancel';
  fireClose(node);
  assert.strictEqual(called, 0, 'callback not fired on cancel');
});

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nAll confirmAction checks passed');
