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
const themeSource = fs.readFileSync(path.join(web, 'js/40-preferences.js'), 'utf8');
const themeList = themeSource.match(/const themes = \[([\s\S]*?)\];/);
assert.ok(themeList, 'registered theme list exists');
const themes = [...themeList[1].matchAll(/id: '([^']+)'/g)].map(m => m[1]);
assert.ok(themes.length > 0, 'registered themes are populated');
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
  await evaluate(`(async () => {
    Trio.state.members = new Map([
      ['operator', { id:'operator', name:'Operator' }],
      ['agent', { id:'agent', name:'Agent', kind:'agent' }],
      ['coordinator', { id:'coordinator', name:'BuildCoordinator', kind:'agent' }],
      ['long', { id:'long', name:'Member' + 'LongName'.repeat(9), kind:'agent' }]
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
      { id:8, member_id:'agent', content:'[joined] Agent' },
      { id:9, member_id:'operator', content:'Recipients.', mentions:['long', 'agent'], refs:['long'], bangs:['long'] },
      { id:10, member_id:'coordinator', content:'Private incoming.', recipients:['operator'], is_dm:true },
      { id:11, member_id:'long', content:'Long incoming author.', recipients:['operator'], is_dm:true, confidence:'medium', task_id:123456789 },
      { id:12, member_id:'agent', content:'Metadata incoming.', recipients:['operator'], is_dm:true, confidence:'medium', task_id:123456789 },
      { id:13, member_id:'operator', content:'Landscape attachment.', attachments:[{id:1, mime:'image/png', filename:'landscape.png'}] },
      { id:14, member_id:'agent', content:'Landscape attachment.', attachments:[{id:2, mime:'image/png', filename:'landscape.png'}] },
      { id:15, member_id:'agent', content:'Incoming recipients.', mentions:['long'] },
      { id:16, member_id:'operator', content:String.fromCharCode(96).repeat(3) + 'text\\n' + 'long-code-'.repeat(50) + '\\n' + String.fromCharCode(96).repeat(3) }
    ];
    const cards = messages.map(msg => Trio.conversation.cardFor({ ...base, ...msg }));
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
    const context = canvas.getContext('2d'); context.fillStyle = '#448866'; context.fillRect(0, 0, 640, 360);
    const source = canvas.toDataURL('image/png');
    for (const card of cards) for (const image of card.querySelectorAll('.message-attachment img')) {
      image.src = source; image.loading = 'eager'; image.classList.remove('error');
    }
    list.replaceChildren(...cards);
    await Promise.all([...list.querySelectorAll('.message-attachment img')].map(image => image.decode()));

  })()`);
  // Role colors use real rendered cards and every registered theme. Own
  // bubbles must be identical with the new stylesheet block removed.
  const roles = await evaluate(`(() => {
    const results = [];
    const list = document.getElementById('messages');
    Trio.state.members.set('human', {id:'human', name:'Human', kind:'human'});
    const cards = ['agent', 'human', '_op_legacy', 'operator'].map((id, i) =>
      Trio.conversation.cardFor({id:100+i, member_id:id, content:'Role color', created_at:'2026-01-01T12:00:00Z'}));
    list.append(...cards);
    const style = document.querySelector('style');
    const full = style.textContent;
    const before = full.split('/* Sender roles are semantic across themes;')[0];
    const color = c => getComputedStyle(c.querySelector('.bubble')).backgroundColor;
    for (const theme of ${JSON.stringify(themes)}) {
      document.documentElement.dataset.theme = theme;
      const colors = cards.map(color);
      style.textContent = before;
      const ownBefore = color(cards[3]);
      style.textContent = full;
      results.push({theme, colors, ownBefore, kinds:cards.map(c => c.dataset.senderKind || '')});
    }
    cards.forEach(c => c.remove());
    return results;
  })()`);
  for (const {theme, colors, ownBefore, kinds} of roles) {
    assert.deepStrictEqual(kinds, ['agent', 'human', 'human', '']);
    const dark = theme.startsWith('dark-');
    assert.strictEqual(colors[0], dark ? 'rgb(32, 59, 83)' : 'rgb(227, 241, 255)', theme + ' agent blue');
    assert.strictEqual(colors[1], dark ? 'rgb(35, 67, 49)' : 'rgb(229, 245, 232)', theme + ' human green');
    assert.strictEqual(colors[2], colors[1], theme + ' legacy operator human');
    assert.strictEqual(colors[3], ownBefore, theme + ' own unchanged');
  }
  console.log('PASS: human/agent colors and unchanged own bubbles across ' + roles.length + ' themes');
  let passed = 0;
  const failures = [], measurements = [];
  for (const width of [320, 360, 390, 412, 768, 1280]) {
    const mobile = width < 640;
    await call('Emulation.setTouchEmulationEnabled', { enabled:mobile, maxTouchPoints:1 }, sessionId);
    await call('Emulation.setDeviceMetricsOverride', { width, height:900, deviceScaleFactor:1, mobile }, sessionId);
    for (const theme of themes) {
      const name = width + 'px ' + theme;
      const geometry = await evaluate(`(async () => {
        document.documentElement.dataset.theme = ${JSON.stringify(theme)};
        const list = document.getElementById('messages'); list.scrollTop = 0; list.scrollLeft = 0;
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const rect = el => { const r = el.getBoundingClientRect();
          return { left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height }; };
        const card = id => list.querySelector('[data-message-id="' + id + '"]');
        const gaps = id => {
          const c = card(id), b = c.querySelector('.bubble'), r = rect(b), l = rect(list);
          const copy = c.querySelector('.msg-copy'), target = getComputedStyle(copy, '::after');
          return { bubble:r, row:rect(c), content:rect(c.querySelector('.message-content')), leftGap:r.left-l.left, rightGap:l.right-r.right,
            avatar:rect((${mobile} && c.querySelector('.header-avatar')) || c.querySelector('.message-avatar')),
            head:rect(c.querySelector('.message-head')), targets:c.querySelector('.message-targets') ? rect(c.querySelector('.message-targets')) : null,
            headerChildren:[...c.querySelector('.message-head').children].filter(el => el.getClientRects().length && getComputedStyle(el).clipPath !== 'inset(50%)').map(el => ({ tag:el.tagName, className:el.className, ...rect(el), whiteSpace:getComputedStyle(el).whiteSpace })),
            chips:[...c.querySelectorAll('.target-chip')].filter(el => el.getClientRects().length).map(el => rect(el)),
            rowInsets:{ left:parseFloat(getComputedStyle(c).paddingLeft) + parseFloat(getComputedStyle(c).borderLeftWidth), right:parseFloat(getComputedStyle(c).paddingRight) + parseFloat(getComputedStyle(c).borderRightWidth) },
            copy:rect(copy), copyHitWidth:copy.offsetWidth + (parseFloat(target.left) || 0)*-1 + (parseFloat(target.right) || 0)*-1 };
        };
        const table = card(6).querySelector('.md-table');
        const imageGeometry = id => { const image = card(id).querySelector('.message-attachment img');
          return { image:rect(image), wrapper:rect(image.parentElement), content:rect(card(id).querySelector('.message-content')),
            naturalWidth:image.naturalWidth, naturalHeight:image.naturalHeight }; };
        const code = card(16).querySelector('pre.mdcode');
        return { width:${width}, theme:${JSON.stringify(theme)}, viewport:document.documentElement.clientWidth,
          coarse:matchMedia('(hover:none) and (pointer:coarse)').matches,
          conversation:rect(list), padding:parseFloat(getComputedStyle(list).paddingRight), rowPadding:parseFloat(getComputedStyle(card(1)).paddingRight),
          avatarWidth:parseFloat(getComputedStyle((${mobile} && card(2).querySelector('.header-avatar')) || card(2).querySelector('.message-avatar')).width),
          retractedAvatarWidth:parseFloat(getComputedStyle(card(7).querySelector('.message-avatar')).width),
          overflow:list.scrollWidth-list.clientWidth, own:gaps(1), other:gaps(2),
          shortOwn:gaps(3), shortOther:gaps(4), private:gaps(5), table:gaps(6),
          longChip:gaps(9), incomingPrivate:gaps(10), longAuthor:gaps(11), incomingMetadata:gaps(12), incomingChip:gaps(15),
          ownImage:imageGeometry(13), incomingImage:imageGeometry(14),
          codeScroll:{ width:code.clientWidth, scrollWidth:code.scrollWidth, overflowX:getComputedStyle(code).overflowX },
          codeCopy:rect(card(16).querySelector('.code-copy')),

          tableScroll:{ width:table.clientWidth, scrollWidth:table.scrollWidth, overflowX:getComputedStyle(table).overflowX },
          privateMarker:!!card(5).querySelector('.private-badge'), privateShadow:getComputedStyle(card(5).querySelector('.bubble')).boxShadow,
          retracted:{ row:rect(card(7)), avatar:rect(card(7).querySelector('.message-avatar')), content:rect(card(7).querySelector('.message-content')), bubble:!!card(7).querySelector('.bubble'), tools:!!card(7).querySelector('.message-tools') },
          system:{ row:rect(card(8)), content:rect(card(8).querySelector('.message-content')), bubble:!!card(8).querySelector('.bubble'), avatar:!!card(8).querySelector('.message-avatar'), tools:!!card(8).querySelector('.message-tools') } };
      })()`);
      if (mobile) geometry.chipHits = await evaluate(`(() => {
        const list = document.getElementById('messages');
        const hits = [...list.querySelectorAll('[data-message-id="9"] .target-chip, [data-message-id="15"] .target-chip')].filter(chip => chip.getClientRects().length).map(chip => {
          chip.scrollIntoView({ block:'center' });
          const r = chip.getBoundingClientRect();
          return [r.left + 2, r.right - 2].map(x => {
            const hit = document.elementFromPoint(x, r.top + r.height / 2);
            return hit === chip || chip.contains(hit);
          });
        });
        list.scrollTop = 0; list.scrollLeft = 0;
        return hits;
      })()`);
      measurements.push(geometry);
      if (evidence) {
        const { data } = await call('Page.captureScreenshot', { format:'png' }, sessionId);
        fs.writeFileSync(path.join(evidence, phase + '-' + width + '-' + theme + '.png'), Buffer.from(data, 'base64'));
        if ([320, 390].includes(width) && ['light-1', 'inspired-slack', 'inspired-messenger'].includes(theme)) {
          for (const id of [9, 11, 14]) {
            await evaluate(`document.querySelector('[data-message-id="${id}"]').scrollIntoView({ block:'center' })`);
            const shot = await call('Page.captureScreenshot', { format:'png' }, sessionId);
            fs.writeFileSync(path.join(evidence, phase + '-' + width + '-' + theme + '-message-' + id + '.png'), Buffer.from(shot.data, 'base64'));
          }
        }
      }
      try {
        assert.strictEqual(geometry.viewport, width, 'actual viewport');
        if (mobile) assert.strictEqual(geometry.overflow, 0, 'conversation must not scroll sideways');
        for (const key of ['own', 'other', 'shortOwn', 'shortOther', 'private', 'table', 'longChip', 'incomingPrivate', 'longAuthor', 'incomingMetadata', 'incomingChip']) {
          const g = geometry[key];
          assert.ok(g.leftGap >= 0 && g.rightGap >= 0, key + ' fits conversation');
          if (mobile) {
            assert.strictEqual(g.copy.width, 0, key + ' outside copy affordance hidden on phone');
            assert.strictEqual(g.copy.height, 0, key + ' copy spends no row');
            assert.ok(g.head.left >= g.content.left - .5 && g.head.right <= g.content.right + .5, key + ' header fits content');
            for (const child of g.headerChildren) {
              assert.ok(child.left >= g.content.left - .5 && child.right <= g.content.right + .5,
                key + ' header child fits content: ' + JSON.stringify(child));
              if (child.tag === 'TIME') assert.strictEqual(child.whiteSpace, 'nowrap', 'timestamp stays together');
            }
            for (const chip of g.chips) assert.ok(chip.left >= g.content.left - .5 && chip.right <= g.content.right + .5,
              key + ' chip fits content: ' + JSON.stringify(chip));
          }
        }
        if (mobile) {
          assert.strictEqual(geometry.coarse, true, 'touch rules active');
          assert.ok(geometry.chipHits.length >= 4 && geometry.chipHits.flat().every(Boolean), 'recipient chip edges are reachable');
          assert.ok(geometry.incomingMetadata.headerChildren.some(child => child.className === 'private-badge'), 'incoming private fixture');
          assert.ok(geometry.incomingMetadata.headerChildren.some(child => child.className === 'task-chip'), 'incoming task fixture');
          assert.ok(geometry.incomingMetadata.headerChildren.some(child => child.className.includes('confidence-')), 'incoming confidence fixture');
          for (const key of ['own', 'shortOwn', 'private', 'longChip']) {
            const g = geometry[key];
            assert.ok(g.rightGap < g.leftGap, key + ' right gap < left gap: ' + JSON.stringify(g));
            assert.ok(Math.abs(g.bubble.right - (g.row.right - g.rowInsets.right)) < .5, key + ' hugs row edge');
            assert.strictEqual(g.avatar.width, 0, key + ' own avatar does not reserve space');
            assert.ok(g.head.left >= g.content.left - .5 && g.head.right <= g.content.right + .5, key + ' header fits');
          }
          for (const key of ['other', 'shortOther', 'table']) {
            const g = geometry[key];
            assert.ok(g.leftGap < g.rightGap, key + ' left gap < right gap: ' + JSON.stringify(g));
            assert.ok(Math.abs(g.bubble.left-g.row.left-g.rowInsets.left)<.5, key + ' incoming bubble starts at page gutter');
            assert.strictEqual(g.avatar.width, 22, key + ' incoming avatar stays in the header');
          }
          for (const key of ['own', 'other']) {
            const g = geometry[key], rowWidth = g.row.width - g.rowInsets.left - g.rowInsets.right;
            const minimumGutter = Math.min(72, Math.max(theme === 'inspired-slack' ? 52 : 48, rowWidth * .16));
            const gutter = key === 'own' ? g.bubble.left - g.row.left - g.rowInsets.left
              : g.row.right - g.rowInsets.right - g.bubble.right;
            assert.ok(gutter >= minimumGutter - .5, key + ' opposite gutter');
          }
        } else if (['light-1', 'dark-1', 'inspired-rescue'].includes(theme)) {
          assert.ok(geometry.own.row.width <= 820, 'desktop row cap');
          assert.ok(geometry.own.bubble.width <= geometry.own.row.width * .72 + .5, 'desktop bubble cap');
          assert.strictEqual(geometry.own.avatar.width, 32, 'desktop own avatar retained');
        }
        if (mobile) for (const key of ['ownImage', 'incomingImage']) {
          const g = geometry[key];
          assert.strictEqual(g.naturalWidth, 640, key + ' intrinsic image loaded');
          assert.strictEqual(g.naturalHeight, 360, key + ' intrinsic image loaded');
          assert.ok(g.image.width > 0, key + ' image visible');
          assert.ok(g.image.left >= g.wrapper.left - .5 && g.image.right <= g.wrapper.right + .5, key + ' image fits wrapper');
          assert.ok(g.wrapper.left >= g.content.left - .5 && g.wrapper.right <= g.content.right + .5, key + ' wrapper fits content');
        }
        assert.strictEqual(geometry.codeScroll.overflowX, 'auto', 'code scroll stays local');
        assert.ok(geometry.codeScroll.scrollWidth > geometry.codeScroll.width, 'wide code scrolls');
        if (mobile) assert.ok(geometry.tableScroll.scrollWidth > geometry.tableScroll.width, 'wide table scrolls');
        assert.strictEqual(geometry.tableScroll.overflowX, 'auto');
        assert.ok(geometry.privateMarker, 'private marker retained');
        if (['light-1', 'dark-1', 'inspired-rescue'].includes(theme)) assert.ok(geometry.privateShadow.includes('inset'), 'private shadow retained');
        assert.ok(!geometry.retracted.bubble && !geometry.retracted.tools, 'retracted message stays plain');
        assert.strictEqual(geometry.retracted.avatar.width, theme === 'inspired-messenger' ? 0 : geometry.retractedAvatarWidth, 'retracted avatar unchanged');
        assert.ok(!geometry.system.bubble && !geometry.system.avatar && !geometry.system.tools, 'system stays plain');
        if (['light-1', 'dark-1', 'inspired-rescue'].includes(theme)) assert.strictEqual(geometry.system.content.width, geometry.system.row.width, 'system stays full width');
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
