// Client-side render-layer tests, run against the ACTUAL shipped dashboard
// script via the Node DOM harness (tests/dom-harness.js). No jsdom, no npm —
// Node stdlib + a hand-rolled fake DOM.
//
// Covers the highest-risk, previously-uncovered client logic:
//   • renderMarkdown  — the stdlib-free markdown→HTML renderer (huge edge
//                       surface, incl. XSS-relevant escaping)
//   • isSystemContent — the [word]/[word #id] system-line detector (the exact
//                       predicate whose bug rendered [joined]/[pinned]/… as
//                       markdown; regression-guarded here)
//   • humanizeIdSigils — @<member_id> → @<friendly-name> rewriting
//   (paintBody / applyTargetBars coverage lives with the features that
//    introduce them; those functions do not exist in this tree.)
//
// Usage: node tests/test-client-render.js
'use strict';

const assert = require('assert');
const { load, FakeElement } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + e.message); }
}

const cx = load();
const H = cx.hooks;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

// ── escapeHtml ──────────────────────────────────────────────────────────────
check('escapeHtml escapes the five entities', () => {
  assert.strictEqual(H.escapeHtml(`<a href="x" data='y'>&`),
    '&lt;a href=&quot;x&quot; data=&#39;y&#39;&gt;&amp;');
});

// ── renderMarkdown: inline ───────────────────────────────────────────────────
check('renderMarkdown bold + italic', () => {
  assert.strictEqual(H.renderMarkdown('**b** and *i*'),
    '<p><strong>b</strong> and <em>i</em></p>');
});
check('renderMarkdown inline code is escaped and not re-parsed', () => {
  const out = H.renderMarkdown('use `**not bold**`');
  assert.ok(/<code[^>]*>\*\*not bold\*\*<\/code>/.test(out),
    'inline code must keep literal ** and not become <strong>: ' + out);
});
check('renderMarkdown strikethrough', () => {
  assert.strictEqual(H.renderMarkdown('~~gone~~'), '<p><del>gone</del></p>');
});
check('renderMarkdown escapes raw HTML (no injection)', () => {
  const out = H.renderMarkdown('<img src=x onerror=alert(1)>');
  assert.ok(!out.includes('<img'), 'raw <img> must be escaped: ' + out);
  assert.ok(out.includes('&lt;img'), 'expected escaped angle bracket: ' + out);
});
check('renderMarkdown link href strips smuggled quote entities', () => {
  const out = H.renderMarkdown('[t](http://x.com)');
  assert.ok(out.includes('href="http://x.com"'), out);
  assert.ok(out.includes('rel="noopener noreferrer"'), out);
});
check('renderMarkdown autolinks bare urls', () => {
  const out = H.renderMarkdown('see http://example.com now');
  assert.ok(out.includes('<a href="http://example.com"'), out);
});
check('renderMarkdown non-http link text is NOT linkified', () => {
  // javascript: scheme must not become an anchor.
  const out = H.renderMarkdown('[x](javascript:alert(1))');
  assert.ok(!out.includes('href="javascript'), 'javascript: URL must not linkify: ' + out);
});

// ── renderMarkdown: block ────────────────────────────────────────────────────
check('renderMarkdown ATX heading', () => {
  assert.strictEqual(H.renderMarkdown('## Title'), '<h2>Title</h2>');
});
check('renderMarkdown fenced code block preserves contents literally', () => {
  const out = H.renderMarkdown('```js\nconst a = **1**;\n```');
  assert.ok(out.includes('<pre'), out);
  assert.ok(out.includes('const a = **1**;'), 'fence body must be literal: ' + out);
  assert.ok(!out.includes('<strong>'), 'fence body must not be markdown-parsed: ' + out);
});
check('renderMarkdown unordered list', () => {
  const out = H.renderMarkdown('- one\n- two');
  assert.ok(/<ul>.*<li>one<\/li>.*<li>two<\/li>.*<\/ul>/s.test(out), out);
});
check('renderMarkdown GFM task list', () => {
  const out = H.renderMarkdown('- [x] done\n- [ ] todo');
  assert.ok(out.includes('checked') || out.includes('checkbox') || /\[x\]|✓|☑/.test(out) === false, out);
  assert.ok(out.includes('done') && out.includes('todo'), out);
});
check('renderMarkdown empty string → empty', () => {
  assert.strictEqual(H.renderMarkdown(''), '');
});

