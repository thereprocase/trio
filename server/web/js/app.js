(() => {
  // ── DOM handles ──
  const chatWrap = document.getElementById('chat-wrap');
  const chat = document.getElementById('chat');
  const rosterEl = document.getElementById('r-list');
  const rosterHeading = document.getElementById('r-heading');
  const chanStatsEl = document.getElementById('chanstats');
  const sparkEl = document.getElementById('sparkline');
  const hChannel = document.getElementById('h-channel');
  const hMeta = document.getElementById('h-meta');
  const hConn = document.getElementById('h-conn');
  const input = document.getElementById('input');
  const inputHighlight = document.getElementById('input-highlight');
  const sendBtn = document.getElementById('send-btn');
  const preview = document.getElementById('preview');
  const compEl = document.getElementById('completions');
  const btnMsgNum = document.getElementById('btn-msgnum');
  const filterEl = document.getElementById('filter');
  const filterBanner = document.getElementById('filter-banner');
  const btnCompact = document.getElementById('btn-compact');
  const btnNotify = document.getElementById('btn-notify');
  const btnSound = document.getElementById('btn-sound');
  const fontPicker = document.getElementById('font-picker');
  const jumpBtn = document.getElementById('jump-btn');
  const jumpCount = document.getElementById('jump-count');
  const newBar = document.getElementById('new-bar');
  const targetBar = document.getElementById('target-bar');

  // Message-font picker — persists per-origin via localStorage.
  try {
    const saved = localStorage.getItem('trio.msgFont');
    if (saved) {
      let found = false;
      for (const opt of fontPicker.options) {
        if (opt.value === saved) { fontPicker.value = saved; found = true; break; }
      }
      if (found) document.documentElement.style.setProperty('--msg-font', saved);
    }
  } catch (_) { /* private-mode: ignore */ }
  fontPicker.addEventListener('change', () => {
    const v = fontPicker.value;
    document.documentElement.style.setProperty('--msg-font', v);
    try { localStorage.setItem('trio.msgFont', v); } catch (_) {}
  });

  // Theme picker — persists per-origin via localStorage. Unknown/missing
  // theme falls back to 'midnight' (the base :root palette).
  const themePicker = document.getElementById('theme-picker');
  const WG_FONT = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Helvetica Neue", "Helvetica", "Arial", sans-serif';
  let _savedFontBeforeWG = null;   // stash the user's font choice while WG is active

  function setFontPickerLocked(locked) {
    if (locked) {
      fontPicker.classList.add('wg-locked');
      for (const opt of fontPicker.options) {
        if (opt.value === WG_FONT) { opt.disabled = false; }
        else { opt.disabled = true; }
      }
      fontPicker.value = WG_FONT;
      document.documentElement.style.setProperty('--msg-font', WG_FONT);
    } else {
      fontPicker.classList.remove('wg-locked');
      for (const opt of fontPicker.options) {
        if (opt.value === WG_FONT) { opt.disabled = true; }
        else { opt.disabled = false; }
      }
      // Restore the user's previous font choice
      if (_savedFontBeforeWG) {
        let found = false;
        for (const opt of fontPicker.options) {
          if (opt.value === _savedFontBeforeWG && !opt.disabled) {
            fontPicker.value = _savedFontBeforeWG; found = true; break;
          }
        }
        if (found) document.documentElement.style.setProperty('--msg-font', _savedFontBeforeWG);
        _savedFontBeforeWG = null;
      } else {
        // Fall back to saved font or default
        try {
          const s = localStorage.getItem('trio.msgFont');
          if (s) {
            for (const opt of fontPicker.options) {
              if (opt.value === s && !opt.disabled) {
                fontPicker.value = s;
                document.documentElement.style.setProperty('--msg-font', s);
                break;
              }
            }
          }
        } catch (_) {}
      }
    }
  }

  function applyTheme(v) {
    const prev = document.documentElement.getAttribute('data-theme') || 'midnight';
    const next = v || 'midnight';
    document.documentElement.setAttribute('data-theme', next);
    // Walled Garden font lock: entering or leaving bluebubble
    if (next === 'bluebubble' && prev !== 'bluebubble') {
      _savedFontBeforeWG = fontPicker.value;
      setFontPickerLocked(true);
    } else if (next !== 'bluebubble' && prev === 'bluebubble') {
      setFontPickerLocked(false);
    }
  }
  try {
    const savedTheme = localStorage.getItem('trio.theme');
    if (savedTheme) {
      for (const opt of themePicker.options) {
        if (opt.value === savedTheme) { themePicker.value = savedTheme; break; }
      }
      applyTheme(savedTheme);
    } else {
      applyTheme('midnight');
    }
  } catch (_) { applyTheme('midnight'); }
  themePicker.addEventListener('change', () => {
    applyTheme(themePicker.value);
    try { localStorage.setItem('trio.theme', themePicker.value); } catch (_) {}
  });

  // ── URL params ──
  const URL_PARAMS = new URLSearchParams(location.search);
  const DM_TARGET_ID = URL_PARAMS.get('dm') || '';
  const DM_MODE = !!DM_TARGET_ID;
  // Landing-mode multiplexing: when this page is served at /c/<code>, the
  // server substitutes a "?channel=<code>" query string here so every API
  // call names its channel. Single-channel mode leaves it '' (the server
  // already knows its one channel) — the token below is valid JS as-is.
  const API_QS = /*__API_QS__*/'';

  // ── State ──
  // How recently a real gesture must have happened for a scroll to count as
  // the user's. Covers a smooth-scroll animation started by a real drag.
  const USER_INTENT_MS = 1500;
  let CAN_CULL = false;
  const state = {
    channel: '',
    operator: { id: '', name: '' },
    server_host: '',
    dmTargetId: DM_TARGET_ID,      // empty string → main channel view
    members: new Map(),            // id → member (roster row)
    messages: new Map(),            // id → message
    messageDomById: new Map(),      // id → DOM node (for ack badge updates)
    seenMsgIds: new Set(),
    completion: { visible: false, index: 0, items: [], atPos: -1, sigil: '@' },
    agentStats: new Map(),          // id → {sent, sent_times[], lengths[], lastSnippet,
                                    //        read_latencies[], queue_depth,
                                    //        directed_received, directed_replied, pending_directed[]}
    filter: '',
    compact: false,                 // global compact mode
    expandedMsgs: new Set(),        // ids with per-msg override (toggle-specific)
    expandedMembers: new Set(),     // member ids with expanded stats
    notifyEnabled: false,
    initialLoad: true,              // pin to newest until the history burst settles
    soundEnabled: false,
    chimeVolume: 0.33,
    soundScope: 'all',        // 'mention' | 'all' — chime scope, INDEPENDENT of
                              // notifyScope. Defaults to 'all' to preserve the
                              // historical "chime on any new message" behavior
                              // for operators who already had the chime on.
    notifyScope: 'mention',   // 'mention' | 'all'
    notifyWhen: 'hidden',     // 'hidden' | 'always'
    pendingAttachments: [],   // images uploaded but not yet attached to a send
    sttMode: 'local',         // 'local' (Whisper sidecar) | 'web' (browser SpeechRecognition)
    sttRecording: false,      // mic is actively capturing
    unreadCount: 0,                 // for tab title while hidden
    jumpUnread: 0,                  // messages arrived while user was scrolled up
    lastSeenId: 0,                  // highest msg id the user has caught up to
    userIntentAt: 0,                // timestamp of the last real scroll gesture
                                    // (session-based; drives the unread divider)
    rateBins: new Map(),            // bin_epoch_10s → count
    startedAt: Date.now(),
    originalTitle: 'nth_web',
    // Persistent target selection: set of member_ids that every send is
    // addressed to (prepended as @name mentions). Empty = broadcast.
    selectedTargets: new Set(),
    // Ordered list of target ids as rendered in the bar — index → id,
    // so Alt+1..9 maps to the Nth pill.
    targetOrder: [],
  };
  const PALETTE = ['#62d7ef','#d070d7','#7ede7e','#e5d35e',
                   '#8eb9ff','#ff8470','#9ef0f0','#f79fea'];
  // Must match Python animal_for() in nth_constants.py — don't reorder.
  const ANIMAL_EMOJIS = /*__ANIMAL_EMOJIS__*/;
  const ANIMAL_NAMES  = /*__ANIMAL_NAMES__*/;
  function hash32(id) {
    let h = 0;
    for (const c of (id || '')) h = ((h * 31 + c.charCodeAt(0)) >>> 0);
    return h;
  }
  function colorFor(id) {
    return PALETTE[hash32(id) % PALETTE.length];
  }
  function animalFor(member) {
    // Prefer the server-assigned avatar when present — the server runs
    // a per-channel collision-free assignment (animal_for_channel) so
    // no two current members share an emoji. Fall back to a local hash
    // pick for historical message authors no longer in the roster.
    if (member && member.animal_emoji) {
      return { name: member.animal_name || '', emoji: member.animal_emoji };
    }
    const id = (member && (member.id || member.member_id)) || '';
    const i = hash32(id) % ANIMAL_EMOJIS.length;
    return { name: ANIMAL_NAMES[i], emoji: ANIMAL_EMOJIS[i] };
  }
  // Lookup table: member_id → {name, emoji} from the most recent roster.
  // Used to resolve avatars on messages whose author is still in the
  // channel — the message object itself doesn't carry the avatar.
  const AVATAR_BY_ID = new Map();
  function rememberAvatars(members) {
    AVATAR_BY_ID.clear();
    for (const m of (members || [])) {
      if (m && m.id && m.animal_emoji) {
        AVATAR_BY_ID.set(m.id, { name: m.animal_name || '', emoji: m.animal_emoji });
      }
    }
  }
  function animalForId(id) {
    const cached = AVATAR_BY_ID.get(id);
    if (cached) return cached;
    const i = hash32(id) % ANIMAL_EMOJIS.length;
    return { name: ANIMAL_NAMES[i], emoji: ANIMAL_EMOJIS[i] };
  }
  function initialOf(member) {
    // Kept as a fallback only; UI uses animalFor().
    const n = (member && (member.name || member.id)) || '?';
    return n.trim().charAt(0).toUpperCase() || '?';
  }
  function escapeHtml(s) { return s.replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }

  // A single character-class run + optional :line[:col] — a flat quantifier
  // (no nested `(…+…)+`), so it scans in LINEAR time and can't be driven into
  // catastrophic/quadratic backtracking (ReDoS) by a long slash-free blob.
  // Candidates are then post-filtered: a real path must contain a '/'.
  const FILE_PATH_RUN_RE = /[A-Za-z0-9_.~/-]+(?::\d+(?::\d+)?)?/g;
  const FILE_PATH_MAX_LEN = 4096;
  // Per-path validation cache (path token → exists bool). Shared across every
  // message so re-renders and repeated paths never re-hit the endpoint.
  // Bounded: keys are every distinct path-like token ever seen, including inert
  // look-alikes, on a tab that may live for days. Oldest-out at the cap.
  const FILE_PATH_CACHE_MAX = 5000;
  const filePathCache = new Map();
  function cacheFilePath(token, ok) {
    if (filePathCache.size >= FILE_PATH_CACHE_MAX) {
      const oldest = filePathCache.keys().next();
      if (!oldest.done) filePathCache.delete(oldest.value);
    }
    filePathCache.set(token, ok);
  }
  // Said once per page: without it the feature simply is not there for a viewer
  // the server will not trust, which is indistinguishable from "none of those
  // files exist".
  let _fileLinksNoticeShown = false;
  function noteFileLinksUnavailable() {
    if (_fileLinksNoticeShown) return;
    _fileLinksNoticeShown = true;
    const bar = document.createElement('div');
    bar.className = 'file-links-unavailable';
    bar.setAttribute('role', 'status');
    bar.textContent = 'File paths are not clickable here — reveal-in-Finder is '
                    + 'limited to the machine running the dashboard.';
    if (chat && chat.parentNode) chat.parentNode.insertBefore(bar, chat);
  }

  function detectFilePathCandidates(text) {
    const out = [];
    if (!text) return out;
    FILE_PATH_RUN_RE.lastIndex = 0;
    let m;
    while ((m = FILE_PATH_RUN_RE.exec(text)) !== null) {
      let tok = m[0];
      const start = m.index;
      if (tok.indexOf('/') === -1) continue;               // not path-like (no separator)
      // Require a real FILENAME SEGMENT, not just separators: a candidate must
      // carry at least one name character ([A-Za-z0-9_]). This rejects a BARE
      // '/' (and pure-punctuation runs like '//', './', '-/-') that a slash used
      // as prose punctuation produces — "reload / incognito", "high / low",
      // "#" / "!". Those would otherwise validate against on-disk roots ('/'
      // exists!) and wrongly pick up a folder link. Slash-joined WORDS ('and/or',
      // 'high/medium/low') still pass here but are gated by real existence, so
      // they only link if they genuinely resolve. (Server rejects roots too —
      // defense in depth.)
      if (!/[A-Za-z0-9_]/.test(tok)) continue;
      // Drop a single trailing sentence period ("…/c.py." → "…/c.py"); never a
      // ".." tail. Trailing trim only, so the start offset stays valid.
      tok = tok.replace(/([^.\/])\.$/, '$1');
      if (!tok || tok.length > FILE_PATH_MAX_LEN) continue;
      out.push({ start, end: start + tok.length, token: tok });
    }
    return out;
  }

  // Wrap candidate tokens the caller marks valid (isValid(token) === true) in a
  // .file-link. Skips code/pre/existing links, the @/#/! sigil spans, and
  // already-linkified paths, so we never double-wrap or touch literal code.
  // onClick (optional) is attached to each created link.
  function linkifyValidatedPaths(root, isValid, onClick) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest(
        'code, pre, a, .inline-mention, .inline-ref, .inline-bang, .file-link')) continue;
      if (detectFilePathCandidates(node.nodeValue || '').some(c => isValid(c.token)))
        nodes.push(node);
    }
    for (const node of nodes) {
      const text = node.nodeValue || '';
      const cands = detectFilePathCandidates(text).filter(c => isValid(c.token));
      if (!cands.length) continue;
      const frag = document.createDocumentFragment();
      let cursor = 0;
      for (const c of cands) {
        if (c.start < cursor) continue;   // defensive: skip any overlap
        frag.appendChild(document.createTextNode(text.slice(cursor, c.start)));
        const link = document.createElement('a');
        link.className = 'file-link';
        link.textContent = c.token;
        link.dataset.path = c.token;
        link.setAttribute('role', 'button');
        link.setAttribute('tabindex', '0');
        link.title = 'Reveal in Finder';
        if (typeof onClick === 'function') {
          link.addEventListener('click', (e) => { e.preventDefault(); onClick(c.token, link); });
          link.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(c.token, link); }
          });
        }
        frag.appendChild(link);
        cursor = c.end;
      }
      frag.appendChild(document.createTextNode(text.slice(cursor)));
      node.replaceWith(frag);
    }
  }

  // Brief inline state on a file link after a reveal attempt (no navigation,
  // no modal). Success/failure both auto-revert; failures surface the reason
  // in the tooltip.
  function flashFileLink(link, ok, msg) {
    if (!link || !link.classList) return;
    const cls = ok ? 'file-link-ok' : 'file-link-err';
    link.classList.add(cls);
    // A failure reason written to link.title is unreadable: the pointer is
    // already over the link when you click, so the native tooltip does not
    // re-fire, and touch has no tooltip at all. Show it inline instead, and
    // announce it, so the reason survives long enough to be read.
    if (!ok && msg) {
      const prev = link.parentNode && link.parentNode.querySelector('.file-link-note');
      if (prev) prev.remove();
      const note = document.createElement('span');
      note.className = 'file-link-note';
      note.setAttribute('role', 'status');
      note.textContent = ' — ' + msg;
      if (link.parentNode) link.parentNode.insertBefore(note, link.nextSibling);
      setTimeout(() => { note.remove(); }, 6000);
    }
    setTimeout(() => { link.classList.remove(cls); }, 1500);
  }

  async function revealPath(path, link) {
    if (typeof fetch !== 'function') return;
    try {
      const r = await fetch('/api/reveal', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data && data.ok) flashFileLink(link, true);
      else flashFileLink(link, false, (data && data.error) || ('reveal failed (' + r.status + ')'));
    } catch (e) {
      flashFileLink(link, false, 'reveal failed: ' + e.message);
    }
  }

  // Detect candidate paths in a rendered message body, validate the uncached
  // ones against the server (batched into one request per message), then
  // linkify only those confirmed to exist. Fire-and-forget from paintBody.
  // Relative candidates are resolved by the server against ITS cwd (best
  // effort); if they don't resolve there, they simply stay unlinked.
  // Validation is batched across every body painted in the same tick. A
  // 200-message history burst otherwise fired ~130 separate POSTs (measured
  // 317ms of pure per-request overhead against 1.8ms for the same candidates
  // sent once); the filesystem work was never the cost. Each caller registers
  // its root, one flush resolves every outstanding token, then each root is
  // linkified from the shared cache.
  let _pendingRoots = [];
  let _pendingTokens = new Set();
  let _flushTimer = null;

  async function _flushFilePathValidation() {
    _flushTimer = null;
    const roots = _pendingRoots; _pendingRoots = [];
    const tokens = _pendingTokens; _pendingTokens = new Set();
    const need = [...tokens].filter(t => !filePathCache.has(t));
    for (let i = 0; i < need.length; i += 200) {   // server caps at 200/req
      const chunk = need.slice(i, i + 200);
      try {
        const r = await fetch('/api/path/validate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths: chunk }),
        });
        if (r.ok) {
          const data = await r.json().catch(() => ({}));
          const ex = (data && data.exists) || {};
          for (const t of chunk) cacheFilePath(t, ex[t] === true);
        } else if (r.status === 403) {
          noteFileLinksUnavailable();
          for (const t of chunk) cacheFilePath(t, false);
        }
      } catch (e) { /* leave uncached — just won't linkify this pass */ }
    }
    for (const root of roots) {
      if (!root.isConnected) continue;     // message re-rendered or removed
      linkifyValidatedPaths(root, (t) => filePathCache.get(t) === true, revealPath);
    }
  }

  function decorateFilePaths(root) {
    if (!root || typeof fetch !== 'function') return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let found = false;
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest(
        'code, pre, a, .inline-mention, .inline-ref, .inline-bang, .file-link')) continue;
      for (const c of detectFilePathCandidates(node.nodeValue || '')) {
        _pendingTokens.add(c.token); found = true;
      }
    }
    if (!found) return;
    _pendingRoots.push(root);
    if (_flushTimer === null) _flushTimer = setTimeout(_flushFilePathValidation, 0);
  }

  function renderMarkdown(text) {
    if (!text) return '';
    text = text.replace(/\u0000/g, '');
    // Stash fenced and inline code FIRST so their contents survive every
    // subsequent transform (including line splitting for block parsing).
    const fences = [];
    let src = text.replace(/```(?:([A-Za-z0-9_+-]+))?\n?([\s\S]*?)```/g, (_m, lang, code) => {
      fences.push(code.replace(/\n$/, ''));
      return '\u0000F' + (fences.length - 1) + '\u0000';
    });
    const inlines = [];
    src = src.replace(/`([^`\n]+)`/g, (_m, code) => {
      inlines.push(code);
      return '\u0000I' + (inlines.length - 1) + '\u0000';
    });

    function inlineFmt(t) {
      t = escapeHtml(t);
      t = humanizeIdSigils(t);
      t = t.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/(^|[\s(\[])\*([^*\n]+?)\*(?=[\s.,!?;:)\]]|$)/g, '$1<em>$2</em>');
      t = t.replace(/(^|[\s(\[])_([^_\n]+?)_(?=[\s.,!?;:)\]]|$)/g, '$1<em>$2</em>');
      t = t.replace(/~~([^~\n]+?)~~/g, '<del>$1</del>');
      t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_m, txt, url) => {
        const safeUrl = url.replace(/&(?:quot|#39);/g, '');
        return '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' + txt + '</a>';
      });
      t = t.replace(/(^|[\s(])(https?:\/\/[^\s<]+[^\s<.,;:!?)])/g, (_m, pre, url) => {
        const safeUrl = url.replace(/&(?:quot|#39);/g, '');
        return pre + '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
      });
      return t;
    }

    function splitRow(row) {
      let r = row.trim();
      if (r.startsWith('|')) r = r.slice(1);
      if (r.endsWith('|')) r = r.slice(0, -1);
      return r.split('|').map(c => c.trim());
    }
    function isTableSep(line) {
      return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    }
    function parseAlign(sep) {
      return splitRow(sep).map(c => {
        const left = c.startsWith(':'), right = c.endsWith(':');
        if (left && right) return 'center';
        if (right) return 'right';
        if (left) return 'left';
        return '';
      });
    }

    // A list marker at the start (after stripping leading indent).
    function listMarker(line) {
      const m = line.match(/^(\s*)(-|\*|\+|\d+\.)\s+(.*)$/);
      if (!m) return null;
      const indent = m[1].replace(/\t/g, '    ').length;
      const ordered = /^\d+\./.test(m[2]);
      let content = m[3];
      let task = null;
      const tm = content.match(/^\[( |x|X)\]\s+(.*)$/);
      if (tm) { task = tm[1].toLowerCase() === 'x'; content = tm[2]; }
      return { indent, ordered, content, task };
    }

    // Consume a list beginning at lines[start] with baseline indent.
    // Returns [html, nextIndex]. Nested lists handled by recursion: a line
    // whose indent is > baseline and is itself a list marker becomes a
    // child list attached to the previous <li>.
    function parseList(lines, start) {
      const first = listMarker(lines[start]);
      if (!first) return null;
      const baseIndent = first.indent;
      const ordered = first.ordered;
      const items = [];  // { html, task }
      let i = start;
      while (i < lines.length) {
        const line = lines[i];
        if (!line.trim()) {
          // Blank line: list continues if the next non-blank is still a
          // list item at the same indent. Otherwise break.
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) j++;
          if (j >= lines.length) { i = j; break; }
          const nxt = listMarker(lines[j]);
          if (!nxt || nxt.indent < baseIndent) { i = j; break; }
          i = j; continue;
        }
        const mk = listMarker(line);
        if (mk && mk.indent === baseIndent && mk.ordered === ordered) {
          // Collect continuation lines (indented more, non-list) and
          // child lists (indented more, list marker).
          let body = inlineFmt(mk.content);
          let task = mk.task;
          i++;
          let childHtml = '';
          while (i < lines.length) {
            const ln = lines[i];
            if (!ln.trim()) break;
            const sub = listMarker(ln);
            if (sub && sub.indent > baseIndent) {
              const [h, ni] = parseList(lines, i);
              childHtml += h;
              i = ni;
              continue;
            }
            if (sub && sub.indent <= baseIndent) break;
            // Lazy continuation — appended as soft-wrapped text.
            body += '\n' + inlineFmt(ln.trim());
            i++;
          }
          items.push({ body: body.replace(/\n/g, '<br>') + childHtml, task });
        } else if (mk && mk.indent < baseIndent) {
          break;
        } else if (!mk) {
          break;
        } else {
          // Different list type (ordered vs unordered) or deeper start —
          // terminate this list so the caller can start a new one.
          break;
        }
      }
      const tag = ordered ? 'ol' : 'ul';
      let html = '<' + tag + '>';
      for (const it of items) {
        if (it.task === null || it.task === undefined) {
          html += '<li>' + it.body + '</li>';
        } else {
          const checked = it.task ? ' checked' : '';
          html += '<li class="task"><input type="checkbox" disabled' + checked + '>' +
                  it.body + '</li>';
        }
      }
      html += '</' + tag + '>';
      return [html, i];
    }

    const lines = src.split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      // Skip blank lines between blocks.
      if (!line.trim()) { i++; continue; }

      // Thematic break.
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
        out.push('<hr>'); i++; continue;
      }

      // ATX heading.
      const h = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (h) {
        const lvl = h[1].length;
        out.push('<h' + lvl + '>' + inlineFmt(h[2]) + '</h' + lvl + '>');
        i++; continue;
      }

      // Blockquote — collect consecutive `>` lines, recurse on dequoted body.
      if (/^\s{0,3}>\s?/.test(line)) {
        const block = [];
        while (i < lines.length && /^\s{0,3}>\s?/.test(lines[i])) {
          block.push(lines[i].replace(/^\s{0,3}>\s?/, ''));
          i++;
        }
        out.push('<blockquote>' + renderMarkdown(block.join('\n')) + '</blockquote>');
        continue;
      }

      // GFM table — require a pipe in the first line AND a separator on the next.
      if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const header = splitRow(line);
        const align = parseAlign(lines[i + 1]);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        let t = '<table><thead><tr>';
        header.forEach((cell, j) => {
          const a = align[j] ? ' style="text-align:' + align[j] + '"' : '';
          t += '<th' + a + '>' + inlineFmt(cell) + '</th>';
        });
        t += '</tr></thead><tbody>';
        rows.forEach(r => {
          t += '<tr>';
          for (let j = 0; j < header.length; j++) {
            const a = align[j] ? ' style="text-align:' + align[j] + '"' : '';
            t += '<td' + a + '>' + inlineFmt(r[j] || '') + '</td>';
          }
          t += '</tr>';
        });
        t += '</tbody></table>';
        out.push(t);
        continue;
      }

      // List (ul / ol).
      if (listMarker(line)) {
        const parsed = parseList(lines, i);
        if (parsed) { out.push(parsed[0]); i = parsed[1]; continue; }
      }

      // Fenced-code sentinel — emit directly to prevent <p><pre> nesting.
      if (/^\u0000F\d+\u0000$/.test(line.trim())) {
        out.push(line.trim()); i++; continue;
      }

      // Paragraph — consume until a block boundary.
      const p = [];
      while (i < lines.length) {
        const ln = lines[i];
        if (!ln.trim()) break;
        if (/^\u0000F\d+\u0000$/.test(ln)) break;
        if (/^\s{0,3}(#{1,6})\s+/.test(ln)) break;
        if (/^\s{0,3}>\s?/.test(ln)) break;
        if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(ln)) break;
        if (listMarker(ln)) break;
        if (ln.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) break;
        p.push(ln);
        i++;
      }
      out.push('<p>' + p.map(inlineFmt).join('<br>') + '</p>');
    }

    let html = out.join('');
    html = html.replace(/\u0000I(\d+)\u0000/g, (_m, k) =>
      '<code class="mdic">' + escapeHtml(inlines[+k]) + '</code>');
    html = html.replace(/\u0000F(\d+)\u0000/g, (_m, k) =>
      '<pre class="mdcode">' + escapeHtml(fences[+k]) + '</pre>');
    return html;
  }

  // ── Time ──
  function formatTime(iso) {
    if (!iso) return '--:--';
    try {
      const d = new Date(iso);
      return d.toTimeString().slice(0, 8);
    } catch (e) { return '--:--'; }
  }
  function fmtRel(seconds) {
    if (seconds == null || !isFinite(seconds)) return '—';
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  }

  const SYSTEM_WORDS = new Set(['claimed', 'done', 'cancelled', 'released',
    'retracted', 'joined', 'left', 'ended', 'locked', 'unlocked', 'status',
    'pinned', 'renamed', 'culled']);
  // System notices come in two shapes: "[word #id] ..." (the task family) and
  // "[word] ..." (join/pin/lock/unlock/rename). A plain startsWith('[word ')
  // only ever matched the first, so the second rendered as ordinary markdown.
  // Requiring a space-or-end after the "]" keeps a markdown link such as
  // [done](url) from being muted as a system notice.
  function isSystemContent(s) {
    const m = /^\[([a-z]+)(?:\s|\](?:\s|$))/.exec(s || '');
    return !!m && SYSTEM_WORDS.has(m[1]);
  }

  // Rewrite @<member_id> / #<member_id> / !<member_id> to @<friendly-name>
  // in message bodies before rendering. The raw id-sigil form is valid
  // input (the server-side parser routes it correctly) but ugly to read;
  // agents can address-by-id for rename resilience and the UI translates
  // back to the current display name on the fly. Unknown ids are left
  // alone so stale history isn't mangled.
  function humanizeIdSigils(text) {
    if (!text) return text;
    if (!state.members || !state.members.size) return text;
    // Build a single alternation across all known ids, longest first so
    // "_op_g_bob_abcdef" beats a hypothetical prefix "_op_g_bob".
    const ids = Array.from(state.members.keys())
      .filter(Boolean)
      .sort((a, b) => b.length - a.length)
      .map(id => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!ids.length) return text;
    const re = new RegExp('([@#!])(' + ids.join('|') + ')(?=\\b|$)', 'g');
    return text.replace(re, (match, sigil, id) => {
      const mem = state.members.get(id);
      const name = mem && mem.name ? escapeHtml(mem.name) : id;
      return sigil + name;
    });
  }

  function mentionMemberForToken(token, allowedIds) {
    const lower = (token || '').toLowerCase();
    if (lower === 'all') return { id: 'all', name: 'all' };
    for (const mem of state.members.values()) {
      if (allowedIds && !allowedIds.has(mem.id)) continue;
      if ((mem.id || '').toLowerCase() === lower ||
          (mem.name || '').toLowerCase() === lower) return mem;
    }
    return null;
  }

  // Find only syntactically complete, roster-resolved @mentions. Unknown
  // @words stay unadorned, which doubles as feedback that they will not ping
  // a participant.
  function collectMentionMatches(text, allowedIds) {
    const matches = [];
    const re = /(^|[^A-Za-z0-9_])@([A-Za-z0-9_.-]+)/g;
    let hit;
    while ((hit = re.exec(text || ''))) {
      // The token class greedily swallows trailing sentence punctuation
      // (".", "-") — e.g. "thanks @Claude." captures "Claude.". Resolve the
      // full token first (so names that legitimately contain "."/"-" like
      // jen.chen / gabe-guest still match), then trim trailing "."/"-" and
      // retry so the mention still highlights, matching the server's routing.
      let token = hit[2];
      let member = mentionMemberForToken(token, allowedIds);
      while (!member && (token.endsWith('.') || token.endsWith('-'))) {
        token = token.slice(0, -1);
        member = mentionMemberForToken(token, allowedIds);
      }
      if (!member) continue;
      const start = hit.index + hit[1].length;
      matches.push({ start, end: start + token.length + 1, member });
    }
    return matches;
  }

  function decorateInlineMentions(root, mentionIds) {
    if (!root || !mentionIds || !mentionIds.length) return;
    const allowed = new Set(mentionIds);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest('code, pre, a, .inline-mention')) continue;
      if (collectMentionMatches(node.nodeValue || '', allowed).length) nodes.push(node);
    }
    for (const node of nodes) {
      const text = node.nodeValue || '';
      const matches = collectMentionMatches(text, allowed);
      if (!matches.length) continue;
      const frag = document.createDocumentFragment();
      let cursor = 0;
      for (const match of matches) {
        frag.appendChild(document.createTextNode(text.slice(cursor, match.start)));
        const span = document.createElement('span');
        span.className = 'inline-mention';
        span.textContent = text.slice(match.start, match.end);
        span.dataset.memberId = match.member.id;
        span.title = match.member.id === 'all'
          ? 'Mentions every participant'
          : 'Mentions ' + (match.member.name || match.member.id);
        if (match.member.id !== 'all') {
          span.style.setProperty('--mention-member-color', colorFor(match.member.id));
        }
        frag.appendChild(span);
        cursor = match.end;
      }
      frag.appendChild(document.createTextNode(text.slice(cursor)));
      node.replaceWith(frag);
    }
  }

  // Pure: draft text -> mirror HTML. Split out from the DOM write so the
  // escaping can actually be tested — this is the one path that builds markup
  // from raw user input, so a missed escape here is exploitable by typing.
  function composerMentionHtml(text) {
    text = text || '';
    const matches = collectMentionMatches(text, null);
    let html = '';
    let cursor = 0;
    for (const match of matches) {
      html += escapeHtml(text.slice(cursor, match.start));
      // colorFor returns a fixed palette hex (injection-safe); @all has no
      // per-member color and falls back to the rainbow shimmer via its own class.
      const isAll = match.member.id === 'all';
      const mc = isAll ? '' : colorFor(match.member.id);
      const styleAttr = mc ? ' style="--mention-member-color:' + mc + '"' : '';
      const cls = isAll ? 'composer-mention composer-mention-all' : 'composer-mention';
      html += '<span class="' + cls + '"' + styleAttr + '>' +
              escapeHtml(text.slice(match.start, match.end)) + '</span>';
      cursor = match.end;
    }
    html += escapeHtml(text.slice(cursor));
    // Preserve a final blank line so the mirror stays aligned with textarea
    // scrollHeight and wrapping behavior.
    return html + (text.endsWith('\n') ? '\n ' : '');
  }

  function renderComposerMentionHighlights() {
    if (!inputHighlight) return;
    inputHighlight.innerHTML = composerMentionHtml(input.value || '');
    inputHighlight.scrollTop = input.scrollTop;
    inputHighlight.scrollLeft = input.scrollLeft;
  }

  // ── Per-member agent stats (client-side aggregate, derived from event stream) ──
  function agentState(id) {
    if (!state.agentStats.has(id)) {
      state.agentStats.set(id, {
        sent: 0, sent_times: [], lengths: [], lastSnippet: '',
        read_latencies: [], queue_depth: 0,
        directed_received: 0, directed_replied: 0, pending_directed: [],
        last_read_seen: 0,    // last snapshot of this member's DB last_read value
      });
    }
    return state.agentStats.get(id);
  }

  function ingestMessageForStats(msg) {
    const s = agentState(msg.member_id);
    s.sent++;
    s.sent_times.push(new Date(msg.created_at).getTime() || Date.now());
    if (s.sent_times.length > 500) s.sent_times.shift();
    s.lengths.push((msg.content || '').length);
    if (s.lengths.length > 20) s.lengths.shift();
    s.lastSnippet = (msg.content || '').slice(0, 100);

    // @-reply accounting: if sender had pending directed messages to reply to,
    // count this send as a reply to all of them (first-response-counts).
    while (s.pending_directed.length > 0) {
      s.pending_directed.shift();
      s.directed_replied++;
    }

    // For every other member, this new message either bumps their queue
    // (if their last_read < msg.id) or is for a mentioned recipient.
    for (const [mid, mem] of state.members) {
      if (mid === msg.member_id) continue;
      if ((mem.last_read || 0) < msg.id) {
        const ms = agentState(mid);
        ms.queue_depth++;
      }
      if ((msg.mentions || []).includes(mid)) {
        const ms = agentState(mid);
        ms.directed_received++;
        ms.pending_directed.push(msg.id);
      }
    }

    // Global activity rate bins (10-second granularity)
    const bin = Math.floor((new Date(msg.created_at).getTime() || Date.now()) / 10000) * 10000;
    state.rateBins.set(bin, (state.rateBins.get(bin) || 0) + 1);
  }

  function applyRosterWatermarkDeltas(newMembers) {
    const now = Date.now();
    for (const m of newMembers) {
      const prev = state.members.get(m.id);
      const prevLR = prev ? (prev.last_read || 0) : 0;
      const newLR = m.last_read || 0;
      if (newLR > prevLR) {
        const s = agentState(m.id);
        // Credit read-latencies for messages in (prevLR, newLR]
        for (const [msgId, msg] of state.messages) {
          if (msgId > prevLR && msgId <= newLR && msg.member_id !== m.id) {
            const sent = new Date(msg.created_at).getTime();
            if (sent) {
              s.read_latencies.push((now - sent) / 1000);
              if (s.read_latencies.length > 20) s.read_latencies.shift();
            }
            // Decrement their queue — they've now read this one.
            s.queue_depth = Math.max(0, s.queue_depth - 1);
          }
        }
        s.last_read_seen = newLR;
      }
    }
  }

  function agentSendRatePerHour(id) {
    const s = state.agentStats.get(id);
    if (!s) return 0;
    const cutoff = Date.now() - 3600 * 1000;
    return s.sent_times.filter(t => t >= cutoff).length;
  }
  function agentAvgReadLatency(id) {
    const s = state.agentStats.get(id);
    if (!s || s.read_latencies.length === 0) return null;
    return s.read_latencies.reduce((a, b) => a + b, 0) / s.read_latencies.length;
  }
  function agentAvgLen(id) {
    const s = state.agentStats.get(id);
    if (!s || s.lengths.length === 0) return null;
    return s.lengths.reduce((a, b) => a + b, 0) / s.lengths.length;
  }
  function agentReplyRate(id) {
    const s = state.agentStats.get(id);
    if (!s || s.directed_received === 0) return null;
    return s.directed_replied / s.directed_received;
  }

  // ── Ack badges per message ──
  function updateAckBadges(msgId) {
    const dom = state.messageDomById.get(msgId);
    if (!dom) return;
    const box = dom.querySelector('.acks');
    if (!box) return;
    box.innerHTML = '';
    const msg = state.messages.get(msgId);
    if (!msg) return;
    // One badge per NON-operator, NON-sender member. Sender doesn't need to
    // ack their own message; operator is already us.
    for (const [mid, mem] of state.members) {
      if (mid === state.operator.id) continue;
      if (mid === msg.member_id) continue;
      const read = (mem.last_read || 0) >= msgId;
      const { name: animalName, emoji } = animalFor(mem);
      const badge = document.createElement('span');
      badge.className = 'ack-badge ' + (read ? 'read' : 'pending');
      badge.textContent = emoji;
      badge.style.borderColor = colorFor(mid);
      badge.title = `${mem.name} (${mid}) — the ${animalName} — ${read ? 'read ✓' : 'pending…'}  · last_read: ${mem.last_read}  (click to open DM tab)`;
      badge.onclick = (e) => {
        e.stopPropagation();
        if (!DM_MODE) window.open('/?dm=' + encodeURIComponent(mid), '_blank');
      };
      box.appendChild(badge);
    }
  }

  function updateAllAckBadges() {
    for (const id of state.messageDomById.keys()) updateAckBadges(id);
  }

  // Build a sigil-bar (@mentions or #refs) for a message — factored so
  // both visual styles use identical markup and differ only in class +
  // label + sigil.
  function renderTargetBar(ids, className, sigil, label) {
    const bar = document.createElement('div');
    bar.className = className;
    const lab = document.createElement('span');
    lab.className = 'to-label';
    lab.textContent = label;
    bar.appendChild(lab);
    for (const id of ids) {
      const mem = state.members.get(id);
      const nm = mem ? mem.name : id;
      const anim = animalFor(mem || { id });
      const chip = document.createElement('span');
      chip.className = 'mchip';
      const a = document.createElement('span');
      a.className = 'manimal';
      a.textContent = anim.emoji;
      chip.appendChild(a);
      chip.appendChild(document.createTextNode(sigil + nm));
      bar.appendChild(chip);
    }
    return bar;
  }

  // ── Message rendering ──
  function applyCompactClass(node, id) {
    const override = state.expandedMsgs.has(id);
    if (state.compact && !override) node.classList.add('compact');
    else node.classList.remove('compact');
  }

  // After the initial history burst goes quiet, snap once more to the bottom
  // (markdown/fonts reflow taller after the synchronous appends) and switch to
  // normal "follow only if near bottom" behavior for live messages.
  let _initialSettleTimer = null;
  let _initialSettleDeadline = 0;
  function settleInitialLoad() {
    _initialSettleTimer = null;
    _initialSettleDeadline = 0;
    state.initialLoad = false;
    // seedBaseline + disownScroll come from the unread-divider work: the
    // baseline must be taken once the history burst has settled, and the
    // programmatic scroll below must NOT count as user intent — otherwise
    // opening a channel marks everything read before the reader has seen it.
    seedBaseline();
    requestAnimationFrame(() => { disownScroll(); chat.scrollTop = chat.scrollHeight; });
  }
  function scheduleInitialSettle() {
    // The quiet gap is rescheduled on each append, so a burst spaced under
    // 250ms would hold initialLoad open for its whole duration — and the chime
    // is gated on that flag, so it would be muted exactly during an agent
    // flurry. Cap the total wait so a dense burst still settles.
    const now = Date.now();
    if (!_initialSettleDeadline) _initialSettleDeadline = now + 3000;
    if (_initialSettleTimer) clearTimeout(_initialSettleTimer);
    // Both sides changed this scheduler. Kept: the renderer's CAPPED wait (a
    // dense burst must still settle, or the chime stays muted through an agent
    // flurry) driving the unread work's settle body, which now lives in
    // settleInitialLoad() above. Taking either side alone would have silently
    // dropped the other's fix.
    const wait = Math.max(0, Math.min(250, _initialSettleDeadline - now));
    _initialSettleTimer = setTimeout(settleInitialLoad, wait);
  }

  function appendMessage(m) {
    if (state.seenMsgIds.has(m.id)) return;
    state.seenMsgIds.add(m.id);
    state.messages.set(m.id, m);
    ingestMessageForStats(m);

    const isMine = m.member_id === state.operator.id;
    const isSystem = isSystemContent(m.content || '');
    const mentionsOperator = (m.mentions || []).includes(state.operator.id);
    // '!' sigils land in a separate `bangs` column, never in `mentions`.
    // A bang is the last-resort signal an agent cannot be opted out of, so
    // it must reach a mention-scoped chime too — otherwise the one message
    // that paints a red BANG bar is the one message that makes no sound.
    const bangsOperator = (m.bangs || []).includes(state.operator.id);

    const div = document.createElement('div');
    div.className = 'msg' + (isMine ? ' mine' : '') + (isSystem ? ' system' : '')
                  + (mentionsOperator ? ' targeted' : '');
    div.dataset.msgId = String(m.id);
    div.dataset.sender = m.member_id || '';
    div.dataset.search = (m.content || '').toLowerCase() + ' '
                       + humanizeIdSigils(m.content || '').toLowerCase() + ' '
                       + (m.member_name || '').toLowerCase();

    // Message-number gutter (#N) — visible only when #chat.show-msg-nums.
    // Absolute + full-height so it centres on the whole message; the inner
    // span is position:sticky (see CSS) so the number rides the visible slice.
    const numGutter = document.createElement('div');
    numGutter.className = 'msg-num-gutter';
    // No ARIA here on purpose. The visible "#N" is real text inside the
    // message's own subtree, ahead of the timestamp in DOM order, so a screen
    // reader already reads the number then the message — the same order a
    // sighted reader gets. A role/aria-label would duplicate that text and add
    // one region boundary per message; aria-hidden would take it away entirely.
    const numEl = document.createElement('span');
    numEl.className = 'msg-num';
    numEl.textContent = '#' + m.id;
    numEl.title = 'message ' + m.id;
    // The number is selectable/copyable; don't let a click on it also toggle
    // the message's compact/expand state.
    numEl.addEventListener('click', (e) => e.stopPropagation());
    numGutter.appendChild(numEl);
    div.appendChild(numGutter);

    const head = document.createElement('div');
    head.className = 'head';
    const timeSpan = document.createElement('span');
    timeSpan.className = 'time';
    timeSpan.textContent = formatTime(m.created_at);
    timeSpan.title = m.created_at || '';
    head.appendChild(timeSpan);
    if (!isSystem) {
      const author = document.createElement('span');
      author.className = 'author';
      author.textContent = m.member_name;
      author.style.color = colorFor(m.member_id);
      head.appendChild(author);
    }
    const acks = document.createElement('span');
    acks.className = 'acks';
    head.appendChild(acks);
    div.appendChild(head);

    // !bangs bar FIRST — unfilterable, loudest visual signal.
    if (!isSystem && m.bangs && m.bangs.length) {
      div.appendChild(renderTargetBar(m.bangs, 'bangs-bar', '!', 'BANG'));
    }
    // @mentions bar (pings) — always rendered above body so auto-@ isn't missed.
    if (!isSystem && m.mentions && m.mentions.length) {
      div.appendChild(renderTargetBar(m.mentions, 'mentions-bar', '@', '→'));
    }
    // #pound refs bar (talked about, not pinged). Softer visual.
    if (!isSystem && m.refs && m.refs.length) {
      div.appendChild(renderTargetBar(m.refs, 'refs-bar', '#', 'about'));
    }

    const body = document.createElement('div');
    body.className = 'body';
    if (isSystem) {
      body.classList.add('plain');
      body.textContent = humanizeIdSigils(m.content || '');
    } else {
      body.innerHTML = renderMarkdown(m.content || '');
      decorateInlineMentions(body, m.mentions || []);
      // Async: validate path-like tokens with the server and linkify the real
      // ones (reveal-in-Finder). Fire-and-forget so paint stays synchronous.
      decorateFilePaths(body);
    }
    div.appendChild(body);

    // Image attachments — inline thumbnails, click opens full size in a new tab.
    if (m.attachments && m.attachments.length) {
      const wrap = document.createElement('div');
      wrap.className = 'msg-attachments';
      for (const att of m.attachments) {
        // API_QS carries ?channel=<code> in landing mode; without it the
        // server cannot tell which channel's attachment is being asked for.
        const url = '/api/attachment/' + att.id + API_QS;
        const a = document.createElement('a');
        a.href = url; a.target = '_blank'; a.rel = 'noopener';
        const img = document.createElement('img');
        img.className = 'msg-img';
        img.src = url;
        img.alt = att.filename || 'image';
        img.loading = 'lazy';
        // Late-loading images reflow taller; keep us pinned if near bottom.
        img.addEventListener('load', () => {
          const nb = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 120;
          if (state.initialLoad || nb) chat.scrollTop = chat.scrollHeight;
        });
        // A failing image otherwise collapses to a bare broken-image glyph:
        // the viewer cannot tell whether it was deleted, whether they are not
        // allowed to see it (the read endpoint requires a resolved identity),
        // or whether the network hiccupped.
        img.addEventListener('error', () => {
          const note = document.createElement('span');
          note.className = 'msg-img-missing';
          note.textContent = '🖼 image unavailable — ' + (att.filename || 'attachment');
          note.title = 'It may have been removed, or you may not have access '
                     + 'to attachments on this machine.';
          if (a.parentNode) a.parentNode.replaceChild(note, a);
        });
        // Opening the image should not also toggle the message's compact state.
        a.addEventListener('click', (e) => { e.stopPropagation(); });
        a.appendChild(img);
        wrap.appendChild(a);
      }
      div.appendChild(wrap);
    }

    // Watermark pins — animals of agents whose last_read == this message id.
    const pins = document.createElement('div');
    pins.className = 'watermark-pins';
    div.appendChild(pins);

    // Toggle expand/compact on click
    div.addEventListener('click', (e) => {
      if (e.target.closest('.ack-badge')) return;
      if (state.expandedMsgs.has(m.id)) state.expandedMsgs.delete(m.id);
      else state.expandedMsgs.add(m.id);
      applyCompactClass(div, m.id);
    });

    applyCompactClass(div, m.id);
    applyFilterToNode(div);
    applyDmFilterToNode(div, m);

    // Mark sender-change boundaries for bluebubble inter-bubble spacing
    const prevMsg = chat.lastElementChild;
    if (prevMsg && prevMsg.dataset.sender !== div.dataset.sender) {
      div.classList.add('sender-break');
    }
    const nearBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80;
    chat.appendChild(div);
    state.messageDomById.set(m.id, div);
    updateAckBadges(m.id);
    renderWatermarkPins();
    scheduleHereUpdate();

    if (state.initialLoad) {
      // Fresh page load: keep pinned to the newest message through the whole
      // history burst, then do one final settle after layout reflows.
      chat.scrollTop = chat.scrollHeight;
      scheduleInitialSettle();
    } else if (nearBottom && !document.hidden) {
      // Only auto-pin to the bottom when the tab is VISIBLE. Pinning while
      // hidden would leave us at the bottom on return, so the "new messages"
      // divider for what arrived while away would be marked caught-up and lost.
      chat.scrollTop = chat.scrollHeight;
    } else {
      // Same rule as the divider: your own message is not something you have
      // yet to read. Without this, sending while scrolled up raises the
      // jump-to-latest badge as well as the divider — two separate claims that
      // there is something new, both of them about you.
      if (!isMine) state.jumpUnread++;
      updateJumpButton();
    }

    // Unread divider: if the user is keeping up (tab visible + at/near bottom),
    // they've seen this message; otherwise it's unread since they looked away or
    // scrolled up, and a "new messages" divider is drawn before the first such.
    if (state.initialLoad) {
      // History burst. The baseline is set once in seedBaseline() when the
      // burst settles; advancing per-message here would race a hidden tab.
    } else if (!document.hidden && nearBottom && !isHiddenMsg(div)) {
      // Only messages the user can actually see count as read on arrival, and
      // the advance has to be the same ascending walk markCaughtUp does — a
      // bare Math.max would jump the watermark over earlier messages a filter
      // is hiding, which is the very thing that walk exists to prevent. One
      // function owns the invariant.
      markCaughtUp();
    } else {
      refreshUnreadDivider();
    }

    // Tab-title badge when hidden
    if (document.hidden && !isMine) {
      state.unreadCount++;
      updateTitle();
    }

    // Desktop notification on @you while hidden (opt-in). In DM mode,
    // only fire for the DM target — don't pull focus for other channel chatter.
    const dmOk = (!state.dmTargetId || m.member_id === state.dmTargetId);
    const scopeOk = state.notifyScope === 'all'
      ? (!isMine && !isSystem)
      : (!isMine && mentionsOperator);
    const whenOk = state.notifyWhen === 'always' ? true : document.hidden;
    if (state.notifyEnabled && whenOk && scopeOk && dmOk &&
        'Notification' in window && Notification.permission === 'granted') {
      try {
        const n = new Notification(`@${state.operator.name} — ${m.member_name}`, {
          body: humanizeIdSigils(m.content || '').slice(0, 140),
          tag: 'trio-' + m.id,
          silent: false,
        });
        n.onclick = () => { window.focus(); n.close(); };
      } catch (e) { /* ignore */ }
    }

    // In-page chime for a new peer message (opt-in, focus-agnostic). The scope
    // (soundScope) is kept independent of the desktop-notify scope, so a quiet
    // chime on all messages can coexist with a popup only on @mentions, or vice
    // versa. Reuses the same mentionsOperator predicate the notify block uses.
    // Skip the primed-history burst on load/reconnect — chime only for LIVE
    // messages once state.initialLoad has settled. Without this, a refresh plays
    // every historical chime at once (overlapping waveforms = loud + phasey).
    // In a DM view every channel message is still appended and merely
    // CSS-hidden, so without this the operator hears a chime for a message
    // they cannot see — an audible event with no visible cause.
    if (shouldChime({
          initialLoad: state.initialLoad, soundEnabled: state.soundEnabled,
          isMine, isSystem,
          dmVisible: (!state.dmTargetId || isRelevantInDm(m)),
          scope: state.soundScope,
          addressed: mentionsOperator || bangsOperator,
        })) playChime();
  }

  // Existing message names may change (rename) — update author labels + mention
  // resolutions in-place so backscroll stays readable.
  function refreshMessageAuthors() {
    for (const [id, m] of state.messages) {
      const dom = state.messageDomById.get(id);
      if (!dom) continue;
      const author = dom.querySelector('.author');
      if (author && !isSystemContent(m.content || '')) {
        author.textContent = m.member_name;
        author.style.color = colorFor(m.member_id);
      }
      // Re-humanize id-sigils in the body: a rename changes the display
      // form, and any unknown ids that have since joined the roster
      // should now resolve.
      const body = dom.querySelector('.body');
      if (body) {
        if (isSystemContent(m.content || '')) {
          body.classList.add('plain');
          body.textContent = humanizeIdSigils(m.content || '');
        } else {
          body.classList.remove('plain');
          body.innerHTML = renderMarkdown(m.content || '');
          decorateInlineMentions(body, m.mentions || []);
          decorateFilePaths(body);
        }
      }
      function rebuildBar(bar, ids, sigil) {
        if (!bar || !ids || !ids.length) return;
        while (bar.childNodes.length > 1) bar.removeChild(bar.lastChild);
        for (const mid of ids) {
          const mem = state.members.get(mid);
          const nm = mem ? mem.name : mid;
          const anim = animalFor(mem || { id: mid });
          const chip = document.createElement('span');
          chip.className = 'mchip';
          const a = document.createElement('span');
          a.className = 'manimal';
          a.textContent = anim.emoji;
          chip.appendChild(a);
          chip.appendChild(document.createTextNode(sigil + nm));
          bar.appendChild(chip);
        }
      }
      rebuildBar(dom.querySelector('.bangs-bar'),    m.bangs,    '!');
      rebuildBar(dom.querySelector('.mentions-bar'), m.mentions, '@');
      rebuildBar(dom.querySelector('.refs-bar'),     m.refs,     '#');
    }
  }

  // ── Roster rendering ──
  // ── Persistent target selector (horizontal bar above the chat box) ──
  // Treat any roster row that isn't this operator and isn't another web
  // operator (_op_*) as a "claude" eligible for targeting.
  function isTargetable(m) {
    if (!m || !m.id) return false;
    if (m.id === state.operator.id) return false;
    if (m.id.startsWith('_op_')) return false;
    return true;
  }
  function targetStorageKey() {
    return 'trio_targets_' + (state.channel || '_');
  }
  function loadPersistedTargets() {
    try {
      const raw = localStorage.getItem(targetStorageKey());
      if (!raw) return;
      const ids = JSON.parse(raw);
      if (Array.isArray(ids)) {
        state.selectedTargets = new Set(ids.filter(x => typeof x === 'string'));
      }
    } catch (_) { /* ignore */ }
  }
  function savePersistedTargets() {
    try {
      localStorage.setItem(targetStorageKey(),
        JSON.stringify([...state.selectedTargets]));
    } catch (_) { /* ignore */ }
  }
  function toggleTarget(id) {
    if (state.selectedTargets.has(id)) state.selectedTargets.delete(id);
    else state.selectedTargets.add(id);
    savePersistedTargets();
    renderComposerTargets();
    updatePreview();
  }
  function toggleAllTargets() {
    const all = state.targetOrder;
    if (all.length === 0) return;
    const allSelected = all.every(id => state.selectedTargets.has(id));
    if (allSelected) state.selectedTargets.clear();
    else for (const id of all) state.selectedTargets.add(id);
    savePersistedTargets();
    renderComposerTargets();
    updatePreview();
  }
  function renderComposerTargets() {
    if (!targetBar) return;
    targetBar.innerHTML = '';
    // Build the ordered list of targetable members. Sort by active-first
    // then name so the numbering is stable-ish across renders.
    const order = { working: 0, active: 1, idle: 2, stale: 3, dead: 4 };
    const targetables = [...state.members.values()]
      .filter(isTargetable)
      .sort((a, b) => {
        const oa = order[a.status] ?? 4;
        const ob = order[b.status] ?? 4;
        if (oa !== ob) return oa - ob;
        return (a.name || '').localeCompare(b.name || '');
      });
    state.targetOrder = targetables.map(m => m.id);
    // Drop stale selections for members who left the channel. Skip pruning
    // before the first roster snapshot arrives — the Map is empty then and
    // we'd clobber a restored-from-localStorage selection.
    if (state.members.size > 0) {
      let mutated = false;
      for (const id of [...state.selectedTargets]) {
        if (!state.members.has(id) || !isTargetable(state.members.get(id))) {
          state.selectedTargets.delete(id);
          mutated = true;
        }
      }
      if (mutated) savePersistedTargets();
    }

    if (targetables.length === 0) {
      const lbl = document.createElement('span');
      lbl.className = 'tb-label';
      lbl.textContent = 'no agents in channel yet';
      targetBar.appendChild(lbl);
      return;
    }
    const lbl = document.createElement('span');
    lbl.className = 'tb-label';
    lbl.textContent = 'send to:';
    targetBar.appendChild(lbl);

    targetables.forEach((m, idx) => {
      const pill = document.createElement('button');
      pill.type = 'button';
      pill.className = 'tb-pill' + (state.selectedTargets.has(m.id) ? ' on' : '');
      const a = animalFor(m);
      pill.innerHTML = '<span class="tb-num">' + (idx + 1) + '</span>' +
                       '<span>' + (a.emoji || '') + '</span>' +
                       '<span>' + escapeHtml(m.name || m.id) + '</span>';
      pill.title = 'click to toggle — Alt+' + (idx + 1) + ' keyboard shortcut';
      pill.addEventListener('click', () => toggleTarget(m.id));
      targetBar.appendChild(pill);
    });

    const allSelected = targetables.length > 0 &&
      targetables.every(m => state.selectedTargets.has(m.id));
    const allPill = document.createElement('button');
    allPill.type = 'button';
    allPill.className = 'tb-pill tb-all' + (allSelected ? ' on' : '');
    allPill.innerHTML = '<span class="tb-num">A</span><span>All</span>';
    allPill.title = 'toggle all targets — Alt+A';
    allPill.addEventListener('click', toggleAllTargets);
    targetBar.appendChild(allPill);

    if (state.selectedTargets.size > 0) {
      const clearPill = document.createElement('button');
      clearPill.type = 'button';
      clearPill.className = 'tb-pill';
      clearPill.textContent = 'clear';
      clearPill.title = 'clear selection (broadcast) — Alt+0';
      clearPill.addEventListener('click', () => {
        state.selectedTargets.clear();
        savePersistedTargets();
        renderComposerTargets();
        updatePreview();
      });
      targetBar.appendChild(clearPill);
    }
  }

  function renderRoster(members) {
    applyRosterWatermarkDeltas(members);
    // Refresh the id→avatar cache so animalForId() resolves message
    // authors to the server-assigned collision-free emoji. Must run
    // before any render path that looks up avatars by id.
    rememberAvatars(members);

    // Per-member context (fingerprint-joined server-side): drives the
    // ring on each member's watermark pin.
    state.contextByMember = new Map(
      members.filter(m => m.context_pct != null).map(m => [m.id, m.context_pct]));
    // Reconcile state.members — and detect name changes so the chat can
    // retroactively re-label past messages from the renamed member.
    const rename_from = new Map();  // id → old member_name for messages
    for (const m of members) {
      const old = state.members.get(m.id);
      state.members.set(m.id, m);
      if (old && old.name !== m.name) rename_from.set(m.id, { from: old.name, to: m.name });
    }
    // Drop members the roster no longer lists. state.members backs the composer
    // target chips (and their Alt+N hotkeys), @-autocomplete, ack badges and
    // watermark pins — without this a culled member stays selectable until reload.
    const liveIds = new Set(members.map(m => m.id));
    for (const id of [...state.members.keys()]) {
      if (!liveIds.has(id)) state.members.delete(id);
    }

    if (rename_from.size > 0) {
      // Patch cached message records so author label follows the current alias.
      for (const [id, msg] of state.messages) {
        const rename = rename_from.get(msg.member_id);
        if (rename) {
          msg.member_name = rename.to;
        }
      }
      refreshMessageAuthors();
    }

    rosterEl.innerHTML = '';
    const sorted = members.slice().sort((a, b) => {
      const order = { working: 0, active: 1, idle: 2, stale: 3, dead: 4 };
      if (a.id === state.operator.id) return 1;
      if (b.id === state.operator.id) return -1;
      const oa = order[a.status] ?? 4;
      const ob = order[b.status] ?? 4;
      if (oa !== ob) return oa - ob;
      return (a.name || '').localeCompare(b.name || '');
    });
    for (const m of sorted) rosterEl.appendChild(renderMemberRow(m));
    rosterHeading.textContent = `Members (${members.length})`;

    renderComposerTargets();
    // A roster arrival/rename can turn an existing @token from unresolved to
    // valid without another keystroke, so refresh the composer mirror too.
    updatePreview();
    updateAllAckBadges();
    renderWatermarkPins();
    scheduleHereUpdate();
    updateChanStats();

    // DM mode: update tab title with target's current name/animal now
    // that we have the roster.
    if (DM_MODE) {
      const tgt = state.members.get(DM_TARGET_ID);
      if (tgt) {
        const a = animalFor(tgt);
        const label = `DM ${a.emoji} ${tgt.name} — trio#${state.channel}`;
        state.originalTitle = label;
        hChannel.textContent = label;
        updateTitle();
      }
    }
  }

  // ── Watermark pins: one animal per member, parked at their last-read msg ──
  function renderWatermarkPins() {
    // Clear existing pins first
    for (const dom of state.messageDomById.values()) {
      const c = dom.querySelector('.watermark-pins');
      if (c) c.innerHTML = '';
    }
    // Sorted message ids (ascending). state.messageDomById preserves
    // insertion order, but be explicit because history prefixing
    // might out-of-order future paths.
    const sortedIds = [...state.messageDomById.keys()].sort((a, b) => a - b);
    if (sortedIds.length === 0) return;
    for (const [mid, mem] of state.members) {
      const lr = mem.last_read || 0;
      if (lr <= 0) continue;
      // Binary search: highest id <= lr in sortedIds
      let lo = 0, hi = sortedIds.length - 1, pinId = -1;
      while (lo <= hi) {
        const k = (lo + hi) >> 1;
        if (sortedIds[k] <= lr) { pinId = sortedIds[k]; lo = k + 1; }
        else hi = k - 1;
      }
      if (pinId < 0) continue;
      const dom = state.messageDomById.get(pinId);
      if (!dom) continue;
      const c = dom.querySelector('.watermark-pins');
      if (!c) continue;
      const a = animalFor(mem);
      const pin = document.createElement('span');
      pin.className = 'watermark-pin' + (mid === state.operator.id ? ' self' : '');
      pin.textContent = a.emoji;
      pin.title = `${mem.name} — the ${a.name} — read through #${lr}`;
      const cpct = state.contextByMember && state.contextByMember.get(mid);
      if (cpct != null) {
        const cc = cpct >= 80 ? 'var(--err)' : cpct >= 60 ? 'var(--warn)' : 'var(--accent2)';
        pin.classList.add('ctx-ringed');
        pin.style.background =
          `conic-gradient(${cc} ${Math.round(cpct)}%, var(--border) 0)`;
        pin.title += ` — context ${Math.round(cpct)}%`;
      }
      c.appendChild(pin);
    }
  }

  // Remove a member from the channel (roster × button). Confirms first — it
  // releases their claimed tasks + locks and posts a [culled] message. The SSE
  // roster refresh drops them from the sidebar; it does not stop a live agent's
  // process (it would just start erroring and could reconnect).
  async function cullMember(id, name, btn) {
    // Single backslash-n. This script is embedded in a Python raw string, so a
    // doubled backslash survives to the browser verbatim and the dialog would
    // display the escape sequence as literal text.
    if (!confirm('Remove ' + name + ' from the channel?\n\n'
        + 'This cannot be undone. Their claimed tasks and held locks are '
        + 'released, their sessions are revoked, and a [culled] notice is '
        + 'posted to the channel.\n\n'
        + 'It does not stop a running process — it only removes them here.')) return;
    // Disable while in flight and bound the wait. Without this the button gives
    // no signal at all after you have confirmed an irreversible action — and a
    // request CAN hang indefinitely: several dashboard tabs consume the
    // browser's per-origin connection cap with their SSE streams, and the DM
    // button opens tabs, so reaching the cap is a normal thing to do.
    const label = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Removing…'; }
    try {
      const r = await fetch('/api/cull' + API_QS, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_member_id: id }),
        signal: (AbortSignal && AbortSignal.timeout) ? AbortSignal.timeout(15000) : undefined,
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ error: 'unknown' }));
        alert('remove failed: ' + (err.error || r.status));
      }
    } catch (e) {
      alert(e.name === 'TimeoutError'
        ? 'remove timed out — the dashboard did not get a reply, so ' + name
          + ' may or may not have been removed. Reload to check.'
        : 'remove failed: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = label; }
    }
  }

  function renderMemberRow(m) {
    const { name: animalName, emoji } = animalFor(m);
    const row = document.createElement('div');
    row.className = 'member' + (state.expandedMembers.has(m.id) ? ' expanded' : '');
    row.title = `${m.name} (${m.id}) — the ${animalName}\n${m.status_text || ''}\nlast_read: ${m.last_read}`;

    const topRow = document.createElement('div');
    topRow.className = 'row';
    const dot = document.createElement('div');
    dot.className = 'dot ' + m.status;
    topRow.appendChild(dot);
    const animalSpan = document.createElement('span');
    animalSpan.className = 'roster-animal';
    animalSpan.textContent = emoji;
    animalSpan.title = `the ${animalName}`;
    topRow.appendChild(animalSpan);
    const nameBox = document.createElement('div');
    nameBox.className = 'name';
    nameBox.textContent = m.name;
    nameBox.style.color = colorFor(m.id);
    topRow.appendChild(nameBox);
    const idSpan = document.createElement('div');
    idSpan.className = 'id';
    idSpan.textContent = m.id.slice(0, 8);
    topRow.appendChild(idSpan);
    // Filter mode pill — "all" shown dim, "about" green, "at" amber. Helps
    // humans see at a glance who will actually hear an ambient message.
    const fm = m.filter_mode || 'all';
    if (fm && fm !== 'all') {
      const fmPill = document.createElement('span');
      fmPill.className = 'fmode ' + fm;
      fmPill.textContent = fm;
      fmPill.title = fm === 'at'
        ? 'Listening mode: at — only wakes on @pings. Ambient messages silent.'
        : 'Listening mode: about — wakes on @pings and #pounds. Ambient silent.';
      topRow.appendChild(fmPill);
    }
    // Context-window usage badge — present only for sessions on the same
    // machine as this nth_web (fed by the statusline publisher).
    if (m.context_pct != null) {
      const ctxPill = document.createElement('span');
      const pct = Math.round(m.context_pct);
      ctxPill.className = 'ctx-pct' + (pct >= 80 ? ' hot' : pct >= 60 ? ' warm' : '');
      ctxPill.textContent = pct + '%';
      ctxPill.title = 'Context window used (from this machine\'s statusline publisher)';
      topRow.appendChild(ctxPill);
    }
    // DM button — opens a filtered-view tab for this agent.
    // Hide for self, for human operator rows, and inside an existing DM tab.
    if (!DM_MODE && m.id !== state.operator.id && !m.id.startsWith('_op_')) {
      const dmBtn = document.createElement('span');
      dmBtn.className = 'dm-btn';
      dmBtn.textContent = 'DM';
      dmBtn.title = `Open DM tab with ${m.name}`;
      dmBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        window.open('/?dm=' + encodeURIComponent(m.id), '_blank');
      });
      topRow.appendChild(dmBtn);
    }
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = '▶';
    topRow.appendChild(caret);
    row.appendChild(topRow);

    if (m.status_text) {
      const st = document.createElement('div');
      st.className = 'stext';
      st.textContent = m.status_text;
      row.appendChild(st);
    }

    const stats = document.createElement('div');
    stats.className = 'stats';
    stats.innerHTML = renderMemberStatsHTML(m);
    row.appendChild(stats);

    // Remove control — revealed only when the row is expanded, so it can't be
    // mis-clicked from the collapsed roster (on a phone the old always-visible
    // × sat 53px from the drawer's own close ×, same glyph, at a sub-44px
    // target). Hidden entirely for identities the server would refuse, rather
    // than walking them through two dialogs into a 403.
    if (!DM_MODE && m.id !== state.operator.id && CAN_CULL) {
      const actions = document.createElement('div');
      actions.className = 'member-actions';
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'rm-btn';
      rm.textContent = 'Remove';
      rm.title = `Remove ${m.name} from this channel — releases their tasks and locks, and cannot be undone`;
      rm.addEventListener('click', (e) => { e.stopPropagation(); cullMember(m.id, m.name, rm); });
      actions.appendChild(rm);
      row.appendChild(actions);
    }

    row.addEventListener('click', (e) => {
      // Clicking the name on a mention-capable row? On shift-click → filter.
      if (e.shiftKey) {
        setFilter(m.name);
        return;
      }
      if (state.expandedMembers.has(m.id)) state.expandedMembers.delete(m.id);
      else state.expandedMembers.add(m.id);
      row.classList.toggle('expanded');
      stats.innerHTML = renderMemberStatsHTML(m);
    });
    return row;
  }

  function renderMemberStatsHTML(m) {
    const maxId = Math.max(0, ...state.messages.keys());
    const behind = Math.max(0, maxId - (m.last_read || 0));
    const lat = agentAvgReadLatency(m.id);
    const latClass = lat == null ? '' : (lat >= 20 ? 'bad' : (lat >= 5 ? 'warn' : 'good'));
    const q = (state.agentStats.get(m.id) || {}).queue_depth || 0;
    const qClass = q >= 10 ? 'bad' : (q >= 3 ? 'warn' : 'good');
    const sent = (state.agentStats.get(m.id) || {}).sent || 0;
    const rate = agentSendRatePerHour(m.id);
    const rr = agentReplyRate(m.id);
    const alen = agentAvgLen(m.id);
    const snippet = (state.agentStats.get(m.id) || {}).lastSnippet || '';
    const lastSeenAge = m.last_seen ? fmtRel((Date.now() - new Date(m.last_seen).getTime()) / 1000) : '—';

    const rows = [
      ['seen',          escapeHtml(lastSeenAge), ''],
      ['last_read',     `${m.last_read} <span style="color:var(--dimmer)">(${behind} behind)</span>`, behind > 5 ? 'warn' : ''],
      ['read-lat',      lat == null ? '—' : lat.toFixed(1) + 's', latClass],
      ['sent',          `${sent} <span style="color:var(--dimmer)">(${rate}/h)</span>`, ''],
      ['queue',         String(q), qClass],
      ['@reply %',      rr == null ? '—' : Math.round(rr * 100) + '%', ''],
      ['avg len',       alen == null ? '—' : Math.round(alen), ''],
    ];
    let html = '';
    for (const [k, v, cls] of rows) {
      html += `<div class="stat-row"><span class="stat-label">${k}</span>`
           +  `<span class="stat-val ${cls}">${v}</span></div>`;
    }
    if (snippet) {
      html += `<div class="snippet" title="${escapeHtml(snippet)}">${escapeHtml(snippet)}</div>`;
    }
    if (m.context) {
      const c = m.context;
      const h = c.harness || {};
      const cw = h.context_window || {};
      const rl = h.rate_limits || {};
      // Claude snapshots nest sizes under harness; codex publisher snapshots
      // carry cw_size (and effort) at the top level.
      const cwSize = (cw.context_window_size || c.cw_size || 0);
      const cwLabel = cwSize >= 1e6 ? (cwSize/1e6)+'M' : cwSize >= 1e3 ? Math.round(cwSize/1e3)+'k' : '';
      const pct = c.used_pct != null ? Math.round(c.used_pct) + '%' : '—';
      const pctClass = (c.used_pct || 0) >= 80 ? 'bad' : (c.used_pct || 0) >= 60 ? 'warn' : 'good';
      const model = ((c.model || '').startsWith('claude-')
        ? c.model.replace(/^claude-/, '').split('-').slice(0, 2).join(' ')
        : (c.model || '')) || '—';
      const fiveH = rl.five_hour || {};
      const sevenD = rl.seven_day || {};
      const fhPct = fiveH.used_percentage != null ? Math.round(fiveH.used_percentage) + '%' : '';
      const sdPct = sevenD.used_percentage != null ? Math.round(sevenD.used_percentage) + '%' : '';
      // Codex publishers refresh their snapshot while the TUI is alive even
      // when no new token count arrived, so a fresh file can carry an old
      // number. data_age_s is the age of the reading itself — say so rather
      // than presenting an hours-old figure as current.
      const dAge = c.data_age_s;
      const staleNote = (typeof dAge === 'number' && dAge > 300)
        ? ` (as of ${dAge >= 3600 ? Math.round(dAge/3600)+'h' : Math.round(dAge/60)+'m'} ago)`
        : '';
      const ctxRows = [
        // cwLabel is '' when the window size is unknown — don't render "45% of ".
        ['context', (cwLabel ? `${pct} of ${cwLabel}` : pct) + escapeHtml(staleNote),
         staleNote ? '' : pctClass],
        ['model', escapeHtml(model), ''],
      ];
      if (c.effort) ctxRows.push(['effort', escapeHtml(c.effort), '']);
      if (fhPct) ctxRows.push(['5h limit', fhPct, (fiveH.used_percentage||0) >= 80 ? 'bad' : '']);
      if (sdPct) ctxRows.push(['7d limit', sdPct, (sevenD.used_percentage||0) >= 80 ? 'bad' : '']);
      if (c.session_name) ctxRows.push(['session', escapeHtml(c.session_name), '']);
      for (const [k2, v2, cl] of ctxRows) {
        html += `<div class="stat-row"><span class="stat-label">${k2}</span>`
             +  `<span class="stat-val ${cl}">${v2}</span></div>`;
      }
    }
    return html;
  }

  // ── Channel stats ──
  function updateChanStats() {
    const totalMsgs = state.messages.size;
    const runtime = (Date.now() - state.startedAt) / 1000;
    const now = Date.now();
    const cutoff = now - 5 * 60 * 1000;
    let recent = 0;
    for (const [bin, count] of state.rateBins) if (bin >= cutoff) recent += count;
    const ratePerMin = recent / 5;   // msgs/min over last 5 min

    const stats = [
      ['total messages', totalMsgs],
      ['rate (5m avg)', ratePerMin.toFixed(1) + '/min'],
      ['session uptime', fmtRel(runtime)],
    ];
    let html = '';
    for (const [k, v] of stats) {
      html += `<div class="stat-row"><span class="stat-label">${k}</span>`
           +  `<span class="stat-val">${v}</span></div>`;
    }
    chanStatsEl.innerHTML = html;
    renderSparkline();
  }
  function renderSparkline() {
    const BARS = '▁▂▃▄▅▆▇█';
    const WIN_MIN = 5;
    const WIN_SEC = WIN_MIN * 60;
    const binSize = 10;
    const now = Date.now();
    const nowBin = Math.floor(now / (binSize * 1000)) * (binSize * 1000);
    const wantBins = WIN_SEC / binSize;
    const vals = [];
    for (let i = wantBins - 1; i >= 0; i--) {
      const k = nowBin - i * (binSize * 1000);
      vals.push(state.rateBins.get(k) || 0);
    }
    const hi = Math.max(1, ...vals);
    sparkEl.textContent = vals.map(v =>
      BARS[Math.min(BARS.length - 1, Math.floor(v / hi * (BARS.length - 1)))]).join('');
    sparkEl.title = `5-min activity · max ${hi} msg / 10s bin`;
  }

  // ── Autocomplete ──
  // @ (ping), # (pound-reference), or ! (bang / unfilterable) trigger the popup.
  // Sigil is carried through so acceptance preserves the user's intent.
  function currentSigilToken() {
    const pos = input.selectionStart;
    const text = input.value.slice(0, pos);
    const atPos   = text.lastIndexOf('@');
    const hashPos = text.lastIndexOf('#');
    const bangPos = text.lastIndexOf('!');
    const sigilPos = Math.max(atPos, hashPos, bangPos);
    if (sigilPos < 0) return null;
    const sigil = text[sigilPos];
    if (sigilPos > 0 && !' \t,;([\n'.includes(text[sigilPos - 1])) return null;
    const frag = text.slice(sigilPos + 1);
    if (frag && !/^[A-Za-z0-9_\-]*$/.test(frag)) return null;
    return { sigilPos, sigil, fragment: frag };
  }
  function computeCompletions() {
    const tok = currentSigilToken();
    if (!tok) return { items: [], atPos: -1, sigil: '@' };
    const frag = tok.fragment.toLowerCase();
    const matches = [];
    for (const m of state.members.values()) {
      if (m.id === state.operator.id) continue;
      const nameL = (m.name || '').toLowerCase();
      if (!frag || nameL.includes(frag) || m.id.toLowerCase().startsWith(frag)) matches.push(m);
    }
    matches.sort((a, b) => {
      const an = (a.name || '').toLowerCase(), bn = (b.name || '').toLowerCase();
      const as = an.startsWith(frag) ? 0 : (frag && an.includes(frag) ? 1 : 2);
      const bs = bn.startsWith(frag) ? 0 : (frag && bn.includes(frag) ? 1 : 2);
      if (as !== bs) return as - bs;
      return an.localeCompare(bn);
    });
    return { items: matches.slice(0, 8), atPos: tok.sigilPos, sigil: tok.sigil };
  }
  function renderCompletions() {
    const { items } = state.completion;
    compEl.innerHTML = '';
    if (!state.completion.visible || items.length === 0) { compEl.classList.remove('active'); return; }
    items.forEach((m, i) => {
      const row = document.createElement('div');
      row.className = 'completion' + (i === state.completion.index ? ' selected' : '');
      const dot = document.createElement('div');
      dot.className = 'cdot dot ' + m.status;
      row.appendChild(dot);
      const anim = animalFor(m);
      const emoji = document.createElement('span');
      emoji.textContent = anim.emoji;
      emoji.style.fontSize = '14px';
      row.appendChild(emoji);
      const name = document.createElement('span');
      name.className = 'cname';
      name.textContent = (state.completion.sigil || '@') + m.name;
      name.style.color = colorFor(m.id);
      row.appendChild(name);
      const id = document.createElement('span');
      id.className = 'cid';
      id.textContent = m.id;
      row.appendChild(id);
      row.onmousedown = (e) => { e.preventDefault(); acceptCompletion(i); };
      compEl.appendChild(row);
    });
    compEl.classList.add('active');
  }
  function refreshCompletions() {
    const { items, atPos, sigil } = computeCompletions();
    state.completion.items = items;
    state.completion.atPos = atPos;
    state.completion.sigil = sigil;
    state.completion.visible = items.length > 0 && atPos >= 0;
    if (state.completion.index >= items.length) state.completion.index = 0;
    renderCompletions();
  }
  function acceptCompletion(i) {
    const { items, atPos, sigil } = state.completion;
    if (atPos < 0 || !items.length) return;
    const idx = i ?? state.completion.index;
    const m = items[idx];
    if (!m) return;
    const before = input.value.slice(0, atPos);
    const endPos = input.selectionStart;
    const after = input.value.slice(endPos);
    const repl = (sigil || '@') + (m.name || m.id) + ' ';
    input.value = before + repl + after;
    const newPos = (before + repl).length;
    input.setSelectionRange(newPos, newPos);
    state.completion.visible = false;
    renderCompletions();
    updatePreview();
  }
  function insertMention(m) {
    const pos = input.selectionStart;
    const before = input.value.slice(0, pos);
    const after = input.value.slice(pos);
    const needSpaceBefore = before && !before.endsWith(' ') && !before.endsWith('\n');
    const tag = (needSpaceBefore ? ' ' : '') + '@' + (m.name || m.id) + ' ';
    input.value = before + tag + after;
    input.focus();
    const p = (before + tag).length;
    input.setSelectionRange(p, p);
    updatePreview();
  }
  function resolveSigilTokens(text, sigil) {
    const out = [];
    const seen = new Set();
    const esc = sigil === '@' ? '@' : '#';
    const re = new RegExp(`(?<![A-Za-z0-9_])${esc}([A-Za-z0-9_\\-]+)`, 'g');
    let m;
    while ((m = re.exec(text))) {
      const tok = m[1];
      let picked = null;
      for (const mem of state.members.values()) {
        if (mem.id === state.operator.id) continue;
        if (mem.id === tok || (mem.name && mem.name.toLowerCase() === tok.toLowerCase())) {
          picked = mem; break;
        }
      }
      if (!picked) {
        const prefix = [...state.members.values()]
          .filter(mem => mem.id !== state.operator.id
                        && mem.id.toLowerCase().startsWith(tok.toLowerCase()));
        if (prefix.length === 1) picked = prefix[0];
      }
      if (picked && !seen.has(picked.id)) {
        seen.add(picked.id);
        out.push(picked);
      }
    }
    return out;
  }
  function resolveMentions(text) { return resolveSigilTokens(text, '@'); }
  function resolveRefs(text)     { return resolveSigilTokens(text, '#'); }
  function resolveBangs(text)    { return resolveSigilTokens(text, '!'); }
  function updatePreview() {
    renderComposerMentionHighlights();
    const pings = resolveMentions(input.value);
    const refs  = resolveRefs(input.value);
    const bangs = resolveBangs(input.value);
    const txtL  = (input.value || '').toLowerCase();
    const parts = [];
    if (!state.dmTargetId && state.selectedTargets.size > 0) {
      const tgts = [...state.selectedTargets]
        .map(id => state.members.get(id))
        .filter(Boolean)
        .map(m => `<span class="tgt">@${escapeHtml(m.name)}</span>`)
        .join(', ');
      parts.push(`locked targets: ${tgts}`);
    }
    if (pings.length) {
      const names = pings.map(m => `<span class="tgt">@${escapeHtml(m.name)}</span>`).join(', ');
      parts.push(`pings: ${names}`);
    }
    if (refs.length) {
      const n = refs.map(m => `<span class="tgt" style="color:var(--ref-chip)">#${escapeHtml(m.name)}</span>`).join(', ');
      parts.push(`refs: ${n}`);
    }
    if (bangs.length || /(^|\s)!all(\b|$)/.test(txtL)) {
      const n = bangs.map(m => `<span class="tgt" style="color:var(--bang-chip)">!${escapeHtml(m.name)}</span>`).join(', ');
      const allTag = /(^|\s)!all(\b|$)/.test(txtL) ? '<span class="tgt" style="color:var(--bang-chip)">!all</span>' : '';
      parts.push(`<b style="color:var(--bang-chip)">BANGS (unfilterable)</b>: ${[allTag, n].filter(Boolean).join(', ')}`);
    }
    preview.innerHTML = parts.join('  ·  ');
  }
  function autoResizeInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(160, Math.max(36, input.scrollHeight)) + 'px';
    if (inputHighlight) {
      inputHighlight.style.height = input.style.height;
      inputHighlight.scrollTop = input.scrollTop;
      inputHighlight.scrollLeft = input.scrollLeft;
    }
  }

  // ── Send ──
  // ── Image attachments (composer upload) ──
  const attachBtn = document.getElementById('attach-btn');
  const fileInput = document.getElementById('file-input');
  const attachStrip = document.getElementById('attach-strip');
  const composerEl = document.getElementById('composer');

  function renderAttachStrip() {
    attachStrip.innerHTML = '';
    state.pendingAttachments.forEach((att, i) => {
      const t = document.createElement('div');
      t.className = 'attach-thumb' + (att.uploading ? ' uploading' : '');
      if (att.url) {
        const img = document.createElement('img');
        img.src = att.url;
        t.appendChild(img);
      }
      if (!att.uploading) {
        const rm = document.createElement('button');
        rm.className = 'rm'; rm.textContent = '×'; rm.title = 'remove';
        rm.addEventListener('click', () => {
          dropSlot(att);
          renderAttachStrip();
        });
        t.appendChild(rm);
      }
      attachStrip.appendChild(t);
    });
  }

  function revokeBlob(att) {
    if (att && att.url && att.url.indexOf('blob:') === 0) URL.revokeObjectURL(att.url);
  }
  function dropSlot(slot) {
    revokeBlob(slot);
    const idx = state.pendingAttachments.indexOf(slot);
    if (idx >= 0) state.pendingAttachments.splice(idx, 1);
  }

  // Mirrors MAX_UPLOAD_BYTES in this file's Python half, so a huge file is
  // refused before it is pushed over the wire. A literal rather than a
  // substitution, so the served bundle carries no placeholder the test
  // harness would need to know about; the server still enforces the real
  // limit, so drift can only make the client stricter, never unsafe.
  const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

  async function uploadImage(file) {
    if (!file) return;
    if (!file.type || !/^image\//.test(file.type)) {
      // The composer flashes an accepting outline on dragover, so returning
      // silently here tells the user the drop landed and then does nothing.
      // Drag-and-drop also bypasses the file picker's accept= filter entirely.
      alert('"' + (file.name || 'that file') + '" is not an image. '
            + 'PNG, JPEG, GIF and WebP can be attached.');
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      // Checked here as well as server-side so a 40MB photo is not pushed over
      // the wire before being refused, and so the number is human-sized.
      const mb = (n) => (n / (1024 * 1024)).toFixed(1).replace(/\.0$/, '');
      alert('"' + (file.name || 'that image') + '" is ' + mb(file.size)
            + ' MB — the limit is ' + mb(MAX_UPLOAD_BYTES) + ' MB.');
      return;
    }
    if (state.pendingAttachments.length >= 8) { alert('max 8 images per message'); return; }
    const slot = { uploading: true, url: URL.createObjectURL(file) };
    state.pendingAttachments.push(slot);
    renderAttachStrip();
    try {
      const r = await fetch('/api/upload' + API_QS, {
        method: 'POST',
        headers: { 'Content-Type': file.type, 'X-Filename': encodeURIComponent(file.name || 'image') },
        body: file,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) {
        alert('upload failed: ' + (data.error || r.status));
        dropSlot(slot);
      } else {
        revokeBlob(slot);                       // free the local preview blob
        slot.id = data.id;
        slot.uploading = false;
        slot.url = '/api/attachment/' + data.id + API_QS;
      }
    } catch (e) {
      alert('upload failed: ' + e.message);
      dropSlot(slot);
    }
    renderAttachStrip();
  }

  attachBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    for (const f of fileInput.files) uploadImage(f);
    fileInput.value = '';
  });
  input.addEventListener('paste', (e) => {
    const items = (e.clipboardData || {}).items || [];
    for (const it of items) {
      if (it.kind === 'file' && /^image\//.test(it.type)) {
        const f = it.getAsFile();
        if (f) { e.preventDefault(); uploadImage(f); }
      }
    }
  });
  ['dragover', 'dragenter'].forEach(ev => composerEl.addEventListener(ev, (e) => {
    e.preventDefault(); composerEl.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(ev => composerEl.addEventListener(ev, (e) => {
    e.preventDefault(); composerEl.classList.remove('dragover');
  }));
  composerEl.addEventListener('drop', (e) => {
    const files = (e.dataTransfer || {}).files || [];
    for (const f of files) uploadImage(f);
  });

  // Remove specific slots, preserving anything added since.
  function dropAttachments(slots) {
    const gone = new Set(slots);
    state.pendingAttachments = state.pendingAttachments.filter(a => !gone.has(a));
  }

  // ── Speech-to-text: mic → composer ──
  // Two modes (state.sttMode): 'local' records a clip and POSTs it to the warm
  // Whisper sidecar (/api/stt/transcribe); 'web' uses the browser's streaming
  // SpeechRecognition. If a LOCAL attempt fails, we auto-fall back to web and
  // show a banner — never a silent failure. Neither endpoint takes a channel,
  // so these fetches intentionally carry no API_QS.
  const micBtn = document.getElementById('mic-btn');
  const sttBanner = document.getElementById('stt-banner');
  const sttViz = document.getElementById('stt-viz');
  const sttWaveCanvas = document.getElementById('stt-wave');
  const sttSpinner = document.getElementById('stt-spinner');
  const sttVizLabel = document.getElementById('stt-viz-label');
  // Glyphs match #attach-btn's text-glyph idiom. ICON_MIC is captured from the
  // button's static markup so the glyph itself lives in exactly one place.
  const ICON_STOP = '⏹';
  const ICON_MIC = micBtn ? micBtn.innerHTML : '';
  // Below this normalized peak amplitude a clip is treated as silent and never
  // sent to Whisper (which otherwise hallucinates words from noise). Kept lenient
  // so quiet speech still goes through; the server no_speech check is the backstop.
  // Only has to reject a clip that captured nothing at all. Anything with real
  // signal is the server's decision, made on a full RMS measurement rather than
  // this coarse peak. It was 0.015, which quiet speech does not always reach.
  const STT_SILENCE_PEAK = 0.004;
  // Display-only amplification for the level meter (see makeWaveform).
  const WAVE_DISPLAY_GAIN = 6;
  const STT_FETCH_TIMEOUT_MS = 240000;   // backstop; cold start can download ~1.5GB
  // Mirrors the server's NTH_STT_LANG so both dictation paths speak the same
  // language. BCP-47 needs a region; a bare "en" is widely mishandled.
  const STT_WEB_LANG = /*__STT_LANG__*/'en-US';
  // Turn an internal engine reason into something a person can read.
  // Map a server reason onto something a person can act on. Order matters:
  // the "not installed" test runs before the generic ones because that is the
  // single most likely failure — every machine without the engine — and it
  // used to fall through to "an unexpected error", which tells nobody anything.
  function humanizeSttError(reason) {
    reason = String(reason || '');
    if (/not installed|no module named|not importable|not available|import failed/i.test(reason))
      return 'the speech engine is not installed';
    if (/still downloading/i.test(reason)) return 'the speech model is still downloading';
    if (/ffmpeg/i.test(reason)) return 'ffmpeg is missing on the server';
    if (/timed out|timeout|stalled/i.test(reason)) return 'it timed out';
    if (/busy/i.test(reason)) return 'it was busy';
    if (/failed to start/i.test(reason)) return 'the engine could not start';
    if (/pipe|exited|respawn|malformed/i.test(reason)) return 'the engine restarted';
    if (/HTTP\s*\d/i.test(reason)) return 'the server returned an error';
    if (/audio|transcrib/i.test(reason)) return 'the audio could not be read';
    return 'an unexpected error';
  }
  try { const m = localStorage.getItem('trio.sttMode'); if (m === 'web' || m === 'local') state.sttMode = m; } catch (_) {}

  function showSttBanner(msg, kind) {
    if (!sttBanner) return;
    sttBanner.textContent = msg;
    sttBanner.className = kind || '';
    sttBanner.hidden = false;
  }
  function hideSttBanner() { if (sttBanner) sttBanner.hidden = true; }

  // Live audio waveform on a <canvas> from a MediaStream. Reusable across the
  // composer and the settings test page. Returns { start(stream), stop() }.
  function makeWaveform(canvas) {
    let raf = null, audioCtx = null, analyser = null, source = null, data = null;
    let peak = 0, sampled = false;   // loudest normalized sample seen this session (0..1)
    function start(stream) {
      stop();
      peak = 0; sampled = false;
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC || !canvas || !stream) return;
      try {
        audioCtx = new AC();
        if (audioCtx.state === 'suspended') { try { audioCtx.resume(); } catch (_) {} }
        source = audioCtx.createMediaStreamSource(stream);
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 1024;
        source.connect(analyser);
        data = new Uint8Array(analyser.fftSize);
      } catch (_) { stop(); return; }
      const cx = canvas.getContext('2d');
      const stroke = (getComputedStyle(document.documentElement)
                      .getPropertyValue('--accent') || '#62d7ef').trim() || '#62d7ef';
      function draw() {
        raf = requestAnimationFrame(draw);
        analyser.getByteTimeDomainData(data);
        const w = canvas.width, h = canvas.height;
        cx.clearRect(0, 0, w, h);
        cx.lineWidth = 2;
        cx.strokeStyle = stroke;
        cx.beginPath();
        const slice = w / data.length;
        let x = 0, frameMax = 0;
        for (let i = 0; i < data.length; i++) {
          const dev = data[i] - 128;
          if (Math.abs(dev) > frameMax) frameMax = Math.abs(dev);
          // Drawn with gain: at true scale a normal speaking voice moves this
          // line by a couple of pixels and a whisper not visibly at all, so it
          // read as "the mic isn't hearing me" when the mic was fine. The gain
          // is display-only — `peak` below stays the true measurement, because
          // the silence gate must not be fooled by a scaled-up picture.
          const shown = Math.max(-128, Math.min(127, dev * WAVE_DISPLAY_GAIN));
          const y = ((shown + 128) / 128.0) * h / 2;   // 128 = silence midline
          if (i === 0) cx.moveTo(x, y); else cx.lineTo(x, y);
          x += slice;
        }
        sampled = true;
        if (frameMax / 128 > peak) peak = frameMax / 128;   // energy proxy for silence detection
        cx.stroke();
      }
      draw();
    }
    function stop() {
      if (raf) { cancelAnimationFrame(raf); raf = null; }
      if (source) { try { source.disconnect(); } catch (_) {} source = null; }
      if (audioCtx) { try { audioCtx.close(); } catch (_) {} audioCtx = null; }
      analyser = null; data = null;
    }
    // getPeak() returns -1 when no audio was ever sampled (analyser unavailable),
    // so callers can distinguish "silent" from "couldn't measure".
    return { start, stop, getPeak: () => (sampled ? peak : -1) };
  }

  const composerWave = makeWaveform(sttWaveCanvas);

  // Composer visualizer: 'wave' while recording, 'spin' while transcribing.
  function showViz(kind, label, stream) {
    if (!sttViz) return;
    sttViz.hidden = false;
    if (sttVizLabel) sttVizLabel.textContent = label || '';
    if (kind === 'wave') {
      if (sttWaveCanvas) sttWaveCanvas.hidden = false;
      if (sttSpinner) sttSpinner.hidden = true;
      composerWave.start(stream);
    } else {   // 'spin'
      composerWave.stop();
      if (sttWaveCanvas) sttWaveCanvas.hidden = true;
      if (sttSpinner) sttSpinner.hidden = false;
    }
  }
  function hideViz() {
    composerWave.stop();
    if (sttViz) sttViz.hidden = true;
    if (sttWaveCanvas) sttWaveCanvas.hidden = false;
    if (sttSpinner) sttSpinner.hidden = true;
  }

  // The mic is a state machine: idle → opening → recording → stopping →
  // working → idle. 'opening' and 'stopping' exist because both ends of a take
  // are ASYNCHRONOUS — getUserMedia resolves later, and MediaRecorder.onstop /
  // SpeechRecognition.onend fire later. Tracking only "is recording" leaves
  // those two windows re-enterable, and a click landing in one starts a SECOND
  // capture while the first is still tearing down: two live recorders, and a
  // MediaStream whose tracks nothing ever stops.
  let micPhase = 'idle';
  function setMicState(s) {   // 'idle' | 'opening' | 'recording' | 'stopping' | 'working'
    micPhase = s;
    state.sttRecording = (s === 'recording');
    if (micBtn) {
      micBtn.classList.toggle('recording', s === 'recording');
      micBtn.classList.toggle('working', s === 'working' || s === 'stopping' || s === 'opening');
      micBtn.classList.toggle('cancelable', s === 'working');
      micBtn.innerHTML = (s === 'recording') ? ICON_STOP : ICON_MIC;
      micBtn.title = (s === 'recording') ? 'stop dictation'
                   : (s === 'opening') ? 'waiting for microphone permission…'
                   : (s === 'stopping') ? 'finishing…'
                   : (s === 'working') ? 'transcribing… (click to cancel)'
                   : 'dictate (speech to text)';
      micBtn.setAttribute('aria-label', micBtn.title);
      micBtn.setAttribute('aria-pressed', String(s === 'recording'));
    }
    if (s === 'idle') hideViz();
  }

  // getUserMedia rejects for several reasons that are NOT permission problems.
  // Reporting them all as "denied" sends people into OS privacy settings that
  // were already correct.
  function micErrorMessage(e) {
    const n = (e && e.name) || '';
    if (n === 'NotAllowedError' || n === 'SecurityError')
      return 'Microphone access is blocked. Allow it from the mic icon in the address bar, then try again.';
    if (n === 'NotFoundError' || n === 'OverconstrainedError')
      return 'No microphone found. Connect one and try again.';
    if (n === 'NotReadableError')
      return 'The microphone is in use by another app (a call or recorder). Close it and try again.';
    if (n === 'AbortError')
      return 'The microphone was interrupted before recording started.';
    return 'Could not open the microphone' + (n ? ' (' + n + ')' : '') + '.';
  }

  // The server reports the clip's measured energy. Near-zero means the mic
  // captured nothing; low-but-present means it heard someone too quiet to
  // clear the silence gate — which needs different advice from "try again".
  function quietHint(rms) {
    if (typeof rms === 'number' && rms > 0 && rms < 0.01) {
      return 'That was very quiet — move closer to the microphone or raise its'
           + ' input level, then try again.';
    }
    return 'No message detected — try again.';
  }

  function webSpeechAvailable() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }

  // OFFER the switch to the browser's speech service — never perform it. The
  // user picked on-device; escalating their voice to a third party because a
  // local attempt failed is not a decision to make on their behalf, and the
  // mic must not be live before they have agreed to it.
  function offerWebFallback(reason) {
    setMicState('idle');
    if (!sttBanner) return;
    const why = humanizeSttError(reason);
    sttBanner.textContent = 'On-device transcription unavailable (' + why + '). ';
    if (/not installed/.test(why)) {
      const fix = document.createElement('span');
      fix.textContent = 'Install it on the server with “pip install mlx-whisper” (Apple silicon only). ';
      sttBanner.appendChild(fix);
    }
    if (webSpeechAvailable()) {
      const btn = document.createElement('button');
      btn.className = 'stt-banner-action';
      btn.textContent = 'Use browser dictation instead';
      btn.title = 'sends your audio to your browser vendor';
      btn.addEventListener('click', () => { hideSttBanner(); startWebDictation(); });
      sttBanner.appendChild(btn);
      const note = document.createElement('span');
      note.textContent = ' — this sends your audio to your browser vendor.';
      sttBanner.appendChild(note);
    } else {
      const note = document.createElement('span');
      note.textContent = 'This browser has no built-in speech recognition either (try Chrome or Safari).';
      sttBanner.appendChild(note);
    }
    sttBanner.className = 'warn';
    sttBanner.hidden = false;
  }

  // Land the transcript where the caret is, replacing any selection, and leave
  // the caret after it. Appending to the end was wrong for anyone who moved the
  // cursor back to fix a word mid-draft: the dictated phrase arrived at the
  // bottom of the message instead of where they were looking.
  function insertTranscript(text) {
    text = (text || '').trim();
    if (!text) return;
    const cur = input.value;
    // selectionStart is null on elements that don't expose a caret; in that
    // case fall back to the old append-at-end behaviour.
    const hasCaret = typeof input.selectionStart === 'number';
    const start = hasCaret ? input.selectionStart : cur.length;
    const end = hasCaret ? input.selectionEnd : cur.length;
    const before = cur.slice(0, start);
    const after = cur.slice(end);
    // Same spacing rule as before, applied at the insertion point rather than
    // at the end — plus its mirror on the trailing side, which an append-only
    // insert never had to think about.
    const lead = (before && !/\s$/.test(before)) ? ' ' : '';
    const trail = (after && !/^\s/.test(after)) ? ' ' : '';
    input.value = before + lead + text + trail + after;
    const caret = (before + lead + text).length;
    input.dispatchEvent(new Event('input'));   // autosize + mention mirror + preview
    input.focus();
    if (hasCaret && input.setSelectionRange) input.setSelectionRange(caret, caret);
  }

  // Web SpeechRecognition (streaming; interim words appear live).
  let webRec = null;
  // Set while web dictation is live; sendMessage() calls it so the recognizer
  // re-anchors to the emptied composer instead of typing the sent text back in.
  let sttReanchor = null;
  function startWebDictation() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      showSttBanner('Web speech recognition isn’t supported here (try Chrome or Safari).', 'err');
      setMicState('idle');
      return;
    }
    hideViz();   // web mode exposes no stream to visualize; the pulsing button conveys state
    // Re-read the composer on every result rather than snapshotting it once:
    // the user can keep typing during dictation, and can even send, and a
    // stale snapshot would overwrite their typing or resurrect a sent message.
    let anchor = input.value;
    let finalTxt = '';
    const rec = new SR();
    webRec = rec;
    // The local path's language comes from NTH_STT_LANG; mirror it here so a
    // non-English deployment doesn't get English-only web recognition.
    rec.lang = STT_WEB_LANG; rec.interimResults = true; rec.continuous = true;
    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalTxt += e.results[i][0].transcript;
        else interim += e.results[i][0].transcript;
      }
      const sep = (anchor && !/\s$/.test(anchor)) ? ' ' : '';
      input.value = anchor + sep + finalTxt + interim;
      input.dispatchEvent(new Event('input'));
    };
    // sendMessage() empties the composer. Re-anchor to the now-empty box and
    // drop what has already been transcribed, so the sent text is not typed
    // back in behind the user.
    sttReanchor = () => { anchor = ''; finalTxt = ''; };
    rec.onerror = (e) => { showSttBanner('Web speech error: ' + (e.error || 'unknown'), 'err'); };
    rec.onend = () => {
      // A newer recognizer has already taken over — this one must not touch
      // shared state or it will report idle while the new one is listening.
      if (webRec !== rec) return;
      // Chrome auto-ends on silence/timeout; while still recording, restart so
      // long dictation keeps going.
      if (micPhase === 'recording') { try { rec.start(); return; } catch (_) {} }
      webRec = null;
      sttReanchor = null;
      setMicState('idle');
    };
    try { rec.start(); setMicState('recording'); }
    catch (e) { showSttBanner('Could not start web speech: ' + e.message, 'err'); setMicState('idle'); }
  }
  function stopWebDictation() {
    if (!webRec) return;
    setMicState('stopping');   // onend is async — hold the gap shut until it fires
    try { webRec.stop(); } catch (_) { webRec = null; setMicState('idle'); }
  }

  // Local dictation: record with MediaRecorder, POST the clip to the sidecar.
  let mediaRec = null, mediaChunks = [], mediaStream = null;
  let localStarting = false;   // synchronous guard: mic is opening (pre-getUserMedia resolve)
  let composerAbort = null;    // AbortController for the in-flight transcribe fetch
  // Always stop the stream you were given, not "the current one". A take's
  // teardown can land after a later take has replaced mediaStream, and stopping
  // the wrong one leaves the earlier microphone live with no way to release it.
  function stopTracks(stream) {
    const s = stream || mediaStream;
    if (s) { try { s.getTracks().forEach(t => t.stop()); } catch (_) {} }
    if (s === mediaStream) mediaStream = null;
  }
  async function startLocalDictation() {
    if (localStarting) return;   // ignore a second click before the mic opens
    localStarting = true;
    setMicState('opening');      // the permission sheet can sit here indefinitely
    // Browsers apply noise suppression and echo cancellation by default. Both
    // are tuned for telephony, where the goal is suppressing anything that is
    // not loud, tonal speech — which describes whispering, so a quiet voice
    // gets attenuated before it ever reaches the recorder. Turn them off and
    // leave AGC on, which is the piece that actually helps a quiet talker.
    // Fall back to plain audio:true if a browser rejects the constraints.
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true },
      });
    } catch (e) {
      if (e && (e.name === 'OverconstrainedError' || e.name === 'NotSupportedError' || e.name === 'TypeError')) {
        try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
        catch (e2) { localStarting = false; showSttBanner(micErrorMessage(e2), 'err'); setMicState('idle'); return; }
      } else {
        localStarting = false; showSttBanner(micErrorMessage(e), 'err'); setMicState('idle'); return;
      }
    }
    const myStream = stream;     // this take's stream, captured for its own teardown
    mediaStream = stream;
    mediaChunks = [];
    const mime = (window.MediaRecorder && MediaRecorder.isTypeSupported('audio/webm')) ? 'audio/webm' : '';
    let rec;
    try { rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined); }
    catch (e) { stopTracks(myStream); localStarting = false; showSttBanner('Recording unsupported: ' + e.message, 'err'); setMicState('idle'); return; }
    mediaRec = rec;
    mediaChunks = [];
    // The peak used by the silence gate is sampled inside requestAnimationFrame,
    // which browsers pause in a hidden or occluded tab. Speaking while the tab
    // is backgrounded therefore yields a near-zero peak from a perfectly good
    // recording — so remember that it happened and skip the gate rather than
    // telling the user they said nothing.
    let hiddenDuringTake = document.hidden;
    const visWatch = () => { if (document.hidden) hiddenDuringTake = true; };
    document.addEventListener('visibilitychange', visWatch);
    rec.ondataavailable = (e) => { if (e.data && e.data.size) mediaChunks.push(e.data); };
    rec.onstop = async () => {
      document.removeEventListener('visibilitychange', visWatch);
      stopTracks(myStream);
      const peak = composerWave.getPeak();
      const blob = new Blob(mediaChunks, { type: rec.mimeType || 'audio/webm' });
      if (!blob.size) {
        setMicState('idle');
        showSttBanner('Nothing was recorded — check that the right microphone is selected.', 'warn');
        return;
      }
      if (!hiddenDuringTake && peak >= 0 && peak < STT_SILENCE_PEAK) {
        setMicState('idle');   // essentially silent — don't feed Whisper
        showSttBanner('No message detected — try again.', 'warn');
        return;
      }
      setMicState('working');
      showViz('spin', 'transcribing…');
      composerAbort = new AbortController();
      // Relabel if it's slow, but only claim what we can check. Saying "first
      // run" unconditionally was false on every later slow take — the model is
      // downloaded once. Start with wording that is true whenever the timer
      // fires, then ask /api/stt/health, which reports `cached` straight off
      // the weights on disk: cached === false at this instant means the
      // download really is still in flight, so the stronger label is earned.
      const slowTimer = setTimeout(() => {
        const myAbort = composerAbort;
        showViz('spin', 'still transcribing…');
        fetch('/api/stt/health')
          .then((r) => r.json())
          .then((d) => {
            // The take may have finished or been cancelled while we asked.
            if (composerAbort !== myAbort || micPhase !== 'working') return;
            if (d && d.available && d.cached === false)
              showViz('spin', 'downloading the speech model (first run)…');
          })
          .catch(() => {});   // a failed health check just leaves the neutral label
      }, 4000);
      const killTimer = setTimeout(() => { try { composerAbort.abort('timeout'); } catch (_) {} }, STT_FETCH_TIMEOUT_MS);
      try {
        const r = await fetch('/api/stt/transcribe', {
          method: 'POST',
          headers: { 'Content-Type': blob.type || 'audio/webm' },
          body: blob,
          signal: composerAbort.signal,
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.ok) { offerWebFallback((data && data.error) || ('HTTP ' + r.status)); return; }
        if (data.no_speech || !(data.text || '').trim()) {   // Whisper's own no-speech backstop
          setMicState('idle');
          // A clip that carried real energy but no words is a different problem
          // from one the gate rejected, and "try again" is the wrong advice for
          // someone who simply spoke too quietly.
          showSttBanner(quietHint(data.rms), 'warn');
          return;
        }
        hideSttBanner();
        insertTranscript(data.text);
        setMicState('idle');
      } catch (e) {
        if (e && e.name === 'AbortError') {
          // A user cancel and a 4-minute timeout both surface as AbortError.
          // Only the signal's reason tells them apart, and reporting a timeout
          // as "cancelled" blames the user for something they did not do.
          const why = composerAbort && composerAbort.signal && composerAbort.signal.reason;
          setMicState('idle');
          if (why === 'timeout') showSttBanner('Transcription timed out — the engine did not respond.', 'err');
          else showSttBanner('Transcription cancelled.', 'warn');
        } else {
          offerWebFallback(e.message || 'network error');
        }
      } finally {
        clearTimeout(slowTimer); clearTimeout(killTimer); composerAbort = null;
      }
    };
    try {
      rec.start();
      setMicState('recording');
      showViz('wave', 'listening…', myStream);
    } catch (e) { stopTracks(myStream); showSttBanner('Could not start recording: ' + e.message, 'err'); setMicState('idle'); }
    localStarting = false;   // recording is live (or failed) — allow the next action
  }
  function stopLocalDictation() {
    if (!mediaRec || mediaRec.state === 'inactive') return;
    setMicState('stopping');   // onstop is async — hold the gap shut until it fires
    try { mediaRec.stop(); } catch (_) { stopTracks(); setMicState('idle'); }
  }

  function micToggle() {
    if (micPhase === 'working') {   // transcribing → click cancels
      if (composerAbort) { try { composerAbort.abort('cancel'); } catch (_) {} }
      return;
    }
    // Teardown or the permission sheet is in flight. Both resolve
    // asynchronously, and acting now starts a second capture on top of a take
    // that has not finished releasing the microphone.
    if (micPhase === 'stopping' || micPhase === 'opening' || localStarting) return;
    if (micPhase === 'recording') { stopWebDictation(); stopLocalDictation(); return; }
    hideSttBanner();
    if (!window.isSecureContext) {
      showSttBanner('Dictation needs HTTPS or localhost (this page is insecure). Use “tailscale serve” for HTTPS on your phone.', 'err');
      return;
    }
    if (state.sttMode === 'web') startWebDictation();
    else startLocalDictation();
  }
  if (micBtn) micBtn.addEventListener('click', micToggle);

  async function sendMessage() {
    let text = input.value.trim();
    const readyAtt = state.pendingAttachments.filter(a => a.id && !a.uploading);
    if (state.pendingAttachments.some(a => a.uploading)) {
      alert('wait for image upload to finish'); return;
    }
    if (!text && readyAtt.length === 0) return;
    const resolved = resolveMentions(input.value);
    const mentionIds = resolved.map(m => m.id);
    // DM mode: always include the DM target so the agent sees the message
    // (even if the operator forgot the @mention). Also prepend the visible
    // @name to the content so it's unambiguous in main-tab backscroll — the
    // composer doesn't need to show it; it's added at send time.
    if (state.dmTargetId) {
      if (!mentionIds.includes(state.dmTargetId)) mentionIds.push(state.dmTargetId);
      const tgt = state.members.get(state.dmTargetId);
      const tgtName = tgt ? tgt.name : state.dmTargetId;
      const atTag = '@' + tgtName;
      if (!text.toLowerCase().startsWith(atTag.toLowerCase())) {
        text = atTag + ' ' + text;
      }
    } else if (state.selectedTargets.size > 0) {
      // Persistent target bar: prepend @name for each selected agent that
      // the typed content doesn't already mention, and make sure all
      // selected ids end up in mentionIds so the server-side wake logic
      // fires. Selection is not cleared after send — it's sticky.
      const tags = [];
      for (const id of state.targetOrder) {
        if (!state.selectedTargets.has(id)) continue;
        if (!mentionIds.includes(id)) mentionIds.push(id);
        const m = state.members.get(id);
        if (!m) continue;
        const atTag = '@' + m.name;
        if (text.toLowerCase().includes(atTag.toLowerCase())) continue;
        tags.push(atTag);
      }
      if (tags.length > 0) text = tags.join(' ') + ' ' + text;
    }
    sendBtn.disabled = true;
    try {
      const r = await fetch('/api/send' + API_QS, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: text, mentions: mentionIds,
                               attachment_ids: readyAtt.map(a => a.id) }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ error: 'unknown' }));
        // A rejected relink means the server already consumed these ids on an
        // earlier attempt whose response we lost — the images DID post. Drop
        // them rather than leaving a composer that can never send again and
        // that invites the user to delete images they actually published.
        if (/already-linked/.test(err.error || '')) {
          dropAttachments(readyAtt);
          renderAttachStrip();
          alert('Those images were already posted — the earlier send did go '
                + 'through even though it reported an error. Removed them from '
                + 'the composer; your text is still here.');
        } else {
          alert('send failed: ' + (err.error || r.status));
        }
        return;
      }
      input.value = '';
      // Web dictation rebuilds the composer from its own anchor on every
      // result. Without this it would re-type the message just sent, behind
      // the user, into the now-empty box.
      if (sttReanchor) sttReanchor();
      // Splice out exactly what we sent. Reassigning to [] would also destroy
      // an image pasted DURING the in-flight send: its upload completes into a
      // slot no longer in the array, so it vanishes from the strip with no
      // error and is orphaned server-side.
      dropAttachments(readyAtt);
      renderAttachStrip();
      autoResizeInput();
      state.completion.visible = false;
      renderCompletions();
      updatePreview();
    } catch (e) {
      alert('send failed: ' + e.message);
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  // ── Key handling ──
  input.addEventListener('keydown', (e) => {
    if (state.completion.visible) {
      if (e.key === 'ArrowDown') {
        state.completion.index = (state.completion.index + 1) % state.completion.items.length;
        renderCompletions(); e.preventDefault(); return;
      }
      if (e.key === 'ArrowUp') {
        state.completion.index = (state.completion.index - 1 + state.completion.items.length)
                                 % state.completion.items.length;
        renderCompletions(); e.preventDefault(); return;
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && state.completion.items.length > 0)) {
        acceptCompletion(); e.preventDefault(); return;
      }
      if (e.key === 'Escape') {
        state.completion.visible = false; renderCompletions();
        e.preventDefault(); return;
      }
    }
    if (e.altKey && !e.ctrlKey && !e.metaKey && !state.dmTargetId) {
      if (e.key >= '1' && e.key <= '9') {
        const idx = parseInt(e.key, 10) - 1;
        const id = state.targetOrder[idx];
        if (id) { toggleTarget(id); e.preventDefault(); return; }
      }
      if (e.key === '0') {
        if (state.selectedTargets.size > 0) {
          state.selectedTargets.clear();
          savePersistedTargets();
          renderComposerTargets();
          updatePreview();
        }
        e.preventDefault(); return;
      }
      if (e.key === 'a' || e.key === 'A') {
        toggleAllTargets(); e.preventDefault(); return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  input.addEventListener('input', () => {
    autoResizeInput();
    refreshCompletions();
    updatePreview();
  });
  input.addEventListener('scroll', () => {
    if (!inputHighlight) return;
    inputHighlight.scrollTop = input.scrollTop;
    inputHighlight.scrollLeft = input.scrollLeft;
  });
  // IME / dead-key / emoji composition: the provisional (pre-commit) glyphs are
  // drawn by the browser in the textarea itself, which is normally transparent
  // (the colored mirror is what shows). Reveal the textarea and hide the mirror
  // for the duration of composition so the preview is visible; on commit, revert
  // and re-render the mirror from the now-updated value.
  input.addEventListener('compositionstart', () => {
    input.style.color = 'var(--fg)';
    // visibility, not colour: a mention chip sets its own colour, so it would
    // stay painted over the revealed textarea and double the token.
    if (inputHighlight) inputHighlight.classList.add('composing');
  });
  input.addEventListener('compositionend', () => {
    input.style.color = '';
    if (inputHighlight) inputHighlight.classList.remove('composing');
    updatePreview();
  });
  sendBtn.addEventListener('click', sendMessage);

  // ── Filter ──
  function setFilter(q) {
    state.filter = (q || '').toLowerCase();
    filterEl.value = q || '';
    filterBanner.classList.toggle('active', !!state.filter);
    if (state.filter) filterBanner.textContent = `filter: “${q}” — click to clear`;
    applyFilterToAll();
  }
  function applyFilterToAll() {
    for (const node of chat.children) applyFilterToNode(node);
    // Re-anchor the unread divider to the first still-visible unread message
    // (a filter may have hidden the one it was sitting before).
    refreshUnreadDivider();
  }
  function applyFilterToNode(node) {
    // Skip non-message children (e.g. the unread divider) — they have no msgId.
    if (!node.dataset || node.dataset.msgId === undefined) return;
    if (!state.filter) { node.classList.remove('filtered-out'); return; }
    const hit = (node.dataset.search || '').includes(state.filter);
    node.classList.toggle('filtered-out', !hit);
  }
  function isRelevantInDm(m) {
    // Conversation between operator and DM target:
    //  • authored by target → must @mention operator
    //  • authored by operator → must @mention target
    //  • system notices about this target (e.g. task claims) stay visible
    if (!state.dmTargetId) return true;
    const ms = m.mentions || [];
    if (m.member_id === state.dmTargetId && ms.includes(state.operator.id)) return true;
    if (m.member_id === state.operator.id && ms.includes(state.dmTargetId)) return true;
    return false;
  }
  function applyDmFilterToNode(node, m) {
    if (!state.dmTargetId) { node.classList.remove('dm-hidden'); return; }
    node.classList.toggle('dm-hidden', !isRelevantInDm(m));
  }
  function refreshDmVisibility() {
    for (const [id, dom] of state.messageDomById) {
      const m = state.messages.get(id);
      if (m) applyDmFilterToNode(dom, m);
    }
  }
  filterEl.addEventListener('input', () => setFilter(filterEl.value));
  filterBanner.addEventListener('click', () => setFilter(''));

  // ── Compact toggle ──
  btnCompact.addEventListener('click', () => {
    state.compact = !state.compact;
    btnCompact.classList.toggle('on', state.compact);
    for (const [id, dom] of state.messageDomById) applyCompactClass(dom, id);
  });

  // ── Message-number toggle (#N in the left gutter) ──
  // Persists per-origin via localStorage, default ON. Toggling just flips a
  // class on #chat; pure-CSS sticky positioning handles the rest (see .msg-num).
  let msgNumsOn = true;
  try { msgNumsOn = localStorage.getItem('trio.msgNumbers') !== '0'; } catch (_) {}
  function applyMsgNums() {
    chat.classList.toggle('show-msg-nums', msgNumsOn);
    btnMsgNum.classList.toggle('on', msgNumsOn);
  }
  applyMsgNums();
  btnMsgNum.addEventListener('click', () => {
    msgNumsOn = !msgNumsOn;
    try { localStorage.setItem('trio.msgNumbers', msgNumsOn ? '1' : '0'); } catch (_) {}
    applyMsgNums();
  });

  // ── Notify toggle ──
  btnNotify.addEventListener('click', async () => {
    if (!('Notification' in window)) {
      alert('This browser does not support desktop notifications.');
      return;
    }
    if (!state.notifyEnabled) {
      if (Notification.permission === 'default') {
        const r = await Notification.requestPermission();
        if (r !== 'granted') return;
      } else if (Notification.permission === 'denied') {
        alert('Notifications are blocked by the browser. Enable them in site settings.');
        return;
      }
      state.notifyEnabled = true;
      btnNotify.textContent = '🔔 on';
      btnNotify.classList.add('on');
    } else {
      state.notifyEnabled = false;
      btnNotify.textContent = '🔔 off';
      btnNotify.classList.remove('on');
    }
    if (typeof syncSettingVisibility === 'function') syncSettingVisibility();
  });

  // ── Chime (WebAudio, no audio asset — synthesized on the fly) ──
  let _audioCtx = null;
  function ensureAudio() {
    if (_audioCtx) return _audioCtx;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      _audioCtx = AC ? new AC() : null;
    } catch (_) { _audioCtx = null; }
    return _audioCtx;
  }
  // Does a peer message qualify for the chime under the current scope?
  //   'all'     → every peer message chimes.
  //   'mention' → only messages that @mention the operator chime.
  // Pure (no DOM/state) so it can be unit-tested via the harness hook. The
  // on/off master is state.soundEnabled + the btn-sound pill; this only refines
  // an already-enabled chime, and stays independent of notifyScope.
  function chimeScopeAllows(scope, mentionsOperator) {
    return scope === 'all' ? true : !!mentionsOperator;
  }
  // The whole chime decision, pure and testable. The gate that actually
  // matters is not the scope predicate but the conditions around it: the
  // history burst, your own messages, system notices, and a DM view where the
  // message is appended but hidden.
  function shouldChime(o) {
    if (!o || o.initialLoad) return false;      // primed history, not live
    if (!o.soundEnabled) return false;
    if (o.isMine || o.isSystem) return false;
    if (!o.dmVisible) return false;             // appended but CSS-hidden
    return chimeScopeAllows(o.scope, o.addressed);
  }
  let _lastChimeAt = 0;
  function playChime() {
    const ctx = ensureAudio();
    if (!ctx) return;
    // Coalesce. A reconnect drains the whole offline backlog through one
    // synchronous handler, and each call ramps a fresh gain to full volume at
    // essentially the same currentTime — forty of those sum into clipping
    // rather than forty chimes. One sound per burst is the useful signal.
    const nowMs = Date.now();
    if (nowMs - _lastChimeAt < 400) return;
    _lastChimeAt = nowMs;
    if (ctx.state === 'suspended') { try { ctx.resume(); } catch (_) {} }
    const vol = Math.max(0, Math.min(1, state.chimeVolume));
    if (vol <= 0) return;
    try {
      const now = ctx.currentTime;
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(vol, now + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.40);
      gain.connect(ctx.destination);
      // two-note ping: E6 -> A6
      [[1318.51, 0], [1760.0, 0.09]].forEach(([freq, t]) => {
        const osc = ctx.createOscillator();
        osc.type = 'sine';
        osc.frequency.value = freq;
        osc.connect(gain);
        osc.start(now + t);
        osc.stop(now + t + 0.28);
      });
    } catch (_) { /* ignore */ }
  }

  // ── Sound (chime) toggle — off by default; the pill is the on/off master and
  //    state.soundScope (settings drawer) refines which peer messages chime. ──
  btnSound.addEventListener('click', () => {
    state.soundEnabled = !state.soundEnabled;
    btnSound.textContent = state.soundEnabled ? '🔊 on' : '🔊 off';
    btnSound.classList.toggle('on', state.soundEnabled);
    try { localStorage.setItem('trio.sound', state.soundEnabled ? '1' : '0'); } catch (_) {}
    // The click is a user gesture — unlock the AudioContext and preview the chime.
    if (state.soundEnabled) { ensureAudio(); playChime(); }
    if (typeof syncSettingVisibility === 'function') syncSettingVisibility();
  });
  // Restore persisted preference (audio stays suspended until the first gesture).
  try {
    if (localStorage.getItem('trio.sound') === '1') {
      state.soundEnabled = true;
      btnSound.textContent = '🔊 on';
      btnSound.classList.add('on');
    }
  } catch (_) {}

  // ── Sidebar collapse toggle — persisted; 'on' pill state == roster visible ──
  const btnSide = document.getElementById('btn-side');
  const appEl = document.getElementById('app');
  function applySidebar(collapsed) {
    appEl.classList.toggle('side-collapsed', collapsed);
    btnSide.classList.toggle('on', !collapsed);
  }
  let _sideCollapsed = false;
  try { _sideCollapsed = localStorage.getItem('trio.sideCollapsed') === '1'; } catch (_) {}
  applySidebar(_sideCollapsed);
  function toggleSidebar() {
    _sideCollapsed = !_sideCollapsed;
    applySidebar(_sideCollapsed);
    try { localStorage.setItem('trio.sideCollapsed', _sideCollapsed ? '1' : '0'); } catch (_) {}
  }
  btnSide.addEventListener('click', () => {
    if (window.innerWidth <= 768) { toggleMobileSidebar(); } else { toggleSidebar(); }
  });
  // Keyboard shortcut: Ctrl+B toggles the roster sidebar (editor convention).
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey &&
        (e.key === 'b' || e.key === 'B')) {
      e.preventDefault();
      if (window.innerWidth <= 768) { toggleMobileSidebar(); } else { toggleSidebar(); }
    }
  });

  // ── Mobile sidebar: overlay with scrim ──
  const mobileScrim = document.getElementById('mobile-scrim');
  const btnMobileRoster = document.getElementById('btn-mobile-roster');
  const btnSideClose = document.getElementById('side-close');
  function closeMobileSidebar() {
    appEl.classList.remove('mobile-side-open');
    btnSide.classList.toggle('on', false);
    if (btnMobileRoster) btnMobileRoster.classList.toggle('on', false);
  }
  function toggleMobileSidebar() {
    const open = appEl.classList.toggle('mobile-side-open');
    btnSide.classList.toggle('on', open);
    if (btnMobileRoster) btnMobileRoster.classList.toggle('on', open);
  }
  // The in-sidebar close control picks the same path as the header pill.
  function closeSidebar() {
    if (window.innerWidth <= 768) { closeMobileSidebar(); }
    else if (!_sideCollapsed) { toggleSidebar(); }
  }
  if (btnMobileRoster) btnMobileRoster.addEventListener('click', toggleMobileSidebar);
  if (btnSideClose) btnSideClose.addEventListener('click', closeSidebar);
  if (mobileScrim) mobileScrim.addEventListener('click', closeMobileSidebar);
  // Auto-collapse sidebar on narrow viewports at load
  if (window.innerWidth <= 768) {
    applySidebar(true);
  }

  // ── Settings panel: relocate controls out of the header into a ⚙ drawer ──
  // appendChild MOVES the live elements, so every existing handler/state stays
  // intact — no rewiring, no reproducing the font list.
  const btnSettings = document.getElementById('btn-settings');
  const settingsPanel = document.getElementById('settings-panel');
  [
    ['Theme', 'theme-picker'],
    ['Message font', 'font-picker'],
    ['Roster sidebar', 'btn-side'],
    ['Compact messages', 'btn-compact'],
    ['Message numbers', 'btn-msgnum'],
    ['Desktop notifications', 'btn-notify'],
    ['Chime on new message', 'btn-sound'],
  ].forEach(([labelText, id]) => {
    const el = document.getElementById(id);
    if (!el) return;
    const row = document.createElement('div');
    row.className = 'set-row';
    const lab = document.createElement('span');
    lab.textContent = labelText;
    row.appendChild(lab);
    row.appendChild(el);
    settingsPanel.appendChild(row);
  });

  // Extra settings built here (not relocated): chime volume + notify prefs.
  function addSettingRow(labelText, controlEl) {
    const row = document.createElement('div');
    row.className = 'set-row';
    const lab = document.createElement('span');
    lab.textContent = labelText;
    row.appendChild(lab);
    row.appendChild(controlEl);
    settingsPanel.appendChild(row);
    return row;
  }

  // Build a <select> preloaded with `options` ([value, label] pairs) and the
  // `current` value pre-selected. Shared by the chime + notification prefs.
  function prefSelect(options, current) {
    const sel = document.createElement('select');
    options.forEach(([val, label]) => {
      const o = document.createElement('option');
      o.value = val; o.textContent = label;
      if (val === current) o.selected = true;
      sel.appendChild(o);
    });
    return sel;
  }

  // Chime scope — off is the btn-sound pill; this refines an enabled chime to
  // fire on every message or only @mentions. Independent of the notify scope.
  try {
    const ss = localStorage.getItem('trio.soundScope'); if (ss) state.soundScope = ss;
  } catch (_) {}
  // Wording ('all messages' / '@mentions only') and the mention-first vs
  // all-first default are matched to the notify-scope select so the two read as
  // siblings; the title spells out that they're independent controls.
  const soundScopeSel = prefSelect(
    [['all', 'all messages'], ['mention', '@mentions only']], state.soundScope);
  soundScopeSel.title = 'Chime scope — independent of desktop notifications';
  soundScopeSel.addEventListener('change', () => {
    state.soundScope = soundScopeSel.value;
    try { localStorage.setItem('trio.soundScope', state.soundScope); } catch (_) {}
  });
  const soundScopeRow = addSettingRow('Chime for', soundScopeSel);

  // Chime volume slider — drives state.chimeVolume; previews on release.
  try {
    const sv = parseFloat(localStorage.getItem('trio.chimeVolume'));
    if (!isNaN(sv)) state.chimeVolume = Math.max(0, Math.min(1, sv));
  } catch (_) {}
  const volSlider = document.createElement('input');
  volSlider.type = 'range';
  volSlider.min = '0'; volSlider.max = '1'; volSlider.step = '0.01';
  volSlider.value = String(state.chimeVolume);
  volSlider.addEventListener('input', () => {
    state.chimeVolume = parseFloat(volSlider.value) || 0;
    try { localStorage.setItem('trio.chimeVolume', String(state.chimeVolume)); } catch (_) {}
  });
  volSlider.addEventListener('change', () => { ensureAudio(); playChime(); });
  const chimeVolRow = addSettingRow('Chime volume', volSlider);

  // Notification preference dropdowns (reuse prefSelect defined above).
  try {
    const ns = localStorage.getItem('trio.notifyScope'); if (ns) state.notifyScope = ns;
    const nw = localStorage.getItem('trio.notifyWhen'); if (nw) state.notifyWhen = nw;
  } catch (_) {}
  const notifyScopeSel = prefSelect(
    [['mention', '@mentions only'], ['all', 'all messages']], state.notifyScope);
  notifyScopeSel.addEventListener('change', () => {
    state.notifyScope = notifyScopeSel.value;
    try { localStorage.setItem('trio.notifyScope', state.notifyScope); } catch (_) {}
  });
  const notifyScopeRow = addSettingRow('Notify for', notifyScopeSel);
  const notifyWhenSel = prefSelect(
    [['hidden', 'tab in background'], ['always', 'always']], state.notifyWhen);
  notifyWhenSel.addEventListener('change', () => {
    state.notifyWhen = notifyWhenSel.value;
    try { localStorage.setItem('trio.notifyWhen', state.notifyWhen); } catch (_) {}
  });
  const notifyWhenRow = addSettingRow('Notify when', notifyWhenSel);

  // ── Transcription (speech-to-text) ──
  // Main panel keeps a SINGLE control (the mode). Status + Test live on their
  // own sub-page, opened via "Test ›".
  try { const sm = localStorage.getItem('trio.sttMode'); if (sm === 'web' || sm === 'local') state.sttMode = sm; } catch (_) {}
  // Labels stay short: the panel is max-width 320px and the longer wording
  // pushed the Test button 39px outside it, wrapping "Test ›" onto two lines.
  const sttModeSel = prefSelect(
    [['local', 'local — on-device'], ['web', 'web — browser']], state.sttMode);
  sttModeSel.addEventListener('change', () => {
    state.sttMode = sttModeSel.value;
    try { localStorage.setItem('trio.sttMode', state.sttMode); } catch (_) {}
    updateSttEntry();
    updateSttModeNote();
  });
  const sttOpenBtn = document.createElement('button');
  sttOpenBtn.className = 'pill';
  sttOpenBtn.textContent = 'Test ›';
  sttOpenBtn.title = 'check local transcription works';
  const sttDictWrap = document.createElement('div');
  sttDictWrap.style.display = 'flex';
  sttDictWrap.style.gap = '8px';
  sttDictWrap.style.alignItems = 'center';
  sttDictWrap.appendChild(sttModeSel);
  sttDictWrap.appendChild(sttOpenBtn);
  addSettingRow('Dictation', sttDictWrap);
  // The only privacy warning used to live on the fallback path — the one the
  // user did NOT choose. Someone who picks web mode deliberately deserves to
  // know where their voice goes just as much.
  const sttModeNote = document.createElement('div');
  sttModeNote.className = 'stt-mode-note';
  const sttModeNoteRow = addSettingRow('', sttModeNote);
  function updateSttModeNote() {
    sttModeNote.textContent = (state.sttMode === 'web')
      ? 'Browser dictation sends your audio to your browser vendor.'
      : 'Audio is transcribed on the server and never leaves it.';
  }
  updateSttModeNote();

  // Sub-page: back link, status, test recorder (waveform → spinner → result).
  const sttPage = document.createElement('div');
  sttPage.id = 'settings-stt-page';
  const sttBack = document.createElement('button');
  sttBack.className = 'stt-back';
  sttBack.textContent = '‹ Settings';
  const sttPageTitle = document.createElement('h3');
  sttPageTitle.textContent = 'Local transcription';
  const sttStatus = document.createElement('div');
  sttStatus.className = 'stt-status';
  sttStatus.textContent = '…';
  const sttTestBtn = document.createElement('button');
  sttTestBtn.className = 'pill';
  sttTestBtn.innerHTML = ICON_MIC + ' Test';
  sttTestBtn.title = 'record a short clip and transcribe it locally';
  const sttTestVizWrap = document.createElement('div');
  sttTestVizWrap.className = 'stt-testviz';
  sttTestVizWrap.hidden = true;
  const sttTestWave = document.createElement('canvas');
  sttTestWave.id = 'stt-test-wave'; sttTestWave.width = 260; sttTestWave.height = 30;
  const sttTestSpin = document.createElement('div');
  sttTestSpin.className = 'stt-spinner'; sttTestSpin.hidden = true;
  const sttTestVizLabel = document.createElement('span');
  sttTestVizLabel.className = 'stt-viz-label';
  sttTestVizWrap.appendChild(sttTestWave);
  sttTestVizWrap.appendChild(sttTestSpin);
  sttTestVizWrap.appendChild(sttTestVizLabel);
  const sttTestOut = document.createElement('div');
  sttTestOut.className = 'stt-test-out';
  sttPage.appendChild(sttBack);
  sttPage.appendChild(sttPageTitle);
  sttPage.appendChild(sttStatus);
  sttPage.appendChild(sttTestBtn);
  sttPage.appendChild(sttTestVizWrap);
  sttPage.appendChild(sttTestOut);
  settingsPanel.appendChild(sttPage);

  const testWave = makeWaveform(sttTestWave);

  function openSttPage() { settingsPanel.classList.add('stt-page-open'); refreshSttStatus(); }
  function closeSttPage() { stopTestRecording(); settingsPanel.classList.remove('stt-page-open'); }
  sttOpenBtn.addEventListener('click', openSttPage);
  sttBack.addEventListener('click', closeSttPage);

  // The test is local-only; hide its entry in web mode.
  function updateSttEntry() { sttOpenBtn.hidden = (state.sttMode !== 'local'); }
  updateSttEntry();

  async function refreshSttStatus() {
    sttStatus.textContent = 'checking…'; sttStatus.className = 'stt-status';
    try {
      const r = await fetch('/api/stt/health');
      const d = await r.json();
      if (d.available) {
        sttStatus.textContent = (d.warm ? '✓ ready (warm) — ' : '✓ ready — ') + (d.model || '');
        sttStatus.className = 'stt-status ok';
      } else {
        sttStatus.textContent = '✗ ' + (d.detail || 'unavailable');
        sttStatus.className = 'stt-status err';
      }
    } catch (e) {
      sttStatus.textContent = '✗ health check failed';
      sttStatus.className = 'stt-status err';
    }
  }

  // Test recorder: waveform while recording, spinner while transcribing.
  let sttTestRec = null, sttTestChunks = [], sttTestStream = null, sttTestRecording = false;
  let sttTestStarting = false, sttTestCancelled = false;
  // Cancel an in-progress test (mic OFF, no transcription). Used when leaving the
  // test page or closing the settings drawer so the microphone never stays hot.
  function stopTestRecording() {
    // sttTestStarting covers the window where getUserMedia has been called but
    // not resolved — i.e. exactly while the permission sheet is up. Returning
    // early there without setting the cancelled flag let the recorder start
    // AFTER the drawer had closed, with no visible indicator anywhere.
    if (sttTestStarting) sttTestCancelled = true;
    if (!sttTestRecording && !sttTestStream && !sttTestStarting) return;
    sttTestCancelled = true;
    sttTestRecording = false;
    testWave.stop();
    if (sttTestRec && sttTestRec.state !== 'inactive') { try { sttTestRec.stop(); } catch (_) {} }
    if (sttTestStream) { try { sttTestStream.getTracks().forEach(t => t.stop()); } catch (_) {} sttTestStream = null; }
    sttTestVizWrap.hidden = true; sttTestSpin.hidden = true; sttTestWave.hidden = false; sttTestVizLabel.textContent = '';
    sttTestBtn.innerHTML = ICON_MIC + ' Test';
    sttTestOut.textContent = ''; sttTestOut.className = 'stt-test-out';
  }
  sttTestBtn.addEventListener('click', async () => {
    if (sttTestRecording) {   // "Stop" → finalize + transcribe (the actual test)
      sttTestRecording = false;
      if (sttTestRec && sttTestRec.state !== 'inactive') { try { sttTestRec.stop(); } catch (_) {} }
      return;
    }
    if (sttTestStarting) return;   // ignore a second click before the mic opens
    sttTestStarting = true;
    sttTestCancelled = false;
    sttTestOut.textContent = ''; sttTestOut.className = 'stt-test-out';
    if (!window.isSecureContext) { sttTestStarting = false; sttTestOut.textContent = 'Dictation needs HTTPS or localhost.'; sttTestOut.className = 'stt-test-out err'; return; }
    try { sttTestStream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { sttTestStarting = false; sttTestOut.textContent = 'Microphone permission denied.'; sttTestOut.className = 'stt-test-out err'; return; }
    if (sttTestCancelled) { sttTestStarting = false; try { sttTestStream.getTracks().forEach(t => t.stop()); } catch (_) {} sttTestStream = null; return; }
    sttTestChunks = [];
    try { sttTestRec = new MediaRecorder(sttTestStream); }
    catch (e) { sttTestStarting = false; sttTestOut.textContent = 'Recording unsupported.'; sttTestOut.className = 'stt-test-out err'; sttTestStream.getTracks().forEach(t => t.stop()); sttTestStream = null; return; }
    sttTestRec.ondataavailable = (e) => { if (e.data && e.data.size) sttTestChunks.push(e.data); };
    sttTestRec.onstop = async () => {
      if (sttTestCancelled) { sttTestCancelled = false; return; }   // cancelled → no transcription
      if (sttTestStream) { try { sttTestStream.getTracks().forEach(t => t.stop()); } catch (_) {} sttTestStream = null; }
      const peak = testWave.getPeak();
      testWave.stop();
      sttTestBtn.innerHTML = ICON_MIC + ' Test';
      const blob = new Blob(sttTestChunks, { type: (sttTestRec && sttTestRec.mimeType) || 'audio/webm' });
      if (peak >= 0 && peak < STT_SILENCE_PEAK) {   // silent — no round trip
        sttTestVizWrap.hidden = true; sttTestSpin.hidden = true; sttTestWave.hidden = false; sttTestVizLabel.textContent = '';
        sttTestOut.textContent = 'No message detected — try again.';
        sttTestOut.className = 'stt-test-out err';
        return;
      }
      sttTestWave.hidden = true; sttTestSpin.hidden = false; sttTestVizLabel.textContent = 'transcribing…';
      sttTestOut.textContent = ''; sttTestOut.className = 'stt-test-out';
      const ctrl = new AbortController();
      const killTimer = setTimeout(() => { try { ctrl.abort('timeout'); } catch (_) {} }, STT_FETCH_TIMEOUT_MS);
      try {
        const r = await fetch('/api/stt/transcribe', { method: 'POST', headers: { 'Content-Type': blob.type || 'audio/webm' }, body: blob, signal: ctrl.signal });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
          if (d.no_speech || !(d.text || '').trim()) {
            sttTestOut.textContent = 'No message detected — try again.';
            sttTestOut.className = 'stt-test-out err';
          } else {
            sttTestOut.textContent = '✓ “' + d.text + '”' + (d.seconds != null ? ' (' + d.seconds + 's)' : '');
            sttTestOut.className = 'stt-test-out ok';
          }
        } else {
          // Same humanizer as the composer banner — otherwise the identical
          // failure is described one way here and another way there.
          sttTestOut.textContent = '✗ ' + humanizeSttError(d.error || ('HTTP ' + r.status));
          sttTestOut.className = 'stt-test-out err';
        }
      } catch (e) {
        sttTestOut.textContent = (e && e.name === 'AbortError') ? '✗ timed out' : ('✗ ' + (e.message || 'failed'));
        sttTestOut.className = 'stt-test-out err';
      } finally {
        clearTimeout(killTimer);
      }
      sttTestVizWrap.hidden = true; sttTestSpin.hidden = true; sttTestWave.hidden = false; sttTestVizLabel.textContent = '';
      refreshSttStatus();
    };
    // The one recorder start that used to run bare. A throw here (some mobile
    // Safari builds raise NotSupportedError) left sttTestStarting latched true
    // and the stream open: a dead Test button and a live microphone.
    try { sttTestRec.start(); }
    catch (e) {
      sttTestStarting = false;
      try { sttTestStream.getTracks().forEach(t => t.stop()); } catch (_) {}
      sttTestStream = null;
      sttTestOut.textContent = 'Recording unsupported here.';
      sttTestOut.className = 'stt-test-out err';
      return;
    }
    sttTestRecording = true; sttTestStarting = false;
    sttTestBtn.innerHTML = ICON_STOP + ' Stop';
    sttTestVizWrap.hidden = false; sttTestWave.hidden = false; sttTestSpin.hidden = true; sttTestVizLabel.textContent = 'listening…';
    sttTestOut.textContent = '';
    testWave.start(sttTestStream);
  });

  // Sub-settings only show when their parent feature is enabled.
  function syncSettingVisibility() {
    if (soundScopeRow) soundScopeRow.hidden = !state.soundEnabled;
    if (chimeVolRow) chimeVolRow.hidden = !state.soundEnabled;
    if (notifyScopeRow) notifyScopeRow.hidden = !state.notifyEnabled;
    if (notifyWhenRow) notifyWhenRow.hidden = !state.notifyEnabled;
  }
  syncSettingVisibility();

  function toggleSettings(force) {
    const show = (force !== undefined) ? force : settingsPanel.hasAttribute('hidden');
    // Closing always cancels a running mic test — the drawer can be dismissed by
    // Escape or an outside click, and neither should leave the microphone hot.
    if (show) { settingsPanel.classList.remove('stt-page-open'); settingsPanel.removeAttribute('hidden'); btnSettings.classList.add('on'); }
    else { stopTestRecording(); settingsPanel.setAttribute('hidden', ''); btnSettings.classList.remove('on'); }
  }
  btnSettings.addEventListener('click', (e) => { e.stopPropagation(); toggleSettings(); });
  document.addEventListener('click', (e) => {
    if (settingsPanel.hasAttribute('hidden')) return;
    if (settingsPanel.contains(e.target) || btnSettings.contains(e.target)) return;
    toggleSettings(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !settingsPanel.hasAttribute('hidden')) toggleSettings(false);
  });

  // ── Jump-to-latest + unread counter ──
  // ── Unread divider ──
  // Count / locate unread (id > lastSeenId), skipping filtered/DM-hidden nodes
  // so the divider + "new" bar stay in sync with what's actually shown.
  function isHiddenMsg(dom) {
    return dom.classList.contains('filtered-out') || dom.classList.contains('dm-hidden');
  }
  // You cannot have unread your own message. Sending while scrolled up used to
  // raise a "new messages" divider above your own post and add it to the
  // counter, because unread was decided purely by id > lastSeenId.
  //
  // Skipped at the point of COUNTING rather than by advancing lastSeenId past
  // it. The watermark is a single high-water mark: moving it over your own
  // message would also mark every earlier message read, so a peer's message
  // that arrived while you were scrolled up would vanish from the divider
  // merely because you replied to something else.
  function isOwnMsg(dom) {
    return !!state.operator.id && dom.dataset.sender === state.operator.id;
  }
  function firstVisibleUnreadDom() {
    for (const id of [...state.messageDomById.keys()].sort((a, b) => a - b)) {
      if (id <= state.lastSeenId) continue;
      const dom = state.messageDomById.get(id);
      if (dom && !isHiddenMsg(dom) && !isOwnMsg(dom)) return dom;
    }
    return null;
  }
  function unreadCountVisible() {
    let n = 0;
    for (const [id, dom] of state.messageDomById) {
      if (id > state.lastSeenId && !isHiddenMsg(dom) && !isOwnMsg(dom)) n++;
    }
    return n;
  }
  // Draw a "new messages" line before the first *visible* unread message.
  function refreshUnreadDivider() {
    const old = document.getElementById('unread-divider');
    if (old) old.remove();
    if (state.lastSeenId) {
      const dom = firstVisibleUnreadDom();
      if (dom) {
        const bar = document.createElement('div');
        bar.id = 'unread-divider';
        bar.className = 'unread-divider';
        bar.textContent = 'new messages';
        chat.insertBefore(bar, dom);
      }
    }
    updateNewBar();
  }
  // Establish the read watermark once, when the history burst settles — the
  // only place that works when the channel was opened in a background tab,
  // which landing mode makes the normal way in.
  //
  // Everything already on screen counts as seen, so you arrive caught up
  // rather than staring at a divider above the entire history. If the server
  // has a last_read for this operator it wins, but note that nth_web.py does
  // not currently write members.last_read for web operators (ensure_operator_row
  // inserts 0 and only last_seen is updated), so in practice this resolves to
  // "newest" today. The branch is here so that persisting a web operator's
  // read position starts working without touching this function.
  function seedBaseline() {
    if (state.lastSeenId) return;
    // reduce(), not Math.max(...spread) — a long channel would exceed the
    // argument limit and throw RangeError.
    const newest = [...state.messageDomById.keys()]
      .reduce((a, b) => (b > a ? b : a), 0);
    const me = state.members.get(state.operator.id);
    const serverLastRead = me ? (me.last_read || 0) : 0;
    state.lastSeenId = serverLastRead > 0 ? Math.min(serverLastRead, newest) : newest;
    refreshUnreadDivider();
  }

  // The user caught up — advance the watermark over the messages they could
  // actually have read, and clear the divider.
  //
  // lastSeenId is a single high-water mark, so it must never jump OVER an
  // unread message the user has not seen. Two kinds of hidden message need
  // opposite treatment:
  //   • filtered-out — the user's own filter is hiding it temporarily. Stop
  //     here. Advancing past it would mark it read because they searched for
  //     something else, and clearing the filter would silently lose it.
  //   • dm-hidden — structurally not part of this view at all. Skip it; if it
  //     blocked the walk the watermark could never advance past it again.
  function markCaughtUp() {
    let mark = state.lastSeenId;
    for (const id of [...state.messageDomById.keys()].sort((a, b) => a - b)) {
      if (id <= state.lastSeenId) continue;
      const dom = state.messageDomById.get(id);
      if (dom.classList.contains('filtered-out')) break;
      mark = id;
    }
    state.lastSeenId = mark;
    if (!unreadCountVisible()) {
      const bar = document.getElementById('unread-divider');
      if (bar) bar.remove();
    }
    updateNewBar();
  }
  // Top "N new messages" bar — the conventional jump-to-first-unread affordance.
  // Shown whenever an unread divider exists; clicking scrolls up to it.
  function updateNewBar() {
    if (!newBar) return;
    if (!document.getElementById('unread-divider')) { newBar.classList.remove('show'); return; }
    // "N new messages below" is meaningless when you are already at the bottom
    // looking at them. This happens two ways: the jump-to-unread clamps here
    // when the unread block is shorter than the viewport (and then there is no
    // scroll left to attribute, so nothing marks), and a filter can leave the
    // walk unable to advance past a hidden message beneath a visible one.
    // Hiding the claim is honest in both; the watermark is deliberately
    // untouched, so nothing is marked read on the user's behalf.
    if (chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80) {
      newBar.classList.remove('show');
      return;
    }
    const n = unreadCountVisible();
    newBar.textContent = '↓ ' + n + ' new message' + (n === 1 ? '' : 's');
    newBar.classList.add('show');
  }

  function updateJumpButton() {
    const atBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80;
    if (atBottom) {
      state.jumpUnread = 0;
      jumpBtn.classList.remove('show');
      jumpCount.style.display = 'none';
      // Only a scroll the USER performed means "I have read to here". A
      // scroll event alone does not say who caused it, and the page issues
      // several of its own (the post-burst settle, the jump-to-unread), so
      // attribute the scroll to a recent real gesture instead of racing it
      // against a timer.
      if (!document.hidden && scrollIsUsers()) markCaughtUp();
      return;
    }
    jumpBtn.classList.add('show');
    if (state.jumpUnread > 0) {
      jumpCount.style.display = '';
      jumpCount.textContent = state.jumpUnread;
    } else {
      jumpCount.style.display = 'none';
    }
  }
  // ── "You are here" indicator — operator's emoji on topmost visible
  //    message when scrolled up. Cleared when scrolled back to bottom. ──
  let hereRaf = 0;
  function scheduleHereUpdate() {
    if (hereRaf) return;
    hereRaf = requestAnimationFrame(() => {
      hereRaf = 0;
      updateHereIndicator();
    });
  }
  function updateHereIndicator() {
    // Remove any stale 'here' pins first
    for (const dom of state.messageDomById.values()) {
      const here = dom.querySelector('.watermark-pin.here');
      if (here) here.remove();
    }
    // Only show when user is scrolled up.
    const scrolledUp = chat.scrollHeight - chat.clientHeight - chat.scrollTop >= 80;
    if (!scrolledUp) return;
    if (!state.operator.id) return;

    // Find topmost message whose bottom is below the viewport top.
    const scrollTop = chat.scrollTop;
    let topDom = null;
    for (const dom of state.messageDomById.values()) {
      if (dom.classList.contains('dm-hidden') || dom.classList.contains('filtered-out')) continue;
      if (dom.offsetTop + dom.offsetHeight > scrollTop) { topDom = dom; break; }
    }
    if (!topDom) return;
    const container = topDom.querySelector('.watermark-pins');
    if (!container) return;
    const a = animalFor(state.operator);
    const pin = document.createElement('span');
    pin.className = 'watermark-pin here self';
    pin.textContent = a.emoji;
    pin.title = `you are here — the ${a.name}`;
    container.appendChild(pin);
  }
  // A scroll is "the user's" when a real input gesture on the scroller
  // preceded it. Programmatic scrolls (settle, jump-to-unread) have none, so
  // they can never mark messages read. Bound to the scroller, not the
  // document, so clicking the "new messages" bar is not mistaken for intent.
  function noteIntent() { state.userIntentAt = Date.now(); }
  // True when a scroll happening right now is attributable to the user.
  function scrollIsUsers() { return Date.now() - state.userIntentAt < USER_INTENT_MS; }
  // A scroll the PAGE issues is never the user's, however recently they moved.
  // Called immediately before every programmatic scrollTop assignment: without
  // it a wheel in the preceding USER_INTENT_MS donates its attribution to the
  // animation, and sustainIntent then carries that donation to the bottom.
  function disownScroll() { state.userIntentAt = 0; }
  // Keep an already-attributed scroll attributed while it is still moving.
  // Cannot bootstrap: an unattributed scroll starts stale and stays stale.
  function sustainIntent() { if (scrollIsUsers()) noteIntent(); }
  for (const ev of ['wheel', 'touchstart', 'touchmove', 'pointerdown', 'mousedown']) {
    chat.addEventListener(ev, noteIntent, { passive: true });
  }
  document.addEventListener('keydown', (e) => {
    // Only keys that could plausibly have scrolled the chat. Typing in the
    // composer must not count: boot focuses #input, so a space typed while a
    // programmatic scroll is still gliding would hand it the user's
    // attribution and let it mark the unread read.
    if (e.target && e.target.closest &&
        e.target.closest('input, textarea, select, [contenteditable]')) return;
    if (['PageDown', 'PageUp', 'End', 'Home', 'ArrowDown', 'ArrowUp', ' '].includes(e.key)) noteIntent();
  }, { passive: true });
  chat.addEventListener('scroll', () => {
    // A scroll that is ALREADY the user's keeps its attribution for as long as
    // it keeps moving — iOS momentum routinely runs 1-3s past touchend, and a
    // long smooth scroll can outlast USER_INTENT_MS on its own. This cannot
    // bootstrap a programmatic scroll into attribution: that one starts stale,
    // so the condition is false on its very first frame and stays false.
    sustainIntent();
    updateJumpButton();
    scheduleHereUpdate();
  });
  jumpBtn.addEventListener('click', () => {
    chat.scrollTop = chat.scrollHeight;
    state.jumpUnread = 0;
    if (!document.hidden) markCaughtUp();
    updateJumpButton();
  });
  // Top bar: scroll UP to the first unread message (the divider). Does not mark
  // caught-up — you're going TO the unread, not past it.
  newBar.addEventListener('click', () => {
    const dom = firstVisibleUnreadDom();
    if (!dom) return;
    // #chat is scroll-behavior: smooth, so this starts an animation lasting
    // well over a second on a long channel, and the browser clamps it to the
    // bottom whenever the unread block is shorter than one viewport. Neither
    // is a scroll the user performed, so neither may count as catching up —
    // see USER_INTENT_MS.
    disownScroll();
    chat.scrollTop = Math.max(0, dom.offsetTop - 8);
  });

  // ── Title / tab badge ──
  function updateTitle() {
    const base = state.channel ? `trio#${state.channel}` : state.originalTitle;
    document.title = state.unreadCount > 0 ? `(${state.unreadCount}) ${base}` : base;
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      state.unreadCount = 0;
      updateTitle();
      // Returning to the tab: if already at the bottom, they've caught up;
      // otherwise surface the "new messages" divider for what arrived while away.
      const atBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80;
      if (atBottom) markCaughtUp();
      else refreshUnreadDivider();
      updateJumpButton();
    }
  });
  window.addEventListener('focus', () => {
    state.unreadCount = 0;
    updateTitle();
  });

  // ── SSE ──
  let es = null;
  let reconnectTimer = null;
  function connect() {
    if (es) try { es.close(); } catch (e) {}
    es = new EventSource('/api/events' + API_QS);
    es.onopen = () => {
      // A channel with no history primes zero messages, so appendMessage never
      // fires and nothing would ever clear initialLoad — the first live message
      // would arrive un-chimed. Arm the settle from the connection itself.
      if (state.initialLoad) scheduleInitialSettle();
      hConn.textContent = '● connected';
      hConn.classList.remove('bad');
      hConn.classList.add('ok');
    };
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        if (payload.type === 'message') appendMessage(payload);
        else if (payload.type === 'roster') renderRoster(payload.members);
        // 'context' frames carry the per-host session list. The channel page
        // renders context per-member (roster badge + stats drill-down); the
        // standalone ring sidebar was removed in b771656, so nothing here
        // consumes them. The landing page still renders rings from its own
        // /api/landing poll.
      } catch (e) { console.error('bad event', e); }
    };
    es.onerror = () => {
      hConn.textContent = '● reconnecting…';
      hConn.classList.remove('ok');
      hConn.classList.add('bad');
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 2000);
      }
    };
  }

  // Periodically refresh stats (queue-depth decay, rate window rolls, sparkline).
  setInterval(() => {
    updateChanStats();
    // Re-render stats for any expanded member.
    for (const id of state.expandedMembers) {
      const m = state.members.get(id);
      if (!m) continue;
      const row = [...rosterEl.querySelectorAll('.member')].find(el =>
        el.querySelector('.id')?.textContent === id.slice(0, 8));
      if (row) {
        const stats = row.querySelector('.stats');
        if (stats) stats.innerHTML = renderMemberStatsHTML(m);
      }
    }
  }, 2000);

  // ── Guest identify modal ──
  function showGuestModal(errMsg) {
    const modal = document.getElementById('guest-modal');
    const err = document.getElementById('guest-err');
    err.textContent = errMsg || '';
    modal.style.display = 'flex';
    const field = document.getElementById('guest-name');
    field.focus();
  }
  function hideGuestModal() {
    document.getElementById('guest-modal').style.display = 'none';
  }
  async function submitGuestName() {
    const field = document.getElementById('guest-name');
    const err = document.getElementById('guest-err');
    const name = (field.value || '').trim();
    if (!name) { err.textContent = 'Name is required.'; return null; }
    try {
      const r = await fetch('/api/identify' + API_QS, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        err.textContent = data.error || 'Failed to register.';
        return null;
      }
      return data.operator;
    } catch (e) {
      err.textContent = 'Request failed: ' + e.message;
      return null;
    }
  }
  document.getElementById('guest-submit').addEventListener('click', async () => {
    const op = await submitGuestName();
    if (op) { hideGuestModal(); applyOperator(op); afterBoot(); }
  });
  document.getElementById('guest-name').addEventListener('keydown', async (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const op = await submitGuestName();
      if (op) { hideGuestModal(); applyOperator(op); afterBoot(); }
    }
  });

  function applyOperator(op) {
    state.operator = op;
    // The server refuses a cull from anything but a local shell or a
    // Tailscale-verified peer. Mirror that here so an identity the server
    // would reject never sees the control at all, rather than being walked
    // through a confirm dialog into a 403.
    CAN_CULL = (op && (op.source === 'loopback' || op.source === 'tailscale'));
    const opAnimal = animalFor(op);
    const srcTag = op.source === 'tailscale' ? '[tailnet]' :
                   op.source === 'loopback'  ? '[local]'   :
                   op.source === 'guest'     ? '[GUEST]'   : '';
    hMeta.textContent = `posting as ${opAnimal.emoji} ${op.name} (${op.id}) — the ${opAnimal.name} ${srcTag}  ·  ${state.server_host}`;
  }

  // ── Bootstrap ──
  async function boot() {
    try {
      const r = await fetch('/api/meta' + API_QS);
      const meta = await r.json();
      state.channel = meta.channel;
      state.server_host = meta.server_host;
      loadPersistedTargets();
      renderComposerTargets();
      hChannel.textContent = (DM_MODE ? 'DM — trio#' : 'trio#') + meta.channel;
      state.originalTitle = (DM_MODE ? 'DM — trio#' : 'trio#') + meta.channel;
      if (DM_MODE) document.body.classList.add('dm-mode');
      updateTitle();
      if (meta.operator && meta.operator.pending) {
        // Untrusted connection — need a name before anything else
        showGuestModal();
        return;
      }
      bootAttempts = 0;
      clearFatal();
      applyOperator(meta.operator);
      afterBoot();
    } catch (e) {
      // Retry like the SSE path does. Without this a single blip while the
      // hub restarts left a permanently dead page — and the message went
      // into header .meta, which mobile CSS hides, so on a phone the whole
      // app was simply blank with no explanation.
      bootAttempts++;
      showFatal('Could not reach the hub (' + e.message + '). Retrying…');
      if (bootAttempts < 20) setTimeout(boot, Math.min(2000 * bootAttempts, 15000));
      else showFatal('Could not reach the hub: ' + e.message +
                     '. Check it is running, then reload.');
    }
  }
  let bootAttempts = 0;
  function showFatal(msg) {
    let el = document.getElementById('fatal-banner');
    if (!el) {
      el = document.createElement('div');
      el.id = 'fatal-banner';
      document.body.prepend(el);
    }
    el.textContent = msg;
    el.style.display = 'block';
  }
  function clearFatal() {
    const el = document.getElementById('fatal-banner');
    if (el) el.style.display = 'none';
  }
  // ── Full-history search (queries the server DB, not just loaded messages) ──
  const btnSearch = document.getElementById('btn-search');
  const searchPanel = document.getElementById('search-panel');
  const searchInput = document.getElementById('search-input');
  const searchClose = document.getElementById('search-close');
  const searchStatus = document.getElementById('search-status');
  const searchResults = document.getElementById('search-results');
  let searchTimer = 0, searchSeq = 0;

  function openSearch() {
    searchPanel.hidden = false;
    if (state.filter && !searchInput.value) searchInput.value = state.filter;
    searchInput.focus(); searchInput.select();
    if (searchInput.value.trim().length >= 2) runSearch();
  }
  function closeSearch() { searchPanel.hidden = true; }
  async function runSearch() {
    const q = searchInput.value.trim();
    searchResults.innerHTML = '';
    if (q.length < 2) { searchStatus.textContent = 'type at least 2 characters'; return; }
    searchStatus.textContent = 'searching…';
    const seq = ++searchSeq;
    try {
      // API_QS carries ?channel=<code> in landing mode and is empty in
      // single-channel mode, so pick the right query-string joiner.
      const r = await fetch('/api/search' + (API_QS ? API_QS + '&' : '?')
                            + 'q=' + encodeURIComponent(q));
      const d = await r.json().catch(() => ({}));
      if (seq !== searchSeq) return;   // a newer query superseded this one
      if (!r.ok || !d.ok) { searchStatus.textContent = 'search failed: ' + (d.error || r.status); return; }
      renderSearchResults(d.results || []);
    } catch (e) {
      if (seq === searchSeq) searchStatus.textContent = 'search failed: ' + e.message;
    }
  }
  function renderSearchResults(results) {
    const capped = results.length >= 200;
    searchStatus.textContent = results.length
      ? (results.length + (capped ? '+' : '') + ' match' + (results.length === 1 ? '' : 'es')
         + ' — newest first')
      : 'no matches';
    const frag = document.createDocumentFragment();
    for (const m of results) {
      const hit = document.createElement('div');
      hit.className = 'search-hit';
      const meta = document.createElement('div');
      meta.className = 'sh-meta';
      const author = document.createElement('span');
      author.className = 'sh-author';
      author.textContent = m.member_name;
      author.style.color = colorFor(m.member_id);
      meta.appendChild(author);
      meta.appendChild(document.createTextNode('  ·  ' + formatTime(m.created_at)));
      const body = document.createElement('div');
      body.className = 'sh-body';
      body.textContent = humanizeIdSigils(m.content || '');
      hit.appendChild(meta);
      hit.appendChild(body);
      // If the match is in the loaded timeline, jump + flash it; otherwise the
      // panel row is the result (it's outside the in-memory window).
      hit.addEventListener('click', () => {
        const dom = state.messageDomById.get(m.id);
        if (dom) {
          closeSearch();
          dom.scrollIntoView({ block: 'center' });
          dom.classList.add('flash');
          setTimeout(() => dom.classList.remove('flash'), 1500);
        }
      });
      frag.appendChild(hit);
    }
    searchResults.appendChild(frag);
  }
  btnSearch.addEventListener('click', openSearch);
  searchClose.addEventListener('click', closeSearch);
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 250);
  });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeSearch(); }
    else if (e.key === 'Enter') { clearTimeout(searchTimer); runSearch(); }
  });

  function afterBoot() {
    // API_QS is only set in landing mode. In single-channel mode "/" IS this
    // page, so the home link would just reload and its tooltip would be a lie.
    if (!API_QS) {
      const bh = document.getElementById('btn-home');
      if (bh) bh.style.display = 'none';
    }
    connect();
    input.focus();
    updatePreview();
    updateChanStats();
  }

  // __TRIO_TEST_HOOK_START__
  // Test hook: when this script is loaded under the Node DOM harness
  // (tests/dom-harness.js), expose the internal render/parse helpers for unit
  // testing. This whole block (marker to marker) is STRIPPED from the served
  // browser bundle at render time (see _strip_test_hook, applied by
  // _compose_index_html in nth_web.py), so the internal state reference never
  // ships to a browser at all. The runtime guard is a second line of defense
  // in case the strip ever fails: the test global is only pre-seeded by the harness
  // sandbox, never in production. Placed before boot() so the hooks are
  // available even if boot() throws against the harness's minimal DOM.
  if (typeof globalThis !== 'undefined' && globalThis.__TRIO_TEST__) {
    globalThis.__TRIO_TEST__ = {
      state,
      renderMarkdown, escapeHtml, isSystemContent, humanizeIdSigils,
      formatTime,
      collectMentionMatches, mentionMemberForToken,
      decorateInlineMentions, composerMentionHtml,
      chimeScopeAllows, shouldChime,
      detectFilePathCandidates, linkifyValidatedPaths, decorateFilePaths,
      offerWebFallback, sttBanner,
      // insertTranscript writes through the composer element it closed over,
      // so the element ships with it or the test has nothing to inspect.
      insertTranscript, composerInput: input,
    };
  }
  // __TRIO_TEST_HOOK_END__

  boot();
})();
