/*__ASK_HELPERS__*/
(function () {
  'use strict';
  const Trio = window.Trio;
  if (!Trio) throw new Error('Trio core must load before 10-markdown.js');
  const state = Trio.state;
  const escapeHtml = Trio.escapeHtml || function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  };

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

    function safeUrl(raw) {
      let u = raw.replace(/&(?:quot|#39);/g, '').trim();
      try {
        const url = new URL(u);
        if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
        return url.href;
      } catch { return ''; }
    }
    function inlineFmt(t) {
      t = humanizeIdSigils(t);
      t = escapeHtml(t);
      t = t.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/(^|[\s(\[])\*([^*\n]+?)\*(?=[\s.,!?;:)\]]|$)/g, '$1<em>$2</em>');
      t = t.replace(/(^|[\s(\[])_([^_\n]+?)_(?=[\s.,!?;:)\]]|$)/g, '$1<em>$2</em>');
      t = t.replace(/~~([^~\n]+?)~~/g, '<del>$1</del>');
      t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_m, txt, url) => {
        const u = safeUrl(url);
        return u ? '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + txt + '</a>' : txt;
      });
      t = t.replace(/(^|[\s(])(https?:\/\/[^\s<]+[^\s<.,;:!?)])/g, (_m, pre, url) => {
        const u = safeUrl(url);
        return u ? pre + '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + url + '</a>' : pre + url;
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
        // Wrap so a table wider than the message bubble scrolls horizontally
        // instead of collapsing its columns (see .md-table in the CSS).
        out.push('<div class="md-table">' + t + '</div>');
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

  // System events are bracket-tagged. They come in two shapes — "[word] …"
  // (joined/left/ended/locked/unlocked/pinned/renamed/objective/culled) and
  // "[word #id] …" (claimed/done/cancelled/released/retracted/status) — so match
  // the leading token (up to a space OR the closing bracket) against a word set.
  // (The old prefix list assumed a trailing space and silently missed every
  // "[word]" event, rendering them as markdown instead of muted system lines.)
  const SYSTEM_WORDS = new Set(['claimed', 'done', 'cancelled', 'released',
    'retracted', 'joined', 'left', 'ended', 'locked', 'unlocked', 'status',
    'pinned', 'renamed', 'culled', 'objective', 'superseded']);
  function isSystemContent(s) {
    if (/^\[channel created\](?:\s|$)/.test(s || '')) return true;
    // "[word " (the #id family) OR "[word]" followed by a space/end. Requiring
    // space-or-end after the "]" avoids muting a markdown link like [done](url).
    const m = /^\[([a-z]+)(?:\s|\](?:\s|$))/.exec(s || '');
    return !!m && SYSTEM_WORDS.has(m[1]);
  }

  // Keep lifecycle notices readable without exposing storage details such as
  // bracket tags, member ids, task ids, or the author who recorded the event.
  function systemMessageText(s, channel) {
    const raw = (s || '').trim();
    let m = /^\[joined\]\s+(.+?)(?:\s+—.*)?$/s.exec(raw);
    if (m) return m[1].trim() + ' joined channel';
    m = /^\[culled\]\s+(.+?)(?:\s+\([^)]*\))?\s+removed from channel(?:\s+—.*)?$/s.exec(raw);
    if (m) return m[1].trim() + ' removed from channel';
    if (/^\[channel created\](?:\s|$)/.test(raw)) return (channel || 'Channel') + ' channel created';
    m = /^\[superseded\]\s+(.+?)(?:\s+\([^)]*\))?\s+—/s.exec(raw);
    if (m) return m[1].trim() + ' superseded';
    if (/^\[retracted(?:\s+#\d+)?\]/.test(raw)) return 'Message deleted';
    m = /^\[(claimed|done|released|cancelled)\s+#?(\d+)\]/.exec(raw);
    if (m) return 'Task #' + m[2] + ' ' + ({ claimed: 'claimed', done: 'completed', released: 'released', cancelled: 'cancelled' }[m[1]]);
    m = /^\[left\]\s+(.+)$/s.exec(raw);
    if (m) return m[1].trim() + ' left channel';
    m = /^\[renamed\]\s+(.+)$/s.exec(raw);
    if (m) return 'Channel renamed: ' + m[1].trim();
    m = /^\[(locked|unlocked)\]\s+(.+?)(?:\s+\(TTL[^)]*\))?$/s.exec(raw);
    if (m) return m[2].trim() + ' ' + m[1];
    m = /^\[objective\]\s+(.+)$/s.exec(raw);
    if (m) return 'Channel objective: ' + m[1].trim();
    m = /^\[pinned\]\s+(.+)$/s.exec(raw);
    if (m) return 'Message pinned: ' + m[1].trim();
    return raw.replace(/^\[[^\]]+\]\s*/, '').trim();
  }

  // Task lifecycle events are ordinary chat messages tagged with a leading
  // marker ("[task #7] …", "[claimed #7] by X", "[done #7] …", "[released
  // #7] …", "[cancelled #7] …" — posted by nth_server.py). We special-case
  // them into a compact status card, the same way isSystemContent muting
  // special-cases the plain "[word] …" notices.
  //
  // BRITTLE (v1): this keys on the text prefix, so renaming a marker server-
  // side silently drops the styling and a user typing "[done #3]" would be
  // mis-styled. The durable fix is a structured kind/task_id column on the
  // messages row so the client keys on data, not a string prefix (same
  // additive-ALTER pattern the tasks table already uses) — intentionally NOT
  // added here.
  const TASK_VERBS = {
    task:      { label: 'posted',    cls: 'open' },
    claimed:   { label: 'claimed',   cls: 'claimed' },
    done:      { label: 'done',      cls: 'completed' },
    released:  { label: 'released',  cls: 'released' },
    cancelled: { label: 'cancelled', cls: 'cancelled' },
  };
  function taskEventInfo(s) {
    const m = /^\[(task|claimed|done|released|cancelled) #?(\d+)\]\s*(.*)$/s.exec(s || '');
    if (!m) return null;
    const meta = TASK_VERBS[m[1]];
    return { verb: m[1], label: meta.label, cls: meta.cls,
             id: m[2], rest: (m[3] || '').trim() };
  }
  function renderTaskEventCard(evt) {
    const card = document.createElement('div');
    card.className = 'task-event-card';
    const badge = document.createElement('span');
    badge.className = 'task-event-badge ' + evt.cls;
    badge.textContent = evt.label;
    card.appendChild(badge);
    const chip = document.createElement('span');
    chip.className = 'task-event-chip';
    chip.textContent = '#' + evt.id;
    chip.title = 'task #' + evt.id;
    card.appendChild(chip);
    if (evt.rest) {
      const txt = document.createElement('span');
      txt.className = 'task-event-text';
      // Humanize any @<member_id> sigils the same way message bodies do, then
      // render as plain text (no markdown — these are short status lines).
      txt.textContent = humanizeIdSigils(evt.rest);
      card.appendChild(txt);
    }
    return card;
  }

  // Rewrite @<member_id> / #<member_id> / !<member_id> to @<friendly-name>
  // in message bodies before rendering. The raw id-sigil form is valid
  // input (the server-side parser routes it correctly) but ugly to read;
  // agents can address-by-id for rename resilience and the UI translates
  // back to the current display name on the fly. Unknown ids are left
  // alone so stale history isn't mangled.
  let sigilRegex = null;
  let sigilMembers = null;
  let sigilSize = -1;
  function buildSigilRegex() {
    sigilMembers = state.members;
    sigilSize = sigilMembers ? sigilMembers.size : -1;
    if (!sigilMembers || !sigilMembers.size) { sigilRegex = null; return; }
    const ids = Array.from(sigilMembers.keys())
      .filter(Boolean)
      .sort((a, b) => b.length - a.length)
      .map(id => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    sigilRegex = ids.length ? new RegExp('([@#!])(' + ids.join('|') + ')(?=\\b|$)', 'g') : null;
  }
  function humanizeIdSigils(text) {
    if (!text) return text;
    if (sigilMembers !== state.members || (state.members && state.members.size !== sigilSize)) buildSigilRegex();
    if (!sigilRegex) return text;
    return text.replace(sigilRegex, (match, sigil, id) => {
      const mem = sigilMembers.get(id);
      return sigil + (mem && mem.name ? mem.name : id);
    });
  }

  Trio.markdown = {
    escapeHtml, renderMarkdown, isSystemContent, systemMessageText, taskEventInfo,
    renderTaskEventCard, humanizeIdSigils,
  };
}());
