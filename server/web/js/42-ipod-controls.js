(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const THEME = 'inspired-ipod';
  const ACTION_LABELS = Object.freeze({
    menu: 'Menu — close or go back',
    previous: 'Previous — move focus left',
    next: 'Next — move focus right',
    play: 'Play/Pause — primary action',
    select: 'Center button — activate focused control',
    wheel: 'Scroll or rotate the wheel — move focus up or down',
  });

  let controller = null;
  let preferenceListener = null;
  let dragAngle = null;
  let dragTravel = 0;
  let wheelTravel = 0;

  function isIpodTheme(theme) { return theme === THEME; }

  function navigate(action) {
    if (!isIpodTheme(document.documentElement.dataset.theme)) return false;
    const controls = Trio.gameboyControls;
    if (!controls) return false;
    if (action === 'menu') return controls.back();
    if (action === 'previous') return controls.move('left');
    if (action === 'next') return controls.move('right');
    if (action === 'play') return controls.start();
    if (action === 'select') return controls.activate();
    if (action === 'up' || action === 'down') return controls.move(action);
    return false;
  }

  function angleFor(event) {
    const rect = controller.querySelector('.ipod-wheel').getBoundingClientRect();
    return Math.atan2(event.clientY - (rect.top + rect.height / 2), event.clientX - (rect.left + rect.width / 2)) * 180 / Math.PI;
  }

  function normalizedDelta(next, previous) {
    let delta = next - previous;
    if (delta > 180) delta -= 360;
    if (delta < -180) delta += 360;
    return delta;
  }

  function stepRotation(delta) {
    dragTravel += delta;
    if (Math.abs(dragTravel) < 18) return;
    navigate(dragTravel > 0 ? 'down' : 'up');
    dragTravel = 0;
  }

  function buildController() {
    const node = document.createElement('aside');
    node.id = 'ipod-controls';
    node.className = 'ipod-controls';
    node.dataset.inspiredController = '';
    node.setAttribute('aria-label', 'Now Playing click wheel controller');
    node.hidden = true;
    node.innerHTML = `
      <div class="ipod-screen" aria-hidden="true">
        <span>trio</span><strong>Now Playing</strong><i></i>
      </div>
      <div class="ipod-wheel" role="group" aria-label="${ACTION_LABELS.wheel}" tabindex="0">
        <button type="button" class="ipod-menu" data-ipod-action="menu" aria-label="${ACTION_LABELS.menu}">MENU</button>
        <button type="button" class="ipod-next" data-ipod-action="next" aria-label="${ACTION_LABELS.next}">▶▶</button>
        <button type="button" class="ipod-play" data-ipod-action="play" aria-label="${ACTION_LABELS.play}">▶❚❚</button>
        <button type="button" class="ipod-prev" data-ipod-action="previous" aria-label="${ACTION_LABELS.previous}">◀◀</button>
        <button type="button" class="ipod-select" data-ipod-action="select" aria-label="${ACTION_LABELS.select}"></button>
      </div>
      <div class="ipod-legend" aria-hidden="true">ROTATE TO MOVE · CENTER TO SELECT</div>`;

    node.addEventListener('click', event => {
      const button = event.target.closest('[data-ipod-action]');
      if (button) navigate(button.dataset.ipodAction);
    });
    node.querySelector('.ipod-wheel').addEventListener('wheel', event => {
      event.preventDefault();
      wheelTravel += event.deltaY || event.deltaX;
      if (Math.abs(wheelTravel) < 20) return;
      navigate(wheelTravel > 0 ? 'down' : 'up');
      wheelTravel = 0;
    }, { passive: false });
    node.querySelector('.ipod-wheel').addEventListener('keydown', event => {
      const map = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'previous', ArrowRight: 'next', Enter: 'select', ' ': 'select', Escape: 'menu' };
      if (!map[event.key]) return;
      event.preventDefault();
      navigate(map[event.key]);
    });
    node.querySelector('.ipod-wheel').addEventListener('pointerdown', event => {
      if (event.target.closest('button')) return;
      dragAngle = angleFor(event);
      dragTravel = 0;
      event.currentTarget.setPointerCapture?.(event.pointerId);
    });
    node.querySelector('.ipod-wheel').addEventListener('pointermove', event => {
      if (dragAngle === null) return;
      const next = angleFor(event);
      stepRotation(normalizedDelta(next, dragAngle));
      dragAngle = next;
    });
    const finishDrag = () => { dragAngle = null; dragTravel = 0; };
    node.querySelector('.ipod-wheel').addEventListener('pointerup', finishDrag);
    node.querySelector('.ipod-wheel').addEventListener('pointercancel', finishDrag);
    return node;
  }

  function sync() {
    if (!controller) return;
    const active = isIpodTheme(document.documentElement.dataset.theme);
    controller.hidden = !active;
    document.body?.classList.toggle('ipod-controller-on', active);
  }

  function mount() {
    if (!controller) {
      controller = buildController();
      document.body.append(controller);
    }
    preferenceListener = sync;
    Trio.events?.addEventListener?.('preferences:changed', preferenceListener);
    sync();
  }

  function unmount() {
    if (preferenceListener) Trio.events?.removeEventListener?.('preferences:changed', preferenceListener);
    controller?.remove();
    controller = preferenceListener = null;
    document.body?.classList.remove('ipod-controller-on');
  }

  Trio.ipodControls = { mount, unmount, navigate, isIpodTheme, normalizedDelta, actionLabels: ACTION_LABELS };
})();
