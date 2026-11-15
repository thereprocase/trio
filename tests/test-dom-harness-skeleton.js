// The DOM harness builds skeleton elements from server/web/index.html. These
// checks keep it from masking missing production markup.
// Usage: node tests/test-dom-harness-skeleton.js
'use strict';
const assert = require('assert');
const { load, skeletonForTest } = require('./dom-harness');

const doc = () => load().document;
let passed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('PASS: ' + name); }
  finally { skeletonForTest.reset(); }
}

check('production pin button carries its static count child', () => {
  const button = doc().getElementById('channel-pins-button');
  assert.strictEqual(button.tagName, 'BUTTON', 'real tag, not a stand-in div');
  assert.ok(button.querySelector('.channel-pin-count'), 'count span read from index.html');
});

check('a missing skeleton element fails on every lookup', () => {
  skeletonForTest.use('<main><span class="channel-pin-count"></span></main>');
  const d = doc();
  assert.throws(() => d.getElementById('channel-pins-button'), /no element with id "channel-pins-button"/);
  assert.throws(() => d.getElementById('channel-pins-button'), /no element with id "channel-pins-button"/,
    'second lookup must not return a cached stand-in');
});

check('a missing static child is visible to the client', () => {
  skeletonForTest.use('<button id="channel-pins-button" type="button"><svg></svg></button>');
  const button = doc().getElementById('channel-pins-button');
  assert.strictEqual(button.querySelector('.channel-pin-count'), null);
});

check('single-quoted ids and id-like attributes', () => {
  skeletonForTest.use("<span data-id='channel-pins-button'>decoy</span>"
    + "<button id='channel-pins-button'><span class='channel-pin-count' id='pin-count-demo'>3</span></button>");
  const d = doc(), button = d.getElementById('channel-pins-button');
  assert.strictEqual(button.tagName, 'BUTTON', 'data-id must not match');
  assert.strictEqual(button.querySelector('.channel-pin-count').textContent, '3');
  assert.strictEqual(d.getElementById('pin-count-demo'), button.querySelector('.channel-pin-count'),
    'static descendants resolve to the same node');
});

console.log(passed + ' harness skeleton checks passed');
