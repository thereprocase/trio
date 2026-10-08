'use strict';

// A hub's default theme (NTH_APP_DEFAULT_THEME). nth_web writes it on
// <html data-theme data-default-theme>; tests/test-app-identity.py covers that
// half. This covers the browser half:
//   * no data-default-theme means today's default (light-1)
//   * a valid one is what a visitor with no saved theme gets, and is where the
//     light/dark toggle and "Reset to defaults" return
//   * an unknown one falls back to light-1
//   * a saved theme wins over the hub default
//   * saving an unrelated setting does not pin the theme, so a later change of
//     the hub default still reaches that visitor
//   * toggling to dark and back onto the hub default does not pin it either
//   * an old full save (every setting written on any save) is migrated once:
//     values equal to the old built-in defaults are dropped, choices kept
//   * a missing light/dark side is inferred from the saved theme's mode
//   * the head script that runs before first paint keeps the server-written
//     theme when nothing is saved and swaps in a saved one otherwise
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { load } = require('./dom-harness');

const KEY = 'trio.preferences.v1';
let failures = 0;
function check(name, fn) {
  try { fn(); console.log('PASS: ' + name); }
  catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); }
}

function boot({ hubTheme, saved } = {}) {
  const cx = load({ setup(win) {
    if (hubTheme !== undefined) win.document.documentElement.dataset.defaultTheme = hubTheme;
    if (saved !== undefined) win.localStorage.setItem(KEY, JSON.stringify(saved));
  } });
  const prefs = cx.hooks.Trio.preferences;
  prefs.apply();   // what preferences.mount() does at boot
  return { cx, prefs, root: cx.document.documentElement, storage: cx.window.localStorage };
}

check('no hub default: the built-in light-1 theme, as before', () => {
  const { prefs, root } = boot();
  assert.strictEqual(prefs.defaultTheme, 'light-1');
  assert.strictEqual(prefs.read().theme, 'light-1');
  assert.strictEqual(prefs.read().lightTheme, 'light-1');
  assert.strictEqual(prefs.read().darkTheme, 'dark-3');
  assert.strictEqual(root.dataset.theme, 'light-1');
});

check('a valid hub default is applied to a visitor with no saved theme', () => {
  const { prefs, root } = boot({ hubTheme: 'inspired-rescue' });
  assert.strictEqual(prefs.defaultTheme, 'inspired-rescue');
  assert.strictEqual(prefs.read().theme, 'inspired-rescue');
  assert.strictEqual(root.dataset.theme, 'inspired-rescue');
});

check('the light/dark toggle returns to the hub default', () => {
  const { prefs } = boot({ hubTheme: 'inspired-rescue' });
  assert.strictEqual(prefs.toggle().theme, 'dark-3');
  assert.strictEqual(prefs.toggle().theme, 'inspired-rescue');
});

check('a dark hub default becomes the dark side of the toggle', () => {
  const { prefs } = boot({ hubTheme: 'dark-1' });
  assert.strictEqual(prefs.read().theme, 'dark-1');
  assert.strictEqual(prefs.toggle().theme, 'light-1');
  assert.strictEqual(prefs.toggle().theme, 'dark-1');
});

check('an unknown hub default falls back to light-1', () => {
  const { prefs, root } = boot({ hubTheme: 'no-such-theme' });
  assert.strictEqual(prefs.defaultTheme, 'light-1');
  assert.strictEqual(prefs.read().theme, 'light-1');
  assert.strictEqual(root.dataset.theme, 'light-1');
});

check('a saved theme wins over the hub default', () => {
  const { prefs, root } = boot({ hubTheme: 'inspired-rescue', saved: { theme: 'dark-1', darkTheme: 'dark-1' } });
  assert.strictEqual(prefs.read().theme, 'dark-1');
  assert.strictEqual(root.dataset.theme, 'dark-1');
});

check('choosing a theme saves it, and it survives a reload', () => {
  const { prefs, storage } = boot({ hubTheme: 'inspired-rescue' });
  prefs.selectTheme('light-4');
  assert.strictEqual(JSON.parse(storage.getItem(KEY)).theme, 'light-4');
  prefs.apply();
  assert.strictEqual(prefs.read().theme, 'light-4');
});

check('saving an unrelated setting does not pin the theme', () => {
  const { prefs, storage } = boot({ hubTheme: 'inspired-rescue' });
  prefs.save({ compact: true });
  const stored = JSON.parse(storage.getItem(KEY));
  assert.deepStrictEqual(Object.keys(stored).sort(), ['compact', 'format']);
  assert.strictEqual(prefs.read().theme, 'inspired-rescue');
});

