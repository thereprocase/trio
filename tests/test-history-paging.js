'use strict';
const assert=require('assert'),{load}=require('./dom-harness');
const cx=load(),T=cx.hooks.Trio,s=T.state;
(async()=>{
 s.channel='sample';s.dmKey='';s.messages=new Map();s.messageDomById=new Map();s.channelHistory={channel:'sample',before_id:100,has_more:true};
 for (const id of [100,101]) s.messages.set(id,{id,channel:'sample',content:'Recent',member_id:'worker'});
 T.conversation.render();const list=cx.document.getElementById('messages');
 Object.defineProperty(list,'scrollHeight',{get:()=>s.messages.size*100,configurable:true});list.clientHeight=50;list.scrollTop=20;
 let finish,calls=0;T.api.get=()=>{calls++;return new Promise(r=>finish=r);};
 const p=T.conversation.loadOlder();T.conversation.loadOlder();assert.strictEqual(calls,1);
 finish({messages:[{id:99,channel:'sample',content:'Older',member_id:'worker'}],before_id:99,has_more:false});await p;
 assert.strictEqual(list.scrollTop,120,'prepend keeps viewport anchored');assert.ok(s.messages.has(99));assert.strictEqual(s.channelHistory.has_more,false);
 s.channelHistory={channel:'sample',before_id:99,has_more:true};const late=T.conversation.loadOlder();
 s.channel='elsewhere';s.channelHistory=null;s.messages=new Map();
 finish({messages:[{id:98,channel:'sample',content:'Do not insert'}],before_id:98,has_more:true});await late;
 assert.strictEqual(s.messages.size,0);
 console.log('PASS: one older request, exhaustion, late channel response rejected');
})().catch(e=>{console.error(e);process.exitCode=1;});
