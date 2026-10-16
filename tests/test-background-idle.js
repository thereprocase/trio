'use strict';
// Drive shipped lifecycle modules, not a copy of their state machine.
const assert = require('assert');
const {load} = require('./dom-harness');
const tick = () => new Promise(resolve => setImmediate(resolve));
const intervals = new Map(); let seq=0;
const cx = load({setup:w=>{
  w.setInterval=(fn,ms)=>{const id=++seq;intervals.set(id,{fn,ms});return id;};
  w.clearInterval=id=>intervals.delete(id);
}});
const T=cx.hooks.Trio,d=cx.document,w=cx.window;
const show=hidden=>{d.hidden=hidden;d.visibilityState=hidden?'hidden':'visible';d.dispatchEvent(new Event('visibilitychange'));};
const live=()=>w.EventSource.instances.filter(s=>!s.closed);
function fixture(){d.getElementById('channel-pins-button').innerHTML='<span class="channel-pin-count"></span>';}
(async()=>{
  T.state.channel='example'; T.state.dmKey='';
  T.startWorkspaceEvents();T.startEvents('example');
  let sources=live();assert.strictEqual(sources.length,2);
  sources[0].fireOpen();sources[0].fireMessage(JSON.stringify({type:'message',id:42,channel:'example',content:'synthetic'}));
  sources[1].fireOpen();sources[1].fireHeartbeat();
  show(true);assert.strictEqual(live().length,0,'hidden closes both streams');
  assert.ok(![...intervals.values()].some(t=>t.ms===10000),'hidden has no stale watchdog');
  const count=w.EventSource.instances.length;
  // A closed source firing a late error must not restart anything.
  sources.forEach(s=>s.fireError());w.dispatchEvent(new Event('online'));
  T.state.channel='other-example';T.startEvents('other-example');T.startWorkspaceEvents();
  assert.strictEqual(w.EventSource.instances.length,count,'no hidden route/online reconnect');
  show(false);assert.strictEqual(live().length,2,'one foreground stream per feed');
  assert.ok(live()[0].url.includes('after_id=42'),'workspace catch-up cursor retained');
  assert.ok(live()[1].url.includes('other-example'),'latest hidden route retained');
  assert.notStrictEqual(T.freshnessSnapshot().text,'live','open alone never claims freshness');
  live().forEach(s=>{s.fireOpen();s.fireHeartbeat();});assert.strictEqual(T.freshnessSnapshot().text,'live');
  show(true);T.stopEvents();T.stopWorkspaceEvents();show(false);
  assert.strictEqual(live().length,0,'explicit stop while hidden is authoritative');
  T.startEvents('example');w.dispatchEvent(new Event('pagehide'));
  assert.strictEqual(live().length,0,'pagehide closes without needing hidden flag');
  w.dispatchEvent(Object.assign(new Event('pageshow'),{persisted:true}));
  assert.strictEqual(live().length,1,'bfcache return reopens exactly once');T.stopEvents();

  // Background safe reads abort through body consumption; writes remain owned.
  let writeResolve,readSignal,fetches=0;
  w.fetch=(_url,init)=>{
    fetches++;
    if(init.method==='POST')return new Promise(resolve=>writeResolve=()=>resolve({ok:true,status:200,text:async()=>'{"ok":true}'}));
    readSignal=init.signal;
    return Promise.resolve({ok:true,status:200,text:()=>new Promise((_,reject)=>init.signal.addEventListener('abort',()=>reject(Object.assign(new Error('cancelled'),{name:'AbortError'})),{once:true}))});
  };
  const read=T.api.get('/api/channels',true,{background:true}).then(()=>false,e=>e.name);
  const write=T.api.post('/api/messages',{text:'synthetic'});await tick();show(true);
  assert.strictEqual(readSignal.aborted,true);assert.strictEqual(await read,'AbortError');
  const before=fetches;await assert.rejects(T.api.get('/api/channels',true,{background:true}),e=>e.name==='AbortError');
  assert.strictEqual(fetches,before,'hidden automatic read never starts');writeResolve();await write;
  show(false);
  // Workspace periodic timers stop, rather than waking and doing a no-op.
  w.fetch=async()=>({ok:true,status:200,text:async()=>'{"ok":true}',json:async()=>({ok:true,build:'synthetic'})});
  fixture();T.state.operator={source:'tailscale'};T.state.workspaceLoading=false;
  T.api.get=async()=>({channels:[],dms:[],tasks:[],approvals:[],questions:[],mentions:[],agents:[]});T.agents.refresh=async()=>{};
  T.workspace.mount();assert.ok([...intervals.values()].some(t=>t.ms===5000));
  T.diagnostics.mount();T.appRefresh.mount({onUnmount:fn=>w.cleanupRefresh=fn});
  show(true);assert.strictEqual(intervals.size,0,'no hidden workspace, version, diagnostic or watchdog intervals');
  show(false);assert.ok([...intervals.values()].some(t=>t.ms===5000));
  T.workspace.unmount();T.diagnostics.unmount();w.cleanupRefresh();T.stopEvents();T.stopWorkspaceEvents();
  assert.strictEqual(intervals.size,0,'unmount clears all timers');
  console.log('PASS: background sockets/timers/reads pause, foreground cursor/route recovery, writes and explicit stops preserved');
})().catch(e=>{console.error(e);process.exitCode=1;});
