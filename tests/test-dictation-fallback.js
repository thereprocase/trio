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
// is today is a today is a beautiful…". The BOX has to be rewritten from a
// fixed baseline, never appended to. The original read the box back and
// appended the running transcript to it, so every event re-added the whole
// sentence so far. Interim results fire on nearly every word, so output grew
// quadratically with speech. The text itself is rebuilt from the whole
// results list on every event (see 3b for why resultIndex is not trusted).
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

// The same list delivered twice must not add its finals twice. The old
// accumulator relied on resultIndex to avoid that; the text is now rebuilt
// from the whole list, so a repeated event is simply the same text again.
absorb = acc('');
const results = [res('one', true), res(' two', true)];
absorb(results.slice(0, 1), 0);
absorb(results, 1);
out = absorb(results, 1);
check('a repeated event does not duplicate finals', out === 'one two');

// ── 3b. Android Chrome result sequences ──
// Android does not follow the desktop model: finals arrive duplicated or
// cumulative, and resultIndex does not advance the way it does on desktop.
// The old accumulator turned these into "hello hello world". Each sequence
// below is a list of (results, resultIndex) events as Android delivers them.
function play(baseline, events) {
  const a = acc(baseline);
  let text = '';
  for (const [list, index] of events) text = a(list.map(([t, f]) => res(t, f)), index);
  return text;
}
check('android: cumulative finals in a growing list, resultIndex stuck at 0',
      play('', [
        [[['hello', false]], 0],
        [[['hello', true]], 0],
        [[['hello', true], ['hello world', false]], 0],
        [[['hello', true], ['hello world', true]], 0],
      ]) === 'hello world');
check('android: a final delivered twice appears once',
      play('', [
        [[['hello world', true]], 0],
        [[['hello world', true], ['hello world', true]], 0],
      ]) === 'hello world');
check('android: one slot whose final keeps growing',
      play('', [
        [[['hello', true]], 0],
        [[['hello world', true]], 0],
        [[['hello world how are you', true]], 0],
      ]) === 'hello world how are you');
check('android: separate finals with resultIndex never advancing',
      play('', [
        [[['hello', true]], 0],
        [[['hello', true], ['world', true]], 0],
      ]) === 'hello world');
check('android: duplicated finals differing only in case collapse',
      play('', [
        [[['hello', true]], 0],
        [[['hello', true], ['Hello there', true]], 0],
      ]) === 'Hello there');
check('android: an interim that restates the finals replaces them on screen',
      play('', [
        [[['send the', true], ['send the report', false]], 0],
      ]) === 'send the report');
// A browser that starts a fresh list mid-session must not lose what was
// already final: finals are immutable, so a final that turns into unrelated
// text means the list was replaced and the old words are carried forward.
check('a replaced results list keeps the earlier finals',
      play('', [
        [[['first part', true]], 0],
        [[['second part', false]], 0],
        [[['second part', true]], 0],
      ]) === 'first part second part');
check('a final revised shorter is not duplicated',
      play('', [
        [[['hello world', true]], 0],
        [[['hello', true]], 0],
      ]) === 'hello');

// ── 3c. desktop sequences still produce the right text ──
check('desktop: two finals with Chrome\'s leading space',
      play('', [
        [[['First sentence.', true]], 0],
        [[['First sentence.', true], [' Second sentence.', true]], 1],
      ]) === 'First sentence. Second sentence.');
check('desktop: interim after a final, then the final',
      play('', [
        [[['one', true], [' two thr', false]], 1],
        [[['one', true], [' two three', true]], 1],
      ]) === 'one two three');
check('desktop: a growing interim never repeats', play('', [
  [[['today', false]], 0], [[['today is', false]], 0], [[['today is a sunny day', true]], 0],
]) === 'today is a sunny day');

// ── 3d. the typed-text baseline ──
check('baseline kept once ahead of an android cumulative sequence',
      play('existing note', [
        [[['hello', true]], 0],
        [[['hello', true], ['hello world', true]], 0],
      ]) === 'existing note hello world');
check('baseline is never collapsed into the dictation, even when it matches',
      play('hello', [[[['hello world', true]], 0]]) === 'hello hello world');
check('a baseline ending in a newline gets no extra space',
      play('line one\n', [[[['line two', true]], 0]]) === 'line one\nline two');
check('nothing heard leaves the baseline exactly as typed',
      play('typed ', [[[['', false]], 0]]) === 'typed ');
check('Japanese pieces are joined without a space',
      play('', [[[['今日は', true], ['晴れです', true]], 1]]) === '今日は晴れです');

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
Trio.state.secureUrl = 'https://host.example.ts.net:8765/';
reason = why();
check('when the server supplies the secure URL, the message names it',
      reason.includes('https://host.example.ts.net:8765/'));

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

// ── 6. which engine a tap starts ──
// On a hub without Whisper (every Linux hub: mlx_whisper is Apple-silicon
// only) the old default recorded the whole utterance, uploaded it after Stop,
// failed, and lost it. The choice now follows /api/stt/health.
const choose = C.chooseDictationEngine;
check('chooseDictationEngine is exported', typeof choose === 'function');
const caps = { canRecord: true, canRecognise: true };
check('auto + hub has local Whisper -> local',
      choose({ mode: 'auto', hubLocal: true, ...caps }).engine === 'local');