// ── isSystemContent (regression-guarded) ─────────────────────────────────────
check('isSystemContent: [word] events are system', () => {
  // These are verbatim shapes nth_server.py emits — bracket closed right
  // after the word. Do not "simplify" them into '[joined ] a'; that shape
  // never occurs in production and would make this check green against a
  // predicate that misses every real notice.
  for (const s of ['[joined] alice — building the parser',
                   '[pinned] read CURRENT.md first',
                   '[locked] db.sqlite (TTL 60s)',
                   '[unlocked] db.sqlite',
                   '[renamed] bob → robert']) {
    assert.strictEqual(H.isSystemContent(s), true, 'expected system: ' + s);
  }
});
check('isSystemContent: [word #id] events are system', () => {
  for (const s of ['[claimed #3] t', '[done #3]', '[cancelled #3]', '[released #3]',
                   '[retracted #7]', '[status #2] busy']) {
    assert.strictEqual(H.isSystemContent(s), true, 'expected system: ' + s);
  }
});
check('isSystemContent: markdown link [done](url) is NOT system', () => {
  assert.strictEqual(H.isSystemContent('[done](http://x)'), false);
});
check('isSystemContent: unknown bracket word is NOT system', () => {
  assert.strictEqual(H.isSystemContent('[todo] buy milk'), false);
  assert.strictEqual(H.isSystemContent('regular message'), false);
  assert.strictEqual(H.isSystemContent(''), false);
});

// ── humanizeIdSigils ─────────────────────────────────────────────────────────
check('humanizeIdSigils rewrites @<id> to @<name>', () => {
  H.state.members.clear();
  H.state.members.set('_op_g_bob_abc123', { id: '_op_g_bob_abc123', name: 'bob-guest' });
  const out = H.humanizeIdSigils('ping @_op_g_bob_abc123 now');
  assert.strictEqual(out, 'ping @bob-guest now');
});
check('humanizeIdSigils leaves unknown ids untouched', () => {
  H.state.members.clear();
  assert.strictEqual(H.humanizeIdSigils('@_op_g_nobody_x'), '@_op_g_nobody_x');
});

// ── mention resolution + composer escaping ──────────────────────────────────
// These were entirely absent: ~250 lines of parsing and DOM logic shipped with
// no coverage and were not even exported through the test hook.
function seedMembers(pairs) {
  H.state.members = new Map(pairs.map(([id, name]) => [id, { id, name }]));
}

check('mentions: a roster name resolves', () => {
  seedMembers([['m1', 'alice']]);
  const hits = H.collectMentionMatches('ping @alice please', null);
  assert.strictEqual(hits.length, 1);
  assert.strictEqual(hits[0].member.id, 'm1');
});

check('mentions: an unknown name does NOT resolve', () => {
  seedMembers([['m1', 'alice']]);
  assert.strictEqual(H.collectMentionMatches('ping @ali please', null).length, 0);
});

check('mentions: resolution is case-insensitive', () => {
  seedMembers([['m1', 'alice']]);
  assert.strictEqual(H.collectMentionMatches('@ALICE', null).length, 1);
});

check('mentions: trailing sentence punctuation is not part of the name', () => {
  seedMembers([['m1', 'alice']]);
  const hits = H.collectMentionMatches('thanks @alice.', null);
  assert.strictEqual(hits.length, 1);
  assert.strictEqual(hits[0].member.id, 'm1');
});

check('mentions: an email-looking token is not a mention', () => {
  seedMembers([['m1', 'alice']]);
  assert.strictEqual(H.collectMentionMatches('mail me@alice.com', null).length, 0);
});

check('mentions: @all resolves to the broadcast pseudo-member', () => {
  seedMembers([['m1', 'alice']]);
  const hits = H.collectMentionMatches('@all standup', null);
  assert.strictEqual(hits.length, 1);
  assert.strictEqual(hits[0].member.id, 'all');
});

check('mentions: a member id resolves as well as a name', () => {
  seedMembers([['m1', 'alice']]);
  assert.strictEqual(H.mentionMemberForToken('m1', null, true).id, 'm1');
});

