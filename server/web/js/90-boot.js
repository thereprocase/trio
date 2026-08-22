(() => {
  'use strict';
  const Trio = window.Trio;
  const $ = id => document.getElementById(id);
  // The mobile nav drawer — the sidebar once it becomes an off-canvas overlay
  // below 880px. Two things were wrong with it, and they share a cause: the
  // drawer had no state, only a class.
  //
  //   * Closed, it was merely translated off-screen. That hides it from the eye
  //     and from nobody else — its ~20 controls stayed in the tab order and the
  //     accessibility tree, so a keyboard or screen-reader user walked the
  //     brand, every channel, every DM and the account trigger before reaching
  //     the conversation. `inert` is the single correct answer: it removes the
  //     subtree from focus, hit-testing and the a11y tree together. Deliberately
  //     NOT `aria-hidden`, which would leave the controls focusable while
  //     claiming they do not exist — the combination ARIA calls broken.
  //   * Nothing closed it. On a phone this drawer is the only way to change
  //     conversation, and it covers the conversation you just chose, so every
  //     navigation ended by hunting for the strip of scrim beside it.
  //
  // Exposed on Trio because the second fix belongs to whoever owns the
  // destination — 20-workspace's rail — not to boot(). Defined at module scope
  // rather than inside boot() so it exists even if boot() bails: a drawer that
  // cannot be closed is worse than one that never opens.
  const NARROW = '(max-width: 880px)';
  const narrowViewport = () => {
    try { return !!window.matchMedia?.(NARROW)?.matches; } catch { return false; }
  };
  // Deliberately the RAIL, not the aside. The aside's first button is
  // #sidebar-toggle (the desktop collapse control), which is `display:none
  // !important` below 880px — focusing it silently does nothing, so the drawer
  // would open and the keyboard user would still be stranded outside it. The
  // disabled filter is done in JS rather than in the selector because it has to
  // survive a fake DOM with no attribute-selector support.
  function firstRailControl() {
    const rail = $('workspace-rail') || $('sidebar');
    const items = rail?.querySelectorAll?.('button, a') || [];
    return [...items].find(el => !el.disabled) || null;
  }
  const drawer = {
    isOpen: () => !!$('app')?.classList.contains('nav-open'),
    isNarrow: narrowViewport,
    // The single place that reflects drawer state into the DOM, so inert, the
    // scrim and aria-expanded cannot drift apart. Called on open, on close, at
    // boot and on every breakpoint crossing — the boot call is also what gives
    // the closed drawer a correct initial state without depending on markup
    // staying in step with the code.
    //
    // `narrow` is an argument rather than a media query read in here for two
    // reasons: `inert` belongs to the CLOSED OVERLAY only (applying it above
    // 880px would disable the permanently-visible sidebar outright), and
    // tests/dom-harness stubs matchMedia to a permanent { matches: false }, so
    // anything reading the query internally could only ever test one branch.
    sync(narrow = narrowViewport()) {
      const open = drawer.isOpen();
      const aside = $('sidebar');
      if (aside) {
        if (narrow && !open) aside.setAttribute('inert', '');
        else aside.removeAttribute('inert');
      }
      const scrim = $('scrim-nav'); if (scrim) scrim.hidden = !open;
      $('nav-toggle')?.setAttribute('aria-expanded', open ? 'true' : 'false');
    },
    open(narrow = narrowViewport()) {
      const app = $('app'); if (!app || app.classList.contains('nav-open')) return;
      app.classList.add('nav-open');
      drawer.sync(narrow);
      // #workspace-rail PRECEDES #nav-toggle in the document, so a keyboard user
      // who opens the drawer and presses Tab moves FORWARD into the header
      // actions — away from the panel that just appeared, which they can only
      // reach by tabbing backwards past everything. Move into it explicitly.
      if (narrow) firstRailControl()?.focus?.();
    },
    // restoreFocus is for dismissals the keyboard initiated (Escape), where the
    // user expects to land back on the control they opened. Moving focus is not
    // always optional though: see focusInside below. The already-closed guard is
    // what makes this safe to call on every navigation.
    // preserveFocus is for the mobile→desktop crossing. There the drawer stops
    // being an overlay and becomes the permanent sidebar, so its contents are
    // still on screen and still usable — but #nav-toggle (`.hamb`) is
    // `display:none` above 880px, so the usual hand-back would strand focus on
    // an invisible control while the thing the user was in remains visible.
    close({ restoreFocus = false, preserveFocus = false, narrow = narrowViewport() } = {}) {
      const app = $('app'); if (!app || !app.classList.contains('nav-open')) return;
      const aside = $('sidebar');
      // If focus is still inside the drawer as it goes away — which is exactly
      // what happens when you TAP A CHANNEL, since the clicked row is in there —
      // the browser drops focus on <body> and a keyboard or switch user loses
      // their place in the document entirely. So this move is mandatory, not a
      // courtesy, and it does not depend on what the caller asked for.
      const focusInside = !!(aside && document.activeElement
                             && aside.contains?.(document.activeElement));
      app.classList.remove('nav-open');
      drawer.sync(narrow);
      if (!preserveFocus && (restoreFocus || focusInside)) $('nav-toggle')?.focus?.();
    },
  };
  Trio.nav = drawer;
  async function boot() {
    const mountFeatures = () => {
      ['conversation', 'workspace', 'agents', 'preferences', 'gameboyControls', 'ipodControls', 'router', 'composer'].forEach(name => {
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
    // BEFORE the await, deliberately. Trio.boot() fetches /api/meta, and until
    // it resolves the off-canvas sidebar would sit focusable and announced —
    // the exact defect the drawer exists to remove, held open for as long as
    // the network takes, and indefinitely if that request hangs. The listeners
    // below can wait for boot; the closed state cannot.
    drawer.sync();
    if (!(await Trio.boot(mountFeatures))) return;
    const navToggle = $('nav-toggle');
    navToggle?.addEventListener('click', () => drawer.isOpen() ? drawer.close() : drawer.open());
    $('scrim-nav')?.addEventListener('click', () => drawer.close());
    // Escape is the expected way out of any overlay, and it is the dismissal
    // that most needs focus handed back — a keyboard user who opened the drawer
    // from the hamburger would otherwise be dropped at the top of the document.
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && drawer.isOpen()) drawer.close({ restoreFocus: true });
    });
    // Rotating a phone or resizing a window crosses the breakpoint without any
    // click, so the drawer's state has to follow. Going wide must clear `inert`
    // or the now-permanent sidebar would be dead to keyboard and pointer alike;
    // going narrow must start closed rather than leaving an overlay covering the
    // conversation. Fires only on the crossing, so it costs nothing to keep.
    try {
      window.matchMedia?.(NARROW)?.addEventListener?.('change', event => {
        // Reset on BOTH directions. Going narrow must not leave an overlay
        // covering the conversation; going wide must not leave `nav-open`, a
        // revealed scrim and `aria-expanded=true` describing a drawer that is
        // now just the permanent sidebar.
        drawer.close({ narrow: event.matches, preserveFocus: !event.matches });
        drawer.sync(event.matches);
      });
    } catch { /* no matchMedia: desktop semantics, nothing to sync */ }
    drawer.sync();
    const themeToggle = document.getElementById('theme-toggle');
    themeToggle?.addEventListener('click', () => Trio.preferences?.toggle?.());
    ['search-btn', 'details-btn'].forEach(id => { const btn = document.getElementById(id); if (btn) { btn.disabled = false; btn.title = id === 'search-btn' ? 'Search (Ctrl/Cmd+K)' : 'Conversation details'; } });
    const archiveBtn = document.getElementById('archive-btn');
    archiveBtn?.addEventListener('click', () => Trio.workspace?.archiveCurrent?.());
  }
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', boot, {once:true}) : boot();
})();
