// Spoken sigils in dictation: "hey Bones" -> @Bones, "hashtag Bones" ->
// #Bones, "bang Bones" -> !Bones, "bang all" -> !all.
//
// The risk runs both ways. A missed match is a small annoyance; a false one
// rewrites ordinary speech, and "hey" is ordinary speech. A bang is an
// unfilterable emergency wake, so a false "bang" match is the worst outcome
// this feature can produce. These checks pin the matcher (hits, mishearings,
// the margin rule, multi-word names, punctuation, the "hey" false positive,
// literal sigils) and that only FINAL text is rewritten, on both engines.
//
// Usage: node tests/test-dictation-sigils.js
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
const C = Trio.composer;
const sig = C.applySpokenSigils;
check('applySpokenSigils is exported', typeof sig === 'function');

const room = ['Bones', 'BOATman', 'codex-sol', 'Theo'];
const eq = (spoken, names, want) => sig(spoken, names) === want;

// ── hits ──
check('hey + exact name -> @Name', eq('hey bones check this', room, '@Bones check this'));
check('hashtag + name -> #Name', eq('hashtag bones is done', room, '#Bones is done'));
check('"hash tag" as two words -> #Name', eq('hash tag Theo is done', room, '#Theo is done'));
check('bang + name -> !Name', eq('bang bones now', room, '!Bones now'));
check('bang all -> !all', eq('bang all the servers are down', room, '!all the servers are down'));
check('several in one utterance all apply',
      eq('hey Bones and hashtag theo, also bang all', room, '@Bones and #Theo also !all'));
check('the mention mid-sentence keeps the words around it',
      eq('please ask hey bones about it', room, 'please ask @Bones about it'));

// ── punctuation the recogniser adds ──
check('"Hey, Bones," swallows the trigger and the trailing comma',
      eq('Hey, Bones, can you check this?', room, '@Bones can you check this?'));
check('"Bang! all" -> !all', eq('Bang! all', room, '!all'));
check('closing punctuation after the name is kept', eq('Is that you, hey Bones?', room, 'Is that you, @Bones?'));
check('a comma after the name stops a multi-word match', eq('hey Theo, man', ['Theo', 'Theoman'], '@Theo man'));

// ── names spoken as several words ──
check('"boat man" -> @BOATman', eq('hey boat man look at this', room, '@BOATman look at this'));
check('"codex sol" -> @codex-sol', eq('Hey codex sol, ship it.', room, '@codex-sol ship it.'));
check('number words read as digits', eq('hey claude one go', ['claude-1', 'Bones'], '@claude-1 go'));

// ── mishearings ──
check('one misheard letter still matches ("bonds" -> Bones)', eq('hey bonds check', room, '@Bones check'));
check('a far-off word does not match ("phones")', eq('hey phones check', room, 'hey phones check'));
check('a prefix overlap is not a match: "big bang theory" wakes nobody',
      eq('the big bang theory', room, 'the big bang theory'));
check('"all" for bang must be heard exactly ("bang hall")', eq('bang hall', room, 'bang hall'));
check('"all" is a bang target only ("hey all" is not @all)', eq('hey all', room, 'hey all'));

// ── the margin rule ──
check('two equally close members: left as spoken',
      eq('hey claude go', ['claude-1', 'claude-2'], 'hey claude go'));
check('an exact name beats a close sibling', eq('hey claude go', ['claude', 'claude-2'], '@claude go'));

// ── "hey" as ordinary speech ──
check('"hey, can you check this" is untouched', eq('hey, can you check this', room, 'hey, can you check this'));
check('"hey there" is untouched with Theo in the room', eq('hey there', room, 'hey there'));
check('"hey" alone at the end is untouched', eq('say hey', room, 'say hey'));
check('no members: nothing changes', eq('hey bones', [], 'hey bones'));
check('a non-member name is left as spoken', eq('hey gandalf look', room, 'hey gandalf look'));

// ── literal sigils the recogniser already wrote ──
check('a literal @Bones is left alone', eq('hey @Bones look', room, 'hey @Bones look'));
check('a literal #Bones is left alone', eq('see #Bones', room, 'see #Bones'));
check('a literal !all is left alone', eq('bang !all', room, 'bang !all'));

