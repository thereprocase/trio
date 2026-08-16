(() => {
  'use strict';
  const Trio = window.Trio;
  if (!Trio) throw new Error('Composer requires Trio core');
  const { state, api, events, actions } = Trio;
  state.selectedTargets = state.selectedTargets instanceof Set ? state.selectedTargets : new Set();
  state.pendingAttachments = Array.isArray(state.pendingAttachments) ? state.pendingAttachments : [];
  state.drafts = state.drafts || {};
  // Bug C: composer artifacts must belong to the conversation they were made
  // in, never ride along when you switch threads. Text drafts were already
  // keyed per-conversation; do the same for @-target chips and pasted images.
  // attachmentStore[cid] is the SOURCE OF TRUTH for a conversation's pending
  // images — state.pendingAttachments is an alias to the CURRENT one (by
  // reference), so an in-flight upload started in conversation X keeps writing
  // to X's array even after you navigate to Y. targetDrafts[cid] holds the
  // @-target ids (rebuilt into the selectedTargets Set on load).
  state.targetDrafts = state.targetDrafts || {};
  state.attachmentStore = state.attachmentStore || {};
  let recognition = null, recorder = null, stream = null, chunks = [];
  // Metering is a SEPARATE stream from the one MediaRecorder/SpeechRecognition
  // consumes — SpeechRecognition never exposes its underlying audio, so a
  // level meter needs its own getUserMedia grab regardless of engine, and
  // local mode keeps its recorder stream independent for a cleaner teardown.
  let meterStream = null, audioCtx = null, analyser = null, meterRaf = null;
  // LOTC/Sauron+Uruk-Hai: toggleDictation()'s "already active" guard only
  // sees `recognition`/`recorder`, both still null during localDictation()'s
  // getUserMedia await — a double-click in that window ran two concurrent
  // localDictation() calls, each overwriting the SAME module vars, leaking
  // the first stream/recorder/AudioContext with nothing left able to stop
  // them (and corrupting `chunks`, shared across both). `starting` closes
  // that window; `dictationGen` (bumped on every stop) lets an in-flight
  // async callback (e.g. browserDictation's metering getUserMedia) detect
  // it's stale — see LOTC/Aragorn's unmount-race finding below.
  let starting = false, dictationGen = 0;
  const byId = id => document.getElementById(id);
  const input = () => byId('input');
  // The message box is a contenteditable div so @-mentions render as inline
  // chips AS YOU TYPE. The tree is kept FLAT — only text nodes and chip <span>s,
  // newlines as literal "\n" (Enter is handled manually) — so the plain-text
  // value is just el.textContent and the caret is a simple char offset.
  const escM = s => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  function getText() { const el = input(); return el ? (el.textContent || '') : ''; }
  function mentionInfo(sigil, word) {
    const w = word.toLowerCase();
    // Only `all` is a real broadcast — the server parses @all/!all and nothing
    // else (see nth_web _parse_sigils_against_roster). Don't chip `everyone`:
    // it stays plain text, an honest "this won't wake anyone" signal.
    if ((sigil === '@' || sigil === '!') && w === 'all') return { cls: 'inline-all' };
    if (sigil !== '@') return null;
    for (const m of (state.members?.values() || [])) {
      if (m && ((m.name && m.name.toLowerCase() === w) || (m.id && m.id.toLowerCase() === w))) {
        return { cls: 'inline-mention', tone: Trio.avatarTone(m.name) || 'eucalyptus' };
      }
    }
    return null;
  }
  function buildMentionHtml(text) {
    let out = '', last = 0, m;
    const re = /[@!]([^\s.,;:!?()[\]{}]+)/g;
    while ((m = re.exec(text))) {
      const info = mentionInfo(text[m.index], m[1]);
      out += escM(text.slice(last, m.index));
      out += info ? `<span class="${info.cls}"${info.tone ? ` data-tone="${info.tone}"` : ''}>${escM(m[0])}</span>` : escM(m[0]);
      last = m.index + m[0].length;
    }
    // A trailing "\n" does NOT render a visible empty final line under
    // white-space:pre-wrap (Chrome trims it), so a single Shift+Enter looked
    // like a no-op — you had to press it twice before the blank line appeared.
    // Append a filler <br> when the text ends in a newline: a <br> contributes
    // nothing to textContent, so getText() still returns the exact value.
    const html = out + escM(text.slice(last));
    return text.endsWith('\n') ? html + '<br>' : html;
  }
  // Caret as a plain-text char offset. Guarded — the Node test DOM has no Selection.
  function getCaret() {
    const el = input(); const sel = typeof window !== 'undefined' && window.getSelection && window.getSelection();
    if (!el || !sel || !sel.rangeCount) return null;
    const r = sel.getRangeAt(0);
    if (!el.contains(r.startContainer)) return null;
    const pre = r.cloneRange(); pre.selectNodeContents(el); pre.setEnd(r.startContainer, r.startOffset);
    return pre.toString().length;
  }
  function setCaret(offset) {
    const el = input(); const sel = typeof window !== 'undefined' && window.getSelection && window.getSelection();
    if (!el || !sel || typeof document.createRange !== 'function') return;
    const range = document.createRange(); let remaining = offset, placed = false, node;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while ((node = walker.nextNode())) {
      const len = node.nodeValue.length;
      if (remaining <= len) { range.setStart(node, remaining); range.collapse(true); placed = true; break; }
      remaining -= len;
    }
    if (!placed) { range.selectNodeContents(el); range.collapse(false); }
    sel.removeAllRanges(); sel.addRange(range);
  }
  // Re-render mention chips from the current text, preserving the caret (on input).
  function renderChips() {
    const el = input(); if (!el || isComposing) return;
    const text = el.textContent || '';
    const html = buildMentionHtml(text);
    // Undo-preserving fast path: when the text needs no chip and the box holds
    // only text nodes (no chip span, no stray <br>/<div>), the browser's own
    // edit already renders it correctly — skip the innerHTML rewrite so native
    // undo/redo survives plain-text typing. The rewrite still runs whenever a
    // chip must appear/disappear or the DOM has drifted structurally. Force-
    // clear on empty so a stray <br> can't block the :empty placeholder.
    if (!text) { if (el.innerHTML !== '') el.innerHTML = ''; return; }
    if (html === escM(text) && el.children.length === 0) return;
    if (el.innerHTML !== html) { const c = getCaret(); el.innerHTML = html; if (c != null) setCaret(c); }
  }
  // Programmatic text set (drafts / dictation / clear / autocomplete pick).
  function setValue(text, caret) {
    const el = input(); if (!el) return;
    el.innerHTML = buildMentionHtml(text || '');
    if (caret != null) setCaret(caret);
  }
  function insertText(t) {
    const text = getText(); const caret = getCaret() ?? text.length;
    setValue(text.slice(0, caret) + t + text.slice(caret), caret + t.length);
    updateSendState(); saveDraft(); renderTargetHint(); updateAutocomplete();
  }
  function inputValue(newValue) { if (newValue !== undefined) setValue(newValue); return getText(); }
  function resize() { /* contenteditable auto-sizes via CSS min/max-height */ }

  function targetName(id) { return state.members?.get(id)?.name || id; }
  function conversationId() { return state.dmKey ? 'dm:' + state.dmKey : (state.channel || 'home'); }
  function saveDraft() { const el = input(); if (!el) return; state.drafts[conversationId()] = getText(); }
  function loadDraft() {
    const el = input(); if (!el) return;
    stopDictation();
    const key = conversationId();
    setValue(state.drafts[key] || '');
    updateSendState();
  }
  // The pending-image array that belongs to conversation `cid` (created on
  // demand). state.pendingAttachments always aliases the CURRENT conversation's
  // array (set by loadComposerAux), so an upload started here writes to this
  // thread even after the operator navigates away (Bug C).
  function attStore(cid) { return (state.attachmentStore[cid] || (state.attachmentStore[cid] = [])); }
  // Swap the composer's @-targets + pending images to the conversation now in
  // view. Called on every route change (mirrors loadDraft for text).
  function loadComposerAux() {
    const cid = conversationId();
    state.selectedTargets = new Set(state.targetDrafts[cid] || []);
    state.pendingAttachments = attStore(cid);
    renderTargets(); renderAttachments(); updateSendState();
  }
  function apiUrl(path) {
    if (typeof api.url === 'function') return api.url(path);
    const channel = state.channel || '';
    return channel ? path + (path.includes('?') ? '&' : '?') + 'channel=' + encodeURIComponent(channel) : path;
  }
  function escHtml(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function revokePreview(att) { if (att && att.url && att.url.startsWith('blob:')) { URL.revokeObjectURL(att.url); att.url = ''; } }
  // Open the pending-upload previews as one gallery in the shared lightbox,
  // starting on the clicked thumbnail.
  function openPreviewLightbox(url) {
    const gallery = state.pendingAttachments
      .filter(a => a && a.url)
      .map(a => ({ url: a.url, alt: a.filename || 'attachment' }));
    const at = gallery.findIndex(g => g.url === url);
    Trio.lightbox.open(gallery, at < 0 ? 0 : at);
  }
  function renderTargets() {
    const bar = byId('target-bar'); if (!bar) return;
    bar.replaceChildren();
    state.selectedTargets.forEach(id => {
      const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'target-chip';
      chip.textContent = '@' + targetName(id) + ' ×';
      // Name the shortcut on the thing it acts on, so Alt+N is discoverable
      // instead of folklore. Digits past 9 simply have no shortcut.
      const slot = targetOrder().indexOf(id) + 1;
      chip.title = slot >= 1 && slot <= 9
        ? `Remove target (Alt+${slot} toggles)` : 'Remove target';
      chip.onclick = () => { state.selectedTargets.delete(id); renderTargets(); saveDraft(); };
      bar.append(chip);
    });
    renderTargetHint();
    // Persist this conversation's @-target chips (Bug C) so they never leak
    // into the next thread you open.
    state.targetDrafts[conversationId()] = [...state.selectedTargets];
  }
  // In a DM, an @-mention of someone who isn't a participant is inert on the
  // server (narrow_wake): it neither wakes nor reaches them. Now that mentions
  // live inline in the text, scan the text itself for @name / @<id> that resolve
  // to a non-participant and surface that above the composer — but never block
  // the send (jds: Slack behavior — mentioning a non-member is just their name
  // as text). Idempotent: clears any prior hint so it can run on every keystroke.
  function renderTargetHint() {
    const bar = byId('target-bar'); if (!bar) return;
    bar.querySelectorAll('.composer-hint').forEach(n => n.remove());
    if (!state.dmKey) return;
    const text = getText();
    if (!text) return;
    const peers = Array.isArray(state.dmMemberIds) ? state.dmMemberIds : [];
    const opId = state.operator?.id;
    const outsiders = [];
    for (const m of (state.members?.values() || [])) {
      if (!m || !m.id || peers.includes(m.id) || m.id === opId) continue;
      if ([m.name, m.id].some(tok => tok && new RegExp('@' + reEsc(tok) + '(?:\\b|$)', 'i').test(text))) {
        outsiders.push(m.name || m.id);
      }
    }
    if (!outsiders.length) return;
    const hint = document.createElement('span');
    hint.className = 'composer-hint';
    hint.textContent = `${outsiders.join(', ')} won't be notified — this is a private DM. Message them directly to reach them.`;
    bar.append(hint);
  }
  // The numbered order behind Alt+1..9. Sorted by name so a given agent keeps
  // the same digit across renders — a hash or roster-insertion order would
  // renumber people as the room changes, which makes the shortcut unusable.
  // The operator is excluded: you cannot address yourself.
  function targetOrder() {
    const opId = state.operator?.id || state.meta?.operator?.id;
    return [...(state.members instanceof Map ? state.members.values() : [])]
      .filter(m => m && m.id && m.id !== opId)
      .sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)))
      .map(m => m.id);
  }
  function toggleTarget(id) {
    if (state.selectedTargets.has(id)) state.selectedTargets.delete(id);
    else state.selectedTargets.add(id);
    renderTargets(); saveDraft(); updateSendState();
  }
  function clearTargets() {
    if (!state.selectedTargets.size) return;
    state.selectedTargets.clear();
    renderTargets(); saveDraft(); updateSendState();
  }
  // Alt+A is a toggle, not an "add all": pressing it twice returns to a
  // broadcast rather than leaving every agent selected.
  function toggleAllTargets() {
    const all = targetOrder();
    if (state.selectedTargets.size >= all.length && all.length) clearTargets();
    else { state.selectedTargets = new Set(all); renderTargets(); saveDraft(); updateSendState(); }
  }
  function setTargets(ids) { state.selectedTargets = new Set(ids || []); renderTargets(); }
  function insertTarget(id) { if (id) { state.selectedTargets.add(id); renderTargets(); input()?.focus(); } }
  // Mentions now live INLINE in the text (@name / @all, inserted at the caret),
  // so the content is sent verbatim. The server derives the wake set by parsing
  // @/!-sigils out of this text (nth_web _handle_send → _parse_sigils_against_
  // roster); the old "prepend selected targets to the front" behaviour is what
  // produced the "@Gale Hi , thanks" reordering bug, so it's gone.
  function renderedContent() {
    return getText().trim();
  }
  function validate() {
    if (state.readOnly) return false;
    // An in-flight upload's placeholder has id:0 and gets silently dropped by
    // buildSendPayload's `id > 0` filter — sending mid-upload used to eat the
    // attachment with no warning (LOTC/Frodo). Block send until every pending
    // attachment has resolved (succeeded or been removed).
    if (state.pendingAttachments.some(a => a.loading)) return false;
    return !!renderedContent() || state.pendingAttachments.length > 0;
  }
  function buildSendPayload() {
    const body = {
      content: renderedContent(),
      attachment_ids: state.pendingAttachments.map(a => a.id).filter(id => Number.isInteger(id) && id > 0),
    };
    if (state.dmKey && state.dmMemberIds?.length) body.recipients = state.dmMemberIds.slice();
    else if (state.dmTargetId) body.recipients = [state.dmTargetId];
    if (state.composerReply?.id) {
      body.reply_to = state.composerReply.id;
      if (state.composerReply.selection) body.selection = state.composerReply.selection;
    }
    return body;
  }
  function updateSendState() { const send = byId('send'); if (send) send.disabled = !validate(); }

  async function upload(file) {
    if (!file) return;
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type || '')) throw new Error('Choose a PNG, JPEG, GIF, or WebP image');
    if (file.size > 10 * 1024 * 1024) throw new Error('Image must be 10 MB or smaller');
    // Bind this upload to the conversation it started in. `arr` is that
    // thread's source-of-truth array; we render only while it's still the one
    // on screen, so navigating away mid-upload never spills the image (or a
    // stuck loading placeholder) into the conversation you land on (Bug C).
    const cid = conversationId();
    const arr = attStore(cid);
    const preview = URL.createObjectURL(file);
    const placeholder = { id: 0, filename: file.name || 'image', loading: true, url: preview };
    arr.push(placeholder);
    if (cid === conversationId()) renderAttachments();
    updateSendState();
    try {
      // Trio.api.upload carries the same request this used to inline; keeping
      // one copy means a server-side change to the upload contract cannot fix
      // one caller and leave the other silently posting the wrong encoding.
      const attachment = await Trio.api.upload(file);
      if (!attachment.ok || !Number.isInteger(attachment.id)) throw new Error('Upload did not return an attachment id');
      revokePreview(placeholder);
      Object.assign(placeholder, attachment, { name: attachment.filename, loading: false, url: apiUrl(attachment.url) });
    } catch (error) {
      revokePreview(placeholder);
      const index = arr.indexOf(placeholder);
      if (index >= 0) { arr.splice(index, 1); }
      throw error;
    } finally {
      // Must run on the error path too — otherwise a failed upload leaves
      // validate()'s loading-guard with nothing left to clear and the send
      // button stays stuck disabled (every caller re-throws past this point
      // to a bare .catch(toast), never re-calling updateSendState itself).
      if (cid === conversationId()) renderAttachments();
      updateSendState();
    }
  }
  function renderAttachments() {
    const strip = byId('attachment-strip'); if (!strip) return;
    strip.replaceChildren();
    state.pendingAttachments.forEach((attachment, index) => {
      const thumb = document.createElement('div');
      thumb.className = 'attachment-thumb';
      thumb.title = attachment.filename || 'attachment';
      const img = document.createElement('img');
      img.src = attachment.url || '';
      img.alt = attachment.filename || 'attachment';
      img.loading = 'lazy';
      // A bare onclick <img> is unreachable by keyboard/screen-reader users
      // (LOTC/Frodo) — the remove button next to it already does this right.
      img.tabIndex = 0;
      img.setAttribute('role', 'button');
      img.setAttribute('aria-label', 'View full image: ' + img.alt);
      const openLightbox = () => openPreviewLightbox(img.src);
      img.onclick = openLightbox;
      img.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openLightbox(); } };
      const rm = document.createElement('button');
      rm.type = 'button'; rm.className = 'rm'; rm.title = 'remove';
      rm.setAttribute('aria-label', 'remove attachment');
      rm.textContent = '×';
      rm.disabled = attachment.loading;
      rm.onclick = () => { revokePreview(attachment); state.pendingAttachments.splice(index, 1); renderAttachments(); updateSendState(); };
      thumb.append(img, rm);
      if (attachment.loading) {
        const mask = document.createElement('div');
        mask.className = 'loading-mask';
        mask.textContent = '…';
        thumb.append(mask);
      }
      strip.append(thumb);
    });
  }
  // ── auto-mention guard (#4) ──────────────────────────────────────────
  // A broadcast that names no agent wakes nobody — the server builds the wake
  // set solely from @/!-sigils in the text. Guard the common "forgot to @ them"
  // slip: a sole agent gets auto-@'d; with several agents we warn once, then
  // honour a deliberate second Send. `#name` doesn't count (it only wakes an
  // agent listening on 'about'), so it never satisfies the guard.
  let noMentionConfirmed = false;
  function reEsc(s) { return String(s).replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&'); }
  function agentMembers() {
    const opId = state.operator?.id;
    return [...(state.members?.values() || [])].filter(m => m && m.kind !== 'human' && m.id !== opId);
  }
  function contentWakesAnAgent(text) {
    if (!text) return false;
    if (/[@!]all(?:\b|$)/i.test(text)) return true;
    return agentMembers().some(m =>
      [m.name, m.id].some(tok => tok && new RegExp('[@!]' + reEsc(tok) + '(?:\\b|$)', 'i').test(text)));
  }
  async function send() {
    if (!validate()) return false;
    let content = renderedContent();
    const isDM = !!(state.dmKey || state.dmTargetId || (state.dmMemberIds && state.dmMemberIds.length));
    if (!isDM) {
      const agents = agentMembers();
      if (agents.length && !contentWakesAnAgent(content)) {
        if (agents.length === 1) {
          content = ('@' + (agents[0].name || agents[0].id) + ' ' + content).trim();
        } else if (!noMentionConfirmed) {
          Trio.ui.toast('No agent @-mentioned — nobody will wake. Add @name or @all, or press Send again to post anyway.');
          noMentionConfirmed = true;
          return false;
        }
      }
    }
    noMentionConfirmed = false;
    const button = byId('send'); if (button) button.disabled = true;
    const body = buildSendPayload();
    body.content = content;
    try {
      const result = await api.post(apiUrl('/api/send'), body);
      // Clear THIS conversation's composer state (text + @-targets + images);
      // other threads' drafts are untouched (Bug C).
      const cid = conversationId();
      delete state.drafts[cid];
      state.targetDrafts[cid] = [];
      attStore(cid).forEach(revokePreview);
      state.attachmentStore[cid] = [];
      state.selectedTargets = new Set();
      state.pendingAttachments = attStore(cid);
      inputValue(''); state.composerReply = null;
      renderTargets(); renderAttachments(); updateSendState();
      if (result?.message) Trio.conversation?.upsert(result.message);
      events.dispatchEvent(new CustomEvent('sent', { detail: result }));
      return true;
    } catch (error) {
      Trio.ui.toast('Message not sent: ' + error.message); return false;
    } finally { updateSendState(); }
  }
  function stopTracks() { stream?.getTracks?.().forEach(track => track.stop()); stream = null; }
  function hasBrowserDictation() { return typeof window.SpeechRecognition === 'function' || typeof window.webkitSpeechRecognition === 'function'; }
  function hasLocalDictation() { return !!window.navigator?.mediaDevices?.getUserMedia && typeof window.MediaRecorder === 'function'; }
  // Simple 5-bar level meter driven by an AnalyserNode — enough to show
  // "yes, your voice is registering" without a full waveform canvas. Reuses
  // whatever MediaStream the caller already opened; browser-engine mode has
  // no stream of its own (SpeechRecognition doesn't expose one) so it opens
  // a metering-only one that captures nothing but the level display.
  function startMeter(meterStreamSource) {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return;
      audioCtx = new AudioContext();
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 32;
      audioCtx.createMediaStreamSource(meterStreamSource).connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);
      const bars = byId('dictate-meter')?.querySelectorAll('.bar');
      const meter = byId('dictate-meter');
      if (meter) meter.hidden = false;
      const tick = () => {
        analyser.getByteFrequencyData(data);
        if (bars) {
          const step = Math.max(1, Math.floor(data.length / bars.length));
          bars.forEach((bar, i) => {
            const level = data[i * step] / 255; // 0..1
            bar.style.setProperty('--level', String(0.15 + level * 0.85));
          });
        }
        meterRaf = requestAnimationFrame(tick);
      };
      tick();
    } catch { /* metering is a nice-to-have; dictation itself still works */ }
  }
  function stopMeter() {
    if (meterRaf) cancelAnimationFrame(meterRaf);
    meterRaf = null;
    analyser = null;
    if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
    meterStream?.getTracks?.().forEach(track => track.stop());
    meterStream = null;
    const meter = byId('dictate-meter');
    if (meter) meter.hidden = true;
  }
  // active: recording/listening (red stop icon + meter). processing: local
  // mode's post-stop transcription request (button disabled, status text
  // names the engine so it's clear this isn't the browser's own STT).
  //
  // button.dataset.unavailable (set once at mount(), see below) tracks the
  // "no mic support in this browser" disablement, which is independent of
  // and must survive the active/processing toggling done here.
  function setDictationButtonState(active, { processing = false, statusText = '' } = {}) {
    const button = byId('dictate-btn');
    const status = byId('dictate-status');
    if (button) {
      button.setAttribute('aria-pressed', String(active));
      button.classList.toggle('recording', active);
      button.classList.toggle('processing', processing);
      button.disabled = processing || button.dataset.unavailable === 'true';
      button.querySelector('.mic-icon')?.toggleAttribute('hidden', active);
      button.querySelector('.stop-icon')?.toggleAttribute('hidden', !active);
      const label = processing ? (statusText || 'Transcribing…')
        : active ? 'Stop dictation' : (button.disabled ? 'Dictation is unavailable in this browser' : 'Dictate');
      button.title = label;
      // LOTC/Frodo: aria-label was static ("Dictate") regardless of state, so
      // a screen reader's announced name never matched the visible tooltip.
      button.setAttribute('aria-label', label);
    }
    if (status) {
      status.hidden = !statusText;
      status.textContent = statusText;
    }
    // LOTC/Frodo: the visible status text was removed for the recording-start
    // case (redundant with the red button + waveform for sighted users), but
    // that text was a screen reader's only chance at a state announcement.
    // #trio-aria-live already exists for exactly this — announce here
    // without reinstating any visible clutter. Idle state clears it.
    Trio.ui?.setLive?.(processing ? (statusText || 'Transcribing') : active ? 'Recording' : '');
  }
  function stopDictation() {
    dictationGen++; // invalidate any in-flight async callback from this session (LOTC/Aragorn)
    if (recognition) { recognition.stop(); recognition = null; }
    if (recorder?.state === 'recording') recorder.stop();
    stopTracks(); stopMeter(); document.body.classList.remove('dictating'); setDictationButtonState(false);
  }
  async function browserDictation() {
    const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Speech) throw new Error('Browser speech recognition is unavailable');
    const myGen = dictationGen; // captured now — stopDictation()/unmount() bump this
    recognition = new Speech(); recognition.continuous = true; recognition.interimResults = true;
    // Same language as the server-side Whisper path. Left unset, the browser
    // transcribes in ITS OWN locale — so an NTH_STT_LANG=fr deployment would
    // return French from /api/stt/transcribe and English from the browser,
    // decided by nothing but which dictation route the visitor's device took.
    recognition.lang = /*__STT_LANG__*/'en-US';
    let finalText = '';
    recognition.onresult = event => { let interim = ''; for (let i = event.resultIndex; i < event.results.length; i++) event.results[i].isFinal ? finalText += event.results[i][0].transcript : interim += event.results[i][0].transcript; inputValue((inputValue() + ' ' + finalText + interim).trim()); updateSendState(); };
    recognition.onend = () => { recognition = null; stopMeter(); document.body.classList.remove('dictating'); setDictationButtonState(false); };
    // LOTC/Aragorn: request the metering stream only once `onstart` confirms
    // SpeechRecognition's OWN mic permission already resolved, instead of
    // firing a second concurrent getUserMedia() request right away — on a
    // first-ever grant that raced two simultaneous browser permission
    // prompts for what looks like one user action.
    recognition.onstart = () => {
      window.navigator.mediaDevices?.getUserMedia?.({ audio: true }).then(s => {
        // Stale by the time this resolved (stopped/unmounted, or a newer
        // dictation session started) — don't leak a mic stream + AudioContext
        // with nothing left able to close them (LOTC/Aragorn, critical).
        if (dictationGen !== myGen) { s.getTracks().forEach(t => t.stop()); return; }
        meterStream = s; startMeter(s);
      }).catch(() => {});
    };
    // No statusText here — the level meter already shows "I'm recording";
    // a redundant "Listening…" label next to a red pulsing button and a
    // waveform is one signal too many (exampleuser).
    recognition.start(); document.body.classList.add('dictating'); setDictationButtonState(true);
  }
  async function localDictation() {
    if (!hasLocalDictation()) throw new Error('Local dictation is unavailable in this browser');
    // LOTC/Sauron+Uruk-Hai: `toggleDictation`'s "already active" guard checks
    // `recognition`/`recorder`, both still null during the getUserMedia
    // await below — a rapid double-click ran two of these concurrently,
    // each overwriting the SAME module vars (stream/recorder/chunks/
    // audioCtx/analyser/meterRaf), leaking the first stream+AudioContext
    // with nothing left able to stop them, and corrupting the shared
    // `chunks` array between two live recorders.
    if (starting) return;
    starting = true;
    try {
      stream = await window.navigator.mediaDevices.getUserMedia({ audio: true }); chunks = [];
    } finally { starting = false; }
    recorder = new window.MediaRecorder(stream);
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      setDictationButtonState(false, { processing: true, statusText: 'Transcribing (local Whisper)…' });
      // LOTC/Sauron: the fallback below starts a NEW dictation session
      // (browserDictation) without awaiting it, so this handler's own
      // `finally` used to run right after and unconditionally strip the
      // 'dictating' class / reset the button — wiping out the state the
      // fallback had just set, even though its mic (`recognition`) was
      // still live and listening. `fellBack` skips that stomp.
      let fellBack = false;
      try {
        const audio = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
        const result = await fetch(apiUrl('/api/stt/transcribe'), { method: 'POST', headers: { 'Content-Type': audio.type || 'audio/webm' }, body: audio });
        const data = await result.json();
        if (!result.ok || !data.ok) throw new Error(data.error || 'transcription failed');
        inputValue((inputValue() + ' ' + (data.text || '')).trim()); updateSendState();
      } catch (error) {
        if (window.SpeechRecognition || window.webkitSpeechRecognition) {
          fellBack = true;
          Trio.ui.toast((error.message || 'Local transcription failed') + '. Falling back to browser speech recognition.');
          // Not awaited — this function's own `finally` below runs first
          // (synchronously, before this promise settles) and already skips
          // its teardown because `fellBack` is true; if the fallback itself
          // then fails, its OWN teardown has to happen here instead.
          browserDictation().catch(fallback => { Trio.ui.toast(fallback.message); document.body.classList.remove('dictating'); setDictationButtonState(false); });
        }
        else Trio.ui.toast(error.message || 'Transcription failed');
      } finally {
        stopTracks();
        if (!fellBack) { document.body.classList.remove('dictating'); setDictationButtonState(false); }
      }
    };
    // No statusText while actively recording (see browserDictation) — the
    // "Transcribing (local Whisper)…" text right after IS still useful,
    // since that's invisible processing time the waveform can't represent.
    recorder.start(); document.body.classList.add('dictating'); setDictationButtonState(true);
    startMeter(stream);
  }
  async function toggleDictation() {
    // Mid-getUserMedia-await: neither recorder nor stream exists yet, so
    // there's nothing for stopDictation() to stop — just ignore the extra
    // click rather than tearing down a session that hasn't started.
    if (starting) return;
    if (recognition || recorder?.state === 'recording') return stopDictation();
    const mode = Trio.preferences?.read?.().sttMode || 'local';
    if (mode === 'web') return browserDictation();
    try { return await localDictation(); }
    catch (error) {
      if (!hasBrowserDictation()) throw error;
      Trio.ui.toast((error.message || 'Local dictation failed') + '. Falling back to browser speech recognition.');
      return browserDictation();
    }
  }
  const domListeners = [];
  let unroute;
  let ac = null;
  let isComposing = false;
  let acIndex = -1;
  let acMatches = [];
  let acToken = null;
  function acEsc(s) { return Trio.markdown.escapeHtml(String(s ?? '')); }
  function acContainer() { return (input() || document.getElementById('input'))?.closest('.composer-shell') || document.body; }
  function closeAutocomplete() { if (ac) { ac.remove(); ac = null; } acIndex = -1; acMatches = []; acToken = null; }
  function findToken(value, caret) {
    let i = caret - 1;
    while (i >= 0 && /[^\s\n]/.test(value[i]) && !/[@#!]/.test(value[i])) i--;
    if (i < 0 || !/[@#!]/.test(value[i])) return null;
    const sigil = value[i];
    if (i > 0 && /[^\s\n]/.test(value[i - 1])) return null;
    return { sigil, start: i, query: value.slice(i + 1, caret) };
  }
  function openAutocomplete(token, matches) {
    closeAutocomplete();
    acToken = token; acMatches = matches; acIndex = 0;
    ac = document.createElement('div'); ac.className = 'ac-pop'; ac.setAttribute('role', 'listbox');
    ac.innerHTML = `<div class="ac-hd">${acEsc(token.sigil === '@' ? 'Mention' : token.sigil === '#' ? 'Reference' : 'Bang')}</div>` +
      matches.map((m, i) => `<button class="ac-opt ${i === 0 ? 'hi' : ''}" data-index="${i}" role="option" aria-selected="${i === 0}"><span class="sig">${acEsc(token.sigil)}</span><span class="nm">${acEsc(m.name)}</span><span class="rl">${acEsc(m.kind || 'agent')}</span></button>`).join('');
    ac.querySelectorAll('button').forEach(b => b.addEventListener('click', () => selectMatch(Number(b.dataset.index))));
    acContainer().append(ac);
  }
  function updateAutocomplete() {
    if (isComposing) return;
    const el = input(); if (!el) return;
    const text = getText();
    const token = findToken(text, getCaret() ?? text.length);
    if (!token || token.query.includes(',')) { closeAutocomplete(); return; }
    const q = token.query.toLowerCase();
    const matches = [...(state.members?.values() || [])]
      .filter(m => m && ((m.name || '').toLowerCase().startsWith(q) || (m.id || '').startsWith(q)))
      .slice(0, 6)
      .map(m => ({ id: m.id, name: m.name || m.id, kind: m.kind || 'agent' }));
    // Synthetic broadcast target: @all / !all wake everyone in the channel
    // (the server honours both — see _parse_sigils_against_roster). Offer it
    // for the ping + bang sigils when the query is a prefix of all/everyone.
    if ((token.sigil === '@' || token.sigil === '!') && ('all'.startsWith(q) || 'everyone'.startsWith(q))) {
      matches.unshift({ id: 'all', name: 'all', kind: 'everyone' });
    }
    if (matches.length) openAutocomplete(token, matches); else closeAutocomplete();
  }
  function selectMatch(index) {
    const match = acMatches[index]; const token = acToken; if (!match || !token) return;
    const el = input(); if (!el) return;
    const text = getText(); const caret = getCaret() ?? text.length;
    const before = text.slice(0, token.start);
    const after = text.slice(caret);
    // Insert the mention inline at the caret; setValue re-renders it as a chip.
    // @name / @all stay in the text (the server parses them for the wake set),
    // so a mention can sit anywhere in the message, not hoisted to the front.
    const label = match.name || match.id;
    const insert = token.sigil + label + ' ';
    setValue(before + insert + after, before.length + insert.length);
    el.focus();
    closeAutocomplete(); updateSendState(); saveDraft();
  }
  function moveAutocomplete(delta) {
    if (!ac || !acMatches.length) return;
    acIndex = (acIndex + delta + acMatches.length) % acMatches.length;
    const buttons = ac.querySelectorAll('.ac-opt');
    buttons.forEach((b, i) => { b.classList.toggle('hi', i === acIndex); b.setAttribute('aria-selected', String(i === acIndex)); });
  }
  function setInputState(text) {
    if (!text) return;
    // contenteditable div — no .disabled/.placeholder; use contentEditable and a
    // data-placeholder rendered via CSS :empty::before.
    const ro = !!state.readOnly;
    text.contentEditable = ro ? 'false' : 'true';
    text.setAttribute('aria-readonly', String(ro));
    text.dataset.placeholder = ro ? 'This conversation is archived.' : 'Message the room…';
  }
  function syncReadOnly() { setInputState(input()); updateSendState(); }
  function init() {
    const text = input(), sendButton = byId('send'), attach = byId('attach-btn');
    if (!text) return;
    setInputState(text);
    // Rendering happens via loadComposerAux() at the end of init (and on every
    // route change) so it always reflects THIS conversation's stored artifacts,
    // never leftover state from a prior mount (Bug C).
    const onInput = () => { noMentionConfirmed = false; renderChips(); updateSendState(); saveDraft(); updateAutocomplete(); renderTargetHint(); };
    const onCompositionStart = () => { isComposing = true; };
    const onCompositionEnd = () => { isComposing = false; renderChips(); updateAutocomplete(); };
    text.addEventListener('compositionstart', onCompositionStart); domListeners.push([text, 'compositionstart', onCompositionStart]);
    text.addEventListener('compositionend', onCompositionEnd); domListeners.push([text, 'compositionend', onCompositionEnd]);
    text.addEventListener('input', onInput); domListeners.push([text, 'input', onInput]);
    // Re-style the composer when the open channel's roster (re)loads. Navigating
    // to a non-conversation view runs showView(), which clears state.members and
    // state.channel; returning re-renders the restored draft via refresh() BEFORE
    // the roster arrives async, so an @-mention fell back to plain text and target
    // chips showed raw ids. When the matching 'roster' event lands, re-render the
    // inline mention chips + target chips so a mentioned member stays recognized
    // across navigation (mirrors 11-conversation.js's onRoster).
    const onRoster = event => {
      if (!event.detail || event.detail.channel !== state.channel) return;
      renderChips(); renderTargets();
    };
    events.addEventListener('roster', onRoster); domListeners.push([events, 'roster', onRoster]);
    const onKey = event => {
      // During IME composition, Enter/arrows belong to the input method — never
      // hijack them to send or drive autocomplete (would mis-send a half-
      // composed message, the classic CJK footgun).
      if (event.isComposing || isComposing) return;
      if (ac) {
        if (event.key === 'ArrowDown') { event.preventDefault(); moveAutocomplete(1); }
        else if (event.key === 'ArrowUp') { event.preventDefault(); moveAutocomplete(-1); }
        else if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); selectMatch(acIndex); }
        else if (event.key === 'Escape') { event.preventDefault(); closeAutocomplete(); }
        return;
      }
      // Alt+1..9 toggles a recipient, Alt+A all, Alt+0 clears. Restored from
      // the client this one replaced: addressing a message to specific agents
      // is the core operator gesture here, and it had gone from one keystroke
      // to typing "@", waiting for the autocomplete, arrowing and tabbing.
      // Not offered inside a DM, where the recipient is already fixed.
      if (event.altKey && !event.ctrlKey && !event.metaKey && !state.dmTargetId) {
        if (event.key >= '1' && event.key <= '9') {
          const id = targetOrder()[Number(event.key) - 1];
          if (id) { toggleTarget(id); event.preventDefault(); }
          return;
        }
        if (event.key === '0') { clearTargets(); event.preventDefault(); return; }
        if (event.key === 'a' || event.key === 'A') { toggleAllTargets(); event.preventDefault(); return; }
      }
      // contenteditable would otherwise insert a <div>/<br> on Enter; handle it
      // ourselves so the tree stays flat (plain \n) — Enter sends, Shift+Enter
      // inserts a newline.
      if (event.key === 'Enter') { event.preventDefault(); if (event.shiftKey) insertText('\n'); else send(); }
    };
    text.addEventListener('keydown', onKey); domListeners.push([text, 'keydown', onKey]);
    const sendClick = () => send();
    sendButton?.addEventListener('click', sendClick); if (sendButton) domListeners.push([sendButton, 'click', sendClick]);
    const onAttach = () => { const picker = document.createElement('input'); picker.type = 'file'; picker.accept = 'image/*'; picker.onchange = () => upload(picker.files[0]).catch(error => Trio.ui.toast(error.message)); picker.click(); };
    attach?.addEventListener('click', onAttach); if (attach) domListeners.push([attach, 'click', onAttach]);
    const onPaste = async (event) => {
      const clip = event.clipboardData || window.clipboardData;
      if (!clip) return;
      const images = [];
      if (clip.files && clip.files.length) {
        for (const f of clip.files) { if (/^image\//.test(f.type)) images.push(f); }
      } else if (clip.items) {
        for (const it of clip.items) {
          if (it.kind === 'file' && /^image\//.test(it.type)) {
            const f = it.getAsFile(); if (f) images.push(f);
          }
        }
      }
      if (!images.length) {
        // Plain-text paste: ALWAYS preventDefault so the browser can never drop
        // rich clipboard HTML into the contenteditable (XSS) — then insert only
        // the plain-text form, which flows through mention chipping. An
        // HTML-only clipboard (no text/plain) inserts nothing.
        event.preventDefault();
        const t = clip.getData && clip.getData('text/plain');
        if (t) insertText(t);
        return;
      }
      event.preventDefault();
      for (const f of images) await upload(f).catch(error => Trio.ui.toast(error.message));
    };
    text.addEventListener('paste', onPaste); if (text) domListeners.push([text, 'paste', onPaste]);
    // Drag-drop mirrors paste: the browser would otherwise insert rich dropped
    // HTML straight into the contenteditable (same XSS surface). Intercept it —
    // upload dropped images, insert only the plain-text form of anything else.
    const onDrop = async (event) => {
      const dt = event.dataTransfer; if (!dt) return;
      event.preventDefault();
      const images = [];
      for (const f of (dt.files || [])) { if (/^image\//.test(f.type)) images.push(f); }
      if (images.length) { for (const f of images) await upload(f).catch(error => Trio.ui.toast(error.message)); return; }
      const t = dt.getData && dt.getData('text/plain');
      if (t) insertText(t);
    };
    const onDragOver = (event) => { event.preventDefault(); };
    text.addEventListener('drop', onDrop); if (text) domListeners.push([text, 'drop', onDrop]);
    text.addEventListener('dragover', onDragOver); if (text) domListeners.push([text, 'dragover', onDragOver]);
    const dictation = Trio.preferences?.read?.().dictation !== false;
    const dictateBtn = byId('dictate-btn');
    const dictationAvailable = hasLocalDictation() || hasBrowserDictation();
    if (dictateBtn) {
      dictateBtn.hidden = !dictation;
      dictateBtn.dataset.unavailable = String(dictation && !dictationAvailable);
      dictateBtn.disabled = dictation && !dictationAvailable;
      if (!dictationAvailable) dictateBtn.title = 'Dictation is unavailable in this browser';
    }
    const onDictate = () => toggleDictation().catch(error => Trio.ui.toast(error?.message || 'Dictation failed'));
    if (dictation && dictationAvailable && dictateBtn) { dictateBtn.addEventListener('click', onDictate); domListeners.push([dictateBtn, 'click', onDictate]); }
    // Aux (targets/images) loading is driven by loadConversation → refresh(),
    // which runs after channel/dmKey are final; the router hook only needs the
    // text draft + input state (kept as-is to avoid touching existing flows).
    unroute = Trio.router?.on?.(() => { loadDraft(); setInputState(text); });
    loadDraft(); loadComposerAux();
  }
  function unmount() {
    domListeners.forEach(([el, type, fn]) => el?.removeEventListener?.(type, fn)); domListeners.length = 0;
    if (unroute) { unroute(); unroute = null; }
    // Browser-engine (web) mode only ever set `recognition`, never `recorder`
    // — the old check here missed it entirely, leaking an open mic stream
    // (recognition's own capture, plus this file's metering stream) past
    // navigation away from the composer.
    if (recognition || (recorder && recorder.state !== 'inactive')) stopDictation();
  }
  function mount() { init(); }
  // Reload the composer for the conversation now in view (text + @-targets +
  // pending images). Called from loadConversation AFTER it has set the final
  // channel/dmKey, because openChannel fires the router BEFORE that state
  // update — so the router hook alone would read stale state (Bug C).
  function refresh() { loadDraft(); loadComposerAux(); setInputState(input()); }
  Object.assign(actions, { sendMessage: send, setTargets, insertTarget, uploadImage: upload, toggleDictation, stopDictation, buildSendPayload });
  Trio.composer = { init, mount, unmount, render: renderTargets, refresh, send, setTargets, insertTarget, targetOrder, toggleTarget, clearTargets, toggleAllTargets, upload, toggleDictation, stopDictation, buildSendPayload, syncReadOnly, setDictationButtonState };
})();