check('"Reset to defaults" returns to the hub default, not light-1', () => {
  const { prefs, root, storage } = boot({ hubTheme: 'inspired-rescue', saved: { theme: 'dark-2' } });
  assert.strictEqual(prefs.read().theme, 'dark-2');
  prefs.reset();
  assert.strictEqual(prefs.read().theme, 'inspired-rescue');
  assert.strictEqual(root.dataset.theme, 'inspired-rescue');
  assert.strictEqual(storage.getItem(KEY), null);
});

check('legacy bare "light"/"dark" values keep meaning light-1/dark-3 on any hub', () => {
  assert.strictEqual(boot({ hubTheme: 'inspired-rescue', saved: { theme: 'light' } }).prefs.read().theme, 'light-1');
  assert.strictEqual(boot({ hubTheme: 'dark-1', saved: { theme: 'dark' } }).prefs.read().theme, 'dark-3');
});

// The stored object a browser carries across page loads, as a reload sees it.
const storedOf = cx => JSON.parse(cx.storage.getItem(KEY));

check('toggling to dark and back does not pin the hub default', () => {
  const first = boot({ hubTheme: 'inspired-rescue' });
  assert.strictEqual(first.prefs.toggle().theme, 'dark-3');
  assert.strictEqual(first.prefs.toggle().theme, 'inspired-rescue');
  assert.ok(!('theme' in storedOf(first)), JSON.stringify(storedOf(first)));
  // The hub owner changes the default; the next load shows it.
  const later = boot({ hubTheme: 'light-4', saved: storedOf(first) });
  assert.strictEqual(later.prefs.read().theme, 'light-4');
  assert.strictEqual(later.root.dataset.theme, 'light-4');
});

check('the same holds on a hub with no default set', () => {
  const first = boot();
  first.prefs.toggle(); first.prefs.toggle();
  assert.strictEqual(first.prefs.read().theme, 'light-1');
  assert.strictEqual(boot({ hubTheme: 'inspired-rescue', saved: storedOf(first) }).prefs.read().theme, 'inspired-rescue');
});

check('toggling away from the hub default is still saved', () => {
  const first = boot({ hubTheme: 'inspired-rescue' });
  first.prefs.toggle();
  assert.strictEqual(storedOf(first).theme, 'dark-3');
  assert.strictEqual(boot({ hubTheme: 'light-4', saved: storedOf(first) }).prefs.read().theme, 'dark-3');
});

check('a theme picked on purpose stays pinned through a toggle round trip', () => {
  const first = boot({ hubTheme: 'inspired-rescue' });
  first.prefs.selectTheme('inspired-rescue');
  first.prefs.toggle(); first.prefs.toggle();
  assert.strictEqual(boot({ hubTheme: 'light-4', saved: storedOf(first) }).prefs.read().theme, 'inspired-rescue');
});

// What the old code wrote when the notification prompt was dismissed:
// every setting at its built-in default, with notifications switched off.
const OLD_FULL_SAVE = { theme: 'light-1', lightTheme: 'light-1', darkTheme: 'dark-3', font: 'default', compact: false, messageNumbers: false, notifications: false, chime: false, chimeVolume: 0.5, dictation: true, sttMode: 'local', staleThreadDays: 7, messageTimes: 'local',
  chimeTierDm: true, chimeTierMention: true, chimeTierRef: true, chimeTierPlain: false,
  notifyTierDm: true, notifyTierMention: true, notifyTierRef: false, notifyTierPlain: false,
  chimeSoundDm: 'alert', chimeSoundMention: 'ping', chimeSoundRef: 'tick', chimeSoundPlain: 'tick' };

check('an old full save is migrated: the unchosen theme goes, real settings stay', () => {
  const cx = boot({ hubTheme: 'inspired-rescue', saved: { ...OLD_FULL_SAVE, chime: true } });
  assert.strictEqual(cx.prefs.read().theme, 'inspired-rescue');
  assert.strictEqual(cx.root.dataset.theme, 'inspired-rescue');
  assert.deepStrictEqual(storedOf(cx), { format: 2, notifications: false, chime: true });
  assert.strictEqual(cx.prefs.read().notifications, false);
  assert.strictEqual(cx.prefs.read().chime, true);
});

check('an old full save keeps a theme that differs from the old default', () => {
  const cx = boot({ hubTheme: 'inspired-rescue', saved: { ...OLD_FULL_SAVE, theme: 'light-4', lightTheme: 'light-4' } });
  assert.strictEqual(cx.prefs.read().theme, 'light-4');
  assert.deepStrictEqual(storedOf(cx), { format: 2, theme: 'light-4', lightTheme: 'light-4', notifications: false });
});

