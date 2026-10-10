'use strict';
const assert=require('assert'),{load}=require('./dom-harness');
const T=load().hooks.Trio,s=T.state;
(async()=>{
 s.view='conversation';s.workspaceLoading=false;T.agents.refresh=async()=>{};
 const paths=[];let resolveDetail;
 T.api.get=async path=>{paths.push(path);if(path==='/api/mentions?summary=1')return {count:7,unread_count:3};if(path==='/api/mentions')return new Promise(r=>resolveDetail=r);return {channels:[],tasks:[],approvals:[],questions:[]};};
 await T.workspace.refresh({messageOnly:true});
 assert.strictEqual(T.workspace.selectors.unreadMentions(),3);assert.ok(!paths.includes('/api/mentions'));
 T.workspace.showView('messages');T.workspace.showView('messages');
 assert.strictEqual(paths.filter(p=>p==='/api/mentions').length,1);
 resolveDetail({unread_count:1,mentions:[{id:1,read:false,content:'Detail',member_name:'Worker',channel:'sample'}]});
 await new Promise(r=>setImmediate(r));
 assert.strictEqual(s.mentionsSummaryOnly,false);assert.strictEqual(s.mentions[0].content,'Detail');
 assert.strictEqual(T.workspace.selectors.unreadMentions(),1);
 console.log('PASS: badge summary without bodies, one detail fetch on inbox open');
})().catch(e=>{console.error(e);process.exitCode=1;});
