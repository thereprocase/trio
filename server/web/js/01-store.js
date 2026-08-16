(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const initial = {
    route: { name: 'home', params: {}, title: 'nth', subtitle: '' },
    session: { operator: null, token: '', channel: '', dmKey: '', dmName: '', dmMemberIds: [], readOnly: false, focused: true },
    dmAudit: false,
    workspace: { channels: [], dms: { your_dms: [], agent_dms: [] }, meta: {}, approvals: [], tasks: [], attention: 0 },
    conversation: { messages: new Map(), messageDomById: new Map(), members: new Map(), answers: new Map(), loading: false, error: '', dm: null, scroll: 0 },
    composer: { selectedTargets: new Set(), pendingAttachments: [], reply: null, draft: '', recording: false, disabled: false },
    agents: { list: [], selected: null, loading: false },
    tasks: { filter: 'open', list: [] },
    attention: { list: [] },
    preferences: { theme: 'light-1', lightTheme: 'light-1', darkTheme: 'dark-3', font: 'default', compact: false, messageNumbers: false, notifications: true, chime: false, dictation: true },
  };
  const state = Trio.state = Trio.state || initial;
  const subs = new Map();
  function get(path) {
    const parts = Array.isArray(path) ? path : path.split('.');
    let node = state;
    for (const p of parts) { if (node == null) return undefined; node = node[p]; }
    return node;
  }
  function set(path, value) {
    const parts = Array.isArray(path) ? path : path.split('.');
    let node = state;
    for (let i = 0; i < parts.length - 1; i++) { if (node[parts[i]] == null) node[parts[i]] = {}; node = node[parts[i]]; }
    const last = parts[parts.length - 1];
    const old = node[last];
    node[last] = value;
    notify(parts[0], { path: parts.join('.'), old, value });
    return value;
  }
  function update(path, fn) { return set(path, fn(get(path))); }
  function notify(slice, event) {
    const list = subs.get(slice);
    if (list) list.forEach(fn => { try { fn(getState(), event); } catch (e) { console.warn('store subscription error', e); } });
  }
  function getState() { return state; }
  function subscribe(slice, fn) {
    if (!subs.has(slice)) subs.set(slice, new Set());
    subs.get(slice).add(fn);
    return () => { subs.get(slice)?.delete(fn); };
  }
  function reset() {
    Object.keys(initial).forEach(k => { state[k] = initial[k]; });
    return state;
  }
  Trio.store = { state, getState, set, get, update, subscribe, reset };
})();
