// Client-side tests for the shared image lightbox (14-lightbox.js), run against
// the ACTUAL shipped module via the Node DOM harness.
//
// 14-lightbox.js was the last DOM-coupled production module absent from the
// harness module list, so Trio.lightbox had no coverage. The module is lazy
// (its IIFE only publishes Trio.lightbox.open; the <dialog> is built on first
// open), so registering it is side-effect-free.
//
// What the harness CAN exercise (no layout, no real <dialog> semantics):
//   • open() builds the dialog, sets the image src/alt, and toggles the
//     nav/counter chrome for single vs. multi-image galleries,
//   • the {url}-filter + single-object convenience form + startIndex clamping,
//   • that an empty / url-less list is a safe no-op.
//
// NOT covered (deliberate harness gaps): zoom/pan transforms, wheel/pointer
// gestures, and native <dialog> showModal/backdrop/Escape behaviour — the fake
// DOM stubs showModal() and never dispatches events.
//
// Usage: node tests/test-lightbox.js
'use strict';

const assert = require('assert');
const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + e.message); }
}

const cx = load();
const H = cx.hooks;
const doc = cx.document;
const lb = H.lightbox;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

// The lightbox chrome lives inside the #trio-lightbox dialog; query it back out
// by class to inspect what open()/show() rendered.
const dialog = () => doc.getElementById('trio-lightbox');
const img = () => dialog().querySelector('.lightbox-img');
const counter = () => dialog().querySelector('.lightbox-counter');
const prev = () => dialog().querySelector('.lightbox-prev');
const next = () => dialog().querySelector('.lightbox-next');

check('module publishes Trio.lightbox.open', () => {
  assert.ok(lb, 'Trio.lightbox missing — 14-lightbox.js did not load');
  assert.strictEqual(typeof lb.open, 'function');
});

check('open() with a single image sets src/alt and hides gallery chrome', () => {
  lb.open([{ url: 'https://x/one.png', alt: 'first' }]);
  assert.strictEqual(img().src, 'https://x/one.png');
  assert.strictEqual(img().alt, 'first');
  assert.strictEqual(prev().hidden, true, 'prev arrow hidden for a single image');
  assert.strictEqual(next().hidden, true, 'next arrow hidden for a single image');
  assert.strictEqual(counter().hidden, true, 'counter hidden for a single image');
});

check('open() accepts a bare object (not just an array)', () => {
  lb.open({ url: 'https://x/solo.png', alt: 'solo' });
  assert.strictEqual(img().src, 'https://x/solo.png');
  assert.strictEqual(img().alt, 'solo');
});

check('open() with multiple images shows nav + counter at startIndex', () => {
  lb.open([
    { url: 'https://x/a.png', alt: 'a' },
    { url: 'https://x/b.png', alt: 'b' },
    { url: 'https://x/c.png', alt: 'c' },
  ], 1);
  assert.strictEqual(img().src, 'https://x/b.png', 'startIndex 1 → second image');
  assert.strictEqual(prev().hidden, false);
  assert.strictEqual(next().hidden, false);
  assert.strictEqual(counter().hidden, false);
  assert.strictEqual(counter().textContent, '2 / 3');
});

check('open() clamps an out-of-range startIndex', () => {
  lb.open([{ url: 'https://x/a.png' }, { url: 'https://x/b.png' }], 99);
  assert.strictEqual(img().src, 'https://x/b.png', 'startIndex past the end clamps to last');
  assert.strictEqual(counter().textContent, '2 / 2');
});

check('open() drops url-less items before counting', () => {
  lb.open([{ url: 'https://x/a.png' }, { alt: 'no url' }, { url: 'https://x/b.png' }]);
  assert.strictEqual(counter().textContent, '1 / 2', 'the url-less middle item is filtered out');
});

check('open() with an empty / url-less list is a safe no-op', () => {
  // Should not throw and should not blow away the previously-shown image.
  const before = img().src;
  assert.doesNotThrow(() => lb.open([]));
  assert.doesNotThrow(() => lb.open([{ alt: 'still no url' }]));
  assert.strictEqual(img().src, before, 'a no-op open leaves the current view untouched');
});

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) { console.error('FAILURES: ' + failures.join(', ')); process.exit(1); }
