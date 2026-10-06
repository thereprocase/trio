'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(
  path.resolve(__dirname, '..', 'server', 'web', 'js', '41-gameboy-controls.js'),
  'utf8');
const context = {
  window: { Trio: {} },
  document: { documentElement: { dataset: { theme: 'light-1' } } },
  console,
};
vm.createContext(context);
vm.runInContext(source, context);

const controls = context.window.Trio.gameboyControls;
let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); }
  catch (error) { failures++; console.error(`FAIL: ${name}\n  ${error.message}`); }
}

const from = { left: 100, top: 100, width: 20, height: 20 };
const right = { left: 160, top: 103, width: 20, height: 20 };
const down = { left: 104, top: 180, width: 20, height: 20 };

check('Game Boy theme predicate is exact', () => {
  assert.strictEqual(controls.isGameboyTheme('historic-gameboy'), true);
  assert.strictEqual(controls.isGameboyTheme('historic-win98'), false);
});

check('controller exposes all eight physical actions', () => {
  assert.deepStrictEqual(
    Object.keys(controls.actionLabels).sort(),
    ['a', 'b', 'down', 'left', 'right', 'select', 'start', 'up']);
});

check('D-pad scoring accepts a candidate in the requested direction', () => {
  assert(Number.isFinite(controls.directionalScore(from, right, 'right')));
  assert(Number.isFinite(controls.directionalScore(from, down, 'down')));
});

check('D-pad scoring rejects candidates behind the requested direction', () => {
  assert.strictEqual(controls.directionalScore(from, right, 'left'), Infinity);
  assert.strictEqual(controls.directionalScore(from, down, 'up'), Infinity);
});

check('controller input is inert outside the Game Boy theme', () => {
  assert.strictEqual(controls.press('a'), false);
  assert.strictEqual(controls.press('down'), false);
});

if (failures) {
  console.error(`\nFAILED — ${failures} failure(s)`);
  process.exit(1);
}
console.log('\nOK — Game Boy controller geometry and action contract');
