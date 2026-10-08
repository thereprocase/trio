'use strict';

// An image attachment shows its filename as a caption under the thumbnail, so
// the descriptive names agents are asked for are visible to people too.
const assert = require('assert');
const { load } = require('./dom-harness');
const H = load().hooks;
let failures = 0;
function check(name, fn) { try { fn(); console.log('PASS: ' + name); } catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); } }

H.Trio.state.channel = 'garage';
H.Trio.state.operator = { id: 'operator' };

check('an image attachment carries its filename as a caption', () => {
  const card = H.cardFor({ id: 7, member_id: 'agent-1', member_name: 'Ada', content: 'two options',
    attachments: [{ id: 3, mime: 'image/png', filename: 'headlights-option-A-segmented.png' }] });
  const caption = card.querySelector('.message-attachment-caption');
  assert.ok(caption, 'caption present');
  assert.strictEqual(caption.textContent, 'headlights-option-A-segmented.png');
  assert.strictEqual(card.querySelector('img').alt, 'headlights-option-A-segmented.png');
});

check('the caption is text, never markup', () => {
  const card = H.cardFor({ id: 8, member_id: 'agent-1', member_name: 'Ada', content: 'x',
    attachments: [{ id: 4, mime: 'image/png', filename: '<b>bold</b>.png' }] });
  const caption = card.querySelector('.message-attachment-caption');
  assert.strictEqual(caption.textContent, '<b>bold</b>.png');
  assert.strictEqual(caption.querySelector('b'), null);
});

check('an image without a name has no caption', () => {
  const card = H.cardFor({ id: 9, member_id: 'agent-1', member_name: 'Ada', content: 'x',
    attachments: [{ id: 5, mime: 'image/png', filename: '' }] });
  assert.strictEqual(card.querySelector('.message-attachment-caption'), null);
});

console.log(failures ? `\n${failures} failure(s)` : '\nOK');
process.exit(failures ? 1 : 0);
