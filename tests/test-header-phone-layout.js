// Real Chromium geometry using production conversation/topbar markup and ordered CSS.
// Usage: node tests/test-header-phone-layout.js (skips if Chromium is absent).
// Optional HEADER_EVIDENCE_DIR saves measurements and screenshots outside the repo;
// HEADER_PHASE=before records a baseline without density assertions.
// TMPDIR controls browser scratch placement.
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
if (!binary) { console.log('SKIP: header phone geometry (Chromium absent)'); process.exit(0); }
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
  + ['js/06-core.js', 'js/09-time.js', 'js/10-markdown.js', 'js/11-conversation.js', 'js/20-workspace.js']
    .map(name => fs.readFileSync(path.join(web, name), 'utf8')).join('\n');
const evidence = process.env.HEADER_EVIDENCE_DIR;
const phase = process.env.HEADER_PHASE || 'after';
assert.ok(/^[a-z-]+$/.test(phase), 'safe artifact label');
if (evidence) fs.mkdirSync(evidence, { recursive: true });
const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
  .replace(/<link\b[^>]*>/g, '') // no font/manifest network requests
  .replace('<!--__TRIO_STYLES__-->', '<style>' + css + '</style>')
  .replace('<!--__TRIO_SCRIPTS__-->', '<script>' + scripts + '</script>');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'nth-headers-chromium-'));
