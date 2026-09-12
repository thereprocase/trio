(() => {
  'use strict';
  const Trio = window.Trio;
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  const KEY = 'trio.preferences.v1';
  // Each preset is a self-contained design — background family AND accent —
  // so there's nothing left to pick independently. Inspired presets go one
  // step further: css/35-historic.css replaces the component language too.
  // `mode` still records which side of the light/dark quick-toggle remembers
  // the preset; `family` controls how it is grouped in the picker.
  const themes = [
    { id: 'light-1', mode: 'light', label: 'Sagebrush' },
    { id: 'light-2', mode: 'light', label: 'Frost' },
    { id: 'light-3', mode: 'light', label: 'Slate' },
    { id: 'light-4', mode: 'light', label: 'Linen' },
    { id: 'light-5', mode: 'light', label: 'Clay' },
    { id: 'light-6', mode: 'light', label: 'Mojave' },
    { id: 'dark-1', mode: 'dark', label: 'Midnight' },
    { id: 'dark-2', mode: 'dark', label: 'Terminal' },
    { id: 'dark-3', mode: 'dark', label: 'Graphite' },
    { id: 'dark-4', mode: 'dark', label: 'Abyss' },
    { id: 'dark-5', mode: 'dark', label: 'Noir' },
    { id: 'dark-6', mode: 'dark', label: 'Torch' },
    { id: 'historic-win98', mode: 'light', family: 'inspired', label: 'Start Menu' },
    { id: 'historic-gameboy', mode: 'light', family: 'inspired', label: 'Link Cable' },
    { id: 'historic-geocities', mode: 'dark', family: 'inspired', label: 'Webmaster' },
    { id: 'inspired-ipod', mode: 'light', family: 'inspired', label: 'Now Playing' },
    { id: 'inspired-messenger', mode: 'dark', family: 'inspired', label: 'Walled Garden' },
    { id: 'inspired-slack', mode: 'light', family: 'inspired', label: 'Threaded' },
    { id: 'inspired-trailhead', mode: 'dark', family: 'inspired', label: 'Trailhead' },
    { id: 'inspired-high-tide', mode: 'light', family: 'inspired', label: 'High Tide' },
  ];
  const lightThemes = themes.filter(theme => theme.mode === 'light');
  const darkThemes = themes.filter(theme => theme.mode === 'dark');
  const modernLightThemes = lightThemes.filter(theme => theme.family !== 'inspired');
  const modernDarkThemes = darkThemes.filter(theme => theme.family !== 'inspired');
  const inspiredThemes = themes.filter(theme => theme.family === 'inspired');
  const THEME_CARD_WIDTH = 104;
  const THEME_CARD_GAP = 10;
  // Keep incomplete rows visually balanced, but never split a group that can
  // still fit in one row. Once it overflows, reduce only when a whole card no
  // longer fits: 7 -> 4/3 -> 3/2/2 and 10 -> 5/5 -> 4/3/3 -> 3/3/2/2.
  function themeGridColumns(count, availableWidth) {
    const fit = Math.max(1, Math.floor((availableWidth + THEME_CARD_GAP) / (THEME_CARD_WIDTH + THEME_CARD_GAP)));
    if (fit >= count) return count;
    return Math.min(Math.ceil(count / 2), fit);
  }
  const themeIds = themes.map(theme => theme.id);
  const lightThemeIds = lightThemes.map(theme => theme.id);
  const darkThemeIds = darkThemes.map(theme => theme.id);
  // Notification tiers, highest priority first — a message is classified
  // into the FIRST tier it qualifies for (see Trio.notifications.classify):
  //   dm      a private DM to you
  //   mention an @you mention, or a !you / !all unfilterable bang (both mean
  //           "someone specifically wants your attention" — one tier, not two)
  //   ref     a #you reference (mentioned about, not pinged)
  //   plain   any other message in a channel you're viewing, no targeting
  // `chime`/`notifications` below stay the master kill switches; the per-tier
  // *Tier* keys only matter when their master is on. Each tier also gets an
  // independent chime *sound preset* — see Trio.notifications.SOUNDS.
  const NOTIFICATION_TIERS = ['dm', 'mention', 'ref', 'plain'];
  const SOUND_IDS = ['ping', 'alert', 'tick'];
  const defaults = { theme: 'light-1', lightTheme: 'light-1', darkTheme: 'dark-3', font: 'default', compact: false, messageNumbers: false, notifications: true, chime: false, chimeVolume: 0.5, dictation: true, sttMode: 'local', staleThreadDays: 7,
    chimeTierDm: true, chimeTierMention: true, chimeTierRef: true, chimeTierPlain: false,
    notifyTierDm: true, notifyTierMention: true, notifyTierRef: false, notifyTierPlain: false,
    chimeSoundDm: 'alert', chimeSoundMention: 'ping', chimeSoundRef: 'tick', chimeSoundPlain: 'tick' };
  const schema = { theme: themeIds, lightTheme: lightThemeIds, darkTheme: darkThemeIds, font: ['default','serif','mono'], compact: 'boolean', messageNumbers: 'boolean', notifications: 'boolean', chime: 'boolean', chimeVolume: 'number', dictation: 'boolean', sttMode: ['local','web'], staleThreadDays: 'number',
    chimeTierDm: 'boolean', chimeTierMention: 'boolean', chimeTierRef: 'boolean', chimeTierPlain: 'boolean',
    notifyTierDm: 'boolean', notifyTierMention: 'boolean', notifyTierRef: 'boolean', notifyTierPlain: 'boolean',
    chimeSoundDm: SOUND_IDS, chimeSoundMention: SOUND_IDS, chimeSoundRef: SOUND_IDS, chimeSoundPlain: SOUND_IDS };
  function cast(key, value) {
    if (schema[key] === 'boolean') return !!value;
    if (schema[key] === 'number') { const n = Number(value); return Number.isFinite(n) && n >= 0 ? n : defaults[key]; }
    if (Array.isArray(schema[key])) return schema[key].includes(value) ? value : defaults[key];
    return value ?? defaults[key];
  }
  function readFromStorage() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
      const legacyTheme = raw.theme === 'dark' ? defaults.darkTheme : raw.theme === 'light' ? defaults.lightTheme : null;
      const next = { ...defaults, ...raw };
      if (legacyTheme) next.theme = legacyTheme;
      if (!raw.lightTheme) next.lightTheme = raw.theme && raw.theme.startsWith('light-') ? raw.theme : defaults.lightTheme;
      if (!raw.darkTheme) next.darkTheme = raw.theme && raw.theme.startsWith('dark-') ? raw.theme : defaults.darkTheme;
      for (const k of Object.keys(schema)) next[k] = cast(k, next[k]);
      return next;
    } catch { return { ...defaults }; }
  }
  function read() { return Trio.store ? Trio.store.get('preferences') : (Trio.state.preferences || readFromStorage()); }
  function requestNotifications() { if (typeof Notification !== 'undefined' && Notification.permission === 'default') { Notification.requestPermission().then(p => { if (p !== 'granted') save({ notifications: false }); }); } }
  // Moon = "click to go dark" (currently light); Sun = "click to go light"
  // (currently dark). The icon always previews the theme you'll switch TO.
  const MOON_PATH = 'M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z';
  const SUN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
  function syncToggleIcon(themeId) {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const selected = themes.find(t => t.id === themeId);
    const isDark = selected ? selected.mode === 'dark' : String(themeId || '').startsWith('dark-');
    if (isDark) {
      btn.innerHTML = SUN_SVG;
      btn.setAttribute('aria-label', 'Switch to light theme');
      btn.title = 'Switch to light theme';
    } else {
      btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${MOON_PATH}"/></svg>`;
      btn.setAttribute('aria-label', 'Switch to dark theme');
      btn.title = 'Switch to dark theme';
    }
  }
  function apply(next = readFromStorage()) {
    const root = document.documentElement;
    root.dataset.theme = next.theme;
    root.dataset.font = next.font;
    document.body?.classList.toggle('compact', !!next.compact);
    document.body?.classList.toggle('message-numbers', !!next.messageNumbers);
    if (next.notifications) requestNotifications();
    if (Trio.store) Trio.store.set('preferences', next);
    else Trio.state.preferences = next;
    syncToggleIcon(next.theme);
    return next;
  }
  function save(change) {
    const current = read(); const next = { ...current };
    for (const k of Object.keys(schema)) if (change[k] !== undefined) next[k] = cast(k, change[k]);
    localStorage.setItem(KEY, JSON.stringify(next));
    apply(next);
    Trio.events.dispatchEvent(new CustomEvent('preferences:changed', { detail: next }));
    return next;
  }
  function selectTheme(theme) {
    const selected = themes.find(option => option.id === theme);
    if (!selected) return read();
    return save(selected.mode === 'light' ? { theme, lightTheme: theme } : { theme, darkTheme: theme });
  }
  function toggle() {
    const current = read();
    const currentTheme = themes.find(theme => theme.id === current.theme);
    return save({ theme: currentTheme?.mode === 'dark' ? current.lightTheme : current.darkTheme });
  }
  function reset() {
    localStorage.removeItem(KEY);
    const next = { ...defaults };
    apply(next);
    // Mirror save(): apply() alone doesn't notify listeners (composer
    // dictation visibility, notification wiring), so a reset left them stale
    // until the next save(). Dispatch the same event so they react immediately.
    Trio.events.dispatchEvent(new CustomEvent('preferences:changed', { detail: next }));
    return next;
  }
  function diagnostics() { const note = typeof Notification !== 'undefined' ? Notification.permission : 'unavailable'; return { online: navigator.onLine ? 'yes' : 'no', channel: (Trio.store ? Trio.store.get('session.channel') : Trio.state.channel) || '', theme: readFromStorage().theme, agents: ((Trio.store ? Trio.store.get('agents.list') : Trio.state.agents) || []).length, notifications: note, stt: Trio.state.sttHealth || 'checking' }; }
  async function checkStt() {
    // secure_url rides on this response: on an insecure origin the mic is
    // unavailable and the only useful thing to tell the operator is the
    // address that WOULD work, which the browser cannot know and the server
    // can. The composer reads it from state when explaining a dead mic button.
    try {
      const h = await Trio.api.get('/api/stt/health');
      Trio.state.sttHealth = h && h.ok ? 'ready' : 'unavailable';
      Trio.state.secureUrl = (h && h.secure_url) || '';
    }
    catch { Trio.state.sttHealth = 'unavailable'; }
  }
  function renderPage(panel) {
    panel._themeLayoutObserver?.disconnect();
    panel.replaceChildren();
    const p = read();
    const hero = document.createElement('div'); hero.className = 'view-hero'; hero.innerHTML = '<h2>Settings & diagnostics</h2><p>Shape how the workspace looks, sounds, and keeps you informed.</p>';
    const appearance = document.createElement('section'); appearance.className = 'pref-group'; appearance.innerHTML = '<h3>Appearance</h3>';
    const themeRow = document.createElement('div'); themeRow.className = 'pref-row pref-row-themes'; themeRow.innerHTML = '<div class="pr-txt"><div class="l">Theme presets</div><div class="d">Choose a palette or an Inspired interface that restyles the controls, windows, navigation, and messages too.</div></div>';
    const themeChoices = document.createElement('div'); themeChoices.className = 'theme-choice';
    const themeGroups = [];
    [['lightTheme', 'Light default', modernLightThemes], ['darkTheme', 'Dark default', modernDarkThemes], ['theme', 'Inspired', inspiredThemes]].forEach(([key, label, options]) => {
      const group = document.createElement('div'); group.className = `theme-choice-group theme-choice-group-${key}`;
      group.innerHTML = `<div class="theme-group-label">${label}</div>`;
      const choices = document.createElement('div'); choices.className = 'theme-choice';
      options.forEach(option => { const selected = p[key] === option.id; const b = document.createElement('button'); b.type = 'button'; b.className = 'theme-opt' + (selected ? ' on' : ''); b.setAttribute('aria-pressed', selected ? 'true' : 'false'); b.innerHTML = `<span class="swatch" data-theme="${option.id}"><span class="a"></span><span class="b"></span></span><span class="tl">${option.label}</span>`; b.addEventListener('click', () => { selectTheme(option.id); renderPage(panel); }); choices.append(b); });
      group.append(choices); themeChoices.append(group); themeGroups.push({ choices, count: options.length });
    });
    themeRow.append(themeChoices); appearance.append(themeRow);
    const behavior = document.createElement('section'); behavior.className = 'pref-group'; behavior.innerHTML = '<h3>Workspace behavior</h3>';
    const behaviors = [['compact','Compact messages','Tighter spacing for dense, high-volume channels.'],['messageNumbers','Message numbers','Show message IDs beside timestamps.'],['notifications','Desktop notifications','Master switch — which message types actually pop one is set below.'],['chime','Notification chime','Master switch — which message types actually play one, and which sound, is set below.'],['dictation','Dictation','Keep the microphone control available in the composer.']];
    behaviors.forEach(([key,label,description]) => { const row = document.createElement('div'); row.className = 'pref-row'; const text = document.createElement('div'); text.className = 'pr-txt'; text.innerHTML = `<div class="l">${esc(label)}</div><div class="d">${esc(description)}</div>`; const toggle = document.createElement('label'); toggle.className = 'switch'; toggle.innerHTML = `<input type="checkbox" ${p[key] ? 'checked' : ''} aria-label="${esc(label)}"><span class="track"></span><span class="knob"></span>`; toggle.querySelector('input').addEventListener('change', event => save({[key]:event.target.checked})); row.append(text, toggle); behavior.append(row); });
    const historyRow = document.createElement('div'); historyRow.className = 'pref-row';
    const historyText = document.createElement('div'); historyText.className = 'pr-txt'; historyText.innerHTML = '<div class="l">Hide old threads</div><div class="d">Move channels and DMs with no activity for this long into a &ldquo;show older&rdquo; group in the sidebar. Nothing is archived or deleted, and anything unread stays put.</div>';
    const historySelect = document.createElement('select'); historySelect.className = 'pref-select'; historySelect.setAttribute('aria-label', 'Hide old threads after');
    const historyOptions = [[3,'3 days'],[7,'7 days'],[14,'14 days'],[30,'30 days'],[0,'Never']];
    historyOptions.forEach(([value,label]) => { const opt = document.createElement('option'); opt.value = String(value); opt.textContent = label; if (Number(p.staleThreadDays) === value) opt.selected = true; historySelect.append(opt); });
    historySelect.addEventListener('change', () => save({ staleThreadDays: Number(historySelect.value) }));
    historyRow.append(historyText, historySelect); behavior.append(historyRow);
    const sttRow = document.createElement('div'); sttRow.className = 'pref-row';
    const sttText = document.createElement('div'); sttText.className = 'pr-txt'; sttText.innerHTML = '<div class="l">Speech-to-text engine</div><div class="d">Local runs Whisper on this machine and keeps audio off the network. Browser uses your browser\'s built-in speech recognition (routed through its vendor\'s cloud service) — faster to start, no local model needed.</div>';
    const sttSelect = document.createElement('select'); sttSelect.className = 'pref-select'; sttSelect.setAttribute('aria-label', 'Speech-to-text engine');
    [['local','Local (Whisper, on-device)'],['web','Browser (built-in speech recognition)']].forEach(([value,label]) => { const opt = document.createElement('option'); opt.value = value; opt.textContent = label; if (p.sttMode === value) opt.selected = true; sttSelect.append(opt); });
    sttSelect.addEventListener('change', () => save({ sttMode: sttSelect.value }));
    sttRow.append(sttText, sttSelect); behavior.append(sttRow);
    const notifyGroup = document.createElement('section'); notifyGroup.className = 'pref-group';
    notifyGroup.innerHTML = '<h3>Notification rules</h3><p class="pref-group-note">Each message is classified into exactly one row — the highest that applies. A DM always wins; an @mention (or an unfilterable !bang) beats a #reference, which beats an untargeted channel message. Set chime/popup independently per row.</p>';
    const tierLabels = { dm: 'Direct message', mention: '@Mention (or !bang)', ref: '#Reference', plain: 'Other channel message' };
    const soundLabels = { ping: 'Ping', alert: 'Alert', tick: 'Tick' };
    const tierTable = document.createElement('div'); tierTable.className = 'notify-tier-table';
    tierTable.innerHTML = '<div class="notify-tier-row notify-tier-head"><span>Message type</span><span>Chime</span><span>Sound</span><span>Desktop popup</span></div>';
    NOTIFICATION_TIERS.forEach(tier => {
      const row = document.createElement('div'); row.className = 'notify-tier-row';
      const label = document.createElement('span'); label.textContent = tierLabels[tier]; row.append(label);
      const chimeToggle = document.createElement('label'); chimeToggle.className = 'switch switch-sm';
      chimeToggle.innerHTML = `<input type="checkbox" ${p['chimeTier' + cap(tier)] ? 'checked' : ''} aria-label="Chime for ${esc(tierLabels[tier])}"><span class="track"></span><span class="knob"></span>`;
      chimeToggle.querySelector('input').addEventListener('change', event => save({ ['chimeTier' + cap(tier)]: event.target.checked }));
      row.append(chimeToggle);
      const soundWrap = document.createElement('span'); soundWrap.className = 'notify-tier-sound';
      const soundSelect = document.createElement('select'); soundSelect.className = 'pref-select'; soundSelect.setAttribute('aria-label', `Chime sound for ${tierLabels[tier]}`);
      SOUND_IDS.forEach(id => { const opt = document.createElement('option'); opt.value = id; opt.textContent = soundLabels[id]; if (p['chimeSound' + cap(tier)] === id) opt.selected = true; soundSelect.append(opt); });
      soundSelect.addEventListener('change', () => save({ ['chimeSound' + cap(tier)]: soundSelect.value }));
      const previewBtn = document.createElement('button'); previewBtn.type = 'button'; previewBtn.className = 'btn ghost sm'; previewBtn.textContent = 'Preview';
      previewBtn.addEventListener('click', () => Trio.notifications?.playPreset?.(soundSelect.value, p.chimeVolume));
      soundWrap.append(soundSelect, previewBtn); row.append(soundWrap);
      const notifyToggle = document.createElement('label'); notifyToggle.className = 'switch switch-sm';
      notifyToggle.innerHTML = `<input type="checkbox" ${p['notifyTier' + cap(tier)] ? 'checked' : ''} aria-label="Desktop popup for ${esc(tierLabels[tier])}"><span class="track"></span><span class="knob"></span>`;
      notifyToggle.querySelector('input').addEventListener('change', event => save({ ['notifyTier' + cap(tier)]: event.target.checked }));
      row.append(notifyToggle);
      tierTable.append(row);
    });
    notifyGroup.append(tierTable);
    const volRow = document.createElement('div'); volRow.className = 'pref-row';
    const volText = document.createElement('div'); volText.className = 'pr-txt'; volText.innerHTML = '<div class="l">Chime volume</div><div class="d">Applies to every tier\'s chime.</div>';
    const volInput = document.createElement('input'); volInput.type = 'range'; volInput.min = '0'; volInput.max = '1'; volInput.step = '0.05'; volInput.value = String(p.chimeVolume); volInput.className = 'pref-range'; volInput.setAttribute('aria-label', 'Chime volume');
    volInput.addEventListener('change', () => save({ chimeVolume: Number(volInput.value) }));
    volRow.append(volText, volInput); notifyGroup.append(volRow);
    const diagnosticsGroup = document.createElement('section'); diagnosticsGroup.className = 'pref-group'; diagnosticsGroup.innerHTML = '<h3>Diagnostics</h3>';
    const diagnostic = diagnostics(); Object.entries(diagnostic).forEach(([key,value]) => { const row = document.createElement('div'); row.className = 'diag-card'; row.innerHTML = `<span class="di ${key === 'online' || key === 'stt' ? 'ok' : 'off'}">●</span><div class="dtxt"><div class="dl">${esc(key.replace(/([A-Z])/g,' $1'))}<span class="stat-chip-sm ${key === 'online' ? 'ok' : 'off'}">${esc(String(value))}</span></div></div>`; diagnosticsGroup.append(row); });
    const resetButton = document.createElement('button'); resetButton.type = 'button'; resetButton.className = 'reset-prefs'; resetButton.textContent = 'Reset to defaults'; resetButton.addEventListener('click', () => { reset(); renderPage(panel); }); diagnosticsGroup.append(resetButton);
    const attribution = document.createElement('div'); attribution.className = 'pref-attribution';
    attribution.innerHTML = 'Character and brand icons from <a href="https://www.svgrepo.com/" target="_blank" rel="noreferrer">SVG Repo</a>, licensed under <a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noreferrer">CC BY 4.0</a>.';
    panel.append(hero, appearance, behavior, notifyGroup, diagnosticsGroup, attribution);
    const layoutThemes = () => themeGroups.forEach(({ choices, count }) => {
      choices.style.setProperty('--theme-columns', themeGridColumns(count, choices.clientWidth));
    });
    if (typeof ResizeObserver !== 'undefined') {
      panel._themeLayoutObserver = new ResizeObserver(layoutThemes);
      themeGroups.forEach(({ choices }) => panel._themeLayoutObserver.observe(choices));
    }
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(layoutThemes);
    else layoutThemes();
  }
  function init() { apply(); }
  function mount() { init(); }
  function unmount() {}
  Trio.preferences = { init, mount, unmount, apply, save, selectTheme, toggle, reset, read, diagnostics, renderPage, themeGridColumns, themes, lightThemes, darkThemes, inspiredThemes };
})();
