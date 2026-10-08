// Explicit hub delivery reports must reach the visible channel roster.
'use strict';
const assert = require('assert');
const { load } = require('./dom-harness');
const cx = load();
const ws = cx.hooks.Trio.workspace;
let passed = 0;
for (const [status, label, expected] of [
  ['idle', 'listening (hooks)', 'idle'],
  ['working', 'working', 'working'],
  ['stale', 'silent since 12:30', 'offline'],
  ['stale', 'unreachable', 'offline'],
]) {
  // Old supervisor flags and status prose cannot override explicit delivery.
  const member = { id: 'reader', name: 'Reader', status, delivery_label: label,
                   status_text: 'waiting on a task', live: true, state: 'running', busy: true };
  assert.strictEqual(ws.channelStatus(member), expected);
  const html = ws.detailMember(member);
  assert.ok(html.includes(label), `roster should show ${label}`);
  assert.ok(html.includes(`channel-status-chip ${expected}`));
  assert.ok(!html.includes('waiting on a task'));
  console.log(`PASS: roster renders ${label}`);
  passed++;
}
const legacy = ws.detailMember({id: 'legacy', name: 'Legacy', status: 'active', status_text: 'ready'});
assert.ok(legacy.includes('ready'));
assert.ok(legacy.includes('channel-status-chip active'));
assert.ok(!legacy.includes('listening (hooks)'));
console.log('PASS: legacy roster rendering is unchanged');
passed++;
const archived = ws.detailMember({id: 'archived', name: 'Archived', archived: true,
                                  status: 'working', delivery_label: 'listening (hooks)'});
assert.ok(archived.includes('Archived — restore to rejoin'));
assert.ok(!archived.includes('listening (hooks)'));
console.log('PASS: archive fact outranks a delivery report');
passed++;
console.log(`${passed} passed`);
