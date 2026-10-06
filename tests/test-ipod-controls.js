'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(
  path.resolve(__dirname, '..', 'server', 'web', 'js', '42-ipod-controls.js'),
  'utf8');
const calls = [];
const shared = {
  back: () => { calls.push('back'); return true; },
  move: direction => { calls.push(`move:${direction}`); return true; },
  start: () => { calls.push('start'); return true; },
  activate: () => { calls.push('activate'); return true; },
};
const context = {
  window: { Trio: { gameboyControls: shared } },
  document: { documentElement: { dataset: { theme: 'light-1' } } },
  console,
};
vm.createContext(context);
vm.runInContext(source, context);

const controls = context.window.Trio.ipodControls;
let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); }
  catch (error) { failures++; console.error(`FAIL: ${name}\n  ${error.message}`); }
}

check('Now Playing theme predicate is exact', () => {
  assert.strictEqual(controls.isIpodTheme('inspired-ipod'), true);
  assert.strictEqual(controls.isIpodTheme('historic-gameboy'), false);
});

check('click wheel exposes its five physical controls plus rotation', () => {
  assert.deepStrictEqual(
    Object.keys(controls.actionLabels).sort(),
    ['menu', 'next', 'play', 'previous', 'select', 'wheel']);
});

check('angle wrap-around stays a small rotation instead of jumping a lap', () => {
  assert.strictEqual(controls.normalizedDelta(-175, 175), 10);
  assert.strictEqual(controls.normalizedDelta(175, -175), -10);
});

check('controller input is inert outside Now Playing', () => {
  assert.strictEqual(controls.navigate('select'), false);
  assert.deepStrictEqual(calls, []);
});

check('physical controls dispatch to the shared workspace navigator', () => {
  context.document.documentElement.dataset.theme = 'inspired-ipod';
  ['menu', 'previous', 'next', 'play', 'select', 'up', 'down'].forEach(controls.navigate);
  assert.deepStrictEqual(calls, ['back', 'move:left', 'move:right', 'start', 'activate', 'move:up', 'move:down']);
});

if (failures) {
  console.error(`\nFAILED — ${failures} failure(s)`);
  process.exit(1);
}
console.log('\nOK — iPod click wheel geometry and action contract');
