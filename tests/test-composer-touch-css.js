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
  /@media\s*\(hover:none\)\s*and\s*\(pointer:coarse\)\s*\{[\s\S]*?\.composer-shell\s+\.composer-input(?:\s*,[^{}]+)?\s*\{\s*font-size:16px;\s*\}[\s\S]*?\}/);
check('touch-primary composer uses the 16px iOS focus-zoom threshold',
      Boolean(touchRule));
check('composer protection is capability-based, not capped at the 880px drawer breakpoint',
      !/@media\s*\(max-width:880px\)\s*\{\s*\.composer-shell\s+\.composer-input/.test(css));
// The mic is tapped again, without looking, to stop recording; at 34px it was
// under a fingertip's ~44px on exactly the devices that dictate most.
check('touch-primary composer tools (mic, attach) are 44px tap targets',
      /@media\s*\(hover:none\)\s*and\s*\(pointer:coarse\)\s*\{[^@]*\.composer-actions\s+\.comp-tool\s*\{\s*width:44px;\s*height:44px;\s*\}/.test(css));

console.log();
if (failures.length) {
  console.log(`FAILED — ${failures.length} failure(s)`);
  process.exit(1);
}
console.log('OK — 3 passed');
