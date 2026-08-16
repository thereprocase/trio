(() => {
  'use strict';
  const root = window.Trio = window.Trio || {};
  // Keep the core usable in the intentionally tiny Node DOM harness too;
  // browsers have URLSearchParams, but the runtime only needs this one value.
  const qs = (globalThis.location?.search) || '';
  const parseParam = name => { const m = qs.match(new RegExp('[?&]' + name + '=([^&]+)')); return m ? decodeURIComponent(m[1].replace(/\+/g, ' ')) : ''; };
  const initialChannel = parseParam('channel');
  const initialDm = parseParam('dm');
  function validDmKey(key) { return typeof key === 'string' && key.length > 0 && key.length <= 512 && /^[A-Za-z0-9_,:-]+$/.test(key); }
  const conversationKind = initialDm ? (validDmKey(initialDm) ? 'dm' : 'unknown') : (initialChannel ? 'channel' : 'unknown');
  const conversationKey = initialDm ? (validDmKey(initialDm) ? initialDm : '') : initialChannel;
  root.state = root.state || { channel: '', messages: new Map(), meta: null, members: new Map() };
  root.state.channel = initialChannel;
  root.state.conversation = { kind: conversationKind, key: conversationKey };
  root.events = root.events || new EventTarget();
  root.api = root.api || {
    url(path, channelScoped = true) {
      if (!channelScoped || !root.state.channel) return path;
      return path + (path.includes('?') ? '&' : '?') + 'channel=' + encodeURIComponent(root.state.channel);
    },
    async get(path, channelScoped = true) { const response = await fetch(this.url(path, channelScoped)); if (!response.ok) throw new Error(`${response.status} ${path}`); return response.json(); },
    async post(path, body, channelScoped = true) { const response = await fetch(this.url(path, channelScoped), { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) }); if (!response.ok) throw new Error(`${response.status} ${path}`); return response.json(); },
  };
  root.actions = root.actions || {};
  // ── Avatar tone assignment ──────────────────────────────────────────
  // Two call sites (11-conversation.js, 20-workspace.js) used to each keep
  // their own copy of a pure hash — tones[sum(charCodes) % 5] — with no
  // collision avoidance at all, so two members could land on the identical
  // tone by pure chance (reported: two agents both plum). Mirrors
  // nth_constants.py's animal_for_channel: hash picks a PREFERRED slot,
  // linear-probing to the next free one only on an actual collision, over
  // ids/labels sorted for determinism — every tone in the pool gets used
  // before any repeats, and a given id keeps the same tone across renders
  // as long as the population (who else is visible) hasn't changed.
  const AVATAR_TONES = ['coral', 'indigo', 'eucalyptus', 'amber', 'plum'];
  function hashLabel(label) {
    let h = 0;
    for (const ch of String(label || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return h;
  }
  root.avatarTones = function avatarTones(labels) {
    const pool = AVATAR_TONES.length;
    const taken = new Set();
    const result = new Map();
    for (const label of [...new Set((labels || []).filter(Boolean))].sort()) {
      let pick = hashLabel(label) % pool;
      for (let i = 0; i < pool && taken.has(pick); i++) pick = (pick + 1) % pool;
      taken.add(pick);
      result.set(label, AVATAR_TONES[pick]);
    }
    return result;
  };
  // Convenience single-lookup form for call sites that just want "the tone
  // for this one label" — builds the population from everyone currently
  // known to the client (this channel's live roster + every managed agent +
  // the operator) so a message/avatar rendered ANYWHERE in the app resolves
  // the same tone for the same identity. Population sizes here are small
  // (roster/agent counts, not message volume), so recomputing per call is
  // cheap — no cache to invalidate when the roster changes.
  root.avatarTone = function avatarTone(label) {
    const ids = new Set();
    const members = root.state?.members;
    if (members instanceof Map) for (const id of members.keys()) ids.add(id);
    // state.agents is a flat array once 30-agents.js's refresh() has run at
    // least once, but 01-store.js's initial shape is {list, selected,
    // loading} — guard against both rather than assume the post-refresh
    // shape everywhere avatarTone might get called from.
    const agents = Array.isArray(root.state?.agents) ? root.state.agents
      : Array.isArray(root.state?.agents?.list) ? root.state.agents.list : [];
    for (const agent of agents) if (agent?.id) ids.add(agent.id);
    if (root.state?.operator?.id) ids.add(root.state.operator.id);
    ids.add(label);
    return root.avatarTones([...ids]).get(label);
  };
  root.boot = async function boot(mountFeatures) {
    // A failed/slow /api/meta must NOT abort boot — otherwise mountFeatures()
    // below never runs and the whole shell (theme, router, views) is skipped,
    // leaving the hardcoded default theme + empty non-channel pages until a
    // later reload. Continue with defaults so the shell always mounts.
    let meta = {};
    try { meta = (await root.api.get('/api/meta')) || {}; }
    catch (e) { console.error('boot: /api/meta failed; continuing with defaults', e); }
    root.state.meta = meta;
    root.state.operator = meta.operator || null;
    root.state.channel = root.state.channel || meta.default_channel || meta.channel || '';
    if (root.store) { root.store.set('session.operator', root.state.operator); root.store.set('session.channel', root.state.channel); }
    // No channel selected on load; stay on the workspace home view instead of
    // forcing the first channel open.
    document.getElementById('h-channel').textContent = root.state.channel ? `#${root.state.channel}` : 'Atrium';
    document.getElementById('h-meta').textContent = root.state.channel ? 'Live agent workspace' : 'No channel selected';
    mountFeatures?.();
    if (root.startEvents) root.startEvents(root.state.channel);
    // Cross-channel notifications (chime/desktop popup for a channel you're
    // NOT currently viewing) need the workspace-wide SSE stream — but
    // /api/workspace/events is operator-only server-side (403 for anyone
    // else), so only the authenticated operator (loopback/tailscale — the
    // same is_all_seeing check the server itself applies) opens it. A
    // guest/pending viewer opening it anyway would just retry against a
    // permanent 403 forever.
    const isOperator = root.state.operator?.source === 'loopback' || root.state.operator?.source === 'tailscale';
    if (isOperator && root.startWorkspaceEvents) root.startWorkspaceEvents();
    root.events.dispatchEvent(new CustomEvent('boot', {detail: meta})); return true;
  };
})();
