'use strict';

// Focused client contract coverage for the ordered modular web bundle.
const assert = require('assert');
const { load, FakeElement } = require('./dom-harness');
const cx = load();
const H = cx.hooks;
let failures = 0;
function check(name, fn) { try { fn(); console.log('PASS: ' + name); } catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); } }

check('markdown escapes HTML and renders emphasis', () => {
  const html = H.renderMarkdown('<img> **safe**');
  assert.ok(html.includes('&lt;img&gt;'));
  assert.ok(html.includes('<strong>safe</strong>'));
});
check('system events are plain and links are not system events', () => {
  assert.strictEqual(H.isSystemContent('[joined] alice'), true);
  assert.strictEqual(H.isSystemContent('[channel created] Testing'), true);
  assert.strictEqual(H.isSystemContent('[channel update] Testing'), false);
  assert.strictEqual(H.isSystemContent('[done](https://example.test)'), false);
});
check('system events collapse to concise human-readable copy', () => {
  assert.strictEqual(H.systemMessageText('[joined] Eomer — online for the UI update (skills: coding)'), 'Eomer joined channel');
  assert.strictEqual(H.systemMessageText('[culled] Boromir (ag_123) removed from channel — released tasks: #4'), 'Boromir removed from channel');
  assert.strictEqual(H.systemMessageText('[channel created] Testing the new Atrium UI', 'atrium-test'), 'atrium-test channel created');
});
check('system cards omit authored-message chrome', () => {
  H.Trio.state.channel = 'atrium-test';
  H.Trio.state.operator = { id: 'operator' };
  const card = H.cardFor({ id: 12, member_id: 'operator', member_name: 'exampleuser', content: '[channel created] Testing' });
  assert.ok(card.classList.contains('system-message'));
  assert.strictEqual(card.querySelector('.message-avatar'), null);
  assert.strictEqual(card.querySelector('.message-head'), null);
  assert.strictEqual(card.querySelector('.message-controls'), null);
  assert.strictEqual(card.querySelector('.message-id'), null);
  assert.strictEqual(card.querySelector('.message-body').textContent, 'atrium-test channel created');
});
check('own message actions stay hidden until a long-press gesture', () => {
  H.Trio.state.operator = { id: 'operator' };
  const card = H.cardFor({ id: 13, member_id: 'operator', member_name: 'me', content: 'hello' });
  assert.strictEqual(card.querySelector('.message-controls'), null);
  assert.strictEqual(card.querySelector('.message-actions-menu'), null);
  assert.ok(card._listeners.pointerdown);
  assert.ok(card._listeners.contextmenu);
});
check('operator messages omit the agent role badge', () => {
  H.Trio.state.operator = { id: 'operator' };
  const operatorCard = H.cardFor({ id: 14, member_id: 'operator', member_name: 'me', content: 'from me' });
  assert.strictEqual(operatorCard.querySelector('.message-role'), null);
  const agentCard = H.cardFor({ id: 15, member_id: 'agent-1', member_name: 'Atlas', content: 'from Atlas' });
  assert.strictEqual(agentCard.querySelector('.message-role').textContent, 'agent');
});
check('repeating the message long press hides its action menu', () => {
  H.Trio.state.operator = { id: 'operator' };
  const card = H.cardFor({ id: 16, member_id: 'operator', member_name: 'me', content: 'toggle me' });
  const contextmenu = card._listeners.contextmenu[0];
  const event = { target: card, preventDefault() {} };
  contextmenu(event);
  const menu = card.querySelector('.message-actions-menu');
  assert.ok(menu && !menu.classList.contains('hidden'));
  contextmenu(event);
  assert.ok(menu.classList.contains('hidden'));
});
check('id sigils render with the current display name', () => {
  H.state.members.set('a1', { id: 'a1', name: 'alice' });
  assert.strictEqual(H.humanizeIdSigils('@a1'), '@alice');
});
check('retracted messages replace their body safely', () => {
  const card = new FakeElement('article'), body = new FakeElement('div'); card.append(body);
  H.paintBody(card, body, { id: 1, content: 'secret', retracted_at: 'now', retraction_reason: 'author deleted' });
  assert.strictEqual(body.textContent, '[deleted — author deleted]');
  assert.ok(card.classList.contains('retracted'));
});
check('message rendering decorates mention, ref, and bang sigils', () => {
  H.state.members.set('b1', { id: 'b1', name: 'bob' });
  const card = new FakeElement('article'), body = new FakeElement('div'); card.append(body);
  H.paintBody(card, body, { id: 2, content: '@bob #bob !bob', mentions:['b1'], refs:['b1'], bangs:['b1'] });
  assert.strictEqual(body.querySelectorAll('.inline-mention').length, 1);
  assert.strictEqual(body.querySelectorAll('.inline-ref').length, 1);
  assert.strictEqual(body.querySelectorAll('.inline-bang').length, 1);
});
check('mention chips carry the agent tone and @all renders as a shimmer', () => {
  H.state.members.set('b1', { id: 'b1', name: 'bob' });
  const card = new FakeElement('article'), body = new FakeElement('div'); card.append(body);
  H.paintBody(card, body, { id: 3, content: '@bob and @all', mentions:['b1'], refs:[], bangs:[] });
  const mention = body.querySelector('.inline-mention');
  assert.ok(mention && mention.dataset.tone, 'an @-mention carries a data-tone for its agent colour');
  assert.strictEqual(body.querySelectorAll('.inline-all').length, 1, '@all gets the shimmer span');
});
check('composer keeps @-mention styling across navigation once the roster reloads', () => {
  const S = H.Trio.state, input = cx.document.getElementById('input');
  H.Trio.composer.init(); // attaches the roster listener (as mount() does in the app)
  // In channel 'atrium' with member nova; a draft mentioning her is stored.
  S.channel = 'atrium'; S.dmKey = '';
  S.drafts = S.drafts || {}; S.drafts['atrium'] = '@nova ship it';
  // Navigate to a non-conversation view: showView() clears members + channel.
  S.members = new Map(); S.channel = '';
  // Navigate back: refresh() re-renders the draft BEFORE the roster arrives,
  // so the mention falls back to plain text — the reported bug's moment.
  S.channel = 'atrium';
  H.Trio.composer.refresh();
  assert.strictEqual(input.querySelectorAll('.inline-mention').length, 0,
    'pre-roster: mention is plain because members are not loaded yet');
  // The roster lands (04-events sets state.members, then dispatches 'roster').
  S.members = new Map([['ag_nova', { id: 'ag_nova', name: 'nova' }]]);
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('roster', { detail: { channel: 'atrium', members: [{ id: 'ag_nova', name: 'nova' }] } }));
  assert.strictEqual(input.querySelectorAll('.inline-mention').length, 1,
    'post-roster: composer re-styles the @-mention so it stays recognized');
});
check('SSE upsert keeps the timeline state keyed by message id', () => {
  H.upsert({ id: 77, member_id: 'b1', content: 'first' });
  H.upsert({ id: 77, member_id: 'b1', content: 'edited', edited_at: 'now' });
  assert.strictEqual(H.state.messages.get(77).content, 'edited');
});
check('answer payload matches the server reply_to and selection contract', () => {
  H.state.operator = { id: 'operator' };
  const message = { id: 91, choices: { target: 'operator', questions: [{ options: ['one', 'two'], mode: 'one' }] } };
  H.state.answers.set(91, [{ picked: new Set([1]), custom: '' }]);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(H.answerPayload(message, message.choices.questions))), {
    content: 'two', reply_to: 91, selection: { answers: [{ picked: [1], custom: [] }] },
  });
  H.state.answers.set(92, [{ picked: new Set([1]), custom: '' }, { picked: new Set([0, 1]), custom: 'Olives' }]);
  const multiMsg = { id: 92, choices: { target: 'operator', questions: [{ question: 'Size?', options: ['S', 'M'] }, { question: 'Top?', options: ['Cheese', 'Pep'] }] } };
  assert.strictEqual(H.answerPayload(multiMsg, multiMsg.choices.questions).content, 'Size? → M\nTop? → Cheese, Pep, Olives');
});
check('private state derives from the server recipients field', () => {
  assert.strictEqual(H.isPrivate({ recipients: ['ag_1'] }), true);
  assert.strictEqual(H.isPrivate({ recipients: [] }), false);
});
check('agent audit history accepts messages without the operator', () => {
  H.Trio.state.dmKey = 'ag_1,ag_2';
  H.Trio.state.dmAudit = true;
  H.Trio.state.dmMemberIds = ['ag_1', 'ag_2'];
  H.upsert({ id: 101, member_id: 'ag_1', recipients: ['ag_2'], content: 'agent-only' });
  assert.strictEqual(H.Trio.state.messages.get(101).content, 'agent-only');
  delete H.Trio.state.dmKey;
  delete H.Trio.state.dmAudit;
  delete H.Trio.state.dmMemberIds;
});
check('composer send payload keeps integer attachment ids and channel targets', () => {
  H.state.selectedTargets = new Set(['ag_1']);
  H.state.pendingAttachments = [{ id: 4 }, { id: 'bad' }, { id: 8 }];
  H.state.dmTargetId = 'ag_1';
  H.Trio.state.channel = 'atrium';
  const input = cx.document.getElementById('input');
  H.Trio.composer.setTargets(['ag_1']);
  H.Trio.composer.init();
  assert.strictEqual(H.Trio.state.composerMode, undefined);
  H.Trio.composer.buildSendPayload();
  // init() → loadComposerAux() resets pendingAttachments to the (empty)
  // per-conversation store, so stage them AFTER init to actually exercise the
  // integer-id filtering ('bad' dropped) in buildSendPayload.
  H.Trio.state.pendingAttachments = [{ id: 4 }, { id: 'bad' }, { id: 8 }];
  input.textContent = 'hello';
  // Mentions live inline in the text now: selected targets are NOT prepended,
  // and the dead `mentions` field is gone. Recipients + attachment filtering
  // still hold. (setTargets above is inert by design — proves no prepend.)
  assert.deepStrictEqual(JSON.parse(JSON.stringify(H.buildSendPayload())), {
    content: 'hello', attachment_ids: [4, 8], recipients: ['ag_1'],
  });
  delete H.state.dmTargetId;
});
check('DM send payload includes the conversation recipients, not just a single target id', () => {
  H.Trio.state.dmKey = 'dm-thread';
  H.Trio.state.dmMemberIds = ['ag_1'];
  H.Trio.state.dmTargetId = undefined;
  const input = cx.document.getElementById('input'); input.textContent = 'hello';
  assert.deepStrictEqual(JSON.parse(JSON.stringify(H.buildSendPayload())), {
    content: 'hello', attachment_ids: [4, 8], recipients: ['ag_1'],
  });
  delete H.Trio.state.dmKey;
  delete H.Trio.state.dmMemberIds;
});
check('dictation control disables itself when no speech engine is available', () => {
  const button = cx.document.getElementById('dictate-btn');
  assert.strictEqual(button.disabled, true);
  assert.strictEqual(button.title, 'Dictation is unavailable in this browser');
});
check('dictation button: active recording shows no redundant status text (the meter already signals it)', () => {
  H.Trio.composer.setDictationButtonState(true);
  const status = cx.document.getElementById('dictate-status');
  const button = cx.document.getElementById('dictate-btn');
  assert.strictEqual(status.hidden, true);
  assert.strictEqual(status.textContent, '');
  assert.strictEqual(button.classList.contains('recording'), true);
});
check('dictation button: processing (transcribing) still shows its status text — that IS invisible work the meter can\'t represent', () => {
  H.Trio.composer.setDictationButtonState(false, { processing: true, statusText: 'Transcribing (local Whisper)…' });
  const status = cx.document.getElementById('dictate-status');
  assert.strictEqual(status.hidden, false);
  assert.strictEqual(status.textContent, 'Transcribing (local Whisper)…');
});
check('dictation button: back to idle clears both the recording state and any status text', () => {
  H.Trio.composer.setDictationButtonState(false);
  const status = cx.document.getElementById('dictate-status');
  const button = cx.document.getElementById('dictate-btn');
  assert.strictEqual(status.hidden, true);
  assert.strictEqual(button.classList.contains('recording'), false);
});
// LOTC/Frodo: the visible "Recording…"/"Listening…" text was removed as
// redundant clutter for sighted users, but it was also a screen reader's
// only chance at a state announcement — reinstate that via the existing
// #trio-aria-live region instead of visible text, and keep aria-label in
// sync with the visible tooltip (it used to stay "Dictate" even mid-recording).
check('dictation button: recording state is announced via the aria-live region, not visible text', () => {
  H.Trio.composer.setDictationButtonState(true);
  const live = cx.document.getElementById('trio-aria-live');
  assert.strictEqual(live.textContent, 'Recording');
  const button = cx.document.getElementById('dictate-btn');
  assert.strictEqual(button.getAttribute('aria-label'), 'Stop dictation');
});
check('dictation button: transcribing state reuses its visible status text for the aria-live announcement too', () => {
  H.Trio.composer.setDictationButtonState(false, { processing: true, statusText: 'Transcribing (local Whisper)…' });
  const live = cx.document.getElementById('trio-aria-live');
  assert.strictEqual(live.textContent, 'Transcribing (local Whisper)…');
});
check('dictation button: returning to idle clears the aria-live announcement', () => {
  H.Trio.composer.setDictationButtonState(false);
  const live = cx.document.getElementById('trio-aria-live');
  assert.strictEqual(live.textContent, '');
  // This test harness's fake browser env has no speech engine (see the very
  // first dictation check in this file), so the idle label reflects that —
  // not a plain "Dictate" — but it must still be kept out of aria-label sync.
  const button = cx.document.getElementById('dictate-btn');
  assert.strictEqual(button.getAttribute('aria-label'), 'Dictation is unavailable in this browser');
});
check('task filters default to open and all shows every row', () => {
  H.Trio.state.tasks = [{ id: 1, message: 'Open task', status: 'open' }, { id: 2, message: 'Claimed task', status: 'claimed' }];
  H.Trio.workspace.showView('tasks');
  const panel = cx.document.getElementById('trio-tasks-view');
  assert.strictEqual(panel.querySelectorAll('.task-row').length, 1);
  H.Trio.state.taskFilter = 'all';
  H.Trio.workspace.showView('tasks');
  assert.strictEqual(panel.querySelectorAll('.task-row').length, 2);
});
check('messages page renders real avatars and markdown, not escaped text', () => {
  H.Trio.state.mentions = [{ id: 501, member_id: 'ag_1', member_name: 'Nova', channel: 'general', content: 'ship **it** now', created_at: '2026-08-06T17:00:00Z', read: false }];
  H.Trio.state.dms = { your_dms: [] };
  H.Trio.state.messagesTab = 'mentions';
  H.Trio.workspace.showView('messages');
  const panel = cx.document.getElementById('trio-messages-view');
  const card = panel.querySelector('.att-card');
  assert.ok(card, 'a mention card is rendered');
  assert.ok(card.querySelector('.av'), 'the card shows a real avatar (not a plain initials fallback)');
  assert.strictEqual(card.querySelector('.avatar-fallback'), null, 'no plain-text avatar fallback');
  const reason = card.querySelector('.reason');
  assert.ok(reason.querySelector('strong'), 'markdown bold is rendered');
  assert.ok(!/\*\*/.test(reason.textContent), 'raw markdown asterisks are consumed, not shown as text');
});
check('sidebar paints the selected channel immediately', () => {
  H.Trio.state.view = 'conversation';
  H.Trio.state.dmKey = '';
  H.Trio.state.channel = 'research';
  H.Trio.state.channels = [{ code: 'general' }, { code: 'research' }];
  H.Trio.state.tasks = { filter: 'open', list: [] };
  H.Trio.state.agents = { list: [] };
  H.Trio.workspace.render();
  const rail = cx.document.getElementById('workspace-rail');
  const active = rail.querySelectorAll('.nav-item.active');
  assert.strictEqual(active.length, 1);
  assert.strictEqual(active[0].querySelector('.nav-label').textContent, 'research');
});
check('A2A sidebar rows render a paired avatar stack', () => {
  H.Trio.state.dms = { your_dms: [], agent_dms: [{ key: 'ag_a,ag_b', name: 'Atlas ↔ Nova', member_ids: ['ag_a', 'ag_b'] }] };
  H.Trio.workspace.render();
  const rail = cx.document.getElementById('workspace-rail');
  const pair = rail.querySelector('.dm-pair');
  assert.ok(pair);
  assert.strictEqual(pair.querySelectorAll('.av').length, 2);
});
check('agent roster panel is not force-hidden by a later render()', () => {
  H.Trio.agents.render([]);
  const panel = cx.document.getElementById('trio-agents');
  assert.ok(panel);
  panel.hidden = false;
  H.Trio.agents.render([]);
  assert.strictEqual(panel.hidden, false);
});
check('agent view model normalizes lifecycle and status', () => {
  const vm = H.Trio.agents.viewModel({ id: 'ag_1', name: 'Test', live: true, busy: true, provider: 'claude', model: 'sonnet', filter_mode: 'about', error: 'boom' });
  assert.strictEqual(vm.lifecycle, 'working');
  assert.strictEqual(vm.wakePolicy, 'about');
  assert.strictEqual(vm.needsAttention, true);
});
check('agent view model surfaces its reasoning effort', () => {
  const vm = H.Trio.agents.viewModel({ id: 'ag_1', effort: 'high' });
  assert.strictEqual(vm.effort, 'high');
  assert.strictEqual(H.Trio.agents.viewModel({ id: 'ag_2' }).effort, '');
});
check('effortsForModel reads the model-specific list from discovered models, not a global default', () => {
  H.Trio.state.agentModels = {
    claude: [{ id: 'haiku', name: 'Claude Haiku', efforts: ['low', 'medium', 'high'] }],
    codex: [{ id: 'fake-codex', name: 'Fake Codex', efforts: ['low', 'high'] }],
  };
  assert.deepStrictEqual(Array.from(H.Trio.agents.effortsForModel('claude', 'haiku')), ['low', 'medium', 'high']);
  assert.deepStrictEqual(Array.from(H.Trio.agents.effortsForModel('codex', 'fake-codex')), ['low', 'high']);
});
check('effortsForModel offers NOTHING for an undiscovered model, rather than inventing a ladder', () => {
  // It used to return ['low','medium','high'] here. That is wrong for nearly
  // every real model — Codex offers xhigh/max/ultra, Haiku has no max — so the
  // operator was shown levels the model does not have and picked one that was
  // then silently coerced. An empty list makes the picker say "choose a model
  // first" instead of guessing.
  H.Trio.state.agentModels = { claude: [] };
  assert.deepStrictEqual(Array.from(H.Trio.agents.effortsForModel('claude', 'unknown-model')), []);
});
check('the effort control explains itself when there is nothing to choose from', () => {
  const html = H.Trio.agents.effortSlider([], '');
  assert.ok(/choose a model first/i.test(html),
    'an empty effort list must say why, not render an empty slider: ' + html);
  assert.ok(!/type="range"/.test(html), 'no slider should be rendered with no steps');
});
check('an existing agent keeps its effort visible even if discovery is thin', () => {
  const html = H.Trio.agents.effortSlider([], 'ultra');
  assert.ok(/ultra/.test(html), 'the current value must survive a failed discovery');
});
check('effortOptions renders a <select>-ready option list with the current value selected', () => {
  const html = H.Trio.agents.effortOptions(['low', 'high'], 'high');
  assert.ok(html.includes('<option value="low">low</option>'));
  assert.ok(html.includes('<option value="high" selected>high</option>'));
});
// LOTC/Frodo (critical): with no explicit selection, browsers auto-select
// the FIRST <option> — which, before this fix, silently created/edited
// agents at the LOWEST effort whenever the user left the control alone
// (create: always; edit: any agent still on its model default, since
// vm.effort is '' there). A real "Model default" option, selected by
// default, is what a blank/'' selection must resolve to.
check('effortOptions: no selection lands on a real empty option, not the first effort', () => {
  // Bulk edit only. The empty value has a statable meaning here — the selected
  // agents may run different models, and an empty effort tells the server to
  // use each one's own default — so the label says that rather than "Default".
  // What must NOT happen is the browser auto-selecting "low" for every agent
  // the operator never touched.
  const html = H.Trio.agents.effortOptions(['low', 'medium', 'high'], '');
  // The label is escaped on the way out (the apostrophe becomes &#39;), which
  // is correct — assert the structure and the sense, not the raw bytes.
  assert.ok(/^<option value="" selected>[^<]*default<\/option>/.test(html), html);
  assert.ok(!html.includes('<option value="low" selected>'));
});
check('effortOptions: a custom default label can name the resolved level', () => {
  const html = H.Trio.agents.effortOptions(['low', 'high'], '', { defaultLabel: 'Model default (medium)' });
  assert.ok(html.includes('>Model default (medium)</option>'));
});
// LOTC/Frodo (warning): an agent already set to an effort that discovery
// didn't return (stale/thin catalog, or a value valid at creation time but
// since dropped from the model's advertised list) must not lose that value
// from the dropdown — losing it meant opening-then-saving without any
// change silently downgraded the agent to whatever option landed first.
check('effortOptions: the agent\'s current value is kept as an option even if missing from the discovered list', () => {
  const html = H.Trio.agents.effortOptions(['low', 'medium', 'high'], 'max');
  assert.ok(html.includes('<option value="max" selected>max</option>'));
});
check('agent last-active timestamps are local and human-readable', () => {
  const iso = '2026-08-02T00:45:14+00:00';
  const options = { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
  const formatted = H.Trio.agents.formatLastActive(iso);
  assert.strictEqual(formatted, new Date(iso).toLocaleString([], options));
  assert.notStrictEqual(formatted, iso);
  assert.ok(!formatted.includes('T'));
  assert.strictEqual(H.Trio.agents.formatLastActive('not a timestamp'), 'not a timestamp');
});
check('agent action capabilities filter by lifecycle', () => {
  const caps = H.Trio.agents.actionCaps(H.Trio.agents.viewModel({ id: 'ag_1', live: false, state: 'stopped' }));
  assert.ok(caps.includes('wake'));
  assert.strictEqual(caps.includes('interrupt'), false);
});
check('agent management exposes every supported lifecycle action', () => {
  const live = H.Trio.agents.viewModel({ id: 'ag_live', live: true, busy: false });
  const resting = H.Trio.agents.viewModel({ id: 'ag_resting', live: false, state: 'sleeping' });
  assert.deepStrictEqual(Array.from(H.Trio.agents.actionCaps(live)), ['hibernate', 'compact', 'clear', 'archive']);
  assert.deepStrictEqual(Array.from(H.Trio.agents.actionCaps(resting)), ['wake', 'clear', 'archive']);
  assert.strictEqual(H.Trio.agents.actionLabel('hibernate'), 'Hibernate');
  assert.strictEqual(H.Trio.agents.actionLabel('compact'), 'Compact context');
  assert.strictEqual(H.Trio.agents.actionLabel('archive'), 'Archive agent');
  assert.strictEqual(H.Trio.agents.actionLabel('unarchive'), 'Unarchive agent');
});
check('archived agents expose only the unarchive action', () => {
  const vm = H.Trio.agents.viewModel({ id: 'ag_archived', archived: true, live: false, state: 'stopped' });
  assert.strictEqual(vm.archived, true);
  assert.deepStrictEqual(Array.from(H.Trio.agents.actionCaps(vm)), ['unarchive']);
});
check('viewModel derives archived from archived_at alone (server contract)', () => {
  const vm = H.Trio.agents.viewModel({ id: 'ag_at', archived_at: '2026-08-01T12:00:00Z', live: false, state: 'stopped' });
  assert.strictEqual(vm.archived, true);
  assert.deepStrictEqual(Array.from(H.Trio.agents.actionCaps(vm)), ['unarchive']);
});
check('agent compaction status outranks the ordinary live state', () => {
  const vm = H.Trio.agents.viewModel({ id: 'ag_compact', live: true, busy: false, state: 'compacting' });
  assert.strictEqual(vm.lifecycle, 'compacting');
  assert.strictEqual(vm.compacting, true);
  assert.deepStrictEqual(Array.from(H.Trio.agents.actionCaps(vm)), ['stop', 'archive']);
});
check('clicking a roster tile opens that agent’s management dialog', () => {
  const originalModal = H.Trio.ui.modal;
  let opened = null;
  H.Trio.ui.modal = (title, body) => { opened = { title, body }; };
  H.Trio.store.set('agents.list', [{ id: 'ag_tile', name: 'Tile Agent', live: true, busy: false, state: 'idle' }]);
  const panel = new FakeElement('section');
  H.Trio.agents.renderPage(panel);
  const card = panel.querySelector('.agent-card');
  assert.ok(card);
  card._listeners.click[0]({ target: card });
  H.Trio.ui.modal = originalModal;
  assert.ok(opened);
  assert.strictEqual(opened.title, 'Manage agent: Tile Agent');
  assert.ok(opened.body.includes('Compact context'));
});
check('agent-management dialogs can omit the unrelated Save action', () => {
  const dialog = cx.document.getElementById('trio-control-modal');
  dialog.showModal = () => {};
  H.Trio.ui.modal('Manage agent', '<p>Controls</p>', undefined, { submit: false, cancelLabel: 'Close' });
  const labels = dialog.querySelectorAll('button').map(button => button.textContent);
  assert.ok(labels.includes('Close'));
  assert.strictEqual(labels.includes('Save'), false);
});
check('agent model options normalize provider model records', () => {
  const models = H.Trio.agents.normalizeModels([
    { id: 'sonnet', name: 'Claude Sonnet' },
    { id: 'opus', name: 'Claude Opus' },
  ]);
  assert.deepStrictEqual(models.map(model => model.id), ['sonnet', 'opus']);
  assert.ok(H.Trio.agents.modelOptions(models).includes('value="sonnet"'));
  assert.ok(H.Trio.agents.modelOptions(models).includes('Claude Sonnet'));
});
check('agent model options are selected from the active provider', () => {
  H.Trio.state.agentModels = {
    claude: [{ id: 'sonnet', name: 'Claude Sonnet' }],
    codex: [{ id: 'gpt-5-codex', name: 'GPT-5-Codex' }],
  };
  assert.ok(H.Trio.agents.modelOptions(H.Trio.state.agentModels.claude).includes('sonnet'));
  assert.ok(H.Trio.agents.modelOptions(H.Trio.state.agentModels.codex).includes('gpt-5-codex'));
});
check('permission profiles expose the backend enum as a dropdown', () => {
  const html = H.Trio.agents.permissionOptions();
  assert.ok(html.includes('name="permission_profile"'));
  assert.ok(html.includes('value="observe"'));
  assert.ok(html.includes('value="balanced"'));
  assert.ok(html.includes('value="autonomous"'));
  assert.ok(html.includes('value="balanced" selected'));
});
check('preferences save persists the dictation switch', () => {
  H.Trio.preferences.save({ dictation: false });
  assert.strictEqual(H.Trio.preferences.read().dictation, false);
  H.Trio.preferences.save({ dictation: true });
});

// LOTC: the channel drawer used to disagree with the Agent roster page
// because the supervisor-backed {state,live,busy} merge only ran on the DM
// code path — a normal channel's member list fell through to the raw
// heartbeat-only rows, so channelStatus()'s "prefer the roster" branch never
// fired for the common case. showDetails() must now merge state.agents into
// EVERY channel's member list, not just DMs.
check('channel drawer status agrees with the Agent roster for a regular (non-DM) channel', () => {
  H.Trio.state.dmKey = null;
  H.Trio.state.channel = 'atrium-test';
  H.Trio.state.channels = [{ code: 'atrium-test', topic: 'Testing' }];
  H.Trio.state.members = new Map([
    ['ag_sleepy', { id: 'ag_sleepy', name: 'Sleepy', status: 'active' }],
  ]);
  H.Trio.state.agents = [{ id: 'ag_sleepy', name: 'Sleepy', state: 'sleeping', live: false, busy: false }];
  H.Trio.workspace.showDetails();
  const html = cx.document.getElementById('channel-drawer-body').innerHTML;
  // The Agent roster page renders a sleeping (hibernated) agent as
  // 'sleeping', not the heartbeat row's stale 'active' — the drawer must
  // agree, matching channelStatus()'s own state==='sleeping' branch.
  assert.ok(html.includes('channel-status-chip sleeping'),
    'expected the roster-backed "sleeping" status, got: ' + html);
  assert.ok(!html.includes('channel-status-chip active'),
    'drawer still showed the stale heartbeat-only "active" status');
});
check('channel drawer never shows the operator as offline while they are viewing it', () => {
  H.Trio.state.dmKey = null;
  H.Trio.state.channel = 'atrium-test';
  H.Trio.state.channels = [{ code: 'atrium-test', topic: 'Testing' }];
  H.Trio.state.members = new Map(); // operator hasn't posted in this channel
  H.Trio.state.agents = [];
  H.Trio.state.operator = { id: '_op_l_exampleuser', name: 'exampleuser', source: 'loopback', pending: false };
  H.Trio.workspace.showDetails();
  const html = cx.document.getElementById('channel-drawer-body').innerHTML;
  assert.ok(!html.includes('channel-status-chip offline'),
    'operator rendered offline while actively viewing the page: ' + html);
});
check('channelStatus prefers roster-backed compacting/error states over heartbeat status', () => {
  // Compaction surfaces as its own 'compacting' status (not 'sleeping'), so the
  // drawer/face-pile match the Agent roster card's "Compacting" label.
  assert.strictEqual(H.Trio.workspace.channelStatus({ live: true, state: 'compacting', busy: true }), 'compacting');
  assert.strictEqual(H.Trio.workspace.channelStatus({ live: false, state: 'errored' }), 'errored');
  assert.strictEqual(H.Trio.workspace.channelStatus({ live: true, state: 'running', busy: false }), 'idle');
});
check('channelStatus: a live agent shows working from the SSE roster status (near-realtime, not the slow busy poll)', () => {
  // member_status is pushed over the roster SSE within ~1s; busy comes from the
  // slower /api/agents poll. A live agent whose roster status is 'working' must
  // read working immediately even while the polled busy flag is still false.
  assert.strictEqual(H.Trio.workspace.channelStatus({ live: true, state: 'running', busy: false, status: 'working' }), 'working');
  assert.strictEqual(H.Trio.workspace.channelStatus({ live: true, state: 'running', busy: false, status: 'idle' }), 'idle');
  assert.strictEqual(H.Trio.workspace.channelStatus({ live: true, state: 'running', busy: true, status: 'idle' }), 'working');
});
check('drawer members re-render live on a roster tick (open drawer reflects a status change)', () => {
  H.Trio.state.dmKey = null;
  H.Trio.state.channel = 'atrium-test';
  H.Trio.state.channels = [{ code: 'atrium-test', topic: 'T' }];
  H.Trio.state.operator = { id: 'op', name: 'op' };
  H.Trio.state.members = new Map([['ag_x', { id: 'ag_x', name: 'X', status: 'idle' }]]);
  H.Trio.state.agents = [{ id: 'ag_x', name: 'X', state: 'running', live: true, busy: false }];
  cx.document.getElementById('channel-drawer').classList.add('open');
  H.Trio.workspace.showDetails();
  assert.ok(cx.document.getElementById('channel-drawer-body').innerHTML.includes('channel-status-chip idle'),
    'drawer should render idle initially');
  // The roster SSE pushes a status change to working (with a tool chip).
  H.Trio.state.members = new Map([['ag_x', { id: 'ag_x', name: 'X', status: 'working', last_tool_name: 'Bash' }]]);
  H.Trio.workspace.refreshDrawerMembers();
  const membersHtml = cx.document.getElementById('channel-drawer-members').innerHTML;
  assert.ok(membersHtml.includes('channel-status-chip working'),
    'open drawer must re-render to working on the roster tick: ' + membersHtml);
});
check('refreshDrawerMembers is a no-op when the drawer is closed', () => {
  cx.document.getElementById('channel-drawer').classList.remove('open');
  assert.doesNotThrow(() => H.Trio.workspace.refreshDrawerMembers());
});
check('channel drawer labels a compacting agent "Compacting", not "Sleeping"', () => {
  H.Trio.state.dmKey = null;
  H.Trio.state.channel = 'atrium-test';
  H.Trio.state.channels = [{ code: 'atrium-test', topic: 'Testing' }];
  H.Trio.state.members = new Map([['ag_c', { id: 'ag_c', name: 'Comp', status: 'active' }]]);
  H.Trio.state.agents = [{ id: 'ag_c', name: 'Comp', state: 'compacting', live: true, busy: true }];
  H.Trio.workspace.showDetails();
  const html = cx.document.getElementById('channel-drawer-body').innerHTML;
  assert.ok(html.includes('channel-status-chip compacting') && html.includes('Compacting'),
    'expected a "Compacting" chip, got: ' + html);
  assert.ok(!html.includes('channel-status-chip sleeping'),
    'compacting agent still mislabeled as sleeping: ' + html);
});
// Face-pile parity: renderFacePile must merge state.agents like the drawer, so
// its dot matches the drawer/roster instead of the heartbeat-only roster status.
check('face-pile merges agent {live,busy,state} so its status matches the drawer', () => {
  H.Trio.state.dmKey = null;
  H.Trio.state.channel = 'atrium-test';
  H.Trio.state.operator = { id: 'op', name: 'op' };
  H.Trio.state.loaded = { meta: true, agents: true };
  H.Trio.state.sliceErrors = {};
  // roster row alone would read 'offline' (no live/state); the agent record says compacting.
  H.Trio.state.members = new Map([['ag_c', { id: 'ag_c', name: 'Comp' }]]);
  H.Trio.state.agents = [{ id: 'ag_c', name: 'Comp', state: 'compacting', live: true, busy: true }];
  H.Trio.workspace.renderFacePile();
  const html = cx.document.getElementById('face-pile').innerHTML;
  assert.ok(html.includes('st-compacting'),
    'face-pile did not reflect the merged compacting state: ' + html);
});

// Notification tier classification — DM > @mention/!bang > #ref > plain,
// first match wins. See 45-notifications.js / 40-preferences.js's
// NOTIFICATION_TIERS comment for the full rationale.
check('classify: a DM to you outranks everything else', () => {
  const msg = { member_id: 'ag_1', recipients: ['me'], mentions: [], refs: [], bangs: [] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), 'dm');
});
check('classify: @mention when not a DM', () => {
  const msg = { member_id: 'ag_1', recipients: [], mentions: ['me'], refs: [], bangs: [] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), 'mention');
});
check('classify: !bang counts as the same tier as @mention', () => {
  const msg = { member_id: 'ag_1', recipients: [], mentions: [], refs: [], bangs: ['me'] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), 'mention');
});
check('classify: #reference below mention, above plain', () => {
  const msg = { member_id: 'ag_1', recipients: [], mentions: [], refs: ['me'], bangs: [] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), 'ref');
});
check('classify: untargeted channel message is plain', () => {
  const msg = { member_id: 'ag_1', recipients: [], mentions: [], refs: [], bangs: [] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), 'plain');
});
check('classify: a DM addressed to someone ELSE is not a DM tier for you', () => {
  const msg = { member_id: 'ag_1', recipients: ['someone-else'], mentions: [], refs: [], bangs: [] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), 'plain');
});
check('classify: your own message never classifies (no self-notify)', () => {
  const msg = { member_id: 'me', recipients: [], mentions: ['me'], refs: [], bangs: [] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), null);
});
check('classify: a system message never classifies', () => {
  const msg = { member_id: 'ag_1', content: '[joined] Someone', mentions: ['me'], refs: [], bangs: [], recipients: [] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), null);
});
check('classify: DM still wins even if you were also @mentioned in it', () => {
  const msg = { member_id: 'ag_1', recipients: ['me'], mentions: ['me'], refs: [], bangs: [] };
  assert.strictEqual(H.Trio.notifications.classify(msg, 'me'), 'dm');
});
check('notifications module exposes the 4-tier priority order and 3 sound presets', () => {
  // Array.from() (not the raw sandboxed array) — the vm sandbox's Array is a
  // different realm than this test's, so a bare literal comparison here
  // fails deepStrictEqual's prototype check despite identical contents.
  assert.deepStrictEqual(Array.from(H.Trio.notifications.TIERS), ['dm', 'mention', 'ref', 'plain']);
  assert.deepStrictEqual(Object.keys(H.Trio.notifications.SOUNDS).sort(), ['alert', 'ping', 'tick']);
});
check('playPreset never throws even with no AudioContext available (headless/CI)', () => {
  assert.doesNotThrow(() => H.Trio.notifications.playPreset('ping', 0.5));
  assert.doesNotThrow(() => H.Trio.notifications.playPreset('nonexistent-preset', 0.5));
  assert.doesNotThrow(() => H.Trio.notifications.playPreset('ping', 0)); // muted — should no-op, not throw
});

