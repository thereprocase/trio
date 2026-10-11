/* Shared image downloads for the dashboard and sandboxed hosted pages. */
(() => {
  'use strict';
  const MAX_PIXELS = 24000000, MAX_EDGE = 8192;
  function filename(name, extension) {
    return (String(name || 'diagram').replace(/\.[a-z0-9]{1,8}$/i, '').replace(/[^a-z0-9._-]+/gi, '-').slice(0, 100) || 'diagram') + extension;
  }
  function save(blob, name) {
    const url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  async function jpeg(source, name) {
    let image = source;
    if (typeof source === 'string' || source.localName === 'svg') {
      image = new Image();
      const url = typeof source === 'string' ? source :
        'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(source));
      image.src = url;
    }
    if (image.localName !== 'canvas') await image.decode();
    const w = image.naturalWidth || image.width, h = image.naturalHeight || image.height;
    if (!(w > 0 && h > 0)) throw new Error('Image has no dimensions');
    const factor = Math.min(1, MAX_EDGE / w, MAX_EDGE / h, Math.sqrt(MAX_PIXELS / (w * h)));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(w * factor)); canvas.height = Math.max(1, Math.round(h * factor));
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('JPEG export unavailable');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.95));
    canvas.width = canvas.height = 1;
    if (!blob) throw new Error('JPEG export failed');
    save(blob, filename(name, '.jpg'));
    return factor < 1;
  }
  function button(source, name) {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = 'Download JPEG';
    b.addEventListener('click', async event => {
      event.preventDefault(); event.stopPropagation(); b.disabled = true; b.textContent = 'Preparing JPEG…';
      try { b.textContent = await jpeg(source, name) ? 'JPEG saved (resized)' : 'JPEG saved'; }
      catch (_) { b.textContent = 'Export failed — try original'; }
      finally { b.disabled = false; }
    });
    return b;
  }
  function enhance(root) {
    root.querySelectorAll('img,svg,canvas').forEach((el, i) => {
      if (el.closest('svg') !== (el.localName === 'svg' ? el : null) || el.dataset.quartetExport) return;
      el.dataset.quartetExport = '1';
      const name = el.getAttribute('alt') || el.getAttribute('aria-label') || document.title + '-' + (i + 1);
      const bar = document.createElement('div'); bar.className = 'quartet-image-downloads';
      bar.append(button(el, name));
      const original = document.createElement('a'); original.textContent = 'Download original';
      if (el.localName === 'svg') {
        original.href = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(el));
        original.download = filename(name, '.svg');
      } else if (el.localName === 'canvas') {
        original.href = '#'; original.download = filename(name, '.png');
        original.addEventListener('click', event => { event.preventDefault(); el.toBlob(blob => { if (blob) save(blob, original.download); }); });
      } else {
        original.href = el.currentSrc || el.src;
        const mime = original.href.match(/^data:image\/([a-z0-9+.-]+)[;,]/i)?.[1];
        original.download = filename(name, '.' + ({jpeg:'jpg','svg+xml':'svg'}[mime] || mime || 'png'));
      }
      bar.append(original); el.after(bar);
    });
  }
  window.TrioImageExport = {button, jpeg, enhance};
})();
