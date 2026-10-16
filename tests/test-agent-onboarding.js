// Member onboarding never requests or renders the protected control plane.
// Owner discovery failures replace stale choices with an actionable error.
'use strict';
const assert = require('assert');
const { load } = require('./dom-harness');

async function main() {
  let passed = 0;
  async function check(name, fn) { await fn(); passed++; console.log('PASS: ' + name); }
  for (const source of ['member', 'guest', 'pending', undefined]) {
    await check(`${source || 'unknown'} gets no managed controls or control-plane requests`, async () => {
      const cx = load(), T = cx.hooks.Trio, A = T.agents;
      T.state.operator = { source };
      T.state.channel = '<script>untrusted</script>';
      T.state.agentModels = { codex: [{ id: 'stale-model' }] };
      T.store.set('agents.list', [{ id: 'private-agent' }]);
      const calls = [];
      T.api.get = async url => { calls.push(url); assert.strictEqual(url, '/api/agents'); return {agents:[{id:'visible',name:'Own helper',channels:['shared'],from_your_tailnet:true}]}; };
      T.api.post = async url => { calls.push(url); throw Error('Unexpected control write'); };
      let modal;
      T.ui.modal = (title, body, submit, options) => { modal = { title, body, submit, options }; };
      await A.create();
      assert.strictEqual(modal, undefined);
      await A.loadDiscovery();
      await A.refresh();
      const panel = cx.document.createElement('section');
      A.renderPage(panel);
      assert.ok(panel.textContent.includes('Connect your own agent'));
      assert.ok(panel.textContent.includes('Spawn on hub'));
      assert.ok(!panel.innerHTML.includes('<script>untrusted</script>'));
      assert.strictEqual(panel.querySelectorAll('.agent-actions').length,0);
      assert.ok(!panel.querySelector('.roster-toolbar'));
      assert.ok(!panel.querySelector('.terminal-sessions'));
      assert.strictEqual(calls.length, ['member','guest'].includes(source) ? 1 : 0);
      assert.strictEqual(T.store.get('agents.list').length, ['member','guest'].includes(source) ? 1 : 0);
      if (['member','guest'].includes(source)) assert.ok(panel.textContent.includes('Own helper'));
      assert.strictEqual(Object.keys(T.state.agentModels).length, 0);
    });
  }
  await check('sidebar reserves spawning for owner and creation for an explicit grant', async () => {
    const cx = load(), T = cx.hooks.Trio;
    T.state.view = 'roster'; T.state.channels = []; T.state.dms = {};
    const rail = cx.document.getElementById('workspace-rail');
    cx.document.getElementById('channel-pins-button').innerHTML='<span class="channel-pin-count"></span>';
    for (const [source, granted, spawn, channels] of [
      ['member', false, 0, 0], ['guest', false, 0, 0],
      ['member', true, 0, 1], ['tailscale', false, 1, 1]]) {
      T.state.operator = {source, can_create_channels:granted};
      T.state.meta = {operator:T.state.operator};
      T.workspace.render();
      assert.strictEqual(rail.querySelectorAll('button').filter(b => b.getAttribute('aria-label') === 'Spawn on hub').length, spawn);
      assert.strictEqual(rail.querySelectorAll('button').filter(b => b.getAttribute('aria-label') === 'Create channel').length, channels);
      const row = T.workspace.detailMember({id:'helper',name:'Helper',kind:'agent'});
      assert.strictEqual(row.includes('data-member-authority='), source === 'tailscale');
    }
  });
  await check('owner sees discovery errors, loses stale models, and can retry', async () => {
    const cx = load(), T = cx.hooks.Trio, A = T.agents;
    T.state.operator = { source: 'tailscale' };
    T.state.agentModels = { claude: [{ id: 'stale-model' }] };
    let working = false, modal, posts = 0;
    T.api.post = async () => { posts++; };
    T.api.get = async url => {
      if (url === '/api/health') return { providers: ['claude'] };
      if (!working) throw Error('Sign-in required');
      return { models: [{ id: 'opus', name: 'Opus', efforts: ['medium'] }] };
    };
    T.ui.modal = (title, body, submit, options) => { modal = { title, body, submit, options }; };
    await A.create();
    assert.strictEqual(modal.title, 'Models unavailable');
    assert.ok(modal.body.includes('Sign-in required'));
    assert.ok(modal.body.includes('Retry model discovery'));
    assert.strictEqual(modal.options.submit, false);
    assert.strictEqual(T.state.agentModels.claude.length, 0);
    working = true;
    await A.create();
    assert.strictEqual(modal.title, 'Spawn on hub');
    assert.ok(modal.body.includes('value="opus" selected'));
    assert.ok(!modal.body.includes('Sign-in required'));
    assert.strictEqual(posts, 0);
  });
  await check('partial discovery selects the available provider and escapes errors', async () => {
    const cx = load(), T = cx.hooks.Trio, A = T.agents;
    T.state.operator = { source: 'loopback' };
    T.api.get = async url => {
      if (url === '/api/health') return { providers: ['claude', 'codex'] };
      if (url.includes('provider=claude')) throw Error('<img src=x onerror=bad()>');
      return { models: [{ id: 'test-model', name: 'Test model' }] };
    };
    let modal;
    T.ui.modal = (title, body) => { modal = { title, body }; };
    await A.create();
    assert.strictEqual(modal.title, 'Spawn on hub');
    assert.ok(modal.body.includes('value="codex" selected'));
    assert.ok(modal.body.includes('value="claude" disabled'));
    assert.ok(modal.body.includes('value="test-model" selected'));
    assert.ok(modal.body.includes('&lt;img'));
    assert.ok(!modal.body.includes('<img'));
  });
  await check('an empty model response never renders a blank picker', async () => {
    const cx = load(), T = cx.hooks.Trio;
    T.state.operator = { source: 'tailscale' };
    T.api.get = async url => url === '/api/health' ? { providers: ['codex'] } : { models: [] };
    let modal;
    T.ui.modal = (title, body) => { modal = { title, body }; };
    await T.agents.create();
    assert.strictEqual(modal.title, 'Models unavailable');
    assert.ok(modal.body.includes('No models were returned.'));
    assert.ok(!modal.body.includes('<select'));
  });
  console.log(`\n${passed} passed`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
