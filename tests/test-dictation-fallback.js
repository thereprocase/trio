// The dictation fallback path — why a failing mic told the operator nothing.
//
// Two independent bugs made every browser-engine failure invisible, and both
// were found the slow way: dictation on a tailnet URL did nothing at all, and
// the visible message ("Falling back to browser speech recognition") described
// a recovery that never happened.
//
// 1. SpeechRecognition had NO onerror handler. The error event went unhandled
//    and onend fired straight after, quietly resetting the button to idle —
//    so a blocked mic, a refused insecure origin, and a dead network were
//    indistinguishable from "nothing happened". A message per failure state is
//    the fix; this pins the mapping, since an unmapped code silently degrades
//    back toward that same "no idea why" outcome.
//
// 2. hasBrowserDictation() tested only that the CONSTRUCTOR EXISTS. It exists
//    on an insecure origin too — it is start() that gets refused there. So the
//    composer promised a fallback it could not deliver, on exactly the http://
//    tailnet URL where the local engine was already dead for the same
//    secure-context reason. This is the check that has to know the difference.
//
// Both are exported as pure helpers rather than exercised through the live
// dictation flow: that flow needs a real MediaRecorder and SpeechRecognition,
// which dom-harness deliberately does not fake (see its header). Testing the
// decision instead of the plumbing is the pattern that header recommends.
//
// Usage: node tests/test-dictation-fallback.js
'use strict';

const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log('PASS: ' + name); }
  else { failures.push(name); console.log('FAIL: ' + name); }
}

const cx = load();
const Trio = cx.hooks.Trio;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

const C = Trio.composer;
const message = C.speechErrorMessage;

// ── 1. every failure state says something actionable ──
check('speechErrorMessage is exported', typeof message === 'function');

// `service-not-allowed` is CONDITIONAL. Now that hasBrowserDictation() refuses
// insecure origins the http case barely fires, and the live causes are
// system-level — a Mac with Dictation switched off, on https, where "reload
// over https" sends the user chasing a URL that is already correct. So the
// message has to read the context it is in. (LOTC/Frodo)
const win = cx.window;
const savedSecure = win.isSecureContext;
win.isSecureContext = false;
check('on an insecure page, service-not-allowed names the https requirement',
      /https/.test(message('service-not-allowed')));
win.isSecureContext = true;
const secureRefusal = message('service-not-allowed');
check('on a secure page, it does NOT tell you to reload over https',
      !/reload over https|https address/i.test(secureRefusal));
check('on a secure page, it points at the system dictation setting',
      /system settings/i.test(secureRefusal));
win.isSecureContext = savedSecure;

// A blocked mic is a browser SETTING, not a page problem — pointing at the
// wrong one costs an afternoon.
check('not-allowed points at browser permissions',
      /(permission|allow|blocked)/i.test(message('not-allowed')));

// This engine transcribes on a remote server, so a working mic plus a dead
// network still fails. But `network` is ALSO the permanent verdict in every
// Chromium browser that is not Chrome — the speech service needs Google API
// keys only Chrome ships, so the engine looks present and never works. Found
// the hard way on Dia. Naming only the connection sends the operator off to
// debug a network that is fine, so the message must name the browser cause
// and point at the local engine, which has neither problem.
const network = message('network');
check('network names the server it could not reach',
      /server|reach/i.test(network));
check('network names the not-Chrome-Chromium cause',
      /chromium/i.test(network));
check('network points at the local engine as the way out',
      /local/i.test(network));
// Order matters, not just content. A phone on a weak signal gets this too,
// and leading with a lecture about Chromium forks buries the one thing that
// user can actually go and check. (LOTC/Frodo)
check('network leads with the checkable cause, not the permanent one',
      network.toLowerCase().indexOf('connection') < network.toLowerCase().indexOf('chromium'));

check('audio-capture names the missing microphone',
      /microphone/i.test(message('audio-capture')));
check('no-speech says nothing was heard',
      /speech/i.test(message('no-speech')));

// 'aborted' is what the user's own stop button produces. Toasting there would
// report every deliberate stop as an error.
check('aborted is deliberately silent', message('aborted') === '');

// An unmapped or absent code must still produce a real sentence: the whole
// defect was a failure that said nothing, and a new browser error string
// must not reopen that hole.
const unknown = message('some-future-code');
check('an unmapped code still yields a message',
      typeof unknown === 'string' && unknown.length > 0);
