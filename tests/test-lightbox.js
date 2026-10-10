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

function pointer(type,id,x,y) {
  for (const fn of img()._listeners[type] || []) fn({pointerId:id,pointerType:'touch',clientX:x,clientY:y});
}
check('pinch zoom anchors at fingers and transitions smoothly to one-finger pan', () => {
  lb.open([{url:'https://x/pinch.png'}]);
  img().getBoundingClientRect=()=>({left:0,top:0,width:200,height:200});
  pointer('pointerdown',1,50,100);pointer('pointerdown',2,150,100);
  pointer('pointermove',2,250,100);
  assert.strictEqual(img().style.transform,'translate(50px,0px) scale(2)');
  pointer('pointerup',2,250,100);pointer('pointermove',1,70,100);
  assert.strictEqual(img().style.transform,'translate(70px,0px) scale(2)');
  pointer('pointerup',1,70,100);
  img()._listeners.click[0]();
  assert.strictEqual(img().style.transform,'translate(70px,0px) scale(2)','pinch release must not toggle zoom');
});
check('pinch limits scale and resets gesture on image change', () => {
  lb.open([{url:'https://x/a.png'},{url:'https://x/b.png'}]);
  pointer('pointerdown',1,50,100);pointer('pointerdown',2,150,100);
  pointer('pointermove',2,2050,100);assert.ok(img().style.transform.endsWith('scale(6)'));
  pointer('pointermove',2,51,100);assert.strictEqual(img().style.transform,'translate(0px,0px) scale(1)');
  next()._listeners.click[0]();pointer('pointermove',1,500,500);
  assert.strictEqual(img().style.transform,'translate(0px,0px) scale(1)');
});
check('cancel releases gesture state', () => {
  pointer('pointerdown',1,50,100);pointer('pointerdown',2,150,100);
  pointer('pointercancel',1,50,100);pointer('pointercancel',2,150,100);
  const before=img().style.transform;pointer('pointermove',1,400,400);
  assert.strictEqual(img().style.transform,before);assert.ok(!img().classList.contains('dragging'));
});

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) { console.error('FAILURES: ' + failures.join(', ')); process.exit(1); }
