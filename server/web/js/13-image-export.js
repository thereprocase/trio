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
  // A standalone SVG loses page CSS and viewBox-only intrinsic dimensions.
  // Snapshot presentation at click time, while leaving the original untouched.
  function svgURL(source) {
    const clone = source.cloneNode(true), box = source.viewBox.baseVal;
    const explicit = axis => {
      const attr = source.getAttribute(axis);
      return attr && !attr.includes('%') ? source[axis].baseVal.value : 0;
    };
    const w = explicit('width') || box.width || 300;
    const h = explicit('height') || box.height || 150;
    clone.setAttribute('width', String(w)); clone.setAttribute('height', String(h));
    const properties = ['color','fill','fill-opacity','fill-rule','stroke','stroke-width',
      'stroke-opacity','stroke-dasharray','stroke-dashoffset','stroke-linecap','stroke-linejoin',
      'opacity','font-family','font-size','font-weight','font-style','letter-spacing',
      'word-spacing','text-anchor','dominant-baseline','visibility','display','paint-order',
      'text-decoration','vector-effect','clip-path','mask','filter'];
    const originals = [source, ...source.querySelectorAll('*')];
    const copies = [clone, ...clone.querySelectorAll('*')];
    originals.forEach((node, i) => {
      const style = getComputedStyle(node);
      for (const prop of properties) {
        let value = style.getPropertyValue(prop);
        // Computed local fragment URLs must remain local in the standalone SVG.
        value = value.replace(/url\(["']?[^)"']*#([^)'" ]+)["']?\)/g, 'url(#$1)');
        if (value) copies[i].style.setProperty(prop, value);
      }
    });
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(clone));
  }
  async function jpeg(source, name) {
    let image = source;
    if (typeof source === 'string' || source.localName === 'svg') {
      image = new Image();
      const url = typeof source === 'string' ? source :
        svgURL(source);
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
  function start() {
    enhance(document);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => enhance(document), {once:true});
    // Also cover charts/figures inserted by asynchronous page scripts.
    new MutationObserver(records => {
      if (records.some(r => [...r.addedNodes].some(n => n.nodeType === 1 &&
          (n.matches?.('img,svg,canvas') || n.querySelector?.('img,svg,canvas'))))) enhance(document);
    }).observe(document.documentElement, {childList:true, subtree:true});
  }
  window.TrioImageExport = {button, jpeg, enhance, start};
})();
