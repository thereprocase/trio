// Decorator-seam tests — guards the INTEGRATION point, not either feature.
//
// Why this file exists: inline sigils (@mention / #ref / !bang) and filesystem
// path links were developed separately and each added its own decorator call at
// the same place in paintBody. Integrating them put BOTH decorators over the
// SAME DOM subtree, in a fixed order, for the first time. Neither feature's own
// tests cover that, because on their own branches the other did not exist.
//
// The order is load-bearing. decorateSigils runs first and wraps each sigil in
// .sigil plus .inline-mention / .inline-ref / .inline-bang; the path walker
// then names those three classes in its skip list. Reverse the order, or let
// the two sides disagree about the class name, and a path-looking token inside
// a mention gets linkified inside a chip — which is precisely what happened
// once already, when the sigil span was emitting only `.sigil` and the path
// walker was still excluding a class no longer produced.
//
// That naming dependency is invisible in either file alone, so it is asserted
// here directly, on top of the behavioural checks.
//
// Usage: node tests/test-decorator-seam.js
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { load, FakeElement } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + e.message); }
}

const cx = load();
const H = cx.hooks;
const Trio = H.Trio;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

function withMember(id, name, fn) {
  H.state.members.set(id, { id, name });
  try { return fn(); } finally { H.state.members.delete(id); }
}

// Paint a message the way the client does, then run the path decorator's
// synchronous half over the result.
//
// decorateFilePaths itself is async: it validates candidates against
// /api/path/validate before linkifying, and the harness stubs fetch as a
// promise that never settles, so nothing would ever be linkified through it.
// linkifyValidatedPaths is the exact function decorateFilePaths calls once
// validation returns, with the same walker and the same skip list — driving it
// with an "everything exists" predicate reproduces the real seam without
// depending on a server.
function paint(content, vmExtra = {}) {
  const card = new FakeElement('div');
  const body = new FakeElement('div');
  const vm = Object.assign({
    isRetracted: false, isSystem: false, isEdited: false,
    content, member_id: 'author', channel: 'test',
    mentions: [], refs: [], bangs: [],
  }, vmExtra);
  Trio.conversation.paintBody(card, body, vm);
  return { card, body };
}

function linkifyAll(body) {
  Trio.fileLinks.linkifyValidatedPaths(body, () => true, () => {});
}

// ── the class names the two sides agree on ──────────────────────────────────
// A pure source-level check, because the runtime tests below can only fail
// once a fixture happens to put a path inside a chip. This states the contract
// itself: every class decorateSigils emits must appear in the path walker's
// skip list.
check('every sigil class is in the path walker\'s skip list', () => {
  const web = path.resolve(__dirname, '..', 'server', 'web', 'js');
  const conv = fs.readFileSync(path.join(web, '11-conversation.js'), 'utf8');
  const links = fs.readFileSync(path.join(web, '13-file-links.js'), 'utf8');
  const emitted = conv.match(/'sigil sigil-' \+ kind \+ ' inline-' \+ kind/)
    ? ['inline-mention', 'inline-ref', 'inline-bang']
    : null;
  assert.ok(emitted,
    'decorateSigils no longer builds its class list the expected way; this ' +
    'test can no longer tell what classes it emits — update it');
  const skip = links.match(/parent\.closest\(\s*'([^']+)'/);
  assert.ok(skip, 'could not find the path walker\'s closest() skip list');
  for (const cls of emitted) {
    assert.ok(skip[1].includes('.' + cls),
      `decorateSigils emits .${cls} but the path walker does not skip it — a ` +
      `path inside a ${cls} chip will be linkified inside the chip`);
  }
});

// ── the seam itself ─────────────────────────────────────────────────────────
// NOTE on assertions: query the DOM, don't match the serialized innerHTML.
// dom-harness sets className as a plain property, and its serializer only emits
// node._attrs — so `class` never appears in innerHTML even when the element
// genuinely carries it. Matching on the string would fail for a decorator that
// worked perfectly. querySelector/closest read the property and are accurate.
check('both decorators applied in paintBody order: mention AND path survive', () => {
  withMember('m1', 'alice', () => {
    const { body } = paint('hey @alice look at /home/example/project/README.md today',
      { mentions: ['m1'] });
    linkifyAll(body);

    const mention = body.querySelector('.inline-mention');
    assert.ok(mention, 'mention decoration was lost: ' + body.innerHTML);
    assert.ok(/alice/.test(mention.textContent), 'mention text was lost');
    assert.ok(/README\.md/.test(body.textContent), 'path text was lost');
    assert.ok(body.querySelector('.file-link'), 'the path was not linkified');
  });
});

