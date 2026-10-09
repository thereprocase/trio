// Real Chromium geometry using production conversation markup and ordered CSS.
// Usage: node tests/test-bubble-phone-layout.js (skips if Chromium is absent).
// Optional BUBBLES_EVIDENCE_DIR saves measurements and screenshots outside the repo;
// BUBBLES_PHASE labels those artifacts. TMPDIR controls browser scratch placement.
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
if (!binary) { console.log('SKIP: bubble phone geometry (Chromium absent)'); process.exit(0); }
assert.strictEqual(typeof WebSocket, 'function', 'Chromium tests require Node with built-in WebSocket (22+)');

const root = path.join(__dirname, '..', 'server');
const web = path.join(root, 'web');
const declaration = fs.readFileSync(path.join(root, 'nth_web.py'), 'utf8').match(/WEB_CSS_FILES = \(([\s\S]*?)\)/);
assert.ok(declaration, 'production CSS order exists');
const cssFiles = [...declaration[1].matchAll(/"(css\/[^"]+)"/g)].map(m => m[1]);
assert.ok(cssFiles.length, 'production CSS list is populated');
const css = cssFiles.map(name => fs.readFileSync(path.join(web, name), 'utf8')).join('\n');
const scripts = `window.Trio = { state: { operator: { id: 'operator' }, readOnly: true },
  api: {}, events: new EventTarget(), actions: {}, avatarTone: () => 'eucalyptus' };\n`
  + ['js/09-time.js', 'js/10-markdown.js', 'js/11-conversation.js']
    .map(name => fs.readFileSync(path.join(web, name), 'utf8')).join('\n');
const evidence = process.env.BUBBLES_EVIDENCE_DIR;
const phase = process.env.BUBBLES_PHASE || 'after';
assert.ok(/^[a-z-]+$/.test(phase), 'safe artifact label');
if (evidence) fs.mkdirSync(evidence, { recursive: true });
const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
  .replace(/<link\b[^>]*>/g, '') // no font/manifest network requests
  .replace('<!--__TRIO_STYLES__-->', '<style>' + css + '</style>')
  .replace('<!--__TRIO_SCRIPTS__-->', '<script>' + scripts + '</script>');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'nth-bubbles-chromium-'));
