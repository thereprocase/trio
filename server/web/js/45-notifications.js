(() => {
  'use strict';
  const Trio = window.Trio;
  if (!Trio) throw new Error('Notifications requires Trio core');
  const { state, events } = Trio;

  // ── Tier classification ──────────────────────────────────────────────
  // Highest priority first. A message is classified into the FIRST tier it
  // qualifies for — see 40-preferences.js's NOTIFICATION_TIERS comment for
  // the full rationale (DM > @/! > # > plain).
  function classify(msg, operatorId) {
    if (!msg || !operatorId) return null;
    if (msg.member_id === operatorId) return null; // never chime/notify on your own message
    if (Trio.markdown?.isSystemContent?.(msg.content || '')) return null;
    const recipients = msg.recipients || [];
    if (recipients.length && recipients.includes(operatorId)) return 'dm';
    const mentions = msg.mentions || [], bangs = msg.bangs || [], refs = msg.refs || [];
    if (mentions.includes(operatorId) || bangs.includes(operatorId)) return 'mention';
    if (refs.includes(operatorId)) return 'ref';
    return 'plain';
  }

  // ── Per-conversation mute ─────────────────────────────────────────────
  // LOTC/Frodo: the channel menu's "Mute notifications" item was a stub —
  // clicking it just toasted "muted" and did nothing. Cross-channel chimes
  // make that broken promise a real problem (a chatty channel you can't
  // silence now interrupts you from anywhere, not just while it's open),
  // so this needed to become real, not just get worse quietly.
  const MUTE_STORAGE_KEY = 'trio.mutedConversations.v1';
  function readMuted() {
    try { return new Set(JSON.parse(localStorage.getItem(MUTE_STORAGE_KEY) || '[]')); }
    catch { return new Set(); }
  }
  function writeMuted(set) {
    try { localStorage.setItem(MUTE_STORAGE_KEY, JSON.stringify([...set])); }
    catch { /* storage unavailable (private mode / quota) — mute just won't persist */ }
  }
  function isMuted(key) { return !!key && readMuted().has(key); }
  // Returns the NEW muted state (true = now muted) so callers can render
  // the right label/toast without a separate isMuted() round-trip.
  function toggleMute(key) {
    if (!key) return false;
    const set = readMuted();
    const nowMuted = !set.has(key);
    if (nowMuted) set.add(key); else set.delete(key);
    writeMuted(set);
    return nowMuted;
  }
  // A live message carries recipients (participant ids) but not the DM's
  // own `key` (that's a server-assigned id from /api/dms, not derivable
  // from the message alone) — resolve it by matching the participant SET
  // against the DM list already loaded client-side. Falls back to the
  // channel code for a channel message, or '' if neither resolves (a DM
  // whose thread hasn't been opened/loaded yet won't have an entry in
  // state.dms.your_dms — best-effort: it just won't be mutable yet, not a
  // crash or a wrong mute).
  function conversationKeyFor(msg) {
    if (!msg) return '';
    if (msg.recipients?.length) {
      const participants = new Set([...msg.recipients, msg.member_id].filter(Boolean));
      const dm = (state.dms?.your_dms || []).find(d => {
        const ids = new Set(d.member_ids || []);
        return ids.size === participants.size && [...participants].every(id => ids.has(id));
      });
      if (dm) return 'dm:' + dm.key;
    }
    return msg.channel || '';
  }

  // ── Chime synthesis (WebAudio, no audio asset) ───────────────────────
  // Three presets instead of one fixed tone, so different tiers can sound
  // distinct (a DM shouldn't sound like an untargeted channel message).
  // Ported from the pre-modularization dashboard's playChime/alertBlocked
  // (git history: f4f0c27, 77b7b96) — same synthesis technique, split into
  // named presets instead of one hardcoded ping.
  const SOUNDS = {
    // Two-note ping, E6 -> A6 — the original default chime.
    ping: { wave: 'sine', decay: 0.40, notes: [[1318.51, 0], [1760.0, 0.09]] },
    // Urgent two-note fall, A5 -> D5, square wave — cuts through; was
    // previously reserved for a peer going 'blocked', reused here for the
    // highest-priority tiers (dm/mention).
    alert: { wave: 'square', decay: 0.55, notes: [[880.0, 0], [587.33, 0.16]] },
    // A single short, quiet tick for low-priority tiers.
    tick: { wave: 'sine', decay: 0.18, notes: [[660.0, 0]] },
  };
  let audioCtx = null;
  function ensureAudio() {
    if (audioCtx) return audioCtx;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      audioCtx = AC ? new AC() : null;
    } catch { audioCtx = null; }
    return audioCtx;
  }
  function playPreset(name, volume) {
    const preset = SOUNDS[name] || SOUNDS.ping;
    const vol = Math.max(0, Math.min(1, Number(volume)));
    if (vol <= 0) return;
    const ctx = ensureAudio();
    if (!ctx) return;
    if (ctx.state === 'suspended') { try { ctx.resume(); } catch { /* needs a user gesture first; next real chime after one will work */ } }
    try {
      const now = ctx.currentTime;
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(vol, now + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + preset.decay);
      gain.connect(ctx.destination);
      preset.notes.forEach(([freq, t]) => {
        const osc = ctx.createOscillator();
        osc.type = preset.wave;
        osc.frequency.value = freq;
        osc.connect(gain);
        osc.start(now + t);
        osc.stop(now + t + preset.decay);
      });
    } catch { /* best-effort — a chime failing must never break message delivery */ }
  }

  // ── Priming guard ─────────────────────────────────────────────────────
  // /api/events seeds each new SSE subscription with a burst of recent
  // history (nth_web.py EventHub.subscribe's "prime" payloads) through the
  // SAME queue and event type as live messages — nothing in the payload
  // shape distinguishes the two. Without a guard, opening a channel (or a
  // reconnect) replays a chime/popup for every recent message at once.
  //
  // LOTC/Sauron: an earlier version of this guard used a single shared
  // "priming" flag armed by any 'connection' event across BOTH the
  // per-channel (/api/events) and cross-channel (/api/workspace/events)
  // streams — since both funnel into the same Trio.events target with no
  // way to tell which stream a message came from, a reconnect on the
  // (currently unused, but extant) workspace stream could silently
  // suppress a genuinely live chime on an already-open, unrelated channel.
  // Comparing each message's own timestamp against wall-clock time is
  // immune to that: it's a property of the message, not of which stream
  // delivered it, so it needs no per-stream bookkeeping at all.
  const PRIME_MAX_AGE_MS = 5000;
  function isPrimedHistory(msg) {
    const createdAt = Date.parse(msg.created_at || '');
    if (!Number.isFinite(createdAt)) return false; // unparseable — don't guess, let it through
    return Date.now() - createdAt > PRIME_MAX_AGE_MS;
  }

  // ── Desktop notification ─────────────────────────────────────────────
  function showDesktopNotification(msg, tier, inCurrentChannel) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try {
      const where = tier === 'dm' ? 'DM' : (!inCurrentChannel && msg.channel) ? `#${msg.channel}` : `#${state.channel || ''}`;
      const title = `${where} — ${msg.member_name || msg.member_id}`;
      const n = new Notification(title, {
        body: String(msg.content || '').slice(0, 140),
        tag: 'trio-' + msg.id,
        silent: true, // the chime (if enabled) already covers sound; avoid a second, uncontrolled OS ding
      });
      n.onclick = () => { window.focus(); if (msg.channel) Trio.workspace?.openChannel?.(msg.channel); n.close(); };
    } catch { /* best-effort */ }
  }

  // ── Cross-stream de-dup ───────────────────────────────────────────────
  // The currently-open channel is covered by TWO independent SSE
  // connections at once once the workspace-wide stream is running: the
  // per-channel one (/api/events, scoped to state.channel) AND the
  // cross-channel one (/api/workspace/events, multiplexing every channel's
  // hub — see 00-core.js). Both dispatch the identical message through the
  // same Trio.events target, so without de-dup the open channel's own
  // messages would chime/notify TWICE. Message ids are globally unique and
  // monotonic, so a bounded "seen" set is enough — no per-stream tagging
  // needed. Capped so a long session can't grow this unboundedly.
  const SEEN_CAP = 500;
  const seenIds = new Set();
  function alreadySeen(id) {
    if (seenIds.has(id)) return true;
    seenIds.add(id);
    if (seenIds.size > SEEN_CAP) seenIds.delete(seenIds.values().next().value);
    return false;
  }

  function onMessage(event) {
    const msg = event.detail;
    if (!msg || msg.id == null || isPrimedHistory(msg) || alreadySeen(msg.id)) return;
    if (isMuted(conversationKeyFor(msg))) return; // per-conversation override beats tier settings entirely
    const operatorId = (state.operator || state.meta?.operator)?.id;
    const tier = classify(msg, operatorId);
    if (!tier) return;
    const prefs = Trio.preferences?.read?.();
    if (!prefs) return;
    const Tier = tier.charAt(0).toUpperCase() + tier.slice(1);
    if (prefs.chime && prefs['chimeTier' + Tier]) playPreset(prefs['chimeSound' + Tier], prefs.chimeVolume);
    // A message in a channel you're not currently viewing deserves a popup
    // regardless of tab focus — you can't see it just by looking at the
    // screen. For the channel you ARE viewing, keep the original behavior:
    // only pop up while the tab itself is hidden (the chime already covers
    // the tab-focused-elsewhere case).
    const inCurrentChannel = !msg.channel || msg.channel === state.channel;
    if (prefs.notifications && prefs['notifyTier' + Tier] && (document.hidden || !inCurrentChannel)) {
      showDesktopNotification(msg, tier, inCurrentChannel);
    }
  }
  events.addEventListener('message', onMessage);

  Trio.notifications = { classify, playPreset, isPrimedHistory, isMuted, toggleMute, conversationKeyFor, SOUNDS, TIERS: ['dm', 'mention', 'ref', 'plain'] };
})();