check('a full save from before the newest settings existed is migrated too', () => {
  const older = { ...OLD_FULL_SAVE }; delete older.messageTimes; delete older.staleThreadDays;
  const cx = boot({ hubTheme: 'inspired-rescue', saved: older });
  assert.strictEqual(cx.prefs.read().theme, 'inspired-rescue');
  assert.deepStrictEqual(storedOf(cx), { format: 2, notifications: false });
});

check('the migration runs once: a marked object is left as it is', () => {
  const marked = { format: 2, theme: 'light-1', lightTheme: 'light-1', darkTheme: 'dark-3' };
  const cx = boot({ hubTheme: 'inspired-rescue', saved: marked });
  assert.strictEqual(cx.prefs.read().theme, 'light-1');
  assert.deepStrictEqual(storedOf(cx), marked);
});

check('a light theme left by the toggle is remembered across a reload', () => {
  const first = boot({ saved: { format: 2, theme: 'inspired-rescue' } });
  first.prefs.toggle();
  const reloaded = boot({ saved: storedOf(first) });
  assert.strictEqual(reloaded.prefs.read().theme, 'dark-3');
  assert.strictEqual(reloaded.prefs.toggle().theme, 'inspired-rescue');
});

check('a missing dark side is inferred from the saved theme\'s mode', () => {
  const cx = boot({ saved: { format: 2, theme: 'inspired-trailhead' } });
  assert.strictEqual(cx.prefs.read().darkTheme, 'inspired-trailhead');
  assert.strictEqual(cx.prefs.read().lightTheme, 'light-1');
});

check('a missing light side is inferred from the saved theme\'s mode', () => {
  const cx = boot({ saved: { format: 2, theme: 'inspired-rescue' } });
  assert.strictEqual(cx.prefs.read().lightTheme, 'inspired-rescue');
  cx.prefs.toggle();
  assert.strictEqual(cx.prefs.toggle().theme, 'inspired-rescue');
});

// The head script runs before first paint, before any module. Run the real
// one out of index.html against an <html> as nth_web serves it.
const indexHtml = fs.readFileSync(path.resolve(__dirname, '..', 'server', 'web', 'index.html'), 'utf8');
const headScript = indexHtml.split('</head>')[0].match(/<script>([\s\S]*?)<\/script>/)[1];
function firstPaint(saved, hubTheme = 'inspired-rescue') {
  const store = new Map(saved === undefined ? [] : [[KEY, JSON.stringify(saved)]]);
  const documentElement = { dataset: { theme: hubTheme, defaultTheme: hubTheme } };
  vm.runInNewContext(headScript, {
    document: { documentElement },
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null) },
  });
  return documentElement.dataset.theme;
}

check('first paint keeps the hub default when nothing is saved', () => {
  assert.strictEqual(firstPaint(), 'inspired-rescue');
});
check('first paint keeps the hub default when only other settings are saved', () => {
  assert.strictEqual(firstPaint({ compact: true }), 'inspired-rescue');
});
check('first paint already shows a saved theme', () => {
  assert.strictEqual(firstPaint({ theme: 'dark-4' }), 'dark-4');
});
check('first paint maps the legacy bare "light"/"dark" values like the booted page', () => {
  assert.strictEqual(firstPaint({ theme: 'light' }), 'light-1');
  assert.strictEqual(firstPaint({ theme: 'dark' }), 'dark-3');
  assert.strictEqual(boot({ hubTheme: 'inspired-rescue', saved: { theme: 'light' } }).root.dataset.theme, 'light-1');
  assert.strictEqual(boot({ hubTheme: 'inspired-rescue', saved: { theme: 'dark' } }).root.dataset.theme, 'dark-3');
});
check('first paint skips the unchosen light-1 of an old full save, as boot does', () => {
  assert.strictEqual(firstPaint(OLD_FULL_SAVE), 'inspired-rescue');
  assert.strictEqual(boot({ hubTheme: 'inspired-rescue', saved: OLD_FULL_SAVE }).root.dataset.theme, 'inspired-rescue');
});
check('first paint keeps a real choice from an old full save', () => {
  assert.strictEqual(firstPaint({ ...OLD_FULL_SAVE, theme: 'dark-1', darkTheme: 'dark-1' }), 'dark-1');
});
check('first paint keeps a deliberately saved light-1 once the object is marked', () => {
  assert.strictEqual(firstPaint({ format: 2, theme: 'light-1' }), 'light-1');
});

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nAll hub default theme checks passed');