check('an unmapped code includes the raw code for diagnosis',
      unknown.includes('some-future-code'));
// ...and an action. A bare code is a fact, not a way out, and this default is
// exactly where an unfamiliar browser lands. (LOTC/Frodo)
check('an unmapped code still offers something to do',
      /try again|preferences/i.test(unknown));
check('no-speech tells you how to avoid it next time',
      /try again/i.test(message('no-speech')));
const missing = message(undefined);
check('a missing code still yields a message',
      typeof missing === 'string' && missing.length > 0);
check('a missing code does not print "undefined"',
      !/undefined/.test(missing));

// Nothing here may be a bare code echoed back at the operator.
const all = ['not-allowed', 'service-not-allowed', 'no-speech', 'audio-capture',
             'network', 'language-not-supported'];
check('no mapped state returns a bare error code',
      all.every(code => message(code) !== code && message(code).length > 10));

// ── 2. availability is secure-context aware ──
// This is the check that decides whether to PROMISE a fallback. On the
// insecure origin it used to say yes, which is how the composer came to
// announce a recovery it could not perform.
const savedSpeech = win.SpeechRecognition;
const savedWebkit = win.webkitSpeechRecognition;

function withWindow({ secure, speech }, fn) {
  win.isSecureContext = secure;
  win.SpeechRecognition = speech ? function () {} : undefined;
  win.webkitSpeechRecognition = undefined;
  try { return fn(); } finally {
    win.isSecureContext = savedSecure;
    win.SpeechRecognition = savedSpeech;
    win.webkitSpeechRecognition = savedWebkit;
  }
}

check('unavailable on an insecure origin even though the constructor exists',
      withWindow({ secure: false, speech: true }, () => C.hasBrowserDictation() === false));
check('available on a secure origin with the standard constructor',
      withWindow({ secure: true, speech: true }, () => C.hasBrowserDictation() === true));
check('unavailable on a secure origin with no engine at all',
      withWindow({ secure: true, speech: false }, () => C.hasBrowserDictation() === false));

// The prefixed constructor is the only one Safari and older Chrome expose;
// dropping it would disable dictation on exactly the mobile browsers this
// whole change exists to serve.
win.isSecureContext = true;
win.SpeechRecognition = undefined;
win.webkitSpeechRecognition = function () {};
check('the webkit-prefixed constructor counts', C.hasBrowserDictation() === true);
win.isSecureContext = savedSecure;
win.SpeechRecognition = savedSpeech;
win.webkitSpeechRecognition = savedWebkit;

// A browser that reports nothing about secure context (isSecureContext
// undefined) must not be treated as insecure — that would disable dictation
// for a browser whose only sin is being old.
win.isSecureContext = undefined;
win.SpeechRecognition = function () {};
check('an unknown secure-context state does not disable dictation',
      C.hasBrowserDictation() === true);
win.isSecureContext = savedSecure;
win.SpeechRecognition = savedSpeech;

// ── 3. the transcript accumulator ──
// Reported live: saying "Today is a beautiful sunny day" produced "today today
// is today is a today is a beautiful…". Each result event carries only the
// results from resultIndex onward, so the running final text must accumulate
// across events — but the BOX has to be rewritten from a fixed baseline, never
// appended to. The original read the box back and appended the running
// transcript to it, so every event re-added the whole sentence so far. Interim
// results fire on nearly every word, so output grew quadratically with speech.
const acc = Trio.composer.makeSpeechAccumulator;
check('makeSpeechAccumulator is exported', typeof acc === 'function');

// One word at a time, interim then final — how Chrome actually streams.
function res(transcript, isFinal) { return { 0: { transcript }, isFinal }; }

let absorb = acc('');
let out;
out = absorb([res('today', false)], 0);
check('first interim shows the word once', out === 'today');
out = absorb([res('today is', false)], 0);
check('a revised interim REPLACES, never appends', out === 'today is');
out = absorb([res('today is a beautiful sunny day', true)], 0);
check('the final result replaces the interim',
      out === 'today is a beautiful sunny day');

// The reported failure, reproduced as a sequence: a growing interim followed
// by a final. Anything that appends produces the doubled string.
absorb = acc('');
[['Today', false], ['Today is', false], ['Today is a beautiful', false],
 ['Today is a beautiful sunny day', true]].forEach(([t, f]) => {
  out = absorb([res(t, f)], 0);
});
check('a full dictation session yields the sentence exactly once',
      out === 'Today is a beautiful sunny day');
