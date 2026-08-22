// A history prime is delivered as hundreds of synchronous message events. The
// conversation should update state immediately but batch the growing-list DOM
// work into one animation frame after the first card.
//
// Usage: node tests/test-conversation-batch.js
'use strict';

const assert = require('assert');
const { load } = require('./dom-harness');

(async () => {
  const cx = load();
  const Trio = cx.hooks.Trio;
  const state = Trio.state;
  state.channel = 'batch-test';
  state.dmKey = '';
  state.messages = new Map();
  state.messageDomById = new Map();
  state.lastSeenByConv = {};
  state.dividerBaseByConv = {};
  const list = cx.document.getElementById('messages');
  list.replaceChildren();

  for (let id = 1; id <= 100; id++) {
    Trio.conversation.upsert({
      id, channel: 'batch-test', member_id: 'ag_batch', member_name: 'Batch',
      content: 'message ' + id, created_at: '2026-08-22T12:00:00Z',
      mentions: [], refs: [], bangs: [], recipients: [],
    });
  }

  assert.strictEqual(state.messages.size, 100,
    'all prime messages must enter state synchronously');
  assert.strictEqual(list.querySelectorAll('.message').length, 1,
    'only the first card should touch the DOM before the animation frame');

  await new Promise(resolve => setTimeout(resolve, 10));
  assert.strictEqual(list.querySelectorAll('.message').length, 100,
    'the queued prime tail should paint completely on the next frame');
  assert.strictEqual(state.messageDomById.size, 100,
    'the DOM index should cover every painted card');

  // An edit racing the queued tail must paint the newest state once, not leave
  // a stale duplicate card behind.
  state.messages = new Map(); state.messageDomById = new Map(); list.replaceChildren();
  Trio.conversation.upsert({ id: 201, channel: 'batch-test', member_id: 'ag_batch', content: 'first' });
  Trio.conversation.upsert({ id: 202, channel: 'batch-test', member_id: 'ag_batch', content: 'old' });
  Trio.conversation.upsert({ id: 202, channel: 'batch-test', member_id: 'ag_batch', content: 'edited' });
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.strictEqual(list.querySelectorAll('.message').length, 2,
    'a queued edit must not create a duplicate card');
  const edited = [...list.querySelectorAll('.message')]
    .find(card => Number(card.dataset.messageId) === 202);
  assert.ok(edited?.textContent.includes('edited'),
    'a queued edit must paint its latest state');

  console.log('OK — conversation prime batches DOM insertion without losing state or edits');
})().catch(error => { console.error(error); process.exit(1); });
