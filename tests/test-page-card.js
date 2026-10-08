'use strict';

// An agent's burner page renders as a card: title, expiry, an Open link and a
// preview that loads only on click, in a frame sandboxed to scripts without
// same-origin. The link is rebuilt from a validated id.
const assert = require('assert');
const { load } = require('./dom-harness');
const cx = load();
const H = cx.hooks;
const C = H.Trio.conversation;
let failures = 0;
function check(name, fn) { try { fn(); console.log('PASS: ' + name); } catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); } }

const ID = 'Xy0_-abcdefghijklmnopqrstuvwxyz012345';
const LATER = new Date(Date.now() + 3600 * 1000).toISOString();
const EARLIER = new Date(Date.now() - 60 * 1000).toISOString();
H.Trio.state.channel = 'charts';
H.Trio.state.operator = { id: 'operator' };

function pageMessage(extra = {}) {
  return Object.assign({ id: 41, member_id: 'agent-1', member_name: 'Ada',
    content: '[page] Latency chart', page: { id: ID, title: 'Latency chart', path: '/pages/' + ID, expires_at: LATER } }, extra);
}

check('a page message shows a card with the title, expiry and actions', () => {
  const card = H.cardFor(pageMessage());
  const pc = card.querySelector('.page-card');
  assert.ok(pc, 'card present');
  assert.strictEqual(pc.querySelector('.page-card-title').textContent, 'Latency chart');
  assert.ok(/expires/.test(pc.querySelector('.page-card-meta').textContent));
  const open = pc.querySelector('.page-card-open');
  assert.strictEqual(open.href, '/pages/' + ID);
  assert.strictEqual(open.target, '_blank');
  assert.ok(/noopener/.test(open.rel) && /noreferrer/.test(open.rel));
});

check('a page with no caption is the card alone, with no echo bubble', () => {
  const card = H.cardFor(pageMessage());
  assert.strictEqual(card.querySelector('.message-body'), null);
});

check('a caption shows in the bubble without the [page] header line', () => {
  const msg = pageMessage({ content: '[page] Latency chart\n\n@Bea the numbers' });
  assert.strictEqual(C.pageCaption(msg), '@Bea the numbers');
  const card = H.cardFor(msg);
  const body = card.querySelector('.message-body');
  assert.ok(body, 'bubble present');
  assert.ok(!/\[page\]/.test(body.textContent));
  assert.ok(card.querySelector('.page-card'));
});

check('the preview frame loads only on click, sandboxed to scripts without same-origin', () => {
  const pc = C.pageCard(pageMessage().page);
  assert.strictEqual(pc.querySelector('iframe'), null, 'no frame before the click');
  const button = pc.querySelector('.page-card-preview');
  button._listeners.click[0]({ preventDefault() {} });
  const frame = pc.querySelector('iframe');
  assert.ok(frame, 'frame after the click');
  assert.strictEqual(frame.getAttribute('sandbox'), 'allow-scripts');
  assert.ok(!/allow-same-origin/.test(frame.getAttribute('sandbox')));
  assert.strictEqual(frame.getAttribute('referrerpolicy'), 'no-referrer');
  assert.strictEqual(frame.src, '/pages/' + ID);
  assert.strictEqual(pc.querySelector('.page-card-preview'), null, 'button removed once loaded');
});

check('an expired page shows as expired with no link or preview', () => {
  const pc = C.pageCard({ id: ID, title: 'Old', expires_at: EARLIER });
  assert.ok(pc.classList.contains('expired'));
  assert.ok(/expired/i.test(pc.querySelector('.page-card-meta').textContent));
  assert.strictEqual(pc.querySelector('.page-card-open'), null);
  assert.strictEqual(pc.querySelector('.page-card-preview'), null);
});

check('the link comes from a validated id, never from the payload path', () => {
  const pc = C.pageCard({ id: 'javascript:alert(1)', title: 'x', path: 'javascript:alert(1)', expires_at: LATER });
  assert.strictEqual(pc.querySelector('.page-card-open'), null);
  assert.strictEqual(pc.querySelector('iframe'), null);
  const other = C.pageCard({ id: ID, title: 'x', path: 'https://elsewhere.example/', expires_at: LATER });
  assert.strictEqual(other.querySelector('.page-card-open').href, '/pages/' + ID);
});

check('the title is text, never markup', () => {
  const pc = C.pageCard({ id: ID, title: '<img src=x onerror=alert(1)>', expires_at: LATER });
  const title = pc.querySelector('.page-card-title');
  assert.strictEqual(title.textContent, '<img src=x onerror=alert(1)>');
  assert.strictEqual(title.querySelector('img'), null);
});

check('a retracted page message shows the tombstone and no card', () => {
  const card = H.cardFor(pageMessage({ retracted_at: '2026-01-01T00:00:00Z', retraction_reason: 'wrong data' }));
  assert.strictEqual(card.querySelector('.page-card'), null);
  assert.ok(/deleted/.test(card.querySelector('.message-body').textContent));
});

check('an ordinary message is unchanged', () => {
  const card = H.cardFor({ id: 42, member_id: 'agent-1', member_name: 'Ada', content: '[page] not a page' });
  assert.strictEqual(card.querySelector('.page-card'), null);
  assert.ok(/\[page\] not a page/.test(card.querySelector('.message-body').textContent));
});

console.log(failures ? `\n${failures} failure(s)` : '\nOK');
process.exit(failures ? 1 : 0);
