'use strict';

// Regression: preferences.reset() used to call apply() only, without
// dispatching 'preferences:changed'. Listeners (composer dictation visibility,
// notification wiring) didn't react on reset until the next save(). The fix
// makes reset() dispatch the same event save() does.
const assert = require('assert');
const { load } = require('./dom-harness');

const cx = load();
const H = cx.hooks;
let failures = 0;
function check(name, fn) { try { fn(); console.log('PASS: ' + name); } catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); } }

check('reset() dispatches preferences:changed', () => {
  let fired = 0;
  let detail = null;
  H.Trio.events.addEventListener('preferences:changed', (e) => { fired++; detail = e.detail; });
  H.Trio.preferences.reset();
  assert.strictEqual(fired, 1, 'preferences:changed fired exactly once on reset');
  assert.ok(detail, 'event carried a detail payload');
});

check('save() still dispatches preferences:changed (no regression)', () => {
  let fired = 0;
  H.Trio.events.addEventListener('preferences:changed', () => { fired++; });
  H.Trio.preferences.save({ compact: true });
  assert.strictEqual(fired, 1, 'preferences:changed fired once on save');
});

check('reset() clears stored preferences back to defaults', () => {
  H.Trio.preferences.save({ compact: true, messageNumbers: true });
  const before = H.Trio.preferences.read();
  assert.ok(before.compact === true, 'save set compact=true');
  H.Trio.preferences.reset();
  const after = H.Trio.preferences.read();
  assert.strictEqual(after.compact, false, 'reset restored compact default (false)');
  assert.strictEqual(after.messageNumbers, false, 'reset restored messageNumbers default (false)');
});

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nAll preferences.reset checks passed');
