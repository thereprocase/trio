// Dictation permissions, bounded requests, retained clips, and recorder failure ownership.
'use strict';
const assert = require('assert');
const { load } = require('./dom-harness');
let passed = 0, finished = false;
const failures = [];
process.on('exit', code => { if (!finished && code === 0) process.exitCode = 1; });
async function check(name, fn) {
  try { await fn(); passed++; console.log('PASS: ' + name); }
  catch (error) { failures.push(name); console.error('FAIL: ' + name + ' — ' + error.stack); }
}
const flush = () => new Promise(resolve => setImmediate(resolve));
function page(health = { available: true }) {
  const cx = load(), win = cx.window, T = cx.hooks.Trio, C = T.composer;
  const p = { cx, win, T, C, health, toasts: [], requests: [], streams: [], recorders: [], browsers: [], timers: [], controllers: [], respond: 'success' };
  win.isSecureContext = true; win.AudioContext = undefined; win.webkitAudioContext = undefined;
  T.state.channel = 'alpha'; T.preferences.save({ sttMode: 'auto' });
  T.ui.toast = (message, ms, action) => p.toasts.push({ message, ms, action });
  win.navigator.mediaDevices = { getUserMedia: async () => {
    const stream = { stopped: 0, getTracks: () => [{ stop: () => stream.stopped++ }] };
    p.streams.push(stream); return stream;
  } };
  win.MediaRecorder = function () {
    if (p.failAt === 'constructor') throw new Error('synthetic recorder constructor failed');
    p.recorders.push(this); this.state = 'inactive'; this.mimeType = 'audio/webm';
    this.start = () => { if (p.failAt === 'start') throw new Error('synthetic recorder start failed'); this.state = 'recording'; };
    this.stop = () => {
      this.state = 'inactive';
      if (p.lostStop) return;
      this.ondataavailable({ data: new Blob(['saved audio bytes']) });
      return this.onstop();
    };
  };
  win.Blob = Blob;
  win.SpeechRecognition = function () { p.browsers.push(this); this.start = () => {}; this.stop = () => {}; };
  win.setTimeout = (fn, ms) => { const timer = { fn, ms, cleared: false }; p.timers.push(timer); return timer; };
  win.clearTimeout = timer => { if (timer) timer.cleared = true; };
  win.AbortSignal = { timeout: ms => {
    const controller = new AbortController(); p.controllers.push({ controller, ms }); return controller.signal;
  } };
  win.fetch = (url, options) => {
    if (!/transcribe/.test(url)) return Promise.resolve({ ok: true, json: async () => p.health });
    p.requests.push(options);
    if (p.respond === 'timeout') return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason)));
    if (p.respond === 'failure') return Promise.resolve({ ok: false, json: async () => ({ ok: false, error: 'busy' }) });
    return Promise.resolve({ ok: true, json: async () => ({ ok: true, text: 'hello' }) });
  };
  p.ready = () => C.refreshSttHealth();
  p.fire = ms => { const timer = p.timers.find(t => t.ms === ms && !t.cleared); assert.ok(timer, 'timer at ' + ms); timer.fn(); };
  return p;
}
(async () => {
  await check('guest Auto starts Browser inside the tap and never records for Hub', async () => {
    const p = page({ available: false, detail: 'dictation on this hub is limited to its members' });
    await p.ready();
    const tap = p.C.toggleDictation();
    assert.strictEqual(p.browsers.length, 1); await tap;
    assert.strictEqual(p.streams.length, 0); assert.strictEqual(p.recorders.length, 0);
    assert.strictEqual(p.requests.length, 0);
  });
  await check('guest explicit Hub refuses before opening the mic and explains membership', async () => {
    const p = page({ available: false, detail: 'dictation on this hub is limited to its members' });
    await p.ready(); p.T.preferences.save({ sttMode: 'local' });
    await p.C.toggleDictation();
    assert.strictEqual(p.streams.length, 0); assert.strictEqual(p.browsers.length, 0);
    assert.strictEqual(p.toasts[0].message, 'Dictation on this hub is limited to its members.');
  });
  await check('Firefox guest wording does not offer unavailable browser dictation', async () => {
    const p = page({ available: false, detail: 'dictation on this hub is limited to its members' });
    delete p.win.SpeechRecognition; await p.ready();
    await p.C.toggleDictation();
    assert.strictEqual(p.streams.length, 0);
    assert.ok(/limited to its members/.test(p.toasts[0].message));
    assert.ok(/no speech recognition/.test(p.toasts[0].message));
    assert.ok(!/Use browser dictation/.test(p.toasts[0].message));
  });
  await check('a kept clip blocks reload until explicitly discarded', async () => {
    const p = page(); await p.ready(); p.respond = 'failure';
    let reloads = 0; p.win.location.reload = () => reloads++;
    await p.C.toggleDictation(); await p.recorders[0].stop();
    assert.strictEqual(p.C.dictationState(), 'kept');
    const kept = p.toasts.at(-1);
    assert.strictEqual(await p.T.appRefresh.reloadApp(), false);
    assert.ok(/retry.*browser dictation.*discard/i.test(p.toasts.at(-1).message));
    assert.strictEqual(reloads, 0);
    const discard = kept.action.find(a => a.label === 'Discard');
    assert.ok(discard); discard.onClick();
    assert.strictEqual(p.C.dictationState(), '');
    const requests = p.requests.length;
    await kept.action.find(a => a.label === 'Retry').onClick();
    assert.strictEqual(p.requests.length, requests, 'discarded audio cannot be replayed');
  });
  await check('choosing Browser explicitly resolves the kept clip', async () => {
    const p = page(); await p.ready(); p.respond = 'failure';
    await p.C.toggleDictation(); await p.recorders[0].stop();
    const offer = p.toasts.at(-1);
    assert.strictEqual(p.C.dictationState(), 'kept');
    await offer.action.find(a => a.label === 'Use browser dictation').onClick();
    assert.strictEqual(p.browsers.length, 1); p.C.stopDictation();
    assert.strictEqual(p.C.dictationState(), '');
  });
  await check('slow stop after watchdog recovers its final audio when idle', async () => {
    const p = page(); await p.ready(); p.T.state.drafts.alpha = 'note';
    await p.C.toggleDictation(); const old = p.recorders[0]; p.lostStop = true;
    p.C.stopDictation(); p.fire(5000);
    old.ondataavailable({ data: new Blob(['slow audio']) });
    await old.onstop();
    assert.strictEqual(p.requests.length, 1);
    assert.strictEqual(await p.requests[0].body.text(), 'slow audio');
    assert.strictEqual(p.cx.document.getElementById('input').textContent, 'hello');
    assert.strictEqual(p.C.dictationState(), '');
    await old.onstop(); assert.strictEqual(p.requests.length, 1, 'late duplicate stop is ignored');
  });
  await check('slow stop while busy offers Retry and never resets the newer session', async () => {
    const p = page(); await p.ready(); p.T.state.drafts.alpha = 'alpha note';
    await p.C.toggleDictation(); const old = p.recorders[0]; p.lostStop = true;
    p.C.stopDictation(); p.fire(5000);
    p.T.state.channel = 'beta'; await p.C.toggleDictation();
    old.ondataavailable({ data: new Blob(['late audio']) });
    await old.onstop();
    assert.strictEqual(p.requests.length, 0);
    assert.strictEqual(p.C.dictationState(), 'recording');
    assert.strictEqual(p.streams.at(-1).stopped, 0);
    assert.strictEqual(p.cx.document.getElementById('dictate-btn').getAttribute('aria-pressed'), 'true');
    const offers = p.toasts.at(-1).action;
    assert.ok(Array.isArray(offers));
    const retry = offers.find(a => a.label === 'Retry'); assert.ok(retry);
    assert.strictEqual(retry.onClick(), false, 'Retry waits for newer recording');
    p.lostStop = false; await p.recorders.at(-1).stop();
    assert.strictEqual(p.C.dictationState(), 'kept', 'new success leaves old kept clip protected');
    const beta = p.cx.document.getElementById('input').textContent;
    await retry.onClick();
    assert.strictEqual(await p.requests[1].body.text(), 'late audio');
    assert.strictEqual(p.T.state.drafts.alpha, 'alpha note hello');
    assert.strictEqual(p.cx.document.getElementById('input').textContent, beta);
    assert.strictEqual(p.streams.length, 2, 'Retry opens no new microphone');
    assert.strictEqual(p.C.dictationState(), '');
  });
  await check('older Safari gets an AbortController deadline and releases its timer', async () => {
    const p = page(); await p.ready(); p.win.AbortSignal = {};
    p.respond = 'timeout'; await p.C.toggleDictation();
    const stopped = p.recorders[0].stop();
    assert.ok(p.requests[0].signal instanceof AbortSignal);
    p.fire(75000); await stopped;
    assert.strictEqual(p.requests[0].signal.aborted, true);
    assert.strictEqual(p.C.dictationState(), 'kept');
    assert.ok(p.timers.find(t => t.ms === 75000).cleared);
  });
  await check('health deadline plus 15 seconds governs the actual upload', async () => {
    const p = page({ available: true, deadline_s: 630 }); await p.ready();
    await p.C.toggleDictation(); await p.recorders[0].stop();
    assert.strictEqual(p.controllers[0].ms, 645000);
  });
  for (const deadline_s of [undefined, null, 0, -1, 'bad', Infinity]) {
    await check('invalid/missing deadline falls back to 75 seconds: ' + deadline_s, async () => {
      const p = page({ available: true, deadline_s }); await p.ready();
      await p.C.toggleDictation(); await p.recorders[0].stop();
      assert.strictEqual(p.controllers[0].ms, 75000);
    });
  }
  for (const outcome of ['timeout', 'failure']) {
    await check(outcome + ' retains the original audio and Retry uses the same clip and conversation', async () => {
      const p = page(); await p.ready(); p.respond = outcome;
      p.T.state.drafts.alpha = 'alpha note';
      await p.C.toggleDictation(); const stopped = p.recorders[0].stop();
      if (outcome === 'timeout') p.controllers[0].controller.abort(new DOMException('deadline', 'TimeoutError'));
      await stopped;
      const offer = p.toasts.at(-1);
      assert.strictEqual(offer.ms, 0, 'offer persists until a choice');
      assert.deepStrictEqual(Array.from(offer.action, a => a.label), ['Retry', 'Use browser dictation', 'Discard']);
      const original = p.requests[0].body;
      assert.strictEqual(await original.text(), 'saved audio bytes');
      p.T.state.channel = 'beta'; p.cx.document.getElementById('input').textContent = 'beta draft';
      p.respond = 'success';
      await offer.action[0].onClick();
      assert.strictEqual(p.requests.length, 2); assert.strictEqual(p.requests[1].body, original);
      assert.strictEqual(p.streams.length, 1, 'Retry does not reopen mic');
      assert.strictEqual(p.T.state.drafts.alpha, 'alpha note hello');
      assert.strictEqual(p.cx.document.getElementById('input').textContent, 'beta draft');
      await offer.action[0].onClick(); assert.strictEqual(p.requests.length, 2, 'completed clip cannot duplicate text');
    });
  }
  await check('an old Retry offer refuses to interfere with a newer recording', async () => {
    const p = page(); await p.ready(); p.respond = 'failure';
    await p.C.toggleDictation(); await p.recorders[0].stop();
    const retry = p.toasts.at(-1).action.find(a => a.label === 'Retry');
    await p.C.toggleDictation();
    assert.strictEqual(retry.onClick(), false);
    assert.strictEqual(p.requests.length, 1); assert.strictEqual(p.streams.at(-1).stopped, 0);
    assert.strictEqual(p.C.dictationState(), 'recording');
  });
  await check('lost stop event expires after five seconds and a late old stop cannot reset new recording', async () => {
    const p = page(); await p.ready(); await p.C.toggleDictation();
    p.lostStop = true; const oldStop = p.recorders[0].onstop;
    p.C.stopDictation(); assert.strictEqual(p.C.dictationState(), 'transcribing');
    p.fire(5000); assert.strictEqual(p.C.dictationState(), '');
    assert.strictEqual(p.cx.document.getElementById('dictate-btn').disabled, false);
    await p.C.toggleDictation(); await oldStop();
    assert.strictEqual(p.requests.length, 0); assert.strictEqual(p.C.dictationState(), 'recording');
    assert.strictEqual(p.streams.at(-1).stopped, 0);
  });
  for (const failAt of ['constructor', 'start']) for (const mode of ['auto', 'local']) {
    await check(mode + ' recorder ' + failAt + ' failure releases tracks without Browser fallback', async () => {
      const p = page(); await p.ready(); p.failAt = failAt; p.T.preferences.save({ sttMode: mode });
      await p.C.toggleDictation();
      assert.strictEqual(p.streams[0].stopped, 1); assert.strictEqual(p.browsers.length, 0);
      assert.strictEqual(p.requests.length, 0); assert.strictEqual(p.C.dictationState(), '');
      assert.strictEqual(p.cx.document.getElementById('dictate-btn').getAttribute('aria-pressed'), 'false');
      assert.ok(p.toasts[0].message.includes('synthetic recorder'));
    });
  }
  await check('toast renders persistent Retry and Browser actions and keeps a refused action visible', async () => {
    const cx = load(), T = cx.hooks.Trio;
    let timers = 0, retried = 0, browser = 0;
    cx.window.setTimeout = () => { timers++; };
    T.ui.toast('Recording retained', 0, [{ label: 'Retry', onClick: () => { retried++; return false; } },
      { label: 'Use browser dictation', onClick: () => { browser++; } }], { dismissible: false });
    const host = cx.document.getElementById('trio-toasts');
    const buttons = host.querySelectorAll('.toast-action');
    assert.strictEqual(buttons.length, 2); assert.strictEqual(timers, 0);
    const toast = host.children[0]; toast._listeners.click[0]({ target: toast });
    assert.strictEqual(host.children.length, 1, 'background tap cannot hide kept-clip actions');
    buttons[0]._listeners.click[0](); assert.strictEqual(retried, 1); assert.strictEqual(host.children.length, 1);
    buttons[1]._listeners.click[0](); assert.strictEqual(browser, 1); assert.strictEqual(host.children.length, 0);
  });
  await flush(); finished = true;
  console.log(passed + ' recovery checks passed, ' + failures.length + ' failed');
  process.exit(failures.length ? 1 : 0);
})().catch(error => { console.error(error.stack); process.exit(1); });