// LOTC/Sauron: the priming guard used to be a single timer-based flag shared
// across the per-channel AND cross-channel SSE streams — a reconnect on
// EITHER could silently suppress a genuinely live chime on an unrelated,
// already-open channel. isPrimedHistory() replaces that with a per-message
// age check (immune to which stream delivered it, no shared state at all).
check('isPrimedHistory: a message from long ago is primed history', () => {
  const old = new Date(Date.now() - 60_000).toISOString();
  assert.strictEqual(H.Trio.notifications.isPrimedHistory({ created_at: old }), true);
});
check('isPrimedHistory: a message from right now is live, not primed', () => {
  const now = new Date().toISOString();
  assert.strictEqual(H.Trio.notifications.isPrimedHistory({ created_at: now }), false);
});
check('isPrimedHistory: missing/unparseable created_at fails open (treated as live)', () => {
  assert.strictEqual(H.Trio.notifications.isPrimedHistory({}), false);
  assert.strictEqual(H.Trio.notifications.isPrimedHistory({ created_at: 'not-a-date' }), false);
});
check('notifications: real message-event dispatch never throws for an old or a live DM', () => {
  // onMessage's internal calls to classify()/isPrimedHistory()/playPreset are
  // closures over the module's own function bindings, not live reads of
  // Trio.notifications — so this can't spy on playPreset from the outside.
  // What IS worth asserting end-to-end: dispatching a real 'message' event
  // (the actual production code path, not just calling classify() directly)
  // never throws for either an old or a live message, for every field shape
  // onMessage touches (recipients/mentions/refs/bangs/created_at).
  H.Trio.state.operator = { id: 'me' };
  H.Trio.preferences.save({ chime: true, chimeTierDm: true });
  const live = { id: 101, member_id: 'ag1', recipients: ['me'], mentions: [], refs: [], bangs: [], created_at: new Date().toISOString() };
  const old = { id: 100, member_id: 'ag1', recipients: ['me'], mentions: [], refs: [], bangs: [], created_at: new Date(Date.now() - 60_000).toISOString() };
  assert.strictEqual(H.Trio.notifications.classify(old, 'me'), 'dm');
  assert.strictEqual(H.Trio.notifications.isPrimedHistory(old), true);
  assert.strictEqual(H.Trio.notifications.classify(live, 'me'), 'dm');
  assert.strictEqual(H.Trio.notifications.isPrimedHistory(live), false);
  assert.doesNotThrow(() => H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail: old })));
  assert.doesNotThrow(() => H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail: live })));
});

