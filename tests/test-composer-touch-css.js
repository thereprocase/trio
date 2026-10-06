'use strict';

const fs = require('fs');
const path = require('path');
const css = fs.readFileSync(
  path.join(__dirname, '..', 'server', 'web', 'css', '40-responsive.css'), 'utf8');

const failures = [];
function check(name, condition) {
  console.log((condition ? 'PASS' : 'FAIL') + ': ' + name);
  if (!condition) failures.push(name);
}

const touchRule = css.match(
  /@media\s*\(hover:none\)\s*and\s*\(pointer:coarse\)\s*\{[\s\S]*?\.composer-shell\s+\.composer-input\s*\{\s*font-size:16px;\s*\}[\s\S]*?\}/);
check('touch-primary composer uses the 16px iOS focus-zoom threshold',
      Boolean(touchRule));
check('composer protection is capability-based, not capped at the 880px drawer breakpoint',
      !/@media\s*\(max-width:880px\)\s*\{\s*\.composer-shell\s+\.composer-input/.test(css));

console.log();
if (failures.length) {
  console.log(`FAILED — ${failures.length} failure(s)`);
  process.exit(1);
}
console.log('OK — 2 passed');
