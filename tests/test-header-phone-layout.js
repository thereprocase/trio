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
const baselineRef = process.env.HEADER_BASELINE_REF;
const read = name => baselineRef ? spawnSync('git', ['show', baselineRef + ':server/' + name], { cwd:path.dirname(root), encoding:'utf8' }).stdout : fs.readFileSync(path.join(root,name),'utf8');
const declaration = read('nth_web.py').match(/WEB_CSS_FILES = \(([\s\S]*?)\)/);
assert.ok(declaration, 'production CSS order exists');
const cssFiles = [...declaration[1].matchAll(/"(css\/[^"]+)"/g)].map(m => m[1]);
assert.ok(cssFiles.length, 'production CSS list is populated');
const css = cssFiles.map(name => read('web/' + name)).join('\n');
const themeSource = read('web/js/40-preferences.js');
const themes = [...themeSource.match(/const themes = \[([\s\S]*?)\];/)[1].matchAll(/id: '([^']+)'/g)].map(m => m[1]);
assert.strictEqual(themes.length, 21, 'all registered themes');
const scripts = `window.Trio = { state: { operator: { id: 'operator' }, readOnly: true },
  api: {}, events: new EventTarget(), actions: {}, avatarTone: () => 'eucalyptus' };\n`
  + ['js/06-core.js', 'js/09-time.js', 'js/10-markdown.js', 'js/11-conversation.js', 'js/20-workspace.js']
    .map(name => read('web/' + name)).join('\n');
