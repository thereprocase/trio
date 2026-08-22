(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  // Resizable + collapsible left sidebar. Drag the right edge to resize; drag
  // it narrower than SNAP_THRESHOLD and it snaps to a 56px icon rail on release.
  // The sidebar-toggle button (formerly the orphan workspace-switch chevron)
  // expands/collapses explicitly. State persists in localStorage.
  const KEY = 'trio.sidebar.v1';
  const MIN_EXPANDED = 220;   // floor for the expanded width
  const MAX_EXPANDED = 480;   // ceiling (also clamped to viewport on drag)
  const COLLAPSED_WIDTH = 56; // icon-rail width
  const SNAP_THRESHOLD = 160; // release below this → collapse
  const DEFAULTS = { width: 300, collapsed: false };

  // Double-chevron icons — « collapses (points left, into the rail), » expands
  // (points right, back out). Cleaner than the panel+chevron it replaced.
  const COLLAPSE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m11 9-3 3 3 3M17 9l-3 3 3 3"/></svg>';
  const EXPAND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m13 9 3 3-3 3M7 9l3 3-3 3"/></svg>';

  function read() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
      const collapsed = !!raw.collapsed;
      let width = Math.round(Number(raw.width));
      if (!width || !isFinite(width)) width = DEFAULTS.width;
      width = Math.max(MIN_EXPANDED, Math.min(MAX_EXPANDED, width));
      return { collapsed, width };
    } catch { return { ...DEFAULTS }; }
  }
  function save(state) { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch {} }

  function syncToggleIcon(state) {
    const btn = document.getElementById('sidebar-toggle');
    if (!btn) return;
    btn.innerHTML = state.collapsed ? EXPAND_SVG : COLLAPSE_SVG;
    const label = state.collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    btn.setAttribute('aria-label', label);
    btn.title = label;
    btn.setAttribute('aria-expanded', String(!state.collapsed));
  }

  function apply(state) {
    const app = document.getElementById('app');
    if (!app) return;
    app.classList.toggle('sidebar-collapsed', state.collapsed);
    app.style.setProperty('--sidebar-width', (state.collapsed ? COLLAPSED_WIDTH : state.width) + 'px');
    const handle = document.getElementById('sidebar-resize');
    if (handle) handle.setAttribute('aria-valuenow', String(state.collapsed ? COLLAPSED_WIDTH : state.width));
    syncToggleIcon(state);
  }

  function startResize(event) {
    if (event.button !== 0) return;
    const app = document.getElementById('app');
    const handle = document.getElementById('sidebar-resize');
    if (!app || !handle) return;
    // Skip on mobile — sidebar is an off-canvas drawer there, not a grid column.
    if (window.matchMedia('(max-width: 880px)').matches) return;
    event.preventDefault();
    const state = read();
    const startWidth = state.collapsed ? COLLAPSED_WIDTH : state.width;
    const startX = event.clientX;
    const maxWidth = Math.min(MAX_EXPANDED, Math.max(MIN_EXPANDED, window.innerWidth - 420));
    app.classList.add('is-resizing');
    let currentWidth = startWidth;
    const move = m => {
      currentWidth = Math.round(startWidth + (m.clientX - startX));
      const clamped = Math.max(COLLAPSED_WIDTH, Math.min(maxWidth, currentWidth));
      // Live-preview the collapsed rail (icons only) once the user drags past
      // the snap threshold, so the snap on release isn't a visual jump.
      const collapsing = clamped < SNAP_THRESHOLD;
      app.classList.toggle('sidebar-collapsed', collapsing);
      app.style.setProperty('--sidebar-width', (collapsing ? COLLAPSED_WIDTH : clamped) + 'px');
      handle.setAttribute('aria-valuenow', String(collapsing ? COLLAPSED_WIDTH : clamped));
    };
    const end = () => {
      app.classList.remove('is-resizing');
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', end);
      if (currentWidth < SNAP_THRESHOLD) {
        const next = { ...read(), collapsed: true };
        save(next); apply(next);
      } else {
        const width = Math.max(MIN_EXPANDED, Math.min(maxWidth, currentWidth));
        const next = { collapsed: false, width };
        save(next); apply(next);
      }
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', end, { once: true });
  }

  function toggle() {
    const next = { ...read() };
    next.collapsed = !next.collapsed;
    save(next); apply(next);
  }

  // ── Floating tooltip for collapsed-rail icons ──────────────────────────
  // The nav-scroll container has overflow:auto, so a CSS ::after tooltip gets
  // clipped. This floats a div on <body> instead, positioned next to the item.
  const TIP_DELAY = 400; // ms before showing — the "split sec"
  let tipEl = null, tipTimer = null, tipTarget = null;
  function ensureTipEl() {
    if (tipEl) return tipEl;
    tipEl = document.createElement('div');
    tipEl.className = 'sidebar-tip';
    tipEl.setAttribute('role', 'tooltip');
    document.body.append(tipEl);
    return tipEl;
  }
  function showTip(target) {
    const text = target.getAttribute('data-tip');
    if (!text) return;
    const el = ensureTipEl();
    el.textContent = text;
    const rect = target.getBoundingClientRect();
    el.style.left = (rect.right + 10) + 'px';
    el.style.top = (rect.top + rect.height / 2) + 'px';
    el.style.transform = 'translateY(-50%)';
    requestAnimationFrame(() => el.classList.add('show'));
  }
  function hideTip() {
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
    tipTarget = null;
    if (tipEl) tipEl.classList.remove('show');
  }
  function isCollapsed() {
    return document.getElementById('app')?.classList.contains('sidebar-collapsed');
  }

  function init() {
    apply(read());
    document.getElementById('sidebar-resize')?.addEventListener('pointerdown', startResize);
    const btn = document.getElementById('sidebar-toggle');
    btn?.addEventListener('click', toggle);
    // Double-click the handle as a quick collapse/expand shortcut.
    document.getElementById('sidebar-resize')?.addEventListener('dblclick', toggle);
    // Keep the ceiling honest if the viewport shrinks below the stored width.
    window.addEventListener('resize', () => {
      const state = read();
      if (!state.collapsed && state.width > window.innerWidth - 420 && window.innerWidth > 880) {
        const width = Math.max(MIN_EXPANDED, Math.min(MAX_EXPANDED, window.innerWidth - 420));
        const next = { collapsed: false, width };
        save(next); apply(next);
      }
    }, { passive: true });
    // Tooltip: event delegation on the rail — survives renderRail() re-renders.
    const rail = document.getElementById('workspace-rail');
    if (rail) {
      rail.addEventListener('mouseover', e => {
        const target = e.target.closest('[data-tip]');
        if (!target || target === tipTarget) return;
        if (!isCollapsed()) return;
        hideTip();
        tipTarget = target;
        tipTimer = setTimeout(() => showTip(target), TIP_DELAY);
      });
      rail.addEventListener('mouseout', e => {
        const target = e.target.closest('[data-tip]');
        if (!target) return;
        const related = e.relatedTarget?.closest('[data-tip]');
        if (related === target) return; // moving within the same item
        hideTip();
      });
      // If the rail scrolls while a tip is pending/showing, reposition or hide.
      rail.addEventListener('scroll', hideTip, { passive: true });
    }
    // Hide tooltip when collapsing/expanding or leaving the window.
    document.getElementById('app')?.addEventListener('transitionstart', e => {
      if (e.propertyName === 'grid-template-columns') hideTip();
    });
    document.addEventListener('mouseleave', hideTip);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();

  Trio.sidebar = { apply, toggle, read };
})();
