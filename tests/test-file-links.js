// Client-side tests for the file-path linkify layer, run against the ACTUAL
// shipped dashboard script via the Node DOM harness (tests/dom-harness.js).
//
// Covers the two halves the DOM can exercise WITHOUT a network:
//   • detectFilePathCandidates — broad path-like candidate detection (absolute,
//     ~/, ./ ../, bare dir/file, and the :line[:col] Claude-Code form), and the
//     no-false-positive cases (plain prose, bare words).
//   • linkifyValidatedPaths   — the VALIDATION GATE: only tokens the caller
//     marks valid become <a class="file-link">; look-alikes that aren't valid
//     stay plain text; code/pre/existing links and @/#/! sigil spans are skipped.
//
// The network path (decorateFilePaths → fetch /api/path/validate, revealPath →
// fetch /api/reveal) is covered server-side in tests/test-file-reveal.py; here
// fetch is absent so those functions are intentionally no-ops.
//
// Usage: node tests/test-file-links.js
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

// Array.from re-homes the sandbox-realm array into this realm so
// deepStrictEqual (which is prototype-sensitive across vm realms) compares by
// value, not by cross-realm Array identity.
function tokens(text) {
  return Array.from(H.detectFilePathCandidates(text)).map(c => c.token);
}

// ── detectFilePathCandidates: positives ──────────────────────────────────────
check('detect: absolute path', () => {
  assert.deepStrictEqual(tokens('see /Users/x/y.py here'), ['/Users/x/y.py']);
});
check('detect: home ~/ path', () => {
  assert.deepStrictEqual(tokens('open ~/.config/nth.db please'), ['~/.config/nth.db']);
});
check('detect: ./ and ../ relative', () => {
  assert.deepStrictEqual(tokens('./a.js and ../b/c.js'), ['./a.js', '../b/c.js']);
});
check('detect: bare dir/file relative', () => {
  assert.deepStrictEqual(tokens('edit server/nth_web.py now'), ['server/nth_web.py']);
});
check('detect: path:line[:col] suffix captured', () => {
  assert.deepStrictEqual(tokens('at src/main.py:42:7 boom'), ['src/main.py:42:7']);
});
check('detect: trailing sentence punctuation not captured', () => {
  assert.deepStrictEqual(tokens('look at /a/b/c.py.'), ['/a/b/c.py']);
});

// ── detectFilePathCandidates: negatives (no slash → never a candidate) ────────
check('detect: plain prose yields nothing', () => {
  assert.deepStrictEqual(tokens('just some words with no slashes'), []);
});
check('detect: a lone word is not a path', () => {
  assert.deepStrictEqual(tokens('README'), []);
});
check('detect: a BARE slash is not a candidate', () => {
  // '/' exists on disk (root), so if it were emitted it would wrongly validate
  // and pick up a folder link. A slash used as punctuation must be inert.
  assert.deepStrictEqual(tokens('press # / ! to reload'), []);
  assert.deepStrictEqual(tokens('reload / incognito'), []);
  assert.deepStrictEqual(tokens('/'), []);
});
check('detect: space-padded slash separators yield nothing', () => {
  assert.deepStrictEqual(tokens('rank is high / medium / low'), []);
});
check('detect: pure-separator runs are not candidates', () => {
  // '//', './', '../', '-/-' — slashes/punctuation only, no filename segment.
  assert.deepStrictEqual(tokens('a // b ./ c ../ d -/- e'), []);
});
check('detect: slash-joined WORDS are candidates (gated by validation)', () => {
  // These carry a filename segment so they ARE candidates, but they only link
  // if they genuinely resolve on disk — 'and/or' / 'high/medium/low' won't.
  assert.deepStrictEqual(tokens('and/or maybe'), ['and/or']);
  assert.deepStrictEqual(tokens('rank high/medium/low here'), ['high/medium/low']);
});
check('detect: long slash-free blob is linear-time (no ReDoS freeze)', () => {
  // A long run of path-charset chars with no '/', terminated by a disallowed
  // char — the classic quadratic-backtracking trigger. The linear scan must
  // handle it near-instantly and find no candidates.
  const blob = 'a'.repeat(200000) + '!';
  const t0 = Date.now();
  const found = tokens(blob);
  const dt = Date.now() - t0;
  assert.deepStrictEqual(found, []);
  assert.ok(dt < 500, 'detection took ' + dt + 'ms — expected linear/near-instant');
});