const fixture = path.join(temporary, 'headers.html');
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
    Trio.state.channel = 'layout-demo';
    Trio.state.readOnly = false;
    Trio.state.loaded = { meta:true, agents:true };
    Trio.state.members = new Map([
      ['operator', { id:'operator', name:'Operator', status:'active' }],
      ['agent', { id:'agent', name:'Agent', kind:'agent', status:'active' }],
      ['peer', { id:'peer', name:'Peer', kind:'agent', status:'active' }],
      ['helper', { id:'helper', name:'Helper', kind:'agent', status:'active' }],
      ['reviewer', { id:'reviewer', name:'Reviewer', kind:'agent', status:'active' }],
      ['long-name', { id:'long-name', name:'AnExtremelyLongSyntheticParticipantName', status:'offline' }]
    ]);
    Trio.setChannelTitle('#layout-demo-with-a-long-title');
    document.getElementById('h-meta').textContent = 'Live agent workspace';
    Trio.workspace.renderChannelMetadataState();
    Trio.workspace.renderFacePile();
    document.body.classList.add('message-numbers');
    const list = document.getElementById('messages');
    const base = { created_at:'2026-01-01T12:00:00Z', channel:'layout-demo' };
    const messages = [
      { id:1, member_id:'operator', content:'The original message.' },
      { id:2, member_id:'agent', content:'A reply with one recipient. This message wraps on a phone.', mentions:['operator'], reply_to:1 },
      { id:3, member_id:'operator', content:'An outgoing reply with one recipient.', mentions:['agent'], reply_to:2 },
      { id:4, member_id:'agent', content:'Several recipients keep their full names.', mentions:['operator','peer','helper'], refs:['reviewer'], bangs:['peer'], reply_to:3 },
      { id:5, member_id:'operator', content:'A private task reply.', recipients:['agent'], mentions:['agent'], is_dm:true, task_id:9, confidence:'high', reply_to:4 },
      { id:123456, member_id:'long-name', content:'Long names and message numbers wrap without losing metadata.', mentions:['long-name'], reply_to:123455 }
    ];
    list.replaceChildren(...messages.map(msg => Trio.conversation.cardFor({ ...base, ...msg })));
    window.headerCopies = [];
    Trio.ui = { copyText: text => { window.headerCopies.push(text); return Promise.resolve(); } };
  })()`);
  let passed = 0;
  const failures = [], measurements = [];
  for (const width of [360, 390, 412, 1280]) {
    const mobile = width < 640;
    await call('Emulation.setTouchEmulationEnabled', { enabled:mobile, maxTouchPoints:1 }, sessionId);
    await call('Emulation.setDeviceMetricsOverride', { width, height:900, deviceScaleFactor:1, mobile }, sessionId);
    // Re-render the actual responsive face pile, including its overflow count.
    await evaluate('Trio.workspace.renderFacePile()');
    for (const theme of ['light-1', 'dark-1', 'inspired-rescue']) {
      const name = width + 'px ' + theme;
      const geometry = await evaluate(`(async () => {
        document.documentElement.dataset.theme = ${JSON.stringify(theme)};
        const list = document.getElementById('messages'); list.scrollTop = 0;
        document.activeElement?.blur();
        document.querySelectorAll('.msg-time.copied').forEach(el => el.classList.remove('copied'));
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const rect = el => {
          const range = document.createRange(); range.selectNodeContents(el);
          const r = getComputedStyle(el).display === 'contents' ? range.getBoundingClientRect() : el.getBoundingClientRect();
          return { left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height }; };
        const card = id => list.querySelector('[data-message-id="' + id + '"]');
        const header = id => {
          const c = card(id), head = c.querySelector('.message-head'), reply = c.querySelector('.reply-context');
          const targets = c.querySelector('.message-targets'), stamp = c.querySelector('time');
          return { head:rect(head), author:rect(head.querySelector('strong')), reply:rect(reply),
            targets:rect(targets), time:rect(stamp), content:rect(c.querySelector('.message-content')),
            totalHeight:c.querySelector('.bubble').getBoundingClientRect().top - head.getBoundingClientRect().top,
            inline:reply.parentNode === head && targets.parentNode === head,
            endStamp:head.lastElementChild === stamp, replyLabel:reply.getAttribute('aria-label'),
            numberVisible:getComputedStyle(stamp.querySelector('.message-id')).display !== 'none',
            targetText:targets.textContent, datetime:stamp.getAttribute('datetime') };
        };
        const bar = document.querySelector('.conversation-header');
        const buttons = [...bar.querySelectorAll('.icon-btn'), document.getElementById('face-pile')]
          .filter(el => getComputedStyle(el).display !== 'none').map(el => ({ id:el.id, ...rect(el) }));
        const meta = document.getElementById('h-meta');
        const title = document.getElementById('h-channel');
        return { width:${width}, theme:${JSON.stringify(theme)}, viewport:document.documentElement.clientWidth,
          coarse:matchMedia('(hover:none) and (pointer:coarse)').matches,
          topbar:rect(bar), title:rect(title), titleOverflow:title.scrollWidth > title.clientWidth,
          metaVisible:getComputedStyle(meta).display !== 'none', buttons,
          overflow:list.scrollWidth-list.clientWidth, own:header(3), other:header(2), many:header(4), private:header(5) };
      })()`);
      measurements.push(geometry);
      if (evidence) {
        const { data } = await call('Page.captureScreenshot', { format:'png' }, sessionId);
        fs.writeFileSync(path.join(evidence, phase + '-' + width + '-' + theme + '.png'), Buffer.from(data, 'base64'));
      }
      try {
        if (phase !== 'before') {
          assert.strictEqual(geometry.viewport, width, 'actual viewport');
          assert.strictEqual(geometry.overflow, 0, 'conversation fits viewport');
          for (const key of ['own', 'other', 'many', 'private']) {
            const g = geometry[key];
            assert.ok(g.inline && g.endStamp, key + ' metadata in header, time at end');
            assert.strictEqual(g.replyLabel, 'Replying to message #' + (key === 'other' ? 1 : key === 'own' ? 2 : key === 'many' ? 3 : 4));
            assert.ok(g.numberVisible, 'message-number setting on');
            assert.strictEqual(g.datetime, '2026-01-01T12:00:00.000Z');
            assert.ok(g.head.left >= g.content.left - .5 && g.head.right <= g.content.right + .5, key + ' header fits content');
            if (mobile) {
              for (const target of [g.reply, g.time]) {
                assert.ok(target.width >= 44 && target.height >= 44, key + ' 44px touch target');
              }
            }
          }
          assert.ok(geometry.many.targetText.includes('@Operator') && geometry.many.targetText.includes('#Reviewer') && geometry.many.targetText.includes('!Peer'), 'all sigils kept');
          if (mobile) {
            assert.strictEqual(geometry.coarse, true, 'touch emulation active');
            assert.ok(geometry.topbar.height <= 64, 'phone top bar <=64px: ' + geometry.topbar.height);
            assert.ok(!geometry.metaVisible, 'static subtitle hidden on phone');
            assert.ok(geometry.titleOverflow && geometry.title.width > 0, 'title truncates with space left');
            for (const button of geometry.buttons) {
              assert.ok(button.width >= 44 && button.height >= 44, button.id + ' 44px touch target');
              assert.ok(button.left >= 0 && button.right <= width, button.id + ' fits viewport');
            }
            if (width >= 360) {
              for (const key of ['own', 'other']) {
                const g = geometry[key];
                assert.ok(g.head.height <= 44.5, key + ' reply+recipient fits one line: ' + g.head.height);
                assert.ok(Math.abs(key === 'own' ? g.head.right - g.content.right : g.head.left - g.content.left) < .5,
                  key + ' header aligns with its bubble side');
                assert.ok(Math.abs(g.reply.top - g.time.top) < .5, key + ' reply and timestamp on same line');
                assert.ok(g.targets.top >= g.head.top && g.targets.bottom <= g.head.bottom, key + ' recipients share the row');
              }
            }
          }
          const behavior = await evaluate(`(async () => {
            document.body.classList.remove('message-numbers');
            const hidden = getComputedStyle(document.querySelector('.message-id')).display === 'none';
            document.body.classList.add('message-numbers');
            const target = document.querySelector('[data-message-id="1"]');
            let jumped = false; target.scrollIntoView = () => { jumped = true; };
            document.querySelector('[data-message-id="2"] .reply-context').click();
            document.querySelector('[data-message-id="2"] time').click();
            await Promise.resolve();
            const hitTargets = [...document.querySelectorAll('[data-message-id="2"] .reply-context, [data-message-id="2"] time')];
            const hits = hitTargets.every(el => {
              el.scrollIntoView({ block:'center' });
              const r = el.getBoundingClientRect();
              return [[r.left+1,r.top+r.height/2], [r.right-1,r.top+r.height/2],
                [r.left+r.width/2,r.top+1], [r.left+r.width/2,r.bottom-1]]
                .every(([x,y]) => el.contains(document.elementFromPoint(x,y)));
            });
            Trio.state.loaded.agents = false; Trio.workspace.renderChannelMetadataState();
            const meta = document.getElementById('h-meta');
            const loading = { visible:getComputedStyle(meta).display !== 'none', text:meta.textContent, width:meta.getBoundingClientRect().width };
            Trio.setChannelSubtitle('Connecting…');
            const connecting = { visible:getComputedStyle(meta).display !== 'none', text:meta.textContent, width:meta.getBoundingClientRect().width };
            Trio.state.loaded.agents = true; Trio.workspace.renderChannelMetadataState();
            return { hidden, jumped, focused:document.activeElement === target, hits, loading, connecting, copied:window.headerCopies.at(-1), height:document.querySelector('.conversation-header').getBoundingClientRect().height };
          })()`);
          geometry.behavior = behavior;
          assert.ok(behavior.hidden && behavior.jumped && behavior.focused, 'numbers setting and reply jump/focus');
          assert.ok(behavior.hits, 'reply and timestamp hit areas are reachable');
          assert.strictEqual(behavior.copied, '2026-01-01T12:00:00.000Z', 'tap copies timestamp');
          assert.ok(behavior.loading.visible && behavior.loading.width > 0 && behavior.connecting.visible && behavior.connecting.width > 0, 'loading and connecting status remains visible');
          if (mobile) assert.ok(behavior.height <= 64, 'status does not enlarge phone bar');
          if (evidence) {
            await evaluate("Trio.setChannelSubtitle('Connecting…'); document.getElementById('messages').scrollTop = 0; document.activeElement?.blur()");
            const { data } = await call('Page.captureScreenshot', { format:'png' }, sessionId);
            fs.writeFileSync(path.join(evidence, phase + '-' + width + '-' + theme + '-connecting.png'), Buffer.from(data, 'base64'));
            await evaluate('Trio.workspace.renderChannelMetadataState()');
          }
        }
        passed++; console.log('PASS: ' + name + ' (bar ' + geometry.topbar.height + 'px, reply header ' + geometry.other.totalHeight + 'px)');
      } catch (error) { failures.push(name); console.error('FAIL: ' + name + ' — ' + error.message); }
    }
  }
  if (evidence) fs.writeFileSync(path.join(evidence, phase + '-measurements.json'), JSON.stringify({ passed, failures, measurements }, null, 2) + '\n');
  console.log(passed + ' geometry cases passed, ' + failures.length + ' failed');
  assert.strictEqual(failures.length, 0, 'header phone geometry');
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
