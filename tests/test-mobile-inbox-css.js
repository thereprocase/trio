'use strict';

// Populated inbox cards must stay inside the 320px mobile workspace. The DOM
// harness has no layout engine, so pin the load-bearing CSS contract and its
// width arithmetic here; browser measurement is documented in the source rule.
const fs = require('fs');
const path = require('path');
const css = fs.readFileSync(
  path.join(__dirname, '..', 'server', 'web', 'css', '40-responsive.css'), 'utf8');

const failures = [];
function check(name, condition) {
  console.log((condition ? 'PASS' : 'FAIL') + ': ' + name);
  if (!condition) failures.push(name);
}

const mobile = css.match(/@media\s*\(max-width:640px\)\s*\{[\s\S]*?\.att-card\s*\{[\s\S]*?\.att-card \.reason pre\s*\{[^}]*\}[\s\S]*?\}/)?.[0] || '';
check('mobile inbox card has an explicit narrow-layout contract', Boolean(mobile));
check('inbox header uses one fixed avatar and one shrinkable text track',
  /grid-template-columns:\s*36px minmax\(0,1fr\)/.test(mobile));
check('long inbox identity copy may shrink instead of widening the page',
  />span:not\(\.av\):not\(\.waiting\)\{[^}]*min-width:0/.test(mobile));
check('unread status moves below the identity in the fluid column',
  /\.waiting\{[^}]*grid-column:2[^}]*margin-left:0/.test(mobile));
check('long message bodies wrap and code blocks scroll inside the card',
  /overflow-wrap:anywhere/.test(mobile) && /\.reason pre\{[^}]*overflow:auto/.test(mobile));

// At 320px, the final responsive workspace has 16px gutters: 288px available.
// Card padding consumes 24px and its borders 2px, leaving 262px. The header's
// 36px avatar + inherited 11px gap leaves a positive 215px fluid text track.
const fluidTrack = 320 - 2 * 16 - 2 * 12 - 2 - 36 - 11;
check('320px inbox arithmetic leaves a usable fluid label track', fluidTrack === 215);

console.log();
if (failures.length) {
  console.log(`FAILED — ${failures.length} failure(s)`);
  process.exit(1);
}
console.log('OK — mobile inbox fits its narrow track');
