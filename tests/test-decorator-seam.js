// Decorator-seam tests — guards the INTEGRATION point, not either feature.
//
// Why this file exists: #23 (inline @mentions) and #22 (filesystem path links)
// were developed on separate branches and each added its own decorator call at
// the same place in paintBody. Integrating them put BOTH decorators over the
// SAME DOM subtree, in a fixed order, for the first time. Neither PR's tests
// cover that, because on their own branches the other decorator did not exist.
//
// The order is load-bearing: decorateInlineMentions runs first and wraps
// mentions in .inline-mention; decorateFilePaths then skips that class
// explicitly. Reverse them, or drop the exclusion, and a path-looking token
// inside a mention gets linkified twice.
//
// Usage: node tests/test-decorator-seam.js
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

function withMember(id, name, fn) {
  H.state.members.set(id, { id, name });
  try { return fn(); } finally { H.state.members.delete(id); }
}

function bodyFrom(markdown) {
  const body = new FakeElement('div');
  body.innerHTML = H.renderMarkdown(markdown);
  return body;
}

// ── the seam itself ─────────────────────────────────────────────────────────
// NOTE on assertions: query the DOM, don't match the serialized innerHTML.
// dom-harness sets className as a plain property, and its serializer only emits
// node._attrs — so `class` never appears in innerHTML even when the element
// genuinely carries it. Matching on the string would fail for a decorator that
// worked perfectly. querySelector/closest read the property and are accurate.
check('both decorators applied in paintBody order: mention AND path survive', () => {
  withMember('m1', 'alice', () => {
    const body = bodyFrom('hey @alice look at /home/example/code/trio/README.md today');
    H.decorateInlineMentions(body, ['m1']);
    H.decorateFilePaths(body);

    const mention = body.querySelector('.inline-mention');
    assert.ok(mention, 'mention decoration was lost: ' + body.innerHTML);
    assert.ok(/alice/.test(mention.textContent), 'mention text was lost');
    assert.ok(/README\.md/.test(body.textContent), 'path text was lost');
  });
});

check('running the pair is idempotent — a second pass adds no duplicate wrappers', () => {
  withMember('m1', 'alice', () => {
    const body = bodyFrom('hey @alice see /home/example/notes.md');
    H.decorateInlineMentions(body, ['m1']);
    H.decorateFilePaths(body);
    const once = body.innerHTML;

    H.decorateInlineMentions(body, ['m1']);
    H.decorateFilePaths(body);
    assert.strictEqual(body.innerHTML, once,
      'second decoration pass mutated the DOM — decorators are not idempotent');
  });
});

// ── the exclusion that makes the order safe ─────────────────────────────────
check('decorateFilePaths does NOT descend into an .inline-mention', () => {
  const body = new FakeElement('div');
  const span = new FakeElement('span');
  span.className = 'inline-mention';
  span.textContent = '@alice /home/example/secret/path.md';
  body.appendChild(span);

  H.decorateFilePaths(body);
  assert.ok(!body.querySelector('.file-link'),
    'a path inside .inline-mention was linkified; the class exclusion in ' +
    'decorateFilePaths is what keeps the two decorators from colliding');
});

check('decorateInlineMentions does NOT descend into an anchor (file-link is an <a>)', () => {
  withMember('m1', 'alice', () => {
    const body = new FakeElement('div');
    const a = new FakeElement('a');
    a.className = 'file-link';
    a.textContent = 'ask @alice';
    body.appendChild(a);

    H.decorateInlineMentions(body, ['m1']);
    assert.ok(!body.querySelector('.inline-mention'),
      'a mention inside an anchor was decorated; anchors must be left alone ' +
      'so the reverse decorator order cannot double-wrap');
  });
});

// ── the detector is unchanged by neighbouring decoration ────────────────────
check('path detection is unaffected by an adjacent mention in the same text', () => {
  const withMention = H.detectFilePathCandidates('@alice /home/example/a/b.md');
  const without = H.detectFilePathCandidates('/home/example/a/b.md');
  assert.strictEqual(withMention.length, without.length,
    'an adjacent @mention changed how many paths were detected');
  assert.ok(withMention.length >= 1, 'expected the path to be detected at all');
});

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
// Exit explicitly. decorateFilePaths issues a fetch, and dom-harness stubs fetch
// as a promise that never settles — enough to hold the event loop open forever.
// Without this the suite HANGS on success and only terminates when it fails,
// which is the worst possible failure mode for a test file: green means hung.
process.exit(failures.length ? 1 : 0);
