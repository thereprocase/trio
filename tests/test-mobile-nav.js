// The mobile navigation drawer.
//
// Below 880px the sidebar becomes an off-canvas overlay, and it was only ever
// moved out of sight by a transform. That hides it from the eye and from nobody
// else: its ~20 controls stayed in the tab order and the accessibility tree, so
// a keyboard or screen-reader user walked the brand, every channel, every DM,
// the add buttons and the account trigger before reaching the conversation.
// Nothing closed it either, so choosing a channel left the drawer covering the
// thing you had just chosen, dismissable only by finding the strip of scrim.
//
// The rules pinned here:
//   * closed + narrow means `inert` — one attribute that removes the subtree
//     from focus, hit-testing and the a11y tree together. Never `aria-hidden`,
//     which would leave the controls focusable while claiming they do not exist
//   * `inert` must never survive a return to desktop, where the sidebar is
//     permanent — that would make the whole navigation dead
//   * choosing any destination closes the drawer
//   * focus must leave the drawer as it closes, because the control you clicked
//     is inside it and the browser would otherwise drop focus on <body>
//
// 🔴 Two harness gaps make the obvious version of this file a false green:
// dom-harness stubs `matchMedia` to a permanent `{ matches: false }`, and
// `focus()` is a no-op with `activeElement` permanently null. So viewport is
// passed as an ARGUMENT rather than read internally, and focus is asserted by
// recording which element was ASKED to take it. That proves the contract this
// code controls; it cannot prove the browser honoured the request.
//
// Usage: node tests/test-mobile-nav.js
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
const doc = cx.document;
const nav = Trio.nav;

// The client calls history.pushState through the router on navigation; the
// harness has no History API.
cx.context.history = { pushState() {}, replaceState() {} };

const aside = doc.getElementById('sidebar');
const rail = doc.getElementById('workspace-rail');
const toggle = doc.getElementById('nav-toggle');
const scrim = doc.getElementById('scrim-nav');
aside.append(rail);

// Record focus requests instead of trusting the no-op stub.
let focused = null;
const track = (el, label) => { el.focus = () => { focused = label; }; return el; };
track(toggle, 'toggle');
// #sidebar-toggle is the aside's FIRST button and is display:none below 880px,
// so a naive "first focusable in the aside" lands on something invisible and
// focus() silently does nothing. Reproduced here: it sits ahead of the rail.
const collapseBtn = doc.createElement('button');
collapseBtn.id = 'sidebar-toggle';
aside.append(collapseBtn);
track(collapseBtn, 'sidebar-toggle');
aside.append(rail);
// A rail control to stand in for "the channel row you just tapped".
const railButton = doc.createElement('button');
railButton.className = 'nav-item';
rail.append(railButton);
track(railButton, 'rail');

const inert = () => aside.getAttribute('inert') !== null && aside.getAttribute('inert') !== undefined;
const expanded = () => toggle.getAttribute('aria-expanded');
const reset = narrow => { nav.close({ narrow }); nav.sync(narrow); focused = null; };

// ── the closed drawer is genuinely closed ───────────────────────────────────
reset(true);
check('closed on a narrow viewport is inert', inert());
check('closed reports aria-expanded=false', expanded() === 'false');
check('the scrim is hidden while closed', scrim.hidden === true);

// ── desktop must never be inert ─────────────────────────────────────────────
reset(false);
check('the permanent desktop sidebar is never inert', !inert());
// The transition that would otherwise strand a desktop user in a dead sidebar.
reset(true);
check('...and inert is cleared when the viewport grows back',
      (nav.sync(false), !inert()));

// ── opening ─────────────────────────────────────────────────────────────────
reset(true);
nav.open(true);
check('opening clears inert so the drawer can be used', !inert());
check('opening reports aria-expanded=true', expanded() === 'true');
check('opening reveals the scrim', scrim.hidden === false);
check('opening moves focus INTO the drawer, not onto the toggle', focused === 'rail');
check('...and specifically not onto #sidebar-toggle, which is hidden below 880px',
      focused !== 'sidebar-toggle');
