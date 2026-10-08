// Dictation across conversation switches, the hub health wait, and the Hub
// engine's labels.
//
// Each of these is a way for spoken words to land somewhere the user did not
// put them, or for the mic to stay on while nothing is listening:
//   - the local engine transcribes after Stop; a thread switch in that window
//     used to drop the text into the NEW thread's box;
//   - a browser session whose conversation changed under it must stop, and
//     words it still delivers belong to the thread they were spoken in;
//   - a tap that waits up to 3 s for the hub's answer must show that it is
//     busy, and must not start recording in a thread the user has left.
// The Hub labels follow /api/stt/health, including the `remote` flag a hub
// sets when it forwards audio to an operator-configured speech service.
//
// Usage: node tests/test-dictation-sessions.js
'use strict';

const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
// An await that never settles lets Node exit 0 with no summary, which would
// read as a pass. Treat stopping before the summary as a failure.
let finished = false;
process.on('exit', code => {
  if (!finished && code === 0) { console.log('FAIL: stopped before finishing (a promise never settled)'); process.exitCode = 1; }
});
function check(name, cond) {
  if (cond) { passed++; console.log('PASS: ' + name); }
  else { failures.push(name); console.log('FAIL: ' + name); }
}
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));
function res(transcript, isFinal) { return { 0: { transcript }, isFinal }; }

// A fresh page with a controllable hub. `health` answers /api/stt/health,
// `transcript` answers /api/stt/transcribe; either may be a pending promise.
function page({ health, transcript } = {}) {
  const cx = load();
  const win = cx.window;
  const Trio = cx.hooks.Trio;
  const toasts = [];
  Trio.ui.toast = (message, ms, action) => toasts.push({ message, action });
  win.isSecureContext = true;
  // No level meter here: the harness's AudioContext fake is for the chime only.
  win.AudioContext = undefined; win.webkitAudioContext = undefined;
  const mic = { opens: 0, release: [] };
  win.navigator.mediaDevices = { getUserMedia: () => {
    mic.opens++;
    return new Promise(resolve => mic.release.push(() => resolve({ getTracks: () => [] })));
  } };
  const recorders = [];
  win.MediaRecorder = function () { recorders.push(this); this.state = 'inactive';
    this.start = () => { this.state = 'recording'; };
    this.stop = () => { this.state = 'inactive'; this.onstop(); }; };
  win.Blob = function () { this.type = 'audio/webm'; };
  const sessions = [];
  win.SpeechRecognition = function () { sessions.push(this); this.start = () => {}; this.stop = () => { this.stopped = true; }; };
  win.fetch = url => {
    const body = /transcribe/.test(url) ? transcript : health;
    return Promise.resolve(body).then(b => ({ ok: true, json: () => Promise.resolve(b) }));
  };
  const box = cx.document.getElementById('input');
  return { cx, win, Trio, C: Trio.composer, state: Trio.state, toasts, mic, recorders, sessions, box };
}
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