// ── linkifyValidatedPaths: the validation gate ───────────────────────────────
// A tiny valid-set stands in for the server's verdict.
function linkify(html, validSet) {
  const body = new FakeElement('div');
  body.innerHTML = html;
  H.linkifyValidatedPaths(body, (t) => validSet.has(t), null);
  return body;
}

check('linkify: a validated path becomes a .file-link with data-path', () => {
  const body = linkify('open /real/file.py now', new Set(['/real/file.py']));
  const links = body.querySelectorAll('.file-link');
  assert.strictEqual(links.length, 1, body.innerHTML);
  assert.strictEqual(links[0].dataset.path, '/real/file.py');
  assert.strictEqual(links[0].textContent, '/real/file.py');
});

check('linkify: a NON-validated look-alike stays plain text', () => {
  const body = linkify('open /not/real.py now', new Set());   // server said: does not exist
  assert.strictEqual(body.querySelectorAll('.file-link').length, 0, body.innerHTML);
});

check('linkify: a BARE slash is never linked (not even if "valid")', () => {
  // Even if the server somehow claimed '/' exists, the candidate detector never
  // emits it, so a slash used as punctuation stays plain text.
  const body = linkify('press / to reload', new Set(['/']));
  assert.strictEqual(body.querySelectorAll('.file-link').length, 0, body.innerHTML);
});
check('linkify: slash-separator words stay plain when not validated', () => {
  const body = linkify('rank is high/medium/low here', new Set());
  assert.strictEqual(body.querySelectorAll('.file-link').length, 0, body.innerHTML);
});
check('linkify: mixed — only the validated one is linked', () => {
  const body = linkify('/yes/a.py vs /no/b.py', new Set(['/yes/a.py']));
  const links = body.querySelectorAll('.file-link');
  assert.strictEqual(links.length, 1, body.innerHTML);
  assert.strictEqual(links[0].dataset.path, '/yes/a.py');
});

check('linkify: path inside inline `code` is NOT linkified', () => {
  const body = linkify('run <code class="mdic">/real/file.py</code> now',
    new Set(['/real/file.py']));
  assert.strictEqual(body.querySelectorAll('.file-link').length, 0, body.innerHTML);
});

check('linkify: path inside <pre> is NOT linkified', () => {
  const body = linkify('<pre class="mdcode">/real/file.py</pre>', new Set(['/real/file.py']));
  assert.strictEqual(body.querySelectorAll('.file-link').length, 0, body.innerHTML);
});

check('linkify: path inside an existing <a> link is NOT re-linkified', () => {
  const body = linkify('<a href="http://x">/real/file.py</a>', new Set(['/real/file.py']));
  assert.strictEqual(body.querySelectorAll('.file-link').length, 0, body.innerHTML);
});

check('linkify: does not touch an @mention sigil span', () => {
  const body = linkify('<span class="inline-mention">@/real/file.py</span>',
    new Set(['/real/file.py']));
  assert.strictEqual(body.querySelectorAll('.file-link').length, 0, body.innerHTML);
});

check('linkify: is idempotent (a second pass does not double-wrap)', () => {
  const body = new FakeElement('div');
  body.innerHTML = 'open /real/file.py now';
  const valid = new Set(['/real/file.py']);
  H.linkifyValidatedPaths(body, (t) => valid.has(t), null);
  H.linkifyValidatedPaths(body, (t) => valid.has(t), null);
  assert.strictEqual(body.querySelectorAll('.file-link').length, 1, body.innerHTML);
});

check('linkify: :line suffix is part of the linked token', () => {
  const body = linkify('at src/main.py:42 boom', new Set(['src/main.py:42']));
  const links = body.querySelectorAll('.file-link');
  assert.strictEqual(links.length, 1, body.innerHTML);
  assert.strictEqual(links[0].dataset.path, 'src/main.py:42');
});

console.log('');
console.log((failures.length ? 'FAILED' : 'OK') + ` — ${passed} passed, ${failures.length} failure(s)`);
process.exit(failures.length ? 1 : 0);