check('auto + hub without local Whisper -> browser, with no message',
      (r => r.engine === 'web' && !r.message)(choose({ mode: 'auto', hubLocal: false, ...caps })));
check('auto + health not known yet -> browser (cannot lose a recording)',
      choose({ mode: 'auto', hubLocal: null, ...caps }).engine === 'web');
check('auto + hub without local + no browser engine -> nothing, with a reason',
      (r => r.engine === 'none' && /isn't installed/.test(r.message))(
        choose({ mode: 'auto', hubLocal: false, canRecord: true, canRecognise: false })));
const explicitDead = choose({ mode: 'local', hubLocal: false,
  detail: 'speech engine (mlx_whisper) not installed', ...caps });
check('explicit local + hub without it -> does not record', explicitDead.engine === 'none');
check('...says so in one short sentence',
      /^Local Whisper isn't installed on this hub\.$/.test(explicitDead.message));
check('...and offers the browser engine as a choice, not a switch', explicitDead.offerBrowser === true);
check('explicit local + other health failure names the reason',
      /ffmpeg/.test(choose({ mode: 'local', hubLocal: false, detail: 'ffmpeg not found', ...caps }).message));
check('explicit local + hub has it -> local',
      choose({ mode: 'local', hubLocal: true, ...caps }).engine === 'local');
check('explicit web -> browser even when the hub has local',
      choose({ mode: 'web', hubLocal: true, ...caps }).engine === 'web');
check('explicit web without a browser engine -> a reason, not a crash',
      (r => r.engine === 'none' && /Preferences/.test(r.message))(
        choose({ mode: 'web', hubLocal: true, canRecord: true, canRecognise: false })));

// The same decision through the live tap, with a fake engine and hub. The
// browser engine must START inside the tap (no await first), or Safari
// refuses it as outside a user gesture; the local engine must never open the
// mic when the hub already said it cannot transcribe.
(async () => {
  const toasts = [];
  const savedToast = Trio.ui.toast;
  Trio.ui.toast = (message, ms, action) => toasts.push({ message, action });
  const started = [];
  function FakeSpeech() { this.start = () => started.push(this); this.stop = () => { this.stopped = true; }; }
  let micOpens = 0;
  win.isSecureContext = true;
  win.SpeechRecognition = FakeSpeech;
  win.MediaRecorder = function () {};
  win.navigator.mediaDevices = { getUserMedia: () => { micOpens++; return new Promise(() => {}); } };
  const savedFetch = win.fetch;
  let health = { engine: 'mlx_whisper', available: false, detail: 'speech engine (mlx_whisper) not installed' };
  win.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(health) });
  try {
    await C.refreshSttHealth();
    Trio.preferences.save({ sttMode: 'auto' });
    const tap = C.toggleDictation();
    check('auto on a hub without Whisper starts the browser engine inside the tap',
          started.length === 1);
    await tap;
    check('...without opening a recording', micOpens === 0);
    check('...and without a toast', toasts.length === 0);
    C.stopDictation();

    Trio.preferences.save({ sttMode: 'local' });
    started.length = 0;
    await C.toggleDictation();
    check('explicit local on a hub without Whisper records nothing', micOpens === 0 && started.length === 0);
    check('...and says so before recording',
          toasts.length === 1 && /isn't installed on this hub/.test(toasts[0].message));
    check('...with a button for the browser engine', toasts[0].action?.label === 'Use browser dictation');
    toasts[0].action.onClick();
    check('the button starts the browser engine', started.length === 1);
    C.stopDictation();

    health = { engine: 'mlx_whisper', available: true, detail: 'model cached' };
    await C.refreshSttHealth();
    Trio.preferences.save({ sttMode: 'auto' });
    started.length = 0;
    C.toggleDictation();
    await new Promise(r => setTimeout(r, 0));
    check('auto on a hub with Whisper records locally', micOpens === 1 && started.length === 0);
    check('the health answer feeds the diagnostics line', Trio.state.sttHealth === 'ready');
  } finally {
    Trio.ui.toast = savedToast;
    win.fetch = savedFetch;
  }

  // ── 7. the stored preference ──
  // sttMode used to default to 'local' and save() writes every key, so a
  // stored 'local' is almost always the old default. It moves to Auto; a
  // stored 'web' and a 'local' chosen since this change both stay.
  const P = Trio.preferences;
  function stored(raw) { win.localStorage.setItem('trio.preferences.v1', JSON.stringify(raw)); return P.apply().sttMode; }
  check('a fresh profile defaults to auto', (win.localStorage.removeItem('trio.preferences.v1'), P.apply().sttMode) === 'auto');
  check('a legacy stored local migrates to auto', stored({ sttMode: 'local' }) === 'auto');
  check('a legacy stored web stays web', stored({ sttMode: 'web' }) === 'web');
  P.save({ sttMode: 'local' });
  check('local chosen after the change survives a reload', P.apply().sttMode === 'local');
  check('an unknown stored value falls back to auto', stored({ sttMode: 'cloud', sttModeVersion: 2 }) === 'auto');

  console.log('');
  if (failures.length) {
    console.log(failures.length + ' FAILED: ' + failures.join(', '));
    process.exit(1);
  }
  console.log(passed + ' dictation fallback checks passed');
})().catch(error => { console.log('FAIL: async section threw: ' + error.stack); process.exit(1); });