// Cross-channel chimes: the workspace-wide SSE stream (06-core.js's
// startWorkspaceEvents, operator-only) delivers messages from every channel,
// not just the one currently open — including the open channel itself,
// which the per-channel stream ALSO delivers. Two things follow: (1) a
// message not in the currently-open channel deserves a desktop popup
// regardless of tab focus (chime already fires unconditionally either way),
// (2) the open channel's own messages must not double-fire from arriving on
// both streams.
check('notifications: a message in a channel you are NOT viewing pops up even with the tab focused', () => {
  const created = [];
  cx.window.Notification = function (title, opts) { created.push({ title, opts }); return { close() {} }; };
  cx.window.Notification.permission = 'granted';
  H.Trio.state.operator = { id: 'me' };
  H.Trio.state.channel = 'chanA';
  H.Trio.preferences.save({ notifications: true, notifyTierDm: true, chime: false });
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail: {
    id: 301, member_id: 'ag1', recipients: ['me'], mentions: [], refs: [], bangs: [],
    created_at: new Date().toISOString(), channel: 'chanB', member_name: 'Bob', content: 'hi',
  } }));
  assert.strictEqual(created.length, 1);
  assert.ok(created[0].title.includes('DM'));
});
check('notifications: a message in the currently-open channel does NOT pop up while the tab is focused', () => {
  const created = [];
  cx.window.Notification = function (title, opts) { created.push({ title, opts }); return { close() {} }; };
  cx.window.Notification.permission = 'granted';
  H.Trio.state.operator = { id: 'me' };
  H.Trio.state.channel = 'chanA';
  H.Trio.preferences.save({ notifications: true, notifyTierDm: true, chime: false });
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail: {
    id: 302, member_id: 'ag1', recipients: ['me'], mentions: [], refs: [], bangs: [],
    created_at: new Date().toISOString(), channel: 'chanA', member_name: 'Bob', content: 'hi',
  } }));
  assert.strictEqual(created.length, 0);
});
check('notifications: the SAME message id delivered twice (per-channel + workspace stream) only fires once', () => {
  const created = [];
  cx.window.Notification = function (title, opts) { created.push({ title, opts }); return { close() {} }; };
  cx.window.Notification.permission = 'granted';
  H.Trio.state.operator = { id: 'me' };
  H.Trio.state.channel = 'chanA';
  H.Trio.preferences.save({ notifications: true, notifyTierDm: true, chime: false });
  const detail = {
    id: 303, member_id: 'ag1', recipients: ['me'], mentions: [], refs: [], bangs: [],
    created_at: new Date().toISOString(), channel: 'chanB', member_name: 'Bob', content: 'hi',
  };
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail }));
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail }));
  assert.strictEqual(created.length, 1);
});