const fixture = path.join(temporary, 'bubbles.html');
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
  while (!await evaluate("document.readyState === 'complete' && !!window.Trio?.conversation")) {
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  await evaluate(`(() => {
    Trio.state.members = new Map([
      ['operator', { id:'operator', name:'Operator' }],
      ['agent', { id:'agent', name:'Agent', kind:'agent' }]
    ]);
    document.getElementById('h-channel').textContent = '#layout-demo';
    document.body.classList.add('message-numbers');
    const list = document.getElementById('messages');
    const prose = 'Here is a longer message to check the bubble edges. It wraps across several lines on a phone and stays readable beside the opposite gutter.';
    const base = { created_at:'2026-01-01T12:00:00Z', channel:'layout-demo' };
    const messages = [
      { id:1, member_id:'operator', content:prose, mentions:['agent'] },
      { id:2, member_id:'agent', content:prose, mentions:['operator'], reply_to:1 },
      { id:3, member_id:'operator', content:'Short reply.' },
      { id:4, member_id:'agent', content:'Short reply.' },
      { id:5, member_id:'operator', content:'Private reply.', recipients:['agent'], is_dm:true },
      { id:6, member_id:'agent', content:'| A very wide first heading | A very wide second heading | A very wide third heading |\\n| --- | --- | --- |\\n| One | Two | Three |' },
      { id:7, member_id:'operator', content:'Deleted.', retracted_at:'2026-01-01T12:01:00Z' },
      { id:8, member_id:'agent', content:'[joined] Agent' }
    ];
    list.replaceChildren(...messages.map(msg => Trio.conversation.cardFor({ ...base, ...msg })));
  })()`);
  let passed = 0;
  const failures = [], measurements = [];
  for (const width of [360, 390, 412, 1280]) {
    const mobile = width < 640;
    await call('Emulation.setTouchEmulationEnabled', { enabled:mobile, maxTouchPoints:1 }, sessionId);
    await call('Emulation.setDeviceMetricsOverride', { width, height:900, deviceScaleFactor:1, mobile }, sessionId);
    for (const theme of ['light-1', 'dark-1', 'inspired-rescue']) {
      const name = width + 'px ' + theme;
      const geometry = await evaluate(`(async () => {
        document.documentElement.dataset.theme = ${JSON.stringify(theme)};
        const list = document.getElementById('messages'); list.scrollTop = 0;
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const rect = el => { const r = el.getBoundingClientRect();
          return { left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height }; };
        const card = id => list.querySelector('[data-message-id="' + id + '"]');
        const gaps = id => {
          const c = card(id), b = c.querySelector('.bubble'), r = rect(b), l = rect(list);
          const copy = c.querySelector('.msg-copy'), target = getComputedStyle(copy, '::after');
          return { bubble:r, row:rect(c), content:rect(c.querySelector('.message-content')), leftGap:r.left-l.left, rightGap:l.right-r.right,
            avatar:rect(c.querySelector('.message-avatar')),
            head:rect(c.querySelector('.message-head')), targets:c.querySelector('.message-targets') ? rect(c.querySelector('.message-targets')) : null,
            copy:rect(copy), copyHitWidth:copy.offsetWidth + (parseFloat(target.left) || 0)*-1 + (parseFloat(target.right) || 0)*-1 };
        };
        const table = card(6).querySelector('.md-table');
        return { width:${width}, theme:${JSON.stringify(theme)}, viewport:document.documentElement.clientWidth,
          coarse:matchMedia('(hover:none) and (pointer:coarse)').matches,
          conversation:rect(list), padding:parseFloat(getComputedStyle(list).paddingRight),
          overflow:list.scrollWidth-list.clientWidth, own:gaps(1), other:gaps(2),
          shortOwn:gaps(3), shortOther:gaps(4), private:gaps(5), table:gaps(6),
          tableScroll:{ width:table.clientWidth, scrollWidth:table.scrollWidth, overflowX:getComputedStyle(table).overflowX },
          privateMarker:!!card(5).querySelector('.private-badge'), privateShadow:getComputedStyle(card(5).querySelector('.bubble')).boxShadow,
          retracted:{ row:rect(card(7)), avatar:rect(card(7).querySelector('.message-avatar')), content:rect(card(7).querySelector('.message-content')), bubble:!!card(7).querySelector('.bubble'), tools:!!card(7).querySelector('.message-tools') },
          system:{ row:rect(card(8)), content:rect(card(8).querySelector('.message-content')), bubble:!!card(8).querySelector('.bubble'), avatar:!!card(8).querySelector('.message-avatar'), tools:!!card(8).querySelector('.message-tools') } };
      })()`);
      measurements.push(geometry);
      if (evidence) {
        const { data } = await call('Page.captureScreenshot', { format:'png' }, sessionId);
        fs.writeFileSync(path.join(evidence, phase + '-' + width + '-' + theme + '.png'), Buffer.from(data, 'base64'));
      }
      try {
        assert.strictEqual(geometry.viewport, width, 'actual viewport');
        assert.strictEqual(geometry.overflow, 0, 'conversation must not scroll sideways');
        for (const key of ['own', 'other', 'shortOwn', 'shortOther', 'private', 'table']) {
          const g = geometry[key];
          assert.ok(g.leftGap >= 0 && g.rightGap >= 0, key + ' fits conversation');
          if (mobile) assert.ok(g.copyHitWidth >= 44, key + ' retains horizontal 44px copy target');
        }
        if (mobile) {
          assert.strictEqual(geometry.coarse, true, 'touch rules active');
          for (const key of ['own', 'shortOwn', 'private']) {
            const g = geometry[key];
            assert.ok(g.rightGap < g.leftGap, key + ' right gap < left gap: ' + JSON.stringify(g));
            assert.ok(Math.abs(g.rightGap - geometry.padding) < .5, key + ' hugs page padding');
            assert.strictEqual(g.avatar.width, 0, key + ' own avatar does not reserve space');
            assert.ok(g.head.left >= g.content.left - .5 && g.head.right <= g.content.right + .5, key + ' header fits');
          }
          for (const key of ['other', 'shortOther', 'table']) {
            const g = geometry[key];
            assert.ok(g.leftGap < g.rightGap, key + ' left gap < right gap: ' + JSON.stringify(g));
            assert.strictEqual(g.avatar.width, 32, key + ' incoming avatar retained');
          }
          const minimumGutter = Math.max(48, geometry.own.row.width * .16);
          assert.ok(geometry.own.leftGap - geometry.padding >= minimumGutter - .5, 'own opposite gutter');
          assert.ok(geometry.other.rightGap - geometry.padding >= minimumGutter - .5, 'incoming opposite gutter');
        } else {
          assert.ok(geometry.own.row.width <= 820, 'desktop row cap');
          assert.ok(geometry.own.bubble.width <= geometry.own.row.width * .72 + .5, 'desktop bubble cap');
          assert.strictEqual(geometry.own.avatar.width, 32, 'desktop own avatar retained');
        }
        if (mobile) assert.ok(geometry.tableScroll.scrollWidth > geometry.tableScroll.width, 'wide table scrolls');
        assert.strictEqual(geometry.tableScroll.overflowX, 'auto');
        assert.ok(geometry.privateMarker && geometry.privateShadow.includes('inset'), 'private marker retained');
        assert.ok(!geometry.retracted.bubble && !geometry.retracted.tools, 'retracted message stays plain');
        assert.strictEqual(geometry.retracted.avatar.width, 32, 'retracted avatar unchanged');
        assert.ok(!geometry.system.bubble && !geometry.system.avatar && !geometry.system.tools, 'system stays plain');
        assert.strictEqual(geometry.system.content.width, geometry.system.row.width, 'system stays full width');
        passed++; console.log('PASS: ' + name);
      } catch (error) { failures.push(name); console.error('FAIL: ' + name + ' — ' + error.message); }
    }
  }
  if (evidence) fs.writeFileSync(path.join(evidence, phase + '-measurements.json'), JSON.stringify({ passed, failures, measurements }, null, 2) + '\n');
  console.log(passed + ' geometry cases passed, ' + failures.length + ' failed');
  assert.strictEqual(failures.length, 0, 'bubble phone geometry');
}

(async () => {
  try {
    await Promise.race([measure(), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Chromium geometry deadline exceeded')), 45000);
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