check('no word is duplicated', (out.match(/beautiful/g) || []).length === 1);

// Multi-utterance: two finals in a row must BOTH survive. Rewriting from
// baseline is the fix, but rewriting from baseline while forgetting to
// accumulate finals would silently drop the first sentence.
// `results` is cumulative across events and resultIndex points at the first
// NEW entry, so the second event sees a two-element list starting at 1.
absorb = acc('');
const utterances = [res('First sentence.', true), res(' Second sentence.', true)];
absorb(utterances.slice(0, 1), 0);
out = absorb(utterances, 1);
check('successive final results both accumulate',
      out === 'First sentence. Second sentence.');

// Text already typed must be preserved and dictation appended after it once.
absorb = acc('existing note');
out = absorb([res('dictated words', true)], 0);
check('pre-existing composer text is kept exactly once',
      out === 'existing note dictated words');

// A baseline that is empty must not leave a leading space.
absorb = acc('');
out = absorb([res('hello', true)], 0);
check('an empty baseline yields no leading whitespace', out === 'hello');

// resultIndex is what makes events incremental — a handler that ignores it
// and rescans from 0 re-adds every already-final result.
absorb = acc('');
const results = [res('one', true), res('two', true)];
absorb(results.slice(0, 1), 0);
out = absorb(results, 1);
check('resultIndex is honoured, not rescanned from zero', out === 'onetwo');

// ── 4. why the mic button is dead ──
// The button used to be `disabled` with title "Dictation is unavailable in
// this browser". Disabled fires no click, and a phone has no hover, so tapping
// it did nothing and said nothing — the exact silence this whole feature keeps
// reinventing. The title was also usually FALSE: on an http tailnet URL the
// browser is fine and the address is the problem. (LOTC/Frodo, critical)
const why = Trio.composer.unavailableReason;
check('unavailableReason is exported', typeof why === 'function');

const savedSecureUrl = Trio.state.secureUrl;
win.isSecureContext = false;
Trio.state.secureUrl = '';
let reason = why();
check('on an insecure page it blames the connection, not the browser',
      /https/i.test(reason) && !/no dictation support/i.test(reason));
check('...and says how to get one when no URL is known',
      /--tailscale-tls/.test(reason));

// The server knows the address that would work; the page cannot. When it has
// been told, it must name it — "use the https address" is unactionable
// otherwise.
Trio.state.secureUrl = 'https://macbook.tail63b486.ts.net:8765/';
reason = why();
check('when the server supplies the secure URL, the message names it',
      reason.includes('https://macbook.tail63b486.ts.net:8765/'));

// Genuinely unsupported browser on a secure page: now the browser IS the
// problem, and saying so is correct.
win.isSecureContext = true;
reason = why();
check('on a secure page it names the browser and suggests real ones',
      /browser/i.test(reason) && /chrome|safari|edge/i.test(reason));
win.isSecureContext = savedSecure;
Trio.state.secureUrl = savedSecureUrl;

// ── 5. server engine errors, translated ──
// /api/stt/transcribe relays its internals verbatim. The operator does not
// know what a worker is, and none of those strings names an action.
const human = Trio.composer.humanEngineError;
check('humanEngineError is exported', typeof human === 'function');
check('the missing-engine case names install, not jargon',
      /install/i.test(human('speech engine (mlx_whisper) not installed')));
check('worker failures become something actionable',
      /restart/i.test(human('worker pipe broken'))
      && /restart/i.test(human('worker exited mid-request'))
      && /restart/i.test(human('worker sent malformed response')));
check('no translated message still says "worker"',
      !/worker/i.test(human('worker pipe broken')));
check('a busy engine reads as temporary',
      /moment|try again/i.test(human('transcription busy — try again in a moment')));
// Unrecognised text passes through rather than being flattened into something
// vaguer than the server bothered to send.
check('an unrecognised error is passed through unchanged',
      human('disk full writing scratch file') === 'disk full writing scratch file');
check('empty input does not become "undefined"',
      human(undefined) === '' && human(null) === '');

console.log('');
if (failures.length) {
  console.log(failures.length + ' FAILED: ' + failures.join(', '));
  process.exit(1);
}
console.log(passed + ' dictation fallback checks passed');
