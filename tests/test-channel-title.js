// The channel title, and the tooltip that makes its truncation survivable.
//
// CSS ellipses #h-channel so a long channel code cannot wrap and inflate the
// fixed header — measured between 881 and 1040px, a code with no hyphen has no
// break opportunity and paints across the connection pill. Ellipsing fixes the
// overflow and creates a second problem: on the one surface where the sidebar
// is hidden, the title is the only thing naming the room, and a truncated stub
// with nothing to hover is unreadable rather than merely tight.
//
// The tooltip is therefore a hard companion to the ellipsis, not a nicety.
//
// The trap this file exists for: #h-channel used to have its text written from
// FOUR separate call sites — boot, the channel load, the DM load, and the
// topbar update. Adding a tooltip to one of them is worse than adding none,
// because it makes the truncation look handled on whichever path happens to be
// checked while the others still show an undiscoverable stub. So every path
// gets its own assertion, and the setter is asserted to be the single writer.
//
// Usage: node tests/test-channel-title.js
'use strict';

const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log('PASS: ' + name); }
  else { failures.push(name); console.log('FAIL: ' + name); }
}

const cx = load();
const Trio = cx.hooks.Trio;
const state = Trio.state;
const doc = cx.document;
cx.context.history = { pushState() {}, replaceState() {} };
Trio.startEvents = () => {};
Trio.stopEvents = () => {};

const h = () => doc.getElementById('h-channel');
// A 32-char code: nth_server caps channel codes there, so this is the longest
// real name the header can be asked to show, and the case the ellipsis exists
// for. No hyphen, because a code with no break opportunity is the one that
// paints across the connection pill rather than wrapping.
const LONGEST = 'abcdefghijklmnopqrstuvwxyz012345';

// ── the setter itself ───────────────────────────────────────────────────────
Trio.setChannelTitle('#short');
check('the setter writes the visible text', h().textContent === '#short');
check('and the tooltip carries the same name', h().title === '#short');

Trio.setChannelTitle(LONGEST);
check('a full-length code is recoverable from the tooltip', h().title === LONGEST);
// Read defensively: if the tooltip is ever dropped entirely, `title` is
// undefined and a bare `.length` throws — which aborts the run and hides every
// assertion after it. A failing check is a better diagnostic than a stack trace.
check('...and is not itself truncated in the attribute', String(h().title ?? '').length === 32);

Trio.setChannelTitle('');
check('an empty name falls back to the product name', h().textContent === 'nth');
check('and the tooltip agrees rather than going blank', h().title === 'nth');

// ── every path that paints the title ────────────────────────────────────────
// One assertion per call site. A tooltip on only one path is the defect this
// file is here to prevent, so these are deliberately not collapsed.

// 1. boot, before any route resolves.
state.channel = LONGEST;
Trio.api.get = async () => ({ operator: { id: 'op', name: 'op', source: 'loopback' }, channel: LONGEST });
(async () => {
  await Trio.boot(() => {});
  check('boot paints a tooltip', h().title === '#' + LONGEST);

  // 2. the channel load path.
  Trio.workspace.mount();
  state.channel = '';
  Trio.workspace.openChannel(LONGEST);
  check('opening a channel paints a tooltip', h().title === '#' + LONGEST);

  // 3. the DM load path. Asserted as the invariant rather than a literal:
  // a DM titles from its display name, not its key, and pinning the exact
  // string here would be testing the product decision instead of the tooltip.
  // What must hold on every path is that the tooltip carries whatever the
  // truncated element is showing.
  Trio.workspace.openDm({ key: 'a-very-long-dm-key-that-truncates',
                          name: 'a-very-long-display-name-that-truncates', member_ids: [] });
  check('opening a DM paints a tooltip', !!h().title);
  check('...and it matches the name being shown', h().title === h().textContent);

  // 4. the topbar update path.
  Trio.workspace.updateTopbar?.('#' + LONGEST, 'subtitle');
  check('the topbar update paints a tooltip', h().title === h().textContent);
  Trio.workspace.unmount();

  // ── nobody writes the element behind the setter's back ────────────────────
  // Pinned by source: a future path that sets textContent directly would paint
  // a name with a stale tooltip from whatever was shown before — worse than no
  // tooltip, because it would be confidently wrong.
  const fs = require('fs'), path = require('path');
  const dir = path.join(__dirname, '..', 'server', 'web', 'js');
  const offenders = fs.readdirSync(dir)
    .filter(f => f.endsWith('.js'))
    .flatMap(f => fs.readFileSync(path.join(dir, f), 'utf8')
      .split('\n')
      .map((line, i) => ({ f, n: i + 1, line }))
      .filter(({ line }) => /getElementById\(['"]h-channel['"]\)\s*\.textContent\s*=/.test(line)
                         || /\$\(['"]h-channel['"]\)[^;]*\.textContent\s*=/.test(line)));
  check('no module writes #h-channel.textContent directly any more',
        offenders.length === 0
        || (console.log('    offenders: ' + offenders.map(o => `${o.f}:${o.n}`).join(', ')), false));

  console.log();
  if (failures.length) {
    console.log(`FAILED — ${failures.length} of ${failures.length + passed}`);
    failures.forEach(f => console.log('  - ' + f));
    process.exit(1);
  }
  console.log(`OK — ${passed} passed`);
})();