check('running the path decorator twice adds no duplicate wrappers', () => {
  withMember('m1', 'alice', () => {
    const { body } = paint('hey @alice see /home/example/notes.md', { mentions: ['m1'] });
    linkifyAll(body);
    const once = body.innerHTML;
    const linksOnce = body.querySelectorAll('.file-link').length;

    linkifyAll(body);
    assert.strictEqual(body.innerHTML, once,
      'second decoration pass mutated the DOM — the decorator is not idempotent');
    assert.strictEqual(body.querySelectorAll('.file-link').length, linksOnce,
      'a second pass wrapped an already-linkified path again');
  });
});

// ── the exclusions that make the order safe ─────────────────────────────────
for (const cls of ['inline-mention', 'inline-ref', 'inline-bang']) {
  check(`the path walker does NOT descend into a .${cls}`, () => {
    const body = new FakeElement('div');
    const span = new FakeElement('span');
    span.className = 'sigil ' + cls;
    span.textContent = '@alice /home/example/secret/path.md';
    body.appendChild(span);

    linkifyAll(body);
    assert.ok(!body.querySelector('.file-link'),
      `a path inside .${cls} was linkified; the class exclusion in the path ` +
      'walker is what keeps the two decorators from colliding');
  });
}

check('decorateSigils does NOT descend into an anchor (file-link is an <a>)', () => {
  withMember('m1', 'alice', () => {
    // The anchor has to be present BEFORE decorateSigils walks the body, and
    // paintBody starts by clearing it — so appending one to the body first
    // proves nothing (an earlier version of this test did exactly that and
    // passed with the anchor exclusion deleted). A markdown link is the one
    // way to have renderMarkdown emit the anchor into the body that
    // decorateSigils then walks, which is also how a real message gets one.
    const { body } = paint('[ask @alice](https://example.invalid/x)',
      { mentions: ['m1'] });

    const anchor = body.querySelector('a');
    assert.ok(anchor, 'fixture did not produce an anchor: ' + body.innerHTML);
    assert.ok(/@alice/.test(anchor.textContent),
      'the anchor no longer carries the mention text; fixture is stale');
    assert.ok(!body.querySelector('.inline-mention'),
      'a mention inside an anchor was decorated; anchors must be left alone ' +
      'so the reverse decorator order cannot double-wrap');
  });
});

// The nastiest fixture: a path that itself contains an "@". Both sigils in one
// message is easy; an "@" INSIDE the path is where the two decorators genuinely
// compete for the same characters, because the sigil scanner runs first and
// could split the text node mid-path before the path scanner ever sees it.
check('a path containing an @ is not shredded by the sigil decorator', () => {
  withMember('m1', 'alice', () => {
    const { body } = paint('@alice check /home/example/mail@archive/x.txt please',
      { mentions: ['m1'] });
    linkifyAll(body);

    assert.ok(body.querySelector('.inline-mention'), 'the real mention was lost');
    assert.ok(/\/home\/example\/mail@archive\/x\.txt/.test(body.textContent),
      'the path text was broken up: ' + body.textContent);
  });
});

check('an @ inside a path is not itself decorated as a mention', () => {
  withMember('m1', 'archive', () => {
    const { body } = paint('see /home/example/mail@archive/x.txt', { mentions: ['m1'] });
    // "@archive" here is part of a filesystem path, not an address. A member
    // named "archive" must not turn the middle of a path into a mention chip.
    assert.ok(!body.querySelector('.inline-mention'),
      '@archive inside a path was decorated as a mention: ' + body.textContent);
  });
});

// ── the detector is unchanged by neighbouring decoration ────────────────────
check('path detection is unaffected by an adjacent mention in the same text', () => {
  const withMention = Trio.fileLinks.detectFilePathCandidates('@alice /home/example/a/b.md');
  const without = Trio.fileLinks.detectFilePathCandidates('/home/example/a/b.md');
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
