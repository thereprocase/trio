(() => {
  'use strict';
  const Trio = window.Trio;
  async function boot() {
    const mountFeatures = () => {
      ['conversation', 'workspace', 'agents', 'preferences', 'router', 'composer'].forEach(name => {
        const feature = Trio[name];
        if (!feature) return;
        // Isolate each mount: one feature throwing must not skip the rest —
        // otherwise an early failure (conversation/workspace/agents) would
        // cascade and leave preferences (theme) + router (home/attention/…)
        // unmounted, i.e. wrong theme + empty non-channel pages together.
        try { Trio.lifecycle?.mount?.(name, feature); }
        catch (e) { console.error('boot: mounting "' + name + '" failed', e); }
      });
    };
    if (!(await Trio.boot(mountFeatures))) return;
    const app = document.getElementById('app');
    const nav = document.getElementById('nav-toggle');
    const scrim = document.getElementById('scrim-nav');
    const closeNav = () => { app?.classList.remove('nav-open'); if (scrim) scrim.hidden = true; };
    nav?.addEventListener('click', () => { app?.classList.add('nav-open'); if (scrim) scrim.hidden = false; });
    scrim?.addEventListener('click', closeNav);
    const themeToggle = document.getElementById('theme-toggle');
    themeToggle?.addEventListener('click', () => Trio.preferences?.toggle?.());
    ['search-btn', 'details-btn'].forEach(id => { const btn = document.getElementById(id); if (btn) { btn.disabled = false; btn.title = id === 'search-btn' ? 'Search (Ctrl/Cmd+K)' : 'Conversation details'; } });
    const archiveBtn = document.getElementById('archive-btn');
    archiveBtn?.addEventListener('click', () => Trio.workspace?.archiveCurrent?.());
    if (Trio.state.conversation?.kind === 'dm' && Trio.workspace?.openDmByKey) {
      Trio.workspace.openDmByKey(Trio.state.conversation.key);
    }
  }
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', boot, {once:true}) : boot();
})();
