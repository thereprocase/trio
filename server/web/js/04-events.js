(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const events = Trio.events = Trio.events || new EventTarget();
  let stream;
  let currentChannel = null;
  let lastId = null;
  let state = 'connecting';
  const STALE_AFTER_MS = 45_000;
  const WATCHDOG_INTERVAL_MS = 10_000;
  let workspaceStartedAt = 0;
  let channelStartedAt = 0;
  let workspaceLastReceivedAt = 0;
  let channelLastReceivedAt = 0;
  let watchdog = null;
  let lifecycleInstalled = false;
  let sawHidden = false;
  let lastLifecycleRestartAt = 0;
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
  let channelLive = false;
  function showLiveWhenAllRequiredFeedsAreFresh() {
    const workspaceReady = !workspaceStream || workspaceLive;
    const channelReady = !stream || channelLive;
    if (workspaceReady && channelReady && (workspaceStream || stream)) setConnection('live');
  }
  function markChannelFresh() {
    channelLastReceivedAt = Date.now();
    const wasLive = channelLive;
    channelLive = true;
    showLiveWhenAllRequiredFeedsAreFresh();
    if (!wasLive) notify('live');
  }
  function markWorkspaceFresh() {
    workspaceLastReceivedAt = Date.now();
    const wasLive = workspaceLive;
    workspaceLive = true;
    showLiveWhenAllRequiredFeedsAreFresh();
    if (!wasLive) notify('workspace:live');
  }
  function ensureWatchdog() {
    if (!watchdog) {
      watchdog = setInterval(() => checkEventFreshness(), WATCHDOG_INTERVAL_MS);
      // Browser timers are numeric; Node's test timers support unref so a
      // passive watchdog cannot keep a completed harness process alive.
      watchdog?.unref?.();
    }
  }
  function maybeStopWatchdog() {
    if (!workspaceStream && !stream && watchdog) {
      clearInterval(watchdog);
      watchdog = null;
    }
  }
  function startEvents(channel = null) {
    currentChannel = channel || null;
    if (!channel) {
      // No per-channel stream to open. Only report offline if the operator's
      // workspace stream isn't carrying the feed.
      if (!workspaceLive) { setConnection('offline', true); notify('offline', { reason: 'no channel' }); }
      return;
    }
    stream?.close();
    channelLive = false;
    channelLastReceivedAt = 0;
    setConnection('connecting…', true);
    notify('connecting');
    channelStartedAt = Date.now();
    const source = new EventSource(Trio.api.url('/api/events'));
    stream = source;
    source.onopen = () => {
      if (stream !== source) return;
      channelStartedAt = Date.now();
      channelLastReceivedAt = 0;
      channelLive = false;
      setConnection('waiting for updates…', true);
      notify('connected');
    };
    source.onmessage = event => {
      if (stream !== source) return;
      markChannelFresh(); onMessage(event);
    };
    source.addEventListener('heartbeat', () => { if (stream === source) markChannelFresh(); });
    source.onerror = () => {
      if (stream !== source) return;
      channelLive = false;
      setConnection('reconnecting…', true);
      notify('reconnecting');
    };
    ensureWatchdog();
  }
  let workspaceStream;
  function startWorkspaceEvents() {
    workspaceStream?.close();
    workspaceLive = false;
    workspaceLastReceivedAt = 0;
    workspaceStartedAt = Date.now();
    setConnection('connecting…', true);
    notify('workspace:connecting');
    const source = new EventSource('/api/workspace/events');
    workspaceStream = source;
    source.onopen = () => {
      if (workspaceStream !== source) return;
      workspaceStartedAt = Date.now();
      workspaceLastReceivedAt = 0;
      workspaceLive = false;
      setConnection('waiting for updates…', true); notify('workspace:connected');
    };
    source.onmessage = event => {
      if (workspaceStream !== source) return;
      markWorkspaceFresh(); onMessage(event);
    };
    source.addEventListener('heartbeat', () => { if (workspaceStream === source) markWorkspaceFresh(); });
    source.onerror = () => {
      if (workspaceStream !== source) return;
      workspaceLive = false; setConnection('reconnecting…', true); notify('workspace:reconnecting');
    };
    ensureWatchdog();
  }
  function restartVisibleStreams() {
    if (document.visibilityState === 'hidden' || document.hidden) return;
    const hadWorkspace = !!workspaceStream;
    if (hadWorkspace) startWorkspaceEvents();
    if (currentChannel) startEvents(currentChannel);
  }
  function checkEventFreshness(now = Date.now()) {
    if (document.visibilityState === 'hidden' || document.hidden) return false;
    const workspaceStale = !!workspaceStream
      && now - Math.max(workspaceLastReceivedAt, workspaceStartedAt) > STALE_AFTER_MS;
    const channelStale = !!stream
      && now - Math.max(channelLastReceivedAt, channelStartedAt) > STALE_AFTER_MS;
    if (!workspaceStale && !channelStale) return false;
    setConnection('reconnecting…', true);
    if (workspaceStale) {
      workspaceLive = false;
      notify('workspace:stale', { lastReceivedAt: workspaceLastReceivedAt || null });
      startWorkspaceEvents();
    }
    if (channelStale && currentChannel) {
      channelLive = false;
      notify('stale', { lastReceivedAt: channelLastReceivedAt || null });
      startEvents(currentChannel);
    }
    return true;
  }
  function recoverFromLifecycle() {
    if (document.visibilityState === 'hidden' || document.hidden) return;
    const now = Date.now();
    if (now - lastLifecycleRestartAt < 1_000) return;
    lastLifecycleRestartAt = now;
    restartVisibleStreams();
  }
  function installLifecycleRecovery() {
    if (lifecycleInstalled) return;
    lifecycleInstalled = true;
    document.addEventListener?.('visibilitychange', () => {
      if (document.visibilityState === 'hidden' || document.hidden) {
        sawHidden = true;
        // A new background/foreground cycle deserves its own recovery even
        // when it begins inside the prior cycle's signal-coalescing window.
        lastLifecycleRestartAt = 0;
        return;
      }
      if (sawHidden) { sawHidden = false; recoverFromLifecycle(); }
    });
    window.addEventListener?.('pageshow', event => { if (event.persisted) recoverFromLifecycle(); });
    window.addEventListener?.('online', recoverFromLifecycle);
  }
  function stopWorkspaceEvents() {
    workspaceStream?.close(); workspaceStream = null; workspaceLive = false;
    workspaceStartedAt = 0; workspaceLastReceivedAt = 0;
    notify('workspace:offline'); setConnection('offline', true); maybeStopWatchdog();
  }
  function stopEvents() {
    stream?.close(); stream = null; currentChannel = null; channelLive = false;
    channelStartedAt = 0; channelLastReceivedAt = 0;
    if (!workspaceLive) setConnection('offline', true);
    notify('offline', { reason: 'stopped' }); maybeStopWatchdog();
  }
  installLifecycleRecovery();
  Trio.startEvents = startEvents;
  Trio.stopEvents = stopEvents;
  Trio.startWorkspaceEvents = startWorkspaceEvents;
  Trio.stopWorkspaceEvents = stopWorkspaceEvents;
  Trio.events = events;
  Trio.checkEventFreshness = checkEventFreshness;
  // Exposed for testability — the DOM test harness has no real EventSource,
  // so this is the entry point tests use to simulate an SSE payload arriving
  // (from either stream; dispatch() itself doesn't know which one a given
  // payload came from, by design — see the cross-channel roster-clobber fix).
  Trio.dispatchSSEEvent = dispatch;
})();
