// Create-agent defaults: what a picker shows before anyone touches it.
//
// These are product decisions, not incidental behaviour, and every one of them
// was wrong in a way that looked plausible on screen:
//
//   * the provider list came from /api/health's `runtimes`, which is hardcoded
//     to claude — so Codex never appeared however well it worked;
//   * providers rendered as raw ids ("claude", "codex");
//   * the effort slider led with an unset step labelled "Default", naming a
//     behaviour nobody could state, and the model list fell back to a
//     hardcoded pair of stale ids when discovery failed.
//
// The rule now: a control always shows a REAL value, resolved as
//   what you chose last  ->  what the model itself runs  ->  the first-run
//   preference (Claude / Opus / medium).
//
// Usage: node tests/test-agent-defaults.js
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
const Trio = cx.hooks.Trio;
const A = Trio.agents;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

// A realistic catalogue: Claude ordered most-capable-first, Codex as the App
// Server reports it, with ladders that genuinely differ.
Trio.state.agentModels = {
  claude: [
    { id: 'fable', name: 'Fable', efforts: ['low', 'medium', 'high', 'max'] },
    { id: 'opus', name: 'Opus', efforts: ['low', 'medium', 'high', 'max'] },
    { id: 'sonnet', name: 'Sonnet', efforts: ['low', 'medium', 'high', 'max'], default: true },
    { id: 'haiku', name: 'Haiku', efforts: ['low', 'medium', 'high'] },
  ],
  codex: [
    { id: 'gpt-5.6-sol', name: 'GPT-5.6-Sol', efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], default_effort: 'low' },
    { id: 'gpt-5.6-luna', name: 'GPT-5.6-Luna', efforts: ['low', 'medium', 'high', 'xhigh', 'max'], default_effort: 'medium' },
  ],
};

check('the first-run preference is Claude / Opus / medium', () => {
  assert.strictEqual(A.FIRST_RUN.provider, 'claude');
  assert.strictEqual(A.FIRST_RUN.model, 'opus');
  assert.strictEqual(A.FIRST_RUN.effort, 'medium');
});

check('Claude is offered first however the hub orders its providers', () => {
  assert.strictEqual(A.orderedProviders(['codex', 'claude'])[0], 'claude');
  assert.strictEqual(A.orderedProviders(['claude', 'codex'])[0], 'claude');
});

check('both providers survive ordering — Codex is not dropped', () => {
  assert.strictEqual(A.orderedProviders(['codex', 'claude']).sort().join(','),
                     'claude,codex');
});

check('providers are shown by name, not by id', () => {
  assert.strictEqual(A.providerLabel('claude'), 'Claude');
  assert.strictEqual(A.providerLabel('codex'), 'Codex');
});

check('an unknown provider still gets a readable label rather than blank', () => {
  assert.strictEqual(A.providerLabel('gemini'), 'Gemini');
});

// ── effort resolution ────────────────────────────────────────────────────
check('first run lands on medium when the model offers it', () => {
  assert.strictEqual(A.initialEffortFor('claude', 'opus'), 'medium');
});

check("the model's OWN default wins over the first-run preference", () => {
  // Sol reports default_effort 'low'; that is a statement about the model and
  // should beat a generic preference.
  assert.strictEqual(A.initialEffortFor('codex', 'gpt-5.6-sol'), 'low');
});

check('what the operator chose last beats everything', () => {
  A.rememberEffort('codex', 'gpt-5.6-sol', 'ultra');
  assert.strictEqual(A.initialEffortFor('codex', 'gpt-5.6-sol'), 'ultra');
});

check('a remembered level the model no longer offers is not used', () => {
  A.rememberEffort('claude', 'haiku', 'max');    // Haiku has no max
  const got = A.initialEffortFor('claude', 'haiku');
  assert.ok(['low', 'medium', 'high'].includes(got),
    `resolved to ${got}, which Haiku does not offer`);
});

check('an undiscovered model resolves to nothing rather than inventing one', () => {
  assert.strictEqual(A.initialEffortFor('claude', 'no-such-model'), '');
});

// ── the slider itself ────────────────────────────────────────────────────
check('the slider has no "Default" step — every stop is a real level', () => {
  const html = A.effortSlider(['low', 'medium', 'high', 'max'], 'medium');
  assert.ok(!/Default/i.test(html), 'a Default step survived: ' + html);
  assert.ok(/Medium/.test(html));
});

check('the slider opens on the resolved value, not on the lowest level', () => {
  const html = A.effortSlider(['low', 'medium', 'high', 'max'], 'medium');
  const hidden = /name="effort" value="([^"]*)"/.exec(html);
  assert.strictEqual(hidden && hidden[1], 'medium',
    'the submitted value must be the resolved one — opening on the lowest '
    + 'level is the silent downgrade this replaced');
});

check("an agent's existing level survives even if discovery is thin", () => {
  const html = A.effortSlider([], 'ultra');
  assert.ok(/ultra/.test(html));
});

check('the model list can pre-select, so the form opens on Opus', () => {
  const html = A.modelOptions(Trio.state.agentModels.claude, 'opus');
  assert.ok(/value="opus" selected/.test(html), html);
  assert.ok(!/value="fable" selected/.test(html));
});

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
process.exit(failures.length ? 1 : 0);