// Cross-channel LEAK guard: the workspace-wide SSE stream multiplexes every
// channel's messages AND roster through the same event target so notifications
// can chime for other channels. The conversation view, which renders only the
// open channel, must drop foreign-channel events — otherwise another channel's
// message renders in this one and another channel's roster (worse,
// AGENT_INBOX_CHANNEL's full agent list) overwrites the face-pile/sidebar.
// The harness suppresses boot, so onMessage/onRoster are NOT auto-registered —
// call conversation.init() to wire the real dispatch → onMessage → ingest →
// upsert path these tests must exercise. (Trio.events is a real EventTarget,
// which dedupes an identical (type, fn) pair across repeated init() calls.)
check('conversation: a message from another channel does NOT render in the open channel', () => {
  H.Trio.conversation.init();
  H.Trio.state.channel = 'atrium-test4';
  delete H.Trio.state.dmKey;
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail: {
    id: 9101, member_id: 'ag_stag', channel: 'other-channel',
    content: 'leaked from elsewhere', created_at: new Date().toISOString(),
  } }));
  assert.strictEqual(H.Trio.state.messages.has(9101), false,
    'foreign-channel message must not enter the open conversation');
});
check('conversation: a message for the open channel (or with no channel stamp) still renders', () => {
  H.Trio.state.channel = 'atrium-test4';
  delete H.Trio.state.dmKey;
  H.upsert({ id: 9102, member_id: 'ag_1', channel: 'atrium-test4', content: 'mine' });
  H.upsert({ id: 9103, member_id: 'ag_1', content: 'unstamped-legacy' });
  assert.strictEqual(H.Trio.state.messages.has(9102), true);
  assert.strictEqual(H.Trio.state.messages.has(9103), true);
});
check('conversation: a foreign-channel edit cannot overwrite an existing open-channel message', () => {
  H.Trio.state.channel = 'atrium-test4';
  delete H.Trio.state.dmKey;
  H.upsert({ id: 9110, member_id: 'ag_1', channel: 'atrium-test4', content: 'original' });
  // A message_update stamped for another channel must be dropped, not applied.
  H.upsert({ id: 9110, member_id: 'ag_1', channel: 'other-channel', content: 'tampered', edited_at: 'now' });
  assert.strictEqual(H.Trio.state.messages.get(9110).content, 'original');
});
check('conversation: in a DM view a message is scoped by recipients and ignores its channel stamp', () => {
  H.Trio.state.channel = 'nth-agent-inbox';
  H.Trio.state.operator = { id: 'op' };
  H.Trio.state.dmKey = 'ag_1';
  H.Trio.state.dmMemberIds = ['ag_1'];
  // channel is neither the DM's channel nor unrelated to matter — the dmKey
  // branch never inspects it; recipients decide. (Guards the "DM views are
  // scoped by recipients" contract the channel guard's else-branch relies on.)
  H.upsert({ id: 9120, member_id: 'ag_1', recipients: ['op'], channel: 'totally-unrelated', content: 'dm ignores channel' });
  assert.strictEqual(H.Trio.state.messages.has(9120), true);
  delete H.Trio.state.dmKey; delete H.Trio.state.dmMemberIds;
});
check('conversation + notifications: a foreign-channel message is dropped from the view but still chimes/notifies', () => {
  H.Trio.conversation.init();
  const created = [];
  cx.window.Notification = function (title, opts) { created.push({ title, opts }); return { close() {} }; };
  cx.window.Notification.permission = 'granted';
  H.Trio.state.operator = { id: 'me' };
  H.Trio.state.channel = 'chanA';
  delete H.Trio.state.dmKey;
  H.Trio.preferences.save({ notifications: true, notifyTierDm: true, chime: false });
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail: {
    id: 9130, member_id: 'ag1', recipients: ['me'], mentions: [], refs: [], bangs: [],
    created_at: new Date().toISOString(), channel: 'chanB', member_name: 'Bob', content: 'hi',
  } }));
  // Both listeners share the same event target: the conversation view drops it
  // (foreign channel) while the notification module still fires — the whole
  // reason the guard lives in the listener, not in dispatch().
  assert.strictEqual(H.Trio.state.messages.has(9130), false, 'foreign message must not render in the open channel');
  assert.strictEqual(created.length, 1, 'but it must still produce a cross-channel notification');
});
// These exercise the conversation module's OWN 'roster' listener (onRoster) —
// the listener that re-derived state.members from the raw event detail and so
// re-bypassed the guard in 04-events.js::dispatch. The harness suppresses boot,
// so wire the listeners explicitly with init() before dispatching.
check('conversation onRoster: a roster for another channel does NOT replace the open channel members', () => {
  H.Trio.conversation.init();
  H.Trio.state.channel = 'atrium-test4';
  delete H.Trio.state.dmKey;
  H.Trio.state.members = new Map([['ag_cedar', { id: 'ag_cedar', name: 'Cedar' }]]);
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('roster', { detail: {
    channel: 'nth-agent-inbox',
    members: [{ id: 'ag_a', name: 'Aragorn' }, { id: 'ag_b', name: 'Boromir' }],
  } }));
  assert.strictEqual(H.Trio.state.members.size, 1,
    'foreign-channel roster must not clobber the open channel member list');
  assert.ok(H.Trio.state.members.has('ag_cedar'));
});
check('conversation onRoster: a roster for the open channel replaces its members', () => {
  H.Trio.conversation.init();
  H.Trio.state.channel = 'atrium-test4';
  delete H.Trio.state.dmKey;
  H.Trio.state.members = new Map([['old', { id: 'old', name: 'stale' }]]);
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('roster', { detail: {
    channel: 'atrium-test4',
    members: [{ id: 'ag_cedar', name: 'Cedar' }, { id: 'op', name: 'exampleuser' }],
  } }));
  assert.strictEqual(H.Trio.state.members.size, 2);
  assert.ok(H.Trio.state.members.has('ag_cedar') && !H.Trio.state.members.has('old'));
});

