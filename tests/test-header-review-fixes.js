// Regression proofs for density review: native touch, identity rows, responsive
// keyboard navigation and exact desktop geometry against main cb03d20.
// Usage: node tests/test-header-review-fixes.js (skips if Chromium is absent).
// HEADER_EVIDENCE_DIR saves measurements outside the repo.
// HEADER_REVIEW_SOURCE/CHECK select a scratch mutation and its regression proof.
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

// Independent viewports use separate browser processes so thousands of native
// touch taps fit the regression runner's per-test timeout. No shared page state.
const worker = process.env.HEADER_REVIEW_WORKER;
if (!worker && !process.env.HEADER_REVIEW_CHECK) {
  const started = Date.now();
  (async () => {
    const jobs = ['desktop','320','360','390','412'];
    await Promise.all(jobs.map(width => new Promise((resolve,reject) => {
      const child = spawn(process.execPath,[__filename],{env:{...process.env,HEADER_REVIEW_WORKER:width},stdio:['ignore','pipe','pipe']});
      let output = '';
      child.stdout.on('data',data=>{output+=data;});child.stderr.on('data',data=>{output+=data;});
      child.once('error',reject);
      child.once('close',code=>{process.stdout.write(output);code===0?resolve():reject(new Error(width+'px review worker failed: '+code));});
    })));
    if(process.env.HEADER_EVIDENCE_DIR){
      const results=jobs.map(width=>JSON.parse(fs.readFileSync(path.join(process.env.HEADER_EVIDENCE_DIR,'review-fixes-'+width+'.json'),'utf8')));
      fs.writeFileSync(path.join(process.env.HEADER_EVIDENCE_DIR,'review-fixes.json'),JSON.stringify({passed:results.reduce((n,r)=>n+r.passed,0),records:results.flatMap(r=>r.records)},null,2)+'\n');
    }
    console.log('210 review cases passed in '+((Date.now()-started)/1000).toFixed(1)+'s');
  })().catch(error=>{console.error(error.stack);process.exitCode=1;});
} else {
const root = process.env.HEADER_REVIEW_SOURCE || path.join(__dirname, '..', 'server');
const check = process.env.HEADER_REVIEW_CHECK || 'all';
assert.ok(['all','touch','identity','keyboard','desktop'].includes(check), 'known review check');
const read = name => fs.readFileSync(path.join(root,name),'utf8');
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
const evidence = process.env.HEADER_EVIDENCE_DIR;
if (evidence) fs.mkdirSync(evidence, { recursive: true });
const html = read('web/index.html')
  .replace(/<link\b[^>]*>/g, '') // no font/manifest network requests
  .replace('<!--__TRIO_STYLES__-->', '<style>' + css + '</style>')
  .replace('<!--__TRIO_SCRIPTS__-->', '<script>' + scripts + '</script>');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'nth-headers-chromium-'));
const fixture = path.join(temporary, 'headers.html');
// Load the actual main markup, scripts and CSS; a cap or a screenshot cannot
// detect a shifted timestamp or a downstream timeline position.
const baseRead = name => {
  const result = spawnSync('git', ['show', 'cb03d20:server/' + name], {cwd:path.join(__dirname,'..'),encoding:'utf8'});
  assert.strictEqual(result.status,0,'main baseline is available: '+name);
  return result.stdout;
};
const baseHtml = baseRead('web/index.html').replace(/<link\b[^>]*>/g,'')
  .replace('<!--__TRIO_STYLES__-->', '<style>'+cssFiles.map(n=>baseRead('web/'+n)).join('\n')+'</style>')
  .replace('<!--__TRIO_SCRIPTS__-->', '<script>'+scripts.slice(0,scripts.indexOf('(() =>'))+
    ['js/06-core.js','js/09-time.js','js/10-markdown.js','js/11-conversation.js','js/20-workspace.js'].map(n=>baseRead('web/'+n)).join('\n')+'</script>');
