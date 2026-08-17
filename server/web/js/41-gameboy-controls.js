(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const THEME = 'historic-gameboy';
  const ACTION_LABELS = Object.freeze({
    up: 'D-pad up — move focus up',
    right: 'D-pad right — move focus right',
    down: 'D-pad down — move focus down',
    left: 'D-pad left — move focus left',
    a: 'A — activate focused control',
    b: 'B — close or go back',
    start: 'Start — primary action',
    select: 'Select — switch navigation region',
  });
  const FOCUSABLE = [
    'button:not([disabled])', 'a[href]', 'input:not([disabled])',
    'select:not([disabled])', 'textarea:not([disabled])',
    '[contenteditable="true"]', '[tabindex]:not([tabindex="-1"])',
  ].join(',');

  let controller = null;
  let lastFocused = null;
  let preferenceListener = null;
  let focusListener = null;

  function isGameboyTheme(theme) { return theme === THEME; }

  function center(rect) {
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  // Lower is better. Infinity means the candidate is not in that direction.
  // The cross-axis penalty keeps Up/Down moving through a visual column while
  // still allowing a diagonal escape when a row has no exact neighbour.
  function directionalScore(fromRect, toRect, direction) {
    const from = center(fromRect); const to = center(toRect);
    const dx = to.x - from.x; const dy = to.y - from.y;
    const primary = direction === 'left' ? -dx
      : direction === 'right' ? dx
      : direction === 'up' ? -dy : dy;
    if (primary <= 2) return Infinity;
    const cross = Math.abs(direction === 'left' || direction === 'right' ? dy : dx);
    const diagonalPenalty = cross > primary * 1.75 ? cross : 0;
    return primary + cross * 1.8 + diagonalPenalty;
  }

  function visible(element) {
    if (!element || element === controller || controller?.contains(element)) return false;
    if (element.hidden || element.closest('[hidden],[inert],[aria-hidden="true"]')) return false;
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function focusables(scope = document) {
    return [...scope.querySelectorAll(FOCUSABLE)].filter(visible);
  }

  function initialTarget() {
    const activeNav = document.querySelector('.nav-item.active,.dm-item.active,.rail-item.active');
    if (visible(activeNav)) return activeNav;
    return focusables(document.querySelector('main') || document)[0]
      || focusables(document.querySelector('.sidebar') || document)[0]
      || null;
  }

  function focusTarget(target) {
    if (!target) return false;
    target.focus({ preventScroll: true });
    target.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    lastFocused = target;
    return true;
  }

  function scrollSurface(direction, anchor) {
    const surface = anchor?.closest?.('.messages,.workspace-view,.view-scroll,.nav-scroll,.channel-drawer-body,.detail-body')
      || document.querySelector('.workspace-view:not(.hidden),.messages,.view-scroll');
    if (!surface?.scrollBy) return false;
    const vertical = direction === 'up' ? -96 : direction === 'down' ? 96 : 0;
    const horizontal = direction === 'left' ? -96 : direction === 'right' ? 96 : 0;
    surface.scrollBy({ top: vertical, left: horizontal, behavior: 'smooth' });
    return true;
  }

  function move(direction) {
    const items = focusables();
    if (!items.length) return false;
    const current = visible(lastFocused) ? lastFocused : initialTarget();
    if (!current) return focusTarget(items[0]);
    const fromRect = current.getBoundingClientRect();
    const next = items
      .filter(item => item !== current)
      .map(item => ({ item, score: directionalScore(fromRect, item.getBoundingClientRect(), direction) }))
      .filter(candidate => Number.isFinite(candidate.score))
      .sort((a, b) => a.score - b.score)[0]?.item;
    return next ? focusTarget(next) : scrollSurface(direction, current);
  }

  function activate() {
    const target = visible(lastFocused) ? lastFocused : initialTarget();
    if (!target) return false;
    if (target.matches('input:not([type="button"]):not([type="submit"]),textarea,select,[contenteditable="true"]')) {
      return focusTarget(target);
    }
    target.click?.();
    return true;
  }

  function closeTopLayer() {
    const dialog = [...document.querySelectorAll('dialog[open]')].pop();
    if (dialog) { dialog.close('cancel'); return true; }
    const drawerClose = document.querySelector('.channel-drawer.open #channel-drawer-close,.detail.open [data-close]');
    if (drawerClose) { drawerClose.click(); return true; }
    const openMenu = document.querySelector('#channel-menu:not([hidden]),#account.open,.account.open');
    if (openMenu) {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      return true;
    }
    return false;
  }

  function back() {
    if (closeTopLayer()) return true;
    const active = document.activeElement;
    if (active?.matches?.('input,textarea,select,[contenteditable="true"]')) {
      active.blur();
      return true;
    }
    const route = Trio.router?.current?.();
    if (route && route.name !== 'home') {
      Trio.router.navigate('home');
      requestAnimationFrame(() => focusTarget(document.querySelector('.nav-item.active,.rail-item.active') || initialTarget()));
      return true;
    }
    return focusTarget(document.querySelector('.nav-item.active,.dm-item.active,.rail-item.active') || initialTarget());
  }

  function start() {
    const dialogPrimary = document.querySelector('dialog[open] button.primary:not([disabled]),dialog[open] button[type="submit"]:not([disabled])');
    if (dialogPrimary) { dialogPrimary.click(); return true; }
    const send = document.getElementById('send');
    if (send && !send.disabled) { send.click(); return true; }
    const composer = document.getElementById('input');
    const route = Trio.router?.current?.();
    if (composer && (route?.name === 'channel' || route?.name === 'dm' || route?.name === 'audit')) return focusTarget(composer);
    const primary = document.querySelector('.workspace-view .view-hero-action:not([disabled]),.workspace-view .btn.primary:not([disabled]),main .btn.primary:not([disabled])');
    if (primary) { primary.click(); return true; }
    return activate();
  }

  function selectRegion() {
    const current = visible(lastFocused) ? lastFocused : initialTarget();
    const inSidebar = !!current?.closest?.('.sidebar');
    if (inSidebar) {
      const mainTarget = focusables(document.querySelector('main') || document)[0];
      return focusTarget(mainTarget);
    }
    const sidebar = document.querySelector('.sidebar');
    if (sidebar && getComputedStyle(sidebar).display === 'none') document.getElementById('nav-toggle')?.click();
    const navTarget = document.querySelector('.nav-item.active,.dm-item.active,.rail-item.active')
      || focusables(sidebar || document)[0];
    return focusTarget(navTarget);
  }

  function press(action) {
    if (!isGameboyTheme(document.documentElement.dataset.theme)) return false;
    if (['up', 'right', 'down', 'left'].includes(action)) return move(action);
    if (action === 'a') return activate();
    if (action === 'b') return back();
    if (action === 'start') return start();
    if (action === 'select') return selectRegion();
    return false;
  }

  function buildController() {
    const node = document.createElement('aside');
    node.id = 'gameboy-controls';
    node.className = 'gameboy-controls';
    node.setAttribute('aria-label', 'Game Boy workspace controller');
    node.hidden = true;
    node.innerHTML = `
      <div class="gb-dpad" role="group" aria-label="D-pad">
        <button type="button" class="gb-pad gb-up" data-gb-action="up" aria-label="${ACTION_LABELS.up}"><span aria-hidden="true">▲</span></button>
        <button type="button" class="gb-pad gb-right" data-gb-action="right" aria-label="${ACTION_LABELS.right}"><span aria-hidden="true">▶</span></button>
        <button type="button" class="gb-pad gb-down" data-gb-action="down" aria-label="${ACTION_LABELS.down}"><span aria-hidden="true">▼</span></button>
        <button type="button" class="gb-pad gb-left" data-gb-action="left" aria-label="${ACTION_LABELS.left}"><span aria-hidden="true">◀</span></button>
        <span class="gb-pad-center" aria-hidden="true"></span>
      </div>
      <div class="gb-system" role="group" aria-label="System controls">
        <button type="button" data-gb-action="select" aria-label="${ACTION_LABELS.select}"><span></span>SELECT</button>
        <button type="button" data-gb-action="start" aria-label="${ACTION_LABELS.start}"><span></span>START</button>
      </div>
      <div class="gb-actions" role="group" aria-label="Action buttons">
        <button type="button" class="gb-round gb-b" data-gb-action="b" aria-label="${ACTION_LABELS.b}">B</button>
        <button type="button" class="gb-round gb-a" data-gb-action="a" aria-label="${ACTION_LABELS.a}">A</button>
      </div>
      <div class="gb-legend" aria-hidden="true">D-PAD MOVE · A OPEN · B BACK</div>`;
    node.addEventListener('click', event => {
      const button = event.target.closest('[data-gb-action]');
      if (button) press(button.dataset.gbAction);
    });
    return node;
  }

  function sync() {
    if (!controller) return;
    const active = isGameboyTheme(document.documentElement.dataset.theme);
    controller.hidden = !active;
    document.body?.classList.toggle('gameboy-controller-on', active);
    if (!active) lastFocused = null;
  }

  function mount() {
    if (!controller) {
      controller = buildController();
      document.body.append(controller);
    }
    preferenceListener = sync;
    Trio.events?.addEventListener?.('preferences:changed', preferenceListener);
    focusListener = event => { if (!controller?.contains(event.target)) lastFocused = event.target; };
    document.addEventListener('focusin', focusListener);
    sync();
  }

  function unmount() {
    if (preferenceListener) Trio.events?.removeEventListener?.('preferences:changed', preferenceListener);
    if (focusListener) document.removeEventListener('focusin', focusListener);
    controller?.remove();
    controller = preferenceListener = focusListener = lastFocused = null;
    document.body?.classList.remove('gameboy-controller-on');
  }

  Trio.gameboyControls = {
    mount, unmount, press, move, directionalScore, isGameboyTheme,
    actionLabels: ACTION_LABELS,
  };
})();
