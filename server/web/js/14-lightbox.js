/* Trio.lightbox — a shared, gallery-capable image lightbox.
 *
 * Trio.lightbox.open(items, startIndex) where items = [{ url, alt }].
 * One image or many; when many, left/right arrows (and ArrowLeft/Right keys)
 * page through them. Every view supports zoom in/out (buttons, +/- keys, and
 * the scroll wheel) and drag-to-pan once zoomed. The close button, Escape,
 * and a click on the backdrop all dismiss it — the last two come free from
 * the native <dialog> element (configureDialog wires backdrop-click).
 *
 * Both the message list (11-conversation) and the composer upload preview
 * (12-composer) call this so there is ONE lightbox implementation, not two
 * drifting copies.
 */
(function () {
  const NS = (window.Trio = window.Trio || {});

  const MIN_SCALE = 1;
  const MAX_SCALE = 6;
  const STEP = 0.4;

  let dialog = null;
  let imgEl = null;
  let errEl = null;
  let counterEl = null;
  let zoomLabel = null;
  let prevBtn = null;
  let nextBtn = null;

  let items = [];
  let index = 0;
  // Per-view transform state.
  let scale = 1, tx = 0, ty = 0;
  // Drag-to-pan state. pointerMoved distinguishes a pan from a click so a
  // pan-release doesn't trigger click-to-zoom.
  let dragging = false, pointerMoved = false;
  const pointers = new Map();
  let pinch = null;
  let dragStartX = 0, dragStartY = 0, panStartX = 0, panStartY = 0;

  function build() {
    dialog = document.getElementById('trio-lightbox');
    if (dialog && dialog.dataset.enhanced === '1') return;
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.id = 'trio-lightbox';
      document.body.append(dialog);
    }
    dialog.className = 'lightbox';
    dialog.dataset.enhanced = '1';
    dialog.replaceChildren();

    const stage = document.createElement('div');
    stage.className = 'lightbox-stage';
    imgEl = document.createElement('img');
    imgEl.className = 'lightbox-img';
    imgEl.alt = '';
    imgEl.draggable = false;
    errEl = document.createElement('div');
    errEl.className = 'lightbox-error';
    errEl.textContent = 'Image couldn’t load';
    errEl.hidden = true;
    stage.append(imgEl, errEl);
    // A broken/expired image would otherwise be a silent void with working
    // chrome — show a message and hide the broken glyph instead.
    imgEl.addEventListener('error', () => { imgEl.hidden = true; errEl.hidden = false; });
    imgEl.addEventListener('load', () => { errEl.hidden = true; imgEl.hidden = false; });

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'modal-close lightbox-close';
    close.setAttribute('aria-label', 'Close');
    close.textContent = '×';
    close.addEventListener('click', () => dialog.close());

    prevBtn = navButton('prev', 'Previous image', '‹', -1);
    nextBtn = navButton('next', 'Next image', '›', 1);

    const toolbar = document.createElement('div');
    toolbar.className = 'lightbox-toolbar';
    const out = toolBtn('Zoom out', '−', () => zoomBy(-STEP));
    zoomLabel = document.createElement('button');
    zoomLabel.type = 'button';
    zoomLabel.className = 'lightbox-tool lightbox-zoom-label';
    zoomLabel.setAttribute('aria-label', 'Reset zoom');
    zoomLabel.textContent = '100%';
    zoomLabel.addEventListener('click', resetZoom);
    const inn = toolBtn('Zoom in', '+', () => zoomBy(STEP));
    toolbar.append(out, zoomLabel, inn);

    counterEl = document.createElement('div');
    counterEl.className = 'lightbox-counter';

    dialog.append(stage, close, prevBtn, nextBtn, toolbar, counterEl);

    if (NS.ui && NS.ui.configureDialog) NS.ui.configureDialog(dialog);
    dialog.addEventListener('keydown', onKeydown);
    // Clear per-session state on close so a drag interrupted by Escape can't
    // leave `dragging` true and pan the next image on a bare pointermove.
    dialog.addEventListener('close', () => {
      items = []; clearGesture(); pointerMoved = false;
      imgEl.classList.remove('dragging');
    });
    imgEl.addEventListener('wheel', onWheel, { passive: false });
    imgEl.addEventListener('pointerdown', onPointerDown);
    imgEl.addEventListener('pointermove', onPointerMove);
    imgEl.addEventListener('pointerup', endDrag);
    imgEl.addEventListener('pointercancel', endDrag);
    imgEl.addEventListener('lostpointercapture', endDrag);
    // Single click toggles zoom (matches the zoom-in / grab cursor). A click
    // that was really a pan-drag is ignored via the pointerMoved guard.
    imgEl.addEventListener('click', onImageClick);
  }

  function navButton(kind, label, glyph, dir) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lightbox-nav lightbox-' + kind;
    b.setAttribute('aria-label', label);
    b.textContent = glyph;
    b.addEventListener('click', () => go(dir));
    return b;
  }

  function toolBtn(label, glyph, fn) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lightbox-tool';
    b.setAttribute('aria-label', label);
    b.textContent = glyph;
    b.addEventListener('click', fn);
    return b;
  }

  function show(i) {
    if (!items.length) return;
    index = (i + items.length) % items.length;
    const it = items[index] || {};
    errEl.hidden = true; imgEl.hidden = false;   // clear any prior error state
    imgEl.src = it.url || '';
    imgEl.alt = it.alt || '';
    resetZoom();
    const multi = items.length > 1;
    prevBtn.hidden = !multi;
    nextBtn.hidden = !multi;
    counterEl.hidden = !multi;
    if (multi) counterEl.textContent = (index + 1) + ' / ' + items.length;
  }

  function go(dir) { if (items.length > 1) show(index + dir); }

  function applyTransform() {
    imgEl.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
    imgEl.classList.toggle('zoomed', scale > 1);
    if (zoomLabel) zoomLabel.textContent = Math.round(scale * 100) + '%';
  }

  function clampScale(s) { return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s)); }

  function zoomBy(delta) {
    const next = clampScale(scale + delta);
    if (next === scale) return;
    scale = next;
    if (scale === 1) { tx = 0; ty = 0; }
    applyTransform();
  }

  function resetZoom() { clearGesture(); scale = 1; tx = 0; ty = 0; applyTransform(); }

  function onWheel(e) {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? STEP : -STEP);
  }

  function clearGesture() {
    const ids = [...pointers.keys()];
    pointers.clear(); pinch = null; dragging = false;
    imgEl?.classList.remove('dragging');
    ids.forEach(id => { try { imgEl.releasePointerCapture(id); } catch (_) {} });
  }

  function onPointerDown(e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (pointers.size >= 2) return;
    if (!pointers.size) pointerMoved = false;
    pointers.set(e.pointerId, {x:e.clientX, y:e.clientY});
    try { imgEl.setPointerCapture(e.pointerId); } catch (_) {}
    if (pointers.size === 2) {
      const [a,b] = [...pointers.values()];
      const rect = imgEl.getBoundingClientRect();
      pinch = {distance:Math.max(1, Math.hypot(b.x-a.x,b.y-a.y)), scale, tx, ty,
        x:(a.x+b.x)/2, y:(a.y+b.y)/2,
        cx:rect.left+rect.width/2-tx, cy:rect.top+rect.height/2-ty};
      pointerMoved = true; dragging = false;
      imgEl.classList.add('dragging');
      return;
    }
    dragging = scale > 1;
    dragStartX = e.clientX; dragStartY = e.clientY;
    panStartX = tx; panStartY = ty;
    imgEl.classList.toggle('dragging', dragging);
  }

  function onPointerMove(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, {x:e.clientX,y:e.clientY});
    if (pinch && pointers.size === 2) {
      const [a,b] = [...pointers.values()];
      scale = clampScale(pinch.scale * Math.hypot(b.x-a.x,b.y-a.y) / pinch.distance);
      const ratio = scale / pinch.scale;
      tx = (a.x+b.x)/2 - pinch.cx - (pinch.x-pinch.cx-pinch.tx)*ratio;
      ty = (a.y+b.y)/2 - pinch.cy - (pinch.y-pinch.cy-pinch.ty)*ratio;
      if (scale === 1) { tx = 0; ty = 0; }
      applyTransform();
      return;
    }
    if (!dragging) return;
    if (Math.hypot(e.clientX-dragStartX,e.clientY-dragStartY) < 3) return;
    pointerMoved = true;
    tx = panStartX + (e.clientX - dragStartX);
    ty = panStartY + (e.clientY - dragStartY);
    applyTransform();
  }

  function endDrag(e) {
    if (!pointers.delete(e.pointerId)) return;
    pinch = null;
    try { imgEl.releasePointerCapture(e.pointerId); } catch (_) {}
    const remaining = [...pointers.values()][0];
    dragging = !!remaining && scale > 1;
    if (remaining) {
      dragStartX = remaining.x; dragStartY = remaining.y;
      panStartX = tx; panStartY = ty;
    }
    imgEl.classList.toggle('dragging', dragging);
  }

  // Single click toggles zoom: 1x → a comfortable 2.5x, otherwise back to fit.
  // Ignored when the "click" was actually a pan (pointerMoved), so panning
  // never snaps the zoom.
  function onImageClick() {
    if (pointerMoved) return;
    if (scale > 1) { resetZoom(); }
    else { scale = 2.5; tx = 0; ty = 0; applyTransform(); }
  }

  function onKeydown(e) {
    switch (e.key) {
      case 'ArrowLeft': e.preventDefault(); go(-1); break;
      case 'ArrowRight': e.preventDefault(); go(1); break;
      case '+': case '=': e.preventDefault(); zoomBy(STEP); break;
      case '-': case '_': e.preventDefault(); zoomBy(-STEP); break;
      case '0': e.preventDefault(); resetZoom(); break;
      // Escape falls through to the native <dialog> close.
    }
  }

  /* Open the lightbox on a list of images at startIndex. Accepts a single
   * {url, alt} object too, for the common one-image case. */
  function open(list, startIndex) {
    const arr = Array.isArray(list) ? list : [list];
    items = arr.filter(it => it && it.url);
    if (!items.length) return;
    build();
    dragging = false; pointerMoved = false;   // never inherit stale drag state
    show(Math.max(0, Math.min(startIndex || 0, items.length - 1)));
    if (!dialog.open) dialog.showModal();
  }

  NS.lightbox = { open };
})();
