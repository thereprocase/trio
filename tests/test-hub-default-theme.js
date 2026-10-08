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
  assert.deepStrictEqual(Object.keys(stored), ['compact']);
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

// The head script runs before first paint, before any module. Run the real
// one out of index.html against an <html> as nth_web serves it.
const indexHtml = fs.readFileSync(path.resolve(__dirname, '..', 'server', 'web', 'index.html'), 'utf8');
const headScript = indexHtml.split('</head>')[0].match(/<script>([\s\S]*?)<\/script>/)[1];
function firstPaint(saved) {
  const store = new Map(saved === undefined ? [] : [[KEY, JSON.stringify(saved)]]);
  const documentElement = { dataset: { theme: 'inspired-rescue', defaultTheme: 'inspired-rescue' } };
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

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nAll hub default theme checks passed');
