(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const events = Trio.events = Trio.events || new EventTarget();
  let stream;
  let lastId = null;
  let state = 'connecting';
  function setConnection(text, failed = false) {
    const el = document.getElementById('h-conn');
    const cls = failed ? (text === 'offline' ? 'offline' : 'reconnect') : 'live';
    if (el) {
      el.className = 'conn ' + cls;
      const label = el.querySelector('.conn-label');
      if (label) label.textContent = text;
      else el.textContent = text;
    }
    if (Trio.store) Trio.store.set('connection', { text, failed, state });
    if (failed) Trio.ui?.setLive?.('Connection ' + text);
  }
  function notify(newState, detail = {}) {
    state = newState;
    events.dispatchEvent(new CustomEvent('connection', { detail: { state, ...detail } }));
  }
  function dispatch(payload) {
    if (payload == null) return;
    const type = payload.type || 'message';
    // Cross-channel chimes wired up a second, multiplexed SSE stream
    // (/api/workspace/events) that emits a 'roster' event per channel's
    // hub, not just the one currently open. This used to overwrite
    // Trio.state.members unconditionally on ANY roster event — so another
    // channel's roster tick (worse, AGENT_INBOX_CHANNEL's, which lists
    // every agent ever created) could replace the currently-viewed
    // channel's member list out from under the user. Only apply it when
    // the event is actually FOR the channel being viewed.
    if (type === 'roster' && Array.isArray(payload.members) && payload.channel === Trio.state.channel) {
      Trio.state.members = new Map(payload.members.map(member => [member.id, member]));
    }
    events.dispatchEvent(new CustomEvent(type, { detail: payload }));
    if (payload.id != null && (type === 'message' || type === 'message_update')) { lastId = payload.id; }
  }
  function onMessage(event) {
    try {
      const payload = JSON.parse(event.data);
      if (Array.isArray(payload)) { payload.forEach(dispatch); }
      else if (Array.isArray(payload.messages)) { payload.messages.forEach(dispatch); }
      else { dispatch(payload); }
    } catch (error) { console.warn('invalid Trio event', error); }
  }
  // For the operator the workspace-wide stream (/api/workspace/events) is the
  // real live feed — it covers every channel including whatever's open. The
  // per-channel stream is supplementary. The connection pill must therefore
  // follow the workspace stream when it's up, or the operator sees "offline"
  // whenever no per-channel stream is open (Home view, or a DM whose channel is
  // the shared inbox) even though live events are flowing. workspaceLive tracks
  // it so the per-channel paths below don't clobber the pill back to offline.
  let workspaceLive = false;
  function startEvents(channel = null) {
    if (!channel) {
      // No per-channel stream to open. Only report offline if the operator's
      // workspace stream isn't carrying the feed.
      if (!workspaceLive) { setConnection('offline', true); notify('offline', { reason: 'no channel' }); }
      return;
    }
    stream?.close();
    if (!workspaceLive) setConnection('connecting');
    notify('connecting');
    stream = new EventSource(Trio.api.url('/api/events'));
    stream.onopen = () => { setConnection('live'); notify('live'); };
    stream.onmessage = onMessage;
    stream.onerror = () => { if (!workspaceLive) setConnection('reconnecting…', true); notify('reconnecting'); };
  }
  let workspaceStream;
  function startWorkspaceEvents() {
    workspaceStream?.close();
    notify('workspace:connecting');
    workspaceStream = new EventSource('/api/workspace/events');
    workspaceStream.onopen = () => { workspaceLive = true; setConnection('live'); notify('workspace:live'); };
    workspaceStream.onmessage = onMessage;
    workspaceStream.onerror = () => { workspaceLive = false; setConnection('reconnecting…', true); notify('workspace:reconnecting'); };
  }
  function stopWorkspaceEvents() { workspaceStream?.close(); workspaceStream = null; workspaceLive = false; notify('workspace:offline'); setConnection('offline', true); }
  function stopEvents() { stream?.close(); stream = null; if (!workspaceLive) setConnection('offline', true); notify('offline', { reason: 'stopped' }); }
  Trio.startEvents = startEvents;
  Trio.stopEvents = stopEvents;
  Trio.startWorkspaceEvents = startWorkspaceEvents;
  Trio.stopWorkspaceEvents = stopWorkspaceEvents;
  Trio.events = events;
  // Exposed for testability — the DOM test harness has no real EventSource,
  // so this is the entry point tests use to simulate an SSE payload arriving
  // (from either stream; dispatch() itself doesn't know which one a given
  // payload came from, by design — see the cross-channel roster-clobber fix).
  Trio.dispatchSSEEvent = dispatch;
})();
