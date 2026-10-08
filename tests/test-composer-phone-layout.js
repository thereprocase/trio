// Real Chromium geometry for the shipped composer. No hub, microphone, external
// fonts, or user browser profile: only production markup/CSS and the UI setters.
// Usage: node tests/test-composer-phone-layout.js (skips if Chromium is absent).
'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');
const { pathToFileURL } = require('url');

const candidates = process.env.CHROMIUM_BIN ? [process.env.CHROMIUM_BIN]
  : ['chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable'];
const binary = candidates.find(name => spawnSync(name, ['--version'], { timeout: 5000 }).status === 0);
if (!binary) { console.log('SKIP: composer phone geometry (Chromium absent)'); process.exit(0); }
assert.strictEqual(typeof WebSocket, 'function', 'Chromium tests require Node with built-in WebSocket (22+)');

const root = path.join(__dirname, '..', 'server');
const web = path.join(root, 'web');
const declaration = fs.readFileSync(path.join(root, 'nth_web.py'), 'utf8').match(/WEB_CSS_FILES = \(([\s\S]*?)\)/);
assert.ok(declaration, 'production CSS order exists');
const cssFiles = [...declaration[1].matchAll(/"(css\/[^"]+)"/g)].map(m => m[1]);
assert.ok(cssFiles.length, 'production CSS list is populated');
const css = cssFiles.map(name => fs.readFileSync(path.join(web, name), 'utf8')).join('\n');
const scripts = 'window.Trio = { state: {}, api: {}, events: new EventTarget(), actions: {} };\n'
  + ['js/09-ui.js', 'js/12-composer.js'].map(name => fs.readFileSync(path.join(web, name), 'utf8')).join('\n');
const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
  .replace(/<link\b[^>]*>/g, '') // no font/manifest network requests
  .replace('<!--__TRIO_STYLES__-->', '<style>' + css + '</style>')
  .replace('<!--__TRIO_SCRIPTS__-->', '<script>' + scripts + '</script>');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'nth-composer-chromium-'));
const fixture = path.join(temporary, 'composer.html');
fs.writeFileSync(fixture, html);
const browser = spawn(binary, ['--headless=new', '--no-sandbox', '--disable-gpu',
  '--disable-background-networking', '--disable-default-apps', '--disable-breakpad',
  '--disable-crash-reporter', '--no-first-run', '--password-store=basic',
  '--remote-debugging-port=0', '--user-data-dir=' + path.join(temporary, 'profile'), 'about:blank'], {
  detached: process.platform !== 'win32', stdio: ['ignore', 'ignore', 'pipe'],
  env: { ...process.env, XDG_CONFIG_HOME: path.join(temporary, 'config'),
    XDG_CACHE_HOME: path.join(temporary, 'cache'), XDG_DATA_HOME: path.join(temporary, 'data'),
    DBUS_SESSION_BUS_ADDRESS: 'unix:path=' + path.join(temporary, 'absent-dbus') },
});
let socket, timer, nextId = 0, stderr = '';
const pending = new Map();
const closed = new Promise(resolve => browser.once('close', resolve));
function stopBrowser(signal) {
  try {
    if (process.platform === 'win32') browser.kill(signal);
    else process.kill(-browser.pid, signal);
  } catch (error) { if (error.code !== 'ESRCH') throw error; }
}