check('the open drawer carries the nav-open class', nav.isOpen() === true);

// The rail precedes the hamburger in the document, so this is the assertion
// that matters: without it, Tab from the toggle goes forward into the header
// actions and the newly visible panel is only reachable by tabbing backwards.
focused = null;
nav.open(true);
check('opening an already-open drawer does nothing', focused === null);

// ── closing ─────────────────────────────────────────────────────────────────
reset(true);
nav.open(true);
focused = null;
nav.close({ narrow: true });
check('closing restores inert', inert());
check('closing resets aria-expanded', expanded() === 'false');
check('closing hides the scrim', scrim.hidden === true);
check('closing an already-closed drawer is a no-op',
      (focused = null, nav.close({ narrow: true }), focused === null));

// ── Escape hands focus back ─────────────────────────────────────────────────
reset(true);
nav.open(true);
focused = null;
nav.close({ restoreFocus: true, narrow: true });
check('an Escape dismissal returns focus to the control that opened it',
      focused === 'toggle');

// ── focus must not be stranded in a closing drawer ──────────────────────────
// This is the tap-a-channel case: the clicked row lives inside the drawer, so
// when the drawer goes away the browser drops focus on <body> unless we move
// it. Mandatory, and independent of restoreFocus.
reset(true);
nav.open(true);
doc.activeElement = railButton;
focused = null;
nav.close({ narrow: true });
check('focus inside the closing drawer is moved out even without restoreFocus',
      focused === 'toggle');
doc.activeElement = null;

// ── growing to desktop must not strand focus ────────────────────────────────
// Crossing to wide turns the drawer back into the permanent sidebar: its
// contents stay visible and usable. Handing focus back to #nav-toggle there
// would park it on a control that is `display:none` above 880px (`.hamb`),
// which is worse than leaving the user where they already were.
reset(true);
nav.open(true);
doc.activeElement = railButton;
focused = null;
nav.close({ narrow: false, preserveFocus: true });
check('growing to desktop leaves focus in the now-permanent sidebar',
      focused === null);
check('...and still clears the overlay state it left behind',
      !inert() && expanded() === 'false' && scrim.hidden === true);
doc.activeElement = null;

// ── choosing a destination closes it ────────────────────────────────────────
Trio.state.channels = [];
reset(true);
nav.open(true);
Trio.workspace.openChannel('somewhere');
check('opening a channel closes the drawer', nav.isOpen() === false);

reset(true);
nav.open(true);
Trio.workspace.openDm({ key: 'k1', name: 'someone', member_ids: [] });
check('opening a DM closes the drawer', nav.isOpen() === false);

// ── the markup carries truthful pre-boot semantics ──────────────────────────
// aria-expanded/aria-controls are static in index.html so the button is honest
// before any script runs, and the aside needs the id for aria-controls to point
// at something.
const html = require('fs').readFileSync(
  require('path').join(__dirname, '..', 'server', 'web', 'index.html'), 'utf8');
check('the toggle declares aria-controls in the markup',
      /id="nav-toggle"[^>]*aria-controls="sidebar"/.test(html));
check('the toggle declares a closed aria-expanded before boot',
      /id="nav-toggle"[^>]*aria-expanded="false"/.test(html));
check('the sidebar carries the id aria-controls points at',
      /<aside[^>]*id="sidebar"/.test(html));
// aria-hidden on a focusable subtree is the combination the ARIA spec calls
// broken; inert is what this code uses instead. Pinned so it is not "helpfully"
// added later.
check('the drawer is never given aria-hidden',
      aside.getAttribute('aria-hidden') == null);

console.log();
if (failures.length) {
  console.log(`FAILED — ${failures.length} of ${failures.length + passed}`);
  failures.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log(`OK — ${passed} passed`);