// Per-conversation mute: LOTC/Frodo found the "Mute notifications" menu item
// was a stub (toasted "muted", did nothing) — a broken promise cross-channel
// chimes made materially worse. toggleMute/isMuted/conversationKeyFor make it
// real.
check('mute: toggleMute flips and persists; isMuted reflects it', () => {
  assert.strictEqual(H.Trio.notifications.isMuted('chanX'), false);
  assert.strictEqual(H.Trio.notifications.toggleMute('chanX'), true);
  assert.strictEqual(H.Trio.notifications.isMuted('chanX'), true);
  assert.strictEqual(H.Trio.notifications.toggleMute('chanX'), false);
  assert.strictEqual(H.Trio.notifications.isMuted('chanX'), false);
});
check('mute: an empty/falsy key is never considered muted and toggle no-ops', () => {
  assert.strictEqual(H.Trio.notifications.isMuted(''), false);
  assert.strictEqual(H.Trio.notifications.isMuted(undefined), false);
  assert.strictEqual(H.Trio.notifications.toggleMute(''), false);
});
check('mute: a muted channel suppresses BOTH chime and desktop popup regardless of tier settings', () => {
  const created = [];
  cx.window.Notification = function (title, opts) { created.push({ title, opts }); return { close() {} }; };
  cx.window.Notification.permission = 'granted';
  H.Trio.state.operator = { id: 'me' };
  H.Trio.state.channel = 'chanA'; // viewing a different channel — would otherwise pop up
  H.Trio.preferences.save({ notifications: true, notifyTierDm: true, chime: true, chimeTierDm: true });
  H.Trio.notifications.toggleMute('chanMuted');
  H.Trio.events.dispatchEvent(new cx.window.CustomEvent('message', { detail: {
    id: 401, member_id: 'ag1', recipients: ['me'], mentions: [], refs: [], bangs: [],
    created_at: new Date().toISOString(), channel: 'chanMuted', member_name: 'Bob', content: 'hi',
  } }));
  assert.strictEqual(created.length, 0);
  H.Trio.notifications.toggleMute('chanMuted'); // clean up for later tests
});
check('conversationKeyFor: resolves a channel message to its channel code', () => {
  assert.strictEqual(H.Trio.notifications.conversationKeyFor({ channel: 'general', recipients: [] }), 'general');
});
check('conversationKeyFor: resolves a DM message to dm:<key> by matching the participant set', () => {
  H.Trio.state.dms = { your_dms: [{ key: 'dm-1', member_ids: ['me', 'ag1'] }] };
  const key = H.Trio.notifications.conversationKeyFor({ member_id: 'ag1', recipients: ['me'], channel: 'nth-agent-inbox' });
  assert.strictEqual(key, 'dm:dm-1');
});
check('conversationKeyFor: an unresolvable DM (no matching thread loaded yet) falls back to the channel code, not a crash', () => {
  H.Trio.state.dms = { your_dms: [] };
  assert.doesNotThrow(() => H.Trio.notifications.conversationKeyFor({ member_id: 'ag1', recipients: ['me'], channel: 'nth-agent-inbox' }));
  assert.strictEqual(H.Trio.notifications.conversationKeyFor({ member_id: 'ag1', recipients: ['me'], channel: 'nth-agent-inbox' }), 'nth-agent-inbox');
});