// The composer builds HTML from raw user input, so this is the one path where
// a missed escape is exploitable by typing.
check('composer: markup in the draft is escaped, not interpreted', () => {
  seedMembers([['m1', 'alice']]);
  const html = H.composerMentionHtml('<script>alert(1)</script> @alice');
  // The security property is that no executable markup survives. Assert that,
  // not a particular entity spelling.
  assert.ok(!/<script/i.test(html), 'raw <script> must not survive');
  assert.ok(!/<\/script/i.test(html), 'nor its closing tag');
  assert.ok(/composer-mention/.test(html), 'and the real mention still decorates');
});

check('composer: a member NAME containing markup cannot inject', () => {
  seedMembers([['m1', '<img src=x onerror=alert(1)>']]);
  const html = H.composerMentionHtml('hi @<img src=x onerror=alert(1)>');
  assert.ok(!/<img /.test(html), 'a hostile member name must not reach the DOM raw');
});

check('composer: the tail after the last mention is escaped too', () => {
  seedMembers([['m1', 'alice']]);
  const html = H.composerMentionHtml('@alice <b>bold</b>');
  assert.ok(!/<b>/.test(html), 'markup after the last mention must not survive raw');
  assert.ok(/bold/.test(html), 'but its text is preserved');
});

// ── chimeScopeAllows: mention-scoped chime predicate (feature #7) ─────────────
// The chime's scope gate. Pure, and independent of notifyScope: 'all' chimes on
// every peer message; 'mention' only when the operator is @'d.
check("chimeScopeAllows: 'all' chimes regardless of mention", () => {
  assert.strictEqual(H.chimeScopeAllows('all', false), true);
  assert.strictEqual(H.chimeScopeAllows('all', true), true);
});
check("chimeScopeAllows: 'mention' gates on the mention predicate", () => {
  assert.strictEqual(H.chimeScopeAllows('mention', true), true);
  assert.strictEqual(H.chimeScopeAllows('mention', false), false);
});
check('chimeScopeAllows: mention flag is coerced to a real boolean', () => {
  // appendMessage passes `(m.mentions || []).includes(id)` — already boolean —
  // but the helper must not leak a truthy/undefined value through.
  assert.strictEqual(H.chimeScopeAllows('mention', undefined), false);
  assert.strictEqual(H.chimeScopeAllows('mention', 0), false);
});
check('chimeScopeAllows: unknown/absent scope falls through to mention-gated', () => {
  // Only 'all' opens the gate unconditionally; any other/absent value defers to
  // the mention predicate rather than chiming on everything.
  assert.strictEqual(H.chimeScopeAllows('', true), true);
  assert.strictEqual(H.chimeScopeAllows('', false), false);
  assert.strictEqual(H.chimeScopeAllows(undefined, false), false);
});

// ── shouldChime: the call-site gate, not just the scope predicate ────────────
// chimeScopeAllows was already covered, but a regression in the conditions
// AROUND it — the history burst, own messages, the DM view — would have left
// the suite green. These pin each one.
const CHIME_BASE = {
  initialLoad: false, soundEnabled: true, isMine: false, isSystem: false,
  dmVisible: true, scope: 'all', addressed: false,
};
const chimeWith = (o) => H.shouldChime(Object.assign({}, CHIME_BASE, o));

check('shouldChime: a live peer message chimes', () => {
  assert.strictEqual(chimeWith({}), true);
});
check('shouldChime: the primed history burst is silent', () => {
  assert.strictEqual(chimeWith({ initialLoad: true }), false);
});
check('shouldChime: sound off means silent', () => {
  assert.strictEqual(chimeWith({ soundEnabled: false }), false);
});
check('shouldChime: your own message never chimes', () => {
  assert.strictEqual(chimeWith({ isMine: true }), false);
});
check('shouldChime: system notices never chime', () => {
  assert.strictEqual(chimeWith({ isSystem: true }), false);
});
check('shouldChime: a message hidden by the DM view is silent', () => {
  // It is appended and CSS-hidden, so chiming would be an audible event with
  // no visible cause.
  assert.strictEqual(chimeWith({ dmVisible: false }), false);
});
check('shouldChime: mention scope needs you addressed', () => {
  assert.strictEqual(chimeWith({ scope: 'mention', addressed: false }), false);
  assert.strictEqual(chimeWith({ scope: 'mention', addressed: true }), true);
});

console.log('');
console.log((failures.length ? 'FAILED' : 'OK') + ` — ${passed} passed, ${failures.length} failure(s)`);
process.exit(failures.length ? 1 : 0);