const baselineFixture = path.join(temporary,'main.html'); fs.writeFileSync(baselineFixture,baseHtml);
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

  const setup = String.raw`(() => {
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
    const base={channel:'layout-demo',created_at:'2026-01-01T12:00:00Z'};
    // References retain expandable chips; @ mentions now use personal outlines.
    window.syntheticMessages=[
      {...base,id:1,member_id:'agent',content:'Ready.'},
      {...base,id:2,member_id:'agent',content:'Acknowledged.'},
      {...base,id:3,member_id:'long',content:'Long name sample.'},
      {...base,id:4,member_id:'operator',content:'OK.',refs:['long','peer','helper','reviewer','agent'],reply_to:3},
      {...base,id:5,member_id:'agent',content:'Recipients and reply.',bangs:['operator'],refs:['long','peer','helper','reviewer','agent'],reply_to:4},
      {...base,id:6,member_id:'peer',content:'[The entire last paragraph is a clickable link with several words to wrap near the timestamp and test the reachable link area on each line.](https://example.com)'},
      {...base,id:7,member_id:'operator',content:'[The entire last paragraph is a clickable link with several words to wrap near the timestamp and test the reachable link area on each line.](https://example.com)'},
      {...base,id:8,member_id:'agent',content:'[joined] Agent'},
      {...base,id:9,member_id:'peer',content:'Deleted message',retracted_at:'2026-01-01T12:01:00Z'},
      {...base,id:10,member_id:'agent',content:'Private task.',recipients:['operator'],refs:['operator'],task_id:123,confidence:'high'},
      {...base,id:11,member_id:'operator',content:'Edited text with a link [final linked words](https://example.com)',edited_at:'2026-01-01T12:01:00Z'}
    ];
    window.paintSynthetic=()=>{
      const list=document.getElementById('messages');
      list.replaceChildren(...syntheticMessages.map(m=>Trio.conversation.cardFor(m)));
      Trio.conversation.syncMessageGroups?.(list); list.scrollTop=0;
      for(const a of list.querySelectorAll('.bubble a'))a.addEventListener('click',e=>{e.preventDefault();window.linkClicks++;});
    };
    window.linkClicks=0;window.copies=[];
    Trio.ui={copyText:text=>{window.copies.push(text);return Promise.resolve();},setLive:()=>{},toast:()=>{}};
    window.paintSynthetic();
  })()`;
  const settle = "new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))";
  const snapshot = String.raw`(() => {
    const rect=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
    const list=document.getElementById('messages');list.scrollTop=0;
    return [...list.querySelectorAll('.message')].map(c=>({id:c.dataset.messageId,
      card:rect(c),head:c.querySelector('.message-head')?rect(c.querySelector('.message-head')):null,
      bubble:c.querySelector('.bubble')?rect(c.querySelector('.bubble')):null,
      time:c.querySelector('.header-time')?rect(c.querySelector('.header-time')):(c.querySelector('time')?rect(c.querySelector('time')):null),
      tabs:[...c.querySelectorAll('a,button,[tabindex]')].filter(el=>el.getClientRects().length&&el.tabIndex>=0)
        .map(el=>({tag:el.tagName,cls:el.className.replace(' header-time','')}))
    }));
  })()`;
  const records=[];let passed=0;
  // Main and worktree run in the same browser, with the same fonts and pointer.
  await call('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false},sessionId);
  await call('Emulation.setTouchEmulationEnabled',{enabled:false},sessionId);
  const desktop={};
  for(const [label,file] of (worker && worker!=='desktop'?[]:[['main',baselineFixture],['worktree',fixture]])){
    await call('Page.navigate',{url:pathToFileURL(file).href},sessionId);
    while(!await evaluate("document.readyState==='complete'&&!!window.Trio?.conversation"))await new Promise(r=>setTimeout(r,20));
    await evaluate(setup);
    for(const theme of themes)for(const numbers of [false,true]){
      await evaluate(`document.documentElement.dataset.theme=${JSON.stringify(theme)};document.body.classList.toggle('message-numbers',${numbers});window.paintSynthetic();${settle}`);
      const result=await evaluate(snapshot),key=theme+'/'+numbers;
      if(label==='main')desktop[key]=result;
      else {
        if(check==='all'||check==='desktop')assert.deepStrictEqual(result,desktop[key],'1280px exact main positions and tab order: '+key);
        records.push({width:1280,theme,numbers,rows:result});passed++;
      }
    }
  }
  if(worker && worker!=='desktop')await evaluate(setup);
  const touch = async (x,y) => {
    await call('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,radiusX:1,radiusY:1}]},sessionId);
    await call('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]},sessionId);
  };
  for(const width of (check==='desktop'||check==='keyboard'||worker==='desktop'?[]:[320,360,390,412].filter(w=>!worker||String(w)===worker))){
    await call('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:1},sessionId);
    await call('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:true},sessionId);
    for(const theme of themes)for(const numbers of [false,true]){
      await evaluate(`document.documentElement.dataset.theme=${JSON.stringify(theme)};document.body.classList.toggle('message-numbers',${numbers});window.paintSynthetic();${settle}`);
      const identity=await evaluate(`(() => {
        const c=document.querySelector('[data-message-id="3"]'),a=c.querySelector('.header-avatar'),n=c.querySelector('strong');
        const ar=a.getBoundingClientRect(),nr=n.getBoundingClientRect();
        const chips=[...document.querySelectorAll('[data-message-id="4"] .target-chip')].filter(e=>e.getClientRects().length);
        return {avatarTop:ar.top,nameTop:nr.top,nameHeight:nr.height,nameWidth:nr.width,
          title:n.title,label:n.getAttribute('aria-label'),name:n.textContent,
          ellipsis:getComputedStyle(n).textOverflow,nowrap:getComputedStyle(n).whiteSpace,
          chipHeights:chips.map(e=>e.getBoundingClientRect().height),
          overflow:document.getElementById('messages').scrollWidth-document.getElementById('messages').clientWidth};
      })()`);
      assert.strictEqual(identity.avatarTop,identity.nameTop,'avatar/name share row '+width+'/'+theme);
      assert.ok(identity.nameWidth>0&&identity.nameHeight===22,'name occupies one line');
      assert.strictEqual(identity.title,identity.name);assert.strictEqual(identity.label,identity.name);
      assert.strictEqual(identity.ellipsis,'ellipsis');assert.strictEqual(identity.nowrap,'nowrap');
      assert.ok(identity.chipHeights.every(h=>h<=22),'collapsed recipients stay on one line');
      assert.strictEqual(identity.overflow,0,'phone has no horizontal overflow');
      // Tap the right edge of both final and penultimate link lines. This is
      // where a 44px clock overlay previously stole native touch events.
      let links=0,toggles=0;
      for(const id of (check==='identity'?[]:[6,7,11])){
        const points=await evaluate(`(() => {
          const a=document.querySelector('[data-message-id="${id}"] .bubble a');a.scrollIntoView({block:'center'});
          const clock=a.closest('.bubble').querySelector('.bubble-time'),b=clock.getBoundingClientRect(),p=getComputedStyle(clock,'::after');
          const hit={left:b.left+(parseFloat(p.left)||0),right:b.right-(parseFloat(p.right)||0),top:b.top+(parseFloat(p.top)||0),bottom:b.bottom-(parseFloat(p.bottom)||0)};
          return [...a.getClientRects()].slice(-2).flatMap(r=>{
            const points=[{x:r.right-2,y:(r.top+r.bottom)/2}];
            const l=Math.max(r.left,hit.left),rr=Math.min(r.right,hit.right),t=Math.max(r.top,hit.top),bb=Math.min(r.bottom,hit.bottom);
            // Explicitly tap any clock/link intersection. In the fixed layout
            // it is empty; restoring the overlay must exercise the stolen tap.
            if(rr>l&&bb>t)points.push({x:(l+rr)/2,y:(t+bb)/2});
            return points;
          });
        })()`);
        for(const p of points){
          const before=await evaluate('({links:window.linkClicks,copies:window.copies.length})');
          await touch(p.x,p.y);
          const after=await evaluate('({links:window.linkClicks,copies:window.copies.length})');
          assert.strictEqual(after.links,before.links+1,'native last-line link tap '+width+'/'+theme+'/'+id);
          assert.strictEqual(after.copies,before.copies,'link tap must not copy time');links++;
        }
      }
      for(const id of (check==='identity'?[]:[4,5])){
        // A 3x3 grid covers corners, edges and centre of the complete +N rect,
        // both collapsed and expanded (each native tap toggles it).
        for(const fy of [.02,.5,.98])for(const fx of [.02,.5,.98]){
          const p=await evaluate(`(() => {
            const e=document.querySelector('[data-message-id="${id}"] .targets-toggle');e.scrollIntoView({block:'center'});
            const r=e.getBoundingClientRect(),s=getComputedStyle(e,'::after');
            const l=r.left+(parseFloat(s.left)||0),t=r.top+(parseFloat(s.top)||0),rr=r.right-(parseFloat(s.right)||0),b=r.bottom-(parseFloat(s.bottom)||0);
            return {x:l+(rr-l)*${fx},y:t+(b-t)*${fy},width:rr-l,height:b-t,expanded:e.getAttribute('aria-expanded'),copies:window.copies.length};
          })()`);
          assert.ok(p.width>=44&&p.height>=44,'full +N target is 44px');
          await touch(p.x,p.y);
          const after=await evaluate(`({expanded:document.querySelector('[data-message-id="${id}"] .targets-toggle').getAttribute('aria-expanded'),copies:window.copies.length})`);
          assert.notStrictEqual(after.expanded,p.expanded,'native full +N hit rect '+width+'/'+theme+'/'+id+'/'+fx+'/'+fy+' '+JSON.stringify(p));
          assert.strictEqual(after.copies,p.copies,'recipient tap must not copy time');toggles++;
        }
      }
      records.push({width,theme,numbers,identity,links,toggles});passed++;
    }
    console.log('PASS: '+width+'px native touch and identity (42 theme/number cases)');
  }
  // Keep the very same cards while crossing the breakpoint in both directions.
  if(check==='keyboard'||worker==='desktop'){
    await call('Emulation.setDeviceMetricsOverride',{width:390,height:900,deviceScaleFactor:1,mobile:true},sessionId);
    await evaluate(settle);await evaluate('window.paintSynthetic()');
  }
  const original=await evaluate("window.keptBubble=document.querySelector('[data-message-id=\"1\"] .bubble');true");
  assert.ok(original);
  for(const width of [1280,390,1280]){
    await call('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width<=640},sessionId);
    await evaluate(settle);
    const keyboard=await evaluate(`(() => {
      const b=document.querySelector('[data-message-id="1"] .bubble');
      const event=new KeyboardEvent('keydown',{key:'F10',shiftKey:true,bubbles:true,cancelable:true});b.dispatchEvent(event);
      return {same:b===window.keptBubble,tab:b.getAttribute('tabindex'),hint:b.getAttribute('aria-keyshortcuts'),
        prevented:event.defaultPrevented,copyFocused:document.activeElement.classList.contains('menu-copy')};
    })()`);
    assert.ok(keyboard.same,'resize updates existing cards');
    assert.strictEqual(keyboard.tab,width<=640?'0':null,'responsive bubble tab stop');
    assert.strictEqual(keyboard.hint,width<=640?'Shift+F10':null,'responsive shortcut hint');
    assert.strictEqual(keyboard.prevented,width<=640,'desktop shortcut not swallowed');
    if(width<=640)assert.ok(keyboard.copyFocused,'phone shortcut focuses Copy');
  }
  if(evidence)fs.writeFileSync(path.join(evidence,'review-fixes'+(worker?'-'+worker:'')+'.json'),JSON.stringify({passed,records},null,2)+'\n');
  console.log(passed+' review cases passed; existing-card breakpoint keyboard checks passed');
}
(async()=>{
  try{await Promise.race([measure(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Chromium review deadline exceeded')),180000);})]);}
  catch(error){console.error(error.stack);process.exitCode=1;}
  finally{clearTimeout(timer);socket?.close();stopBrowser('SIGTERM');let killTimer;await Promise.race([closed,new Promise(resolve=>{killTimer=setTimeout(()=>{stopBrowser('SIGKILL');resolve();},3000);})]);clearTimeout(killTimer);fs.rmSync(temporary,{recursive:true,force:true,maxRetries:5,retryDelay:100});}
})();

}
