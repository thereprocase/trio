'use strict';
const assert = require('assert');
const { load } = require('./dom-harness');
function room(agents) {
  const x = load(), T = x.hooks.Trio, posts = [], notices = [];
  T.state.channel = 'synthetic'; T.state.operator = { id: 'operator' };
  T.state.members = new Map([{ id: 'operator', name: 'Human', kind: 'human', filter_mode: 'all' }, ...agents].map(m => [m.id, m]));
  T.api.post = async (url, body) => { posts.push({ url, body }); return { ok: true }; };
  T.ui.toast = text => notices.push(text);
  return { ...x, T, posts, notices, C: T.composer, input: x.document.getElementById('input') };
}
const agent = (name, mode, extra = {}) => ({ id: name.toLowerCase(), name, kind: 'agent', filter_mode: mode, ...extra });
let passed = 0;
async function sends(label, agents, content, expected = content) {
  const r = room(agents); r.input.textContent = content;
  assert.strictEqual(await r.C.send(), true, label);
  assert.strictEqual(r.posts.length, 1, label);
  assert.strictEqual(r.posts[0].body.content, expected, label);
  assert.strictEqual(r.notices.length, 0, label);
  console.log('PASS: ' + label); passed++;
}
(async () => {
  await sends('all listener accepts an unmentioned broadcast', [agent('Alpha', 'at'), agent('Beta', 'all')], 'Hello room');
  await sends('sole all listener gets no injected mention', [agent('Alpha', 'all')], 'Hello room');
  await sends('transient presence does not override all filter', [agent('Alpha', 'at'), agent('Beta', 'all', { active: false, status: 'working', ready: false })], 'Hello room');
  await sends('missing filter uses server all default', [agent('Alpha', 'at'), agent('Beta', undefined)], 'Hello room');
  await sends('about reference qualifies without adding @', [agent('Alpha', 'about'), agent('Beta', 'at')], 'Regarding #Alpha, this is ready');
  await sends('about reference also matches durable id', [agent('Alpha', 'about', { id: 'member-17' }), agent('Beta', 'at')], 'Regarding #member-17, this is ready');
  await sends('agents-only bang qualifies', [agent('Alpha', 'at'), agent('Beta', 'at')], '!agents recovery');
  await sends('all mention qualifies', [agent('Alpha', 'at'), agent('Beta', 'at')], '@all this is ready');
  await sends('direct ping qualifies', [agent('Alpha', 'at'), agent('Beta', 'at')], '@Alpha this is ready');
  await sends('sole at listener keeps existing auto-mention', [agent('Alpha', 'at')], 'Hello', '@Alpha Hello');
  for (const content of ['Hello room', '#Alpha reference', '!agents-extra is not a group']) {
    const r = room([agent('Alpha', 'at'), agent('Beta', 'at')]); r.input.textContent = content;
    assert.strictEqual(await r.C.send(), false); assert.strictEqual(r.posts.length, 0);
    assert.strictEqual(r.notices.length, 1); assert.ok(!/nobody will wake/i.test(r.notices[0]));
    assert.strictEqual(await r.C.send(), true); assert.strictEqual(r.posts[0].body.content, content);
    console.log('PASS: unmatched advisory remains overridable: ' + content); passed++;
  }
  const r = room([agent('Alpha', 'at'), agent('Beta', 'all')]); r.C.init(); r.input.textContent = 'One Enter';
  const enter = { key: 'Enter', preventDefault() {}, shiftKey: false };
  for (const fn of r.input._listeners.keydown || []) fn(enter);
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(r.posts.length, 1); assert.strictEqual(r.posts[0].body.content, 'One Enter'); assert.strictEqual(r.notices.length, 0);
  console.log('PASS: first Enter sends through the actual keyboard handler'); passed++;
  console.log(`${passed} composer wake tests passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
