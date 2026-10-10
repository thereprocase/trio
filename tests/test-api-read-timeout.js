'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
let expire, cleared=0, calls=0, lastSignal;
const context={window:{},AbortController,setTimeout:fn=>(expire=fn,1),clearTimeout:()=>cleared++};context.window=context;
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../server/web/js/02-api.js'),'utf8'),context);
const api=context.Trio.api;
const pending=signal=>new Promise((_,reject)=>{lastSignal=signal;if(signal.aborted)reject(new Error('aborted'));else signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});});
(async()=>{
 context.fetch=async(_,init)=>{calls++;return pending(init.signal);};
 let p=api.get('/api/channels');expire();await assert.rejects(p,e=>e.status===408);assert.strictEqual(calls,1);assert.ok(lastSignal.aborted);
 context.fetch=async(_,init)=>({ok:true,text:()=>pending(init.signal)});
 p=api.get('/api/channels');await Promise.resolve();expire();await assert.rejects(p,e=>e.status===408,'body stalls bounded');
 const c=new AbortController();p=api.get('/api/channels',true,{signal:c.signal});c.abort();await assert.rejects(p,e=>e.message==='aborted');
 context.fetch=async()=>({ok:true,text:async()=>'{"value":1}'});assert.strictEqual((await api.get('/api/channels')).value,1);assert.ok(cleared>=4);
 let postSignal;context.fetch=async(_,init)=>{postSignal=init.signal;return {ok:true,text:async()=>'{"ok":true}'};};await api.post('/api/send',{content:'hello'});assert.strictEqual(postSignal,undefined);
 console.log('PASS: reads bounded through body, caller abort preserved, timers cleared, writes unchanged');
})().catch(e=>{console.error(e);process.exitCode=1;});
