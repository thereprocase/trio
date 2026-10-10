'use strict';
const assert = require('assert');
const {load} = require('./dom-harness');
const Trio = load().hooks.Trio;
Trio.state.operator = {id:'me', name:'Operator'};
Trio.state.members = new Map([['agent',{id:'agent',name:'Worker',kind:'agent'}]]);
function card(mentions, extra={}) {
  return Trio.conversation.cardFor({id:99,member_id:'agent',content:'Plain message',created_at:'2026-01-01T12:00:00Z',mentions,...extra});
}
for (const [mentions, expected] of [[['me'],true],[['other'],false],[[],false],[['me','other'],true]]) {
  const c=card(mentions);
  assert.strictEqual(c.classList.contains('mentions-me'),expected);
  assert.strictEqual(c.querySelector('.message-targets'),null,'no @ metadata row');
}
assert.ok(!card(['me'],{member_id:'me'}).classList.contains('mentions-me'),'own unhighlighted');
assert.ok(!card(['me'],{retracted_at:'2026-01-01T12:01:00Z'}).classList.contains('mentions-me'),'retracted unhighlighted');
assert.ok(card([],{bangs:['agent']}).querySelector('.message-targets'),'urgent target remains');
console.log('PASS: personal mention highlight, no @ row, own/retracted unchanged');