// Avatar tone collision avoidance: two separate copies of the same bare
// hash (sum of char codes mod 5) meant two members could land on the
// identical tone purely by chance (reported: two agents both showing
// plum). Trio.avatarTones mirrors nth_constants.py's animal_for_channel —
// hash picks a preferred slot, linear-probing to the next free one only on
// an actual collision — so every tone in the pool is used before any repeat.
check('avatarTones: every tone in the pool is used before any repeats, up to pool size', () => {
  // 5 labels chosen so at least two would hash to the same slot under the
  // bare "sum of char codes mod 5" scheme (this is the actual bug report).
  const labels = ['Clover', 'Stag', 'Frost', 'Gale', 'Delta'];
  const assignment = H.Trio.avatarTones(labels);
  const tones = labels.map(label => assignment.get(label));
  assert.strictEqual(new Set(tones).size, 5, 'expected all 5 distinct tones, got: ' + tones.join(','));
});
check('avatarTones: a 6th label beyond the pool size must repeat (only 5 tones exist)', () => {
  const labels = ['Clover', 'Stag', 'Frost', 'Gale', 'Delta', 'Sixth'];
  const assignment = H.Trio.avatarTones(labels);
  assert.strictEqual(new Set(labels.map(l => assignment.get(l))).size, 5);
});
check('avatarTones: deterministic — the same label set always resolves the same way', () => {
  const labels = ['Clover', 'Stag', 'Frost'];
  const a = H.Trio.avatarTones(labels);
  const b = H.Trio.avatarTones([...labels].reverse()); // input order must not matter — sorted internally
  for (const label of labels) assert.strictEqual(a.get(label), b.get(label));
});
check('avatarTones: falsy/duplicate labels are ignored, never crash', () => {
  assert.doesNotThrow(() => H.Trio.avatarTones([]));
  assert.doesNotThrow(() => H.Trio.avatarTones([null, undefined, '', 'Clover', 'Clover']));
  assert.strictEqual(H.Trio.avatarTones([null, undefined, '', 'Clover', 'Clover']).size, 1);
});
check('avatarTone: two known agents never share a tone (the reported bug)', () => {
  H.Trio.state.members = new Map();
  H.Trio.state.agents = [{ id: 'ag_clover' }, { id: 'ag_stag' }];
  H.Trio.state.operator = null;
  assert.notStrictEqual(H.Trio.avatarTone('ag_clover'), H.Trio.avatarTone('ag_stag'));
});
check('avatarTone: tolerates state.agents in its pre-refresh {list,...} shape (not yet a flat array)', () => {
  H.Trio.state.members = new Map();
  H.Trio.state.agents = { list: [{ id: 'ag_x' }], selected: null, loading: false };
  assert.doesNotThrow(() => H.Trio.avatarTone('ag_x'));
});

