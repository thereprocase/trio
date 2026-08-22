(() => {
  'use strict';
  const Trio = window.Trio;
  if (!Trio || !Trio.markdown) throw new Error('Conversation requires Trio core and markdown');
  const { state, events } = Trio;
  const M = Trio.markdown;
  const dom = () => document.getElementById('messages');

  state.messages = state.messages instanceof Map ? state.messages : new Map();
  state.members = state.members instanceof Map ? state.members : new Map();
  state.messageDomById = state.messageDomById instanceof Map ? state.messageDomById : new Map();
  state.answers = state.answers instanceof Map ? state.answers : new Map();
  state.askPage = state.askPage instanceof Map ? state.askPage : new Map();
  state.scrollPositions = state.scrollPositions || {};
  state.lastSeenByConv = state.lastSeenByConv || {};
  state.dividerBaseByConv = state.dividerBaseByConv || {};
  state.jumpUnread = state.jumpUnread || 0;
  state.activeMessageActions = state.activeMessageActions || null;

  function member(id) { return state.members.get(id) || {}; }
  function nameFor(id, fallback) { return member(id).name || fallback || id || 'unknown'; }
  // Shared with 20-workspace.js via Trio.avatarTone (00-core.js) — a bare
  // per-label hash used to duplicate this logic in both files with no
  // collision avoidance, so two members could land on the identical tone.
  const avatarTone = Trio.avatarTone;
  function avatarUrlFor(id) {
    const fromMember = member(id).avatar_url;
    if (fromMember) return fromMember;
    const agents = Trio.store?.get('agents.list') || state.agents || [];
    return agents.find(agent => agent.id === id)?.avatar_url || '';
  }
  function operator() { return state.operator || state.meta?.operator || {}; }
  function isOwn(msg) { return msg.member_id === operator().id; }
  function isPrivate(msg) { return !!msg.is_dm || Array.isArray(msg.recipients) && msg.recipients.length > 0; }
  function viewModel(msg) {
    const op = operator().id;
    const memberObj = member(msg.member_id);
    return {
      id: msg.id,
      member_id: msg.member_id,
      author: nameFor(msg.member_id, msg.member_name),
      isOwn: msg.member_id === op,
      role: msg.member_id === op ? '' : (memberObj.kind || 'agent'),
      isPrivate: isPrivate(msg),
      isSystem: M.isSystemContent(msg.content || ''),
      channel: msg.channel || state.channel || '',
      isTask: !!msg.task_id,
      isQuestion: !!msg.choices,
      isEdited: !!msg.edited_at,
      isRetracted: !!msg.retracted_at,
      retractionReason: msg.retraction_reason || '',
      content: msg.retracted_at ? '' : (msg.content || ''),
      createdAt: msg.created_at,
      date: date(msg.created_at),
      timestamp: time(msg.created_at),
      editedAt: msg.edited_at,
      editedTime: msg.edited_at ? time(msg.edited_at) : '',
      confidence: msg.confidence,
      recipients: msg.recipients || [],
      mentions: msg.mentions || [],
      refs: msg.refs || [],
      bangs: msg.bangs || [],
      attachments: msg.attachments || [],
      choices: msg.choices,
      selection: msg.selection,
      replyTo: msg.reply_to,
      taskId: msg.task_id,
      avatarUrl: avatarUrlFor(msg.member_id),
    };
  }
  function time(iso) {
    if (!iso) return '';
    try { return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
    catch (_) { return ''; }
  }
  function date(iso) {
    if (!iso) return '';
    try { return new Date(iso).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' }); }
    catch (_) { return ''; }
  }
  function nearBottom(el) { return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80; }
  function convId() { return state.dmKey ? 'dm:' + state.dmKey : (state.channel || 'home'); }
  // ── Read watermarks ──────────────────────────────────────────────────────
  // TWO values, per conversation, because they answer different questions.
  //
  //   lastSeenByConv[conv]     how far the operator has read. Advances as they
  //                            scroll; drives what gets flushed to the server.
  //   dividerBaseByConv[conv]  where "New since your last visit" is drawn.
  //                            FROZEN when the conversation is opened.
  //
  // Both used to be one global scalar, which was wrong twice over. Being
  // global, switching from a busy channel read to id 900 into a quieter one
  // whose newest id is 500 made findIndex(id > 900) return -1, so the divider
  // could never appear there again for the rest of the session — and entering
  // a channel with higher ids positioned the divider by a DIFFERENT
  // conversation's watermark. Being one value, the prime burst destroyed it:
  // every upsert ends in markRead() when the view is near the bottom, which it
  // is on entry, so the watermark raced to the newest message before the first
  // paint and the divider — whose entire purpose is to show on entry — never
  // appeared at all.
  function seenId() { return Number(state.lastSeenByConv?.[convId()]) || 0; }
  function setSeenId(id) {
    state.lastSeenByConv = state.lastSeenByConv || {};
    state.lastSeenByConv[convId()] = id;
  }
  // Called once per conversation entry, before its history is ingested.
  // `serverLastRead` is the operator's own watermark from the roster; without
  // it (roster not in yet) the base stays unset and the divider is simply not
  // drawn, which is the honest answer to "we do not know what you had read".
  function seedWatermark(serverLastRead) {
    const key = convId();
    state.dividerBaseByConv = state.dividerBaseByConv || {};
    state.lastSeenByConv = state.lastSeenByConv || {};
    // The DIVIDER is re-frozen on every entry: leaving a conversation and
    // coming back should show what is new since THAT visit, not since the
    // first one this session. (loadConversation runs per navigation — a
    // same-channel partial switch deliberately bypasses it — so this cannot
    // move the divider out from under someone who is mid-read.)
    state.dividerBaseByConv[key] = Number(serverLastRead) || 0;
    // The READ watermark is only seeded, never rewound: it may already have
    // advanced past the server's value locally, and lowering it would re-send
    // reads the server has already recorded.
    const seeded = Number(serverLastRead) || 0;
    if (!(key in state.lastSeenByConv) || state.lastSeenByConv[key] < seeded) {
      state.lastSeenByConv[key] = seeded;
    }
  }
  function dividerBase() { return Number(state.dividerBaseByConv?.[convId()]) || 0; }
  function markRead() { const list = ordered(); const last = list[list.length - 1]; if (last && Number(last.id) > seenId()) setSeenId(last.id); flushRead(); }
  let flushRefreshTimer = null;
  function flushRead() {
    if (state.readOnly) return;
    const op = state.operator?.id;
    if (!op) return;
    state.readFlushByConv = state.readFlushByConv || {};
    const key = convId();
    const lastFlushed = Number(state.readFlushByConv[key]) || 0;
    const ids = [];
    for (const msg of ordered()) {
      if (Number(msg.id) <= lastFlushed) continue;
      if (msg.member_id !== op) ids.push(msg.id);
    }
    if (!ids.length) return;
    state.readFlushByConv[key] = Math.max(...ids);
    Trio.api.post('/api/messages/mark-read', { ids }).then(() => {
      if (Trio.workspace?.refresh) {
        clearTimeout(flushRefreshTimer);
        flushRefreshTimer = setTimeout(() => Trio.workspace.refresh(), 1500);
      }
    }).catch(err => console.warn('flush read failed', err));
  }

  // Icon markup is static, trusted constants — innerHTML is safe here. Do NOT
  // reuse this innerHTML pattern for dynamic/user content.
  const COPY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  const CHECK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
  // A copy button that pulls its text lazily (so message edits are reflected),
  // then flips to a check-mark for ~1.4s as feedback. Clipboard state is also
  // pushed to the aria-live region so screen-reader users hear the result — an
  // aria-label swap on an already-focused button is not reliably announced.
  function makeCopyButton(getText, opts) {
    opts = opts || {};
    const label = opts.title || 'Copy';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'copy-btn' + (opts.className ? ' ' + opts.className : '');
    btn.title = label; btn.setAttribute('aria-label', label);
    btn.innerHTML = COPY_SVG;
    let resetTimer = null;
    btn.addEventListener('click', event => {
      event.preventDefault(); event.stopPropagation();
      const text = typeof getText === 'function' ? getText() : getText;
      Trio.ui.copyText(text).then(() => {
        btn.classList.add('copied'); btn.innerHTML = CHECK_SVG;
        btn.title = 'Copied'; btn.setAttribute('aria-label', 'Copied');
        Trio.ui?.setLive?.('Copied');
        clearTimeout(resetTimer);
        resetTimer = setTimeout(() => {
          btn.classList.remove('copied'); btn.innerHTML = COPY_SVG;
          btn.title = label; btn.setAttribute('aria-label', label);
        }, 1400);
      }).catch(() => { Trio.ui?.toast?.('Copy failed'); Trio.ui?.setLive?.('Copy failed'); });
    });
    return btn;
  }
  // Wrap each rendered code block in a positioned container and drop a copy
  // button in its corner. textContent gives the raw (unescaped) source. Keys on
  // the `pre.mdcode` class minted by 10-markdown.js — if that class is renamed
  // there, update this selector to match.
  function decorateCodeBlocks(root) {
    if (!root) return;
    root.querySelectorAll('pre.mdcode').forEach(pre => {
      if (pre.parentElement && pre.parentElement.classList.contains('code-block')) return;
      const wrap = document.createElement('div'); wrap.className = 'code-block';
      pre.replaceWith(wrap); wrap.append(pre);
      wrap.append(makeCopyButton(() => pre.textContent, { title: 'Copy code', className: 'code-copy' }));
    });
  }

  function decorateSigils(root, vm) {
    const ids = new Set([...(vm.mentions || []), ...(vm.refs || []), ...(vm.bangs || [])]);
    if (!ids.size || !root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const n = walker.currentNode;
      if (!n.parentElement.closest('code,pre,a,.sigil')) nodes.push(n);
    }
    for (const node of nodes) {
      const text = node.nodeValue || '';
      const re = /([@#!])([^\s.,;:!?()[\]{}]+)/g;
      let match, cursor = 0, changed = false;
      const frag = document.createDocumentFragment();
      while ((match = re.exec(text))) {
        const wordLower = match[2].toLowerCase();
        // @all / !all broadcast — rainbow shimmer, independent of any member
        // match (there's no member literally named "all"). Only `all` is wired
        // to a broadcast server-side, so `everyone` renders as plain text.
        if ((match[1] === '@' || match[1] === '!') && wordLower === 'all') {
          changed = true;
          frag.append(document.createTextNode(text.slice(cursor, match.index)));
          const span = document.createElement('span');
          span.className = 'sigil inline-all';
          span.textContent = match[0];
          frag.append(span);
          cursor = match.index + match[0].length;
          continue;
        }
        const lookup = [...ids].find(id => nameFor(id, id) === match[2] || id === match[2]);
        if (!lookup) continue;
        changed = true;
        frag.append(document.createTextNode(text.slice(cursor, match.index)));
        const span = document.createElement('span');
        const kind = ({'@':'mention','#':'ref','!':'bang'}[match[1]]);
        span.className = 'sigil sigil-' + kind + ' inline-' + kind;
        // Tint an @-mention with the agent's own tone (same palette as roster/
        // facepile). data-tone, not the .tone-* class, so it doesn't inherit the
        // avatar gradient background.
        if (kind === 'mention') span.dataset.tone = Trio.avatarTone(nameFor(lookup, match[2])) || 'eucalyptus';
        span.dataset.memberId = lookup;
        span.textContent = match[1] + nameFor(lookup, match[2]);
        frag.append(span);
        cursor = match.index + match[0].length;
      }
      if (changed) { frag.append(document.createTextNode(text.slice(cursor))); node.replaceWith(frag); }
    }
  }

  function paintBody(card, body, msgOrVm) {
    const vm = (msgOrVm && typeof msgOrVm.isRetracted === 'boolean') ? msgOrVm : viewModel(msgOrVm);
    card.classList.toggle('retracted', vm.isRetracted);
    body.replaceChildren();
    if (vm.isRetracted) {
      body.className = 'message-body plain';
      body.textContent = '[deleted' + (vm.retractionReason ? ' — ' + vm.retractionReason : '') + ']';
      return;
    }
    if (vm.isSystem) {
      body.className = 'message-body plain system';
      body.textContent = M.systemMessageText(vm.content, vm.channel);
    } else {
      body.className = 'message-body bubble';
      body.innerHTML = M.renderMarkdown(vm.content);
      decorateSigils(body, vm);
      Trio.fileLinks?.decorateFilePaths?.(body, vm.member_id);
      decorateCodeBlocks(body);
    }
    if (vm.isEdited) {
      const edited = document.createElement('span');
      edited.className = 'edited-mark'; edited.textContent = ' (edited)';
      edited.title = 'edited ' + vm.editedTime; body.append(edited);
    }
  }

  function answerState(msg, questions) {
    let answers = state.answers.get(msg.id);
    if (!answers || answers.length !== questions.length) {
      answers = questions.map(() => ({ picked: new Set(), custom: '' }));
      state.answers.set(msg.id, answers);
    }
    return answers;
  }
  function answerPayload(msg, questions) {
    const raw = answerState(msg, questions);
    const answers = raw.map(answer => ({
      picked: [...answer.picked], custom: answer.custom.trim() ? [answer.custom.trim()] : [],
    }));
    if (answers.some(answer => !answer.picked.length && !answer.custom.length)) throw new Error('Answer every question before sending');
    const multi = questions.length > 1;
    let content = '';
    if (typeof composeAnswer === 'function') {
      content = composeAnswer(questions, answers, multi);
    } else {
      content = multi ? ('Answered ' + questions.length + ' questions') : 'Answered question #' + msg.id;
    }
    return { content, reply_to: msg.id, selection: { answers } };
  }
  async function submitAnswer(msg, questions, button) {
    try {
      button.disabled = true;
      const result = await Trio.api.post(apiUrl('/api/send'), answerPayload(msg, questions));
      if (result?.message) upsert(result.message);
      button.textContent = 'Answer sent';
    } catch (error) {
      button.disabled = false;
      Trio.ui.toast(error.message || 'Could not send answer');
    }
  }
  // Dismiss lives in workspace state (shared with the attention inbox) so a
  // question waved off here also leaves the "needs an answer" count.
  function askDismissKey(msg) { return 'question-' + msg.id; }
  // Render the collapsed "dismissed" bar into an ask-card wrap. Used both when
  // the user clicks × AND when the card rebuilds for an already-dismissed
  // question (channel switch / reload) — otherwise the dismissal would silently
  // revert on the next render while the attention inbox still hid it.
  function renderDismissedBar(msg, wrap) {
    wrap.replaceChildren();
    wrap.classList.add('dismissed');
    const bar = document.createElement('div'); bar.className = 'ask-dismissed-bar';
    const label = document.createElement('span'); label.textContent = 'Question dismissed';
    const undo = document.createElement('button'); undo.type = 'button'; undo.className = 'ask-undo'; undo.textContent = 'Undo';
    undo.addEventListener('click', () => {
      Trio.workspace?.undismissQuestion?.(askDismissKey(msg));
      const fresh = askCard(msg); if (fresh) { wrap.replaceWith(fresh); Trio.workspace?.render?.(); }
    });
    bar.append(label, undo); wrap.append(bar);
  }
  function collapseDismissed(msg, wrap) {
    Trio.workspace?.dismissQuestion?.(askDismissKey(msg));
    renderDismissedBar(msg, wrap);
    Trio.workspace?.render?.();
  }
  // One clean, self-contained card for a trio_ask. Renders ONE question at a
  // time (batches get a pager), a corner dismiss, and a single submit. The
  // message bubble that would otherwise echo the same question+options as prose
  // is suppressed upstream (cardFor) so this card is the only thing shown.
  function askCard(msg) {
    const choices = msg.choices;
    if (!choices || !Array.isArray(choices.questions || choices.options)) return null;
    const questions = choices.questions || [choices];
    const wrap = document.createElement('section'); wrap.className = 'ask-card'; wrap.setAttribute('role', 'group');
    // A persisted dismissal (shared with the attention inbox) stays collapsed on
    // every rebuild, not just until the next render.
    if (Trio.workspace?.isQuestionDismissed?.(askDismissKey(msg))) { renderDismissedBar(msg, wrap); return wrap; }
    // The human's answer is recorded on the REPLY message's `selection`, not on
    // the ask itself — so find the reply and read the selection from there.
    const replyMsg = [...state.messages.values()].find(c => c.reply_to === msg.id && c.selection);
    const answered = !!replyMsg || !!msg.selection;
    const isTarget = choices.target === operator().id;
    const canAnswer = isTarget && !answered;
    const multi = questions.length > 1;
    const answers = answerState(msg, questions);
    // Once answered, mirror the recorded selection into the local answer state so
    // the locked card highlights the chosen option for EVERY viewer — not just
    // the person who clicked (whose choice lives only in in-memory state.answers,
    // gone after a reload). Prefer the ask's own selection if present, else the
    // reply's.
    const recorded = (msg.selection && Array.isArray(msg.selection.answers)) ? msg.selection
                   : (replyMsg && replyMsg.selection && Array.isArray(replyMsg.selection.answers)) ? replyMsg.selection
                   : null;
    if (answered && recorded) {
      recorded.answers.forEach((a, i) => {
        if (!answers[i] || !a) return;
        answers[i].picked = new Set(Array.isArray(a.picked) ? a.picked : []);
        if (Array.isArray(a.custom) && a.custom.length) answers[i].custom = a.custom[0];
      });
    }

    const head = document.createElement('header'); head.className = 'ask-head';
    const eyebrow = document.createElement('span'); eyebrow.className = 'ask-eyebrow';
    head.append(eyebrow);
    if (canAnswer) {
      const dismiss = document.createElement('button');
      dismiss.type = 'button'; dismiss.className = 'ask-dismiss';
      dismiss.setAttribute('aria-label', 'Dismiss question'); dismiss.title = 'Dismiss';
      dismiss.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18"/></svg>';
      dismiss.addEventListener('click', () => collapseDismissed(msg, wrap));
      head.append(dismiss);
    }
    wrap.append(head);

    const bodyEl = document.createElement('div'); bodyEl.className = 'ask-body'; wrap.append(bodyEl);
    const foot = document.createElement('footer'); foot.className = 'ask-foot'; wrap.append(foot);

    let current = Number(state.askPage.get(msg.id)) || 0;
    if (current < 0 || current >= questions.length) current = 0;

    const answeredCount = i => (answers[i].picked.size > 0 || String(answers[i].custom || '').trim().length > 0);

    function setEyebrow() {
      if (!canAnswer) { eyebrow.textContent = answered ? 'Answered' : 'Question'; return; }
      eyebrow.textContent = multi ? ('Question ' + (current + 1) + ' of ' + questions.length) : 'Question';
    }
    function go(index) {
      current = index; state.askPage.set(msg.id, current); paint();
      // Keep keyboard users oriented — focus the new page's question heading
      // instead of dropping focus to <body> on every navigation.
      const t = bodyEl.querySelector('.ask-question');
      if (t) { t.setAttribute('tabindex', '-1'); t.focus(); }
    }

    function makeSend() {
      const send = document.createElement('button'); send.type = 'button'; send.className = 'ask-send';
      send.textContent = multi ? 'Send answers' : 'Send answer';
      send.addEventListener('click', () => {
        // On a batch, jump to the first still-unanswered question instead of a
        // bare error toast, so the person knows exactly what's missing.
        if (multi) { const gap = questions.findIndex((_, i) => !answeredCount(i)); if (gap !== -1 && gap !== current) { go(gap); return; } }
        submitAnswer(msg, questions, send);
      });
      return send;
    }
    function paintFoot() {
      foot.replaceChildren();
      const last = current === questions.length - 1;
      // The pager renders for ANY multi-question card — answerable or not — so an
      // answered card or a non-target observer can still page through questions
      // 2..N and see each recorded answer, not just question 1.
      if (multi) {
        const back = document.createElement('button'); back.type = 'button'; back.className = 'ask-nav'; back.textContent = 'Back';
        back.disabled = current === 0; back.addEventListener('click', () => go(current - 1)); foot.append(back);
        const dots = document.createElement('div'); dots.className = 'ask-dots';
        questions.forEach((_, i) => {
          const dot = document.createElement('button'); dot.type = 'button';
          dot.className = 'ask-dot' + (i === current ? ' on' : '') + (answeredCount(i) ? ' done' : '');
          dot.setAttribute('aria-label', 'Go to question ' + (i + 1)); dot.addEventListener('click', () => go(i)); dots.append(dot);
        });
        foot.append(dots);
        if (!last) { const next = document.createElement('button'); next.type = 'button'; next.className = 'ask-nav primary'; next.textContent = 'Next'; next.addEventListener('click', () => go(current + 1)); foot.append(next); }
        else if (canAnswer) foot.append(makeSend());
      } else if (canAnswer) {
        foot.append(makeSend());
      }
      if (!canAnswer) {
        const note = document.createElement('small');
        note.className = 'ask-note' + (answered ? ' done' : '');
        note.textContent = answered ? 'Answer sent'
          : (choices.target ? 'Awaiting ' + nameFor(choices.target, choices.target) : '');
        if (note.textContent) foot.append(note);
      }
    }
    function paint() {
      setEyebrow();
      bodyEl.replaceChildren();
      const q = questions[current];
      if (q.header) { const section = document.createElement('div'); section.className = 'ask-section'; section.textContent = q.header; bodyEl.append(section); }
      const title = document.createElement('p'); title.className = 'ask-question'; title.textContent = q.question || ''; bodyEl.append(title);
      const opts = document.createElement('div'); opts.className = 'ask-options';
      const type = q.mode === 'one' ? 'radio' : 'checkbox';
      const name = 'ask-' + msg.id + '-' + current;
      const picked = answers[current].picked;
      (q.options || []).forEach((option, optionIndex) => {
        const id = name + '-' + optionIndex;
        const row = document.createElement('label'); row.className = 'ask-option' + (picked.has(optionIndex) ? ' selected' : ''); row.htmlFor = id;
        const input = document.createElement('input'); input.type = type; input.name = name; input.id = id; input.value = optionIndex;
        input.checked = picked.has(optionIndex); input.disabled = !canAnswer;
        input.addEventListener('change', () => {
          if (q.mode === 'one') { picked.clear(); picked.add(optionIndex); }
          else if (input.checked) { picked.add(optionIndex); } else { picked.delete(optionIndex); }
          opts.querySelectorAll('.ask-option').forEach((label, idx) => label.classList.toggle('selected', picked.has(idx)));
          if (multi) paintFoot();
        });
        const span = document.createElement('span'); span.textContent = option;
        row.append(input, span); opts.append(row);
      });
      bodyEl.append(opts);
      if (q.custom !== false) {
        const custom = document.createElement('input'); custom.className = 'ask-custom'; custom.placeholder = 'Type your own answer…'; custom.disabled = !canAnswer; custom.value = answers[current].custom;
        custom.addEventListener('input', () => { answers[current].custom = custom.value; if (multi) paintFoot(); });
        bodyEl.append(custom);
      }
      paintFoot();
    }

    paint();
    return wrap;
  }

  function renderTargets(msg) {
    const targets = [['!', msg.bangs, 'bang'], ['@', msg.mentions, 'to'], ['#', msg.refs, 'about']]
      .filter(([, ids]) => ids && ids.length);
    if (!targets.length) return null;
    const bar = document.createElement('div'); bar.className = 'message-targets';
    for (const [sigil, ids, label] of targets) {
      const group = document.createElement('span'); group.className = 'target-group';
      group.append(document.createTextNode(label + ' '));
      ids.forEach(id => { const chip = document.createElement('span'); chip.className = 'target-chip'; chip.textContent = sigil + nameFor(id, id); group.append(chip); });
      bar.append(group);
    }
    return bar;
  }

  function apiUrl(path) {
    if (typeof Trio.api.url === 'function') return Trio.api.url(path);
    return state.channel ? path + '?channel=' + encodeURIComponent(state.channel) : path;
  }
  function retract(msg) {
    const html = '<label class="field">Reason for deleting this message (optional) <textarea name="reason" rows="2"></textarea></label>';
    Trio.ui.modal('Delete message', html, async node => {
      const reason = node.querySelector('[name="reason"]').value || '';
      try {
        const response = await fetch(apiUrl('/api/delete'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message_id: msg.id, reason }) });
        if (!response.ok) throw new Error('delete failed (' + response.status + ')');
      } catch (error) { Trio.ui.toast(error.message); }
    });
  }
  function edit(msg, body) {
    const html = '<label class="field">Edit message <textarea name="content" rows="4">' + M.escapeHtml(msg.content || '') + '</textarea></label>';
    Trio.ui.modal('Edit message', html, async node => {
      const content = node.querySelector('[name="content"]').value;
      if (content == null || content === msg.content) return;
      try {
        const response = await fetch(apiUrl('/api/edit'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message_id: msg.id, content }) });
        if (!response.ok) throw new Error('edit failed (' + response.status + ')');
        body.textContent = content;
      } catch (error) { Trio.ui.toast(error.message); }
    });
  }

  function closeMessageActions() {
    const menu = state.activeMessageActions;
    if (!menu) return;
    menu.classList.add('hidden');
    state.activeMessageActions = null;
  }
  function interactiveMessageTarget(target) {
    return !!target?.closest?.('a,button,input,textarea,select,fieldset');
  }
  function showMessageActions(card, content, msg, body) {
    if (!isOwn(msg) || msg.retracted_at || state.readOnly) return;
    const existing = content.querySelector('.message-actions-menu');
    if (existing && state.activeMessageActions === existing && !existing.classList.contains('hidden')) {
      closeMessageActions();
      return;
    }
    closeMessageActions();
    let menu = existing;
    if (!menu) {
      menu = document.createElement('div'); menu.className = 'message-actions-menu hidden';
      for (const [label, fn] of [['Edit', () => edit(msg, body)], ['Delete', () => retract(msg)]]) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
        button.addEventListener('click', () => { closeMessageActions(); fn(); });
        menu.append(button);
      }
      content.append(menu);
    }
    menu.classList.remove('hidden');
    state.activeMessageActions = menu;
  }
  function bindMessageActions(card, content, msg, body) {
    let pressTimer = null;
    const clearPress = () => { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } };
    const startPress = event => {
      if (event.isPrimary === false || (event.button != null && event.button !== 0) || interactiveMessageTarget(event.target)) return;
      clearPress();
      pressTimer = setTimeout(() => {
        pressTimer = null;
        showMessageActions(card, content, msg, body);
      }, 500);
    };
    card.addEventListener('pointerdown', startPress);
    card.addEventListener('pointerup', clearPress);
    card.addEventListener('pointercancel', clearPress);
    card.addEventListener('pointerleave', clearPress);
    card.addEventListener('contextmenu', event => {
      if (interactiveMessageTarget(event.target)) return;
      event.preventDefault(); clearPress();
      showMessageActions(card, content, msg, body);
    });
  }

  function cardFor(msg) {
    const vm = viewModel(msg);
    const card = document.createElement('article'); card.className = 'message msg' + (vm.isSystem ? ' system-message' : '') + (!vm.isSystem && vm.isOwn ? ' own me' : '') + (!vm.isSystem && vm.isPrivate ? ' private' : '');
    card.dataset.messageId = vm.id;
    if (vm.isSystem) {
      const content = document.createElement('div'); content.className = 'message-content msg-body';
      const body = document.createElement('div'); paintBody(card, body, vm); content.append(body);
      card.append(content);
      return card;
    }
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar av av-32 tone-' + avatarTone(vm.author);
    avatar.setAttribute('aria-hidden', 'true');
    if (vm.avatarUrl) {
      const image = document.createElement('img');
      image.className = 'avatar-svg-image'; image.src = vm.avatarUrl; image.alt = '';
      avatar.append(image);
    } else avatar.textContent = (vm.author || '?').split(/\s+/).map(part => part[0]).join('').slice(0, 2).toUpperCase();
    const content = document.createElement('div'); content.className = 'message-content msg-body';
    const head = document.createElement('header'); head.className = 'message-head';
    const author = document.createElement('strong'); author.textContent = vm.author;
    const stamp = document.createElement('time');
    const idPart = document.createElement('span'); idPart.className = 'message-id'; idPart.textContent = '#' + vm.id + ' · ';
    stamp.append(idPart, document.createTextNode(vm.timestamp));
    head.append(author);
    if (vm.role) {
      const role = document.createElement('span'); role.className = 'message-role role-' + vm.role; role.textContent = vm.role;
      head.append(role);
    }
    head.append(stamp);
    if (vm.confidence) { const confidence = document.createElement('span'); confidence.className = 'confidence confidence-' + vm.confidence; confidence.textContent = vm.confidence; head.append(confidence); }
    if (vm.isPrivate) { const badge = document.createElement('span'); badge.className = 'private-badge'; badge.textContent = 'private'; head.append(badge); }
    if (vm.isTask) { const task = document.createElement('span'); task.className = 'task-chip'; task.textContent = 'task #' + vm.taskId; head.append(task); }
    if (vm.isQuestion) card.classList.add('question');
    content.append(head);
    if (vm.replyTo) {
      const reply = document.createElement('a'); reply.className = 'reply-context'; reply.href = '#m' + vm.replyTo; reply.textContent = 'replying to #' + vm.replyTo;
      reply.addEventListener('click', (e) => { e.preventDefault(); const target = document.querySelector(`[data-message-id="${vm.replyTo}"]`); if (target) { target.scrollIntoView({ behavior: 'smooth', block: 'center' }); target.focus(); } });
      content.append(reply);
    }
    const target = renderTargets(vm); if (target) content.append(target);
    // A trio_ask stores the question BOTH as prose (content) and as a structured
    // picker (choices). Rendering both stacks a redundant echo bubble above the
    // picker — so when the ask-card will render, suppress the bubble and let the
    // card be the whole message. Retracted questions fall back to the bubble
    // ("[deleted]") since there's nothing to answer.
    const ask = (!vm.isRetracted) ? askCard(msg) : null;
    // `body` stays function-scoped: bindMessageActions (below) needs it, and a
    // question message simply leaves it null (its own messages are never asks).
    let body = null;
    if (!ask) { body = document.createElement('div'); body.className = 'message-body bubble'; paintBody(card, body, vm); content.append(body); }
    if (vm.attachments.length) {
      const attachments = document.createElement('div'); attachments.className = 'message-attachments';
      // Every image in THIS message forms one gallery, so the lightbox's
      // left/right arrows page through exactly the images posted together.
      const gallery = [];
      vm.attachments.forEach(attachment => {
        if (!attachment || !attachment.id) return;
        const href = apiUrl('/api/attachment/' + attachment.id);
        const link = document.createElement('a'); link.href = href; link.target = '_blank'; link.rel = 'noopener'; link.className = 'message-attachment';
        if (/^image\//.test(attachment.mime || '')) {
          const alt = attachment.filename || 'Attached image';
          const at = gallery.length; gallery.push({ url: href, alt });
          const image = document.createElement('img'); image.src = href; image.alt = alt; image.loading = 'lazy';
          // Keyboard-openable to the same lightbox as a mouse click — otherwise
          // Enter on the wrapping link opens the raw image in a new tab and
          // keyboard/SR users never reach the gallery/zoom (mirrors composer).
          image.tabIndex = 0;
          image.setAttribute('role', 'button');
          image.setAttribute('aria-label', 'View image: ' + alt);
          const openHere = (e) => { e.preventDefault(); Trio.lightbox.open(gallery, at); };
          image.addEventListener('click', openHere);
          image.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') openHere(e); });
          image.addEventListener('error', () => { image.classList.add('error'); });
          link.append(image);
        }
        else link.textContent = attachment.filename || ('Attachment #' + attachment.id);
        attachments.append(link);
      });
      if (attachments.children.length) content.append(attachments);
    }
    if (ask) content.append(ask);
    // Copy-markdown affordance — a hover button that copies the message's raw
    // markdown source (like ChatGPT). Appended last so it sits at the bottom of
    // the card, below any attachments/ask picker. Skipped for system/retracted
    // rows and for empty-content messages (image-only), where a "copied" flash
    // would falsely confirm an empty clipboard.
    if (!vm.isSystem && !vm.isRetracted) {
      const copySource = () => (state.messages.get(vm.id)?.content) ?? vm.content ?? '';
      if (copySource().trim()) {
        const tools = document.createElement('div'); tools.className = 'message-tools';
        tools.append(makeCopyButton(copySource, { title: 'Copy markdown', className: 'msg-copy' }));
        content.append(tools);
      }
    }
    if (body && isOwn(msg) && !msg.retracted_at && !state.readOnly) bindMessageActions(card, content, msg, body);
    card.append(avatar, content);
    return card;
  }

  function ordered() { return [...state.messages.values()].sort((a, b) => Number(a.id) - Number(b.id)); }
  // A history prime arrives as hundreds of synchronous `message` events. Paint
  // the first one immediately (so ordinary live delivery keeps its current
  // synchronous behavior), then collect the rest until the next frame. That
  // turns a 500-message prime from 500 growing-list layouts into one immediate
  // insert plus one fragment append. State is still updated synchronously.
  let pendingInsertIds = new Set();
  let pendingInsertFrame = 0;
  let pendingWasNear = false;
  function cancelPendingInserts() {
    if (pendingInsertFrame) cancelAnimationFrame(pendingInsertFrame);
    pendingInsertFrame = 0;
    pendingInsertIds.clear();
    pendingWasNear = false;
  }
  function insertCardInOrder(list, card, id) {
    const next = [...list.children].find(el => el.dataset?.messageId && Number(el.dataset.messageId) > Number(id));
    if (next) list.insertBefore(card, next); else list.append(card);
  }
  function flushPendingInserts() {
    const ids = [...pendingInsertIds].sort((a, b) => Number(a) - Number(b));
    const wasNear = pendingWasNear;
    pendingInsertFrame = 0;
    pendingInsertIds.clear();
    pendingWasNear = false;
    const list = dom();
    if (!list || !ids.length) return;
    const cards = ids.flatMap(id => {
      const msg = state.messages.get(id);
      return msg && !state.messageDomById.has(id) ? [[id, cardFor(msg)]] : [];
    });
    if (!cards.length) return;
    // Prime history is ordered, so its tail can be appended as one DOM write.
    // Retain the old ordered-insert defense for the rare prime/live race where
    // an older id arrives after a newer card is already present.
    const paintedIds = [...list.children]
      .map(el => Number(el.dataset?.messageId)).filter(Number.isFinite);
    const lastPainted = paintedIds.length ? Math.max(...paintedIds) : -Infinity;
    if (cards.every(([id]) => Number(id) > lastPainted)) {
      const fragment = document.createDocumentFragment();
      cards.forEach(([id, card]) => { fragment.append(card); state.messageDomById.set(id, card); });
      list.append(fragment);
    } else {
      cards.forEach(([id, card]) => { insertCardInOrder(list, card, id); state.messageDomById.set(id, card); });
    }
    if (wasNear) { list.scrollTop = list.scrollHeight; markRead(); }
    pruneMessages();
  }
  function openInsertWindow(wasNear) {
    if (pendingInsertFrame) return;
    pendingWasNear = wasNear;
    pendingInsertFrame = requestAnimationFrame(flushPendingInserts);
  }
  function render() {
    const list = dom(); if (!list) return;
    // A full render already paints every state.messages entry; discard a
    // scheduled incremental tail so the same cards cannot be appended twice.
    cancelPendingInserts();
    const stick = nearBottom(list); list.replaceChildren(); state.messageDomById.clear();
    const messages = ordered();
    if (!messages.length) {
      const empty = document.createElement('div'); empty.className = 'conversation-empty';
      const p = document.createElement('p');
      if (state.dmLoading) { p.textContent = 'Loading private conversation…'; }
      else if (state.dmError) { p.textContent = 'Could not load conversation: ' + state.dmError; }
      else if (state.dmKey) { p.textContent = 'No messages yet. This is the start of your private conversation.'; }
      else { p.textContent = 'No messages yet. Say hello to get things moving.'; }
      empty.append(p);
      if (state.dmError && state.dmThread) {
        const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Try again';
        retry.addEventListener('click', () => Trio.workspace?.openDm?.(state.dmThread));
        empty.append(retry);
      }
      list.append(empty);
      const saved = state.scrollPositions[convId()];
      if (stick) { list.scrollTop = list.scrollHeight; markRead(); }
      else if (saved != null) { list.scrollTop = saved; }
      return;
    }
    const rendered = messages;
    // Unread divider index, mapped into the rendered slice.
    // Drawn against the FROZEN entry watermark, not the advancing read one —
    // otherwise reading the conversation erases the divider you opened it to
    // see. A base of 0 means "we never knew what you had read", and no
    // divider is drawn rather than marking the whole history unread.
    const base = dividerBase();
    let unread = base ? messages.findIndex(msg => Number(msg.id) > base) : -1;
    let unreadRel = -1;
    // No age-based collapse any more, so the divider index needs no remapping:
    // the rendered list IS the full list. (It used to be a slice, and mapping
    // between the two was where the divider went missing.)
    if (unread >= 0) unreadRel = unread;
    let lastDate = '';
    rendered.forEach((msg, index) => {
      const d = date(msg.created_at);
      if (d && d !== lastDate) { lastDate = d; const day = document.createElement('div'); day.className = 'day-separator'; day.textContent = d; list.append(day); }
      if (index === unreadRel) { const divider = document.createElement('div'); divider.className = 'unread-divider'; divider.textContent = 'New since your last visit'; list.append(divider); }
      const card = cardFor(msg); list.append(card); state.messageDomById.set(msg.id, card);
    });
    const saved = state.scrollPositions[convId()];
    if (stick) { list.scrollTop = list.scrollHeight; markRead(); }
    else if (saved != null) { list.scrollTop = saved; }
  }

  function upsert(msg) {
    if (!msg || msg.id == null) return;
    if (state.dmKey) {
      const recips = new Set([...(msg.recipients || []), msg.member_id].filter(Boolean));
      const expected = state.dmMemberIds || [];
      if (state.dmAudit) {
        if (expected.length && (recips.size !== expected.length || [...recips].some(id => !expected.includes(id)))) return;
      } else {
        // operator() falls back to state.meta.operator, which is what every
        // other call site in this workspace does. Reading state.operator
        // directly meant a boot that failed to populate it turned the
        // membership test into "drop everything": the thread listed, opened,
        // and rendered empty — no error, just an apparently silent agent.
        const op = operator().id;
        if (op) {
          if (!recips.has(op)) return;
          const others = [...recips].filter(id => id !== op);
          if (expected.length && (others.length !== expected.length || others.some(id => !expected.includes(id)))) return;
        } else if (expected.length) {
          // Identity genuinely unknown. Match on the thread's participants, as
          // the audit branch above does. The server has already scoped this
          // thread to what the caller may read, so showing what it returned is
          // not a disclosure — showing NOTHING is the worse failure, because an
          // empty conversation reads as "they never replied".
          if (!expected.some(id => recips.has(id))) return;
        }
      }
    } else if (isPrivate(msg)) {
      // Channel view (no dmKey): a recipients-scoped DM belongs to the separate
      // DM surface and must NEVER render inline in a channel — not even for the
      // all-seeing operator (the server can't distinguish a channel view from a
      // DM view on the shared per-channel stream, so this is enforced here).
      // DMs appear only in the DM inbox, grouped globally by participant — which
      // is what makes them "global" from the user's seat and fixes "messages
      // showing up in a channel I didn't send them in". The DM view (state.dmKey
      // set, handled above) still receives these, so live DM updates are
      // unaffected. See the humans-and-agents workspace model.
      return;
    } else if (msg.channel && state.channel && msg.channel !== state.channel) {
      // A message from another channel must not render in the channel
      // currently open. The workspace-wide SSE stream (00-core.js) multiplexes
      // EVERY channel's messages through this same event target so the
      // notification module can chime for other channels; the central guard in
      // 04-events.js::dispatch only protects its own state write, not this
      // fan-out — so re-check here. DM views are scoped by recipients above.
      return;
    }
    const wasNear = nearBottom(dom()); const previous = state.messages.get(msg.id) || {};
    state.messages.set(msg.id, Object.assign({}, previous, msg));
    const existing = state.messageDomById.get(msg.id);
    const list = dom();
    if (!existing) {
      if (pendingInsertFrame) {
        pendingInsertIds.add(msg.id);
        return;
      }
      if (list) {
        {
          const card = cardFor(state.messages.get(msg.id));
          // Defense in depth against an EventHub prime/live race delivering a
          // newer id ahead of older history: insert in id order instead of
          // blindly appending, so a late-arriving older message still lands
          // before the newer cards already painted.
          insertCardInOrder(list, card, msg.id);
          state.messageDomById.set(msg.id, card);
        }
      }
      else { render(); }
      if (wasNear && list) { list.scrollTop = list.scrollHeight; markRead(); }
      pruneMessages();
      if (list) openInsertWindow(wasNear);
      return;
    }
    const replacement = cardFor(state.messages.get(msg.id)); existing.replaceWith(replacement); state.messageDomById.set(msg.id, replacement);
    if (wasNear) list.scrollTop = list.scrollHeight;
  }

  function pruneMessages(limit = 500) {
    const entries = [...state.messages.entries()];
    if (entries.length <= limit) return;
    entries.sort((a, b) => b[0] - a[0]).slice(limit).forEach(([id]) => {
      const node = state.messageDomById.get(id);
      if (node) { node.remove(); state.messageDomById.delete(id); }
      state.messages.delete(id);
    });
  }
  function ingest(payload) {
    const messages = Array.isArray(payload) ? payload : (payload.messages || [payload.message || payload]);
    messages.filter(Boolean).forEach(upsert);
  }
  const listeners = {};
  function onMessage(event) { ingest(event.detail); }
  function onRoster(event) {
    // Only apply a roster snapshot explicitly stamped for the open channel.
    // The workspace-wide SSE stream multiplexes every channel's roster through
    // this same event target; without this guard another channel's roster
    // (e.g. AGENT_INBOX_CHANNEL's full agent list) overwrites the open
    // channel's members and paints the wrong face-pile / sidebar.
    // 04-events.js::dispatch guards its own state.members write the same way
    // (exact channel match, so a missing channel fails closed), but listeners
    // re-derive from the raw detail and bypass it — so re-check identically.
    // Ignore a foreign-channel tick WITHOUT re-rendering: only the open
    // channel's roster changes what this view paints (name/sigil resolution),
    // and these ticks arrive constantly on the multiplexed stream.
    const detail = event.detail;
    if (!detail || detail.channel !== state.channel) return;
    if (!Array.isArray(detail.members)) return;
    // Re-render only when something the CONVERSATION paints actually changed.
    //
    // A roster event does not mean a person joined or left. The server
    // broadcasts whenever its snapshot differs, and that snapshot carries
    // messenger_heartbeat, watchdog_heartbeat and last_seen — which the
    // monitor rewrites every ~10s FOR EVERY MEMBER, with no message traffic at
    // all. So in a room of ten agents the snapshot changes every second or
    // two, forever. render() clears #messages and rebuilds every visible card
    // through cardFor -> paintBody -> two TreeWalkers, measured at ~116ms for
    // 500 messages, which threw away the whole point of the incremental
    // upsert() path on nothing but presence churn.
    //
    // What this view reads off a member is their display name and their
    // avatar tone (for the author line and @mention chips). Nothing else here
    // repaints from the roster — the face-pile and the drawer are 20-workspace's,
    // and they listen separately.
    const painted = list => (list || []).map(m => m.id + '\u0000' + (m.name || ''))
      .sort().join('');
    const before = painted([...state.members.values()]);
    state.members = new Map(detail.members.map(m => [m.id, m]));
    if (painted(detail.members) !== before) render();
  }
  function onBoot(event) { state.operator = event.detail?.operator || state.operator; }
  function onPrefsChanged() { render(); }
  function init() {
    state.operator = state.operator || state.meta?.operator;
    events.addEventListener('boot', onBoot); listeners.boot = onBoot;
    events.addEventListener('message', onMessage); listeners.message = onMessage;
    events.addEventListener('message_update', onMessage); listeners.message_update = onMessage;
    events.addEventListener('roster', onRoster); listeners.roster = onRoster;
    events.addEventListener('preferences:changed', onPrefsChanged); listeners['preferences:changed'] = onPrefsChanged;
    render();
    const list = dom(); const jump = document.getElementById('jump-latest');
    if (list && jump) {
      const onScroll = () => { jump.classList.toggle('hidden', nearBottom(list)); state.scrollPositions[convId()] = list.scrollTop; if (nearBottom(list)) markRead(); };
      const onClick = () => { list.scrollTop = list.scrollHeight; markRead(); };
      list.addEventListener('scroll', onScroll); listeners.listScroll = [list, onScroll];
      jump.addEventListener('click', onClick); listeners.jumpClick = [jump, onClick];
    }
  }
  function unmount() {
    cancelPendingInserts();
    for (const [type, fn] of Object.entries(listeners)) {
      if (Array.isArray(fn) && fn[0]?.removeEventListener) { fn[0].removeEventListener(type === 'listScroll' ? 'scroll' : 'click', fn[1]); }
      else { events?.removeEventListener(type, fn); }
    }
    Object.keys(listeners).forEach(k => delete listeners[k]);
  }
  function mount() { init(); }

  Trio.conversation = { init, mount, unmount, render, ingest, upsert, paintBody, cardFor, viewModel, answerPayload, isPrivate, seedWatermark };
})();