(async () => {
  // Cancel even before an engine exists: health and permission awaits.
  {
    const answer = deferred();
    const p = page({ health: answer.promise });
    p.Trio.preferences.save({ sttMode: 'local' });
    const tap = p.C.toggleDictation();
    p.C.unmount();
    answer.resolve({ available: true, detail: 'ok' });
    await Promise.race([tap, tick(50)]);
    check('unmount during health wait never opens the mic', p.mic.opens === 0);
  }
  {
    const p = page({ health: { available: true, detail: 'ok' } });
    await p.C.refreshSttHealth();
    p.Trio.preferences.save({ sttMode: 'local' });
    let stopped = 0;
    p.win.navigator.mediaDevices.getUserMedia = () => new Promise(resolve =>
      p.mic.release.push(() => resolve({ getTracks: () => [{ stop: () => stopped++ }] })));
    const tap = p.C.toggleDictation();
    p.C.unmount();
    p.mic.release.shift()();
    await tap;
    check('unmount during mic prompt never starts a recorder', p.recorders.length === 0);
    check('unmount during mic prompt releases the returned stream', stopped === 1);
  }
  {
    const p = page({ health: { available: true, detail: 'ok' } });
    await p.C.refreshSttHealth();
    p.Trio.preferences.save({ sttMode: 'local' });
    const controller = new AbortController();
    let timeoutMs, requestSignal;
    p.win.AbortSignal = { timeout: ms => { timeoutMs = ms; return controller.signal; } };
    const healthFetch = p.win.fetch;
    p.win.fetch = (url, options) => {
      if (!/transcribe/.test(url)) return healthFetch(url);
      requestSignal = options.signal;
      return new Promise((_, reject) => options.signal?.addEventListener('abort', () => reject(controller.signal.reason)));
    };
    const tap = p.C.toggleDictation();
    p.mic.release.shift()();
    await tap;
    p.recorders[0].stop();
    check('deadline: transcription is busy before the deadline', p.C.dictationState() === 'transcribing');
    check('deadline: request carries a 75-second timeout signal', timeoutMs === 75000 && requestSignal === controller.signal);
    controller.abort(new DOMException('The operation timed out', 'TimeoutError'));
    await tick(10);
    check('deadline: abort clears transcribing and processing', p.C.dictationState() === 'kept'
      && !p.cx.document.getElementById('dictate-btn').classList.contains('processing'));
    check('deadline: abort shows the exact recovery wording and browser offer', p.toasts.some(t =>
      t.message === 'Hub dictation timed out. Retry this recording, or use browser dictation.' && t.action));
    check('deadline: abort does not reopen browser dictation', p.sessions.length === 0);
  }
  // A pending transcription owns the shared recorder state until it settles.
  {
    const p = page({ health: { available: true, detail: 'ok' }, transcript: new Promise(() => {}) });
    await p.C.refreshSttHealth();
    p.Trio.preferences.save({ sttMode: 'local' });
    const tap = p.C.toggleDictation();
    p.mic.release.shift()();
    await tap;
    const rec = p.recorders[0];
    let dispatchStop;
    rec.stop = () => { rec.state = 'inactive'; dispatchStop = () => rec.onstop(); };
    p.C.stopDictation();
    p.state.channel = 'beta';
    p.C.refresh();
    check('queued onstop preserves processing before the event arrives', p.C.dictationState() === 'transcribing'
      && p.cx.document.getElementById('dictate-btn').disabled);
    await Promise.race([p.C.toggleDictation(), tick(20)]);
    check('queued onstop refuses a new mic before the event arrives', p.mic.opens === 1);
    dispatchStop();
  }
  {
    const p = page({ health: { available: true, detail: 'ok' }, transcript: new Promise(() => {}) });
    await p.C.refreshSttHealth();
    p.Trio.preferences.save({ sttMode: 'local' });
    p.win.navigator.mediaDevices.getUserMedia = () => Promise.reject(new Error('mic interrupted'));
    await p.C.toggleDictation();
    const earlierOffer = p.toasts.at(-1).action;
    p.win.navigator.mediaDevices.getUserMedia = () => Promise.resolve({ getTracks: () => [] });
    await p.C.toggleDictation();
    p.recorders[0].stop();
    await earlierOffer.onClick();
    check('pending transcription refuses an earlier toast browser action', p.sessions.length === 0
      && p.C.dictationState() === 'transcribing');
  }
  for (const outcome of ['timeout', 'success']) {
    const answer = deferred();
    const p = page({ health: { available: true, detail: 'ok' }, transcript: answer.promise });
    await p.C.refreshSttHealth();
    p.Trio.preferences.save({ sttMode: 'local' });
    p.state.channel = 'alpha';
    p.state.drafts.beta = 'beta draft';
    const streams = [];
    p.win.navigator.mediaDevices.getUserMedia = () => {
      p.mic.opens++;
      const stream = { stopped: 0, getTracks: () => [{ stop: () => stream.stopped++ }] };
      streams.push(stream);
      return Promise.resolve(stream);
    };
    const controller = new AbortController();
    p.win.AbortSignal = { timeout: () => controller.signal };
    const healthFetch = p.win.fetch;
    p.win.fetch = (url, options) => {
      if (!/transcribe/.test(url)) return healthFetch(url);
      return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(options.signal.reason));
        answer.promise.then(body => resolve({ ok: true, json: async () => body }));
      });
    };
    await p.C.toggleDictation();
    p.C.stopDictation();
    p.state.channel = 'beta';
    p.C.refresh();
    const button = p.cx.document.getElementById('dictate-btn');
    check(outcome + ': switching channels preserves processing', button.disabled
      && button.classList.contains('processing') && !p.cx.document.getElementById('dictate-status').hidden);
    await p.C.toggleDictation();
    check(outcome + ': a new recording attempt is refused while transcribing', p.mic.opens === 1
      && p.recorders.length === 1 && p.sessions.length === 0);
    if (outcome === 'timeout') controller.abort(new DOMException('deadline', 'TimeoutError'));
    else answer.resolve({ ok: true, text: 'alpha words' });
    await tick(10);
    check(outcome + ': completion makes dictation usable again', p.C.dictationState() === (outcome === 'timeout' ? 'kept' : '') && !button.disabled);
    check(outcome + ': completion leaves beta draft alone', p.box.textContent === 'beta draft');
    if (outcome === 'success') check('success: old transcript is saved in alpha', p.state.drafts.alpha === 'alpha words');
    await p.C.toggleDictation();
    await tick(10);
    check(outcome + ': the next recording stays active with its own live microphone',
      p.recorders.at(-1).state === 'recording' && streams.at(-1).stopped === 0
      && button.getAttribute('aria-pressed') === 'true');
  }
  for (const mode of ['auto', 'local']) {
    for (const cancel of ['unmount', 'switch']) {
      const p = page({ health: { available: true, detail: 'ok' } });
      await p.C.refreshSttHealth();
      p.Trio.preferences.save({ sttMode: mode });
      p.state.channel = 'alpha';
      const mic = deferred();
      p.win.navigator.mediaDevices.getUserMedia = () => mic.promise;
      const tap = p.C.toggleDictation();
      if (cancel === 'unmount') p.C.unmount();
      else p.state.channel = 'beta'; // also cancel a switch that skipped refresh
      mic.reject(new DOMException('Microphone capture interrupted', 'AbortError'));
      await tap;
      check(mode + ': rejected start after ' + cancel + ' is silent cancellation',
        p.sessions.length === 0 && p.recorders.length === 0 && p.toasts.length === 0 && p.C.dictationState() === '');
    }
  }
  // ── local engine: thread switch while transcribing ──
  {
    const p = page({ health: { available: true, detail: 'ok' }, transcript: { ok: true, text: 'ship it' } });
    p.Trio.preferences.save({ sttMode: 'local' });
    await p.C.refreshSttHealth();
    p.state.channel = 'alpha';
    p.box.textContent = 'note:';
    p.state.drafts.alpha = 'note:';
    p.state.drafts.beta = 'beta draft';
    const tap = p.C.toggleDictation();
    p.mic.release.shift()();
    await tap;
    check('local: recording started in alpha', p.recorders[0]?.state === 'recording');
    // Opening another conversation runs refresh(), which stops the recorder;
    // the transcript arrives while beta is on screen.
    p.state.channel = 'beta';
    p.C.refresh();
    await tick(10);
    check('local: the transcript did not land in the new thread', p.box.textContent === 'beta draft');
    check('local: it went into the draft of the thread it was spoken in',
          p.state.drafts.alpha === 'note: ship it');
    check('local: the user is told where it went',
          p.toasts.some(t => /draft of the conversation you recorded it in/.test(t.message)));
  }

  // ── local engine: thread switch during the mic permission prompt ──
  {
    const p = page({ health: { available: true, detail: 'ok' } });
    p.Trio.preferences.save({ sttMode: 'local' });
    await p.C.refreshSttHealth();
    p.state.channel = 'alpha';
    const tap = p.C.toggleDictation();
    p.state.channel = 'beta';
    p.C.refresh();
    p.mic.release.shift()();
    await tap;
    check('local: no recording starts in a thread the user has left', p.recorders.length === 0);
  }

  // ── browser engine: conversation changes under a live session ──
  {
    const p = page({ health: { available: false, detail: 'not installed' } });
    await p.C.refreshSttHealth();
    p.Trio.preferences.save({ sttMode: 'auto' });
    p.state.channel = 'alpha';
    p.box.textContent = '';
    await p.C.toggleDictation();
    const rec = p.sessions[0];
    rec.onresult({ results: [res('hello', false)], resultIndex: 0 });
    check('browser: words appear in the thread being dictated', p.box.textContent === 'hello');
    // A conversation change that did not go through refresh(): the next
    // result must stop the session rather than keep the mic hot for nothing.
    p.state.channel = 'beta';
    p.box.textContent = 'beta draft';
    rec.onresult({ results: [res('hello world', true)], resultIndex: 0 });
    check('browser: the session is stopped once its thread is gone', rec.stopped === true);
    check('browser: the button is back to idle', p.cx.document.getElementById('dictate-btn').getAttribute('aria-pressed') === 'false');
    check('browser: the words went to the original thread\'s draft', p.state.drafts.alpha === 'hello world');
    check('browser: the new thread\'s box is untouched', p.box.textContent === 'beta draft');
  }
  {
    const p = page({ health: { available: false, detail: 'not installed' } });
    await p.C.refreshSttHealth();
    p.state.channel = 'alpha';
    p.box.textContent = 'typed';
    await p.C.toggleDictation();
    const rec = p.sessions[0];
    p.state.channel = 'beta';
    p.state.drafts.beta = '';
    p.C.refresh(); // the normal path: refresh() stops the session
    check('browser: refresh() stops the session', rec.stopped === true);
    rec.onresult({ results: [res('late final', true)], resultIndex: 0 });
    check('browser: a final arriving after the switch is kept in the original draft',
          p.state.drafts.alpha === 'typed late final');
    check('browser: ...and not shown in the new thread', p.box.textContent === '');
  }

  // ── the health wait ──
  {
    const answer = deferred();
    const p = page({ health: answer.promise });
    p.Trio.preferences.save({ sttMode: 'local' });
    p.state.channel = 'alpha';
    const tap = p.C.toggleDictation();
    const button = p.cx.document.getElementById('dictate-btn');
    const status = p.cx.document.getElementById('dictate-status');
    check('wait: the button shows it is busy while asking the hub',
          button.classList.contains('processing') && /Checking the hub/.test(status.textContent));
    p.state.channel = 'beta';
    answer.resolve({ available: true, detail: 'ok' });
    await Promise.race([tap, tick(50)]); // a wrongly started recording waits on the mic forever
    check('wait: nothing records after the user moved to another thread', p.mic.opens === 0);
    check('wait: the busy state is cleared', !button.classList.contains('processing'));
  }
  {
    const answer = deferred();
    const p = page({ health: answer.promise });
    p.Trio.preferences.save({ sttMode: 'local' });
    const tap = p.C.toggleDictation();
    answer.resolve({ available: false, detail: 'speech engine (mlx_whisper) not installed' });
    await tap;
    check('wait: an explicit Hub choice learns the answer before recording', p.mic.opens === 0
          && p.toasts.some(t => /speech engine isn't installed/.test(t.message)));
  }

  // ── Hub labels and the remote flag ──
  {
    const label = load().hooks.Trio.preferences.hubOptionLabel;
    check('label: unknown health reads as on-hub Whisper', label(null) === 'Hub (Whisper on this hub)');
    check('label: a missing remote field means on-hub',
          label({ available: true, detail: 'ok' }) === 'Hub (Whisper on this hub)');
    check('label: remote names the hub\'s speech service',
          label({ available: true, remote: true }) === "Hub (this hub's speech service)");
    check('label: not installed keeps that wording',
          label({ available: false, detail: 'speech engine (mlx_whisper) not installed' }) === 'Hub — not installed on this hub');
    check('label: another failure says not working',
          label({ available: false, detail: 'ffmpeg not found' }) === 'Hub — not working on this hub');
  }
  function hubOption(panel) {
    const select = panel.querySelectorAll('select').find(el => el.getAttribute('aria-label') === 'Speech-to-text engine');
    return select && select.querySelectorAll('option').find(o => o.value === 'local');
  }
  {
    // Preferences opened before the composer ever asked: the label starts
    // neutral and is filled in when the answer arrives.
    const answer = deferred();
    const p = page({ health: answer.promise });
    const panel = p.cx.document.createElement('div');
    p.Trio.preferences.renderPage(panel);
    check('prefs: the Hub label starts neutral while the hub is asked', hubOption(panel)?.textContent === 'Hub (Whisper on this hub)');
    answer.resolve({ available: false, detail: 'speech engine (mlx_whisper) not installed' });
    await tick(5);
    check('prefs: the Hub label updates when the answer arrives', hubOption(panel)?.textContent === 'Hub — not installed on this hub');
  }
  {
    const p = page({ health: { available: true, remote: true, engine: 'remote' }, transcript: new Promise(() => {}) });
    await p.C.refreshSttHealth();
    const panel = p.cx.document.createElement('div');
    p.Trio.preferences.renderPage(panel);
    const note = panel.querySelectorAll('.stt-hub-note')[0];
    check('prefs: a remote hub says where the audio goes',
          note && !note.hidden && /audio goes to this hub's speech service/.test(note.textContent));
    p.Trio.preferences.save({ sttMode: 'auto' });
    const tap = p.C.toggleDictation();
    p.mic.release.shift()();
    await tap;
    check('remote: Auto uses the hub engine', p.recorders.length === 1);
    p.recorders[0].stop();
    check('remote: the transcribing status names the hub\'s speech service',
          /hub's speech service/.test(p.cx.document.getElementById('dictate-status').textContent));
  }

  console.log('');
  if (failures.length) {
    finished = true;
    console.log(failures.length + ' FAILED: ' + failures.join(', '));
    process.exit(1);
  }
  finished = true;
  console.log(passed + ' dictation session checks passed');
})().catch(error => { console.log('FAIL: async section threw: ' + error.stack); process.exit(1); });