// Cross-channel roster clobber: the workspace-wide SSE stream multiplexes
// EVERY channel's roster events into one connection, alongside the
// per-channel stream for whatever's currently open. A roster event used to
// overwrite Trio.state.members unconditionally regardless of which channel
// it was for — so viewing channel A while channel B's (or, worse,
// AGENT_INBOX_CHANNEL's — every agent ever created) roster ticked would
// silently replace A's member list. Reported live: a viewer saw the
// participant list "keep changing around", including a flash of every
// agent ever created.
check('roster clobber: a roster event for a DIFFERENT channel does NOT overwrite state.members', () => {
  H.Trio.state.channel = 'chanA';
  H.Trio.state.members = new Map([['real1', { id: 'real1', name: 'Real Member' }]]);
  H.Trio.dispatchSSEEvent({ type: 'roster', channel: 'nth-agent-inbox', members: [
    { id: 'ag1', name: 'Agent One' }, { id: 'ag2', name: 'Agent Two' },
  ] });
  assert.deepStrictEqual([...H.Trio.state.members.keys()], ['real1']);
});
check('roster clobber: a roster event for the CURRENTLY open channel is applied normally', () => {
  H.Trio.state.channel = 'chanA';
  H.Trio.state.members = new Map([['stale', { id: 'stale', name: 'Stale' }]]);
  H.Trio.dispatchSSEEvent({ type: 'roster', channel: 'chanA', members: [
    { id: 'fresh', name: 'Fresh Member' },
  ] });
  assert.deepStrictEqual([...H.Trio.state.members.keys()], ['fresh']);
});
check('roster clobber: a roster event missing the channel field fails closed (not applied)', () => {
  H.Trio.state.channel = 'chanA';
  H.Trio.state.members = new Map([['real1', { id: 'real1', name: 'Real Member' }]]);
  H.Trio.dispatchSSEEvent({ type: 'roster', members: [{ id: 'ghost', name: 'Ghost' }] });
  assert.deepStrictEqual([...H.Trio.state.members.keys()], ['real1']);
});
check('roster clobber: the roster CustomEvent still dispatches even when not applied to state.members', () => {
  let seen = null;
  const handler = event => { seen = event.detail; };
  H.Trio.events.addEventListener('roster', handler);
  try {
    H.Trio.state.channel = 'chanA';
    H.Trio.dispatchSSEEvent({ type: 'roster', channel: 'chanB', members: [{ id: 'x' }] });
    assert.ok(seen, 'roster event should still fire for listeners that want to filter themselves');
    assert.strictEqual(seen.channel, 'chanB');
  } finally {
    H.Trio.events.removeEventListener('roster', handler);
  }
});