async function measure() {
  const endpoint = await new Promise((resolve, reject) => {
    browser.stderr.on('data', data => {
      stderr += data;
      const match = stderr.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) resolve(match[1]);
    });
    browser.once('error', reject);
    browser.once('exit', code => reject(new Error('Chromium exited before ready: ' + code)));
  });
  socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    message.error ? request.reject(new Error(JSON.stringify(message.error))) : request.resolve(message.result);
  };
  socket.onclose = () => { for (const request of pending.values()) request.reject(new Error('Chromium disconnected')); pending.clear(); };
  const call = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const id = ++nextId; pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const { targetId } = await call('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await call('Target.attachToTarget', { targetId, flatten: true });
  await call('Page.enable', {}, sessionId);
  await call('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 }, sessionId);
  await call('Emulation.setDeviceMetricsOverride', { width: 360, height: 800, deviceScaleFactor: 1, mobile: true }, sessionId);
  await call('Page.navigate', { url: pathToFileURL(fixture).href }, sessionId);
  const evaluate = async expression => {
    const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
    assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  while (!await evaluate("document.readyState === 'complete' && !!window.Trio?.composer")) {
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  let passed = 0;
  const failures = [];
  for (const width of [360, 390, 412]) {
    await call('Emulation.setDeviceMetricsOverride', { width, height: 800, deviceScaleFactor: 1, mobile: true }, sessionId);
    for (const text of ['', 'Transcribing (Whisper on the hub)…', "Transcribing (hub's speech service)…"]) {
      const name = width + 'px ' + (text || 'idle');
      const geometry = await evaluate(`(async () => {
        const text = ${JSON.stringify(text)};
        Trio.composer.setDictationButtonState(false, { processing: !!text, statusText: text });
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const rect = el => { const r = el.getBoundingClientRect ? el.getBoundingClientRect() : el;
          return { left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height }; };
        const status = document.getElementById('dictate-status');
        const range = document.createRange(); range.selectNodeContents(status);
        const live = document.getElementById('trio-aria-live');
        const mic = document.getElementById('dictate-btn');
        return { viewport:document.documentElement.clientWidth,
          coarse:matchMedia('(hover:none) and (pointer:coarse)').matches,
          composer:rect(document.querySelector('.composer-inner')),
          controls:['attach-btn', 'dictate-btn', 'send'].map(id => ({ id, ...rect(document.getElementById(id)) })),
          status:rect(status), textRects:[...range.getClientRects()].map(rect), hidden:status.hidden,
          text:status.textContent, disabled:mic.disabled, label:mic.getAttribute('aria-label'),
          liveText:live.textContent, liveMode:live.getAttribute('aria-live') };
      })()`);
      try {
        assert.strictEqual(geometry.viewport, width, 'actual mobile viewport');
        assert.strictEqual(geometry.coarse, true, 'production touch rules are active');
        const fits = (rect, label) => {
          assert.ok(rect.left >= geometry.composer.left - .5 && rect.right <= geometry.composer.right + .5
            && rect.top >= geometry.composer.top - .5 && rect.bottom <= geometry.composer.bottom + .5
            && rect.left >= 0 && rect.right <= width + .5, label + ' fits: ' + JSON.stringify(rect));
        };
        geometry.controls.forEach(control => fits(control, control.id));
        for (const control of geometry.controls) {
          assert.ok(control.width >= 44 && control.height >= 44, control.id + ' has a 44px target');
        }
        if (text) {
          fits(geometry.status, 'status');
          assert.ok(geometry.status.height > 0 && !geometry.hidden, 'status is visible');
          assert.ok(geometry.textRects.length, 'full status text is laid out');
          geometry.textRects.forEach(rect => fits(rect, 'status text'));
          assert.strictEqual(geometry.text, text);
          assert.strictEqual(geometry.disabled, true);
          assert.strictEqual(geometry.label, text);
          assert.strictEqual(geometry.liveText, text);
          assert.strictEqual(geometry.liveMode, 'polite');
        } else {
          assert.strictEqual(geometry.hidden, true);
          assert.strictEqual(geometry.disabled, false);
          assert.strictEqual(geometry.liveText, '');
        }
        passed++; console.log('PASS: ' + name);
      } catch (error) { failures.push(name); console.error('FAIL: ' + name + ' — ' + error.message); }
    }
  }
  console.log(passed + ' geometry cases passed, ' + failures.length + ' failed');
  assert.strictEqual(failures.length, 0, 'composer phone geometry');
}

(async () => {
  try {
    await Promise.race([measure(), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Chromium geometry deadline exceeded')), 25000);
    })]);
  } catch (error) { console.error(error.stack); process.exitCode = 1; }
  finally {
    clearTimeout(timer); socket?.close(); stopBrowser('SIGTERM');
    let killTimer;
    await Promise.race([closed, new Promise(resolve => { killTimer = setTimeout(() => { stopBrowser('SIGKILL'); resolve(); }, 3000); })]);
    clearTimeout(killTimer);
    fs.rmSync(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
})();
