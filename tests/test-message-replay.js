'use strict';
const assert=require('assert'),{load}=require('./dom-harness');
const cx=load(),T=cx.hooks.Trio,s=T.state;s.channel='sample';s.dmKey='';s.messages=new Map();s.messageDomById=new Map();
const m={id:1,channel:'sample',member_id:'worker',content:'Hello',created_at:'2026-01-01T00:00:00Z',attachments:[]};
s.channelLoading=true;T.conversation.render();
assert.ok(cx.document.getElementById('messages').textContent.includes('Loading conversation'));
T.conversation.upsert(m);const first=s.messageDomById.get(1);
assert.strictEqual(cx.document.getElementById('messages').querySelector('.conversation-empty'),null,'first message removes placeholder');
T.conversation.upsert({...m,attachments:[]});assert.strictEqual(s.messageDomById.get(1),first);
T.conversation.upsert({id:1,channel:'sample',content:'Hello'});assert.strictEqual(s.messageDomById.get(1),first);
T.conversation.upsert({...m,content:'Edited'});const edited=s.messageDomById.get(1);assert.notStrictEqual(edited,first);
T.conversation.upsert({...m,content:'Edited',retracted_at:'2026-01-02'});assert.notStrictEqual(s.messageDomById.get(1),edited);
console.log('PASS: duplicate and partial replays preserve nodes; edits and retractions update');

s.messages.clear();s.messageDomById.clear();s.channelLoading=true;T.conversation.render();
T.dispatchSSEEvent({type:'history_ready',channel:'elsewhere'});assert.strictEqual(s.channelLoading,true);
T.dispatchSSEEvent({type:'history_ready',channel:'sample'});assert.strictEqual(s.channelLoading,false);
assert.ok(cx.document.getElementById('messages').textContent.includes('No messages yet'));