// Connection pill: for the operator the workspace-wide stream is the live feed,
// so the pill must follow IT — otherwise landing on Home / an inbox DM (no
// per-channel stream) leaves the pill stuck "offline" while events flow. Bug:
// startWorkspaceEvents never called setConnection, so a live workspace stream
// couldn't clear the offline set by startEvents(no-channel).
check('connection pill: workspace heartbeat, not socket open, proves it live', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  H.Trio.startWorkspaceEvents();
  const ws = ES.instances.find(s => String(s.url).includes('/api/workspace/events'));
  assert.ok(ws, 'workspace EventSource should be created');
  ws.fireOpen();
  const conn = cx.document.getElementById('h-conn');
  assert.ok(!String(conn.className).includes('live'), 'open-but-silent stream must not claim live');
  ws.fireHeartbeat();
  assert.ok(String(conn.className).includes('live') && !String(conn.className).includes('offline'),
    'workspace heartbeat should set the pill live, got: ' + conn.className);
});
check('connection pill: no per-channel stream does NOT clobber a live workspace pill', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  H.Trio.startWorkspaceEvents();
  const ws = ES.instances.find(s => String(s.url).includes('/api/workspace/events'));
  ws.fireOpen(); ws.fireHeartbeat();
  H.Trio.startEvents('');            // Home view / inbox DM — no channel
  const conn = cx.document.getElementById('h-conn');
  assert.ok(!String(conn.className).includes('offline'),
    'no-channel must not flip a live workspace pill offline: ' + conn.className);
  assert.ok(String(conn.className).includes('live'));
});
check('connection pill: workspace error shows reconnecting, then recovers to live', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  H.Trio.startWorkspaceEvents();
  const ws = ES.instances.find(s => String(s.url).includes('/api/workspace/events'));
  ws.fireOpen(); ws.fireHeartbeat(); ws.fireError();
  const conn = cx.document.getElementById('h-conn');
  assert.ok(String(conn.className).includes('reconnect'), 'workspace error should show reconnecting: ' + conn.className);
  ws.fireOpen(); ws.fireHeartbeat();
  assert.ok(String(conn.className).includes('live') && !String(conn.className).includes('offline'));
  H.Trio.stopWorkspaceEvents();       // reset module workspaceLive for later tests
});
check('connection freshness: an open-but-silent workspace stream is restarted', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  H.Trio.startWorkspaceEvents();
  const first = ES.instances.find(s => String(s.url).includes('/api/workspace/events'));
  first.fireOpen();
  assert.strictEqual(H.Trio.checkEventFreshness(Date.now() + 46_000), true);
  assert.ok(first.closed, 'stale stream should be closed');
  assert.strictEqual(ES.instances.filter(s => String(s.url).includes('/api/workspace/events')).length, 2);
  H.Trio.stopWorkspaceEvents();
});
check('connection freshness: an open-but-silent channel-only stream is restarted', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  H.Trio.startEvents('guest-channel');
  const first = ES.instances[0]; first.fireOpen(); first.fireHeartbeat();
  assert.strictEqual(H.Trio.checkEventFreshness(Date.now() + 46_000), true);
  assert.ok(first.closed, 'stale channel-only stream should be closed');
  assert.strictEqual(ES.instances.length, 2);
  H.Trio.stopEvents();
});
check('connection freshness: slow connect gets a fresh heartbeat deadline on open', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  const realNow = cx.window.Date.now;
  let now = 1_000;
  cx.window.Date.now = () => now;
  try {
    H.Trio.startWorkspaceEvents();
    const ws = ES.instances[0];
    now = 40_000; ws.fireOpen();
    assert.strictEqual(H.Trio.checkEventFreshness(50_000), false,
      'successful open should reset the proof deadline');
  } finally {
    cx.window.Date.now = realNow;
    H.Trio.stopWorkspaceEvents();
  }
});
check('connection freshness: native reconnect resets old receipt proof for both feeds', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  const realNow = cx.window.Date.now;
  let now = 1_000;
  cx.window.Date.now = () => now;
  try {
    H.Trio.startWorkspaceEvents(); H.Trio.startEvents('chanA');
    const ws = ES.instances[0], ch = ES.instances[1];
    ws.fireOpen(); ws.fireHeartbeat(); ch.fireOpen(); ch.fireHeartbeat();
    now = 70_000; ws.fireOpen(); ch.fireOpen();
    assert.strictEqual(H.Trio.checkEventFreshness(70_001), false,
      'native reopen should get a new proof deadline');
    assert.strictEqual(ES.instances.length, 2, 'native reopen must not cause explicit replacements');
  } finally {
    cx.window.Date.now = realNow;
    H.Trio.stopEvents(); H.Trio.stopWorkspaceEvents();
  }
});
check('connection freshness: a healthy workspace feed cannot mask a stale channel feed', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  const realNow = cx.window.Date.now;
  let now = 1_000;
  cx.window.Date.now = () => now;
  try {
    H.Trio.startWorkspaceEvents(); H.Trio.startEvents('cold-channel');
    const ws = ES.instances[0], ch = ES.instances[1];
    ws.fireOpen(); ws.fireHeartbeat(); ch.fireOpen(); ch.fireHeartbeat();
    now = 50_000; ws.fireHeartbeat();
    assert.strictEqual(H.Trio.checkEventFreshness(50_001), true);
    assert.ok(ch.closed, 'stale channel should be replaced');
    assert.ok(!ws.closed, 'fresh workspace should be retained');
    assert.strictEqual(ES.instances.length, 3, 'only the stale channel feed should restart');
  } finally {
    cx.window.Date.now = realNow;
    H.Trio.stopEvents(); H.Trio.stopWorkspaceEvents();
  }
});
check('connection freshness: returning visible restarts workspace and channel streams once', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  H.Trio.startWorkspaceEvents(); H.Trio.startEvents('chanA');
  const before = ES.instances.slice();
  cx.document.hidden = true; cx.document.visibilityState = 'hidden';
  cx.document.dispatchEvent(new Event('visibilitychange'));
  assert.strictEqual(ES.instances.length, before.length, 'hidden transition must not restart streams');
  cx.document.hidden = false; cx.document.visibilityState = 'visible';
  cx.document.dispatchEvent(new Event('visibilitychange'));
  assert.strictEqual(ES.instances.length, before.length + 2, 'visible transition restarts both required streams');
  cx.window.dispatchEvent({ type: 'pageshow', persisted: true });
  cx.window.dispatchEvent(new Event('online'));
  assert.strictEqual(ES.instances.length, before.length + 2, 'clustered resume signals must coalesce');
  before.forEach(source => assert.ok(source.closed, 'superseded stream should be closed'));
  H.Trio.stopEvents(); H.Trio.stopWorkspaceEvents();
});
check('connection freshness: callbacks from a replaced stream cannot overwrite live state', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  H.Trio.startWorkspaceEvents();
  const old = ES.instances[0]; old.fireOpen();
  H.Trio.startWorkspaceEvents();
  const current = ES.instances[1]; current.fireOpen(); current.fireHeartbeat();
  old.fireError();
  const conn = cx.document.getElementById('h-conn');
  assert.ok(String(conn.className).includes('live') && !String(conn.className).includes('reconnect'),
    'late error from closed generation must be ignored');
  H.Trio.stopWorkspaceEvents();
});
check('connection freshness: online while hidden does not consume foreground recovery', () => {
  const ES = cx.window.EventSource; ES.instances.length = 0;
  H.Trio.startWorkspaceEvents(); H.Trio.startEvents('chanA');
  const before = ES.instances.slice();
  cx.document.hidden = true; cx.document.visibilityState = 'hidden';
  cx.document.dispatchEvent(new Event('visibilitychange'));
  cx.window.dispatchEvent(new Event('online'));
  assert.strictEqual(ES.instances.length, before.length, 'hidden online event must not restart or start cooldown');
  cx.document.hidden = false; cx.document.visibilityState = 'visible';
  cx.document.dispatchEvent(new Event('visibilitychange'));
  assert.strictEqual(ES.instances.length, before.length + 2, 'foreground transition must still restart both feeds');
  before.forEach(source => assert.ok(source.closed));
  H.Trio.stopEvents(); H.Trio.stopWorkspaceEvents();
});

// The workspace refresh loop (15s interval + on-message) must re-poll
// /api/agents so state.agents — read by the drawer, face-pile, and roster card
// — stays fresh between Agent-roster page visits, and repaint the face-pile.
async function checkAsync(name, fn) {
  try { await fn(); console.log('PASS: ' + name); }
  catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); }
}
(async () => {
  await checkAsync('workspace refresh re-polls /api/agents (keeps state.agents fresh)', async () => {
    let agentsRefreshed = 0;
    const realAgentsRefresh = H.Trio.agents.refresh;
    const realGet = H.Trio.api.get;
    H.Trio.agents.refresh = () => { agentsRefreshed++; return Promise.resolve(); };
    H.Trio.api.get = () => Promise.resolve({ channels: [], your_dms: [], tasks: [], approvals: [], questions: [], mentions: [] });
    H.Trio.state.channel = 'chanA';
    H.Trio.state.agents = [];
    delete H.Trio.state.dmKey;
    try {
      await H.Trio.workspace.refresh();
      assert.ok(agentsRefreshed >= 1, 'workspace refresh should re-poll agents (got ' + agentsRefreshed + ')');
    } finally {
      H.Trio.agents.refresh = realAgentsRefresh;
      H.Trio.api.get = realGet;
    }
  });

  console.log((failures ? 'FAILED' : 'OK') + ' — ' + failures + ' failure(s)');
  process.exit(failures ? 1 : 0);
})();
