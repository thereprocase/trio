/* Actual layout/reopen regression, using an existing optional Playwright. */
'use strict';
const fs = require('fs'), http = require('http'), path = require('path'), assert = require('assert');
let chromium;
try { ({chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright')); }
catch { console.log('SKIP: set PLAYWRIGHT_MODULE to an existing Playwright installation'); process.exit(0); }
const root = path.join(__dirname, '..', 'server');
const scripts = ['06-core', '09-time', '10-markdown', '11-conversation'].map(n => fs.readFileSync(path.join(root, 'web/js/' + n + '.js'), 'utf8')).join('\n');
const fixture = `<!doctype html><style>
#messages{height:400px;overflow:auto;overflow-anchor:none;width:360px}.message.msg{height:110px;box-sizing:border-box;margin:8px 0;overflow:hidden}.day-separator,.unread-divider{height:25px}.hidden{display:none}
</style><div id="messages" tabindex="0"></div><button id="jump-latest">Latest</button><script>
window.Trio={state:{operator:{id:new URLSearchParams(location.search).get('user')||'reader'},channel:'synthetic',view:'conversation',channelLoading:true,readOnly:false},api:{},events:new EventTarget(),actions:{},avatarTone:()=> 'eucalyptus'};
window.calls=[];window.notices=[];
Trio.api.post=async(url,body)=>{calls.push({url,body});return {ok:true}};
Trio.api.get=async url=>{calls.push({url});const before=Number(new URL(url,location.href).searchParams.get('before'));await new Promise(r=>setTimeout(r,new URLSearchParams(location.search).has('slow')?350:20));return {messages:rows(Math.max(1,before-50),before-1),before_id:Math.max(1,before-50),has_more:before>51}};
Trio.ui={toast:t=>notices.push(t)};
${scripts}
Trio.state.channel="synthetic";
if(new URLSearchParams(location.search).has("dm")){Trio.state.dmKey="peer";Trio.state.dmMemberIds=["peer"];Trio.state.channelLoading=false;}
function rows(lo,hi){return Array.from({length:hi-lo+1},(_,i)=>({id:lo+i,channel:'synthetic',member_id:'peer',content:'Message '+(lo+i),created_at:'2026-01-01T00:00:00Z',recipients:[]}))}
if(new URLSearchParams(location.search).has('slow')) localStorage.setItem(positionKey(),JSON.stringify({id:10,offset:-15,bottom:false}));
if(new URLSearchParams(location.search).has('missing')) localStorage.setItem(positionKey(),JSON.stringify({id:9999,offset:-5,bottom:false}));
Trio.conversation.seedWatermark(30,new URLSearchParams(location.search).has('unknown')?undefined:3);Trio.conversation.init();
setTimeout(()=>{const q=new URLSearchParams(location.search),lo=q.has('partial')?101:1,hi=Number(q.get('hi'))||120;
 Trio.state.messages=new Map(rows(lo,hi).map(m=>[m.id,m]));Trio.state.channelHistory={channel:'synthetic',before_id:lo,has_more:lo>1};Trio.conversation.render();
 Trio.state.channelLoading=false;Trio.events.dispatchEvent(new CustomEvent('history_ready',{detail:{channel:'synthetic'}}));window.loaded=true;
},80);
function positionKey(){return 'trio:reading-position:v1:'+JSON.stringify([Trio.state.operator.id,Trio.state.dmKey?'dm:'+Trio.state.dmKey:'synthetic'])}
function saved(){return JSON.parse(localStorage.getItem(positionKey())||'null')}
function geometry(p=saved()){const list=document.getElementById('messages'),card=Trio.state.messageDomById.get(p.id);return card.getBoundingClientRect().top-list.getBoundingClientRect().top}
function move(id,offset=-23){const list=document.getElementById('messages');list.dispatchEvent(new WheelEvent('wheel'));list.scrollTop+=Trio.state.messageDomById.get(id).getBoundingClientRect().top-list.getBoundingClientRect().top-offset;Trio.conversation.savePosition();return saved()}
</script>`;
const server = http.createServer((req,res) => { res.writeHead(200, {'Content-Type':'text/html'});res.end(fixture); });
(async () => {
 await new Promise(r=>server.listen(0,'127.0.0.1',r));let browser;
 try {
  browser=await chromium.launch({executablePath:process.env.CHROMIUM_BIN||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:390,height:844}}),base='http://127.0.0.1:'+server.address().port;let page=await context.newPage();
  const ready=async()=>{await page.waitForFunction(()=>window.loaded);await page.waitForTimeout(700)};
  await page.goto(base);await ready();
  const left=await page.evaluate(()=>move(70));assert(left && !left.bottom);
  await page.reload();await ready();
  assert.deepEqual(await page.evaluate(()=>saved()),left);assert(Math.abs(await page.evaluate(()=>geometry())-left.offset)<1,'reload restores anchor, not old unread gap');
  await page.close();page=await context.newPage();await page.goto(base);await ready();assert(Math.abs(await page.evaluate(()=>geometry())-left.offset)<1,'close/reopen retains position');
  await page.evaluate(()=>Trio.state.messageDomById.get(20).style.height='290px');await page.waitForTimeout(150);
  assert(Math.abs(await page.evaluate(()=>geometry())-left.offset)<1,'late layout preserves anchor');
  const hidden=await page.evaluate(()=>{const list=document.getElementById('messages');list.dispatchEvent(new WheelEvent('wheel'));list.scrollTop+=Trio.state.messageDomById.get(85).getBoundingClientRect().top-list.getBoundingClientRect().top+17;window.dispatchEvent(new Event('pagehide'));return saved()});
  await page.reload();await ready();assert(Math.abs(await page.evaluate(()=>geometry())-hidden.offset)<1,'page exit synchronously captures final position');
  await page.evaluate(()=>document.getElementById('jump-latest').click());await page.waitForTimeout(100);assert((await page.evaluate(()=>saved())).bottom);
  await page.goto(base+'/?hi=128');await ready();assert(await page.evaluate(()=>{const x=document.getElementById('messages');return x.scrollHeight-x.scrollTop-x.clientHeight<2}),'bottom reopens at newest');
  await page.evaluate(()=>move(40,-19));
  await page.goto(base+'/?partial=1');await ready();assert(Math.abs(await page.evaluate(()=>geometry())+19)<1,'anchor outside prime loads older history');
  assert((await page.evaluate(()=>calls.filter(c=>c.url.startsWith('/api/history')).length))>=2);
  await page.goto(base+'/?missing=1');await ready();assert.equal((await page.evaluate(()=>saved())).id,9999,'unavailable anchor is not overwritten');assert((await page.evaluate(()=>notices)).length);
  await page.goto(base+'/?partial=1&slow=1');await page.waitForFunction(()=>window.loaded);await page.waitForTimeout(300);
  await page.evaluate(()=>{Trio.state.channel='elsewhere';Trio.state.messages=new Map(rows(1,4).map(m=>[m.id,{...m,channel:'elsewhere'}]));Trio.state.channelHistory=null;Trio.conversation.seedWatermark(4,null);Trio.conversation.render()});await page.waitForTimeout(600);
  assert.equal(await page.evaluate(()=>Trio.state.channel),'elsewhere');assert.equal(await page.evaluate(()=>Trio.state.messages.size),4,'late history fetch does not pollute next conversation');
  await page.goto(base+'/?dm=1');await ready();const dmLeft=await page.evaluate(()=>move(60,-12));await page.reload();await ready();assert(Math.abs(await page.evaluate(()=>geometry())-dmLeft.offset)<1,'DM resumes its own anchor');
  await page.goto(base+'/?unknown=1');await ready();assert.equal(await page.evaluate(()=>calls.filter(c=>c.url==='/api/messages/mark-read').length),0,'unknown server read state does not mark history read');
  await page.goto(base+'/?user=other');await ready();assert.notEqual((await page.evaluate(()=>saved())).id,9999,'positions scoped by authenticated user');
  await page.addInitScript(()=>{Storage.prototype.getItem=()=>{throw Error('blocked')};Storage.prototype.setItem=()=>{throw Error('blocked')}});
  await page.goto(base);await ready();assert(Math.abs(await page.evaluate(()=>geometry({id:3})) )<1,'blocked storage uses server unread fallback');
  console.log('PASS: mobile close/reopen/reload, navigation race, unread-gap separation, late layout, pagehide, bottom/new messages, paged anchors, missing anchors, DM/user scope, unavailable read-state and blocked storage');
 } finally {await browser?.close();await new Promise(r=>server.close(r))}
})().catch(e=>{console.error(e);process.exitCode=1});