// Six synthetic one-line acknowledgements measured in Chromium at 390px,
// with production markup/CSS from gutter commit 60be439, before density changes.
const BASELINE_ACK_HEIGHT = {"light-1": 605.625, "dark-1": 605.625, "inspired-rescue": 607.125};
const evidence = process.env.HEADER_EVIDENCE_DIR;
const phase = process.env.HEADER_PHASE || 'after';
assert.ok(/^[a-z-]+$/.test(phase), 'safe artifact label');
if (evidence) fs.mkdirSync(evidence, { recursive: true });
const html = read('web/index.html')
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
  await evaluate(String.raw`(() => {
    Trio.state.channel='layout-demo'; Trio.state.readOnly=false;
    Trio.state.loaded={meta:true,agents:true};
    Trio.state.members=new Map([
      ['operator',{id:'operator',name:'Operator',kind:'human'}],
      ['agent',{id:'agent',name:'Agent',kind:'agent'}],
      ['peer',{id:'peer',name:'Peer',kind:'agent'}],
      ['helper',{id:'helper',name:'Helper',kind:'agent'}],
      ['reviewer',{id:'reviewer',name:'Reviewer',kind:'agent'}],
      ['long',{id:'long',name:'AnExtremelyLongSyntheticParticipantName',kind:'agent'}]
    ]);
    Trio.setChannelTitle('#layout-demo-with-a-long-title');
    Trio.workspace.renderChannelMetadataState(); Trio.workspace.renderFacePile();
    document.body.classList.add('message-numbers');
    const base={channel:'layout-demo',created_at:'2026-01-01T12:00:00Z'};
    window.syntheticMessages=[
      ...Array.from({length:6},(_,i)=>({...base,id:i+1,member_id:i<3?'agent':'peer',content:['Ready.','Acknowledged.','On it.'][i%3],created_at:'2026-01-01T12:0'+i+':00Z'})),
      {...base,id:7,member_id:'operator',content:'Thanks.'},
      {...base,id:8,member_id:'agent',content:'The first pass is ready for review. The narrow layout now has room for short acknowledgements as well as longer explanations.\n\nEach update keeps the conversation in order and retains the original text. A reviewer can follow the reply marker, expand the recipients, or copy the message from its actions menu.\n\nThe next step is to compare the screenshots and verify the interaction targets.',mentions:['operator','peer','helper','reviewer'],reply_to:7},
      {...base,id:9,member_id:'long',content:'A long sender name remains available.'},
      {...base,id:10,member_id:'agent',content:'A long recipient name still fits.',mentions:['long'],reply_to:9},
      {...base,id:11,member_id:'peer',content:'[joined] Peer'},
      {...base,id:12,member_id:'agent',content:'Code example:\n\n\`\`\`text\nhello world\n\`\`\`'}
    ];
    window.paintSynthetic=()=>{const list=document.getElementById('messages');list.replaceChildren(...window.syntheticMessages.map(m=>Trio.conversation.cardFor(m)));Trio.conversation.syncMessageGroups?.(list);list.scrollTop=0;};
    window.paintSynthetic(); window.copies=[];
    Trio.ui={copyText:text=>{window.copies.push(text);return Promise.resolve();},setLive:()=>{}};
  })()`);
  const measurements=[], failures=[];let passed=0;
  for (const width of [360,390,412,1280]) {
    const mobile=width<=640;
    await call('Emulation.setTouchEmulationEnabled',{enabled:mobile,maxTouchPoints:1},sessionId);
    await call('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile},sessionId);
    for (const theme of themes) {
      const geometry=await evaluate(`(async()=>{
        document.documentElement.dataset.theme=${JSON.stringify(theme)};window.paintSynthetic();
        await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
        const list=document.getElementById('messages'),cards=[...list.querySelectorAll('.message')];
        const rect=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
        const ack=cards.slice(0,6),own=cards.find(c=>c.dataset.messageId==='7');
        return {width:${width},theme:${JSON.stringify(theme)},overflow:list.scrollWidth-list.clientWidth,
          ackHeight:rect(ack.at(-1)).bottom-rect(ack[0]).top,
          rows:cards.map(c=>({id:Number(c.dataset.messageId),...rect(c)})),
          grouped:ack.map(c=>({grouped:c.classList.contains('message-grouped'),avatar:getComputedStyle(c.querySelector('.header-avatar')||c.querySelector('.message-avatar')).display,
            author:rect(c.querySelector('.message-head strong')),authorText:c.querySelector('.message-head strong').textContent,
            time:rect(c.querySelector('.bubble-time')||c.querySelector('time')),bubble:rect(c.querySelector('.bubble'))})),
          own:{author:rect(own.querySelector('.message-head strong')),head:rect(own.querySelector('.message-head')),
            avatar:rect(own.querySelector('.message-avatar')),time:rect(own.querySelector('.bubble-time')||own.querySelector('time')),bubble:rect(own.querySelector('.bubble'))},
          topbar:rect(document.querySelector('.conversation-header'))};
      })()`);
      measurements.push(geometry);
      try {
        if(phase!=='before') {
          assert.strictEqual(geometry.overflow,0,'conversation fits viewport');
          if(mobile) {
            for(const i of [1,2,4,5]) {
              const g=geometry.grouped[i];assert.ok(g.grouped,'same-sender run groups');
              assert.strictEqual(g.avatar,'none','repeated avatar hidden');
              assert.ok(g.author.width<=1 && g.author.height<=1,'repeated name visually hidden');
              assert.ok(g.authorText,'author still available to screen readers');
              assert.strictEqual(g.bubble.left,geometry.grouped[i<3?0:3].bubble.left,'run bubbles align');
              if(['light-1','dark-1','inspired-rescue'].includes(theme))assert.strictEqual(g.bubble.left,16,'incoming bubble starts at the 16px page padding');
            }
            assert.ok(geometry.own.author.width<=1 && geometry.own.head.height===0,'own message has no name header');
            assert.strictEqual(geometry.own.avatar.width,0,'own avatar absent');
            for(const g of [...geometry.grouped,geometry.own]) assert.ok(g.time.left>=g.bubble.left && g.time.right<=g.bubble.right && g.time.top>=g.bubble.top && g.time.bottom<=g.bubble.bottom,'clock inside bubble');
            assert.ok(geometry.topbar.height<=64,'phone app bar stays compact');
            if(width===390 && ['light-1','dark-1','inspired-rescue'].includes(theme)) assert.ok(geometry.ackHeight<=BASELINE_ACK_HEIGHT[theme]*.60,'ack cluster <=60% of measured pre-change height: '+geometry.ackHeight);
          }else for(const i of [1,2,4,5])assert.ok(geometry.grouped[i].author.width>1,'desktop repeated names remain');
          const behavior=await evaluate(`(async()=>{
            const list=document.getElementById('messages');
            document.body.classList.remove('message-numbers');
            const hidden=[...list.querySelectorAll('.message-id')].every(el=>getComputedStyle(el).display==='none');
            document.body.classList.add('message-numbers');
            const visible=[...list.querySelectorAll('.message-id')].filter(el=>el.getClientRects().length).every(el=>getComputedStyle(el).display!=='none');
            const more=list.querySelector('[data-message-id="8"] .targets-toggle');
            const before=[...list.querySelectorAll('[data-message-id="8"] .target-chip')].filter(el=>el.getClientRects().length).length;
            more.click();const after=[...list.querySelectorAll('[data-message-id="8"] .target-chip')].filter(el=>el.getClientRects().length).length;
            const expanded=more.getAttribute('aria-expanded');more.click();
            const hitTargets=[...list.querySelectorAll('time[datetime],.reply-context,.targets-toggle,.code-copy')].filter(el=>el.getClientRects().length);
            const targets=hitTargets.map(el=>{
              el.scrollIntoView({block:'center'});const box=el.getBoundingClientRect(),p=getComputedStyle(el,'::after');
              const r={left:box.left+(parseFloat(p.left)||0),right:box.right-(parseFloat(p.right)||0),top:box.top+(parseFloat(p.top)||0),bottom:box.bottom-(parseFloat(p.bottom)||0)};
              const width=r.right-r.left,height=r.bottom-r.top;
              const hits=[[r.left+1,(r.top+r.bottom)/2],[r.right-1,(r.top+r.bottom)/2],[(r.left+r.right)/2,r.top+1],[(r.left+r.right)/2,r.bottom-1]].map(([x,y])=>el.contains(document.elementFromPoint(x,y)));
              return {id:el.closest('.message').dataset.messageId,cls:el.className,width,height,hits,label:el.getAttribute('aria-label')||el.title};
            });
            // Contextmenu is the same menu path opened by a touch long press.
            const incoming=list.querySelector('[data-message-id="1"]'),bubble=incoming.querySelector('.bubble');
            if (${mobile} && ${width} === 390 && ['light-1','dark-1','inspired-rescue'].includes(${JSON.stringify(theme)})) {
              bubble.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerType:'touch',isPrimary:true,clientX:100,clientY:100}));
              await new Promise(resolve=>setTimeout(resolve,520));
              bubble.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerType:'touch'}));
            } else bubble.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));
            const menu=incoming.querySelector('.message-actions-menu'),copy=menu?.querySelector('.menu-copy');
            const copyRect=copy?.getBoundingClientRect();if(copy && ${mobile})copy.click();await Promise.resolve();
            list.querySelector('[data-message-id="12"] .code-copy').click();await Promise.resolve();
            let jumped=false;const replyTarget=list.querySelector('[data-message-id="7"]');replyTarget.scrollIntoView=()=>{jumped=true;};
            list.querySelector('[data-message-id="8"] .reply-context').click();
            const focused=document.activeElement===replyTarget;
            const clock=list.querySelector('[data-message-id="1"] time[datetime]'+(${mobile}?'.bubble-time':'.header-time'));
            clock.click();await Promise.resolve();
            return {hidden,visible,before,after,expanded,targets,jumped,focused,
              copySize:copyRect?{width:copyRect.width,height:copyRect.height}:null,
              incomingCopied:window.copies.at(-3),codeCopied:window.copies.at(-2),timeCopied:window.copies.at(-1),
              externalCopies:[...list.querySelectorAll('.message-tools')].filter(el=>el.getClientRects().length).length,
              system:{bubble:!!list.querySelector('[data-message-id="11"] .bubble'),font:getComputedStyle(list.querySelector('[data-message-id="11"] .message-body')).fontSize}};
          })()`);
          geometry.behavior=behavior;
          assert.ok(behavior.hidden&&behavior.visible,'message-number preference works');
          assert.strictEqual(behavior.codeCopied,'hello world','code copy retains source');
          assert.strictEqual(behavior.timeCopied,'2026-01-01T12:00:00.000Z','timestamp still copies UTC instant');
          assert.ok(behavior.jumped && behavior.focused,'reply jumps and focuses the original message');
          if(mobile){
            assert.strictEqual(behavior.before,3,'three recipients before expansion');assert.strictEqual(behavior.after,4,'all recipients after expansion');assert.strictEqual(behavior.expanded,'true');
            assert.strictEqual(behavior.externalCopies,0,'no outside copy row on phone');
            assert.strictEqual(behavior.incomingCopied,'Ready.','incoming message copies from long-press menu');
            assert.ok(behavior.copySize.width>=44 && behavior.copySize.height>=44,'menu copy target 44px');
            for(const t of behavior.targets){assert.ok(t.width>=44&&t.height>=44,t.cls+' 44px target');assert.ok(t.hits.every(Boolean),JSON.stringify(t)+' actual hit edges');}
            assert.ok(!behavior.system.bubble && behavior.system.font==='11px','system line has no bubble and dim small text');
          }
        }
        passed++;console.log('PASS: '+width+'px '+theme+' (ack '+geometry.ackHeight+'px)');
      }catch(error){failures.push(width+'px '+theme);console.error('FAIL: '+width+'px '+theme+' — '+error.message);}
    }
  }
  if (phase !== 'before') {
    const lifecycle = await evaluate(`(async()=>{
      Trio.state.readOnly=true;
      const make=(id,member_id,created_at)=>({id,member_id,created_at,channel:'layout-demo',content:'Acknowledged.'});
      const first=make(101,'agent','2026-01-01T12:00:00Z'), second=make(103,'agent','2026-01-01T12:05:00Z');
      Trio.state.messages=new Map([[101,first],[103,second]]);Trio.state.messageDomById.clear();
      Trio.conversation.render();const card=id=>document.querySelector('[data-message-id="'+id+'"]');
      const withinFive=card(103).classList.contains('message-grouped');
      Trio.conversation.upsert(make(103,'agent','2026-01-01T12:05:01Z'));
      const expired=!card(103).classList.contains('message-grouped');
      Trio.conversation.upsert(second);
      Trio.conversation.upsert(make(102,'peer','2026-01-01T12:02:00Z'));
      await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      const inserted=!card(103).classList.contains('message-grouped');
      Trio.conversation.upsert(make(102,'agent','2026-01-01T12:02:00Z'));
      const edited=card(102).classList.contains('message-grouped')&&card(103).classList.contains('message-grouped');
      Trio.conversation.upsert({id:102,member_id:'agent',content:'Acknowledged.',retracted_at:'2026-01-01T12:06:00Z'});
      const retracted=!card(103).classList.contains('message-grouped');
      return {withinFive,expired,inserted,edited,retracted};
    })()`);
    assert.ok(Object.values(lifecycle).every(Boolean), 'production render and incremental run boundaries: '+JSON.stringify(lifecycle));
  }
  if(evidence)fs.writeFileSync(path.join(evidence,phase+'-measurements.json'),JSON.stringify({passed,failures,measurements},null,2)+'\n');
  console.log(passed+' geometry cases passed, '+failures.length+' failed');assert.strictEqual(failures.length,0,'phone headers');
}

(async()=>{
  try{await Promise.race([measure(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Chromium geometry deadline exceeded')),45000);})]);}
  catch(error){console.error(error.stack);process.exitCode=1;}
  finally{clearTimeout(timer);socket?.close();stopBrowser('SIGTERM');let killTimer;await Promise.race([closed,new Promise(resolve=>{killTimer=setTimeout(()=>{stopBrowser('SIGKILL');resolve();},3000);})]);clearTimeout(killTimer);fs.rmSync(temporary,{recursive:true,force:true,maxRetries:5,retryDelay:100});}
})();