// ── finals only, browser engine ──
// Interim text is shown as heard; only once it is final does it become a sigil.
function res(transcript, isFinal) { return { 0: { transcript }, isFinal }; }
const finalize = text => sig(text, room);
let absorb = C.makeSpeechAccumulator('', finalize);
check('interim "hey bones" stays as heard', absorb([res('hey bones', false)], 0) === 'hey bones');
check('the same words become a sigil once final', absorb([res('hey bones check', true)], 0) === '@Bones check');
absorb = C.makeSpeechAccumulator('', finalize);
check('final part rewritten, interim tail left as heard',
      absorb([res('hey bones', true), res(' and hashtag theo', false)], 1) === '@Bones and hashtag theo');
check('...and the tail is rewritten when it is final',
      absorb([res('hey bones', true), res(' and hashtag theo', true)], 1) === '@Bones and #Theo');
absorb = C.makeSpeechAccumulator('hey bones', finalize);
check('text typed before dictation is never rewritten',
      absorb([res('hashtag theo', true)], 0) === 'hey bones #Theo');

// ── finals only, live: both engines read the channel roster ──
(async () => {
  const win = cx.window;
  const state = Trio.state;
  state.members = new Map([
    ['m1', { id: 'm1', name: 'Bones' }],
    ['m2', { id: 'm2', name: 'codex-sol' }],
    ['me', { id: 'me', name: 'Frodo' }],
  ]);
  state.operator = { id: 'me' };
  const box = cx.document.getElementById('input');
  const savedToast = Trio.ui.toast;
  Trio.ui.toast = () => {};
  win.isSecureContext = true;

  // Browser engine.
  let rec = null;
  win.SpeechRecognition = function () { rec = this; this.start = () => {}; this.stop = () => {}; };
  const savedFetch = win.fetch;
  win.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ available: false, detail: 'not installed' }) });
  try {
    await C.refreshSttHealth();
    Trio.preferences.save({ sttMode: 'web' });
    box.textContent = '';
    await C.toggleDictation();
    rec.onresult({ results: [res('hey bones', false)], resultIndex: 0 });
    check('browser engine: interim stays as heard in the box', box.textContent === 'hey bones');
    rec.onresult({ results: [res('hey bones, bang Frodo now', true)], resultIndex: 0 });
    check('browser engine: final rewritten in the box; the operator is not a target',
          box.textContent === '@Bones bang Frodo now');
    check('dictation never sends: the text is still in the box', box.textContent.length > 0);
    C.stopDictation();

    // Local engine: the transcript returned after Stop is final text.
    let recorder = null;
    win.MediaRecorder = function () { recorder = this; this.state = 'inactive';
      this.start = () => { this.state = 'recording'; };
      this.stop = () => { this.state = 'inactive'; this.onstop(); }; };
    win.navigator.mediaDevices = { getUserMedia: () => Promise.resolve({ getTracks: () => [] }) };
    win.Blob = function () { this.type = 'audio/webm'; };
    const sent = [];
    win.fetch = (url) => {
      sent.push(url);
      if (/transcribe/.test(url)) return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, text: 'Hey codex sol, ship it.' }) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ available: true, detail: 'ok' }) });
    };
    await C.refreshSttHealth();
    Trio.preferences.save({ sttMode: 'local' });
    box.textContent = 'note:';
    await C.toggleDictation();
    check('local engine: recording started', recorder && recorder.state === 'recording');
    recorder.stop();
    await new Promise(r => setTimeout(r, 10));
    check('local engine: returned transcript rewritten after the typed text',
          box.textContent === 'note: @codex-sol ship it.');
    check('local engine: nothing posted except the audio', sent.every(u => !/\/api\/send/.test(u)));
  } finally {
    Trio.ui.toast = savedToast;
    win.fetch = savedFetch;
  }

  console.log('');
  if (failures.length) {
    console.log(failures.length + ' FAILED: ' + failures.join(', '));
    process.exit(1);
  }
  console.log(passed + ' spoken sigil checks passed');
})().catch(error => { console.log('FAIL: async section threw: ' + error.stack); process.exit(1); });
