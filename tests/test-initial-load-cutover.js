'use strict';

// The operator's first REST snapshot must start after the workspace EventSource
// opens. The server captures its per-channel cursors before sending those SSE
// response headers, so this order makes every message belong to the snapshot,
// the bounded stream catch-up, or both -- never neither.
const assert = require('assert');
const { load } = require('./dom-harness');

(async () => {
  const cx = load();
  const Trio = cx.hooks.Trio;
  const calls = [];
  let usageGate = null;
  const originalGet = Trio.api.get;
  const originalAgentsRefresh = Trio.agents.refresh;
  try {
    Trio.state.operator = { id: 'operator', source: 'tailscale' };
    Trio.api.get = async path => {
      calls.push(path);
      if (path.startsWith('/api/channels')) return { channels: [] };
      if (path.startsWith('/api/dms')) return { threads: [] };
      if (path.startsWith('/api/tasks')) return { tasks: [] };
      if (path.startsWith('/api/approvals')) return { approvals: [] };
      if (path.startsWith('/api/questions')) return { questions: [] };
      if (path.startsWith('/api/mentions')) return { mentions: [] };
      if (path.startsWith('/api/usage')) {
        if (usageGate) await usageGate;
        return {};
      }
      return {};
    };
    Trio.agents.refresh = async () => {};

    Trio.workspace.mount();
    assert.deepStrictEqual(calls, [],
      'operator REST snapshot raced ahead of workspace SSE open');

    cx.window.EventSource.instances.length = 0;
    Trio.startWorkspaceEvents();
    const stream = cx.window.EventSource.instances[0];
    assert.ok(stream, 'workspace EventSource was not created');
    assert.deepStrictEqual(calls, [],
      'constructing EventSource is not proof the server captured baselines');

    stream.fireOpen();
    assert.ok(calls.includes('/api/channels') && calls.includes('/api/dms'),
      'SSE open did not release the initial workspace snapshot');
    await new Promise(resolve => setImmediate(resolve));
    console.log('PASS: workspace stream opens before the initial REST snapshot');

    const channelsBefore = calls.filter(path => path === '/api/channels').length;
    let releaseUsage;
    usageGate = new Promise(resolve => { releaseUsage = resolve; });
    const slowRefresh = Trio.workspace.refresh();
    await Promise.resolve();
    Trio.workspace.refresh();
    assert.strictEqual(
      calls.filter(path => path === '/api/channels').length,
      channelsBefore + 1,
      'overlap should coalesce while the slow refresh remains in flight');
    releaseUsage();
    await slowRefresh;
    await new Promise(resolve => setImmediate(resolve));
    assert.strictEqual(
      calls.filter(path => path === '/api/channels').length,
      channelsBefore + 2,
      'refresh requested during an in-flight snapshot was discarded');
    console.log('PASS: an overlapping live refresh is coalesced and rerun');

    stream.fireMessage(JSON.stringify({
      type: 'message', id: 77, channel: 'elsewhere', member_id: 'agent',
      member_name: 'Agent', content: 'before disconnect', recipients: []
    }));
    Trio.startWorkspaceEvents();
    const replacement = cx.window.EventSource.instances.at(-1);
    assert.ok(String(replacement.url).endsWith('?after_id=77'),
      'workspace reconnect did not carry its last received message cursor: ' + replacement.url);
    console.log('PASS: workspace reconnect carries its last message cursor');
  } finally {
    Trio.stopWorkspaceEvents();
    Trio.workspace.unmount();
    Trio.api.get = originalGet;
    Trio.agents.refresh = originalAgentsRefresh;
  }
})().catch(error => {
  console.error('FAIL: ' + error.stack);
  process.exitCode = 1;
});
