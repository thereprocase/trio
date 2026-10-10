'use strict';
const assert = require('assert');
const {load} = require('./dom-harness');
(async () => {
  const cx = load(), {Trio} = cx.hooks, doc = cx.document, state = Trio.state;
  const originalGet = doc.getElementById.bind(doc);
  const drawerBody = originalGet('channel-drawer-body');
  doc.getElementById = id => drawerBody.querySelector('#' + id) || originalGet(id);
  state.channel = 'controls'; state.channels = [{code:'controls'}];
  state.dmKey = ''; state.dmThread = null; state.dms = {your_dms:[]};
  state.loaded = {...state.loaded, meta:true, agents:true}; state.sliceErrors = {};
  state.operator = {id:'_op_owner', name:'Owner', source:'tailscale'};
  state.members = new Map([
    ['worker', {id:'worker',name:'Worker',kind:'agent',status:'active'}],
    ['_op_guest', {id:'_op_guest',name:'Guest',kind:'human',status:'active'}]
  ]);
  state.agents = [];
  const calls = [];
  Trio.api.get = async url => {calls.push(url); return {agents:[],sessions:[]};};
  Trio.workspace.renderFacePile();
  const pile = doc.getElementById('face-pile');
  const face = pile.querySelector('.agent-control-link');
  assert.ok(face, 'owner can manage an agent from the buddy strip');
  assert.strictEqual(pile.querySelectorAll('.agent-control-link').length, 1, 'humans remain plain');
  let prevented = false, stopped = false;
  face._listeners.click[0]({preventDefault(){prevented=true;},stopPropagation(){stopped=true;}});
  await Promise.resolve(); await Promise.resolve();
  assert.ok(prevented && stopped, 'agent tap does not bubble into open-members');
  assert.ok(calls.includes('/api/terminals'), 'buddy tap resolves explicit session bindings');
  Trio.workspace.showDetails();
  let list = doc.getElementById('channel-drawer-members');
  assert.strictEqual(list.querySelectorAll('.agent-control-link').length, 2, 'drawer avatar and name both manage the agent');
  const name = list.querySelector('.channel-member-name.agent-control-link');
  assert.strictEqual(name.getAttribute('role'), 'button');
  assert.strictEqual(name.tabIndex, 0);
  const priorCalls = calls.length;
  name._listeners.keydown[0]({key:'Enter',preventDefault(){},stopPropagation(){}});
  await Promise.resolve(); await Promise.resolve();
  assert.ok(calls.length > priorCalls, 'drawer name supports keyboard activation');
  Trio.workspace.refreshDrawerMembers();
  list = doc.getElementById('channel-drawer-members');
  assert.strictEqual(list.querySelectorAll('.agent-control-link').length, 2, 'controls survive roster repaint');
  state.operator.source = 'guest';
  Trio.workspace.renderFacePile(); Trio.workspace.refreshDrawerMembers();
  assert.strictEqual(pile.querySelectorAll('.agent-control-link').length, 0, 'guest buddies have no owner controls');
  assert.strictEqual(list.querySelectorAll('.agent-control-link').length, 0, 'guest drawer has no owner controls');
  console.log('PASS: owner buddy/drawer controls, keyboard, bubbling, repaint and guest isolation');
})().catch(error => {console.error(error);process.exitCode = 1;});
