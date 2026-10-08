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
  // True while a finished recording is being sent to /api/stt/transcribe.
  let transcribing = false;
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
    // Images only: an uploaded PDF or ZIP also has a url, and the lightbox shows <img>.
    const gallery = state.pendingAttachments
      .filter(a => a && a.url && INLINE_IMAGE.test(a.mime || ''))
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
  function hasDmRecipients() {
    return !!(state.dmMemberIds?.length || state.dmTargetId);
  }
  function validate() {
    if (state.readOnly || (state.dmKey && (state.dmRouteResolved === false || !hasDmRecipients()))) return false;
    // An in-flight upload's placeholder has id:0 and gets silently dropped by
    // buildSendPayload's `id > 0` filter — sending mid-upload used to eat the
    // attachment with no warning (LOTC/Frodo). Block send until every pending
    // attachment has resolved (succeeded or been removed).
    if (state.pendingAttachments.some(a => a.loading)) return false;
    return !!renderedContent() || state.pendingAttachments.length > 0;
  }
  function buildSendPayload() {
    // Declaring a DM without recipients must never fall through to a channel
    // post. The disabled composer is UX; this throw is the privacy boundary for
    // programmatic callers and any future send path that bypasses validate().
    if (state.dmKey && !hasDmRecipients()) throw new Error('Private conversation has no resolved recipients');
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

  // What the server accepts (it sniffs the bytes; this only gives a fast, clear
  // refusal). Office files are ZIPs. A type the browser leaves blank, common for
  // HEIC and logs, is judged by its extension.
  const UPLOAD_TYPES = /^(image\/(png|jpeg|gif|webp|heic|heif)|application\/(pdf|zip|x-zip-compressed|vnd\.openxmlformats-officedocument\..+)|text\/(plain|csv))$/;
  const UPLOAD_EXTENSIONS = /\.(png|jpe?g|gif|webp|heic|heif|pdf|zip|docx|xlsx|pptx|txt|csv|log|md)$/i;
  const UPLOAD_ACCEPT = 'image/*,.heic,.heif,application/pdf,.pdf,.zip,.docx,.xlsx,.pptx,.txt,.csv,.log,.md';
  const MAX_UPLOAD_MB = 25;
  const INLINE_IMAGE = /^image\/(png|jpeg|gif|webp)$/;
  async function uploadAll(files) {
    // One at a time, in order: each keeps its own placeholder and error.
    for (const f of files) await upload(f).catch(error => Trio.ui.toast(error.message));
  }
  async function upload(file) {
    if (!file) return;
    if (!UPLOAD_TYPES.test(file.type || '') && !UPLOAD_EXTENSIONS.test(file.name || '')) throw new Error((file.name || 'That file') + ': attach images, HEIC photos, PDFs, ZIP or Office files, or text');
    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) throw new Error((file.name || 'File') + ' is larger than ' + MAX_UPLOAD_MB + ' MB');
    // Bind this upload to the conversation it started in. `arr` is that
    // thread's source-of-truth array; we render only while it's still the one
    // on screen, so navigating away mid-upload never spills the image (or a
    // stuck loading placeholder) into the conversation you land on (Bug C).
    const cid = conversationId();
    const arr = attStore(cid);
    const inline = INLINE_IMAGE.test(file.type || '');
    const preview = inline ? URL.createObjectURL(file) : '';
    const placeholder = { id: 0, filename: file.name || 'file', mime: file.type || '', loading: true, url: preview };
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
      const rmButton = () => {
        const rm = document.createElement('button');
        rm.type = 'button'; rm.className = 'rm'; rm.title = 'remove';
        rm.setAttribute('aria-label', 'remove attachment');
        rm.textContent = '×';
        rm.disabled = attachment.loading;
        rm.onclick = () => { revokePreview(attachment); state.pendingAttachments.splice(index, 1); renderAttachments(); updateSendState(); };
        return rm;
      };
      if (!INLINE_IMAGE.test(attachment.mime || '')) {
        // A file that is not a web image: a labelled chip, no preview.
        thumb.classList.add('attachment-file');
        const label = document.createElement('span');
        label.className = 'attachment-file-name';
        label.textContent = '📄 ' + (attachment.filename || 'file');
        thumb.append(label, rmButton());
        if (attachment.loading) {
          const mask = document.createElement('div'); mask.className = 'loading-mask'; mask.textContent = '…'; thumb.append(mask);
        }
        strip.append(thumb);
        return;
      }
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
      thumb.append(img, rmButton());
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
    if (state.dmKey && !hasDmRecipients()) {
      Trio.ui.toast('Private conversation is not ready — message not sent.');
      return false;
    }
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
    try {
      const body = buildSendPayload();
      body.content = content;
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
  // SpeechRecognition reports failures as a terse code on an error event.
  // Unmapped, the user sees nothing useful; unhandled (as it was), the user
  // sees NOTHING AT ALL, because onend fires straight after and quietly
  // resets the button to idle. Every entry here is a state the operator can
  // act on, so name the action rather than the code.
  const SPEECH_ERRORS = {
    'not-allowed': 'Microphone access was blocked. Allow it for this site in your browser settings.',
    // Conditional: now that hasBrowserDictation() refuses insecure origins,
    // the http case barely fires and the live causes are system-level — a Mac
    // or iPhone with Dictation switched off, or a policy block, both on https,
    // where "reload over https" sends the user chasing a URL that is already
    // correct. (LOTC/Frodo)
    'service-not-allowed': () => window.isSecureContext === false
      ? "Your browser won't allow the microphone here because this page is http. Open the dashboard's https address instead."
      : 'Your browser refused speech recognition. On a Mac or iPhone, check that Dictation is turned on in System Settings → Keyboard, then try again.',
    'no-speech': 'No speech was detected. Try again and start speaking right after you tap the mic.',
    'audio-capture': 'No microphone was found.',
    // This engine transcribes on a REMOTE server, not on the device, so a
    // dead network breaks it with the mic working fine. But `network` is also
    // what a Chromium browser that is not Chrome reports permanently: the
    // speech service needs Google API keys that only Chrome ships, so forks
    // (Dia, Brave, Vivaldi, plain Chromium builds) expose a working-looking
    // webkitSpeechRecognition that can never succeed. Naming only the
    // connection sends someone to debug a network that is fine, so name the
    // likelier cause and the option that does work — the local engine runs on
    // the operator's own machine and has neither problem.
    // Checkable cause first, permanent one second. On a phone over Tailscale
    // a weak signal produces this too, and leading with a lecture about
    // Chromium forks buries the thing the user can actually go fix.
    // (LOTC/Frodo)
    'network': "Speech recognition couldn't reach its speech server. Check your connection and try again. If you're on a Chromium browser that isn't Chrome (Dia, Brave, Vivaldi) it can never work — switch dictation to Hub in Preferences.",
    'aborted': '',   // user pressed stop; not a failure worth a toast
    'language-not-supported': 'This browser cannot transcribe the configured language.',
  };
  // Joins two pieces of transcript with one space, except between CJK
  // characters, which are written without spaces (NTH_STT_LANG can be ja/zh).
  const CJK = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;
  function joinSpeech(a, b) {
    if (!a) return b; if (!b) return a;
    return CJK.test(a.slice(-1)) && CJK.test(b[0]) ? a + b : a + ' ' + b;
  }
  const speechKey = s => s.toLowerCase();
  // One piece "repeats" another when it equals it or starts with it as whole
  // words — the two shapes Android Chrome produces (a duplicated final, or a
  // cumulative final that restates everything said so far and adds to it).
  // Whole words: a raw string prefix turned "no" + "nobody came" into
  // "nobody came", and "so" + "some people" into "some people". CJK text has
  // no spaces between words, so any boundary counts there.
  function repeats(piece, earlier) {
    if (!earlier) return false;
    const p = speechKey(piece), e = speechKey(earlier);
    if (!p.startsWith(e)) return false;
    if (p.length === e.length) return true;
    const next = p[e.length];
    return /[\s\p{P}]/u.test(next) || CJK.test(next) || CJK.test(e.slice(-1));
  }
  // Collapses a run of transcript pieces into text. A piece that repeats the
  // whole text so far, or just the previous piece, REPLACES it; anything else
  // is appended. Trade-off: someone who says "Yes." and then, as a separate
  // utterance, "Yes, and…" loses the first "Yes." — rare, and a duplicated
  // sentence on every Android pause was not.
  function collapseSpeech(pieces) {
    let segments = [];
    for (const raw of pieces) {
      const piece = String(raw || '').replace(/\s+/g, ' ').trim();
      if (!piece) continue;
      if (repeats(piece, segments.reduce(joinSpeech, ''))) segments = [piece];
      else if (repeats(piece, segments[segments.length - 1])) segments[segments.length - 1] = piece;
      else segments.push(piece);
    }
    return segments.reduce(joinSpeech, '');
  }
  // Composer text after dictating `spoken`: the text that was already in the
  // box, then the dictation, then nothing extra when nothing was heard.
  function withBaseline(baseline, spoken) {
    if (!spoken) return baseline;
    if (!baseline) return spoken;
    return /\s$/.test(baseline) ? baseline + spoken : baseline + ' ' + spoken;
  }
  // ── Spoken sigils ──
  // "hey Bones" → @Bones, "hashtag Bones" → #Bones, "bang Bones" → !Bones,
  // "bang all" → !all. Applied to FINAL text only, so words never flicker
  // into sigils mid-sentence, and dictation only ever fills the box: the
  // user sees every sigil before sending, which matters because a bang is an
  // unfilterable emergency wake.
  //
  // "hey" is ordinary speech ("hey, can you check this"), so nothing changes
  // without a confident match against the channel's own members:
  //   - the 1-3 words after the trigger are joined and compared with each
  //     name, ignoring case, spaces and punctuation ("boat man" = BOATman,
  //     "codex sol" = codex-sol), with number words read as digits;
  //   - names of four letters or fewer need an exact or near-exact hearing
  //     (SHORT_NAME_SCORE): one wrong letter in "sol" is a different word
  //     ("hey sole"), where one wrong letter in "bones" is a mishearing;
  //   - similarity is normalized Levenshtein, which tolerates one misheard
  //     letter in a five-letter name ("bonds" → Bones) but rejects the
  //     prefix overlaps Jaro-Winkler rewards ("big bang theory" must not wake
  //     a member called Theo);
  //   - the best match must clear SIGIL_MIN_SCORE and beat the runner-up
  //     member by SIGIL_MARGIN, so "hey claude" with claude-1 and claude-2 in
  //     the room is left as spoken;
  //   - "all" is accepted for bang only, and only when heard exactly.
  //
  // "bang" is held to more, because a false bang is an unfilterable wake of
  // the whole room: only a single following word counts, the trigger may not
  // carry a comma or full stop ("bang, all of it" is a sentence, while
  // "Bang! all" is how recognisers write the command), and "all" counts only
  // at the end of the utterance or before punctuation ("they bang all
  // night" is a sentence).
  const SIGIL_MIN_SCORE = 0.75;
  const SIGIL_MARGIN = 0.1;
  const SHORT_NAME_LETTERS = 4;
  const SHORT_NAME_SCORE = 0.9;
  const NUMBER_WORDS = { zero: '0', one: '1', two: '2', three: '3', four: '4', five: '5', six: '6', seven: '7', eight: '8', nine: '9', ten: '10' };
  const sigilKey = s => String(s || '').toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^\p{L}\p{N}]/gu, '');
  function levenshteinSimilarity(a, b) {
    if (!a.length || !b.length) return 0;
    let row = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      const next = [i];
      for (let j = 1; j <= b.length; j++) {
        next[j] = Math.min(row[j] + 1, next[j - 1] + 1, row[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      row = next;
    }
    return 1 - row[b.length] / Math.max(a.length, b.length);
  }
  // A word that can be part of a spoken name: starts with a letter or digit
  // (so a literal "@Bones" the recogniser wrote is never re-sigiled), and may
  // end in punctuation, which then ends the name.
  const NAME_WORD = /^[\p{L}\p{N}][\p{L}\p{N}'’_-]*([,.!?:;]*)$/u;
  function spokenTrigger(word, next) {
    const w = word.toLowerCase().replace(/[,.!?:;]+$/, '');
    if (w === 'hey') return { sigil: '@', words: 1 };
    if (w === 'bang') return /^bang!?$/i.test(word) ? { sigil: '!', words: 1 } : null;
    if (w === 'hashtag') return { sigil: '#', words: 1 };
    if (w === 'hash' && /^tag[,.!?:;]*$/i.test(next || '')) return { sigil: '#', words: 2 };
    return null;
  }
  // The confident member for the words after a trigger, or null.
  function matchSpokenName(words, sigil, names, utteranceFinal = true) {
    const candidates = names.map(name => ({ name, key: sigilKey(name), exactOnly: false })).filter(c => c.key);
    if (sigil === '!') candidates.push({ name: 'all', key: 'all', exactOnly: true });
    const best = new Map(); // name -> { score, n }
    const heard = [];
    const maxWords = sigil === '!' ? 1 : 3;
    for (let n = 1; n <= Math.min(maxWords, words.length); n++) {
      const m = words[n - 1].match(NAME_WORD);
      if (!m) break;
      heard.push(words[n - 1].replace(/[,.!?:;]+$/, ''));
      const spoken = [sigilKey(heard.join('')), sigilKey(heard.map(w => NUMBER_WORDS[w.toLowerCase()] || w).join(''))];
      for (const c of candidates) {
        let score = Math.max(...spoken.map(s => (c.exactOnly ? (s === c.key ? 1 : 0) : levenshteinSimilarity(s, c.key))));
        if (c.key.length <= SHORT_NAME_LETTERS && score < SHORT_NAME_SCORE) score = 0;
        // "all" only as the whole command: last word, or followed by punctuation.
        if (c.key === 'all' && c.exactOnly && !(m[1] || (utteranceFinal && words.length === n))) score = 0;
        const prior = best.get(c.name);
        if (!prior || score > prior.score) best.set(c.name, { score, n });
      }
      if (m[1]) break; // punctuation after this word ends the name
    }
    const ranked = [...best.entries()].map(([name, r]) => ({ name, ...r })).sort((a, b) => b.score - a.score);
    const [top, runnerUp] = ranked;
    if (!top || top.score < SIGIL_MIN_SCORE) return null;
    if (runnerUp && top.score - runnerUp.score < SIGIL_MARGIN) return null;
    return top;
  }
  // Rewrites spoken triggers in `text` as sigils for the given member names.
  // Pure: the composer passes the current channel's names.
  function applySpokenSigils(text, names, utteranceFinal = true) {
    const source = String(text || '');
    const words = [...source.matchAll(/\S+/g)].map(m => ({ raw: m[0], start: m.index, end: m.index + m[0].length }));
    let out = '', cursor = 0, i = 0;
    while (i < words.length) {
      const trigger = spokenTrigger(words[i].raw, words[i + 1]?.raw);
      const first = i + (trigger?.words || 0);
      const match = trigger && matchSpokenName(words.slice(first, first + 3).map(w => w.raw), trigger.sigil, names || [], utteranceFinal);
      if (!match) { i++; continue; }
      const last = words[first + match.n - 1];
      // Keep the name's closing punctuation ("@Bones?"), except the comma
      // that only separated the name from the rest of the sentence.
      const trail = last.raw.match(/[,.!?:;]*$/)[0].replace(/^,/, '');
      out += source.slice(cursor, words[i].start) + trigger.sigil + match.name + trail;
      cursor = last.end;
      i = first + match.n;
    }
    return out + source.slice(cursor);
  }
  // Names a spoken sigil may resolve to: the current channel's members, less
  // the operator (you cannot address yourself), each as a single token so
  // the result is a sigil the server parses.
  function spokenSigilNames() {
    const self = state.operator?.id;
    const names = [];
    for (const m of (state.members?.values() || [])) {
      if (!m || (self && m.id === self)) continue;
      const name = m.name && /^\S+$/.test(m.name) ? m.name : m.id;
      if (name) names.push(name);
    }
    return names;
  }
  // Sigils for a dictation started in conversation `startedIn`: matched
  // against that conversation's roster, taken live while it is still open
  // and as captured at the start once the user has moved elsewhere.
  function sigilFinalizer(startedIn) {
    const namesAtStart = spokenSigilNames();
    return (text, utteranceFinal = true) => applySpokenSigils(text, conversationId() === startedIn ? spokenSigilNames() : namesAtStart, utteranceFinal);
  }
  // Turns a SpeechRecognition session into composer text.
  //
  // Two things bit us, in this order.
  //
  // 1. The box must be rewritten from a fixed baseline on every event, never
  //    appended to. The original read the box back and appended the running
  //    transcript, so every interim event re-added the whole sentence so far
  //    ("today today is today is a…"). Baseline is captured once at session
  //    start, so text the operator had already typed is kept exactly once.
  //
  // 2. Android Chrome does not follow the desktop result model. Desktop keeps
  //    one growing results list, finals never change, and resultIndex points at
  //    the first new entry — so accumulating finals from resultIndex worked
  //    there. On Android, finals arrive duplicated or cumulative ("hello",
  //    then "hello world"), and resultIndex does not advance the same way, so
  //    the same accumulator produced "hello hello world". So the text is now
  //    REBUILT from the whole results list on every event (resultIndex is
  //    ignored) and repeated pieces are collapsed (collapseSpeech).
  //
  // Rebuilding from the list would lose words if a browser ever started a
  // fresh list mid-session. Finals are immutable in the spec, so a final that
  // vanishes, or turns into unrelated text, means the list was replaced: the
  // previous finals are carried forward instead of dropped.
  //
  // `finalize` rewrites FINAL text only (spoken sigils). The interim tail is
  // shown as heard, so a half-spoken "hey bo…" never flickers into a sigil.
  function makeSpeechAccumulator(baseline, finalize = text => text) {
    let carried = '';
    let previousFinals = [];
    return function absorb(results) {
      const list = Array.from({ length: results.length }, (_, i) => ({
        text: String(results[i]?.[0]?.transcript || '').replace(/\s+/g, ' ').trim(),
        isFinal: !!results[i]?.isFinal,
      }));
      const replaced = previousFinals.some((before, i) => {
        if (before == null) return false;
        const now = list[i];
        if (!now) return true;
        return !repeats(now.text, before) && !repeats(before, now.text);
      });
      if (replaced) carried = collapseSpeech([carried, ...previousFinals]);
      previousFinals = list.map(r => (r.isFinal ? r.text : null));
      const heard = collapseSpeech([carried, ...list.map(r => r.text)]);
      const finals = collapseSpeech([carried, ...list.filter(r => r.isFinal).map(r => r.text)]);
      const utteranceFinal = !list.some(r => !r.isFinal && r.text);
      // The interim tail normally follows the finals; when an interim
      // restates them instead, show it as heard until its final arrives.
      const spoken = finals && speechKey(heard).startsWith(speechKey(finals))
        ? joinSpeech(finalize(finals, utteranceFinal), heard.slice(finals.length).trim())
        : heard;
      return withBaseline(baseline, spoken);
    };
  }
  // These messages run to ~200 characters and name an action. The 3500ms
  // default is not long enough to read one on a phone, and there is no
  // dismiss control to buy time with. (LOTC/Frodo)
  const DICTATION_TOAST_MS = 9000;
  // Why the mic cannot be used, in the user's terms rather than the API's.
  // "Dictation is unavailable in this browser" was the old text and it was
  // usually FALSE: on an http:// tailnet URL the browser is perfectly capable
  // and the address is the problem. Naming the working address matters more
  // than naming the cause — "use the https one" is unactionable if nobody
  // says which. The server supplies it via /api/stt/health.secure_url,
  // because the page cannot know it and the server can.
  function unavailableReason() {
    if (window.isSecureContext === false) {
      const url = Trio.state?.secureUrl;
      return url
        ? `Dictation needs a secure (https) connection, and this page is http. Open ${url} instead.`
        : 'Dictation needs a secure (https) connection, and this page is http. Reopen the dashboard at its https address — restart the hub with --tailscale-tls if it does not have one yet.';
    }
    return 'This browser has no dictation support. Chrome, Edge or Safari can dictate here.';
  }
  // The transcribe endpoint relays its own internal failures verbatim —
  // "worker pipe broken", "worker exited mid-request", "worker sent malformed
  // response". The operator does not know what a worker is, and none of those
  // strings names an action. Translate the ones with a real answer; anything
  // unrecognised passes through unchanged rather than being flattened into a
  // vaguer message than the server bothered to send. (LOTC/Frodo)
  function humanEngineError(raw) {
    const message = String(raw || '');
    if (/mlx_whisper\) not installed|not installed/i.test(message)) {
      return "This hub's speech engine isn't installed. Use browser dictation, or install it on the hub.";
    }
    if (/worker|pipe|malformed/i.test(message)) {
      return 'Dictation stopped working on the server. Restarting the dashboard usually fixes it.';
    }
    if (/busy|try again/i.test(message)) {
      return 'The dictation engine is busy right now. Try again in a moment.';
    }
    return message;
  }
  function speechErrorMessage(code) {
    if (code in SPEECH_ERRORS) {
      const entry = SPEECH_ERRORS[code];
      return typeof entry === 'function' ? entry() : entry;
    }
    // Name the code AND an action. A code alone is a fact, not a way out —
    // and this default is precisely where an unfamiliar browser lands.
    return code
      ? `Browser speech recognition failed (${code}). Try again, or switch dictation engines in Preferences.`
      : 'Browser speech recognition failed. Try again, or switch dictation engines in Preferences.';
  }
  // A secure context is REQUIRED, not merely preferred: on an insecure origin
  // the constructor still exists (so an existence check passes happily) but
  // start() is refused. Reporting "available" there is what let the composer
  // promise a fallback it could never deliver, then fail silently.
  function hasBrowserDictation() {
    if (window.isSecureContext === false) return false;
    return typeof window.SpeechRecognition === 'function' || typeof window.webkitSpeechRecognition === 'function';
  }
  function hasLocalDictation() { return !!window.navigator?.mediaDevices?.getUserMedia && typeof window.MediaRecorder === 'function'; }
  // Whether the hub can actually transcribe (/api/stt/health). The local
  // engine records first and uploads after Stop, so choosing it on a hub
  // without Whisper — every Linux hub, since mlx_whisper is Apple-silicon
  // only — cost the user their whole utterance before saying anything.
  // Fetched once at mount and refreshed in the background when stale, so the
  // tap itself can decide synchronously and keep its user gesture (Safari
  // refuses SpeechRecognition.start() outside one).
  const STT_HEALTH_TTL_MS = 60000;
  let sttHealthCache = null, sttHealthPending = null;
  function refreshSttHealth() {
    if (sttHealthPending) return sttHealthPending;
    // Plain fetch rather than api.get: a guest who has not picked a name gets
    // a 403 here, and api.get answers that by prompting for a name — not
    // something a background check may do. Any failure reads as "unknown".
    sttHealthPending = fetch(apiUrl('/api/stt/health'))
      .then(response => (response.ok ? response.json() : null))
      .catch(() => null)
      .then(h => {
        // A failed check keeps the last real answer; it only restarts the clock.
        const available = h && typeof h.available === 'boolean' ? h.available : (sttHealthCache?.available ?? null);
        const detail = h ? (h.detail || '') : (sttHealthCache?.detail || '');
        // remote: the hub forwards audio to a speech service its operator
        // configured. Absent means the engine runs on the hub itself.
        const remote = h ? h.remote === true : !!sttHealthCache?.remote;
        sttHealthCache = { available, detail, remote, at: Date.now() };
        if (h) {
          state.sttHealth = available ? 'ready' : 'unavailable';
          state.secureUrl = h.secure_url || '';
        }
        return sttHealthCache;
      })
      .finally(() => { sttHealthPending = null; });
    return sttHealthPending;
  }
  // The cached answer (possibly stale, possibly null); kicks a refresh when stale.
  function sttHealthNow() {
    if (!sttHealthCache || Date.now() - sttHealthCache.at > STT_HEALTH_TTL_MS) refreshSttHealth();
    return sttHealthCache;
  }
  // Resolves to the health answer, or to the cached one after `ms`.
  function awaitSttHealth(ms) {
    return Promise.race([refreshSttHealth(), new Promise(resolve => setTimeout(() => resolve(sttHealthCache), ms))]);
  }
  // The 'local' preference value is the HUB engine: Whisper on the hub, or a
  // speech service the hub's operator configured. Either way the audio goes
  // to the hub, never to the browser vendor, so both read as "Hub".
  function localUnavailableMessage(detail) {
    return /not installed/i.test(detail || '')
      ? "This hub's speech engine isn't installed."
      : `This hub's speech engine isn't working (${detail || 'no reason given'}).`;
  }
  // Which engine a tap should start. Pure, so the decision is testable.
  //   mode            'auto' | 'local' | 'web' (the preference)
  //   hubLocal        true / false / null — /api/stt/health `available`, null = not known yet
  //   detail          the health `detail` text, for the explicit-local message
  //   canRecord       this browser can record for the local engine
  //   canRecognise    this browser has usable SpeechRecognition
  // Returns { engine: 'local' | 'web' | 'none', message, offerBrowser }.
  //
  // Auto prefers local when the hub says it works, and otherwise goes straight
  // to the browser engine in the same tap — including while the health answer
  // is still unknown, because the browser engine cannot lose a recording and
  // waiting for the answer would cost the gesture.
  //
  // An explicit Hub choice ('local') is never switched to the browser engine
  // behind the user's back: Hub is the "my audio never goes to the browser
  // vendor" setting. When it cannot work, say so before recording and offer
  // the browser engine as a button, a choice made for that one recording.
  // A hub that forwards to an operator-configured speech service (health
  // `remote: true`) counts as the hub's engine for both Auto and Hub.
  function chooseDictationEngine({ mode, hubLocal, detail, canRecord, canRecognise }) {
    if (mode === 'web') {
      return canRecognise ? { engine: 'web' }
        : { engine: 'none', message: "This browser has no speech recognition. Set the speech-to-text engine to Auto in Preferences." };
    }
    if (mode === 'local') {
      if (hubLocal === false) return { engine: 'none', message: localUnavailableMessage(detail), offerBrowser: canRecognise };
      if (!canRecord) return { engine: 'none', message: "This browser can't record audio for the hub's speech engine.", offerBrowser: canRecognise };
      return { engine: 'local' };
    }
    if (hubLocal === true && canRecord) return { engine: 'local' };
    if (canRecognise) return { engine: 'web' };
    if (hubLocal === null && canRecord) return { engine: 'local' };
    return { engine: 'none', message: hubLocal === false
      ? localUnavailableMessage(detail) + ' This browser has no speech recognition either; Chrome, Edge or Safari can dictate here.'
      : unavailableReason() };
  }
  function sttMode() {
    const mode = Trio.preferences?.read?.().sttMode;
    return mode === 'local' || mode === 'web' ? mode : 'auto';
  }
  // Writes dictated text into the box. The caret goes to the end only if the
  // box already has focus: focusing it here would raise the phone keyboard
  // over the conversation in the middle of talking. The draft is saved like
  // typed text, so switching conversations no longer discards a dictation.
  function applyDictatedText(text) {
    const el = input(); if (!el) return;
    const focused = document.activeElement === el;
    setValue(text, focused ? text.length : null);
    updateSendState(); saveDraft(); renderTargetHint();
  }
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
    // A pending request owns dictation across navigation until its cleanup
    // settles. Keep that visible even when refresh/unmount stops the mic.
    if (transcribing) {
      active = false; processing = true;
      statusText = sttHealthCache?.remote ? "Transcribing (hub's speech service)…" : 'Transcribing (Whisper on the hub)…';
    }
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
    if (recorder?.state === 'recording') {
      transcribing = true; // stop queues onstop; reserve ownership immediately
      recorder.stop();
    }
    stopTracks(); stopMeter(); document.body.classList.remove('dictating'); setDictationButtonState(false);
  }
  async function browserDictation() {
    if (transcribing) return; // also guard an earlier toast's browser action
    const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Speech) throw new Error('Browser speech recognition is unavailable');
    const myGen = dictationGen; // captured now — stopDictation()/unmount() bump this
    const rec = recognition = new Speech(); recognition.continuous = true; recognition.interimResults = true;
    // Same language as the server-side Whisper path. Left unset, the browser
    // transcribes in ITS OWN locale — so an NTH_STT_LANG=fr deployment would
    // return French from /api/stt/transcribe and English from the browser,
    // decided by nothing but which dictation route the visitor's device took.
    recognition.lang = /*__STT_LANG__*/'en-US';
    // Baseline captured BEFORE start: every event rewrites the box from it
    // rather than appending to the box's own contents (see the accumulator).
    const startedIn = conversationId();
    const absorb = makeSpeechAccumulator(inputValue(), sigilFinalizer(startedIn));
    // A session's events only count while it is the live one, or after the
    // user's Stop (recognition === null), when the last final arrives late.
    // Without the check, a quick Stop-then-start let the OLD session's late
    // onend null out the NEW session and reset the button mid-recording. The
    // Words heard after the user moved to another conversation belong to the
    // one they were spoken in: they go into that thread's draft, and a
    // session still running there is stopped, so the mic is never left
    // listening for a box it can no longer write to.
    recognition.onresult = event => {
      if (recognition !== rec && recognition !== null) return;
      const text = absorb(event.results, event.resultIndex);
      if (conversationId() === startedIn) { applyDictatedText(text); return; }
      state.drafts[startedIn] = text;
      if (recognition === rec) stopDictation();
    };
    // Android Chrome ends a session by itself after a few seconds of silence,
    // continuous=true or not. We let it end: the button goes back to the mic,
    // and the text stays in the box for the user to send or extend with
    // another tap. Restarting automatically was the alternative, but Android
    // plays its system beep on every start, so each pause to think would
    // beep, and a silent phone left recording would beep every few seconds
    // (with a "no speech" error each time) until someone noticed.
    recognition.onend = () => {
      if (recognition !== rec) return;
      recognition = null; stopMeter(); document.body.classList.remove('dictating'); setDictationButtonState(false);
    };
    // Without this handler EVERY failure of this engine was silent: the error
    // event went unhandled, onend fired immediately after and reset the button
    // to idle, and the user was left looking at a mic that had apparently done
    // nothing for no stated reason. onend still does the teardown — this only
    // has to explain what happened, and only for the session it belongs to.
    recognition.onerror = event => {
      if (dictationGen !== myGen) return;
      const message = speechErrorMessage(event?.error);
      if (message) Trio.ui.toast(message, DICTATION_TOAST_MS);
    };
    // LOTC/Aragorn: request the metering stream only once `onstart` confirms
    // SpeechRecognition's OWN mic permission already resolved, instead of
    // firing a second concurrent getUserMedia() request right away — on a
    // first-ever grant that raced two simultaneous browser permission
    // prompts for what looks like one user action.
    recognition.onstart = () => {
      // No meter on Android: there the recogniser runs in a system service
      // that shares the one microphone, and a second capture opened by the
      // page is reported to starve it. The red pulsing button still shows
      // that it is listening.
      if (/Android/i.test(window.navigator.userAgent || '')) return;
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
    // waveform is one signal too many.
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
    const myGen = dictationGen, startedIn = conversationId();
    const finalize = sigilFinalizer(startedIn);
    try {
      stream = await window.navigator.mediaDevices.getUserMedia({ audio: true }); chunks = [];
    } finally { starting = false; }
    // The permission prompt can sit open for a while. If the user stopped or
    // moved to another conversation meanwhile, release the mic and stop here.
    if (dictationGen !== myGen || conversationId() !== startedIn) { stopTracks(); return; }
    recorder = new window.MediaRecorder(stream);
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      transcribing = true;
      setDictationButtonState(false, { processing: true,
        statusText: sttHealthCache?.remote ? "Transcribing (hub's speech service)…" : 'Transcribing (Whisper on the hub)…' });
      try {
        const audio = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
        // Health exposes no server deadline; allow the default 60 s plus
        // upload/response time, then use the existing failure and cleanup path.
        const result = await fetch(apiUrl('/api/stt/transcribe'), { method: 'POST', headers: { 'Content-Type': audio.type || 'audio/webm' }, body: audio, signal: AbortSignal.timeout(75000) });
        const data = await result.json();
        if (!result.ok || !data.ok) throw new Error(data.error || 'transcription failed');
        const text = (data.text || '').trim();
        // Success with nothing in it. The old code appended '' and said
        // nothing at all — you watched "Transcribing…" and then got silence,
        // the same invisible-failure class this whole feature keeps hitting.
        // The server already distinguishes the two causes; use them.
        // (LOTC/Frodo, critical)
        if (!text) {
          Trio.ui.toast(data.no_speech
            ? 'Nothing was picked up — try again and start speaking right after you tap the mic.'
            : 'That was too quiet to transcribe. Move closer to the mic and try again.',
            DICTATION_TOAST_MS);
        } else if (conversationId() === startedIn) {
          applyDictatedText(withBaseline(inputValue(), finalize(text)));
        } else {
          // The user switched threads while this was transcribing. Keep the
          // words with the conversation they were spoken in rather than
          // dropping them, and say where they went.
          state.drafts[startedIn] = withBaseline(state.drafts[startedIn] || '', finalize(text));
          Trio.ui.toast('Your dictation was added to the draft of the conversation you recorded it in.', DICTATION_TOAST_MS);
        }
      } catch (error) {
        // This runs AFTER the user has stopped speaking. The old code
        // responded by starting browserDictation() right here — which
        // discarded the clip they had just recorded and silently opened a
        // fresh live mic, so the honest instruction was "say the whole thing
        // again into a microphone you were not told is on", while the toast
        // implied their existing words were being transcribed. Two separate
        // reasons that could never work: a post-recording start is outside
        // the user gesture Safari requires, and on an insecure origin the
        // engine is refused outright.
        //
        // So: say what broke and OFFER the browser engine as a button on the
        // toast. The first version of this fix set a sticky flag that silently
        // rerouted every later tap — which overrode the visible "Local
        // (Whisper)" preference, fired on transient failures like a 503
        // "busy, try again", and left no way back short of a reload. A setting
        // that reads Local while doing Browser is the same lie this branch
        // exists to delete. An explicit button makes the switch a thing the
        // user chose, once, for this recording only. (LOTC/Frodo, critical)
        const reason = error.name === 'TimeoutError' || error.name === 'AbortError'
          ? 'Hub dictation timed out. Tap the mic and say it again, or use browser dictation.'
          : humanEngineError(error.message) || 'Local transcription failed';
        refreshSttHealth(); // the hub may have lost its engine; let the next tap know
        if (hasBrowserDictation()) {
          Trio.ui.toast(reason, DICTATION_TOAST_MS, offerBrowserAction());
        } else Trio.ui.toast(reason, DICTATION_TOAST_MS);
      } finally {
        transcribing = false;
        stopTracks();
        document.body.classList.remove('dictating');
        setDictationButtonState(false);
      }
    };
    // No statusText while actively recording (see browserDictation) — the
    // "Transcribing (…on the hub)…" text right after IS still useful,
    // since that's invisible processing time the waveform can't represent.
    recorder.start(); document.body.classList.add('dictating'); setDictationButtonState(true);
    startMeter(stream);
  }
  async function toggleDictation() {
    if (transcribing) return; // serialize recordings until the request settles
    // Mid-getUserMedia-await: neither recorder nor stream exists yet, so
    // there's nothing for stopDictation() to stop — just ignore the extra
    // click rather than tearing down a session that hasn't started.
    if (starting) return;
    if (recognition || recorder?.state === 'recording') return stopDictation();
    const mode = sttMode();
    const canRecord = hasLocalDictation(), canRecognise = hasBrowserDictation();
    let health = sttHealthNow();
    // Only an explicit Local choice (which must refuse BEFORE recording when
    // the hub cannot transcribe) or a browser with no speech recognition at
    // all waits for an unknown answer. Everything else decides right now,
    // inside the tap — no await may come before browserDictation() here.
    if (health?.available == null && canRecord && (mode === 'local' || (mode === 'auto' && !canRecognise))) {
      const myGen = dictationGen, startedIn = conversationId();
      // Up to 3 s: show it, so a second tap meets a visibly busy button.
      starting = true;
      setDictationButtonState(false, { processing: true, statusText: 'Checking the hub…' });
      try { health = await awaitSttHealth(3000); } finally { starting = false; setDictationButtonState(false); }
      if (dictationGen !== myGen || conversationId() !== startedIn) return;
    }
    const choice = chooseDictationEngine({ mode, hubLocal: health?.available ?? null, detail: health?.detail, canRecord, canRecognise });
    if (choice.engine === 'web') return browserDictation();
    if (choice.engine === 'none') {
      Trio.ui.toast(choice.message, DICTATION_TOAST_MS, choice.offerBrowser ? offerBrowserAction() : null);
      return;
    }
    const myGen = dictationGen, startedIn = conversationId();
    try { return await localDictation(); }
    catch (error) {
      if (dictationGen !== myGen || conversationId() !== startedIn) return;
      const reason = humanEngineError(error.message) || 'Local dictation failed';
      if (!canRecognise) throw new Error(reason);
      // Explicit Local: say what broke and offer the browser engine; never
      // switch to it on the user's behalf (see chooseDictationEngine).
      if (mode === 'local') { Trio.ui.toast(reason, DICTATION_TOAST_MS, offerBrowserAction()); return; }
      // Auto: this failure happens BEFORE anything was recorded, so falling
      // through to the browser engine loses no audio.
      Trio.ui.toast(reason + '. Using browser speech recognition instead.', DICTATION_TOAST_MS);
      return browserDictation();
    }
  }
  function offerBrowserAction() {
    return {
      label: 'Use browser dictation',
      onClick: () => browserDictation().catch(fallback => Trio.ui.toast(fallback.message, DICTATION_TOAST_MS)),
    };
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
    const resolvingDm = !!state.dmKey && state.dmRouteResolved === false;
    const ro = !!state.readOnly || resolvingDm;
    text.contentEditable = ro ? 'false' : 'true';
    text.setAttribute('aria-readonly', String(ro));
    text.dataset.placeholder = resolvingDm && state.dmError ? 'Private conversation unavailable.'
      : resolvingDm ? 'Resolving private conversation…'
      : state.readOnly ? 'This conversation is archived.' : 'Message the room…';
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
    // multiple + a broad accept list opens the system picker with photos, camera and files.
    const onAttach = () => { const picker = document.createElement('input'); picker.type = 'file'; picker.multiple = true; picker.accept = UPLOAD_ACCEPT; picker.onchange = () => uploadAll(Array.from(picker.files || [])); picker.click(); };
    attach?.addEventListener('click', onAttach); if (attach) domListeners.push([attach, 'click', onAttach]);
    const onPaste = async (event) => {
      const clip = event.clipboardData || window.clipboardData;
      if (!clip) return;
      const images = [];
      if (clip.files && clip.files.length) {
        for (const f of clip.files) images.push(f);
      } else if (clip.items) {
        for (const it of clip.items) {
          if (it.kind === 'file') {
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
      await uploadAll(images);
    };
    text.addEventListener('paste', onPaste); if (text) domListeners.push([text, 'paste', onPaste]);
    // Drag-drop mirrors paste: the browser would otherwise insert rich dropped
    // HTML straight into the contenteditable (same XSS surface). Intercept it —
    // upload dropped images, insert only the plain-text form of anything else.
    const onDrop = async (event) => {
      const dt = event.dataTransfer; if (!dt) return;
      event.preventDefault();
      const images = [];
      for (const f of (dt.files || [])) images.push(f);
      if (images.length) { await uploadAll(images); return; }
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
      // NOT `disabled`. A disabled button fires no click, and on a phone there
      // is no hover, so `title` never renders either — tapping it did
      // absolutely nothing and said absolutely nothing. That is the very
      // failure this feature's whole history is about, just relocated from the
      // engine to the button. aria-disabled keeps the state announced to
      // assistive tech while leaving the control able to explain itself.
      // (LOTC/Frodo, critical)
      dictateBtn.disabled = false;
      dictateBtn.setAttribute('aria-disabled', String(dictation && !dictationAvailable));
      if (!dictationAvailable) dictateBtn.title = unavailableReason();
    }
    const onDictate = () => {
      // Unavailable: explain, don't no-op. The reason is almost never "this
      // browser" — it is overwhelmingly an http:// address, which the user
      // can actually fix if someone tells them the right one.
      if (!dictationAvailable) { Trio.ui.toast(unavailableReason(), DICTATION_TOAST_MS); return; }
      toggleDictation().catch(error => Trio.ui.toast(error?.message || 'Dictation failed', DICTATION_TOAST_MS));
    };
    if (dictation && dictateBtn) { dictateBtn.addEventListener('click', onDictate); domListeners.push([dictateBtn, 'click', onDictate]); }
    // Ask the hub early so the first tap can already pick the right engine.
    if (dictation && dictationAvailable) refreshSttHealth();
    // Aux (targets/images) loading is driven by loadConversation → refresh(),
    // which runs after channel/dmKey are final; the router hook only needs the
    // text draft + input state (kept as-is to avoid touching existing flows).
    unroute = Trio.router?.on?.(() => { loadDraft(); setInputState(text); });
    loadDraft(); loadComposerAux();
  }
  function unmount() {
    dictationGen++; // cancel starts still awaiting health or mic permission
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
  // speechErrorMessage / hasBrowserDictation are exported for the same reason
  // What a reload would lose (48-app-refresh.js asks before reloading).
  function dictationState() {
    if (transcribing) return 'transcribing';
    return recognition || recorder?.state === 'recording' ? 'recording' : '';
  }
  // dom-harness.js recommends extracting pure helpers: the dictation paths
  // around them need a live MediaRecorder and SpeechRecognition, which the
  // harness deliberately does not fake, but the decisions they encode are the
  // part that regressed and they are testable on their own.
  Trio.composer = { init, mount, unmount, render: renderTargets, refresh, send, conversationId, dictationState, setTargets, insertTarget, targetOrder, toggleTarget, clearTargets, toggleAllTargets, upload, toggleDictation, stopDictation, buildSendPayload, syncReadOnly, setDictationButtonState, speechErrorMessage, hasBrowserDictation, makeSpeechAccumulator, unavailableReason, humanEngineError, chooseDictationEngine, collapseSpeech, sttHealthNow, refreshSttHealth, applySpokenSigils };
})();
